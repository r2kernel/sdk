from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

_ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import WORLD_EVENT_SIZE, as_bool, as_float, clamp, extract_payload, response_ok  # noqa: E402
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
    CLEAR_FAULT_REQUEST,
    COMMAND_ACK,
    DISABLE_DRIVE_REQUEST,
    ENABLE_DRIVE_REQUEST,
    FAULT_STATE,
    MOBILE_BASE_STATE,
    ODOMETRY,
    SERVO_CONTROL_REQUEST,
    SERVO_CONTROL_RESPONSE,
    STOP_REQUEST,
    VELOCITY_COMMAND,
    VELOCITY_LIMITS,
    pose,
    twist,
)

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class MobileControllerSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.mobile",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "actuator_servo",
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "mobile"),
            manifest_path=manifest_path or default_manifest_path(__file__, "mobile_controller"),
            deployment_manifest_path=deployment_manifest_path,
        )
        self._binding = binding
        self._enabled = True
        self._paused = False
        self._faulted = False
        self._fault_detail = ""
        self._limits = {"max_linear": 1.0, "max_angular": 1.0, "max_dt": 0.50}
        self._last_command: dict[str, Any] = {"vx": 0.0, "vy": 0.0, "wz": 0.0, "dt": 0.16}
        self._last_world: Any = {}
        self._last_servo_tick_ns: int | None = None
        self._servo_cycles = ControlCycleIssuer(lambda: self.now_ns())
        self._spatial_x = 0.0
        self._spatial_y = 0.0
        self._spatial_heading = 0.0

    def on_initialize(self) -> bool:
        self.register_event_publish(topic="mobile_base_state", type=MOBILE_BASE_STATE, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_event_publish(topic="fault_state", type=FAULT_STATE, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_event_publish(topic="odometry", type=ODOMETRY, size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_control_endpoint(
            "velocity_command",
            self._velocity_command,
            request_type=VELOCITY_COMMAND,
            response_type=COMMAND_ACK,
            timing_support="required",
        )
        self.register_procedure(
            "set_velocity_reference",
            self._set_velocity_reference,
            request_type=VELOCITY_COMMAND,
            response_type=COMMAND_ACK,
        )
        self.register_procedure("set_velocity_limits", self._set_velocity_limits, request_type=VELOCITY_LIMITS, response_type=COMMAND_ACK)
        self.register_procedure("get_velocity_limits", self._get_velocity_limits, request_type="msg.core.common.primitive.v1/Empty", response_type=VELOCITY_LIMITS)
        self.register_procedure("stop", self._stop_drive, request_type=STOP_REQUEST, response_type=COMMAND_ACK)
        self.register_procedure("get_status", self._get_status, request_type="msg.core.common.primitive.v1/Empty", response_type=MOBILE_BASE_STATE)
        self.register_procedure("enable_drive", self._enable_drive, request_type=ENABLE_DRIVE_REQUEST, response_type=COMMAND_ACK)
        self.register_procedure("disable_drive", self._disable_drive, request_type=DISABLE_DRIVE_REQUEST, response_type=COMMAND_ACK)
        self.register_procedure("clear_fault", self._clear_fault, request_type=CLEAR_FAULT_REQUEST, response_type=COMMAND_ACK)
        self.logger.info("Lifecycle", "mobile controller initialized")
        return True

    def on_start(self) -> bool:
        self._paused = False
        self._servo_cycles.restart()
        self.start_event_transport()
        self.start_control_server()
        self.start_procedure_server()
        self._publish_state(reason="start")
        self._report_body_transform()
        self.logger.info("Lifecycle", f"mobile controller ready procedures={self.get_procedure_info()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        self.stop_control_server()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        self._paused = False
        self.stop_procedure_server()
        self.stop_control_server()
        self.stop_event_transport()
        return True

    def on_pause(self) -> bool:
        self._paused = True
        self._stop_drive({})
        self._publish_state(reason="pause")
        return True

    def on_resume(self) -> bool:
        self._paused = False
        self._servo_cycles.restart()
        self.start_event_transport()
        self.start_control_server()
        self.start_procedure_server()
        self._publish_state(reason="resume")
        return True

    def on_stop(self) -> bool:
        self._paused = False
        self._stop_drive({})
        return self.on_terminate()

    def _velocity_command(self, params: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
        return self._apply_velocity_command(params, context=context, reason="velocity_command")

    def _set_velocity_reference(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return self._apply_velocity_command(params, context=None, reason="set_velocity_reference", procedure_call=True)

    def _apply_velocity_command(
        self,
        params: dict[str, Any] | None,
        *,
        context: Any,
        reason: str,
        procedure_call: bool = False,
    ) -> dict[str, Any]:
        params = params or {}
        if self._paused:
            return self._ack(False, "drive is paused")
        if self._faulted:
            return self._ack(False, self._fault_detail or "faulted")
        if not self._enabled:
            return self._ack(False, "drive is disabled")
        command = self._normalize_velocity_command(params)
        generated_at_sync_ns = self.now_ns() if procedure_call else None
        if procedure_call:
            period_ms = self._procedure_control_period_ms(generated_at_sync_ns)
        else:
            period_ms = self._control_period_ms(command["dt"], context)
        command["dt"] = clamp(period_ms / 1000.0, 0.02, self._limits["max_dt"])
        timing = {
            "generated_at_sync_ns": getattr(context, "generated_at_sync_ns", None),
            "valid_until_sync_ns": getattr(context, "valid_until_sync_ns", None),
            "late_policy": "reject",
        }
        if procedure_call:
            # The gateway holds the base velocity for the remaining validity.
            validity_ms = self._procedure_control_validity_ms(period_ms)
            timing = {
                "generated_at_sync_ns": generated_at_sync_ns,
                "valid_for_ns": int(validity_ms * 1_000_000),
                "late_policy": "reject",
            }
        response, cycle = self._send_base_velocity(
            command,
            period_ms=period_ms,
            deadline_ms=max(period_ms, 50.0),
            timing=timing,
        )
        self._last_world = extract_payload(response)
        accepted = self._control_accepted(response, cycle)
        if accepted:
            self._last_command = command
            self._integrate_body_transform(command)
        self._publish_state(reason=reason)
        acknowledgement_reason = "ok" if accepted else self._servo_failure_reason(response, "servo command failed")
        applied_command = command if accepted else self._last_command
        return self._ack(
            accepted,
            acknowledgement_reason,
            vx=float(applied_command.get("vx", 0.0)),
            vy=float(applied_command.get("vy", 0.0)),
            wz=float(applied_command.get("wz", 0.0)),
            deadline_missed=self._control_deadline_missed(response),
        )

    def _send_base_velocity(
        self,
        command: dict[str, Any],
        *,
        period_ms: float,
        deadline_ms: float,
        timing: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
                    "target": {
                        "names": ["base_vx", "base_vy", "base_wz"],
                        "target_velocity": [float(command["vx"]), float(command["vy"]), float(command["wz"])],
                    },
                },
            },
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=self._last_servo_tick_ns,
            request_type=SERVO_CONTROL_REQUEST,
            response_type=SERVO_CONTROL_RESPONSE,
            timing=timing,
        )
        self._last_servo_tick_ns = self.now_ns()
        self._warn_control_result("actuator_servo.servo_control", response)
        return response, cycle

    @staticmethod
    def _servo_failure_reason(response: dict[str, Any], fallback: str) -> str:
        if not isinstance(response, dict):
            return fallback
        transport_reason = response.get("error_message") or response.get("error_code")
        if transport_reason:
            return str(transport_reason)
        payload = extract_payload(response) if response_ok(response) else None
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str) and payload["detail"]:
            return payload["detail"]
        return fallback

    def _procedure_control_period_ms(self, now_ns: int) -> float:
        if self._last_servo_tick_ns is None:
            elapsed_ms = 160.0
        else:
            elapsed_ms = (int(now_ns) - int(self._last_servo_tick_ns)) / 1_000_000.0
        return clamp(elapsed_ms, 20.0, 500.0)

    def _procedure_control_validity_ms(self, period_ms: float) -> float:
        configured_maximum_ms = min(self._limits["max_dt"] * 1000.0, 500.0)
        requested_validity_ms = max(float(period_ms) * 2.5, 200.0)
        return min(requested_validity_ms, configured_maximum_ms)

    @staticmethod
    def _control_accepted(response: dict[str, Any], cycle: dict[str, Any]) -> bool:
        if not response_ok(response):
            return False
        payload = extract_payload(response)
        return cycle_echo_matches(payload, cycle) and payload.get("status") == "applied"

    @staticmethod
    def _control_deadline_missed(response: dict[str, Any]) -> bool:
        if not isinstance(response, dict):
            return False
        transport = response.get("transport")
        if isinstance(transport, dict) and transport.get("timeout"):
            return True
        error_codes = {str(response.get("error_code") or "").casefold()}
        if isinstance(transport, dict):
            error_codes.add(str(transport.get("error") or "").casefold())
        if error_codes & {
            "expired_timed_request",
            "deadline_exceeded",
            "deadline_missed",
            "timeout",
            "timed_out",
        }:
            return True
        diagnostic_values = [response.get("error_message")]
        if isinstance(transport, dict):
            diagnostic_values.append(transport.get("error_message"))
        diagnostic = " ".join(str(value) for value in diagnostic_values if value).casefold()
        return any(
            marker in diagnostic
            for marker in (
                "expired timed request",
                "deadline exceeded",
                "deadline expired",
                "timed out",
                "read exceeded its complete-frame deadline",
                "write exceeded its complete-frame deadline",
            )
        )

    def _stop_drive(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        # A normal zero-velocity command. Its validity stays inside the
        # simulator's 50 ms window in which a zero command stops immediately.
        response, cycle = self._send_base_velocity(
            {"vx": 0.0, "vy": 0.0, "wz": 0.0},
            period_ms=20.0,
            deadline_ms=50.0,
            timing={"generated_at_sync_ns": self.now_ns(), "valid_for_ns": 50_000_000, "late_policy": "reject"},
        )
        self._last_world = extract_payload(response)
        accepted = self._control_accepted(response, cycle)
        if accepted:
            self._last_command = {"vx": 0.0, "vy": 0.0, "wz": 0.0, "dt": 0.02}
        self._publish_state(reason="stop")
        reason = "stopped" if accepted else self._servo_failure_reason(response, "servo stop command failed")
        applied_command = self._last_command
        return self._ack(
            accepted,
            reason,
            vx=float(applied_command.get("vx", 0.0)),
            vy=float(applied_command.get("vy", 0.0)),
            wz=float(applied_command.get("wz", 0.0)),
            deadline_missed=self._control_deadline_missed(response),
        )

    def _set_velocity_limits(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        self._limits = {
            "max_linear": clamp(as_float(params, "max_linear_x_mps", as_float(params, "max_linear", self._limits["max_linear"])), 0.05, 2.0),
            "max_angular": clamp(as_float(params, "max_angular_z_radps", as_float(params, "max_angular", self._limits["max_angular"])), 0.05, 4.0),
            "max_dt": clamp(as_float(params, "max_dt", self._limits["max_dt"]), 0.02, 1.0),
        }
        self._publish_state(reason="set_velocity_limits")
        return self._ack(True, "ok")

    def _get_velocity_limits(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        return self._velocity_limits_payload()

    def _get_status(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        return self._state_payload(reason="get_status")

    def _enable_drive(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        self._enabled = as_bool(params, "enable", as_bool(params, "enabled", True))
        self._publish_state(reason="enable_drive")
        return self._ack(True, "enabled" if self._enabled else "disabled")

    def _disable_drive(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._enabled = False
        self._stop_drive({})
        self._publish_state(reason="disable_drive")
        return self._ack(True, "disabled")

    def _clear_fault(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        self._faulted = False
        self._fault_detail = ""
        self._publish_fault(reason="clear_fault")
        self._publish_state(reason="clear_fault")
        return self._ack(True, "cleared")

    def _normalize_velocity_command(self, params: dict[str, Any]) -> dict[str, float]:
        body_twist = params.get("twist")
        if not isinstance(body_twist, dict):
            raise ValueError("velocity command requires msg.locomotion.velocity.v1/VelocityCommand.twist")
        linear_part = body_twist.get("linear") or {}
        angular_part = body_twist.get("angular") or {}
        vx = float(linear_part.get("x") or 0.0)
        vy = float(linear_part.get("y") or 0.0)
        wz = float(angular_part.get("z") or 0.0)
        return {
            "axis": "base",
            "vx": clamp(vx, -self._limits["max_linear"], self._limits["max_linear"]),
            "vy": clamp(vy, -self._limits["max_linear"], self._limits["max_linear"]),
            "wz": clamp(wz, -self._limits["max_angular"], self._limits["max_angular"]),
            "dt": 0.16,
        }

    def _control_period_ms(self, dt: float, context: Any = None) -> float:
        context_period = getattr(context, "period_ms", None)
        try:
            period = float(context_period)
        except (TypeError, ValueError):
            period = 0.0
        if period > 0.0:
            return period
        return max(float(dt) * 1000.0, 1.0)

    def _warn_control_result(self, label: str, response: dict[str, Any]) -> None:
        transport = response.get("transport") if isinstance(response, dict) else None
        if not isinstance(transport, dict):
            return
        if transport.get("overrun"):
            self.logger.warning("Control", f"{label} control-cycle period overrun detected")
        if transport.get("timeout"):
            self.logger.warning("Control", f"{label} transport deadline timeout")

    def _ack(
        self,
        accepted: bool,
        reason: str,
        *,
        vx: float = 0.0,
        vy: float = 0.0,
        wz: float = 0.0,
        deadline_missed: bool = False,
    ) -> dict[str, Any]:
        return {
            "accepted": bool(accepted),
            "reason": reason,
            "applied_twist": twist(linear_x=vx, linear_y=vy, angular_z=wz),
            "deadline_missed": bool(deadline_missed),
        }

    def _velocity_limits_payload(self) -> dict[str, float]:
        return {
            "max_linear_x_mps": float(self._limits["max_linear"]),
            "max_angular_z_radps": float(self._limits["max_angular"]),
        }

    def _state_payload(self, *, reason: str) -> dict[str, Any]:
        vx = float(self._last_command.get("vx", 0.0))
        vy = float(self._last_command.get("vy", 0.0))
        wz = float(self._last_command.get("wz", 0.0))
        mode = "fault" if self._faulted else ("active" if self._enabled else "disabled")
        return {
            "drive_enabled": self._enabled,
            "fault_active": self._faulted,
            "mode": {"value": mode},
            "command_age_ms": 0,
            "applied_twist": twist(linear_x=vx, linear_y=vy, angular_z=wz),
        }

    def _fault_payload(self, *, reason: str) -> dict[str, Any]:
        return {
            "fault_active": self._faulted,
            "fault_code": "tutorial_fault" if self._faulted else "",
            "fault_text": self._fault_detail or reason,
        }

    def _publish_state(self, *, reason: str) -> None:
        payload = self._state_payload(reason=reason)
        timestamp = self.now_ns()
        self.publish_event(topic="mobile_base_state", value=payload, timestamp=timestamp)
        self._publish_fault(reason=reason)
        self._publish_odometry()

    def _publish_fault(self, *, reason: str) -> None:
        payload = self._fault_payload(reason=reason)
        self.publish_event(topic="fault_state", value=payload, timestamp=self.now_ns())

    def _integrate_body_transform(self, command: dict[str, float]) -> None:
        dt = max(0.0, float(command.get("dt", 0.0)))
        vx = float(command.get("vx", 0.0)) * 0.36 * 1.015
        vy = float(command.get("vy", 0.0)) * 0.36 * 1.015
        wz = float(command.get("wz", 0.0)) * math.radians(150.0) * 0.992
        heading = self._spatial_heading
        self._spatial_x += (math.cos(heading) * vx - math.sin(heading) * vy) * dt
        self._spatial_y += (math.sin(heading) * vx + math.cos(heading) * vy) * dt
        self._spatial_heading += wz * dt
        self._report_body_transform()

    def _report_body_transform(self) -> None:
        half_yaw = self._spatial_heading * 0.5
        self.set_frame_transform(
            parent_frame="reference",
            child_frame="body",
            translation=(self._spatial_x, self._spatial_y, 0.0),
            rotation_xyzw=(0.0, 0.0, math.sin(half_yaw), math.cos(half_yaw)),
            stale_after_ms=3000,
        )

    def _publish_odometry(self) -> None:
        command = self._last_command
        self.publish_event(
            topic="odometry",
            value={
                "frame_id": "odom",
                "child_frame_id": "cleaner_base",
                "pose": pose(x=self._spatial_x, y=self._spatial_y, yaw=self._spatial_heading),
                "twist": twist(
                    linear_x=float(command.get("vx", 0.0)) * 0.36,
                    linear_y=float(command.get("vy", 0.0)) * 0.36,
                    angular_z=float(command.get("wz", 0.0)) * math.radians(150.0),
                ),
            },
            timestamp=self.now_ns(),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner mobile controller.")
    parser.add_argument("--name", default="component.mobile", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="actuator_servo", help="Required actuator gateway binding name.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = MobileControllerSubsystem(
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
