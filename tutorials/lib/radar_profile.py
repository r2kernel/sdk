"""Tutorial validation for the public integrated-radar sector profile.

The validator checks message relationships and selected resource limits. It does
not infer missing sectors, estimate serialized size, or repair invalid samples.
"""
from __future__ import annotations

import math
import struct
from collections.abc import Collection, Mapping
from typing import Any


_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_TAU = math.tau
_AZIMUTH_ENDPOINT_TOLERANCE_RAD = 1e-6
_REQUEST_FIELDS = {"cycle", "expected_session_id", "after_sample_id"}
_RESPONSE_FIELDS = {"cycle", "session_id", "status", "sector", "detail"}
_SECTOR_FIELDS = {
    "session_id", "sample_id", "revolution_id", "azimuth_start_rad", "azimuth_span_rad",
    "scan_started_at_sync_ns", "scan_ended_at_sync_ns", "frame_id", "revolution_complete", "returns",
}
_RETURN_FIELDS = {"range_m", "azimuth_rad", "radial_velocity_mps", "signal_strength", "observed_at_sync_ns"}
_STATUSES = {"sample_available", "no_new_sample", "resync_required", "device_error"}
_CYCLE_FIELDS = {"generation", "sequence", "intent", "state_revision"}


def _mapping(
    value: Any,
    name: str,
    required_fields: set[str],
    optional_fields: Collection[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    keys = set(value)
    missing = required_fields - keys
    unknown = keys - required_fields - optional_fields
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(map(str, unknown))))
        raise ValueError(f"{name} has " + " and ".join(details))
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int = _INT64_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _float32(value: Any, name: str) -> float:
    number = _number(value, name)
    try:
        packed = struct.pack("!f", number)
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"{name} must fit in a finite float32") from exc
    rounded = struct.unpack("!f", packed)[0]
    if not math.isfinite(rounded):
        raise ValueError(f"{name} must fit in a finite float32")
    return rounded


def _cycle(value: Any, name: str) -> tuple[int, int, str, int]:
    cycle = _mapping(value, name, _CYCLE_FIELDS)
    generation = _integer(cycle["generation"], name + ".generation", minimum=1)
    sequence = _integer(cycle["sequence"], name + ".sequence", minimum=1)
    intent = cycle["intent"]
    if intent != "sample":
        raise ValueError(f"{name}.intent must be sample")
    state_revision = _integer(cycle["state_revision"], name + ".state_revision", minimum=0)
    if state_revision != 0:
        raise ValueError(f"{name}.state_revision must be zero for sample_exchange")
    return generation, sequence, intent, state_revision


def _validate_sector(sector_value: Any, *, response_session_id: str, max_return_count: int) -> int:
    sector = _mapping(sector_value, "sector", _SECTOR_FIELDS)
    sector_session_id = _text(sector["session_id"], "sector.session_id")
    if sector_session_id != response_session_id:
        raise ValueError("sector.session_id must match response.session_id")
    _integer(sector["sample_id"], "sector.sample_id", minimum=1)
    _integer(sector["revolution_id"], "sector.revolution_id", minimum=1)
    start_azimuth = _number(sector["azimuth_start_rad"], "sector.azimuth_start_rad")
    if not 0 <= start_azimuth < _TAU:
        raise ValueError("sector.azimuth_start_rad must be normalized to [0, 2*pi)")
    azimuth_span = _number(sector["azimuth_span_rad"], "sector.azimuth_span_rad")
    if not 0 < azimuth_span <= _TAU:
        raise ValueError("sector.azimuth_span_rad must be positive and at most 2*pi")
    scan_started_at = _integer(
        sector["scan_started_at_sync_ns"], "sector.scan_started_at_sync_ns", minimum=_INT64_MIN
    )
    scan_ended_at = _integer(
        sector["scan_ended_at_sync_ns"], "sector.scan_ended_at_sync_ns", minimum=_INT64_MIN
    )
    if scan_ended_at <= scan_started_at:
        raise ValueError("sector.scan_ended_at_sync_ns must follow scan_started_at_sync_ns")
    _text(sector["frame_id"], "sector.frame_id")
    if not isinstance(sector["revolution_complete"], bool):
        raise ValueError("sector.revolution_complete must be a boolean")
    returns = sector["returns"]
    if not isinstance(returns, (list, tuple)):
        raise ValueError("sector.returns must be a bounded sequence")
    if len(returns) > max_return_count:
        raise ValueError("sector.returns exceeds max_return_count")

    for index, return_value in enumerate(returns):
        name = f"sector.returns[{index}]"
        radar_return = _mapping(return_value, name, _RETURN_FIELDS)
        measured_range = _float32(radar_return["range_m"], name + ".range_m")
        if measured_range <= 0:
            raise ValueError(f"{name}.range_m must be positive")
        return_azimuth = _number(radar_return["azimuth_rad"], name + ".azimuth_rad")
        if not 0 <= return_azimuth < _TAU:
            raise ValueError(f"{name}.azimuth_rad must be normalized to [0, 2*pi)")
        offset = (return_azimuth - start_azimuth) % _TAU
        if offset > azimuth_span and min(offset - azimuth_span, _TAU - offset) > _AZIMUTH_ENDPOINT_TOLERANCE_RAD:
            raise ValueError(f"{name}.azimuth_rad lies outside the sector sweep")
        _float32(radar_return["radial_velocity_mps"], name + ".radial_velocity_mps")
        raw_signal_strength = _number(radar_return["signal_strength"], name + ".signal_strength")
        if not 0 <= raw_signal_strength <= 1:
            raise ValueError(f"{name}.signal_strength must be normalized to [0, 1]")
        _float32(raw_signal_strength, name + ".signal_strength")
        observed_at = _integer(
            radar_return["observed_at_sync_ns"], name + ".observed_at_sync_ns", minimum=_INT64_MIN
        )
        if not scan_started_at <= observed_at <= scan_ended_at:
            raise ValueError(f"{name}.observed_at_sync_ns must lie within the sector scan interval")
    return sector["sample_id"]


