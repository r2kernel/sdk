from __future__ import annotations

import threading
import time
from typing import Any

from common import (
    APP_STATUS_TOPIC,
    WORLD_EVENT_SIZE,
    clamp,
    decode_json_text,
    encode_json_text,
    extract_payload,
    json_payload,
    response_ok,
)
from r2kernel.wrapper.subsystem import AppSubsystem
from standard_types import (
    APP_STATUS,
    BOOL,
    COMMAND_ACK,
    EMPTY,
    JSON_TEXT,
    MOVE_J_ARGS,
    NAVIGATE_TO_POSE_REQUEST,
    STOP_REQUEST,
    VELOCITY_COMMAND,
)


API_RESULT = "msg.core.common.result.v1/Result"
APP_STATUS_EVENT_MAX_BYTES = 896


class _BoundApi:
    """Small tutorial-side facade over a declared required API binding."""

    def __init__(self, subsystem: AppSubsystem, binding: str) -> None:
        self._subsystem = subsystem
        self._binding = binding

    def procedure(self, name: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self._subsystem.call_bound_procedure(
            binding=self._binding,
            procedure=name,
            params=params,
            **kwargs,
        )

    def operation(self, name: str, params: Any = None, **kwargs: Any) -> str | dict[str, Any]:
        return self._subsystem.start_bound_operation(
            binding=self._binding,
            operation=name,
            params=params,
            **kwargs,
        )


class CleanerApplicationBase(AppSubsystem):
    def __init__(
        self,
        *,
        name: str,
        log_level: str,
        user_data_path: str,
        manifest_path: str,
        deployment_manifest_path: str,
        mode: str,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path,
            manifest_path=manifest_path,
            deployment_manifest_path=deployment_manifest_path,
        )
        self._mode = mode
        self._status_lock = threading.RLock()
        self._last_action = "boot"
        self._last_result: dict[str, Any] = {}
        self._active = False
        self._body_motion = (
            self._require_api("body_motion")
            if "body_motion" in self.subsystem_manifest.requires
            else None
        )
        self._navigation = (
            self._require_api("navigation")
            if "navigation" in self.subsystem_manifest.requires
            else None
        )
        self._active_navigation_operation_id = ""
        self._trash_disposal = (
            self._require_api("trash_disposal")
            if "trash_disposal" in self.subsystem_manifest.requires
            else None
        )
        self._manipulation = (
            self._require_api("manipulation")
            if "manipulation" in self.subsystem_manifest.requires
            else None
        )
        self._gripper = (
            self._require_api("gripper")
            if "gripper" in self.subsystem_manifest.requires
            else None
        )
        self._perception = (
            self._require_api("perception")
            if "perception" in self.subsystem_manifest.requires
            else None
        )

    def _require_api(self, binding: str) -> _BoundApi:
        self.resolve_binding(binding)
        return _BoundApi(self, binding)

    def on_initialize(self) -> bool:
        self.register_event_publish(topic=APP_STATUS_TOPIC, type="bytes", size=WORLD_EVENT_SIZE, priority=5, queue_depth=4)
        self.register_procedure("status", self._procedure_status, request_type=EMPTY, response_type=APP_STATUS)
        return True

    def on_start(self) -> bool:
        self.start_event_transport()
        self.start_procedure_server()
        self._publish_status(reason="start")
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        self._safe_stop_drive()
        self.stop_procedure_server()
        self._set_status(action=f"{self._mode}.reset", active=False)
        self._publish_status(reason=f"{self._mode}.reset")
        return True

    def on_pause(self) -> bool:
        self._safe_stop_drive()
        self.stop_procedure_server()
        self._set_status(action=f"{self._mode}.pause", active=False)
        self._publish_status(reason=f"{self._mode}.pause")
        return True

    def on_resume(self) -> bool:
        self.start_event_transport()
        self.start_procedure_server()
        self._set_status(action=f"{self._mode}.resume", active=False)
        self._publish_status(reason=f"{self._mode}.resume")
        return True

    def on_stop(self) -> bool:
        self._safe_stop_drive()
        self.stop_procedure_server()
        self._set_status(action=f"{self._mode}.stop", active=False)
        self._publish_status(reason=f"{self._mode}.stop")
        return True

    def _procedure_status(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        return self._status_payload(reason="procedure.status")

    def _set_status(self, *, action: str, result: dict[str, Any] | None = None, active: bool | None = None) -> dict[str, Any]:
        with self._status_lock:
            self._last_action = action
            if result is not None:
                self._last_result = result
            if active is not None:
                self._active = active
            return self._status_payload_locked(reason=action)

    def _status_payload(self, *, reason: str) -> dict[str, Any]:
        with self._status_lock:
            return self._status_payload_locked(reason=reason)

    def _status_payload_locked(self, *, reason: str) -> dict[str, Any]:
        return {
            "kind": "robot_cleaner.app",
            "topic": APP_STATUS_TOPIC,
            "subsystem": self.name,
            "mode": self._mode,
            "active": self._active,
            "last_action": self._last_action,
            "last_result": self._last_result,
            "reason": reason,
            "ts": self.now_ns(),
        }

    def _publish_status(self, *, reason: str) -> dict[str, Any]:
        payload = self._status_payload(reason=reason)
        event_payload = json_payload(payload)
        if len(event_payload) > APP_STATUS_EVENT_MAX_BYTES:
            payload = {
                **payload,
                "last_result": {
                    "detail": "result omitted from event; call the status procedure for the full value",
                    "encoded_bytes": len(event_payload),
                },
            }
            event_payload = json_payload(payload)
        self.publish_event(topic=APP_STATUS_TOPIC, value=event_payload, timestamp=payload["ts"])
        return payload

    def _safe_stop_drive(self) -> None:
        try:
            if self._active_navigation_operation_id and self._navigation is not None:
                self.cancel_operation(
                    target=self.binding_target("navigation"),
                    operation_id=self._active_navigation_operation_id,
                    timeout_ms=1000,
                )
                self._active_navigation_operation_id = ""
            elif self._navigation is not None:
                self._navigation.procedure("cancel_navigation", {}, timeout_ms=1000, request_type=EMPTY, response_type="msg.core.common.primitive.v1/BoolValue")
            elif self._body_motion is not None:
                self._stop_drive()
        except Exception as exc:
            self._set_status(
                action="drive.stop_failed",
                result={"error": str(exc), "error_type": type(exc).__name__},
                active=True,
            )

    def _record_loop_error(self, *, action: str, exc: Exception) -> None:
        self._safe_stop_drive()
        self._set_status(
            action=action,
            result={"error": str(exc), "error_type": type(exc).__name__},
            active=True,
        )
        self._publish_status(reason=action)

    def _call(self, binding: str, procedure: str, params: dict[str, Any] | None = None, timeout_ms: int = 1000) -> Any:
        response = self.call_bound_procedure(
            binding=binding,
            procedure=procedure,
            params=params or {},
            timeout_ms=timeout_ms,
        )
        return extract_payload(response)

    def _drive(
        self,
        *,
        linear: float = 0.0,
        lateral: float = 0.0,
        angular: float = 0.0,
        vx: float | None = None,
        vy: float | None = None,
        wz: float | None = None,
    ) -> Any:
        if self._body_motion is None:
            raise RuntimeError("Application does not declare a body-motion API")
        payload = {
            "twist": {
                "linear": {
                    "x": clamp(float(linear if vx is None else vx), -1.0, 1.0),
                    "y": clamp(float(lateral if vy is None else vy), -1.0, 1.0),
                    "z": 0.0,
                },
                "angular": {
                    "x": 0.0,
                    "y": 0.0,
                    "z": clamp(float(angular if wz is None else wz), -1.0, 1.0),
                },
            },
            "source": self.name,
        }
        response = self._body_motion.procedure(
            "set_velocity_reference",
            payload,
            timeout_ms=1000,
            request_type=VELOCITY_COMMAND,
            response_type=COMMAND_ACK,
        )
        return self._api_detail(extract_payload(response))

    def _stop_drive(self) -> Any:
        if self._body_motion is None:
            return None
        return self._api_detail(
            extract_payload(
                self._body_motion.procedure(
                    "stop",
                    {},
                    timeout_ms=1000,
                    request_type=STOP_REQUEST,
                    response_type=COMMAND_ACK,
                )
            )
        )

    def _navigate_to_pose(
        self,
        *,
        frame_id: str,
        target_pose: dict[str, Any],
        should_cancel=None,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        if self._navigation is None:
            raise RuntimeError("Application does not declare a navigation API")
        operation_id = str(
            self._navigation.operation(
                "navigate_to_pose",
                {"frame_id": frame_id, "target_pose": target_pose},
                timeout_ms=1500,
                request_type=NAVIGATE_TO_POSE_REQUEST,
            )
        )
        self._active_navigation_operation_id = operation_id
        target = self.binding_target("navigation")
        deadline = time.monotonic() + max(timeout_s, 1.0)
        try:
            while time.monotonic() < deadline:
                if should_cancel is not None and should_cancel():
                    self.cancel_operation(target=target, operation_id=operation_id, timeout_ms=1000)
                query = self.query_operation(target=target, operation_id=operation_id, timeout_ms=1000)
                if query.get("ok") is False:
                    return {
                        "completed": False,
                        "state": "failed",
                        "operation_id": operation_id,
                        "detail": str(query.get("error_message") or "navigation operation query failed"),
                    }
                state = str(query.get("state") or "unknown")
                if state in {"succeeded", "failed", "canceled", "rejected"}:
                    result = query.get("result")
                    if isinstance(result, dict):
                        return {**result, "state": state, "operation_id": operation_id}
                    return {
                        "completed": state == "succeeded",
                        "state": state,
                        "operation_id": operation_id,
                        "detail": str(query.get("error_message") or state),
                    }
                time.sleep(0.10)
            self.cancel_operation(target=target, operation_id=operation_id, timeout_ms=1000)
            return {"completed": False, "state": "timeout", "operation_id": operation_id, "detail": "navigation timeout"}
        finally:
            self._active_navigation_operation_id = ""

    def _scan(self, *, max_range: float = 0.70) -> dict[str, Any]:
        if self._perception is None:
            raise RuntimeError("Application does not declare a perception API")
        result = extract_payload(
            self._perception.procedure(
                "scan",
                encode_json_text(
                    {
                        "mode": "radar",
                        "max_range": max_range,
                        "fov_deg": 30.0,
                        "visualize": True,
                    }
                ),
                timeout_ms=2000,
                request_type=JSON_TEXT,
                response_type=JSON_TEXT,
            )
        )
        result, _ = decode_json_text(result)
        if not isinstance(result, dict):
            return {"detections": []}
        detections = result.get("detections")
        return {
            "detections": detections if isinstance(detections, list) else [],
            "source_frame_id": str(result.get("source_frame_id") or "cleaner_base"),
        }

    def _open_gripper(self) -> Any:
        if self._gripper is None:
            raise RuntimeError("Application does not declare a gripper API")
        return self._api_detail(
            extract_payload(
                self._gripper.procedure(
                    "open", {}, timeout_ms=1000, request_type=EMPTY, response_type=API_RESULT
                )
            )
        )

    def _close_gripper(self) -> Any:
        if self._gripper is None:
            raise RuntimeError("Application does not declare a gripper API")
        return self._api_detail(
            extract_payload(
                self._gripper.procedure(
                    "close", {}, timeout_ms=1000, request_type=EMPTY, response_type=API_RESULT
                )
            )
        )

    def _extend_arm(self, extension: float = 1.0) -> Any:
        return self._move_arm(extension)

    def _shrink_arm(self, extension: float = 0.0) -> Any:
        return self._move_arm(extension)

    def _stow_arm(self) -> Any:
        return self._move_arm(0.0)

    def _move_arm(self, extension: float) -> Any:
        if self._manipulation is None:
            raise RuntimeError("Application does not declare a manipulation API")
        target_extension = max(0.0, min(float(extension), 1.0))
        started = self._manipulation.operation(
            "move_j",
            {
                "waypoints": [
                    {
                        "q": {"names": ["arm_extension"], "values": [target_extension]},
                        "radius": 0.0,
                        "vel": 1.0,
                        "cornering_vel": 0.0,
                        "acc": 1.0,
                    }
                ],
                "finish_mode": "stop",
            },
            timeout_ms=1500,
            request_type=MOVE_J_ARGS,
            return_details=True,
        )
        operation_id = str(started if isinstance(started, str) else (started or {}).get("operation_id") or "")
        if not operation_id:
            raise RuntimeError("move_j did not return an operation id")
        target = self.binding_target("manipulation")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            query = self.query_operation(target=target, operation_id=operation_id, timeout_ms=1000)
            if query.get("ok") is False:
                detail = query.get("error_message") or query.get("error_code") or query
                raise RuntimeError(f"move_j query failed for {operation_id}: {detail}")
            state = str(query.get("state") or "unknown")
            if state == "succeeded":
                result = query.get("result")
                return result if isinstance(result, dict) else {"completed": True, "extension": target_extension}
            if state in {"failed", "canceled", "rejected"}:
                raise RuntimeError(str(query.get("error_message") or f"move_j {state}"))
            time.sleep(0.02)
        self.cancel_operation(target=target, operation_id=operation_id, timeout_ms=1000)
        raise RuntimeError("move_j timed out")

    def _burn(self) -> Any:
        if self._trash_disposal is None:
            raise RuntimeError("Application does not declare a trigger-actuation API")
        response = self._trash_disposal.procedure(
            "trigger", {}, timeout_ms=1500, request_type=EMPTY, response_type=BOOL
        )
        if not response_ok(response):
            transport = response.get("transport") if isinstance(response, dict) else None
            message = (
                response.get("error_message") or response.get("error_code")
                or (transport.get("error") if isinstance(transport, dict) else None)
                if isinstance(response, dict) else None
            )
            raise RuntimeError(str(message or "trigger Procedure failed"))
        result = extract_payload(response)
        if not isinstance(result, dict) or not isinstance(result.get("value"), bool):
            raise RuntimeError("trigger Procedure returned no boolean value")
        return {"fired": result["value"]}

    @staticmethod
    def _api_detail(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        detail = result.get("detail")
        if not isinstance(detail, (bytes, bytearray)):
            return result
        import json

        try:
            return json.loads(bytes(detail).decode("utf-8"))
        except Exception:
            return result
