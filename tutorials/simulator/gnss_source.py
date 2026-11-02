from __future__ import annotations

import copy
import math
import threading
import uuid
from pathlib import Path
from typing import Any

from common import robot_cleaner_root
from gnss_fixture import GnssFixtureConfiguration, load_gnss_fixture
from gnss_math import local_enu_to_geodetic
from gnss_mount import GnssMount, load_gnss_mount


class GnssReceiverSimulator:
    """Finite-rate simulated receiver that exposes only its latest antenna fix."""

    def __init__(self, *, fixture_path: Path | None = None) -> None:
        tutorials_root = robot_cleaner_root(__file__)
        self._fixture_path = fixture_path or tutorials_root / "config" / "gnss_fixture.yaml"
        self._fixture: GnssFixtureConfiguration = load_gnss_fixture(self._fixture_path)
        self._mount: GnssMount = load_gnss_mount(
            tutorials_root / "subsystems" / "compound.cleaner" / "subsystem.yaml",
            tutorials_root / "subsystems" / "component.localizer" / "subsystem.yaml",
        )
        self._sample_period_ns = self._fixture.simulator_sample_period_ns
        self._lock = threading.RLock()
        self._session_id = uuid.uuid4().hex
        self._sample_id = 0
        self._latest_sample: dict[str, Any] | None = None
        self._last_sample_end_ns = 0
        self._capture_error: str | None = None

    @property
    def sample_period_ns(self) -> int:
        return self._sample_period_ns

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def reset(self, *, now_ns: int) -> None:
        current_time_ns = _integer(now_ns, "now_ns", minimum=0)
        with self._lock:
            self._session_id = uuid.uuid4().hex
            self._sample_id = 0
            self._latest_sample = None
            self._last_sample_end_ns = current_time_ns
            self._capture_error = None

    def capture(
        self,
        *,
        body_x_m: float,
        body_y_m: float,
        body_yaw_rad: float,
        now_ns: int,
    ) -> bool:
        current_time_ns = _integer(now_ns, "now_ns", minimum=0)
        body_x = _finite(body_x_m, "body_x_m")
        body_y = _finite(body_y_m, "body_y_m")
        body_yaw = _finite(body_yaw_rad, "body_yaw_rad")
        with self._lock:
            if self._capture_error is not None:
                raise RuntimeError(f"GNSS acquisition is faulted: {self._capture_error}")
            elapsed_ns = current_time_ns - self._last_sample_end_ns
            if elapsed_ns < 0:
                self._capture_error = "GNSS acquisition clock moved backwards"
                raise RuntimeError(self._capture_error)
            if elapsed_ns < self._sample_period_ns:
                return False
            if self._sample_id >= (1 << 63) - 1:
                self._capture_error = "GNSS sample_id exceeded the positive int64 range"
                raise RuntimeError(self._capture_error)

            cosine_yaw = math.cos(body_yaw)
            sine_yaw = math.sin(body_yaw)
            antenna_east_m = (
                body_x
                + cosine_yaw * self._mount.translation_x_m
                - sine_yaw * self._mount.translation_y_m
            )
            antenna_north_m = (
                body_y
                + sine_yaw * self._mount.translation_x_m
                + cosine_yaw * self._mount.translation_y_m
            )
            antenna_up_m = self._mount.translation_z_m
            latitude_deg, longitude_deg, altitude_m = local_enu_to_geodetic(
                antenna_east_m,
                antenna_north_m,
                antenna_up_m,
                self._fixture.geographic_origin,
            )
            self._sample_id += 1
            self._latest_sample = {
                "session_id": self._session_id,
                "sample_id": self._sample_id,
                "frame_id": self._mount.frame_id,
                "fix_mode": "fix_3d",
                "correction_status": "autonomous",
                "latitude_deg": latitude_deg,
                "longitude_deg": longitude_deg,
                "altitude_m": altitude_m,
                "altitude_reference": "wgs84_ellipsoid",
                "position_covariance_m2": list(
                    self._fixture.simulator_position_covariance_m2_enu
                ),
                "acquired_at_clock_ns": current_time_ns,
                "acquisition_clock_id": self._fixture.simulator_clock_id,
            }
            self._last_sample_end_ns = current_time_ns
            return True

    def read_cycle(self, params: dict[str, Any] | None, *, now_ns: int) -> dict[str, Any]:
        request = _validate_request(params)
        simulator_now_ns = _integer(now_ns, "now_ns", minimum=0)
        with self._lock:
            response: dict[str, Any] = {
                "cycle": dict(request["cycle"]),
                "session_id": self._session_id,
                "status": "no_new_sample",
                "simulator_now_ns": simulator_now_ns,
                "simulator_clock_id": self._fixture.simulator_clock_id,
            }
            expected_session_id = request.get("expected_session_id")
            after_sample_id = request.get("after_sample_id", 0)
            if expected_session_id is not None and expected_session_id != self._session_id:
                response.update(
                    status="resync_required",
                    detail="GNSS session changed; reset the sample cursor",
                )
                return response
            if self._capture_error is not None:
                response.update(status="device_error", detail=self._capture_error)
                return response
            if self._latest_sample is None:
                if after_sample_id != 0:
                    raise ValueError("after_sample_id is ahead of the current GNSS session")
                return response

            latest_sample_id = int(self._latest_sample["sample_id"])
            if after_sample_id > latest_sample_id:
                raise ValueError("after_sample_id is ahead of the current GNSS session")
            if latest_sample_id <= after_sample_id:
                return response
            response["status"] = "sample_available"
            response["sample"] = copy.deepcopy(self._latest_sample)
            return response

    def record_capture_error(self, error: BaseException | str) -> None:
        detail = str(error).strip() or "GNSS capture failed without a diagnostic"
        with self._lock:
            self._capture_error = detail


def _validate_request(params: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("gnss_cycle request must be an object")
    if set(params) - {"cycle", "expected_session_id", "after_sample_id"}:
        raise ValueError("gnss_cycle request contains unsupported fields")
    cycle = params.get("cycle")
    cycle_fields = {"generation", "sequence", "intent", "state_revision"}
    if not isinstance(cycle, dict) or set(cycle) != cycle_fields:
        raise ValueError("gnss_cycle cycle must be a complete ControlCycle object")
    for field_name in ("generation", "sequence", "state_revision"):
        _integer(cycle.get(field_name), f"cycle.{field_name}", minimum=0)
    if cycle["generation"] == 0 or cycle["sequence"] == 0:
        raise ValueError("gnss_cycle cycle generation and sequence must be positive")
    if cycle["intent"] != "sample" or cycle["state_revision"] != 0:
        raise ValueError("gnss_cycle requires sample intent with state_revision zero")
    expected_session_id = params.get("expected_session_id")
    if expected_session_id is not None and (
        not isinstance(expected_session_id, str) or not expected_session_id.strip()
    ):
        raise ValueError("expected_session_id must be non-empty text when provided")
    after_sample_id = params.get("after_sample_id", 0)
    after_sample_id = _integer(after_sample_id, "after_sample_id", minimum=0)
    if expected_session_id is None and after_sample_id != 0:
        raise ValueError("after_sample_id must be zero when expected_session_id is omitted")
    return {
        "cycle": dict(cycle),
        "expected_session_id": expected_session_id,
        "after_sample_id": after_sample_id,
    }


def _integer(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer greater than or equal to {minimum}")
    return value


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    return float(value)
