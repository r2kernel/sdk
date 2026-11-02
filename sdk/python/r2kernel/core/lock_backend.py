from __future__ import annotations

import os
from dataclasses import dataclass
import ctypes
from typing import Protocol


class LockHandle(Protocol):
    name: str | None


class LockBackend(Protocol):
    def create(self, name: str) -> LockHandle | None: ...
    def open(self, name: str) -> LockHandle | None: ...
    def close(self, handle: LockHandle) -> None: ...
    def unlink(self, name: str) -> None: ...
    def try_acquire(self, handle: LockHandle) -> bool: ...
    def wait(self, handle: LockHandle, timeout_s: float | None = None) -> bool: ...
    def release(self, handle: LockHandle) -> None: ...


@dataclass
class _PosixHandle:
    sem: object
    name: str | None


class PosixSemaphoreBackend:
    def __init__(self, posix_ipc_module):
        self._posix = posix_ipc_module

    def create(self, name: str) -> _PosixHandle | None:
        try:
            sem = self._posix.Semaphore(name, flags=self._posix.O_CREAT, initial_value=0)
            return _PosixHandle(sem=sem, name=name)
        except Exception:
            return None

    def open(self, name: str) -> _PosixHandle | None:
        try:
            sem = self._posix.Semaphore(name)
            return _PosixHandle(sem=sem, name=name)
        except Exception:
            return None

    def close(self, handle: _PosixHandle) -> None:
        try:
            handle.sem.close()
        except Exception:
            pass

    def unlink(self, name: str) -> None:
        try:
            sem = self._posix.Semaphore(name)
            sem.unlink()
            sem.close()
        except Exception:
            pass

    def try_acquire(self, handle: _PosixHandle) -> bool:
        try:
            handle.sem.acquire(timeout=0)
            return True
        except Exception:
            return False

    def wait(self, handle: _PosixHandle, timeout_s: float | None = None) -> bool:
        try:
            if timeout_s is None:
                handle.sem.acquire()
            else:
                handle.sem.acquire(timeout=max(0.0, float(timeout_s)))
            return True
        except Exception:
            return False

    def release(self, handle: _PosixHandle) -> None:
        try:
            handle.sem.release()
        except Exception:
            pass


@dataclass
class _WinHandle:
    handle: int
    name: str | None


class Win32SemaphoreBackend:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wintypes = wintypes

        self._kernel32.CreateSemaphoreW.argtypes = [wintypes.LPVOID, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR]
        self._kernel32.CreateSemaphoreW.restype = wintypes.HANDLE
        self._kernel32.OpenSemaphoreW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.OpenSemaphoreW.restype = wintypes.HANDLE
        self._kernel32.ReleaseSemaphore.argtypes = [wintypes.HANDLE, wintypes.LONG, wintypes.LPVOID]
        self._kernel32.ReleaseSemaphore.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD

        self._SEMAPHORE_ALL_ACCESS = 0x1F0003
        self._WAIT_OBJECT_0 = 0x00000000
        self._WAIT_TIMEOUT = 0x00000102
        self._INFINITE = 0xFFFFFFFF

    def create(self, name: str) -> _WinHandle | None:
        handle = self._kernel32.CreateSemaphoreW(None, 0, 0x7FFFFFFF, name)
        if not handle:
            return None
        return _WinHandle(handle=int(handle), name=name)

    def open(self, name: str) -> _WinHandle | None:
        handle = self._kernel32.OpenSemaphoreW(self._SEMAPHORE_ALL_ACCESS, False, name)
        if not handle:
            return None
        return _WinHandle(handle=int(handle), name=name)

    def close(self, handle: _WinHandle) -> None:
        try:
            self._kernel32.CloseHandle(handle.handle)
        except Exception:
            pass

    def unlink(self, name: str) -> None:
        # Windows named semaphores are released when all handles are closed.
        return None

    def try_acquire(self, handle: _WinHandle) -> bool:
        result = self._kernel32.WaitForSingleObject(handle.handle, 0)
        return result == self._WAIT_OBJECT_0

    def wait(self, handle: _WinHandle, timeout_s: float | None = None) -> bool:
        if timeout_s is None:
            timeout_ms = self._INFINITE
        else:
            timeout_ms = max(0, int(float(timeout_s) * 1000.0))
        result = self._kernel32.WaitForSingleObject(handle.handle, timeout_ms)
        return result == self._WAIT_OBJECT_0

    def release(self, handle: _WinHandle) -> None:
        try:
            self._kernel32.ReleaseSemaphore(handle.handle, 1, None)
        except Exception:
            pass


class NullSemaphoreBackend:
    def create(self, name: str):
        return None

    def open(self, name: str):
        return None

    def close(self, handle) -> None:
        return None

    def unlink(self, name: str) -> None:
        return None

    def try_acquire(self, handle) -> bool:
        return False

    def wait(self, handle, timeout_s: float | None = None) -> bool:
        return False

    def release(self, handle) -> None:
        return None


_BACKEND: LockBackend | None = None


def get_lock_backend() -> LockBackend:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if os.getenv("R2KERNEL_FORCE_PYTHON") != "1":
        try:
            from r2kernel.ffi import R2KernelCore  # type: ignore
            _BACKEND = RustLockBackend(R2KernelCore())
            return _BACKEND
        except Exception:
            pass
    if os.name == "nt":
        _BACKEND = Win32SemaphoreBackend()
        return _BACKEND
    try:
        import posix_ipc  # type: ignore
    except Exception:
        _BACKEND = NullSemaphoreBackend()
        return _BACKEND
    _BACKEND = PosixSemaphoreBackend(posix_ipc)
    return _BACKEND


@dataclass
class _RustHandle:
    handle: object
    name: str | None


class RustLockBackend:
    def __init__(self, core):
        self._core = core
        self._lib = core.lib

    def create(self, name: str) -> _RustHandle | None:
        try:
            out = ctypes.c_void_p()
            rc = self._lib.r2kernel_lock_create(name.encode("utf-8"), ctypes.byref(out))
            if rc != 0 or not out.value:
                return None
            return _RustHandle(handle=out, name=name)
        except Exception:
            return None

    def open(self, name: str) -> _RustHandle | None:
        try:
            out = ctypes.c_void_p()
            rc = self._lib.r2kernel_lock_open(name.encode("utf-8"), ctypes.byref(out))
            if rc != 0 or not out.value:
                return None
            return _RustHandle(handle=out, name=name)
        except Exception:
            return None

    def close(self, handle: _RustHandle) -> None:
        try:
            self._lib.r2kernel_lock_close(handle.handle)
        except Exception:
            pass

    def unlink(self, name: str) -> None:
        try:
            self._lib.r2kernel_lock_unlink(name.encode("utf-8"))
        except Exception:
            pass

    def try_acquire(self, handle: _RustHandle) -> bool:
        try:
            rc = self._lib.r2kernel_lock_trywait(handle.handle)
            return rc == 0
        except Exception:
            return False

    def wait(self, handle: _RustHandle, timeout_s: float | None = None) -> bool:
        try:
            timeout_ms = -1 if timeout_s is None else max(0, int(float(timeout_s) * 1000.0))
            rc = self._lib.r2kernel_lock_wait(handle.handle, timeout_ms)
            return rc == 0
        except Exception:
            return False

    def release(self, handle: _RustHandle) -> None:
        try:
            self._lib.r2kernel_lock_post(handle.handle)
        except Exception:
            pass
