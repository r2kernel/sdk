from __future__ import annotations
import struct
import os
import ctypes
import fcntl
import threading
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing import shared_memory, resource_tracker


HEADER_SEQ_INDEX = 0
HEADER_LEN_INDEX = 8
HEADER_GEN_INDEX = 16
HEADER_TS_INDEX = 32
HEADER_PREV_TS_INDEX = 40
HEADER_PUBLISHED_TS_INDEX = 48
PAYLOAD_OFFSET = 64

SEQ_READY = 0
SEQ_WRITING = 1


class _Flock(ctypes.Structure):
    # Linux x86_64/aarch64 struct flock, including native trailing padding.
    _fields_ = [("type", ctypes.c_short), ("whence", ctypes.c_short),
                ("start", ctypes.c_int64), ("length", ctypes.c_int64),
                ("pid", ctypes.c_int)]


class _NamedShmAccess:
    """Slot-owned Linux OFD exclusion; never reuse a handle after fork."""

    def __init__(self, fd: int, slot_size: int, slot_count: int):
        if not sys.platform.startswith("linux") or os.uname().machine not in {"x86_64", "aarch64"}:
            raise RuntimeError("Named SHM OFD locks require Linux x86_64/aarch64")
        if not all(hasattr(fcntl, name) for name in ("F_OFD_SETLKW", "F_OFD_SETLK")):
            raise RuntimeError("Named SHM requires Linux OFD lock support")
        if slot_size < PAYLOAD_OFFSET or slot_count <= 0 or slot_size * slot_count > (1 << 63) - 1:
            raise ValueError("invalid SHM slot geometry or off_t range")
        self._fd = fd
        self._slot_size = slot_size
        self._mutexes = [threading.Lock() for _ in range(slot_count)]

    @contextmanager
    def __call__(self, slot_index: int):
        if not 0 <= slot_index < len(self._mutexes):
            raise IndexError("SHM slot out of range")
        with self._mutexes[slot_index]:
            lock = _Flock(fcntl.F_WRLCK, os.SEEK_SET, slot_index * self._slot_size, self._slot_size, 0)
            # Unsupported filesystem/kernel errors propagate, never change domains.
            fcntl.fcntl(self._fd, fcntl.F_OFD_SETLKW, bytes(lock))
            try:
                yield
            finally:
                lock.type = fcntl.F_UNLCK
                fcntl.fcntl(self._fd, fcntl.F_OFD_SETLK, bytes(lock))


@dataclass(frozen=True)
class SlotSnapshot:
    seq: int
    length: int
    generation: int
    timestamp: int
    prev_timestamp: int
    published_timestamp: int
    payload: bytes | None = None


