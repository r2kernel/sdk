import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from radar_profile import validate_radar_cycle_response  # noqa: E402


def cycle(sequence=4):
    return {"generation": 2, "sequence": sequence, "intent": "sample", "state_revision": 0}


def request(*, expected_session_id="radar-process-1", after_sample_id=2, sequence=4):
    result = {"cycle": cycle(sequence)}
    if expected_session_id is not None:
        result["expected_session_id"] = expected_session_id
    if after_sample_id is not None:
        result["after_sample_id"] = after_sample_id
    return result


def radar_return(*, azimuth_rad=6.2, observed_at_sync_ns=150, signal_strength=0.75):
    return {"range_m": 3.25, "azimuth_rad": azimuth_rad, "radial_velocity_mps": -1.5,
            "signal_strength": signal_strength, "observed_at_sync_ns": observed_at_sync_ns}


def sector(*, sample_id=3, session_id="radar-process-1", azimuth_start_rad=6.0,
           azimuth_span_rad=0.5, returns=None):
    return {"session_id": session_id, "sample_id": sample_id, "revolution_id": 8,
            "azimuth_start_rad": azimuth_start_rad, "azimuth_span_rad": azimuth_span_rad,
            "scan_started_at_sync_ns": 100, "scan_ended_at_sync_ns": 200,
            "frame_id": "radar_sensor", "revolution_complete": False,
            "returns": [radar_return()] if returns is None else returns}


def response(*, status="sample_available", session_id="radar-process-1", sample=None, sequence=4):
    result = {"cycle": cycle(sequence), "session_id": session_id, "status": status}
    if sample is not None:
        result["sector"] = sample
    return result


def validate(request_value=None, response_value=None, *, max_return_count=4,
             response_size_bytes=256, max_response_bytes=512):
    validate_radar_cycle_response(
        request_value or request(), response_value or response(sample=sector()),
        max_return_count=max_return_count, response_size_bytes=response_size_bytes,
        max_response_bytes=max_response_bytes,
    )


def test_valid_wrapped_sector_and_return_are_accepted():
    validate()


def test_return_azimuth_near_two_pi_keeps_float64_boundary():
    validate(response_value=response(sample=sector(returns=[radar_return(azimuth_rad=math.tau - 1e-10)])))


@pytest.mark.parametrize("azimuth_rad", [0.3000005, 0.0999995])
def test_rounded_return_azimuth_within_endpoint_tolerance_is_accepted(azimuth_rad):
    selected_sector = sector(azimuth_start_rad=0.1, azimuth_span_rad=0.2,
                             returns=[radar_return(azimuth_rad=azimuth_rad)])
    validate(response_value=response(sample=selected_sector))


def test_return_azimuth_beyond_endpoint_tolerance_is_rejected():
    selected_sector = sector(azimuth_start_rad=0.1, azimuth_span_rad=0.2,
                             returns=[radar_return(azimuth_rad=0.300002)])
    with pytest.raises(ValueError, match="outside the sector sweep"):
        validate(response_value=response(sample=selected_sector))


def test_normalized_signal_strength_includes_zero_and_profile_maximum():
    validate(response_value=response(sample=sector(returns=[radar_return(signal_strength=0.0)])))
    validate(response_value=response(sample=sector(returns=[radar_return(signal_strength=1.0)])))


def test_empty_returns_are_valid_completed_coverage():
    selected = sector(returns=[])
    selected["azimuth_span_rad"] = math.tau
    selected["revolution_complete"] = True
    validate(response_value=response(sample=selected))


def test_bootstrap_reports_session_and_can_return_first_sector():
    validate(
        request_value=request(expected_session_id=None, after_sample_id=0),
        response_value=response(sample=sector(sample_id=42)),
    )
    validate(
        request_value=request(expected_session_id=None, after_sample_id=None),
        response_value=response(status="no_new_sample"),
    )


def test_changed_process_session_requires_resync_without_sector():
    selected_response = response(status="resync_required", session_id="radar-process-2")
    validate(response_value=selected_response)
    selected_response["status"] = "no_new_sample"
    with pytest.raises(ValueError, match="session mismatch"):
        validate(response_value=selected_response)