def validate_radar_cycle_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    max_return_count: int,
    response_size_bytes: int,
    max_response_bytes: int,
) -> None:
    """Validate one radar response against its request and selected resource caps.

    ``response_size_bytes`` is the serialized response size measured by the
    selected codec. Limits are supplied by the selected source profile/binding;
    this validator defines no policy defaults.
    """
    request = _mapping(request, "request", {"cycle"}, {"expected_session_id", "after_sample_id"})
    response = _mapping(response, "response", {"cycle", "session_id", "status"}, {"sector", "detail"})
    request_cycle = _cycle(request["cycle"], "request.cycle")
    response_cycle = _cycle(response["cycle"], "response.cycle")
    if response_cycle != request_cycle:
        raise ValueError("response.cycle must match request.cycle")

    selected_return_count = _integer(max_return_count, "max_return_count", minimum=1)
    selected_response_bytes = _integer(max_response_bytes, "max_response_bytes", minimum=1)
    measured_response_bytes = _integer(response_size_bytes, "response_size_bytes", minimum=1)
    if measured_response_bytes > selected_response_bytes:
        raise ValueError("serialized response exceeds max_response_bytes")

    expected_session_id = request.get("expected_session_id")
    if expected_session_id is not None:
        expected_session_id = _text(expected_session_id, "request.expected_session_id")
    after_sample_id = request.get("after_sample_id")
    after_sample_id = 0 if after_sample_id is None else _integer(
        after_sample_id, "request.after_sample_id", minimum=0
    )
    if expected_session_id is None and after_sample_id != 0:
        raise ValueError("request.after_sample_id must be zero when expected_session_id is omitted")

    session_id = _text(response["session_id"], "response.session_id")
    status = response["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("response.status must be sample_available, no_new_sample, resync_required, or device_error")
    detail = response.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("response.detail must be text when present")
    sector_value = response.get("sector")
    has_sector = sector_value is not None

    session_mismatch = expected_session_id is not None and expected_session_id != session_id
    if session_mismatch:
        if status != "resync_required" or has_sector:
            raise ValueError("a session mismatch requires resync_required with no sector")
        return

    if status == "sample_available":
        if not has_sector:
            raise ValueError("sample_available requires a sector")
        sample_id = _validate_sector(
            sector_value, response_session_id=session_id, max_return_count=selected_return_count
        )
        if sample_id <= after_sample_id:
            raise ValueError("sector.sample_id must be greater than request.after_sample_id")
        if expected_session_id is not None and sample_id != after_sample_id + 1:
            raise ValueError("sample_available cannot skip unseen sectors; provider must return resync_required")
    elif has_sector:
        raise ValueError(f"{status} must not include a sector")
