from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

_ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import WORLD_EVENT_SIZE, clamp, extract_payload, response_ok  # noqa: E402
from localization_math import planar_pose  # noqa: E402
from navigation_math import navigation_command, resolve_target_pose  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import (  # noqa: E402
    BOOL,
    COMMAND_ACK,
    EMPTY,
    FOLLOW_PATH_REQUEST,
    NAVIGATION_FEEDBACK,
    NAVIGATION_RESULT,
    NAVIGATION_STATE,
    NAVIGATE_TO_POSE_REQUEST,
    POSE_ESTIMATE,
    STOP_REQUEST,
    VELOCITY_COMMAND,
    twist,
)

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class NavigationSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.navigation",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        position_tolerance: float = 0.018,
        yaw_tolerance_deg: float = 4.0,
        max_linear: float = 0.70,
        max_angular: float = 1.0,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "navigation"),
            manifest_path=manifest_path or default_manifest_path(__file__, "navigation"),
            deployment_manifest_path=deployment_manifest_path,
        )
        self._position_tolerance = max(float(position_tolerance), 0.005)
        self._yaw_tolerance = math.radians(max(float(yaw_tolerance_deg), 0.5))
        self._max_linear = clamp(float(max_linear), 0.05, 1.0)
        self._max_angular = clamp(float(max_angular), 0.05, 2.0)
        self._pose_lock = threading.RLock()
        self._current_pose: tuple[float, float, float] | None = None
        self._pose_timestamp_ns = 0
        self._goal_lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._active_operation_id = ""
        self._last_control_tick_ns: int | None = None

    def on_initialize(self) -> bool:
        self.register_event_subscribe(
            topic="pose_estimate",
            type=POSE_ESTIMATE,
            size=WORLD_EVENT_SIZE,
            callback=self._on_pose_estimate,
        )
        self.register_event_publish(
            topic="navigation_state",
            type=NAVIGATION_STATE,
            size=WORLD_EVENT_SIZE,
            priority=5,
            queue_depth=4,
        )
        self.register_procedure("cancel_navigation", self._cancel_navigation, request_type=EMPTY, response_type=BOOL)
        self.register_operation(
            "navigate_to_pose",
            self._navigate_to_pose,
            request_type=NAVIGATE_TO_POSE_REQUEST,
            feedback_type=NAVIGATION_FEEDBACK,
            result_type=NAVIGATION_RESULT,
            state_type=NAVIGATION_STATE,
        )
        self.register_operation(
            "follow_path",
            self._follow_path,
            request_type=FOLLOW_PATH_REQUEST,
            feedback_type=NAVIGATION_FEEDBACK,
            result_type=NAVIGATION_RESULT,
            state_type=NAVIGATION_STATE,
        )
        return True

    def on_start(self) -> bool:
        self._cancel_requested.clear()
        self.start_event_transport()
        self.start_procedure_server()
        self.start_operation_server()
        self._publish_state("idle")
        return True

    def on_terminate(self) -> bool:
        self._cancel_requested.set()
        self._stop_motion()
        self.stop_operation_server()
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        return self.on_terminate()

    def on_pause(self) -> bool:
        return self.on_terminate()

    def on_resume(self) -> bool:
        return self.on_start()

    def on_stop(self) -> bool:
        return self.on_terminate()

    def _on_pose_estimate(self, value: Any, timestamp: int, _timing: Any = None) -> None:
        if not isinstance(value, dict):
            return
        if str(value.get("frame_id") or "") != "map":
            return
        with self._pose_lock:
            self._current_pose = planar_pose(value)
            self._pose_timestamp_ns = int(timestamp)

    def _cancel_navigation(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._cancel_requested.set()
        self._stop_motion()
        self._publish_state("cancel_requested")
        return {"value": True}

    def _navigate_to_pose(self, params: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
        params = params or {}
        target_pose = params.get("target_pose")
        if not isinstance(target_pose, dict):
            raise ValueError("navigate_to_pose requires target_pose")
        frame_id = str(params.get("frame_id") or "map")
        body_radar_transform = None
        if frame_id.strip() == "cleaner_radar":
            body_radar_transform = self.lookup_frame_transform("cleaner_base", "cleaner_radar")
        current = self._fresh_pose()
        target = resolve_target_pose(
            frame_id=frame_id,
            target_pose=target_pose,
            current_map_pose=current,
            body_radar_transform=body_radar_transform,
        )
        return self._run_goal(target, context=context)

    def _follow_path(self, params: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
        path = (params or {}).get("path")
        if not isinstance(path, dict):
            raise ValueError("follow_path requires path")
        waypoints = path.get("waypoints")
        if not isinstance(waypoints, list) or not waypoints:
            raise ValueError("follow_path requires at least one waypoint")
        frame_id = str(path.get("frame_id") or "map")
        for waypoint in waypoints:
            if not isinstance(waypoint, dict):
                raise ValueError("follow_path waypoint must be a pose")
            body_radar_transform = None
            if frame_id.strip() == "cleaner_radar":
                body_radar_transform = self.lookup_frame_transform("cleaner_base", "cleaner_radar")
            current = self._fresh_pose()
            target = resolve_target_pose(
                frame_id=frame_id,
                target_pose=waypoint,
                current_map_pose=current,
                body_radar_transform=body_radar_transform,
            )
            result = self._run_goal(target, context=context)
            if not result.get("completed"):
                return result
        return {"completed": True, "detail": "path completed"}

    def _run_goal(self, target: tuple[float, float, float], *, context: Any = None) -> dict[str, Any]:
        if not self._goal_lock.acquire(blocking=False):
            raise RuntimeError("navigation already has an active goal")
        self._cancel_requested.clear()
        self._active_operation_id = str(getattr(context, "operation_id", "") or "")
        self._last_control_tick_ns = None
        self._publish_state("navigating")
        try:
            while True:
                if self._cancel_requested.is_set() or bool(context and context.is_cancel_requested()):
                    self._stop_motion()
                    result = {"completed": False, "detail": "navigation canceled"}
                    if context is not None:
                        context.canceled(result)
                    return result
                current = self._fresh_pose()
                command = navigation_command(
                    current,
                    target,
                    position_tolerance=self._position_tolerance,
                    yaw_tolerance=self._yaw_tolerance,
                    max_linear=self._max_linear,
                    max_angular=self._max_angular,
                )
                if bool(command["reached"]):
                    self._stop_motion()
                    self._publish_state("succeeded")
                    return {"completed": True, "detail": "target pose reached"}
                self._send_velocity(float(command["linear"]), float(command["angular"]))
                feedback = {
                    "state": "navigating",
                    "remaining_distance": float(command["remaining_distance"]),
                }
                if context is not None:
                    context.publish_feedback(feedback)
                self._publish_state("navigating")
                time.sleep(0.10)
        except Exception:
            self._stop_motion()
            self._publish_state("failed")
            raise
        finally:
            self._active_operation_id = ""
            self._goal_lock.release()

    def _fresh_pose(self) -> tuple[float, float, float]:
        with self._pose_lock:
            if self._current_pose is None:
                raise RuntimeError("navigation is waiting for localization")
            if self.now_ns() - self._pose_timestamp_ns > 2_000_000_000:
                raise RuntimeError("localization estimate is stale")
            return self._current_pose

    def _send_velocity(self, linear: float, angular: float) -> None:
        now_ns = self.now_ns()
        period_ms = 100.0 if self._last_control_tick_ns is None else clamp((now_ns - self._last_control_tick_ns) / 1_000_000.0, 50.0, 250.0)
        response = self.transact_bound_control(
            binding="mobility",
            topic="velocity_command",
            type="bytes",
            value={"twist": twist(linear_x=linear, angular_z=angular), "source": self.name},
            period_ms=period_ms,
            deadline_ms=max(period_ms * 1.5, 80.0),
            last_tick_ns=self._last_control_tick_ns,
            request_type=VELOCITY_COMMAND,
            response_type=COMMAND_ACK,
            timing={"generated_at_sync_ns": now_ns, "valid_for_ns": 250_000_000, "late_policy": "reject"},
        )
        self._last_control_tick_ns = now_ns
        if not response_ok(response):
            raise RuntimeError(str(response.get("error_message") or response.get("error") or "mobility command failed"))

    def _stop_motion(self) -> None:
        try:
            response = self.call_bound_procedure(
                binding="mobility",
                procedure="stop",
                params={},
                timeout_ms=1000,
                request_type=STOP_REQUEST,
                response_type=COMMAND_ACK,
            )
            if not response_ok(response):
                self.logger.warning("Navigation", f"stop command failed: {extract_payload(response)}")
        except Exception as exc:
            self.logger.warning("Navigation", f"stop command failed: {exc}")

    def _publish_state(self, state: str) -> None:
        self.publish_event(
            topic="navigation_state",
            value={"state": state, "active_goal_id": self._active_operation_id},
            timestamp=self.now_ns(),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner navigation component.")
    parser.add_argument("--name", default="component.navigation", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    parser.add_argument("--position-tolerance", type=float, default=0.018)
    parser.add_argument("--yaw-tolerance-deg", type=float, default=4.0)
    parser.add_argument("--max-linear", type=float, default=0.70)
    parser.add_argument("--max-angular", type=float, default=1.0)
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    run_subsystem(
        NavigationSubsystem(
            name=args.name,
            log_level=args.log_level,
            user_data_path=args.user_data_path,
            manifest_path=args.manifest,
            deployment_manifest_path=args.deployment_manifest,
            position_tolerance=args.position_tolerance,
            yaw_tolerance_deg=args.yaw_tolerance_deg,
            max_linear=args.max_linear,
            max_angular=args.max_angular,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
