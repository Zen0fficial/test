"""Windows process-incarnation checks for reboot-safe local monotonic task leases.

Creation FILETIME comes directly from GetProcessTimes, without deriving a boot
identifier from wall time and uptime. Process handles are queried only and closed
immediately. Unknown access/errors fail closed rather than declaring an owner dead.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import errno
from functools import lru_cache
import math
import os
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Iterator


@lru_cache(maxsize=1)
def windows_api() -> Any:
    """Bind documented read-only Win32 process functions with exact pointer-width signatures."""
    if os.name != "nt":
        raise RuntimeError("Verified joint task leases require the pinned Windows shared environment")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def process_birth(pid: int | None) -> str | None:
    """Return an active process's exact creation identity, or None only for a proven absent/exited owner."""
    if pid is None or pid <= 0:
        return None
    kernel = windows_api()
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process exists for this nonzero PID.
            return None
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:  # The process object is signaled only after process exit.
            return None
        if state != 0x102:  # WAIT_TIMEOUT is the nonblocking active-process result.
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, kernel_time, user_time = [wintypes.FILETIME() for unused in range(4)]
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        value = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"win32-filetime:{value:016x}"
    finally:
        kernel.CloseHandle(handle)


def owner_alive(pid: int | None, birth: str | None) -> bool:
    """Distinguish a surviving worker from a reused PID or any owner left behind across a reboot."""
    return birth is not None and process_birth(pid) == birth


@contextmanager
def reconciliation_lock(path: Path, timeout: float = 30) -> Iterator[None]:
    """Serialize recorder reconciliation before reading its outbox, with a crash-released OS lock.

    No SQLite transaction is held during MLflow I/O. Windows releases the byte
    range lock when its owning handle/process closes; a killed reconciler cannot
    leave a permanent lock file owner. Unknown I/O failures remain explicit.
    """
    if os.name != "nt":
        raise RuntimeError("Verified recorder reconciliation requires the pinned Windows runtime")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Reconciliation lock timeout must be finite and nonnegative")
    import msvcrt

    with path.open("a+b") as handle:
        # Locking beyond EOF is supported. Avoid writing a marker while another
        # process owns the first byte; the persistent file itself contains no data.
        deadline = monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if monotonic() >= deadline:
                    raise TimeoutError("Another local process is reconciling this task pool") from error
                sleep(min(.05, max(0, deadline - monotonic())))
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
