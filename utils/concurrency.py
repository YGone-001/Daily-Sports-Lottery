import errno
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import config
from utils import refresh_state

class RefreshBusyError(Exception):
    """
    Raised when the refresh coordination lock is already held.

    ``owner`` carries the writer identity recorded by the holder when it is
    available. It is **advisory**: a stale marker can outlive its process, so the
    value must never be used to decide whether a lock is free.
    """

    def __init__(self, message: str, owner: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.owner = owner


class SidecarLockBusy(Exception):
    """
    Raised when a sidecar single-writer lock could not be acquired in time.

    Sidecar locks protect **one** store's read-modify-write section (for example
    the prediction snapshot store). They never represent refresh semantics and
    must never be used to infer whether a refresh is running.
    """

    def __init__(self, lock_path: str, timeout: float) -> None:
        super().__init__(f"sidecar lock busy: {lock_path} (timeout={timeout}s)")
        self.lock_path = lock_path
        self.timeout = timeout


# ---------------------------------------------------------------------------
# OS byte-range lock primitives (shared by every single-writer section)
# ---------------------------------------------------------------------------

def _try_os_lock(fd: int) -> bool:
    """
    Attempt a non-blocking exclusive 1-byte lock. ``True`` = acquired.

    Only *busy* conditions return ``False``; any other OS error propagates so the
    caller fails closed instead of pretending to hold a lock.
    """
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if os.name == "nt" and exc.errno in (errno.EACCES, errno.EDEADLOCK):
            return False
        if os.name != "nt" and exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


def _release_os_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _open_lock_file(lock_path: str) -> int:
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    # Windows byte-range locking needs at least one byte in the file.
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    return fd


@contextmanager
def acquire_sidecar_lock(
    lock_name: str,
    *,
    data_dir: str | None = None,
    timeout: float = 5.0,
) -> Iterator[str]:
    """
    Exclusive single-writer lock for one non-refresh store's read-modify-write.

    - Cross-process and cross-platform (Windows ``msvcrt`` / POSIX ``flock``).
    - ``timeout=0`` is a single non-blocking attempt.
    - Distinguishes "busy" from real I/O errors: only busy raises
      :class:`SidecarLockBusy`; everything else propagates (fail closed).
    - The lock file is **never** deleted, so a crash can never block the next
      writer: the OS drops the byte lock when the process dies.
    """
    directory = data_dir or config.DATA_DIR
    lock_path = os.path.join(directory, lock_name)
    deadline = time.monotonic() + max(float(timeout), 0.0)
    fd = _open_lock_file(lock_path)
    acquired = False
    try:
        while True:
            if _try_os_lock(fd):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise SidecarLockBusy(lock_path, timeout) from None
            time.sleep(0.05)
        yield lock_path
    finally:
        try:
            if acquired:
                _release_os_lock(fd)
        finally:
            os.close(fd)

# Process-local thread guards per DATA_DIR
_THREAD_GUARDS = {}
_THREAD_GUARDS_LOCK = threading.Lock()

def _get_thread_guard(data_dir: str) -> threading.Lock:
    norm_path = os.path.normpath(os.path.abspath(data_dir))
    with _THREAD_GUARDS_LOCK:
        if norm_path not in _THREAD_GUARDS:
            _THREAD_GUARDS[norm_path] = threading.Lock()
        return _THREAD_GUARDS[norm_path]


def active_writer(data_dir: str | None = None) -> dict[str, Any] | None:
    """
    Read-only: identify the refresh writer currently marked IN_PROGRESS.

    Returns ``None`` when no refresh is in progress **or** when the marker is
    missing/corrupt. An unreadable marker is never treated as "free lock": the OS
    lock remains the only authority on exclusion.
    """
    directory = data_dir or config.DATA_DIR
    state = refresh_state.read_state(directory)
    if not state or state.get("status") != refresh_state.STATUS_IN_PROGRESS:
        return None
    owner = state.get("owner")
    return owner if isinstance(owner, dict) else None


class RefreshLease:
    """
    Handle returned by :func:`acquire_refresh_lock`.

    Holding the lease means holding the cross-process writer lock. ``begin()``
    and ``complete()`` additionally publish the generation marker used by
    readers to tell mixed multi-file snapshots apart. They are **optional**: a
    caller that only needs mutual exclusion (for example a production repair)
    simply never calls them, and therefore never claims refresh semantics.
    """

    def __init__(self, data_dir: str, owner: dict[str, Any]) -> None:
        self.data_dir = data_dir
        self.owner = owner
        self.generation: int | None = None

    def begin(self) -> int:
        self.generation = refresh_state.begin(self.data_dir, owner=self.owner)
        return self.generation

    def complete(self, outcome: str = refresh_state.STATUS_COMPLETED, error: str | None = None) -> dict[str, Any]:
        generation = self.generation or 0
        payload = refresh_state.complete(
            self.data_dir,
            owner=self.owner,
            generation=generation,
            outcome=outcome,
            error=error,
        )
        return payload


@contextmanager
def acquire_refresh_lock(action: str = "refresh"):
    """
    Acquires a cross-process and cross-thread lock for scraper.refresh().
    Raises RefreshBusyError if already locked.

    The OS byte-range lock on ``<DATA_DIR>/.refresh.lock`` remains the single
    source of truth for exclusion; its content is deliberately left untouched so
    that other holders of the same lock file keep working unchanged.
    """
    data_dir = config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)

    # 1. In-process thread exclusion
    thread_guard = _get_thread_guard(data_dir)
    if not thread_guard.acquire(blocking=False):
        raise RefreshBusyError(
            "Refresh is already running in this process.",
            owner=refresh_state.read_state(data_dir) or None,
        )

    fd = None
    try:
        # 2. Cross-process OS exclusion
        lockfile = os.path.join(data_dir, ".refresh.lock")
        fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o666)

        # Ensure at least 1 byte exists for Windows byte-range locking
        st = os.fstat(fd)
        if st.st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)

        # Apply OS lock (non-blocking: a second refresh must be rejected, not queued)
        if not _try_os_lock(fd):
            raise RefreshBusyError(
                "Refresh is already running in another process.",
                owner=active_writer(data_dir),
            )

        yield RefreshLease(data_dir, refresh_state.owner_record(action))

    finally:
        try:
            if fd is not None:
                _release_os_lock(fd)
                os.close(fd)
        finally:
            thread_guard.release()