class _PyTopicBufferPool:
    def __init__(self, *, name: str, slot_size: int, slot_count: int):
        if slot_size < PAYLOAD_OFFSET or slot_count <= 0 or slot_size * slot_count > (1 << 63) - 1:
            raise ValueError("invalid SHM slot geometry")
        self.name = name
        self.slot_size = slot_size
        self.slot_count = slot_count
        self.buffer_size = slot_size * slot_count
        self._shm = shared_memory.SharedMemory(name=name, create=False, size=self.buffer_size)
        resource_tracker.unregister(f"/{self._shm.name}", "shared_memory")
        try:
            if self._shm.size != self.buffer_size:
                raise ValueError("SHM size does not match slot geometry")
            self._access = _NamedShmAccess(self._shm._fd, slot_size, slot_count)
        except Exception:
            self._shm.close()
            raise

    def close(self):
        self._shm.close()

    def unlink(self):
        self._shm.unlink()

    def _ensure_buffer(self, slot_index: int, length: int) -> None:
        if not 0 <= slot_index < self.slot_count or not 0 <= length <= min(self.slot_size - PAYLOAD_OFFSET, 0xffffffff):
            raise IndexError("slot payload out of range")

    def publish(self, slot_index: int, data: bytes, generation: int, timestamp: int, published_timestamp: int):
        if not isinstance(data, bytes):
            raise TypeError("SHM payload must be bytes")
        self._ensure_buffer(slot_index, len(data))
        # Validate all foreign values before changing the mapped slot.
        metadata = struct.pack("<Qqq", generation, int(timestamp), int(published_timestamp))
        with self._access(slot_index):
            start = slot_index * self.slot_size
            previous = struct.unpack_from("<q", self._shm.buf, start + HEADER_TS_INDEX)[0]
            self._shm.buf[start] = SEQ_WRITING
            self._shm.buf[start + PAYLOAD_OFFSET:start + PAYLOAD_OFFSET + len(data)] = data
            struct.pack_into("<I", self._shm.buf, start + HEADER_LEN_INDEX, len(data))
            self._shm.buf[start + HEADER_GEN_INDEX:start + HEADER_GEN_INDEX + 8] = metadata[:8]
            struct.pack_into("<q", self._shm.buf, start + HEADER_PREV_TS_INDEX, previous)
            self._shm.buf[start + HEADER_TS_INDEX:start + HEADER_TS_INDEX + 8] = metadata[8:16]
            self._shm.buf[start + HEADER_PUBLISHED_TS_INDEX:start + HEADER_PUBLISHED_TS_INDEX + 8] = metadata[16:]
            self._shm.buf[start] = SEQ_READY

    def reset_slot(self, slot_index: int):
        self._ensure_buffer(slot_index, 0)
        with self._access(slot_index):
            start = slot_index * self.slot_size
            self._shm.buf[start:start + PAYLOAD_OFFSET] = bytes(PAYLOAD_OFFSET)

    def snapshot(self, slot_index: int, *, expected_generation: int | None = None, include_payload: bool = True) -> SlotSnapshot | None:
        self._ensure_buffer(slot_index, 0)
        if expected_generation is not None:
            struct.pack("<Q", expected_generation)
        with self._access(slot_index):
            start = slot_index * self.slot_size
            seq = self._shm.buf[start]
            generation = struct.unpack_from("<Q", self._shm.buf, start + HEADER_GEN_INDEX)[0]
            if expected_generation is not None and generation != expected_generation:
                return None
            if include_payload and seq != SEQ_READY:
                return None
            length = struct.unpack_from("<I", self._shm.buf, start + HEADER_LEN_INDEX)[0]
            self._ensure_buffer(slot_index, length)
            timestamp, previous, published = struct.unpack_from("<qqq", self._shm.buf, start + HEADER_TS_INDEX)
            payload = bytes(self._shm.buf[start + PAYLOAD_OFFSET:start + PAYLOAD_OFFSET + length]) if include_payload else None
            return SlotSnapshot(seq, length, generation, timestamp, previous, published if published > 0 else timestamp, payload)

    def read_payload(self, slot_index: int) -> bytes:
        value = self.snapshot(slot_index)
        if value is None:
            raise RuntimeError("SHM slot is not ready")
        return value.payload


