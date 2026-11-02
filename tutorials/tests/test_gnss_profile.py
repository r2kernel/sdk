import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from gnss_profile import (  # noqa: E402
    validate_gnss_cycle_response,
    validate_gnss_cycle_response_semantics,
)


def cycle(sequence=5):
    return {"generation": 2, "sequence": sequence, "intent": "sample", "state_revision": 0}


def request(*, expected_session_id="gnss-process-1", after_sample_id=1, sequence=5):
    result = {"cycle": cycle(sequence)}
    if expected_session_id is not None:
        result["expected_session_id"] = expected_session_id
    if after_sample_id is not None:
        result["after_sample_id"] = after_sample_id
    return result


def fix(
    *,
    session_id="gnss-process-1",
    sample_id=2,
    fix_mode="fix_2d",
    observation_time_status="unknown",
):
    result = {
        "session_id": session_id,
        "sample_id": sample_id,
        "frame_id": "gnss_antenna",
        "fix_mode": fix_mode,
        "correction_status": "unknown" if fix_mode in {"unknown", "no_fix"} else "autonomous",
        "observation_time_status": observation_time_status,
    }
    if fix_mode in {"fix_2d", "fix_3d"}:
        result.update({"latitude_deg": 37.5, "longitude_deg": -122.25})
    return result


def response(*, status="sample_available", session_id="gnss-process-1", selected_fix=None, sequence=5):
    result = {"cycle": cycle(sequence), "session_id": session_id, "status": status}
    if selected_fix is not None:
        result["fix"] = selected_fix
    return result


def validate(request_value=None, response_value=None, *, response_size_bytes=128, max_response_bytes=512):
    validate_gnss_cycle_response(
        request_value or request(),
        response_value or response(selected_fix=fix()),
        response_size_bytes=response_size_bytes,
        max_response_bytes=max_response_bytes,
    )


def test_valid_2d_fix_with_accuracy_dop_and_enu_velocity_is_accepted():
    selected_fix = fix()
    selected_fix.update(
        {
            "horizontal_accuracy_m": 0.8,
            "accuracy_confidence": "95_percent",
            "hdop": 0.9,
            "pdop": 1.2,
            "velocity_enu_mps": [1.0, -2.0, 0.0],
        }
    )
    validate(response_value=response(selected_fix=selected_fix))


def test_valid_3d_fix_with_optional_altitude_reference_and_covariance_is_accepted():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix.update(
        {
            "altitude_m": 14.0,
            "altitude_reference": "wgs84_ellipsoid",
            "position_covariance_m2": [4.0, 0.5, 0.0, 0.5, 9.0, 0.0, 0.0, 0.0, 16.0],
            "horizontal_accuracy_m": 2.0,
            "vertical_accuracy_m": 4.0,
            "accuracy_confidence": "one_sigma",
        }
    )
    validate(response_value=response(selected_fix=selected_fix))


def test_3d_fix_can_omit_unavailable_altitude():
    validate(response_value=response(selected_fix=fix(fix_mode="fix_3d")))


def test_latest_only_response_may_skip_intermediate_sample_ids():
    selected_request = request(after_sample_id=10)
    selected_fix = fix(sample_id=19)
    validate(request_value=selected_request, response_value=response(selected_fix=selected_fix))
    validate_gnss_cycle_response_semantics(
        selected_request,
        response(selected_fix=selected_fix),
    )


def test_bootstrap_can_discover_session_and_latest_sample():
    selected_request = request(expected_session_id=None, after_sample_id=None)
    selected_fix = fix(sample_id=42, fix_mode="no_fix")
    validate(request_value=selected_request, response_value=response(selected_fix=selected_fix))


def test_no_fix_is_a_valid_sample_without_position():
    validate(response_value=response(selected_fix=fix(fix_mode="no_fix")))


def test_no_fix_may_carry_known_acquisition_time():
    selected_fix = fix(fix_mode="no_fix", observation_time_status="known_sync_time")
    selected_fix["observed_at_sync_ns"] = 0
    validate(response_value=response(selected_fix=selected_fix))


def test_unknown_observation_time_must_omit_timestamp():
    selected_fix = fix()
    selected_fix["observed_at_sync_ns"] = 123
    with pytest.raises(ValueError, match="unknown observation time must omit"):
        validate(response_value=response(selected_fix=selected_fix))


