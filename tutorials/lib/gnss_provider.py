from __future__ import annotations

import copy
from typing import Any

from gnss_fixture import GnssFixtureConfiguration
from r2kernel.core.common_control import ControlCycle, ControlContractError


class GnssResponseNormalizer:
    """Validate simulator-only metadata and freeze each latest sample's mapping decision."""

    def __init__(self, fixture: GnssFixtureConfiguration) -> None:
        self._fixture = fixture
        self._cached_session_id: str | None = None
        self._cached_sample_id = 0
        self._cached_sample: dict[str, Any] | None = None
        self._cached_outcome: dict[str, Any] | None = None

    def normalize_response(
        self,
        response: Any,
        *,
        expected_cycle: ControlCycle,
        expected_session_id: str | None,
        before_sync_ns: int,
        after_sync_ns: int,
    ) -> dict[str, Any]:
        internal = self._validate_simulator_response(response, expected_cycle=expected_cycle)
        session_id = internal["session_id"]
        if expected_session_id is not None and expected_session_id != session_id:
            self._clear_sample_cache(session_id)
            return {
                "cycle": expected_cycle.to_mapping(),
                "session_id": session_id,
                "status": "resync_required",
                "detail": "GNSS session changed; reset the sample cursor",
            }

        status = internal["status"]
        if status == "resync_required":
            self._clear_sample_cache(session_id)
            return {
                "cycle": expected_cycle.to_mapping(),
                "session_id": session_id,
                "status": status,
                **({"detail": internal["detail"]} if "detail" in internal else {}),
            }
        if status != "sample_available":
            return {
                "cycle": expected_cycle.to_mapping(),
                "session_id": session_id,
                "status": status,
                **({"detail": internal["detail"]} if "detail" in internal else {}),
            }

        sample = internal["sample"]
        sample_id = sample["sample_id"]
        if self._cached_session_id == session_id:
            if sample_id < self._cached_sample_id:
                raise RuntimeError("simulator replayed an older GNSS sample after a newer identity")
            if sample_id == self._cached_sample_id:
                if sample != self._cached_sample:
                    raise RuntimeError("simulator changed immutable GNSS sample content for an existing identity")
                if self._cached_outcome is None:
                    raise RuntimeError("GNSS sample cache is missing its frozen normalization outcome")
                return {
                    "cycle": expected_cycle.to_mapping(),
                    "session_id": session_id,
                    **copy.deepcopy(self._cached_outcome),
                }

        outcome = self._normalize_new_sample(
            sample,
            session_id=session_id,
            simulator_now_ns=internal["simulator_now_ns"],
            simulator_clock_id=internal["simulator_clock_id"],
            before_sync_ns=before_sync_ns,
            after_sync_ns=after_sync_ns,
        )
        self._cached_session_id = session_id
        self._cached_sample_id = sample_id
        self._cached_sample = copy.deepcopy(sample)
        self._cached_outcome = copy.deepcopy(outcome)
        return {
            "cycle": expected_cycle.to_mapping(),
            "session_id": session_id,
            **copy.deepcopy(outcome),
        }

    def _normalize_new_sample(
        self,
        sample: dict[str, Any],
        *,
        session_id: str,
        simulator_now_ns: int,
        simulator_clock_id: str,
        before_sync_ns: int,
        after_sync_ns: int,
    ) -> dict[str, Any]:
        if self._fixture.clock_status == "known":
            offset_ns = self._fixture.clock_offset_ns
            tolerance_ns = self._fixture.clock_alignment_tolerance_ns
            if offset_ns is None or tolerance_ns is None:
                raise RuntimeError("known GNSS clock mapping is missing its offset or tolerance")
            if simulator_clock_id != self._fixture.source_clock:
                return {
                    "status": "device_error",
                    "detail": "simulator clock ID does not match the explicit GNSS clock mapping",
                }
            mapped_simulator_now_ns = simulator_now_ns + offset_ns
            if not _clock_is_aligned(
                mapped_simulator_now_ns=mapped_simulator_now_ns,
                before_sync_ns=before_sync_ns,
                after_sync_ns=after_sync_ns,
                tolerance_ns=tolerance_ns,
            ):
                return {
                    "status": "device_error",
                    "detail": "configured simulator clock mapping does not align with r2kernel_sync",
                }
        else:
            offset_ns = None
        return {
            "status": "sample_available",
            "fix": _canonical_fix(sample, session_id=session_id, clock_offset_ns=offset_ns),
        }

    def _clear_sample_cache(self, session_id: str) -> None:
        self._cached_session_id = session_id
        self._cached_sample_id = 0
        self._cached_sample = None
        self._cached_outcome = None

    @classmethod
    def _validate_simulator_response(
        cls, response: Any, *, expected_cycle: ControlCycle
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise RuntimeError("simulator gnss_cycle response must be an object")
        allowed_fields = {
            "cycle",
            "session_id",
            "status",
            "detail",
            "simulator_now_ns",
            "simulator_clock_id",
            "sample",
        }
        if set(response) - allowed_fields:
            raise RuntimeError("simulator gnss_cycle response contains unsupported fields")
        session_id = response.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RuntimeError("simulator gnss_cycle response is missing session_id")
        try:
            response_cycle = ControlCycle.from_mapping(response.get("cycle"))
        except ControlContractError as exc:
            raise RuntimeError(f"simulator gnss_cycle response has an invalid cycle: {exc}") from exc
        if response_cycle != expected_cycle:
            raise RuntimeError("simulator gnss_cycle response cycle does not match the request")
        status = response.get("status")
        if status not in {"sample_available", "no_new_sample", "resync_required", "device_error"}:
            raise RuntimeError(f"simulator gnss_cycle response has unsupported status {status!r}")
        detail = response.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise RuntimeError("simulator gnss_cycle detail must be text")
        simulator_now_ns = _integer(response.get("simulator_now_ns"), "simulator_now_ns", minimum=0)
        simulator_clock_id = response.get("simulator_clock_id")
        if not isinstance(simulator_clock_id, str) or not simulator_clock_id.strip():
            raise RuntimeError("simulator_clock_id must be non-empty text")
        sample = response.get("sample")
        if status == "sample_available":
            _validate_sample(sample, session_id=session_id)
            if sample["acquisition_clock_id"] != simulator_clock_id:
                raise RuntimeError("GNSS sample acquisition clock does not match simulator clock")
        elif sample is not None:
            raise RuntimeError("simulator gnss_cycle returned a sample without sample_available status")

        validated: dict[str, Any] = {
            "cycle": expected_cycle.to_mapping(),
            "session_id": session_id,
            "status": status,
            "simulator_now_ns": simulator_now_ns,
            "simulator_clock_id": simulator_clock_id,
        }
        if sample is not None:
            validated["sample"] = sample
        if detail is not None:
            validated["detail"] = detail
        return validated


def _validate_sample(sample: Any, *, session_id: str) -> None:
    required_fields = {
        "session_id",
        "sample_id",
        "frame_id",
        "fix_mode",
        "correction_status",
        "acquired_at_clock_ns",
        "acquisition_clock_id",
    }
    optional_fields = {
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "altitude_reference",
        "position_covariance_m2",
        "horizontal_accuracy_m",
        "vertical_accuracy_m",
        "accuracy_confidence",
        "hdop",
        "vdop",
        "pdop",
        "velocity_enu_mps",
    }
    if not isinstance(sample, dict):
        raise RuntimeError("simulator GNSS sample must be an object")
    if not required_fields.issubset(sample) or set(sample) - required_fields - optional_fields:
        raise RuntimeError("simulator GNSS sample has missing or unsupported fields")
    if sample.get("session_id") != session_id:
        raise RuntimeError("simulator GNSS sample session_id does not match its response")
    _integer(sample.get("sample_id"), "sample.sample_id", minimum=1)
    _integer(sample.get("acquired_at_clock_ns"), "sample.acquired_at_clock_ns", minimum=0)
    for field_name in ("frame_id", "acquisition_clock_id"):
        if not isinstance(sample.get(field_name), str) or not sample[field_name].strip():
            raise RuntimeError(f"simulator GNSS sample {field_name} must be non-empty text")


def _canonical_fix(
    sample: dict[str, Any], *, session_id: str, clock_offset_ns: int | None
) -> dict[str, Any]:
    fix_fields = {
        "sample_id",
        "frame_id",
        "fix_mode",
        "correction_status",
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "altitude_reference",
        "position_covariance_m2",
        "horizontal_accuracy_m",
        "vertical_accuracy_m",
        "accuracy_confidence",
        "hdop",
        "vdop",
        "pdop",
        "velocity_enu_mps",
    }
    fix: dict[str, Any] = {
        "session_id": session_id,
        **{key: value for key, value in sample.items() if key in fix_fields},
        "observation_time_status": "unknown",
    }
    if clock_offset_ns is not None:
        observed_at_sync_ns = sample["acquired_at_clock_ns"] + clock_offset_ns
        if observed_at_sync_ns < 0:
            raise RuntimeError("mapped GNSS observation time is negative")
        fix["observation_time_status"] = "known_sync_time"
        fix["observed_at_sync_ns"] = observed_at_sync_ns
    return fix


def _clock_is_aligned(
    *,
    mapped_simulator_now_ns: int,
    before_sync_ns: int,
    after_sync_ns: int,
    tolerance_ns: int,
) -> bool:
    return before_sync_ns - tolerance_ns <= mapped_simulator_now_ns <= after_sync_ns + tolerance_ns


def _integer(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"{field_name} must be an integer greater than or equal to {minimum}")
    return value