class _RustTopicBufferPool:
    def __init__(self, *, name: str, slot_size: int, slot_count: int):
        if slot_size < PAYLOAD_OFFSET or slot_count <= 0 or slot_size * slot_count > (1 << 63) - 1:
            raise ValueError("invalid SHM slot geometry or off_t range")
        from r2kernel.ffi import R2KernelCore  # type: ignore
        self._core = R2KernelCore()
        self._lib = self._core.lib
        self.name = name
        self.slot_size = slot_size
        self.slot_count = slot_count
        self.buffer_size = slot_size * slot_count
        out = ctypes.c_void_p()
        rc = self._lib.r2kernel_shm_open(name.encode("utf-8"), slot_size, slot_count, ctypes.byref(out))
        if rc != 0 or not out.value:
            raise RuntimeError(f"Failed to open SHM via rust core rc={rc}")
        self._handle = out

    def publish(self, slot_index: int, data: bytes, generation: int, timestamp: int, published_timestamp: int):
        if not isinstance(data, bytes):
            raise TypeError("SHM payload must be bytes")
        self._ensure_buffer(slot_index, len(data))
        # ctypes scalar constructors wrap integers, so validate before crossing FFI.
        struct.pack("<Qqq", generation, int(timestamp), int(published_timestamp))
        rc = self._lib.r2kernel_shm_publish(
            self._handle, slot_index, data, len(data), generation, int(timestamp), int(published_timestamp))
        if rc != 0:
            raise RuntimeError(f"publish failed rc={rc}")

    def reset_slot(self, slot_index: int):
        self._ensure_buffer(slot_index, 0)
        rc = self._lib.r2kernel_shm_reset_slot(self._handle, slot_index)
        if rc != 0:
            raise RuntimeError(f"reset_slot failed rc={rc}")

    def _ensure_buffer(self, slot_index: int, length: int):
        if not 0 <= slot_index < self.slot_count or not 0 <= length <= min(self.slot_size - PAYLOAD_OFFSET, 0xffffffff):
            raise IndexError("slot payload out of range")

    def snapshot(self, slot_index: int, *, expected_generation: int | None = None, include_payload: bool = True) -> SlotSnapshot | None:
        from r2kernel.ffi.r2kernel_core import R2KernelShmSlotInfo
        self._ensure_buffer(slot_index, 0)
        if expected_generation is not None:
            struct.pack("<Q", expected_generation)
        expected = ctypes.c_uint64(expected_generation) if expected_generation is not None else None
        expected_ptr = ctypes.byref(expected) if expected is not None else None
        info = R2KernelShmSlotInfo()
        rc = self._lib.r2kernel_shm_read_snapshot(self._handle, slot_index, expected_ptr, ctypes.byref(info), None, 0)
        if rc == -2:
            return None
        if rc == -4:
            if include_payload and info.seq != SEQ_READY:
                return None
            self._ensure_buffer(slot_index, info.length)
        if rc != 0:
            raise RuntimeError(f"read_snapshot failed rc={rc}")
        self._ensure_buffer(slot_index, info.length)
        payload = None
        if include_payload:
            if info.seq != SEQ_READY:
                return None
            while True:
                capacity = info.length
                # A non-NULL dummy distinguishes a zero-length full read.
                buffer = (ctypes.c_uint8 * max(1, capacity))()
                rc = self._lib.r2kernel_shm_read_snapshot(
                    self._handle, slot_index, expected_ptr, ctypes.byref(info), buffer, capacity)
                if rc == -2:
                    return None
                if rc == -4:
                    self._ensure_buffer(slot_index, info.length)
                    continue
                if rc != 0:
                    raise RuntimeError(f"read_snapshot failed rc={rc}")
                self._ensure_buffer(slot_index, info.length)
                if info.length > capacity:
                    raise RuntimeError("native snapshot exceeded output capacity")
                payload = ctypes.string_at(buffer, info.length)
                break
        return SlotSnapshot(int(info.seq), int(info.length), int(info.generation),
                            int(info.timestamp), int(info.prev_timestamp),
                            int(info.published_timestamp), payload)

    def close(self):
        try:
            self._lib.r2kernel_shm_close(self._handle)
        except Exception:
            pass

    def unlink(self):
        try:
            self._lib.r2kernel_shm_unlink(self.name.encode("utf-8"))
        except Exception:
            pass

    def read_payload(self, slot_index: int) -> bytes:
        value = self.snapshot(slot_index)
        if value is None:
            raise RuntimeError("SHM slot is not ready")
        return value.payload


_FORCE_PYTHON = os.getenv("R2KERNEL_FORCE_PYTHON") == "1"
TopicBufferPool = _PyTopicBufferPool
if not _FORCE_PYTHON:
    try:
        import ctypes  # noqa: F401
        from r2kernel.ffi import R2KernelCore  # noqa: F401
        TopicBufferPool = _RustTopicBufferPool
    except Exception:
        TopicBufferPool = _PyTopicBufferPool
