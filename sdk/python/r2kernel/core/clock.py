from __future__ import annotations

import time
import ctypes
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping

from r2kernel.ffi import R2KernelClockSnapshot as _RawClockSnapshot
from r2kernel.ffi import R2KernelCore


class ClockSource(IntEnum):
    LOCAL_UNSYNCED = 0
    LOCAL_SYSTEM = 1
    SOFTWARE_PTP = 2
    HARDWARE_PTP = 3
    R2KERNEL_LOGICAL = 4
    R2KERNEL_LOGICAL_HW = 5


_SOURCE_NAMES = {
    ClockSource.LOCAL_UNSYNCED: "local_unsynced",
    ClockSource.LOCAL_SYSTEM: "local_system",
    ClockSource.SOFTWARE_PTP: "software_ptp",
    ClockSource.HARDWARE_PTP: "hardware_ptp",
    ClockSource.R2KERNEL_LOGICAL: "r2kernel_logical",
    ClockSource.R2KERNEL_LOGICAL_HW: "r2kernel_logical_hw",
}


@dataclass(frozen=True)
class ClockSnapshot:
    realtime_ns: int
    monotonic_ns: int
    logical_ns: int
    offset_ns: int
    rtt_ns: int
    uncertainty_ns: int
    source: ClockSource
    stable: bool
    accepted_for_one_way: bool

    @property
    def source_name(self) -> str:
        return _SOURCE_NAMES.get(self.source, "unknown")

    def to_dict(self) -> dict[str, int | bool | str]:
        return {
            "realtime_ns": self.realtime_ns,
            "monotonic_ns": self.monotonic_ns,
            "logical_ns": self.logical_ns,
            "offset_ns": self.offset_ns,
            "rtt_ns": self.rtt_ns,
            "uncertainty_ns": self.uncertainty_ns,
            "source": self.source_name,
            "source_code": int(self.source),
            "stable": self.stable,
            "accepted_for_one_way": self.accepted_for_one_way,
        }


_core: R2KernelCore | None = None


def _load_core() -> R2KernelCore:
    global _core
    if _core is None:
        _core = R2KernelCore()
    return _core


def _coerce_source(source: ClockSource | int | str) -> ClockSource:
    if isinstance(source, ClockSource):
        return source
    if isinstance(source, str):
        normalized = source.strip().lower()
        for item, name in _SOURCE_NAMES.items():
            if normalized == name:
                return item
        raise ValueError(f"unknown R2Kernel clock source: {source}")
    return ClockSource(int(source))


def _from_raw(snapshot: _RawClockSnapshot) -> ClockSnapshot:
    return ClockSnapshot(
        realtime_ns=int(snapshot.realtime_ns),
        monotonic_ns=int(snapshot.monotonic_ns),
        logical_ns=int(snapshot.logical_ns),
        offset_ns=int(snapshot.offset_ns),
        rtt_ns=int(snapshot.rtt_ns),
        uncertainty_ns=int(snapshot.uncertainty_ns),
        source=_coerce_source(int(snapshot.source)),
        stable=bool(snapshot.stable),
        accepted_for_one_way=bool(snapshot.accepted_for_one_way),
    )


def clock_now_ns() -> int:
    try:
        return int(_load_core().lib.r2kernel_clock_now_ns())
    except Exception:
        return time.time_ns()


def clock_snapshot() -> ClockSnapshot:
    try:
        raw = _RawClockSnapshot()
        rc = _load_core().lib.r2kernel_clock_snapshot(ctypes.byref(raw))
        if rc != 0:
            raise RuntimeError(f"r2kernel_clock_snapshot failed: rc={rc}")
        return _from_raw(raw)
    except Exception:
        realtime_ns = time.time_ns()
        return ClockSnapshot(
            realtime_ns=realtime_ns,
            monotonic_ns=time.monotonic_ns(),
            logical_ns=realtime_ns,
            offset_ns=0,
            rtt_ns=-1,
            uncertainty_ns=-1,
            source=ClockSource.LOCAL_UNSYNCED,
            stable=False,
            accepted_for_one_way=False,
        )


def configure_clock(
    *,
    offset_ns: int = 0,
    rtt_ns: int = -1,
    uncertainty_ns: int = -1,
    source: ClockSource | int | str = ClockSource.R2KERNEL_LOGICAL,
    stable: bool = False,
    accepted_for_one_way: bool = False,
) -> ClockSnapshot:
    source_code = _coerce_source(source)
    rc = _load_core().lib.r2kernel_clock_configure(
        int(offset_ns),
        int(rtt_ns),
        int(uncertainty_ns),
        int(source_code),
        1 if stable else 0,
        1 if accepted_for_one_way else 0,
    )
    if rc != 0:
        raise RuntimeError(f"r2kernel_clock_configure failed: rc={rc}")
    return clock_snapshot()


def configure_clock_from_status(status: Mapping[str, Any] | None) -> ClockSnapshot | None:
    if not status:
        return None
    clock_status = status.get("clock_status") if isinstance(status.get("clock_status"), Mapping) else status
    if not isinstance(clock_status, Mapping):
        return None
    source = str(
        clock_status.get("source")
        or status.get("clock_source")
        or ClockSource.LOCAL_UNSYNCED.name.lower()
    )
    fallback_offset_ns = _int_field(clock_status, "offset_ns", _int_field(status, "clock_offset_ns", 0))
    correction_ns = _int_field(
        clock_status,
        "correction_ns",
        _int_field(status, "clock_correction_ns", fallback_offset_ns),
    )
    correction_applied = _bool_field(
        clock_status,
        "correction_applied",
        _bool_field(status, "clock_correction_applied", source in {"r2kernel_logical", "r2kernel_logical_hw"}),
    )
    offset_ns = correction_ns if correction_applied else 0
    return configure_clock(
        offset_ns=offset_ns,
        rtt_ns=_int_field(clock_status, "rtt_ns", _int_field(status, "clock_rtt_ns", -1)),
        uncertainty_ns=_int_field(
            clock_status,
            "uncertainty_ns",
            _int_field(status, "clock_uncertainty_ns", -1),
        ),
        source=source,
        stable=_bool_field(clock_status, "stable", _bool_field(status, "clock_stable", False)),
        accepted_for_one_way=_bool_field(
            clock_status,
            "accepted_for_one_way",
            _bool_field(status, "clock_accepted_for_one_way", False),
        ),
    )


def reset_clock() -> ClockSnapshot:
    rc = _load_core().lib.r2kernel_clock_reset()
    if rc != 0:
        raise RuntimeError(f"r2kernel_clock_reset failed: rc={rc}")
    return clock_snapshot()


def _int_field(values: Mapping[str, Any], key: str, default: int) -> int:
    value = values.get(key)
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_field(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int):
        return value != 0
    return default


__all__ = [
    "ClockSnapshot",
    "ClockSource",
    "clock_now_ns",
    "clock_snapshot",
    "configure_clock",
    "configure_clock_from_status",
    "reset_clock",
]
