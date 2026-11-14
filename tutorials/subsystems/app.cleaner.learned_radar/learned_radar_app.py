from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _tutorial_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir():
            return parent
    candidates = [
        os.environ.get("R2KERNEL_TUTORIAL_ROOT"),
        str(Path(os.environ.get("R2KERNEL_HOME", "/opt/r2kernel")) / "tutorials"),
        "/opt/r2kernel/tutorials",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "subsystems").is_dir() and (root / "simulator").is_dir():
            return root
    raise RuntimeError(
        "could not locate R2Kernel tutorial runtime; set R2KERNEL_TUTORIAL_ROOT or install tutorials under /opt/r2kernel/tutorials"
    )


_PACKAGE_ROOT = Path(__file__).resolve().parent
_ROOT = _tutorial_root()
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import parse_bool_text  # noqa: E402
from learned_radar_policy import RadarPolicyModel, feature_vector, select_radar_observation  # noqa: E402
from navigation_math import radar_detection_in_body  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import APP_STATUS, EMPTY, POSE_ESTIMATE  # noqa: E402

try:
    from r2kernel.wrapper.subsystem import AppSubsystem as _SubsystemImportCheck  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))

from app_base import CleanerApplicationBase  # noqa: E402


class _RunCancelled(Exception):
    """Internal control-flow signal for a stopped or superseded policy run."""


BODY_FRAME_ID = "cleaner_base"
RADAR_FRAME_ID = "cleaner_radar"


