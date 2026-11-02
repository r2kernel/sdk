from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common import (
    WORLD_TOPIC,
    angle_wrap_degrees,
    angle_wrap_radians,
    as_bool,
    as_float,
    clamp,
    robot_cleaner_root,
    trash_layout,
)
from radar_mount import RadarMount, load_radar_mount
from gnss_source import GnssReceiverSimulator


class ArenaSimulator:
    """Provided 2D cleaner arena used by the tutorial device gateway."""

    RADAR_SWEEP_DEG_PER_S = 120.0
    RADAR_SAMPLE_PERIOD_NS = 100_000_000
    RADAR_MAX_ACQUISITION_GAP_PERIODS = 5
    RADAR_HISTORY_CAPACITY = 64
    RADAR_MAX_RANGE_M = 2.0
    RADAR_TRACK_TTL_NS = 3_500_000_000

    def __init__(
        self,
        *,
        now_ns: Callable[[], int],
        radar_sample_period_ms: float = RADAR_SAMPLE_PERIOD_NS / 1_000_000,
        radar_history_capacity: int = RADAR_HISTORY_CAPACITY,
        gnss_fixture_path: Path | None = None,
    ) -> None:
        sample_period_ns = int(float(radar_sample_period_ms) * 1_000_000)
        history_capacity = int(radar_history_capacity)
        revolution_period_ns = int(360.0 / self.RADAR_SWEEP_DEG_PER_S * 1_000_000_000)
        if sample_period_ns <= 0 or sample_period_ns > revolution_period_ns:
            raise ValueError("radar_sample_period_ms must be positive and no longer than one revolution")
        if history_capacity <= 0:
            raise ValueError("radar_history_capacity must be positive")
        self._now_ns = now_ns
        self._radar_mount: RadarMount = load_radar_mount(
            robot_cleaner_root(__file__) / "subsystems" / "compound.cleaner" / "subsystem.yaml"
        )
        self._gnss_source = GnssReceiverSimulator(fixture_path=gnss_fixture_path)
        self._radar_sample_period_ns = sample_period_ns
        self._radar_history_capacity = history_capacity
        self._lock = threading.RLock()
        self._seq = 0
        self._robot: dict[str, Any] = {}
        self._trash: list[dict[str, Any]] = []
        self._last_scan: dict[str, Any] = self._inactive_scan()
        self._radar_sector_history: deque[dict[str, Any]] = deque(maxlen=history_capacity)
        self._radar_session_id = ""
        self._radar_sample_id = 0
        self._radar_capture_error: str | None = None
        self._last_motion_ns = self._now_ns()
        self._drive_until_ns = self._last_motion_ns
        self._reset_world(seed=7, count=9)

    def reset(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        seed = int(as_float(params, "seed", 7))
        count = int(as_float(params, "count", 9))
        scenario = str(params.get("scenario") or "default")
        with self._lock:
            self._reset_world(seed=seed, count=count)
            if scenario == "far_target":
                self._trash = [{"id": "trash-far-1", "x": 0.82, "y": 0.78, "state": "loose"}]
            return self._snapshot_locked(reason="reset")

    def spawn_trash(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        with self._lock:
            index = len(self._trash) + 1
            x = clamp(as_float(params, "x", 0.15 + ((index * 0.137) % 0.70)), 0.08, 0.92)
            y = clamp(as_float(params, "y", 0.18 + ((index * 0.211) % 0.64)), 0.08, 0.92)
            item = {"id": f"trash-{index}", "x": round(x, 3), "y": round(y, 3), "state": "loose"}
            self._trash.append(item)
            snapshot = self._snapshot_locked(reason="spawn_trash")
        return {"spawned": item, "state": snapshot}

    def get_state(self, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(reason="get_state")

    def apply_drive(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        vx = clamp(as_float(params, "vx", as_float(params, "linear", 0.0)), -1.0, 1.0)
        vy = clamp(as_float(params, "vy", as_float(params, "lateral", 0.0)), -1.0, 1.0)
        wz = clamp(as_float(params, "wz", as_float(params, "angular", 0.0)), -1.0, 1.0)
        frame_id = str(params.get("frame_id") or params.get("command_frame_id") or "base_link")
        dt = clamp(as_float(params, "dt", 0.16), 0.02, 0.50)
        immediate_stop = as_bool(params, "immediate_stop", False)
        zero_command = abs(vx) < 1e-9 and abs(vy) < 1e-9 and abs(wz) < 1e-9
        with self._lock:
            now_ns = self._now_ns()
            self._advance_motion_locked(now_ns=now_ns)
            self._robot["target_vx"] = vx
            self._robot["target_vy"] = vy
            self._robot["target_wz"] = wz
            self._robot["velocity_frame"] = frame_id
            if immediate_stop or (zero_command and dt <= 0.05):
                self._robot["vx"] = 0.0
                self._robot["vy"] = 0.0
                self._robot["wz"] = 0.0
                self._robot["linear"] = 0.0
                self._robot["angular"] = 0.0
            self._last_motion_ns = now_ns
            self._drive_until_ns = now_ns if immediate_stop else now_ns + int(dt * 1_000_000_000)
            self._sync_held_trash_locked()
            return self._snapshot_locked(reason="apply_drive")

    def set_arm(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        extension = clamp(as_float(params, "extension", self._robot.get("arm_extension", 0.0)), 0.0, 1.0)
        with self._lock:
            self._advance_motion_locked()
            previous = float(self._robot.get("arm_extension", 0.0))
            if extension > previous and not bool(self._robot.get("gripper_open", True)) and self._robot.get("holding") is None:
                self._push_trash_locked(previous, extension)
            self._robot["arm_extension"] = extension
            self._sync_held_trash_locked()
            return self._snapshot_locked(reason="set_arm")

    def set_gripper(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        open_gripper = as_bool(params, "open", True)
        result: dict[str, Any] = {"grabbed": None, "released": None}
        with self._lock:
            self._advance_motion_locked()
            if open_gripper:
                result["released"] = self._release_held_locked()
                self._robot["gripper_open"] = True
            else:
                self._robot["gripper_open"] = False
                if self._robot.get("holding") is None:
                    result["grabbed"] = self._grab_nearest_locked()
            self._sync_held_trash_locked()
            snapshot = self._snapshot_locked(reason="set_gripper")
        return {**result, "state": snapshot}

    def incinerate(self, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._advance_motion_locked()
            self._deposit_held_if_retracted_locked()
            burned: list[str] = []
            for item in self._trash:
                if item.get("state") == "payload":
                    item["state"] = "burned"
                    burned.append(str(item["id"]))
            if burned:
                self._robot["incinerated"] += len(burned)
            snapshot = self._snapshot_locked(reason="incinerate")
        return {"ok": bool(burned), "burned": burned, "state": snapshot}

    def scan(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        mode = str(params.get("mode") or "nearest")
        max_range = clamp(as_float(params, "max_range", 0.35), 0.05, 2.0)
        fov_deg = clamp(as_float(params, "fov_deg", 360.0), 1.0, 360.0)
        visualize = bool(params.get("visualize", True))
        with self._lock:
            self._advance_motion_locked()
            bearing_center_deg = as_float(params, "bearing_center_deg", 0.0)
            beam_detections: list[dict[str, Any]] = []
            if mode == "radar":
                radar_state = self._radar_state_locked(max_range=max_range, fov_deg=fov_deg)
                bearing_center_deg = float(radar_state["bearing_center_deg"])
                beam_detections = radar_state["beam_detections"]
                detections = radar_state["detections"]
            else:
                detections = self._detections_locked(
                    max_range=max_range,
                    fov_deg=fov_deg,
                    bearing_center_deg=bearing_center_deg,
                )
            if mode == "front":
                detections = [item for item in detections if abs(float(item["bearing_deg"])) <= fov_deg / 2.0]
            if mode == "nearest" and detections:
                detections = [detections[0]]
            if visualize:
                self._last_scan = radar_state if mode == "radar" else {
                    "mode": mode,
                    "max_range": max_range,
                    "fov_deg": fov_deg,
                    "bearing_center_deg": bearing_center_deg,
                    "detections": detections,
                    "beam_detections": beam_detections,
                }
            snapshot = self._snapshot_locked(reason="scan")
        return {"mode": mode, "detections": detections, "state": snapshot}

    def tick(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(reason="tick")

    @property
    def gnss_sample_period_ns(self) -> int:
        return self._gnss_source.sample_period_ns

    def capture_gnss_sample(self, *, now_ns: int | None = None) -> bool:
        """Prepare one finite-rate latest-only antenna fix independently of Control reads."""
        provided_time_ns = None if now_ns is None else self._require_integer(now_ns, "now_ns", minimum=0)
        with self._lock:
            current_time_ns = self._now_ns() if provided_time_ns is None else provided_time_ns
            if self._last_motion_ns > current_time_ns:
                self._gnss_source.record_capture_error(
                    "GNSS acquisition time precedes the simulator world state"
                )
                raise RuntimeError("GNSS acquisition time precedes the simulator world state")

            self._advance_motion_locked(now_ns=current_time_ns)
            return self._gnss_source.capture(
                body_x_m=float(self._robot["x"]),
                body_y_m=float(self._robot["y"]),
                body_yaw_rad=float(self._robot["heading"]),
                now_ns=current_time_ns,
            )

    def read_gnss_cycle(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Return only the latest prepared receiver sample newer than its cursor."""
        return self._gnss_source.read_cycle(params, now_ns=int(self._now_ns()))

    @property
    def radar_sample_period_ns(self) -> int:
        return self._radar_sample_period_ns

    def capture_radar_sectors(self, *, now_ns: int | None = None) -> int:
        """Capture a due finite-rate sector, independent of Control reads."""
        provided_time_ns = None if now_ns is None else self._require_integer(now_ns, "now_ns", minimum=0)
        with self._lock:
            current_time_ns = self._now_ns() if provided_time_ns is None else provided_time_ns
            if self._radar_capture_error is not None:
                raise RuntimeError(f"radar capture is faulted: {self._radar_capture_error}")
            elapsed_ns = current_time_ns - self._radar_last_sector_end_ns
            if elapsed_ns < 0:
                self._radar_capture_error = "radar clock moved backwards during acquisition"
                raise RuntimeError(self._radar_capture_error)
            if elapsed_ns < self._radar_sample_period_ns:
                return 0
            scan_started_at_sync_ns = self._radar_last_sector_end_ns
            revolution_period_ns = int(360.0 / self.RADAR_SWEEP_DEG_PER_S * 1_000_000_000)
            # Bridge several missed polling deadlines as one wider, correctly
            # timed sector; only a longer discontinuity starts a new session.
            max_acquisition_gap_ns = min(
                self._radar_sample_period_ns * self.RADAR_MAX_ACQUISITION_GAP_PERIODS,
                revolution_period_ns,
            )
            if elapsed_ns > max_acquisition_gap_ns:
                self._reset_radar_acquisition_locked(now_ns=current_time_ns)
                return 0
            if self._last_motion_ns > current_time_ns:
                self._radar_capture_error = "radar acquisition time precedes the simulator world state"
                raise RuntimeError(self._radar_capture_error)

            self._advance_motion_locked(now_ns=current_time_ns)
            sector = self._build_radar_sector_locked(
                scan_started_at_sync_ns=scan_started_at_sync_ns,
                scan_ended_at_sync_ns=current_time_ns,
            )
            self._radar_sector_history.append(sector)
            self._radar_sample_id += 1
            self._radar_last_sector_end_ns = current_time_ns
            # This ID-bearing view is simulator-only UI/debug state. The
            # robot-facing radar_cycle contract is built independently above.
            self._last_scan = self._radar_state_locked(
                max_range=self.RADAR_MAX_RANGE_M,
                fov_deg=max(math.degrees(float(sector["azimuth_span_rad"])), 0.1),
            )
            return 1

    def read_radar_cycle(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Return the next retained prepared sector after a session-scoped cursor."""
        if not isinstance(params, dict):
            raise ValueError("radar_cycle request must be an object")
        if set(params) - {"cycle", "expected_session_id", "after_sample_id"}:
            raise ValueError("radar_cycle request contains unsupported fields")
        cycle = params.get("cycle")
        if not isinstance(cycle, dict) or set(cycle) != {"generation", "sequence", "intent", "state_revision"}:
            raise ValueError("radar_cycle cycle must be a complete ControlCycle object")
        for field_name in ("generation", "sequence", "state_revision"):
            self._require_integer(cycle.get(field_name), f"cycle.{field_name}", minimum=0)
        if cycle["generation"] <= 0 or cycle["sequence"] <= 0:
            raise ValueError("radar_cycle cycle generation and sequence must be positive")
        if cycle.get("intent") != "sample" or cycle["state_revision"] != 0:
            raise ValueError("radar_cycle requires sample intent with state_revision zero")
        after_sample_id = params.get("after_sample_id")
        if after_sample_id is not None:
            after_sample_id = self._require_integer(after_sample_id, "after_sample_id", minimum=0)
        expected_session_id = params.get("expected_session_id")
        if expected_session_id is not None and (not isinstance(expected_session_id, str) or not expected_session_id):
            raise ValueError("expected_session_id must be non-empty text when provided")

        with self._lock:
            response: dict[str, Any] = {
                "cycle": dict(cycle),
                "session_id": self._radar_session_id,
                "status": "no_new_sample",
                "simulator_now_ns": int(self._now_ns()),
            }
            if expected_session_id is not None and expected_session_id != self._radar_session_id:
                response.update(status="resync_required", detail="radar session changed; reset the sample cursor")
                return response
            if self._radar_capture_error is not None:
                response.update(status="device_error", detail=self._radar_capture_error)
                return response
            if expected_session_id is None and after_sample_id not in (None, 0):
                response.update(
                    status="resync_required",
                    detail="expected_session_id is required when continuing a nonzero radar cursor",
                )
                return response

            if not self._radar_sector_history:
                if after_sample_id not in (None, 0):
                    response.update(status="resync_required", detail="radar cursor is ahead of the current session")
                return response

            oldest_sample_id = int(self._radar_sector_history[0]["sample_id"])
            newest_sample_id = int(self._radar_sector_history[-1]["sample_id"])
            if expected_session_id is None:
                response.update(status="sample_available", sector=dict(self._radar_sector_history[-1]))
                return response
            cursor = 0 if after_sample_id is None else after_sample_id
            if cursor > newest_sample_id:
                response.update(status="resync_required", detail="radar cursor is ahead of the current session")
                return response
            if cursor < oldest_sample_id - 1:
                response.update(status="resync_required", detail="radar cursor fell behind the retained sector history")
                return response

            for sector in self._radar_sector_history:
                if int(sector["sample_id"]) > cursor:
                    response.update(status="sample_available", sector=dict(sector))
                    return response
            return response

    def record_radar_capture_error(self, error: BaseException | str) -> None:
        message = str(error).strip()
        if not message:
            message = "radar capture failed without a diagnostic"
        with self._lock:
            self._radar_capture_error = message

    def record_gnss_capture_error(self, error: BaseException | str) -> None:
        self._gnss_source.record_capture_error(error)

    def start_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(reason="start")

    def _reset_world(self, *, seed: int, count: int) -> None:
        now_ns = self._now_ns()
        self._robot = {
            "x": 0.18,
            "y": 0.50,
            "heading": 0.0,
            "arm_extension": 0.0,
            "gripper_open": True,
            "holding": None,
            "incinerated": 0,
            "linear": 0.0,
            "angular": 0.0,
            "vx": 0.0,
            "vy": 0.0,
            "wz": 0.0,
            "target_vx": 0.0,
            "target_vy": 0.0,
            "target_wz": 0.0,
            "velocity_frame": "base_link",
        }
        self._trash = trash_layout(seed=seed, count=count)
        self._last_scan = self._inactive_scan()
        self._last_motion_ns = now_ns
        self._drive_until_ns = now_ns
        self._radar_epoch_ns = now_ns
        self._radar_tracks: dict[str, int] = {}
        self._radar_session_id = uuid.uuid4().hex
        self._radar_sample_id = 0
        self._radar_last_sector_end_ns = now_ns
        self._radar_capture_error = None
        self._radar_sector_history.clear()
        self._gnss_source.reset(now_ns=now_ns)

    def _reset_radar_acquisition_locked(self, *, now_ns: int) -> None:
        """Start a new session after an acquisition gap that cannot be reconstructed."""
        self._radar_session_id = uuid.uuid4().hex
        self._radar_sample_id = 0
        self._radar_epoch_ns = now_ns
        self._radar_last_sector_end_ns = now_ns
        self._radar_sector_history.clear()
        self._last_scan = self._inactive_scan()

    def _build_radar_sector_locked(
        self,
        *,
        scan_started_at_sync_ns: int,
        scan_ended_at_sync_ns: int,
    ) -> dict[str, Any]:
        rotation_rad_per_second = math.radians(self.RADAR_SWEEP_DEG_PER_S)
        elapsed_start_s = (scan_started_at_sync_ns - self._radar_epoch_ns) / 1_000_000_000.0
        elapsed_end_s = (scan_ended_at_sync_ns - self._radar_epoch_ns) / 1_000_000_000.0
        absolute_start_rad = max(0.0, elapsed_start_s * rotation_rad_per_second)
        absolute_end_rad = max(absolute_start_rad, elapsed_end_s * rotation_rad_per_second)
        azimuth_start_rad = absolute_start_rad % math.tau
        azimuth_span_rad = absolute_end_rad - absolute_start_rad
        revolution_before = int(math.floor(absolute_start_rad / math.tau))
        revolution_after = int(math.floor(absolute_end_rad / math.tau))
        revolution_complete = revolution_after > revolution_before
        revolution_id = revolution_before + 1

        returns: list[dict[str, Any]] = []
        sensor_x, sensor_y, sensor_heading = self._radar_pose_locked()
        velocity_x, velocity_y = self._radar_world_velocity_locked()
        for item in self._trash:
            if item.get("state") != "loose":
                continue
            delta_x = float(item["x"]) - sensor_x
            delta_y = float(item["y"]) - sensor_y
            range_m = math.hypot(delta_x, delta_y)
            if range_m <= 0.0 or range_m > self.RADAR_MAX_RANGE_M:
                continue
            azimuth_rad = (math.atan2(delta_y, delta_x) - sensor_heading) % math.tau
            angular_offset_rad = (azimuth_rad - azimuth_start_rad) % math.tau
            if angular_offset_rad > azimuth_span_rad + 1e-12:
                continue
            observed_at_sync_ns = scan_ended_at_sync_ns
            unit_x = delta_x / range_m
            unit_y = delta_y / range_m
            radial_velocity_mps = -(velocity_x * unit_x + velocity_y * unit_y)
            signal_strength = 1.0 / (1.0 + (range_m / 0.1) ** 2)
            returns.append(
                {
                    "range_m": round(range_m, 6),
                    "azimuth_rad": round(azimuth_rad, 9),
                    "radial_velocity_mps": round(radial_velocity_mps, 6),
                    "signal_strength": round(signal_strength, 6),
                    "observed_at_sync_ns": observed_at_sync_ns,
                }
            )
        returns.sort(key=lambda item: (float(item["azimuth_rad"]) - azimuth_start_rad) % math.tau)
        rounded_start_rad = round(azimuth_start_rad, 9)
        rounded_span_rad = round(azimuth_span_rad, 9)
        self._normalize_return_azimuths(
            returns,
            azimuth_start_rad=rounded_start_rad,
            azimuth_span_rad=rounded_span_rad,
        )
        return {
            "session_id": self._radar_session_id,
            "sample_id": self._radar_sample_id + 1,
            "revolution_id": revolution_id,
            "azimuth_start_rad": rounded_start_rad,
            "azimuth_span_rad": rounded_span_rad,
            "scan_started_at_sync_ns": scan_started_at_sync_ns,
            "scan_ended_at_sync_ns": scan_ended_at_sync_ns,
            "frame_id": "cleaner_radar",
            "revolution_complete": revolution_complete,
            "returns": returns,
        }

    def _radar_pose_locked(self) -> tuple[float, float, float]:
        """Return the radar origin and heading in the world frame."""
        body_heading = float(self._robot["heading"])
        cosine = math.cos(body_heading)
        sine = math.sin(body_heading)
        sensor_x = float(self._robot["x"]) + cosine * self._radar_mount.translation_x_m - sine * self._radar_mount.translation_y_m
        sensor_y = float(self._robot["y"]) + sine * self._radar_mount.translation_x_m + cosine * self._radar_mount.translation_y_m
        sensor_heading = angle_wrap_radians(body_heading + self._radar_mount.yaw_rad)
        return sensor_x, sensor_y, sensor_heading

    def _radar_pose_payload_locked(self) -> dict[str, float]:
        sensor_x, sensor_y, sensor_heading = self._radar_pose_locked()
        return {
            "x": round(sensor_x, 9),
            "y": round(sensor_y, 9),
            "heading": round(sensor_heading, 9),
        }

    @staticmethod
    def _normalize_return_azimuths(
        returns: list[dict[str, Any]],
        *,
        azimuth_start_rad: float,
        azimuth_span_rad: float,
    ) -> None:
        """Keep return azimuths inside the sector interval within the shared 1e-6 rad tolerance."""
        tolerance_rad = 1e-6
        sector_end_rad = (azimuth_start_rad + azimuth_span_rad) % math.tau
        for return_value in returns:
            azimuth_rad = float(return_value["azimuth_rad"])
            offset_rad = (azimuth_rad - azimuth_start_rad) % math.tau
            if offset_rad <= azimuth_span_rad or azimuth_span_rad >= math.tau - tolerance_rad:
                continue
            if offset_rad > math.tau - tolerance_rad:
                return_value["azimuth_rad"] = round(azimuth_start_rad, 7)
            elif offset_rad - azimuth_span_rad <= tolerance_rad:
                return_value["azimuth_rad"] = round(sector_end_rad, 7)
            else:
                raise RuntimeError("synthesized radar return fell outside its acquired sector")

    def _robot_world_velocity_locked(self) -> tuple[float, float]:
        velocity_x = float(self._robot.get("vx", 0.0)) * 0.36
        velocity_y = float(self._robot.get("vy", 0.0)) * 0.36
        if str(self._robot.get("velocity_frame") or "base_link") == "base_link":
            heading = float(self._robot["heading"])
            return (
                math.cos(heading) * velocity_x - math.sin(heading) * velocity_y,
                math.sin(heading) * velocity_x + math.cos(heading) * velocity_y,
            )
        return velocity_x, velocity_y

    def _radar_world_velocity_locked(self) -> tuple[float, float]:
        """Return radar-origin velocity, including rotation around the body origin."""
        body_velocity_x, body_velocity_y = self._robot_world_velocity_locked()
        body_heading = float(self._robot["heading"])
        offset_x = math.cos(body_heading) * self._radar_mount.translation_x_m - math.sin(body_heading) * self._radar_mount.translation_y_m
        offset_y = math.sin(body_heading) * self._radar_mount.translation_x_m + math.cos(body_heading) * self._radar_mount.translation_y_m
        angular_velocity_rad_per_second = float(self._robot.get("wz", 0.0)) * math.radians(150.0)
        return (
            body_velocity_x - angular_velocity_rad_per_second * offset_y,
            body_velocity_y + angular_velocity_rad_per_second * offset_x,
        )

    @staticmethod
    def _require_integer(value: Any, field_name: str, *, minimum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{field_name} must be an integer greater than or equal to {minimum}")
        return value

    @staticmethod
    def _inactive_scan() -> dict[str, Any]:
        return {
            "mode": "none",
            "max_range": 0.0,
            "fov_deg": 0.0,
            "bearing_center_deg": 0.0,
            "detections": [],
            "beam_detections": [],
        }

    def _snapshot_locked(self, *, reason: str) -> dict[str, Any]:
        self._advance_motion_locked()
        if self._last_scan.get("mode") == "radar":
            self._last_scan = self._radar_state_locked(
                max_range=float(self._last_scan.get("max_range") or 0.30),
                fov_deg=float(self._last_scan.get("fov_deg") or 30.0),
            )
        self._seq += 1
        self._sync_attached_trash_locked()
        active_trash = [dict(item) for item in self._trash if item.get("state") != "burned"]
        burned_count = len([item for item in self._trash if item.get("state") == "burned"])
        payload_count = len([item for item in active_trash if item.get("state") == "payload"])
        reach = self._reach_point_locked()
        scan_snapshot = dict(self._last_scan)
        if scan_snapshot.get("mode") in {"radar", "none"}:
            scan_snapshot["sensor_pose"] = self._radar_pose_payload_locked()
        return {
            "kind": "robot_cleaner.world",
            "topic": WORLD_TOPIC,
            "seq": self._seq,
            "reason": reason,
            "ts": self._now_ns(),
            "world": {"width": 1.0, "height": 1.0},
            "robot": {
                **self._robot,
                "heading_deg": math.degrees(float(self._robot["heading"])),
                "reach_x": reach["x"],
                "reach_y": reach["y"],
            },
            "trash": active_trash,
            "score": {
                "remaining": len([item for item in active_trash if item.get("state") == "loose"]),
                "payload": payload_count,
                "burned": burned_count,
                "incinerated": self._robot["incinerated"],
            },
            "scan": scan_snapshot,
        }

    def _radar_state_locked(self, *, max_range: float, fov_deg: float) -> dict[str, Any]:
        now_ns = self._now_ns()
        elapsed_s = max(0.0, (now_ns - self._radar_epoch_ns) / 1_000_000_000.0)
        bearing_center_deg = angle_wrap_degrees(elapsed_s * self.RADAR_SWEEP_DEG_PER_S)
        candidates = self._radar_detections_locked(max_range=max_range, fov_deg=360.0)
        beam_detections = self._radar_detections_locked(
            max_range=max_range,
            fov_deg=fov_deg,
            bearing_center_deg=bearing_center_deg,
        )
        for detection in beam_detections:
            self._radar_tracks[str(detection["id"])] = now_ns
        candidate_by_id = {str(item["id"]): item for item in candidates}
        self._radar_tracks = {
            target_id: seen_ns
            for target_id, seen_ns in self._radar_tracks.items()
            if target_id in candidate_by_id and now_ns - seen_ns <= self.RADAR_TRACK_TTL_NS
        }
        fresh_ids = {str(item["id"]) for item in beam_detections}
        detections = [
            {
                **candidate_by_id[target_id],
                "fresh": target_id in fresh_ids,
                "track_age_ms": round((now_ns - seen_ns) / 1_000_000.0, 1),
            }
            for target_id, seen_ns in self._radar_tracks.items()
        ]
        return {
            "mode": "radar",
            "max_range": max_range,
            "fov_deg": fov_deg,
            "bearing_center_deg": bearing_center_deg,
            "sensor_pose": self._radar_pose_payload_locked(),
            "sweep_deg_per_s": self.RADAR_SWEEP_DEG_PER_S,
            "sampled_at_ns": now_ns,
            "detections": detections,
            "beam_detections": beam_detections,
        }

    def _radar_detections_locked(
        self,
        *,
        max_range: float,
        fov_deg: float,
        bearing_center_deg: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Build simulator-only radar detections in the mounted sensor frame."""
        sensor_x, sensor_y, sensor_heading = self._radar_pose_locked()
        detections: list[dict[str, Any]] = []
        for item in self._trash:
            if item.get("state") != "loose":
                continue
            delta_x = float(item["x"]) - sensor_x
            delta_y = float(item["y"]) - sensor_y
            distance = math.hypot(delta_x, delta_y)
            if distance > max_range:
                continue
            bearing_deg = angle_wrap_degrees(math.degrees(math.atan2(delta_y, delta_x) - sensor_heading))
            beam_bearing_deg = angle_wrap_degrees(bearing_deg - bearing_center_deg)
            if fov_deg < 360.0 and abs(beam_bearing_deg) > fov_deg / 2.0:
                continue
            detections.append(
                {
                    "id": item["id"],
                    "relative_x": round(math.cos(math.radians(bearing_deg)) * distance, 4),
                    "relative_y": round(math.sin(math.radians(bearing_deg)) * distance, 4),
                    "distance": round(distance, 4),
                    "bearing_deg": round(bearing_deg, 2),
                }
            )
        detections.sort(key=lambda item: (float(item["distance"]), abs(float(item["bearing_deg"]))))
        return detections

    def _advance_motion_locked(self, *, now_ns: int | None = None) -> None:
        if now_ns is None:
            now_ns = self._now_ns()
        last_ns = self._last_motion_ns
        if now_ns <= last_ns:
            return

        if now_ns >= self._drive_until_ns:
            self._robot["target_vx"] = 0.0
            self._robot["target_vy"] = 0.0
            self._robot["target_wz"] = 0.0

        elapsed_s = (now_ns - last_ns) / 1_000_000_000.0
        self._integrate_drive_locked(elapsed_s)

        self._last_motion_ns = now_ns
        self._sync_attached_trash_locked()

    def _integrate_drive_locked(self, elapsed_s: float) -> None:
        if elapsed_s <= 0.0:
            return

        speed = 0.36
        turn_rate = math.radians(150.0)
        accel = 8.0
        angular_accel = 10.0
        step_s = 1.0 / 60.0
        remaining_s = min(elapsed_s, 0.75)
        while remaining_s > 1e-9:
            dt = min(step_s, remaining_s)
            self._robot["vx"] = self._approach(float(self._robot.get("vx", 0.0)), float(self._robot.get("target_vx", 0.0)), accel * dt)
            self._robot["vy"] = self._approach(float(self._robot.get("vy", 0.0)), float(self._robot.get("target_vy", 0.0)), accel * dt)
            self._robot["wz"] = self._approach(float(self._robot.get("wz", 0.0)), float(self._robot.get("target_wz", 0.0)), angular_accel * dt)
            vx = float(self._robot["vx"])
            vy = float(self._robot["vy"])
            wz = float(self._robot["wz"])
            if abs(vx) > 1e-6 or abs(vy) > 1e-6 or abs(wz) > 1e-6:
                heading = float(self._robot["heading"])
                if str(self._robot.get("velocity_frame") or "base_link") == "base_link":
                    world_vx = math.cos(heading) * vx - math.sin(heading) * vy
                    world_vy = math.sin(heading) * vx + math.cos(heading) * vy
                else:
                    world_vx = vx
                    world_vy = vy
                self._robot["heading"] = angle_wrap_radians(heading + wz * turn_rate * dt)
                self._robot["x"] = clamp(float(self._robot["x"]) + world_vx * speed * dt, 0.05, 0.95)
                self._robot["y"] = clamp(float(self._robot["y"]) + world_vy * speed * dt, 0.05, 0.95)
            self._robot["linear"] = vx
            self._robot["angular"] = wz
            remaining_s -= dt

    @staticmethod
    def _approach(current: float, target: float, max_delta: float) -> float:
        if current < target:
            return min(current + max_delta, target)
        if current > target:
            return max(current - max_delta, target)
        return current

    def _reach_point_locked(self) -> dict[str, float]:
        return self._reach_point_for_extension_locked(float(self._robot["arm_extension"]))

    def _reach_point_for_extension_locked(self, extension: float) -> dict[str, float]:
        reach = 0.08 + 0.18 * clamp(extension, 0.0, 1.0)
        return {
            "x": clamp(self._robot["x"] + math.cos(self._robot["heading"]) * reach, 0.0, 1.0),
            "y": clamp(self._robot["y"] + math.sin(self._robot["heading"]) * reach, 0.0, 1.0),
        }

    def _sync_attached_trash_locked(self) -> None:
        self._deposit_held_if_retracted_locked()
        self._sync_payload_trash_locked()
        self._sync_held_trash_locked()

    def _sync_held_trash_locked(self) -> None:
        held = self._robot.get("holding")
        if held is None:
            return
        reach = self._reach_point_locked()
        for item in self._trash:
            if item["id"] == held:
                item["x"] = round(reach["x"], 3)
                item["y"] = round(reach["y"], 3)
                item["state"] = "held"
                return
        self._robot["holding"] = None

    def _sync_payload_trash_locked(self) -> None:
        payload_items = [item for item in self._trash if item.get("state") == "payload"]
        if not payload_items:
            return
        heading = float(self._robot["heading"])
        body_x = float(self._robot["x"])
        body_y = float(self._robot["y"])
        forward = (math.cos(heading), math.sin(heading))
        lateral = (-math.sin(heading), math.cos(heading))
        for index, item in enumerate(payload_items):
            row = index // 3
            col = index % 3
            lateral_offset = (col - 1) * 0.018
            forward_offset = -0.018 - row * 0.016
            item["x"] = round(clamp(body_x + forward[0] * forward_offset + lateral[0] * lateral_offset, 0.0, 1.0), 3)
            item["y"] = round(clamp(body_y + forward[1] * forward_offset + lateral[1] * lateral_offset, 0.0, 1.0), 3)

    def _deposit_held_if_retracted_locked(self) -> None:
        held = self._robot.get("holding")
        if held is None or float(self._robot.get("arm_extension", 0.0)) > 0.06:
            return
        for item in self._trash:
            if item["id"] == held:
                item["state"] = "payload"
                break
        self._robot["holding"] = None
        self._sync_payload_trash_locked()

    def _release_held_locked(self) -> str | None:
        held = self._robot.get("holding")
        if held is None:
            return None
        for item in self._trash:
            if item["id"] == held:
                item["state"] = "loose"
                break
        self._robot["holding"] = None
        return str(held)

    def _grab_nearest_locked(self) -> str | None:
        reach = self._reach_point_locked()
        best: tuple[float, dict[str, Any]] | None = None
        for item in self._trash:
            if item.get("state") != "loose":
                continue
            dist = math.hypot(float(item["x"]) - reach["x"], float(item["y"]) - reach["y"])
            if dist <= 0.04 and (best is None or dist < best[0]):
                best = (dist, item)
        if best is None:
            return None
        item = best[1]
        item["state"] = "held"
        self._robot["holding"] = item["id"]
        self._sync_held_trash_locked()
        return str(item["id"])

    def _push_trash_locked(self, previous_extension: float, next_extension: float) -> None:
        start = self._reach_point_for_extension_locked(previous_extension)
        end = self._reach_point_for_extension_locked(next_extension)
        dx = end["x"] - start["x"]
        dy = end["y"] - start["y"]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-9:
            return
        length = math.sqrt(length_sq)
        ux = dx / length
        uy = dy / length
        sweep_radius = 0.055
        push_distance = 0.045
        for item in self._trash:
            if item.get("state") != "loose":
                continue
            px = float(item["x"])
            py = float(item["y"])
            projection = ((px - start["x"]) * dx + (py - start["y"]) * dy) / length_sq
            if projection < -0.05 or projection > 1.10:
                continue
            nearest_x = start["x"] + clamp(projection, 0.0, 1.0) * dx
            nearest_y = start["y"] + clamp(projection, 0.0, 1.0) * dy
            if math.hypot(px - nearest_x, py - nearest_y) > sweep_radius:
                continue
            item["x"] = round(clamp(end["x"] + ux * push_distance, 0.03, 0.97), 3)
            item["y"] = round(clamp(end["y"] + uy * push_distance, 0.03, 0.97), 3)

    def _detections_locked(
        self,
        *,
        max_range: float,
        fov_deg: float,
        bearing_center_deg: float = 0.0,
    ) -> list[dict[str, Any]]:
        detections: list[dict[str, Any]] = []
        heading_deg = math.degrees(float(self._robot["heading"]))
        for item in self._trash:
            if item.get("state") != "loose":
                continue
            dx = float(item["x"]) - float(self._robot["x"])
            dy = float(item["y"]) - float(self._robot["y"])
            distance = math.hypot(dx, dy)
            if distance > max_range:
                continue
            bearing = angle_wrap_degrees(math.degrees(math.atan2(dy, dx)) - heading_deg)
            beam_bearing = angle_wrap_degrees(bearing - bearing_center_deg)
            if fov_deg < 360.0 and abs(beam_bearing) > fov_deg / 2.0:
                continue
            detections.append(
                {
                    "id": item["id"],
                    "relative_x": round(math.cos(math.radians(bearing)) * distance, 4),
                    "relative_y": round(math.sin(math.radians(bearing)) * distance, 4),
                    "distance": round(distance, 4),
                    "bearing_deg": round(bearing, 2),
                }
            )
        detections.sort(key=lambda item: (float(item["distance"]), abs(float(item["bearing_deg"]))))
        return detections
