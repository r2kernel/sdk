"""Control SHM limits, protocol errors, region policy/budget and the mapped payload region."""

from __future__ import annotations

import mmap
import os
import struct
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any


CONTROL_SHM_MIN_REGION_BYTES = 4096
CONTROL_SHM_MAX_CHANNELS = 128
CONTROL_SHM_MAX_OWNED_BYTES = 64 * 1024 * 1024
CONTROL_SHM_MAX_MAPPED_BYTES = 64 * 1024 * 1024
CONTROL_SHM_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
CONTROL_SHM_MAX_REGION_BYTES = 16 * 1024 * 1024
CONTROL_SHM_PAYLOAD_OFFSET = 64
CONTROL_SHM_HEADER_SEQUENCE_OFFSET = 0
CONTROL_SHM_HEADER_LENGTH_OFFSET = 8
CONTROL_SHM_HEADER_GENERATION_OFFSET = 16
CONTROL_SHM_HEADER_TIMESTAMP_OFFSET = 32
CONTROL_SHM_HEADER_PREVIOUS_TIMESTAMP_OFFSET = 40
CONTROL_SHM_HEADER_PUBLISHED_TIMESTAMP_OFFSET = 48
CONTROL_SHM_SEQUENCE_READY = 0
CONTROL_SHM_SEQUENCE_WRITING = 1


CONTROL_RT_MEMORY_BUDGET_ENV = "R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES"
CONTROL_RT_MEMORY_BUDGET_DEFAULT_BYTES = 64 * 1024 * 1024


def configured_rt_memory_budget() -> int:
    """Prepared (locked) memory admission limit from ``R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES``."""
    configured = os.environ.get(CONTROL_RT_MEMORY_BUDGET_ENV)
    if configured is None:
        return CONTROL_RT_MEMORY_BUDGET_DEFAULT_BYTES
    if not configured or not configured.isascii() or not configured.isdigit() or int(configured) == 0:
        raise ValueError(f"Invalid {CONTROL_RT_MEMORY_BUDGET_ENV}")
    return int(configured)