def _yaw_from_pose(value: dict[str, Any]) -> float:
    orientation = ((value.get("pose") or {}).get("orientation") or {})
    x = float(orientation.get("x") or 0.0)
    y = float(orientation.get("y") or 0.0)
    z = float(orientation.get("z") or 0.0)
    w = float(orientation.get("w") if orientation.get("w") is not None else 1.0)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class LearnedRadarCleanerApp(CleanerApplicationBase):
    def __init__(
        self,
        *,
        name: str = "app.cleaner.learned_radar",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        model_path: str | None = None,
        auto_start: bool = False,
        scan_range: float = 0.30,
        collect_distance: float = 0.18,
        collect_bearing_deg: float = 7.0,
        loop_period: float = 0.12,
        search_period: float = 32.0,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "learned_radar"),
            manifest_path=manifest_path or default_manifest_path(__file__, "learned_radar_app"),
            deployment_manifest_path=deployment_manifest_path or str((_ROOT / "deployments" / "learned-radar.deployment.yaml").resolve()),
            mode="learned_radar",
        )
        self._auto_start = auto_start
        self._scan_range = max(0.05, min(float(scan_range), 0.30))
        self._collect_distance = max(0.08, min(float(collect_distance), self._scan_range))
        self._collect_bearing_deg = max(1.0, min(float(collect_bearing_deg), 20.0))
        self._loop_period = max(0.08, min(float(loop_period), 0.30))
        self._search_period = max(12.0, float(search_period))
        model_candidate = Path(model_path) if model_path else (_PACKAGE_ROOT / "radar_policy_v1.json")
        if not model_candidate.is_absolute() and not model_candidate.exists():
            model_candidate = _PACKAGE_ROOT / model_candidate
        self._model_path = model_candidate.resolve()
        self._model = RadarPolicyModel.load(self._model_path)
        self._model_sha256 = hashlib.sha256(self._model_path.read_bytes()).hexdigest()[:12]
        self._pose_lock = threading.RLock()
        self._pose: tuple[float, float, float] | None = None
        self._pose_timestamp_ns = 0
        self._run_transition_lock = threading.RLock()
        self._run_state_lock = threading.RLock()
        self._command_lock = threading.RLock()
        self._run_generation = 0
        self._stop_loop = threading.Event()
        self._worker: threading.Thread | None = None
        self._search_started = time.monotonic()
        self._progress_tick = 0
        self._active_target_id: str | None = None

    def on_initialize(self) -> bool:
        if not super().on_initialize():
            return False
        self.register_event_subscribe(
            topic="pose_estimate",
            type=POSE_ESTIMATE,
            callback=self._on_pose_estimate,
        )
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

    def on_stop(self) -> bool:
        self._stop({})
        return super().on_stop()

    def _on_pose_estimate(self, value: Any, timestamp: int, _timing: Any = None) -> None:
        if not isinstance(value, dict) or str(value.get("frame_id") or "") != "map":
            return
        position = ((value.get("pose") or {}).get("position") or {})
        with self._pose_lock:
            self._pose = (float(position.get("x") or 0.0), float(position.get("y") or 0.0), _yaw_from_pose(value))
            self._pose_timestamp_ns = int(timestamp)

    def _fresh_pose(self) -> tuple[float, float, float]:
        with self._pose_lock:
            if self._pose is None:
                raise RuntimeError("learned radar policy is waiting for localization")
            if self.now_ns() - self._pose_timestamp_ns > 2_000_000_000:
                raise RuntimeError("learned radar policy localization is stale")
            return self._pose

    def _start(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        with self._run_transition_lock:
            with self._run_state_lock:
                if self._worker is not None and self._worker.is_alive():
                    return self._status_payload(reason="learned_radar.already_running")
                self._run_generation += 1
                generation = self._run_generation
                stop_event = threading.Event()
                self._stop_loop = stop_event
                self._search_started = time.monotonic()
                self._active_target_id = None
                self._progress_tick = 0
                worker = threading.Thread(
                    target=self._loop,
                    args=(generation, stop_event),
                    daemon=True,
                )
                self._worker = worker
            worker.start()
            status = self._set_status(
                action="learned_radar.start",
                result={"model": "behavior_cloning_mlp", "sha256": self._model_sha256},
                active=True,
            )
            self._publish_status(reason="learned_radar.start")
            return status

    def _stop(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        with self._run_transition_lock:
            with self._run_state_lock:
                stop_event = self._stop_loop
                worker = self._worker
                stop_event.set()
                self._run_generation += 1

            # Every policy actuation holds this lock from its final cancellation
            # check until the call returns. Once this section completes, no
            # canceled worker can issue a command after the Stop response.
            with self._command_lock:
                self._safe_stop_drive()
                try:
                    applied = self._stop_drive()
                except Exception as exc:
                    applied = {"accepted": False, "reason": str(exc)}

            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1.0)
            with self._run_state_lock:
                if self._worker is worker and (worker is None or not worker.is_alive()):
                    self._worker = None

            status = self._set_status(
                action="learned_radar.stop",
                result={
                    "policy": "behavior_cloning_mlp",
                    "model_sha256": self._model_sha256,
                    "observation": {"detected": False, "target_id": None, "distance": None, "bearing_deg": None},
                    "proposed": {"linear": 0.0, "angular": 0.0},
                    "applied": applied,
                },
                active=False,
            )
            self._publish_status(reason="learned_radar.stop")
            return status

    def _run_context(self) -> tuple[int, threading.Event]:
        with self._run_state_lock:
            return self._run_generation, self._stop_loop

    def _require_current_run(self, generation: int, stop_event: threading.Event) -> None:
        with self._run_state_lock:
            if (
                stop_event.is_set()
                or generation != self._run_generation
                or stop_event is not self._stop_loop
            ):
                raise _RunCancelled()

    def _run_guarded(
        self,
        generation: int,
        stop_event: threading.Event,
        action: Callable[[], Any],
    ) -> Any:
        with self._command_lock:
            self._require_current_run(generation, stop_event)
            result = action()
            self._require_current_run(generation, stop_event)
            return result

    def _wait_for_run(
        self,
        generation: int,
        stop_event: threading.Event,
        timeout: float,
    ) -> None:
        if stop_event.wait(timeout):
            raise _RunCancelled()
        self._require_current_run(generation, stop_event)

    def _loop(self, generation: int, stop_event: threading.Event) -> None:
        try:
            while not stop_event.wait(self._loop_period):
                try:
                    self._loop_once(generation, stop_event)
                except _RunCancelled:
                    break
                except Exception as exc:
                    try:
                        self._run_guarded(
                            generation,
                            stop_event,
                            lambda: self._record_loop_error(action="learned_radar.error", exc=exc),
                        )
                    except _RunCancelled:
                        break
                    stop_event.wait(0.35)
        finally:
            current = threading.current_thread()
            with self._run_state_lock:
                if self._worker is current:
                    self._worker = None

    def _select_detection(self, detections: list[dict[str, Any]]) -> dict[str, Any] | None:
        target, self._active_target_id = select_radar_observation(detections, active_target_id=self._active_target_id)
        return target

    def _wait_for_target_sample(self, generation: int, stop_event: threading.Event) -> None:
        def stop_and_report() -> None:
            applied = self._stop_drive()
            if not isinstance(applied, dict) or applied.get("accepted") is not True:
                raise RuntimeError(f"waiting_sample stop failed: {applied!r}")
            self._set_status(
                action="learned_radar.waiting_sample",
                result={
                    "policy": "behavior_cloning_mlp",
                    "model_sha256": self._model_sha256,
                    "observation": {"frame_id": BODY_FRAME_ID, "detected": False,
                                    "target_id": self._active_target_id, "distance": None, "bearing_deg": None},
                    "proposed": None,
                    "applied": applied,
                },
                active=True,
            )
            self._progress_tick += 1
            if self._progress_tick % 4 == 0:
                self._publish_status(reason="learned_radar.waiting_sample")

        self._run_guarded(generation, stop_event, stop_and_report)

    def _loop_once(
        self,
        generation: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if generation is None or stop_event is None:
            generation, stop_event = self._run_context()
        self._require_current_run(generation, stop_event)
        x, y, heading = self._fresh_pose()
        self._require_current_run(generation, stop_event)
        scan = self._scan(max_range=self._scan_range)
        self._require_current_run(generation, stop_event)
        target = self._select_detection(scan.get("detections") or [])
        if target is None and self._active_target_id is not None:
            self._wait_for_target_sample(generation, stop_event)
            return
        body_radar_transform = None
        source_frame_id = scan.get("source_frame_id")
        if target is not None and source_frame_id == RADAR_FRAME_ID:
            body_radar_transform = self.lookup_frame_transform(BODY_FRAME_ID, RADAR_FRAME_ID)
        # Training's expert maps body bearing directly to body angular velocity;
        # the same body geometry determines the gripper's collection reach.
        if target is not None:
            target = radar_detection_in_body(target, source_frame_id=source_frame_id,
                                             body_radar_transform=body_radar_transform)
            distance = float(target.get("distance") or self._scan_range)
            bearing = float(target.get("bearing_deg") or 0.0)
            if distance <= self._collect_distance and abs(bearing) <= self._collect_bearing_deg:
                self._run_guarded(generation, stop_event, self._stop_drive)
                self._collect_target(target, generation, stop_event)
                return

        phase = ((time.monotonic() - self._search_started) / self._search_period) * math.tau
        features = feature_vector(
            x=x,
            y=y,
            heading=heading,
            phase=phase,
            detection=target,
            scan_range=self._scan_range,
        )
        action = self._model.predict(features)
        proposed = {"linear": round(float(action[0]), 4), "angular": round(float(action[1]), 4)}
        mode = "approach" if target is not None else "explore"

        def drive_and_report() -> None:
            applied = self._drive(
                vx=float(action[0]),
                wz=float(action[1]),
            )
            result = {
                "policy": "behavior_cloning_mlp",
                "model_sha256": self._model_sha256,
                "observation": {
                    "frame_id": BODY_FRAME_ID,
                    "detected": target is not None,
                    "target_id": None if target is None else target.get("id"),
                    "distance": None if target is None else target.get("distance"),
                    "bearing_deg": None if target is None else target.get("bearing_deg"),
                },
                "proposed": proposed,
                "applied": applied,
            }
            self._set_status(action=f"learned_radar.{mode}", result=result, active=True)
            self._progress_tick += 1
            if self._progress_tick % 4 == 0:
                self._publish_status(reason=f"learned_radar.{mode}")

        self._run_guarded(generation, stop_event, drive_and_report)

    def _collect_target(
        self,
        target: dict[str, Any],
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        def report_start() -> None:
            self._set_status(action="learned_radar.collect", result={"target": target}, active=True)
            self._publish_status(reason="learned_radar.collect")

        self._run_guarded(generation, stop_event, report_start)
        steps: list[dict[str, Any]] = []
        distance = float(target.get("distance") or 0.0)
        extension = max(0.0, min((distance - 0.08) / 0.18, 1.0))
        try:
            opened = self._run_guarded(generation, stop_event, self._open_gripper)
            self._require_action(opened, field="opened", stage="gripper_open")
            steps.append({"name": "gripper_open", "state": "done"})
            self._run_guarded(generation, stop_event, lambda: self._extend_arm(extension))
            steps.append({"name": "reach", "state": "done"})
            self._wait_for_run(generation, stop_event, 0.08)
            closed = self._run_guarded(generation, stop_event, self._close_gripper)
            self._require_action(closed, field="closed", stage="gripper_close")
            steps.append({"name": "gripper_close", "state": "done"})
            self._wait_for_run(generation, stop_event, 0.08)
            self._run_guarded(generation, stop_event, lambda: self._shrink_arm(0.0))
            steps.append({"name": "retract", "state": "done"})
            self._wait_for_run(generation, stop_event, 0.08)
            burned = self._run_guarded(generation, stop_event, self._burn)
            self._require_action(burned, field="fired", stage="burn")
            steps.append({"name": "burn", "state": "done"})
        except _RunCancelled:
            raise
        except Exception as exc:
            try:
                self._run_guarded(generation, stop_event, lambda: self._shrink_arm(0.0))
            except _RunCancelled:
                raise
            except Exception:
                pass
            self._run_guarded(generation, stop_event, self._safe_stop_drive)
            def report_fault() -> None:
                self._set_status(
                    action="learned_radar.fault",
                    result={"collected": False, "target": target, "steps": steps, "error": str(exc)},
                    active=False,
                )
                self._publish_status(reason="learned_radar.fault")

            self._run_guarded(generation, stop_event, report_fault)
            stop_event.set()
            return

        def report_complete() -> None:
            self._active_target_id = None
            self._set_status(
                action="learned_radar.collect_done",
                result={"collected": True, "target_id": target.get("id"), "steps": steps},
                active=True,
            )
            self._publish_status(reason="learned_radar.collect_done")

        self._run_guarded(generation, stop_event, report_complete)

    @staticmethod
    def _require_action(result: Any, *, field: str, stage: str) -> None:
        if not isinstance(result, dict) or not bool(result.get(field)):
            raise RuntimeError(f"{stage} failed: {result!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the model-based learned radar cleaner app.")
    parser.add_argument("--name", default="app.cleaner.learned_radar")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--auto-start", nargs="?", const="true", default="false")
    parser.add_argument("--model", default=None)
    parser.add_argument("--scan-range", type=float, default=0.30)
    parser.add_argument("--collect-distance", type=float, default=0.18)
    parser.add_argument("--collect-bearing-deg", type=float, default=7.0)
    parser.add_argument("--loop-period", type=float, default=0.12)
    parser.add_argument("--search-period", type=float, default=32.0)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--deployment-manifest", default=None)
    parser.add_argument("--user-data-path", default=None)
    args = parser.parse_args()
    ensure_control_running()
    wait_for_control_ready()
    run_subsystem(
        LearnedRadarCleanerApp(
            name=args.name,
            log_level=args.log_level,
            user_data_path=args.user_data_path,
            manifest_path=args.manifest,
            deployment_manifest_path=args.deployment_manifest,
            model_path=args.model,
            auto_start=parse_bool_text(args.auto_start),
            scan_range=args.scan_range,
            collect_distance=args.collect_distance,
            collect_bearing_deg=args.collect_bearing_deg,
            loop_period=args.loop_period,
            search_period=args.search_period,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
