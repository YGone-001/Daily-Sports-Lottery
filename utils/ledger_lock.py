"""本机合作账本锁：同一截止时间、有界线程/OS 等待、明确所有权。"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

LOCK_POLICY_VERSION = "local-ledger-lock-v1"


class LedgerLockError(OSError):
    def __init__(self, code, diagnostics, cause=None):
        super().__init__(getattr(cause, "errno", None) or errno.EBUSY, code)
        self.rejection_codes = (code,)
        self.diagnostics = dict(diagnostics, cause_errno=getattr(cause, "errno", None),
            cause_winerror=getattr(cause, "winerror", None))


def validate_lock_path(path):
    raw = os.fspath(path)
    if not Path(raw).is_absolute() or ".." in Path(raw).parts or raw.startswith("\\\\") or ":" in Path(raw).name:
        raise LedgerLockError("LEDGER_LOCK_PATH_INVALID", dict(phase="path", policy=LOCK_POLICY_VERSION))
    for component in (Path(raw), *Path(raw).parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise LedgerLockError("LEDGER_LOCK_PATH_ALIAS", dict(phase="path", policy=LOCK_POLICY_VERSION))
    return os.path.normcase(os.path.abspath(raw))


class BoundedLedgerLock:
    _thread_guards = {}
    _thread_guards_lock = threading.Lock()
    _local = threading.local()

    def __init__(self, path, *, timeout=120.0, poll_interval=0.05):
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("finite positive timeout and poll interval required")
        self.path = validate_lock_path(path)
        self.timeout, self.poll_interval = timeout, poll_interval
        self._fd, self._guard = None, None
        self._thread_owned, self._os_owned = False, False
        self.diagnostics = dict(policy=LOCK_POLICY_VERSION, phase="not-started", attempts=0,
            elapsed_seconds=0.0, platform=os.name, byte_offset=0, byte_count=1, errno=None, winerror=None)

    def _error(self, code, cause=None):
        self.diagnostics["elapsed_seconds"] = time.monotonic() - self._started
        return LedgerLockError(code, self.diagnostics, cause)

    def __enter__(self):
        self._started = time.monotonic()
        self._deadline = self._started + self.timeout
        paths = getattr(self._local, "paths", set())
        if self._thread_owned or self.path in paths:
            self.diagnostics["phase"] = "recursion"
            raise self._error("LEDGER_LOCK_RECURSIVE")
        self.diagnostics["phase"] = "registry"
        if not BoundedLedgerLock._thread_guards_lock.acquire(timeout=max(0, self._deadline-time.monotonic())):
            raise self._error("LEDGER_LOCK_TIMEOUT")
        try:
            self._guard = BoundedLedgerLock._thread_guards.setdefault(self.path, threading.Lock())
        finally:
            BoundedLedgerLock._thread_guards_lock.release()
        self.diagnostics["phase"] = "thread"
        try:
            if not self._guard.acquire(timeout=max(0, self._deadline-time.monotonic())):
                raise self._error("LEDGER_LOCK_TIMEOUT")
            self._thread_owned = True
            self._owner = threading.get_ident()
            self._local.paths = paths
            paths.add(self.path)
            self.diagnostics["phase"] = "open"
            validate_lock_path(self.path)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            validate_lock_path(self.path)
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise self._error("LEDGER_LOCK_PATH_ALIAS")
            self.diagnostics["phase"] = "os"
            self._acquire_os_lock()
            self._os_owned = True
            # Windows permits locking beyond EOF. Initialize only while owning
            # byte zero, so racing creators never write through another lock.
            if os.fstat(self._fd).st_size == 0:
                os.write(self._fd, b"0")
            self.diagnostics["phase"] = "held"
            self.diagnostics["elapsed_seconds"] = time.monotonic() - self._started
            return self
        except BaseException as exc:
            self._cleanup()
            if isinstance(exc, OSError) and not isinstance(exc, LedgerLockError):
                raise self._error("LEDGER_LOCK_OS_ERROR", exc) from exc
            raise

    def _acquire_os_lock(self):
        while True:
            if time.monotonic() >= self._deadline:
                raise self._error("LEDGER_LOCK_TIMEOUT")
            self.diagnostics["attempts"] += 1
            try:
                self._try_os_lock()
                return
            except OSError as exc:
                self.diagnostics.update(errno=exc.errno, winerror=getattr(exc, "winerror", None))
                busy_errnos = (errno.EACCES, errno.EAGAIN, errno.EDEADLK) if os.name == "nt" else (errno.EAGAIN, errno.EWOULDBLOCK)
                expected = exc.errno in busy_errnos
                if not expected or getattr(exc, "winerror", None) not in (None, 33):
                    raise self._error("LEDGER_LOCK_OS_ERROR", exc) from exc
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise self._error("LEDGER_LOCK_TIMEOUT", exc) from exc
                time.sleep(min(self.poll_interval, remaining))

    def _try_os_lock(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_os(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _cleanup(self):
        failure = None
        try:
            if self._os_owned:
                try:
                    self._unlock_os()
                except BaseException as exc:
                    failure = exc
                finally:
                    self._os_owned = False
        finally:
            try:
                if self._fd is not None:
                    fd, self._fd = self._fd, None
                    try:
                        os.close(fd)
                    except BaseException as exc:
                        failure = failure or exc
            finally:
                if self._thread_owned:
                    self._thread_owned = False
                    self._local.paths.discard(self.path)
                    self._guard.release()
        if failure is not None:
            self.diagnostics["release_error"] = type(failure).__name__
        return failure

    def __exit__(self, exc_type, exc, tb):
        if self._thread_owned and self._owner != threading.get_ident():
            raise self._error("LEDGER_LOCK_OWNER_MISMATCH")
        failure = self._cleanup()
        if failure is not None:
            self.diagnostics["phase"] = "release"
            self.diagnostics["release_error"] = type(failure).__name__
            if exc is None:
                raise self._error("LEDGER_LOCK_RELEASE_FAILED", failure) from failure
        return False


@dataclass(frozen=True)
class VerifiedLedgerSnapshot:
    """一个已校验副本；报告字段只从同一 records 派生，不构成独立认证。"""
    records: tuple
    report_json: str

    @property
    def entries(self):
        return self.records

    @property
    def report(self):
        return json.loads(self.report_json)
