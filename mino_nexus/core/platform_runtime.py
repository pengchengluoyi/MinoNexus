"""Cross-platform local filesystem and cooperative user-process service adapter.

No PID-based killing, privileged service installation, or additional listener port.
A lifetime OS lock proves exit; stop requests are consumed by the native app.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import time


def executable(path):
    return Path(str(path) + '.exe') if os.name == 'nt' else Path(path)


def sync_directory(path):
    if os.name == 'nt':
        return  # Windows flushes the file before atomic replace; directories cannot be fsynced.
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _windows_lock(fd, *, shared, blocking):
    """CRT LK_NBRLCK is exclusive too; use native shared byte-range locks."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    overlap = OVERLAPPED()
    flags = 1 | (0 if shared else 2)  # FAIL_IMMEDIATELY, optional EXCLUSIVE_LOCK
    while not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
        error = ctypes.get_last_error()
        if error != 33:  # ERROR_LOCK_VIOLATION; access errors are not contention.
            raise ctypes.WinError(error)
        if not blocking:
            raise BlockingIOError('Lock is held')
        time.sleep(.05)

    def release():
        if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
            raise ctypes.WinError(ctypes.get_last_error())
    return release


@contextmanager
def file_lock(path, shared=False, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Lock file cannot be a link')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    release = None
    try:
        if os.name == 'nt':
            release = _windows_lock(fd, shared=shared, blocking=blocking)
        else:
            import fcntl
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        try:
            if release is not None:
                release()
        finally:
            os.close(fd)
