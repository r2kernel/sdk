"""Tutorial validation for the public receiver-reported GNSS fix profile.

The validator checks message relationships and the selected serialized response
limit. It does not synthesize position, time, accuracy, covariance, or velocity.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any


_INT64_MAX = (1 << 63) - 1
_MAX_ALTITUDE_M = 10_000_000.0
_MAX_ACCURACY_M = 1_000_000.0
_MAX_DOP = 1_000.0
_MAX_VELOCITY_MPS = 10_000.0
_MAX_COVARIANCE_M2 = 1_000_000_000_000.0
_COVARIANCE_RELATIVE_TOLERANCE = 1e-12
_FIX_REQUIRED_FIELDS = {
    "session_id", "sample_id", "frame_id", "fix_mode",
    "correction_status", "observation_time_status",
}
_FIX_OPTIONAL_FIELDS = {
    "observed_at_sync_ns", "latitude_deg", "longitude_deg", "altitude_m",
    "altitude_reference", "position_covariance_m2", "horizontal_accuracy_m",
    "vertical_accuracy_m", "accuracy_confidence", "hdop", "vdop", "pdop",
    "velocity_enu_mps",
}
_STATUSES = {"sample_available", "no_new_sample", "resync_required", "device_error"}
_FIX_MODES = {"unknown", "no_fix", "fix_2d", "fix_3d"}
_CORRECTION_STATUSES = {"unknown", "autonomous", "differential", "rtk_float", "rtk_fixed"}
_OBSERVATION_TIME_STATUSES = {"known_sync_time", "unknown"}
_ALTITUDE_REFERENCES = {"wgs84_ellipsoid", "mean_sea_level"}
_ACCURACY_CONFIDENCES = {"unknown", "one_sigma", "95_percent"}
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


def _bounded_number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    number = _number(value, name)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}")
    return number


def _cycle(value: Any, name: str) -> tuple[int, int, str, int]:
    cycle = _mapping(value, name, _CYCLE_FIELDS)
    generation = _integer(cycle["generation"], name + ".generation", minimum=1)
    sequence = _integer(cycle["sequence"], name + ".sequence", minimum=1)
    if cycle["intent"] != "sample":
        raise ValueError(f"{name}.intent must be sample")
    state_revision = _integer(cycle["state_revision"], name + ".state_revision", minimum=0)
    if state_revision != 0:
        raise ValueError(f"{name}.state_revision must be zero for sample_exchange")
    return generation, sequence, "sample", state_revision


def validate_position_covariance_m2(value: Any, name: str = "position_covariance_m2") -> None:
    """Validate a row-major ENU covariance matrix for shape and PSD semantics."""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a nine-value sequence")
    if len(value) != 9:
        raise ValueError(f"{name} must contain exactly nine row-major values")

    entries = [
        _bounded_number(
            entry,
            f"{name}[{index}]",
            minimum=-_MAX_COVARIANCE_M2,
            maximum=_MAX_COVARIANCE_M2,
        )
        for index, entry in enumerate(value)
    ]
    matrix = [entries[0:3], entries[3:6], entries[6:9]]
    for row in range(3):
        for column in range(row + 1, 3):
            if not math.isclose(
                matrix[row][column], matrix[column][row], rel_tol=1e-12, abs_tol=0.0
            ):
                raise ValueError(f"{name} must be symmetric")
            symmetric_value = (matrix[row][column] + matrix[column][row]) / 2.0
            matrix[row][column] = symmetric_value
            matrix[column][row] = symmetric_value

    scale = max(abs(entry) for entry in entries)
    if scale == 0:
        return
    normalized = [[entry / scale for entry in row] for row in matrix]
    diagonal = [normalized[index][index] for index in range(3)]
    if any(value < 0.0 for value in diagonal):
        raise ValueError(f"{name} must be positive semidefinite")

    for first, second in ((0, 1), (0, 2), (1, 2)):
        diagonal_product = normalized[first][first] * normalized[second][second]
        off_diagonal_product = normalized[first][second] * normalized[second][first]
        principal_minor = diagonal_product - off_diagonal_product
        tolerance = _COVARIANCE_RELATIVE_TOLERANCE * max(
            abs(diagonal_product), abs(off_diagonal_product)
        )
        if principal_minor < -tolerance:
            raise ValueError(f"{name} must be positive semidefinite")

    determinant_terms = (
        normalized[0][0]
        * (normalized[1][1] * normalized[2][2] - normalized[1][2] * normalized[2][1]),
        -normalized[0][1]
        * (normalized[1][0] * normalized[2][2] - normalized[1][2] * normalized[2][0]),
        normalized[0][2]
        * (normalized[1][0] * normalized[2][1] - normalized[1][1] * normalized[2][0]),
    )
    determinant = sum(determinant_terms)
    determinant_tolerance = _COVARIANCE_RELATIVE_TOLERANCE * sum(
        abs(term) for term in determinant_terms
    )
    if determinant < -determinant_tolerance:
        raise ValueError(f"{name} must be positive semidefinite")


def _validate_velocity(value: Any, name: str) -> None:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a three-value ENU sequence")
    if len(value) != 3:
        raise ValueError(f"{name} must contain exactly three ENU values")
    velocity = [
        _bounded_number(
            entry,
            f"{name}[{index}]",
            minimum=-_MAX_VELOCITY_MPS,
            maximum=_MAX_VELOCITY_MPS,
        )
        for index, entry in enumerate(value)
    ]
    if math.hypot(*velocity) > _MAX_VELOCITY_MPS:
        raise ValueError(f"{name} speed must not exceed {_MAX_VELOCITY_MPS} m/s")


def _validate_fix(fix_value: Any, *, response_session_id: str) -> int:
    fix = _mapping(fix_value, "fix", _FIX_REQUIRED_FIELDS, _FIX_OPTIONAL_FIELDS)
    fix_session_id = _text(fix["session_id"], "fix.session_id")
    if fix_session_id != response_session_id:
        raise ValueError("fix.session_id must match response.session_id")
    sample_id = _integer(fix["sample_id"], "fix.sample_id", minimum=1)
    _text(fix["frame_id"], "fix.frame_id")

    fix_mode = fix["fix_mode"]
    if not isinstance(fix_mode, str) or fix_mode not in _FIX_MODES:
        raise ValueError("fix.fix_mode must be unknown, no_fix, fix_2d, or fix_3d")
    correction_status = fix["correction_status"]
    if not isinstance(correction_status, str) or correction_status not in _CORRECTION_STATUSES:
        raise ValueError(
            "fix.correction_status must be unknown, autonomous, differential, rtk_float, or rtk_fixed"
        )
    if fix_mode in {"unknown", "no_fix"} and correction_status != "unknown":
        raise ValueError("fix.correction_status must be unknown when no valid solution exists")

    observation_time_status = fix["observation_time_status"]
    if not isinstance(observation_time_status, str) or observation_time_status not in _OBSERVATION_TIME_STATUSES:
        raise ValueError("fix.observation_time_status must be known_sync_time or unknown")
    has_observed_at = "observed_at_sync_ns" in fix
    if observation_time_status == "known_sync_time":
        if not has_observed_at:
            raise ValueError("known_sync_time requires fix.observed_at_sync_ns")
        _integer(fix["observed_at_sync_ns"], "fix.observed_at_sync_ns", minimum=0)
    elif has_observed_at:
        raise ValueError("unknown observation time must omit fix.observed_at_sync_ns")

    has_latitude = "latitude_deg" in fix
    has_longitude = "longitude_deg" in fix
    has_altitude = "altitude_m" in fix
    has_altitude_reference = "altitude_reference" in fix
    has_covariance = "position_covariance_m2" in fix
    has_horizontal_accuracy = "horizontal_accuracy_m" in fix
    has_vertical_accuracy = "vertical_accuracy_m" in fix
    has_accuracy_confidence = "accuracy_confidence" in fix
    has_velocity = "velocity_enu_mps" in fix

    if fix_mode in {"unknown", "no_fix"}:
        position_fields = {
            "latitude_deg", "longitude_deg", "altitude_m", "altitude_reference",
            "position_covariance_m2", "horizontal_accuracy_m", "vertical_accuracy_m",
            "accuracy_confidence", "velocity_enu_mps",
        }
        present_position_fields = position_fields & set(fix)
        if present_position_fields:
            raise ValueError(
                f"fix.{fix_mode} must omit position fields: " + ", ".join(sorted(present_position_fields))
            )
    else:
        if not has_latitude or not has_longitude:
            raise ValueError(f"fix.{fix_mode} requires latitude_deg and longitude_deg")
        _bounded_number(fix["latitude_deg"], "fix.latitude_deg", minimum=-90.0, maximum=90.0)
        _bounded_number(fix["longitude_deg"], "fix.longitude_deg", minimum=-180.0, maximum=180.0)

    if fix_mode == "fix_2d" and (has_altitude or has_altitude_reference):
        raise ValueError("fix_2d must omit altitude_m and altitude_reference")
    if has_altitude != has_altitude_reference:
        raise ValueError("fix.altitude_m and fix.altitude_reference must be present together")
    if has_altitude:
        if fix_mode != "fix_3d":
            raise ValueError("fix.altitude_m is allowed only with fix_3d")
        _bounded_number(
            fix["altitude_m"],
            "fix.altitude_m",
            minimum=-_MAX_ALTITUDE_M,
            maximum=_MAX_ALTITUDE_M,
        )
        altitude_reference = fix["altitude_reference"]
        if not isinstance(altitude_reference, str) or altitude_reference not in _ALTITUDE_REFERENCES:
            raise ValueError("fix.altitude_reference must be wgs84_ellipsoid or mean_sea_level")

    if has_covariance:
        if fix_mode != "fix_3d":
            raise ValueError("fix.position_covariance_m2 is allowed only with fix_3d")
        validate_position_covariance_m2(
            fix["position_covariance_m2"], "fix.position_covariance_m2"
        )

    if has_horizontal_accuracy:
        if fix_mode not in {"fix_2d", "fix_3d"}:
            raise ValueError("fix.horizontal_accuracy_m requires a valid fix")
        horizontal_accuracy = _bounded_number(
            fix["horizontal_accuracy_m"],
            "fix.horizontal_accuracy_m",
            minimum=0.0,
            maximum=_MAX_ACCURACY_M,
        )
        if horizontal_accuracy == 0:
            raise ValueError("fix.horizontal_accuracy_m must be positive")
    if has_vertical_accuracy:
        if not has_altitude:
            raise ValueError("fix.vertical_accuracy_m requires altitude_m")
        vertical_accuracy = _bounded_number(
            fix["vertical_accuracy_m"],
            "fix.vertical_accuracy_m",
            minimum=0.0,
            maximum=_MAX_ACCURACY_M,
        )
        if vertical_accuracy == 0:
            raise ValueError("fix.vertical_accuracy_m must be positive")
    if has_accuracy_confidence != (has_horizontal_accuracy or has_vertical_accuracy):
        raise ValueError("fix.accuracy_confidence must be present exactly when an accuracy value is present")
    if has_accuracy_confidence:
        accuracy_confidence = fix["accuracy_confidence"]
        if not isinstance(accuracy_confidence, str) or accuracy_confidence not in _ACCURACY_CONFIDENCES:
            raise ValueError("fix.accuracy_confidence must be unknown, one_sigma, or 95_percent")

    for field_name in ("hdop", "vdop", "pdop"):
        if field_name in fix:
            dop_value = _bounded_number(
                fix[field_name],
                f"fix.{field_name}",
                minimum=0.0,
                maximum=_MAX_DOP,
            )
            if dop_value == 0:
                raise ValueError(f"fix.{field_name} must be positive")

    if has_velocity:
        if fix_mode not in {"fix_2d", "fix_3d"}:
            raise ValueError("fix.velocity_enu_mps requires a valid fix")
        _validate_velocity(fix["velocity_enu_mps"], "fix.velocity_enu_mps")

    return sample_id


def validate_gnss_cycle_response_semantics(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> None:
    """Validate GNSS request/response semantics without assuming encoded size."""
    request = _mapping(request, "request", {"cycle"}, {"expected_session_id", "after_sample_id"})
    response = _mapping(response, "response", {"cycle", "session_id", "status"}, {"fix", "detail"})
    request_cycle = _cycle(request["cycle"], "request.cycle")
    response_cycle = _cycle(response["cycle"], "response.cycle")
    if response_cycle != request_cycle:
        raise ValueError("response.cycle must match request.cycle")

    expected_session_id = request.get("expected_session_id")
    if expected_session_id is not None:
        expected_session_id = _text(expected_session_id, "request.expected_session_id")
    raw_after_sample_id = request.get("after_sample_id")
    after_sample_id = 0 if raw_after_sample_id is None else _integer(
        raw_after_sample_id, "request.after_sample_id", minimum=0
    )
    if expected_session_id is None and after_sample_id != 0:
        raise ValueError("request.after_sample_id must be zero when expected_session_id is omitted")

    session_id = _text(response["session_id"], "response.session_id")
    status = response["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("response.status must be sample_available, no_new_sample, resync_required, or device_error")
    detail = response.get("detail")
    if detail is not None:
        _text(detail, "response.detail")
    fix_value = response.get("fix")
    has_fix = fix_value is not None

    session_mismatch = expected_session_id is not None and expected_session_id != session_id
    if session_mismatch:
        if status != "resync_required" or has_fix:
            raise ValueError("a session mismatch requires resync_required with no fix")
        return

    if status == "resync_required":
        raise ValueError("resync_required requires a changed expected_session_id")
    if status == "sample_available":
        if not has_fix:
            raise ValueError("sample_available requires a fix")
        sample_id = _validate_fix(fix_value, response_session_id=session_id)
        if sample_id <= after_sample_id:
            raise ValueError("fix.sample_id must be greater than request.after_sample_id")
    elif has_fix:
        raise ValueError(f"{status} must not include a fix")


def validate_gnss_cycle_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    response_size_bytes: int,
    max_response_bytes: int,
) -> None:
    """Validate one GNSS response against semantics and a selected byte cap.

    response_size_bytes is measured by the selected codec. The byte limit is
    supplied by the selected source profile/binding and has no policy default.
    """
    selected_response_bytes = _integer(max_response_bytes, "max_response_bytes", minimum=1)
    measured_response_bytes = _integer(response_size_bytes, "response_size_bytes", minimum=1)
    if measured_response_bytes > selected_response_bytes:
        raise ValueError("serialized response exceeds max_response_bytes")
    validate_gnss_cycle_response_semantics(request, response)
