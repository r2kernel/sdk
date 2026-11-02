from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

_ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import parse_bool_text  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)

try:
    from r2kernel.wrapper.subsystem import AppSubsystem as _SubsystemImportCheck  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))

from app_base import CleanerApplicationBase  # noqa: E402
from navigation_math import body_pose_in_radar, radar_point_in_body  # noqa: E402
from standard_types import APP_STATUS, EMPTY, pose  # noqa: E402


BODY_FRAME_ID = "cleaner_base"
RADAR_FRAME_ID = "cleaner_radar"


class RadarTargetQueue:
    """Locks one visible target and queues other contacts by acquisition order."""

    def __init__(self) -> None:
        self.active_id: str | None = None
        self._pending: deque[str] = deque()

    def clear(self) -> None:
        self.active_id = None
        self._pending.clear()

    def complete_active(self) -> None:
        self.active_id = None

    def select(self, detections: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
        by_id = {
            str(item.get("id") or ""): item
            for item in detections
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        visible_ids = set(by_id)
        lost_id: str | None = None
        if self.active_id and self.active_id not in visible_ids:
            lost_id = self.active_id
            self.active_id = None

        self._pending = deque(
            target_id
            for target_id in self._pending
            if target_id in visible_ids and target_id != self.active_id
        )
        queued = set(self._pending)
        for target_id in by_id:
            if target_id != self.active_id and target_id not in queued:
                self._pending.append(target_id)
                queued.add(target_id)

        if self.active_id is None and self._pending:
            self.active_id = self._pending.popleft()
        return (by_id.get(self.active_id) if self.active_id else None), lost_id

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class RadarCleanerApp(CleanerApplicationBase):
    def __init__(
        self,
        *,
        name: str = "app.cleaner.radar",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        auto_start: bool = False,
        scan_range: float = 0.30,
        collect_distance: float = 0.18,
        collect_bearing_deg: float = 5.0,
        loop_period: float = 0.12,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "radar"),
            manifest_path=manifest_path or default_manifest_path(__file__, "radar_app"),
            deployment_manifest_path=deployment_manifest_path or str((_ROOT / "deployments" / "radar.deployment.yaml").resolve()),
            mode="radar",
        )
        self._auto_start = auto_start
        self._scan_range = scan_range
        self._collect_distance = collect_distance
        self._collect_bearing_deg = collect_bearing_deg
        self._minimum_collect_distance = 0.07
        self._loop_period = loop_period
        self._stop_loop = threading.Event()
        self._worker: threading.Thread | None = None
        self._progress_tick = 0
        self._targets = RadarTargetQueue()

    def on_initialize(self) -> bool:
        if not super().on_initialize():
            return False
        self.register_procedure("start", self._start, request_type=EMPTY, response_type=APP_STATUS)
        self.register_procedure("stop", self._stop, request_type=EMPTY, response_type=APP_STATUS)
        return True

    def on_start(self) -> bool:
        if not super().on_start():
            return False
        if self._auto_start:
            self._start({})
        return True

    def on_terminate(self) -> bool:
        self._stop({})
        return super().on_terminate()

    def on_pause(self) -> bool:
        self._stop({})
        return super().on_pause()

    def on_resume(self) -> bool:
        return super().on_resume()

    def on_stop(self) -> bool:
        self._stop({})
        return super().on_stop()

    def _start(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        if self._worker is not None and self._worker.is_alive():
            return self._status_payload(reason="radar.already_running")
        self._stop_loop.clear()
        self._progress_tick = 0
        self._targets.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        status = self._set_status(action="radar.start", active=True)
        self._publish_status(reason="radar.start")
        return status

    def _stop(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._stop_loop.set()
        self._safe_stop_drive()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        self._targets.clear()
        status = self._set_status(action="radar.stop", active=False)
        self._publish_status(reason="radar.stop")
        return status

    def _command(self, params: dict[str, Any] | None) -> dict[str, Any]:
        command = str((params or {}).get("command") or (params or {}).get("action") or "status").strip().lower()
        if command == "start":
            return self._start({})
        if command == "stop":
            return self._stop({})
        return self._status_payload(reason=f"radar.command.{command or 'status'}")

    def _loop(self) -> None:
        while not self._stop_loop.wait(self._loop_period):
            try:
                self._loop_once()
            except Exception as exc:
                self._record_loop_error(action="radar.error", exc=exc)
                self._stop_loop.wait(0.35)

    def _loop_once(self) -> None:
        scan = self._scan(max_range=self._scan_range)
        detections = [
            detection
            for detection in scan.get("detections") or []
            if isinstance(detection, dict) and detection.get("fresh") is True
        ]
        target, lost_target_id = self._targets.select(detections)
        if target is None:
            self._set_status(
                action="radar.target_lost" if lost_target_id else "radar.no_target",
                result={
                    "scan_range": self._scan_range,
                    "detections": len(detections),
                    "lost_target_id": lost_target_id,
                    "queued_targets": self._targets.pending_count,
                },
                active=True,
            )
            self._publish_progress(reason="radar.target_lost" if lost_target_id else "radar.no_target")
            return

        source_frame_id = scan.get("source_frame_id")
        if source_frame_id not in {BODY_FRAME_ID, RADAR_FRAME_ID}:
            raise ValueError(f"unsupported radar observation frame: {source_frame_id!r}")
        bearing_degrees = float(target["bearing_deg"])
        sensor_distance = float(target["distance"])
        if not math.isfinite(bearing_degrees) or not math.isfinite(sensor_distance) or sensor_distance <= 0.0:
            raise ValueError("radar target requires finite bearing and positive distance")
        sensor_bearing = math.radians(bearing_degrees)
        sensor_x = math.cos(sensor_bearing) * sensor_distance
        sensor_y = math.sin(sensor_bearing) * sensor_distance
        body_radar_transform = None
        if source_frame_id == RADAR_FRAME_ID:
            body_radar_transform = self.lookup_frame_transform(BODY_FRAME_ID, RADAR_FRAME_ID)
            body_x, body_y = radar_point_in_body(sensor_x, sensor_y, body_radar_transform)
        else:
            body_x, body_y = sensor_x, sensor_y
        body_distance = math.hypot(body_x, body_y)
        body_bearing = math.atan2(body_y, body_x)
        if (
            self._minimum_collect_distance <= body_distance < self._collect_distance
            and abs(math.degrees(body_bearing)) < self._collect_bearing_deg
        ):
            self._collect_target(target, body_distance=body_distance)
            return

        stand_off = max(self._collect_distance * 0.82, self._minimum_collect_distance)
        # Keep a target inside the gripper's reachable band. A signed travel is
        # intentional: targets closer than the minimum reach require the base
        # to move away before attempting collection.
        travel = body_distance - stand_off
        body_goal_x = math.cos(body_bearing) * travel
        body_goal_y = math.sin(body_bearing) * travel
        if source_frame_id == RADAR_FRAME_ID:
            goal_x, goal_y, goal_yaw = body_pose_in_radar(
                body_goal_x, body_goal_y, body_bearing, body_radar_transform
            )
        else:
            goal_x, goal_y, goal_yaw = body_goal_x, body_goal_y, body_bearing
        goal = pose(x=goal_x, y=goal_y, yaw=goal_yaw)
        self._set_status(action="radar.navigate", result={"target": target}, active=True)
        self._publish_status(reason="radar.navigate")
        navigation = self._navigate_to_pose(
            frame_id=source_frame_id,
            target_pose=goal,
            should_cancel=self._stop_loop.is_set,
            timeout_s=8.0,
        )
        if not bool(navigation.get("completed")):
            if str(navigation.get("state") or "") != "canceled":
                self._set_status(
                    action="radar.navigation_failed",
                    result={"target": target, "navigation": navigation},
                    active=True,
                )
                self._publish_status(reason="radar.navigation_failed")
            return
        # Navigation changes the target geometry. Collect only after the next
        # radar observation confirms the updated distance and bearing.
        self._set_status(
            action="radar.navigation_done",
            result={"target_id": target.get("id"), "navigation": navigation},
            active=True,
        )
        self._publish_status(reason="radar.navigation_done")

    def _collect_target(self, target: dict[str, Any], *, body_distance: float) -> None:
        self._set_status(action="radar.collect", result={"target": target}, active=True)
        self._publish_status(reason="radar.collect")
        steps: list[dict[str, Any]] = []
        extension = max(0.0, min((body_distance - 0.08) / 0.18, 1.0))
        try:
            opened = self._open_gripper()
            self._require_action(opened, field="opened", stage="gripper_open")
            steps.append({"name": "gripper_open", "state": "done", "detail": opened})

            reached = self._extend_arm(extension)
            steps.append({"name": "reach", "state": "done", "detail": reached})
            time.sleep(0.08)

            closed = self._close_gripper()
            self._require_action(closed, field="closed", stage="gripper_close")
            steps.append({"name": "gripper_close", "state": "done", "detail": closed})
            time.sleep(0.08)

            retracted = self._shrink_arm(0.0)
            steps.append({"name": "retract", "state": "done", "detail": retracted})
            time.sleep(0.08)

            burned = self._burn()
            self._require_action(burned, field="fired", stage="burn")
            steps.append({"name": "burn", "state": "done", "detail": burned})
        except Exception as exc:
            failure = {
                "collected": False,
                "target": target,
                "steps": steps,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            try:
                self._shrink_arm(0.0)
            except Exception:
                pass
            self._stop_loop.set()
            self._safe_stop_drive()
            self._set_status(action="radar.fault", result=failure, active=False)
            self._publish_status(reason="radar.fault")
            return

        result = {"collected": True, "target": target, "steps": steps}
        self._targets.complete_active()
        self._set_status(action="radar.collect_done", result=result, active=True)
        self._publish_status(reason="radar.collect_done")

    @staticmethod
    def _require_action(result: Any, *, field: str, stage: str) -> None:
        if not isinstance(result, dict) or not bool(result.get(field)):
            raise RuntimeError(f"{stage} failed: {result!r}")

    def _publish_progress(self, *, reason: str) -> None:
        self._progress_tick += 1
        if self._progress_tick % 5 == 0:
            self._publish_status(reason=reason)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner radar app.")
    parser.add_argument("--name", default="app.cleaner.radar", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--auto-start", nargs="?", const="true", default="false", help="Start the cleaning loop immediately.")
    parser.add_argument("--scan-range", type=float, default=0.30, help="Trash detection range for 360 degree scans.")
    parser.add_argument("--collect-distance", type=float, default=0.18, help="Distance threshold used to collect detected trash.")
    parser.add_argument("--collect-bearing-deg", type=float, default=5.0, help="Bearing threshold used to collect detected trash.")
    parser.add_argument("--loop-period", type=float, default=0.12, help="Radar decision-loop period in seconds.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = RadarCleanerApp(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        auto_start=parse_bool_text(args.auto_start),
        scan_range=args.scan_range,
        collect_distance=args.collect_distance,
        collect_bearing_deg=args.collect_bearing_deg,
        loop_period=args.loop_period,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
