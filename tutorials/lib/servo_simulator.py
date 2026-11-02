"""Map ``msg.actuator.servo.v1`` payloads onto the tutorial simulator axes."""

from __future__ import annotations

import math
from typing import Any

BASE_VELOCITY_JOINTS = ("base_vx", "base_vy", "base_wz")


def servo_target_to_simulator_command(
    target: Any,
    *,
    duration_s: float,
    immediate_stop: bool,
) -> dict[str, Any] | None:
    """Translate a JointCommandPrecise target into one simulator axis command.

    Base joints are velocity commands held for ``duration_s``; the arm extension
    and gripper joints are position commands. Returns ``None`` when the target
    does not identify a supported simulator axis.
    """
    if not isinstance(target, dict):
        return None
    names = [str(name) for name in _list(target.get("names"))]
    name_text = " ".join(names).lower()
    if "base" in name_text or "linear" in name_text or "angular" in name_text:
        values = _finite_values(target.get("target_velocity"), len(names))
        if values is None:
            return None
        value_by_name = {name.lower(): values[index] for index, name in enumerate(names)}
        if any(name in value_by_name for name in BASE_VELOCITY_JOINTS):
            vx = value_by_name.get("base_vx", 0.0)
            vy = value_by_name.get("base_vy", 0.0)
            wz = value_by_name.get("base_wz", 0.0)
        else:
            vx = value_by_name.get("base_linear", values[0] if values else 0.0)
            vy = 0.0
            wz = value_by_name.get("base_angular", values[1] if len(values) > 1 else 0.0)
        return {
            "axis": "base",
            "vx": vx,
            "vy": vy,
            "wz": wz,
            "dt": float(duration_s),
            "immediate_stop": bool(immediate_stop),
        }
    if "arm" in name_text or "extension" in name_text:
        values = _finite_values(target.get("target_position"), len(names))
        if not values:
            return None
        return {"axis": "arm", "extension": values[0]}
    if "gripper" in name_text or "tool" in name_text:
        values = _finite_values(target.get("target_position"), len(names))
        if not values:
            return None
        return {"axis": "gripper", "open": values[0] > 0.5}
    return None


def actuator_state_from_simulator(
    simulator_command: dict[str, Any],
    simulator_result: Any,
    *,
    servo_on: bool,
) -> dict[str, Any]:
    """Build an ActuatorState from the simulator snapshot after one command."""
    snapshot = simulator_result.get("state") if isinstance(simulator_result, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = simulator_result if isinstance(simulator_result, dict) else {}
    robot = snapshot.get("robot") if isinstance(snapshot.get("robot"), dict) else {}
    axis = simulator_command.get("axis")
    if axis == "base":
        joint_state = {
            "names": list(BASE_VELOCITY_JOINTS),
            "position": [],
            "velocity": [_number(robot.get(field)) for field in ("vx", "vy", "wz")],
        }
    elif axis == "arm":
        joint_state = {"names": ["arm_extension"], "position": [_number(robot.get("arm_extension"))]}
    else:
        joint_state = {"names": ["gripper"], "position": [1.0 if robot.get("gripper_open") else 0.0]}
    link_alive = not (isinstance(simulator_result, dict) and simulator_result.get("ok") is False)
    return {"link_alive": link_alive, "servo_on": bool(servo_on), "state": joint_state}


def servo_control_response(
    cycle: Any,
    status: str,
    *,
    state: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a ServoControlResponse echoing the request ControlCycle."""
    response: dict[str, Any] = {"cycle": cycle.to_mapping(), "status": status}
    if state is not None:
        response["state"] = state
    if detail:
        response["detail"] = detail
    return response


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _finite_values(value: Any, expected_count: int) -> list[float] | None:
    values = _list(value)
    if len(values) != expected_count:
        return None
    result: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            return None
        result.append(float(item))
    return result


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0.0
    return float(value)