def page_rounded_bytes(size: int) -> int:
    page = mmap.PAGESIZE
    return ((int(size) + page - 1) // page) * page


class ControlShmProtocolError(RuntimeError):
    """Raised when a v5 local-Control frame or shared-memory lease is invalid."""


class ControlShmCapacityError(ControlShmProtocolError):
    """Raised when a negotiated local-Control resource limit is exceeded."""


@dataclass(frozen=True)
class ControlShmPolicy:
    minimum_region_bytes: int = CONTROL_SHM_MIN_REGION_BYTES
    maximum_channels: int = CONTROL_SHM_MAX_CHANNELS
    maximum_owned_bytes: int = CONTROL_SHM_MAX_OWNED_BYTES
    maximum_mapped_bytes: int = CONTROL_SHM_MAX_MAPPED_BYTES
    maximum_region_bytes: int = CONTROL_SHM_MAX_REGION_BYTES

    def __post_init__(self) -> None:
        positive_values = (
            self.minimum_region_bytes,
            self.maximum_channels,
            self.maximum_owned_bytes,
            self.maximum_mapped_bytes,
            self.maximum_region_bytes,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("local Control shared-memory policy values must be positive")
        if self.minimum_region_bytes < CONTROL_SHM_MIN_REGION_BYTES:
            raise ValueError("local Control minimum shared-memory region is below the 4096-byte minimum")
        if self.minimum_region_bytes > self.maximum_region_bytes:
            raise ValueError("local Control minimum shared-memory region exceeds its hard cap")
        if self.maximum_region_bytes > CONTROL_SHM_MAX_REGION_BYTES:
            raise ValueError("local Control region hard cap exceeds the 16 MiB SDK limit")
        if self.maximum_region_bytes > self.maximum_owned_bytes:
            raise ValueError("local Control region hard cap exceeds the owned-byte budget")
        if self.maximum_region_bytes > self.maximum_mapped_bytes:
            raise ValueError("local Control region hard cap exceeds the mapped-byte budget")

    def region_size_for(self, payload_size: int, current_size: int = 0) -> int:
        length = int(payload_size)
        if length < 0 or length > min(
            CONTROL_SHM_MAX_PAYLOAD_BYTES,
            self.maximum_region_bytes - CONTROL_SHM_PAYLOAD_OFFSET,
        ):
            raise ControlShmCapacityError("local Control payload is outside the SDK payload limit")
        previous_size = int(current_size)
        if previous_size < 0 or previous_size > self.maximum_region_bytes:
            raise ValueError("current local Control region size is invalid")
        required_size = length + CONTROL_SHM_PAYLOAD_OFFSET
        if previous_size >= required_size and previous_size > 0:
            return previous_size
        page_size = mmap.PAGESIZE
        growth_size = max(self.minimum_region_bytes, required_size, previous_size * 2)
        rounded_size = ((growth_size + page_size - 1) // page_size) * page_size
        region_size = min(self.maximum_region_bytes, rounded_size)
        if region_size < required_size:
            raise ControlShmCapacityError("local Control payload exceeds the negotiated shared-memory region cap")
        return region_size


class ControlShmBudget:
    """Thread-safe accounting for active v5 channels and mappings."""

    def __init__(self, policy: ControlShmPolicy, rt_memory_budget_bytes: int | None = None):
        self.policy = policy
        self.rt_memory_budget_bytes = (
            configured_rt_memory_budget() if rt_memory_budget_bytes is None else int(rt_memory_budget_bytes)
        )
        if self.rt_memory_budget_bytes <= 0:
            raise ValueError("local Control prepared/RT memory budget must be positive")
        self._prepared_bytes = 0
        self._peak_prepared_bytes = 0
        self._lock = threading.Lock()
        self._channels = 0
        self._peak_channels = 0
        self._owned_bytes = 0
        self._peak_owned_bytes = 0
        self._mapped_bytes = 0
        self._peak_mapped_bytes = 0

    def acquire_channel(self) -> None:
        with self._lock:
            if self._channels >= self.policy.maximum_channels:
                raise ControlShmCapacityError("local Control shared-memory channel limit reached")
            self._channels += 1
            self._peak_channels = max(self._peak_channels, self._channels)

    def release_channel(self) -> None:
        with self._lock:
            if self._channels <= 0:
                raise RuntimeError("local Control channel budget released more than once")
            self._channels -= 1

    def reserve_owned(self, size: int) -> None:
        self._reserve("owned", int(size))

    def release_owned(self, size: int) -> None:
        self._release("owned", int(size))

    def reserve_mapped(self, size: int) -> None:
        self._reserve("mapped", int(size))

    def release_mapped(self, size: int) -> None:
        self._release("mapped", int(size))

    def reserve_prepared(self, size: int) -> None:
        """Admit ``size`` (page-rounded) prepared/locked bytes against this client's and the process budget."""
        actual = page_rounded_bytes(size)
        with self._lock:
            limit = self.rt_memory_budget_bytes
            if actual > limit or self._prepared_bytes > limit - actual:
                raise ControlShmCapacityError("Control prepared/RT memory budget exceeded")
            _ProcessPreparedMemory.instance().reserve(actual)
            self._prepared_bytes += actual
            self._peak_prepared_bytes = max(self._peak_prepared_bytes, self._prepared_bytes)

    def release_prepared(self, size: int) -> None:
        actual = page_rounded_bytes(size)
        with self._lock:
            self._prepared_bytes -= min(actual, self._prepared_bytes)
            _ProcessPreparedMemory.instance().release(actual)

    def reset_lock_after_fork(self) -> None:
        """In a forked child: a lock another parent thread held at the fork stays held."""
        self._lock = threading.Lock()
        process = _ProcessPreparedMemory._instance
        if process is not None:
            process.reset_lock_after_fork()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "channels": self._channels,
                "peak_channels": self._peak_channels,
                "owned_bytes": self._owned_bytes,
                "peak_owned_bytes": self._peak_owned_bytes,
                "mapped_bytes": self._mapped_bytes,
                "peak_mapped_bytes": self._peak_mapped_bytes,
                "prepared_bytes": self._prepared_bytes,
                "peak_prepared_bytes": self._peak_prepared_bytes,
            }

    def _reserve(self, kind: str, size: int) -> None:
        if size <= 0:
            raise ValueError("local Control reservation size must be positive")
        with self._lock:
            if kind == "owned":
                current = self._owned_bytes
                maximum = self.policy.maximum_owned_bytes
            else:
                current = self._mapped_bytes
                maximum = self.policy.maximum_mapped_bytes
            if current + size > maximum:
                raise ControlShmCapacityError(f"local Control {kind} shared-memory budget exceeded")
            if kind == "owned":
                self._owned_bytes += size
                self._peak_owned_bytes = max(self._peak_owned_bytes, self._owned_bytes)
            else:
                self._mapped_bytes += size
                self._peak_mapped_bytes = max(self._peak_mapped_bytes, self._mapped_bytes)

    def _release(self, kind: str, size: int) -> None:
        if size <= 0:
            raise ValueError("local Control reservation size must be positive")
        with self._lock:
            if kind == "owned":
                current = self._owned_bytes
            else:
                current = self._mapped_bytes
            if size > current:
                raise RuntimeError(f"local Control {kind} budget released more than reserved")
            if kind == "owned":
                self._owned_bytes -= size
            else:
                self._mapped_bytes -= size


class _ProcessPreparedMemory:
    """Prepared (locked) bytes of every Control client in this process; the limit is fixed at first use."""

    _instance: "_ProcessPreparedMemory | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._bytes = 0
        self._peak = 0

    @classmethod
    def instance(cls) -> "_ProcessPreparedMemory":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(configured_rt_memory_budget())
            return cls._instance

    @classmethod
    def configured_snapshot(cls) -> dict[str, int] | None:
        instance = cls._instance
        return instance.snapshot() if instance is not None else None

    def reserve(self, size: int) -> None:
        with self._lock:
            if size > self.limit or self._bytes > self.limit - size:
                raise ControlShmCapacityError("Control prepared/RT memory budget exceeded")
            self._bytes += size
            self._peak = max(self._peak, self._bytes)

    def release(self, size: int) -> None:
        with self._lock:
            self._bytes -= min(size, self._bytes)

    def reset_lock_after_fork(self) -> None:
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"limit": self.limit, "bytes": self._bytes, "peak": self._peak}


def _reserve_prepared(budget: "ControlShmBudget | None", size: int) -> None:
    if budget is not None:
        budget.reserve_prepared(size)
    else:
        _ProcessPreparedMemory.instance().reserve(page_rounded_bytes(size))


def _release_prepared(budget: "ControlShmBudget | None", size: int) -> None:
    if budget is not None:
        budget.release_prepared(size)
    else:
        _ProcessPreparedMemory.instance().release(page_rounded_bytes(size))


class ControlShmRegion:
    """A producer-owned anonymous POSIX region or a borrowed received mapping."""

    def __init__(
        self,
        *,
        token: str,
        size: int,
        file_descriptor: int,
        mapped: mmap.mmap,
        writable: bool,
        shared_memory_owner: Any | None = None,
        budget: ControlShmBudget | None = None,
        budget_kind: str | None = None,
    ):
        self.token = token
        self.size = int(size)
        self.file_descriptor = int(file_descriptor)
        self.mapping = mapped
        self.writable = bool(writable)
        self._shared_memory_owner = shared_memory_owner
        self._budget = budget
        self._budget_kind = budget_kind
        self._prepared = False
        self._closed = False

    def prepare_memory(self) -> None:
        """Fault in and lock this mapping within the prepared/RT memory budget; failure fails cold preparation."""
        if self._prepared:
            self._lock_memory()
            return
        _reserve_prepared(self._budget, self.size)
        try:
            self._lock_memory()
        except BaseException:
            _release_prepared(self._budget, self.size)
            raise
        self._prepared = True

    def _lock_memory(self) -> None:
        import ctypes

        class Buffer(ctypes.Structure):
            _fields_ = [
                ("buf", ctypes.c_void_p), ("obj", ctypes.c_void_p),
                ("len", ctypes.c_ssize_t), ("itemsize", ctypes.c_ssize_t),
                ("readonly", ctypes.c_int), ("ndim", ctypes.c_int),
                ("format", ctypes.c_void_p), ("shape", ctypes.c_void_p),
                ("strides", ctypes.c_void_p), ("suboffsets", ctypes.c_void_p),
                ("internal", ctypes.c_void_p),
            ]

        for offset in range(0, self.size, mmap.PAGESIZE):
            value = self.mapping[offset]
            if self.writable:
                self.mapping[offset] = value
        view = Buffer()
        get_buffer = ctypes.pythonapi.PyObject_GetBuffer
        get_buffer.argtypes = [ctypes.py_object, ctypes.POINTER(Buffer), ctypes.c_int]
        get_buffer.restype = ctypes.c_int
        release_buffer = ctypes.pythonapi.PyBuffer_Release
        release_buffer.argtypes = [ctypes.POINTER(Buffer)]
        release_buffer.restype = None
        get_buffer(self.mapping, ctypes.byref(view), 0)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            libc.mlock.restype = ctypes.c_int
            if libc.mlock(view.buf, self.size) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            release_buffer(ctypes.byref(view))

    @classmethod
    def create(cls, size: int, budget: ControlShmBudget | None = None) -> "ControlShmRegion":
        region_size = int(size)
        if region_size < CONTROL_SHM_MIN_REGION_BYTES or region_size > CONTROL_SHM_MAX_REGION_BYTES:
            raise ControlShmCapacityError("local Control shared-memory region size is invalid")
        if budget is not None and region_size > budget.policy.maximum_region_bytes:
            raise ControlShmCapacityError("local Control shared-memory region exceeds the negotiated region hard cap")
        token = uuid.uuid4().hex
        if budget is not None:
            budget.reserve_owned(region_size)
        try:
            if sys.platform.startswith("linux"):
                descriptor = cls._create_memfd(token, region_size)
                owner = None
            elif sys.platform == "darwin":
                descriptor, owner = cls._create_unlinked_posix_region(token, region_size)
            else:
                raise RuntimeError(f"reusable local Control shared memory is unsupported on {sys.platform}")
            cls._validate_file_descriptor(descriptor, region_size)
            mapped = mmap.mmap(descriptor, region_size, access=mmap.ACCESS_WRITE)
            return cls(
                token=token,
                size=region_size,
                file_descriptor=descriptor,
                mapped=mapped,
                writable=True,
                shared_memory_owner=owner,
                budget=budget,
                budget_kind="owned" if budget is not None else None,
            )
        except Exception:
            try:
                if "descriptor" in locals():
                    cls._close_descriptor(descriptor, owner)
            finally:
                if budget is not None:
                    budget.release_owned(region_size)
            raise

    @classmethod
    def from_received_descriptor(
        cls,
        token: str,
        size: int,
        file_descriptor: int,
        budget: ControlShmBudget | None = None,
    ) -> "ControlShmRegion":
        region_size = int(size)
        if not token or len(token) > 128:
            os.close(file_descriptor)
            raise ControlShmProtocolError("local Control shared-memory generation token is invalid")
        if region_size < CONTROL_SHM_MIN_REGION_BYTES or region_size > CONTROL_SHM_MAX_REGION_BYTES:
            os.close(file_descriptor)
            raise ControlShmProtocolError("local Control shared-memory region size is invalid")
        if budget is not None:
            if region_size > budget.policy.maximum_region_bytes:
                os.close(file_descriptor)
                raise ControlShmProtocolError("local Control shared-memory region exceeds the negotiated hard cap")
            try:
                budget.reserve_mapped(region_size)
            except Exception:
                os.close(file_descriptor)
                raise
        try:
            cls._validate_file_descriptor(file_descriptor, region_size)
            mapped = mmap.mmap(file_descriptor, region_size, access=mmap.ACCESS_READ)
            return cls(
                token=token,
                size=region_size,
                file_descriptor=file_descriptor,
                mapped=mapped,
                writable=False,
                budget=budget,
                budget_kind="mapped" if budget is not None else None,
            )
        except Exception:
            try:
                os.close(file_descriptor)
            finally:
                if budget is not None:
                    budget.release_mapped(region_size)
            raise

    @staticmethod
    def _create_memfd(token: str, size: int) -> int:
        import fcntl

        create_memfd = getattr(os, "memfd_create", None)
        required_flags = (getattr(os, "MFD_CLOEXEC", None), getattr(os, "MFD_ALLOW_SEALING", None))
        if create_memfd is None or any(flag is None for flag in required_flags):
            raise RuntimeError("Python runtime does not expose sealed memfd support required by local Control v5")
        descriptor = create_memfd(f"r2kernel-control-{token}", required_flags[0] | required_flags[1])
        try:
            os.ftruncate(descriptor, size)
            posix_fallocate = getattr(os, "posix_fallocate", None)
            if posix_fallocate is None:
                raise RuntimeError("Python runtime does not expose posix_fallocate required by local Control v5")
            posix_fallocate(descriptor, 0, size)
            seals = getattr(fcntl, "F_SEAL_GROW", None), getattr(fcntl, "F_SEAL_SHRINK", None)
            if any(seal is None for seal in seals) or not hasattr(fcntl, "F_ADD_SEALS"):
                raise RuntimeError("Python runtime does not expose memfd size seals required by local Control v5")
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals[0] | seals[1])
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _create_unlinked_posix_region(token: str, size: int) -> tuple[int, Any]:
        try:
            import posix_ipc
        except ImportError as exc:
            raise RuntimeError("posix_ipc is required for local Control v5 on macOS") from exc
        name = f"/r{token[:27]}"
        region = posix_ipc.SharedMemory(name, flags=posix_ipc.O_CREX, mode=0o600, size=size)
        try:
            os.set_inheritable(region.fd, False)
            if int(os.fstat(region.fd).st_size) != size:
                raise ControlShmProtocolError("created local Control region has an unexpected size")
            region.unlink()
            return int(region.fd), region
        except Exception:
            try:
                region.unlink()
            except Exception:
                pass
            region.close_fd()
            raise

    @staticmethod
    def _validate_file_descriptor(file_descriptor: int, expected_size: int) -> None:
        try:
            actual_size = int(os.fstat(file_descriptor).st_size)
        except OSError as exc:
            raise ControlShmProtocolError("local Control shared-memory descriptor is not readable") from exc
        if actual_size != int(expected_size):
            raise ControlShmProtocolError("local Control shared-memory size does not match its descriptor")
        if sys.platform.startswith("linux"):
            import fcntl

            if not hasattr(fcntl, "F_GET_SEALS"):
                raise ControlShmProtocolError("Python runtime cannot validate local Control memfd seals")
            required_seals = getattr(fcntl, "F_SEAL_GROW", 0) | getattr(fcntl, "F_SEAL_SHRINK", 0)
            try:
                actual_seals = fcntl.fcntl(file_descriptor, fcntl.F_GET_SEALS)
            except OSError as exc:
                raise ControlShmProtocolError("local Control descriptor is not a sealed memfd") from exc
            if actual_seals & required_seals != required_seals:
                raise ControlShmProtocolError("local Control memfd is missing size seals")

    @staticmethod
    def _close_descriptor(file_descriptor: int, shared_memory_owner: Any | None = None) -> None:
        if shared_memory_owner is not None:
            shared_memory_owner.close_fd()
        else:
            os.close(file_descriptor)

    def write_payload(self, payload: bytes, timestamp_ns: int) -> dict[str, Any]:
        if self._closed:
            raise ControlShmProtocolError("local Control shared-memory region is closed")
        if not self.writable:
            raise ControlShmProtocolError("local Control shared-memory receiver mapping is read-only")
        data = bytes(payload)
        if len(data) > self.size - CONTROL_SHM_PAYLOAD_OFFSET or len(data) > CONTROL_SHM_MAX_PAYLOAD_BYTES:
            raise ValueError("local Control payload exceeds the region capacity")
        self.mapping[CONTROL_SHM_HEADER_SEQUENCE_OFFSET] = CONTROL_SHM_SEQUENCE_WRITING
        struct.pack_into("<I", self.mapping, CONTROL_SHM_HEADER_LENGTH_OFFSET, len(data))
        struct.pack_into("<Q", self.mapping, CONTROL_SHM_HEADER_GENERATION_OFFSET, 0)
        struct.pack_into("<q", self.mapping, CONTROL_SHM_HEADER_TIMESTAMP_OFFSET, int(timestamp_ns))
        struct.pack_into("<q", self.mapping, CONTROL_SHM_HEADER_PREVIOUS_TIMESTAMP_OFFSET, 0)
        struct.pack_into("<q", self.mapping, CONTROL_SHM_HEADER_PUBLISHED_TIMESTAMP_OFFSET, int(timestamp_ns))
        self.mapping[CONTROL_SHM_PAYLOAD_OFFSET : CONTROL_SHM_PAYLOAD_OFFSET + len(data)] = data
        self.mapping[CONTROL_SHM_HEADER_SEQUENCE_OFFSET] = CONTROL_SHM_SEQUENCE_READY
        return {
            "region_name": self.token,
            "slot_index": 0,
            "slot_size": self.size,
            "slot_count": 1,
            "length": len(data),
        }

    def read_payload(self, descriptor: dict[str, Any]) -> bytes:
        if self._closed:
            raise ControlShmProtocolError("local Control shared-memory region is closed")
        normalized = normalize_control_shm_lease(descriptor)
        if normalized["region_name"] != self.token or normalized["slot_size"] != self.size:
            raise ControlShmProtocolError("local Control lease does not match the mapped region")
        first_sequence = self.mapping[CONTROL_SHM_HEADER_SEQUENCE_OFFSET]
        if first_sequence != CONTROL_SHM_SEQUENCE_READY:
            raise ControlShmProtocolError("local Control shared-memory slot is not ready")
        stored_length = struct.unpack_from("<I", self.mapping, CONTROL_SHM_HEADER_LENGTH_OFFSET)[0]
        if stored_length != normalized["length"]:
            raise ControlShmProtocolError("local Control lease length does not match its shared-memory header")
        payload_end = CONTROL_SHM_PAYLOAD_OFFSET + stored_length
        if payload_end > self.size or stored_length > CONTROL_SHM_MAX_PAYLOAD_BYTES:
            raise ControlShmProtocolError("local Control payload exceeds its mapped region")
        payload = bytes(self.mapping[CONTROL_SHM_PAYLOAD_OFFSET:payload_end])
        last_sequence = self.mapping[CONTROL_SHM_HEADER_SEQUENCE_OFFSET]
        if last_sequence != CONTROL_SHM_SEQUENCE_READY or last_sequence != first_sequence:
            raise ControlShmProtocolError("local Control shared-memory slot changed while being read")
        return payload

    def close(self) -> None:
        if self._closed:
            return
        try:
            try:
                self.mapping.close()
            finally:
                self._close_descriptor(self.file_descriptor, self._shared_memory_owner)
        finally:
            self._closed = True
            if self._prepared:
                self._prepared = False
                _release_prepared(self._budget, self.size)
            if self._budget is not None and self._budget_kind == "owned":
                self._budget.release_owned(self.size)
            elif self._budget is not None and self._budget_kind == "mapped":
                self._budget.release_mapped(self.size)


