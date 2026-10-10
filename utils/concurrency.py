import os
import threading
from contextlib import contextmanager
import config

class RefreshBusyError(Exception):
    """Raised when the refresh coordination lock is already held."""
    pass

# Process-local thread guards per DATA_DIR
_THREAD_GUARDS = {}
_THREAD_GUARDS_LOCK = threading.Lock()

def _get_thread_guard(data_dir: str) -> threading.Lock:
    norm_path = os.path.normpath(os.path.abspath(data_dir))
    with _THREAD_GUARDS_LOCK:
        if norm_path not in _THREAD_GUARDS:
            _THREAD_GUARDS[norm_path] = threading.Lock()
        return _THREAD_GUARDS[norm_path]

@contextmanager
def acquire_refresh_lock():
    """
    Acquires a cross-process and cross-thread lock for scraper.refresh().
    Raises RefreshBusyError if already locked.
    """
    data_dir = config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    
    # 1. In-process thread exclusion
    thread_guard = _get_thread_guard(data_dir)
    if not thread_guard.acquire(blocking=False):
        raise RefreshBusyError("Refresh is already running in this process.")

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

        # Apply OS lock
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # On Windows, EACCES (13) or EDEADLOCK (36) indicates busy. On POSIX, EWOULDBLOCK/EAGAIN.
            import errno
            if os.name == "nt" and e.errno in (errno.EACCES, errno.EDEADLOCK):
                raise RefreshBusyError("Refresh is already running in another process.") from None
            elif os.name != "nt" and e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise RefreshBusyError("Refresh is already running in another process.") from None
            raise # Re-raise unexpected I/O errors

        yield

    finally:
        try:
            if fd is not None:
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
                finally:
                    os.close(fd)
        finally:
            thread_guard.release()