def test_evicted_same_session_cursor_requires_resync_without_sector():
    validate(response_value=response(status="resync_required"))


@pytest.mark.parametrize("status", ["no_new_sample", "resync_required", "device_error"])
def test_non_sample_status_cannot_include_sector(status):
    with pytest.raises(ValueError, match="must not include a sector"):
        validate(response_value=response(status=status, sample=sector()))


def test_sample_status_requires_sector():
    with pytest.raises(ValueError, match="requires a sector"):
        validate(response_value=response(status="sample_available"))


def test_response_must_echo_the_request_cycle():
    with pytest.raises(ValueError, match="must match request.cycle"):
        validate(response_value=response(sample=sector(), sequence=5))


def test_sector_session_and_cursor_are_cross_checked():
    with pytest.raises(ValueError, match="sector.session_id"):
        validate(response_value=response(sample=sector(session_id="different-process")))
    with pytest.raises(ValueError, match="greater than request.after_sample_id"):
        validate(response_value=response(sample=sector(sample_id=2)))
    with pytest.raises(ValueError, match="cannot skip unseen sectors"):
        validate(response_value=response(sample=sector(sample_id=4)))


def test_unknown_fields_are_rejected_including_uncontracted_target_annotations():
    selected_sector = sector()
    selected_sector["object_class"] = "vehicle"
    with pytest.raises(ValueError, match="unknown object_class"):
        validate(response_value=response(sample=selected_sector))


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("sample_id", 0, "sample_id"),
        ("revolution_id", True, "revolution_id"),
        ("azimuth_start_rad", math.tau, "azimuth_start_rad"),
        ("azimuth_span_rad", 0, "azimuth_span_rad"),
        ("azimuth_span_rad", math.tau + 0.01, "azimuth_span_rad"),
        ("scan_ended_at_sync_ns", 100, "must follow"),
        ("frame_id", "  ", "frame_id"),
        ("revolution_complete", 1, "revolution_complete"),
    ],
)
def test_invalid_sector_profile_values_are_rejected(field, value, match):
    selected_sector = sector()
    selected_sector[field] = value
    with pytest.raises(ValueError, match=match):
        validate(response_value=response(sample=selected_sector))


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("range_m", 0, "range_m"),
        ("range_m", math.inf, "range_m"),
        ("range_m", 1e100, "float32"),
        ("azimuth_rad", math.tau, "azimuth_rad"),
        ("azimuth_rad", 5.0, "outside the sector"),
        ("radial_velocity_mps", math.nan, "radial_velocity_mps"),
        ("signal_strength", -1.0, "signal_strength"),
        ("signal_strength", 1.01, "signal_strength"),
        ("signal_strength", math.inf, "signal_strength"),
        ("observed_at_sync_ns", 201, "within the sector scan interval"),
    ],
)
def test_invalid_return_profile_values_are_rejected(field, value, match):
    selected_return = radar_return()
    selected_return[field] = value
    with pytest.raises(ValueError, match=match):
        validate(response_value=response(sample=sector(returns=[selected_return])))


def test_response_return_and_encoded_byte_limits_are_selected_not_hardcoded():
    selected_sector = sector(returns=[radar_return(), radar_return(azimuth_rad=6.25)])
    validate(response_value=response(sample=selected_sector), max_return_count=2)
    with pytest.raises(ValueError, match="max_return_count"):
        validate(response_value=response(sample=selected_sector), max_return_count=1)
    with pytest.raises(ValueError, match="max_response_bytes"):
        validate(response_size_bytes=513, max_response_bytes=512)


def test_bootstrap_cannot_use_an_unanchored_nonzero_cursor():
    with pytest.raises(ValueError, match="must be zero when expected_session_id is omitted"):
        validate(
            request_value=request(expected_session_id=None, after_sample_id=3),
            response_value=response(status="no_new_sample"),
        )


def test_selected_limits_and_response_size_must_be_positive_integers():
    with pytest.raises(ValueError, match="max_return_count"):
        validate(max_return_count=0)
    with pytest.raises(ValueError, match="response_size_bytes"):
        validate(response_size_bytes=True)
