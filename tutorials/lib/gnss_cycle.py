from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gnss_profile import validate_gnss_cycle_response_semantics


@dataclass(frozen=True)
class GnssMeasurement:
    session_id: str
    sample_id: int
    frame_id: str
    fix_mode: str
    observed_at_sync_ns: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float | None
    altitude_reference: str | None
    position_covariance_m2: tuple[float, ...]


class GnssSampleTracker:
    """Track latest-only receiver identity, measurement freshness, and usability."""

    def __init__(self, *, expected_frame_id: str) -> None:
        if not expected_frame_id.strip():
            raise ValueError("expected_frame_id must not be empty")
        self.expected_frame_id = expected_frame_id
        self.session_id: str | None = None
        self.last_sample_id = 0
        self.measurement: GnssMeasurement | None = None
        self.status = "acquiring"
        self.detail: str | None = None

    def reset(self) -> None:
        self.session_id = None
        self.last_sample_id = 0
        self.measurement = None
        self.status = "acquiring"
        self.detail = None

    def request_cursor(self) -> dict[str, Any]:
        request: dict[str, Any] = {"after_sample_id": self.last_sample_id}
        if self.session_id is not None:
            request["expected_session_id"] = self.session_id
        return request

    def apply_response(
        self,
        response: Any,
        *,
        expected_cycle: dict[str, Any],
        now_sync_ns: int,
        maximum_measurement_age_ns: int,
        future_timestamp_tolerance_ns: int,
    ) -> str:
        self.refresh_usability(
            now_sync_ns=now_sync_ns,
            maximum_measurement_age_ns=maximum_measurement_age_ns,
            future_timestamp_tolerance_ns=future_timestamp_tolerance_ns,
        )
        request = {"cycle": expected_cycle, **self.request_cursor()}
        validate_gnss_cycle_response_semantics(request, response)
        response_value = response
        session_id = response_value["session_id"]
        status = response_value["status"]
        if status == "resync_required":
            self.session_id = session_id
            self.last_sample_id = 0
            self.measurement = None
            self.status = "resync_required"
            self.detail = response_value.get("detail")
            return self.status

        if self.session_id is None:
            self.session_id = session_id
        elif session_id != self.session_id:
            raise ValueError("GNSS session changed without resync_required")

        if status == "no_new_sample":
            if self.measurement is not None:
                self.status = "localized"
            elif self.status in {"acquiring", "device_error", "resync_required"}:
                self.status = "acquiring"
                self.detail = None
            return self.status
        if status == "device_error":
            self.status = "device_error"
            self.detail = response_value.get("detail") or "GNSS receiver reported a device error"
            return self.status

        fix = response_value["fix"]
        sample_id = fix["sample_id"]
        if sample_id <= self.last_sample_id:
            raise ValueError("GNSS sample_id must strictly increase within its session")
        self.last_sample_id = sample_id
        self.detail = None

        fix_mode = fix["fix_mode"]
        if fix_mode == "no_fix":
            self.measurement = None
            self.status = "no_fix"
            return self.status
        if fix_mode == "unknown":
            self.measurement = None
            self.status = "unknown_fix_mode"
            return self.status

        if fix["frame_id"] != self.expected_frame_id:
            self.measurement = None
            self.status = "unexpected_antenna_frame"
            self.detail = (
                f"GNSS antenna frame {fix['frame_id']!r} does not match "
                f"configured frame {self.expected_frame_id!r}"
            )
            return self.status
        if fix["observation_time_status"] == "unknown":
            self.measurement = None
            self.status = "unknown_measurement_time"
            return self.status

        observed_at_sync_ns = fix["observed_at_sync_ns"]
        if observed_at_sync_ns > now_sync_ns + future_timestamp_tolerance_ns:
            self.measurement = None
            self.status = "future_measurement_time"
            self.detail = "GNSS observation exceeds the configured future-time tolerance"
            return self.status
        if now_sync_ns - observed_at_sync_ns > maximum_measurement_age_ns:
            self.measurement = None
            self.status = "stale_measurement"
            self.detail = "GNSS observation exceeds the configured maximum measurement age"
            return self.status

        covariance = fix.get("position_covariance_m2")
        if covariance is None:
            self.measurement = None
            self.status = "uncertainty_unavailable"
            self.detail = "GNSS sample lacks the covariance required by the 6x6 pose output"
            return self.status

        altitude_m = fix.get("altitude_m")
        altitude_reference = fix.get("altitude_reference")
        if altitude_m is not None and altitude_reference != "wgs84_ellipsoid":
            self.measurement = None
            self.status = "unsupported_altitude_reference"
            self.detail = "Localizer has no configured geoid model for mean_sea_level altitude"
            return self.status
        self.measurement = GnssMeasurement(
            session_id=session_id,
            sample_id=sample_id,
            frame_id=fix["frame_id"],
            fix_mode=fix_mode,
            observed_at_sync_ns=observed_at_sync_ns,
            latitude_deg=fix["latitude_deg"],
            longitude_deg=fix["longitude_deg"],
            altitude_m=altitude_m,
            altitude_reference=altitude_reference,
            position_covariance_m2=tuple(covariance),
        )
        self.status = "localized"
        return self.status

    def refresh_usability(
        self,
        *,
        now_sync_ns: int,
        maximum_measurement_age_ns: int,
        future_timestamp_tolerance_ns: int,
    ) -> str:
        """Re-evaluate retained measurement time validity against the current sync clock."""
        if isinstance(now_sync_ns, bool) or not isinstance(now_sync_ns, int) or now_sync_ns < 0:
            raise ValueError("now_sync_ns must be a non-negative integer")
        if (
            isinstance(maximum_measurement_age_ns, bool)
            or not isinstance(maximum_measurement_age_ns, int)
            or maximum_measurement_age_ns <= 0
        ):
            raise ValueError("maximum_measurement_age_ns must be a positive integer")
        if (
            isinstance(future_timestamp_tolerance_ns, bool)
            or not isinstance(future_timestamp_tolerance_ns, int)
            or future_timestamp_tolerance_ns < 0
        ):
            raise ValueError("future_timestamp_tolerance_ns must be a non-negative integer")
        if self.measurement is None:
            return self.status
        timestamp = self.measurement.observed_at_sync_ns
        if timestamp > now_sync_ns + future_timestamp_tolerance_ns:
            self.measurement = None
            self.status = "future_measurement_time"
            self.detail = "GNSS observation exceeds the configured future-time tolerance"
            return self.status
        age_ns = now_sync_ns - self.measurement.observed_at_sync_ns
        if age_ns <= maximum_measurement_age_ns:
            if self.status in {"stale_measurement", "future_measurement_time"}:
                self.status = "localized"
                self.detail = None
            return self.status
        self.measurement = None
        self.status = "stale_measurement"
        self.detail = "GNSS observation exceeded the configured maximum measurement age"
        return self.status

    def mark_device_error(self, detail: str) -> str:
        self.status = "device_error"
        self.detail = str(detail).strip() or "GNSS source is unavailable"
        return self.status
