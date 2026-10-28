"""固定起点 6146ff1 的原始锁源码；仅供跨平台基线子进程探针。

不是运行期锁，也不导入生产调用方。源码摘自修改前 inspect.getsource，保持字节内容。
"""
import hashlib

LEGACY_LOCK_SOURCE = "class _ledger_lock:\n    \"\"\"\n    账本专用的跨进程 + 跨线程互斥锁。\n\n    刻意**不复用** ``utils/concurrency.acquire_refresh_lock``：后者绑定\n    ``config.DATA_DIR`` 且属于刷新锁语义，二者不得相互影响。\n    \"\"\"\n\n    _thread_guards: dict[str, Any] = {}\n    _thread_guards_lock = None\n\n    def __init__(self, lock_path: str) -> None:\n        self._lock_path = lock_path\n        self._fd: int | None = None\n        self._guard = None\n\n    def __enter__(self):\n        import threading\n\n        if _ledger_lock._thread_guards_lock is None:\n            _ledger_lock._thread_guards_lock = threading.Lock()\n        with _ledger_lock._thread_guards_lock:\n            guard = _ledger_lock._thread_guards.get(self._lock_path)\n            if guard is None:\n                guard = threading.Lock()\n                _ledger_lock._thread_guards[self._lock_path] = guard\n        self._guard = guard\n        self._guard.acquire()\n\n        directory = os.path.dirname(self._lock_path) or \".\"\n        os.makedirs(directory, exist_ok=True)\n        self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)\n        st = os.fstat(self._fd)\n        if st.st_size == 0:\n            os.write(self._fd, b\"0\")\n        os.lseek(self._fd, 0, os.SEEK_SET)\n\n        try:\n            self._acquire_os_lock()\n        except BaseException:\n            os.close(self._fd)\n            self._fd = None\n            self._guard.release()\n            raise\n        return self\n\n    def _acquire_os_lock(self) -> None:\n        \"\"\"获取 OS 级排他锁；以截止时间为界重试，超时才放弃。\"\"\"\n        if os.name == \"nt\":\n            import msvcrt\n\n            deadline = time.monotonic() + LEDGER_LOCK_TIMEOUT_SECONDS\n            while True:\n                os.lseek(self._fd, 0, os.SEEK_SET)\n                try:\n                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)\n                    return\n                except OSError:\n                    if time.monotonic() >= deadline:\n                        raise\n                    time.sleep(LEDGER_LOCK_POLL_SECONDS)\n        else:\n            import fcntl\n\n            fcntl.flock(self._fd, fcntl.LOCK_EX)\n\n    def __exit__(self, exc_type, exc, tb):\n        try:\n            if self._fd is not None:\n                try:\n                    if os.name == \"nt\":\n                        import msvcrt\n\n                        os.lseek(self._fd, 0, os.SEEK_SET)\n                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)\n                    else:\n                        import fcntl\n\n                        fcntl.flock(self._fd, fcntl.LOCK_UN)\n                except OSError:\n                    pass\n                finally:\n                    os.close(self._fd)\n                    self._fd = None\n        finally:\n            if self._guard is not None:\n                self._guard.release()\n        return False\n"
LEGACY_LOCK_SHA256 = "5973d08e67e7ebb4f6b13ab8e0d4db839f98066e5f4bfb94f8167f87870ccf78"


def legacy_lock_namespace():
    if hashlib.sha256(LEGACY_LOCK_SOURCE.encode()).hexdigest() != LEGACY_LOCK_SHA256:
        raise RuntimeError("pinned baseline source changed")
    import os
    import time
    from typing import Any
    namespace = dict(os=os, time=time, Any=Any, LEDGER_LOCK_TIMEOUT_SECONDS=0.1, LEDGER_LOCK_POLL_SECONDS=0.05)
    exec(LEGACY_LOCK_SOURCE, namespace)
    return namespace
