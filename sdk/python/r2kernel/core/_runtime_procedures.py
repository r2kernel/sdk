"""SubsystemRuntime procedure/operation registration, servers and procedure calls."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .codec_lifecycle import codec_activity
from .timing import (
    compact_request_id as _compact_request_id,
    compact_response_header as _compact_response_header,
)
from ._runtime_common import _OPERATION_TERMINAL_STATES


_OPERATION_SHUTDOWN_GRACE_S = 1.0


def _procedure_transport_failure(result: dict[str, Any], default_message: str) -> tuple[str, str]:
    """The documented error code and a message for a failed Procedure exchange.

    As in the C++ SDK, no reply before the deadline is ``timeout`` and a reply
    with a nonzero Control channel status is ``procedure_transport_failed``;
    the transport's own reason stays in the message (cross-audit SDK-112-02).
    """
    response = result.get("response") or {}
    payload = response.get("payload") or b""
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else ""
    code = "timeout" if result.get("timeout") or not response else "procedure_transport_failed"
    return code, text or str(result.get("error") or default_message)


class _ProcedureMixin:
    """Procedure/operation registration, server lifecycle and procedure client calls of SubsystemRuntime."""

    def _register_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        procedure_name = str(name).strip()
        if not procedure_name:
            raise ValueError("procedure name is required")
        timing_support = self._normalize_timing_support(timing_support)
        self._ensure_endpoint_plane_available(procedure_name, plane="procedure")
        self._procedure_handlers[procedure_name] = handler
        if request_type is not None or response_type is not None or timing_support != "none":
            self._procedure_contracts[procedure_name] = {
                "request_type": request_type,
                "response_type": response_type,
                "timing_support": timing_support,
            }
        else:
            self._procedure_contracts.pop(procedure_name, None)

    def register_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        self._register_procedure(
            name,
            handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def _register_typed_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str,
        response_type: str,
        timing_support: str = "none",
    ) -> None:
        procedure_name = str(name).strip()
        if not procedure_name:
            raise ValueError("procedure name is required")
        if not str(request_type).strip() or not str(response_type).strip():
            raise ValueError("typed procedure requires request_type and response_type")
        timing_support = self._normalize_timing_support(timing_support)
        self._ensure_endpoint_plane_available(procedure_name, plane="procedure")
        self._typed_procedure_handlers[procedure_name] = handler
        self._procedure_contracts[procedure_name] = {
            "request_type": str(request_type).strip(),
            "response_type": str(response_type).strip(),
            "timing_support": timing_support,
        }

    def register_typed_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str,
        response_type: str,
        timing_support: str = "none",
    ) -> None:
        self._register_typed_procedure(
            name,
            handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def _get_procedure_info(self) -> list[str]:
        return sorted(set(self._procedure_handlers.keys()) | set(self._typed_procedure_handlers.keys()))

    def get_procedure_info(self) -> list[str]:
        return self._get_procedure_info()

    def _get_procedure_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return sorted(self._procedure_contracts.items())

    def get_procedure_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._get_procedure_contract_info()

    def _get_operation_info(self) -> list[str]:
        return sorted(self._operation_handlers.keys())

    def get_operation_info(self) -> list[str]:
        return self._get_operation_info()

    def _get_operation_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return sorted(self._operation_contracts.items())

    def get_operation_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._get_operation_contract_info()

    def _register_operation(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        feedback_type: str | None = None,
        result_type: str | None = None,
        state_type: str | None = "string",
        timing_support: str = "none",
    ) -> None:
        operation_name = str(name).strip()
        if not operation_name:
            raise ValueError("operation name is required")
        timing_support = self._normalize_timing_support(timing_support)
        self._ensure_endpoint_plane_available(operation_name, plane="operation")
        self._operation_handlers[operation_name] = handler
        if any(value is not None for value in (request_type, feedback_type, result_type, state_type)):
            self._operation_contracts[operation_name] = {
                "request_type": request_type,
                "feedback_type": feedback_type,
                "result_type": result_type,
                "state_type": state_type,
                "timing_support": timing_support,
            }
        else:
            if timing_support != "none":
                self._operation_contracts[operation_name] = {
                    "request_type": request_type,
                    "feedback_type": feedback_type,
                    "result_type": result_type,
                    "state_type": state_type,
                    "timing_support": timing_support,
                }
            else:
                self._operation_contracts.pop(operation_name, None)

    def register_operation(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        feedback_type: str | None = None,
        result_type: str | None = None,
        state_type: str | None = "string",
        timing_support: str = "none",
    ) -> None:
        self._register_operation(
            name,
            handler,
            request_type=request_type,
            feedback_type=feedback_type,
            result_type=result_type,
            state_type=state_type,
            timing_support=timing_support,
        )

    def _start_procedure_server(self) -> None:
        self._procedure_server_enabled = True
        if self._get_control_port() is None:
            self._bind_control(port=self._allocate_control_port(), host="0.0.0.0")
        self._ensure_control_dispatch_server()

    def start_procedure_server(self) -> None:
        self._start_procedure_server()

    def _stop_procedure_server(self) -> None:
        self._procedure_server_enabled = False
        if not self._control_server_enabled and not self._operation_server_enabled:
            if getattr(self._procedure_handler_context, "active", False):
                self._schedule_control_dispatch_stop()
            else:
                self._stop_control_dispatch_server()

    def stop_procedure_server(self) -> None:
        self._stop_procedure_server()

    def _start_operation_server(self) -> None:
        self._operation_server_enabled = True
        if self._get_control_port() is None:
            self._bind_control(port=self._allocate_control_port(), host="0.0.0.0")
        self._ensure_control_dispatch_server()
        self._ensure_operation_executor()

    def start_operation_server(self) -> None:
        self._start_operation_server()

    def _stop_operation_server(self) -> None:
        self._operation_server_enabled = False
        self._wait_for_operation_quiescence(timeout_s=_OPERATION_SHUTDOWN_GRACE_S)
        cancel_operation_ids: list[str] = []
        with self._operation_lock:
            now_ns = self.now_ns()
            for record in self._operation_records.values():
                if record.state not in _OPERATION_TERMINAL_STATES and not record.cancel_requested:
                    record.cancel_requested = True
                    record.updated_ts = now_ns
                    cancel_operation_ids.append(record.operation_id)
        for operation_id in cancel_operation_ids:
            self._publish_operation_state(operation_id=operation_id, state="cancel_requested", payload=None)
        self._wait_for_operation_quiescence(timeout_s=0.25)
        self._stop_operation_executor()
        if not self._control_server_enabled and not self._procedure_server_enabled:
            self._stop_control_dispatch_server()

    def stop_operation_server(self) -> None:
        self._stop_operation_server()

    @codec_activity
    def _call_procedure(
        self,
        *,
        target: str,
        procedure: str,
        params: Any = None,
        timeout_ms: int = 1000,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(timeout_ms)
        if timing is not None:
            if not request_type or not str(request_type).strip() or not response_type or not str(response_type).strip():
                raise ValueError("timed procedure call requires both request_type and response_type")
            if params is None:
                payload = {}
            elif isinstance(params, dict):
                payload = dict(params)
            else:
                raise ValueError("timed procedure call requires params to be a dict payload")
            timing_options = dict(timing)
            return self._call_timed_procedure(
                target=target,
                procedure=procedure,
                payload_type=str(request_type).strip(),
                response_type=str(response_type).strip(),
                payload=payload,
                execute_after_ns=timing_options.get("execute_after_ns"),
                execute_at_sync_ns=timing_options.get("execute_at_sync_ns"),
                priority=timing_options.get("priority", 128),
                late_policy=timing_options.get("late_policy", "execute_immediately"),
                max_lateness_ns=timing_options.get("max_lateness_ns", 1_000_000),
                timeout_ms=timeout_ms,
                correlation_id=timing_options.get("correlation_id"),
                request_id=timing_options.get("request_id"),
                generated_at_sync_ns=timing_options.get("generated_at_sync_ns"),
                valid_for_ns=timing_options.get("valid_for_ns"),
                valid_until_sync_ns=timing_options.get("valid_until_sync_ns"),
                **({"_deadline": deadline} if deadline is not None else {}),
            )
        if request_type is not None or response_type is not None:
            if not str(request_type or "").strip() or not str(response_type or "").strip():
                raise ValueError("typed procedure call requires both request_type and response_type")
            if params is None:
                payload = {}
            elif isinstance(params, dict):
                payload = dict(params)
            else:
                raise ValueError("typed procedure call requires params to be a dict payload")
            return self.call_typed_procedure(
                target=target,
                procedure=procedure,
                request_type=str(request_type).strip(),
                response_type=str(response_type).strip(),
                params=payload,
                timeout_ms=timeout_ms,
                **({"_deadline": deadline} if deadline is not None else {}),
            )
        try:
            self._prepare_control_target(
                target, procedure=procedure, **({"deadline": deadline} if deadline is not None else {}),
            )
        except RuntimeError as exc:
            return {
                "ok": False,
                "request_id": "",
                "error_code": "not_found",
                "error_message": str(exc),
            }
        request_id = uuid.uuid4().hex
        payload = {
            "kind": "procedure_request",
            "target": target,
            "procedure": procedure,
            "request_id": request_id,
            "timeout_ms": int(timeout_ms),
            "params": params,
        }
        result = self._transact_control_packet(
            target=target,
            topic="procedure.call",
            type="bytes",
            value=json.dumps(payload).encode("utf-8"),
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms, 1)),
            last_tick_ns=None,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            error_code, error_message = _procedure_transport_failure(result, "procedure transport failed")
            return {
                "ok": False,
                "request_id": request_id,
                "error_code": error_code,
                "error_message": error_message,
            }

        response = result.get("response") or {}
        response_payload = response.get("payload") or b""
        if not isinstance(response_payload, (bytes, bytearray)):
            response_payload = bytes(response_payload)
        try:
            decoded = json.loads(response_payload.decode("utf-8")) if response_payload else {}
        except Exception as exc:
            return {
                "ok": False,
                "request_id": request_id,
                "error_code": "invalid_response",
                "error_message": f"failed to decode procedure response: {exc}",
            }
        if not isinstance(decoded, dict):
            return {
                "ok": False,
                "request_id": request_id,
                "error_code": "invalid_response",
                "error_message": "procedure response is not an object",
            }
        decoded.setdefault("request_id", request_id)
        decoded.setdefault("ok", False)
        if decoded.get("ok"):
            self._check_control_deadline(deadline)
        return decoded

    def call_procedure(
        self,
        *,
        target: str,
        procedure: str,
        params: Any = None,
        timeout_ms: int = 1000,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(timeout_ms)
        return self._call_procedure(
            target=target,
            procedure=procedure,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            response_type=response_type,
            timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def call_typed_procedure(
        self,
        *,
        target: str,
        procedure: str,
        request_type: str,
        response_type: str,
        params: dict[str, Any],
        timeout_ms: int = 1000,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(timeout_ms)
        try:
            self._prepare_control_target(
                target, procedure=procedure, **({"deadline": deadline} if deadline is not None else {}),
            )
        except RuntimeError as exc:
            return {
                "ok": False,
                "error_code": "not_found",
                "error_message": str(exc),
            }

        codec = self._get_rust_message_codec()
        request_payload = codec.encode_message(request_type, params)
        result = self._transact_control_packet(
            target=target,
            topic=self._typed_procedure_topic(procedure),
            type="bytes",
            value=request_payload,
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms, 1)),
            last_tick_ns=None,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            error_code, error_message = _procedure_transport_failure(result, "typed procedure transport failed")
            return {
                "ok": False,
                "error_code": error_code,
                "error_message": error_message,
            }

        response = result.get("response") or {}
        payload = response.get("payload") or b""
        if not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        decoded = codec.decode_message(response_type, payload)
        self._check_control_deadline(deadline)
        return {
            "ok": True,
            "result": decoded,
        }

    def _call_timed_procedure(
        self,
        *,
        target: str,
        procedure: str,
        payload_type: str,
        response_type: str,
        payload: dict[str, Any],
        execute_after_ns: int | None = None,
        execute_at_sync_ns: int | None = None,
        priority: int | None = 128,
        late_policy: str | None = "execute_immediately",
        max_lateness_ns: int | None = 1_000_000,
        timeout_ms: int = 1000,
        correlation_id: str | None = None,
        request_id: str | None = None,
        generated_at_sync_ns: int | None = None,
        valid_for_ns: int | None = None,
        valid_until_sync_ns: int | None = None,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(timeout_ms)
        try:
            self._prepare_control_target(
                target, procedure=procedure, **({"deadline": deadline} if deadline is not None else {}),
            )
        except RuntimeError as exc:
            return {
                "ok": False,
                "error_code": "not_found",
                "error_message": str(exc),
            }

        user_header = self.build_timing_request_header(
            target_subsystem=target,
            execute_after_ns=execute_after_ns,
            execute_at_sync_ns=execute_at_sync_ns,
            priority=priority,
            late_policy=late_policy,
            max_lateness_ns=max_lateness_ns,
            correlation_id=correlation_id,
            request_id=request_id,
            generated_at_sync_ns=generated_at_sync_ns,
            valid_for_ns=valid_for_ns,
            valid_until_sync_ns=valid_until_sync_ns,
        )
        normalized_header, accepted_at_sync_ns = self.normalize_timing_request_header(user_header)
        codec = self._get_rust_message_codec()
        request_payload = codec.encode_message(payload_type, payload)
        request_id_u64, request_id_text = _compact_request_id(request_id or user_header.get("request_id"))
        priority_value = int(priority if priority is not None else 128)
        max_lateness_value = None if max_lateness_ns is None else int(max_lateness_ns)
        sent_at_sync_ns = accepted_at_sync_ns
        execute_at_value = int(
            execute_at_sync_ns
            if execute_at_sync_ns is not None
            else sent_at_sync_ns + int(execute_after_ns or 0)
        )
        wire = codec.encode_control_request_wire(
            request_id=request_id_u64,
            generated_at_sync_ns=int(user_header["generated_at_sync_ns"]),
            sent_at_sync_ns=sent_at_sync_ns,
            execute_at_sync_ns=execute_at_value,
            valid_until_sync_ns=(normalized_header.get("execution") or {}).get("valid_until_sync_ns"),
            max_lateness_ns=max_lateness_value,
            priority=priority_value,
            late_policy=late_policy,
            payload=request_payload,
        )
        result = self._transact_control_packet(
            target=target,
            topic=self._typed_procedure_topic(procedure),
            type="bytes",
            value=wire,
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms, 1)),
            last_tick_ns=self.now_ns(),
            priority=priority_value,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        summary: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "target": target,
            "procedure": procedure,
            "payload_type": str(payload_type),
            "response_type": str(response_type),
            "payload": dict(payload),
            "user_header": user_header,
            "normalized_header": normalized_header,
            "runtime_accepted_at_sync_ns": accepted_at_sync_ns,
        }
        if not result.get("ok"):
            self._invalidate_control_target(target)
            summary["error_code"], summary["error_message"] = _procedure_transport_failure(
                result, "typed timed procedure transport failed",
            )
            summary["procedure_response"] = {
                "ok": False,
                "error_code": summary["error_code"],
                "error_message": summary["error_message"],
            }
            return summary

        response = result.get("response") or {}
        payload_bytes = response.get("payload") or b""
        if not isinstance(payload_bytes, (bytes, bytearray)):
            payload_bytes = bytes(payload_bytes)
        try:
            response_meta, response_payload_bytes = codec.decode_control_response_wire(bytes(payload_bytes))
            response_payload = codec.decode_message(response_type, payload=response_payload_bytes)
            response_header = _compact_response_header(
                request_id_u64=response_meta["request_id_u64"],
                responded_at_sync_ns=response_meta["responded_at_sync_ns"],
                status_code=response_meta["status_code"],
                accepted_at_sync_ns=response_meta["accepted_at_sync_ns"],
                scheduled_for_sync_ns=response_meta["scheduled_for_sync_ns"],
                dispatched_at_sync_ns=response_meta["dispatched_at_sync_ns"],
                completed_at_sync_ns=response_meta["completed_at_sync_ns"],
                lateness_ns=response_meta["lateness_ns"],
                final_status=response_meta["final_status"],
            )
            summary["timing_response"] = {
                "response_header": response_header,
                "payload_ref": response_type,
                "payload": response_payload,
            }
            summary["procedure_response"] = {
                "ok": response_meta["status_code"] == 0,
                "result": response_payload,
            }
            summary["ok"] = response_meta["status_code"] == 0
            if response_meta["status_code"] != 0:
                summary["error_code"] = "procedure_response_error"
                summary["error_message"] = f"timed procedure response status={response_meta['final_status']}"
            else:
                summary["result"] = response_payload
        except Exception as exc:
            summary["ok"] = False
            summary["error_code"] = "invalid_response"
            summary["error_message"] = f"failed to decode timed procedure response: {exc}"
            summary["procedure_response"] = {
                "ok": False,
                "error_code": summary["error_code"],
                "error_message": summary["error_message"],
            }
        if summary.get("ok"):
            self._check_control_deadline(deadline)
        return summary