def normalize_control_shm_lease(descriptor: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(descriptor, dict):
        raise ControlShmProtocolError("local Control lease descriptor is not an object")
    try:
        token = str(descriptor.get("region_name") or "")
        slot_index = int(descriptor.get("slot_index", -1))
        slot_size = int(descriptor.get("slot_size", -1))
        slot_count = int(descriptor.get("slot_count", -1))
        length = int(descriptor.get("length", -1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ControlShmProtocolError("local Control lease descriptor has invalid fields") from exc
    if not token or len(token) > 128:
        raise ControlShmProtocolError("local Control lease token is invalid")
    if slot_index != 0 or slot_count != 1:
        raise ControlShmProtocolError("local Control v5 requires exactly one slot at index zero")
    if slot_size < CONTROL_SHM_MIN_REGION_BYTES or slot_size > CONTROL_SHM_MAX_REGION_BYTES:
        raise ControlShmProtocolError("local Control lease region size is invalid")
    if length < 0 or length > min(
        CONTROL_SHM_MAX_PAYLOAD_BYTES,
        slot_size - CONTROL_SHM_PAYLOAD_OFFSET,
    ):
        raise ControlShmProtocolError("local Control lease length exceeds its region")
    if length > CONTROL_SHM_MAX_PAYLOAD_BYTES:
        raise ControlShmProtocolError("local Control lease exceeds the SDK payload limit")
    return {
        "region_name": token,
        "slot_index": slot_index,
        "slot_size": slot_size,
        "slot_count": slot_count,
        "length": length,
    }