def test_known_observation_time_requires_nonnegative_sync_timestamp():
    selected_fix = fix(observation_time_status="known_sync_time")
    with pytest.raises(ValueError, match="requires fix.observed_at_sync_ns"):
        validate(response_value=response(selected_fix=selected_fix))
    selected_fix["observed_at_sync_ns"] = -1
    with pytest.raises(ValueError, match="observed_at_sync_ns"):
        validate(response_value=response(selected_fix=selected_fix))


def test_no_fix_cannot_include_position():
    selected_fix = fix(fix_mode="no_fix")
    selected_fix["latitude_deg"] = 37.5
    with pytest.raises(ValueError, match="no_fix must omit position fields"):
        validate(response_value=response(selected_fix=selected_fix))


def test_unknown_mode_cannot_include_position_fields():
    selected_fix = fix(fix_mode="unknown")
    selected_fix["longitude_deg"] = 10.0
    with pytest.raises(ValueError, match="unknown must omit position fields"):
        validate(response_value=response(selected_fix=selected_fix))


@pytest.mark.parametrize("fix_mode", ["unknown", "no_fix"])
def test_unknown_or_missing_fix_cannot_claim_a_solution_class(fix_mode):
    selected_fix = fix(fix_mode=fix_mode)
    selected_fix["correction_status"] = "rtk_fixed"
    with pytest.raises(ValueError, match="must be unknown when no valid solution exists"):
        validate(response_value=response(selected_fix=selected_fix))


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("latitude_deg", 90.001, "latitude_deg"),
        ("longitude_deg", -180.001, "longitude_deg"),
        ("latitude_deg", float("nan"), "finite number"),
        ("longitude_deg", float("inf"), "finite number"),
    ],
)
def test_invalid_geodetic_coordinates_are_rejected(field, value, match):
    selected_fix = fix()
    selected_fix[field] = value
    with pytest.raises(ValueError, match=match):
        validate(response_value=response(selected_fix=selected_fix))


def test_valid_fix_requires_both_wgs84_coordinates():
    selected_fix = fix()
    del selected_fix["latitude_deg"]
    with pytest.raises(ValueError, match="requires latitude_deg and longitude_deg"):
        validate(response_value=response(selected_fix=selected_fix))


def test_altitude_requires_reference_and_is_only_valid_for_3d_fix():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["altitude_m"] = 12.5
    with pytest.raises(ValueError, match="must be present together"):
        validate(response_value=response(selected_fix=selected_fix))
    selected_fix["altitude_reference"] = "mean_sea_level"
    validate(response_value=response(selected_fix=selected_fix))

    two_dimensional_fix = fix()
    two_dimensional_fix.update({"altitude_m": 12.5, "altitude_reference": "mean_sea_level"})
    with pytest.raises(ValueError, match="fix_2d must omit"):
        validate(response_value=response(selected_fix=two_dimensional_fix))


def test_vertical_accuracy_requires_altitude_and_accuracy_confidence():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["vertical_accuracy_m"] = 1.0
    selected_fix["accuracy_confidence"] = "unknown"
    with pytest.raises(ValueError, match="vertical_accuracy_m requires altitude_m"):
        validate(response_value=response(selected_fix=selected_fix))


def test_confidence_must_appear_exactly_when_accuracy_is_reported():
    selected_fix = fix()
    selected_fix["horizontal_accuracy_m"] = 1.0
    with pytest.raises(ValueError, match="accuracy_confidence must be present"):
        validate(response_value=response(selected_fix=selected_fix))

    selected_fix["accuracy_confidence"] = "not_documented"
    with pytest.raises(ValueError, match="accuracy_confidence must be unknown"):
        validate(response_value=response(selected_fix=selected_fix))


