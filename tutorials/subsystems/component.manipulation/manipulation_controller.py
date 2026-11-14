from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


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


_ROOT = _tutorial_root()
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import WORLD_EVENT_SIZE, as_float, clamp, response_ok  # noqa: E402
from control_cycles import ControlCycleIssuer, cycle_echo_matches  # noqa: E402
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
    EMPTY,
    JOINT_STATE,
    JOINT_VELOCITY,
    MOTION_FEEDBACK,
    MOTION_RESULT,
    MOTION_STATE,
    MOVE_C_ARGS,
    MOVE_J_ARGS,
    MOVE_L_ARGS,
    PAUSE_ARGS,
    POSE,
    SERVO_CONTROL_REQUEST,
    SERVO_CONTROL_RESPONSE,
    SET_PAYLOAD_ARGS,
    SET_TCP_ARGS,
    STOP_ARGS,
    TOOL_SETTINGS,
    empty_tool_settings,
    pose,
)

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class ManipulationControllerSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.manipulation",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "actuator_servo",
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "manipulation"),
            manifest_path=manifest_path or default_manifest_path(__file__, "manipulation_controller"),
            deployment_manifest_path=deployment_manifest_path,
        )
        self._binding = binding
        self._extension = 0.0
        self._paused = False
        self._tcp: dict[str, Any] = {}
        self._payload: dict[str, Any] = {}
        self._last_servo_tick_ns: int | None = None
        self._servo_cycles = ControlCycleIssuer(lambda: self.now_ns())

    def on_initialize(self) -> bool:
        self.register_event_publish(topic="joint_state", type=JOINT_STATE, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_event_publish(topic="tool_pose", type=POSE, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_event_publish(topic="motion_state", type=MOTION_STATE, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_event_publish(topic="tool_settings", type=TOOL_SETTINGS, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_control_endpoint(
            "joint_velocity",
            self._joint_velocity,
            request_type=JOINT_VELOCITY,
            response_type=BOOL,
            timing_support="required",
        )
        self.register_procedure("stop", self._stop, request_type=STOP_ARGS, response_type=BOOL)
        self.register_procedure("pause", self._pause, request_type=PAUSE_ARGS, response_type=BOOL)
        self.register_procedure("resume", self._resume, request_type=EMPTY, response_type=BOOL)
        self.register_procedure("set_tcp", self._set_tcp, request_type=SET_TCP_ARGS, response_type=BOOL)
        self.register_procedure("set_payload", self._set_payload, request_type=SET_PAYLOAD_ARGS, response_type=BOOL)
        self.register_operation("move_j", self._move_j, request_type=MOVE_J_ARGS, feedback_type=MOTION_FEEDBACK, result_type=MOTION_RESULT, state_type=MOTION_STATE)
        self.register_operation("move_l", self._move_l, request_type=MOVE_L_ARGS, feedback_type=MOTION_FEEDBACK, result_type=MOTION_RESULT, state_type=MOTION_STATE)
        self.register_operation("move_c", self._move_c, request_type=MOVE_C_ARGS, feedback_type=MOTION_FEEDBACK, result_type=MOTION_RESULT, state_type=MOTION_STATE)
        self.logger.info("Lifecycle", "manipulation controller initialized")
        return True

    def on_start(self) -> bool:
        self._paused = False
        self._servo_cycles.restart()
        self.start_event_transport()
        self.start_control_server()
        self.start_procedure_server()
        self.start_operation_server()
        self._publish_state(reason="start")
        self._report_tool_transform()
        return True

    def on_terminate(self) -> bool:
        self.stop_operation_server()
        self.stop_procedure_server()
        self.stop_control_server()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        self._paused = False
        self.stop_operation_server()
        self.stop_procedure_server()
        self.stop_control_server()
        self.stop_event_transport()
        return True

    def on_pause(self) -> bool:
        self._pause({})
        return True

    def on_resume(self) -> bool:
        self._resume({})
        self.start_event_transport()
        self.start_control_server()
        self.start_procedure_server()
        self.start_operation_server()
        return True

    def on_stop(self) -> bool:
        self._stop({})
        return self.on_terminate()

    def _joint_velocity(self, params: dict[str, Any] | None, _context: Any = None) -> dict[str, Any]:
        params = params or {}
        if self._paused:
            return {"value": False}
        if "extension" in params:
            extension = clamp(as_float(params, "extension", self._extension), 0.0, 1.0)
        elif isinstance(params.get("values"), list) and params["values"]:
            extension = clamp(float(params["values"][0]), 0.0, 1.0)
        else:
            velocity = clamp(as_float(params, "velocity", 0.0), -1.0, 1.0)
            dt = clamp(as_float(params, "dt", 0.08), 0.01, 0.5)
            extension = clamp(self._extension + velocity * dt, 0.0, 1.0)
        self._apply_extension(extension, reason="joint_velocity")
        return {"value": True}

    def _stop(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._publish_state(reason="stop")
        return {"value": True}

    def _pause(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._paused = True
        self._publish_state(reason="pause")
        return {"value": True}

    def _resume(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._paused = False
        self._publish_state(reason="resume")
        return {"value": True}

    def _set_tcp(self, params: dict[str, Any] | None) -> dict[str, Any]:
        self._tcp = dict(params or {})
        self._publish_state(reason="set_tcp")
        return {"value": True}

    def _set_payload(self, params: dict[str, Any] | None) -> dict[str, Any]:
        self._payload = dict(params or {})
        self._publish_state(reason="set_payload")
        return {"value": True}

    def _move_j(self, params: dict[str, Any] | None) -> dict[str, Any]:
        extension = self._target_extension(params)
        self._apply_extension(extension, reason="move_j")
        return {"completed": True, "detail": "move_j command acknowledged; physical completion is not verified"}

    def _move_l(self, params: dict[str, Any] | None) -> dict[str, Any]:
        extension = self._target_extension(params)
        self._apply_extension(extension, reason="move_l")
        return {"completed": True, "detail": "move_l command acknowledged; physical completion is not verified"}

    def _move_c(self, params: dict[str, Any] | None) -> dict[str, Any]:
        extension = self._target_extension(params)
        self._apply_extension(extension, reason="move_c")
        return {"completed": True, "detail": "move_c command acknowledged; physical completion is not verified"}

    def _target_extension(self, params: dict[str, Any] | None) -> float:
        params = params or {}
        if "extension" in params:
            return clamp(as_float(params, "extension", self._extension), 0.0, 1.0)
        for key in ("target_point", "via_point"):
            item = params.get(key)
            if isinstance(item, dict) and "extension" in item:
                return clamp(as_float(item, "extension", self._extension), 0.0, 1.0)
        waypoints = params.get("waypoints")
        if isinstance(waypoints, list) and waypoints:
            last = waypoints[-1]
            if isinstance(last, dict):
                joints = last.get("q")
                if isinstance(joints, dict) and isinstance(joints.get("values"), list) and joints["values"]:
                    try:
                        return clamp(float(joints["values"][0]), 0.0, 1.0)
                    except (TypeError, ValueError):
                        pass
                if "extension" in last:
                    return clamp(as_float(last, "extension", self._extension), 0.0, 1.0)
                if "positions" in last and isinstance(last["positions"], list) and last["positions"]:
                    try:
                        return clamp(float(last["positions"][0]), 0.0, 1.0)
                    except (TypeError, ValueError):
                        pass
        return self._extension

    def _apply_extension(self, extension: float, *, reason: str) -> None:
        # Record an attempt even when delivery is ambiguous; never replay it.
        last_tick_ns = self._last_servo_tick_ns
        self._last_servo_tick_ns = self.now_ns()
        cycle = self._servo_cycles.command()
        response = self.transact_bound_control(
            binding=self._binding,
            topic="servo_control",
            type="bytes",
            value={
                "cycle": cycle,
                "command": {
                    "emergency": False,
                    "servo_cmd": 0,
                    "drive_mode": [],
                    "target": {"names": ["arm_extension"], "target_position": [float(extension)]},
                },
            },
            period_ms=80.0,
            deadline_ms=100.0,
            last_tick_ns=last_tick_ns,
            request_type=SERVO_CONTROL_REQUEST,
            response_type=SERVO_CONTROL_RESPONSE,
        )
        self._warn_control_result("actuator_servo.servo_control", response)
        transport = response.get("transport") if isinstance(response, dict) else None
        acknowledgement = response.get("result") if isinstance(response, dict) else None
        if (
            not response_ok(response)
            or response.get("ok") is not True
            or response.get("error_code")
            or (transport is not None and not isinstance(transport, dict))
            or (isinstance(transport, dict) and (
                transport.get("ok", True) is not True or transport.get("timeout") or transport.get("error")
            ))
            or not cycle_echo_matches(acknowledgement, cycle)
            or acknowledgement.get("status") != "applied"
        ):
            raise RuntimeError("actuator_servo.servo_control command was not applied; physical state is unknown")
        self._extension = extension
        self._report_tool_transform()
        self._publish_state(reason=reason)

    def _warn_control_result(self, label: str, response: dict[str, Any]) -> None:
        transport = response.get("transport") if isinstance(response, dict) else None
        if not isinstance(transport, dict):
            return
        if transport.get("overrun"):
            self.logger.warning("Control", f"{label} overrun detected")
        if transport.get("timeout"):
            self.logger.warning("Control", f"{label} deadline timeout")

    def _publish_state(self, *, reason: str) -> None:
        ts = self.now_ns()
        settings = empty_tool_settings()
        payloads = {
            "joint_state": {
                "names": ["arm_extension"],
                "position": [self._extension],
                "velocity": [0.0],
            },
            "tool_pose": pose(x=self._extension),
            "motion_state": {
                "value": "paused" if self._paused else "idle",
                "progress": self._extension,
            },
            "tool_settings": settings,
        }
        for topic, payload in payloads.items():
            self.publish_event(topic=topic, value=payload, timestamp=ts)

    def _report_tool_transform(self) -> None:
        self.set_frame_transform(
            parent_frame="arm_base",
            child_frame="cleaner_gripper",
            translation=(0.08 + 0.18 * self._extension, 0.0, 0.0),
            rotation_xyzw=(0.0, 0.0, 0.0, 1.0),
            stale_after_ms=3000,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner manipulation controller.")
    parser.add_argument("--name", default="component.manipulation", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="actuator_servo", help="Required actuator gateway binding name.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = ManipulationControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
