from __future__ import annotations

from typing import Any

EMPTY = "msg.core.common.primitive.v1/Empty"
BOOL = "msg.core.common.primitive.v1/BoolValue"
UINT32 = "msg.core.common.primitive.v1/UInt32Value"
INDEXED_BOOL = "msg.core.common.primitive.v1/IndexedBoolValue"
JSON_TEXT = "msg.core.common.primitive.v1/StringValue"
APP_STATUS = "bytes"

SERVO_CONTROL_REQUEST = "msg.actuator.servo.v1/ServoControlRequest"
SERVO_CONTROL_RESPONSE = "msg.actuator.servo.v1/ServoControlResponse"
TIMED_OUTPUT_CONTROL_REQUEST = "msg.io.digital.timed_output.v1/TimedOutputControlRequest"
TIMED_OUTPUT_CONTROL_RESPONSE = "msg.io.digital.timed_output.v1/TimedOutputControlResponse"
JOINT_VELOCITY = "msg.core.kinematics.v1/JointVelocity"
JOINT_STATE = "msg.core.kinematics.v1/JointState"
POSE = "msg.core.spatial.v1/Pose"
ABSOLUTE_POSE_FIX = "msg.positioning.v1/AbsolutePoseFix"
ODOMETRY = "msg.locomotion.mobile_base.v2/Odometry"
POSE_ESTIMATE = "msg.localization.v1/PoseEstimate"
LOCALIZATION_STATUS = "msg.localization.v1/LocalizationStatus"
NAVIGATE_TO_POSE_REQUEST = "msg.navigation.v1/NavigateToPoseRequest"
FOLLOW_PATH_REQUEST = "msg.navigation.v1/FollowPathRequest"
NAVIGATION_FEEDBACK = "msg.navigation.v1/NavigationFeedback"
NAVIGATION_RESULT = "msg.navigation.v1/NavigationResult"
NAVIGATION_STATE = "msg.navigation.v1/NavigationState"

VELOCITY_COMMAND = "msg.locomotion.velocity.v1/VelocityCommand"
COMMAND_ACK = "msg.locomotion.velocity.v1/CommandAck"
VELOCITY_LIMITS = "msg.locomotion.velocity.v1/VelocityLimits"
STOP_REQUEST = "msg.locomotion.velocity.v1/StopRequest"
MOBILE_BASE_STATE = "msg.locomotion.mobile_base.v1/MobileBaseState"
FAULT_STATE = "msg.locomotion.mobile_base.v1/FaultState"
ENABLE_DRIVE_REQUEST = "msg.locomotion.mobile_base.v1/EnableDriveRequest"
DISABLE_DRIVE_REQUEST = "msg.locomotion.mobile_base.v1/DisableDriveRequest"
CLEAR_FAULT_REQUEST = "msg.locomotion.mobile_base.v1/ClearFaultRequest"

MOTION_FEEDBACK = "msg.motion.manipulator.v1/MotionFeedback"
MOTION_RESULT = "msg.motion.manipulator.v1/MotionResult"
MOTION_STATE = "msg.motion.manipulator.v1/MotionState"
TOOL_SETTINGS = "msg.motion.manipulator.v1/ToolSettings"
MOVE_J_ARGS = "msg.motion.articulated.v1/MoveJRequest"
MOVE_L_ARGS = "msg.motion.manipulator.v1/MoveLRequest"
MOVE_C_ARGS = "msg.motion.manipulator.v1/MoveCRequest"
STOP_ARGS = "msg.motion.articulated.v1/RampRequest"
PAUSE_ARGS = "msg.motion.articulated.v1/RampRequest"
SET_TCP_ARGS = "msg.motion.manipulator.v1/SetTcpRequest"
SET_PAYLOAD_ARGS = "msg.motion.manipulator.v1/SetPayloadRequest"


def vector3(*, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, float]:
    return {"x": float(x), "y": float(y), "z": float(z)}


def twist(
    *,
    linear_x: float = 0.0,
    linear_y: float = 0.0,
    angular_z: float = 0.0,
) -> dict[str, Any]:
    return {
        "linear": vector3(x=linear_x, y=linear_y),
        "angular": vector3(z=angular_z),
    }


def pose(*, x: float = 0.0, y: float = 0.0, z: float = 0.0, yaw: float = 0.0) -> dict[str, Any]:
    import math

    half_yaw = float(yaw) * 0.5
    return {
        "position": vector3(x=x, y=y, z=z),
        "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(half_yaw), "w": math.cos(half_yaw)},
    }


def covariance6(*, x: float, y: float, yaw: float) -> dict[str, list[float]]:
    values = [0.0] * 36
    values[0] = float(x)
    values[7] = float(y)
    values[14] = 1.0
    values[21] = 1.0
    values[28] = 1.0
    values[35] = float(yaw)
    return {"values": values}


def empty_tool_settings() -> dict[str, Any]:
    return {
        "payload": {
            "mass": 0.0,
            "center_of_mass": vector3(),
            "inertia": {"ixx": 0.0, "iyy": 0.0, "izz": 0.0, "ixy": 0.0, "ixz": 0.0, "iyz": 0.0},
        },
        "tcp": {"pose": pose()},
    }