def test_covariance_requires_nine_symmetric_psd_values():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["position_covariance_m2"] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    validate(response_value=response(selected_fix=selected_fix))

    selected_fix["position_covariance_m2"] = [1.0, 2.0, 0.0, 2.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    with pytest.raises(ValueError, match="positive semidefinite"):
        validate(response_value=response(selected_fix=selected_fix))


def test_covariance_must_be_symmetric_and_bounded():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["position_covariance_m2"] = [1.0, 0.5, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 1.0]
    with pytest.raises(ValueError, match="symmetric"):
        validate(response_value=response(selected_fix=selected_fix))
    selected_fix["position_covariance_m2"] = [1e20, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    with pytest.raises(ValueError, match="position_covariance_m2"):
        validate(response_value=response(selected_fix=selected_fix))


@pytest.mark.parametrize("scale", [1e-12, 1.0, 1e12])
def test_covariance_psd_validation_is_scale_invariant(scale):
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["position_covariance_m2"] = [scale, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, scale]
    validate(response_value=response(selected_fix=selected_fix))

    selected_fix["position_covariance_m2"] = [
        0.25 * scale, 0.375 * scale, 0.0,
        0.375 * scale, 0.25 * scale, 0.0,
        0.0, 0.0, scale,
    ]
    with pytest.raises(ValueError, match="positive semidefinite"):
        validate(response_value=response(selected_fix=selected_fix))


def test_covariance_psd_check_handles_anisotropic_and_rank_deficient_matrices():
    selected_fix = fix(fix_mode="fix_3d")
    selected_fix["position_covariance_m2"] = [
        1e-5, 0.0, 0.0,
        0.0, 2e-5, 0.0,
        0.0, 0.0, 100.0,
    ]
    validate(response_value=response(selected_fix=selected_fix))

    selected_fix["position_covariance_m2"] = [
        1e-5, 2e-5, 0.0,
        2e-5, 1e-5, 0.0,
        0.0, 0.0, 100.0,
    ]
    with pytest.raises(ValueError, match="positive semidefinite"):
        validate(response_value=response(selected_fix=selected_fix))

    selected_fix["position_covariance_m2"] = [1.0] * 9
    validate(response_value=response(selected_fix=selected_fix))
    selected_fix["position_covariance_m2"] = [0.0] * 9
    validate(response_value=response(selected_fix=selected_fix))


def test_velocity_is_rejected_without_valid_fix_and_when_unbounded():
    selected_fix = fix(fix_mode="no_fix")
    selected_fix["velocity_enu_mps"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match="no_fix must omit position fields"):
        validate(response_value=response(selected_fix=selected_fix))

    selected_fix = fix()
    selected_fix["velocity_enu_mps"] = [9000.0, 9000.0, 0.0]
    with pytest.raises(ValueError, match="speed must not exceed"):
        validate(response_value=response(selected_fix=selected_fix))


def test_dop_must_be_positive_finite_and_bounded():
    selected_fix = fix()
    selected_fix["hdop"] = 0.0
    with pytest.raises(ValueError, match="hdop must be positive"):
        validate(response_value=response(selected_fix=selected_fix))
    selected_fix["hdop"] = float("inf")
    with pytest.raises(ValueError, match="finite number"):
        validate(response_value=response(selected_fix=selected_fix))


def test_response_cycle_and_session_must_match_request_and_fix():
    with pytest.raises(ValueError, match="must match request.cycle"):
        validate(response_value=response(selected_fix=fix(), sequence=6))

    mismatched_fix = fix(session_id="other")
    with pytest.raises(ValueError, match="fix.session_id"):
        validate(response_value=response(selected_fix=mismatched_fix))


def test_session_change_requires_resync_without_fix():
    selected_request = request(expected_session_id="old-session")
    validate(
        request_value=selected_request,
        response_value=response(status="resync_required", session_id="new-session"),
    )
    with pytest.raises(ValueError, match="session mismatch"):
        validate(
            request_value=selected_request,
            response_value=response(status="sample_available", session_id="new-session",
                                    selected_fix=fix(session_id="new-session")),
        )
    with pytest.raises(ValueError, match="requires a changed expected_session_id"):
        validate(response_value=response(status="resync_required"))


@pytest.mark.parametrize("status", ["no_new_sample", "resync_required", "device_error"])
def test_non_sample_statuses_cannot_include_a_fix(status):
    with pytest.raises(ValueError, match="must not include a fix|requires a changed"):
        validate(response_value=response(status=status, selected_fix=fix()))


def test_sample_available_requires_a_fix():
    with pytest.raises(ValueError, match="sample_available requires a fix"):
        validate(response_value=response(status="sample_available"))


def test_unanchored_request_cannot_use_nonzero_cursor():
    with pytest.raises(ValueError, match="must be zero when expected_session_id is omitted"):
        validate(
            request_value=request(expected_session_id=None, after_sample_id=3),
            response_value=response(status="no_new_sample"),
        )


def test_unknown_fields_and_response_size_limit_are_rejected():
    selected_fix = fix()
    selected_fix["yaw_rad"] = 1.0
    with pytest.raises(ValueError, match="unknown yaw_rad"):
        validate(response_value=response(selected_fix=selected_fix))

    with pytest.raises(ValueError, match="max_response_bytes"):
        validate(response_size_bytes=513, max_response_bytes=512)


def test_selected_response_limits_must_be_positive_integers():
    with pytest.raises(ValueError, match="max_response_bytes"):
        validate(max_response_bytes=0)
    with pytest.raises(ValueError, match="response_size_bytes"):
        validate(response_size_bytes=True)
