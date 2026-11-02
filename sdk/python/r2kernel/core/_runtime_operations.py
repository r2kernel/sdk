"""SubsystemRuntime Operation client: start, cancel, query and subscriptions."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .codec_lifecycle import codec_activity
from .timing import compact_request_id as _compact_request_id
from ._runtime_common import OPERATION_EVENT_SIZE, _TYPED_OPERATION_CANCEL_TOPIC, _TYPED_OPERATION_QUERY_TOPIC


class _OperationClientMixin:
    """Operation start/cancel/query and feedback/result/state subscriptions of SubsystemRuntime."""

    @codec_activity
    def _start_operation(
        self,
        *,
        target: str,
        operation: str,
        params: Any = None,
        timeout_ms: int | None = None,
        request_type: str | None = None,
        timing: dict[str, Any] | None = None,
        return_details: bool = False,
        _deadline: float | None = None,
    ) -> str | dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(
            timeout_ms if timeout_ms is not None else 1000,
        )
        if timing is not None and request_type is None:
            if params is None:
                payload = {}
            elif isinstance(params, dict):
                payload = dict(params)
            else:
                raise ValueError("timed operation start requires params to be a dict payload")
            details = self._start_timed_operation(
                target=target,
                operation=operation,
                payload=payload,
                timeout_ms=timeout_ms,
                timing=timing,
                **({"_deadline": deadline} if deadline is not None else {}),
            )
            if return_details:
                return details
            return str(details["operation_id"])
        if request_type is not None:
            if not str(request_type).strip():
                raise ValueError("typed operation start requires request_type")
            if params is None:
                payload = {}
            elif isinstance(params, dict):
                payload = dict(params)
            else:
                raise ValueError("typed operation start requires params to be a dict payload")
            details = self._start_typed_operation(
                target=target,
                operation=operation,
                request_type=str(request_type).strip(),
                payload=payload,
                timeout_ms=timeout_ms,
                timing=timing,
                **({"_deadline": deadline} if deadline is not None else {}),
            )
            if return_details:
                return details
            return str(details["operation_id"])
        self._prepare_control_target(
            target, operation=operation, **({"deadline": deadline} if deadline is not None else {}),
        )
        request_id = uuid.uuid4().hex
        payload = {
            "kind": "operation_start_request",
            "target": target,
            "operation": operation,
            "request_id": request_id,
            "timeout_ms": int(timeout_ms) if timeout_ms is not None else None,
            "params": params,
        }
        result = self._transact_control_packet(
            target=target,
            topic="operation.start",
            type="bytes",
            value=json.dumps(payload).encode("utf-8"),
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms or 1000, 1)),
            last_tick_ns=None,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            raise RuntimeError(str(result.get("error") or "operation transport failed"))
        decoded = self._decode_json_control_payload(result, request_id=request_id, kind="operation start")
        if decoded.get("ok"):
            self._check_control_deadline(deadline)
        if not decoded.get("ok") or not decoded.get("accepted"):
            error_message = decoded.get("error_message") or "operation start rejected"
            raise RuntimeError(str(error_message))
        operation_id = str(decoded.get("operation_id") or "")
        if not operation_id:
            raise RuntimeError("operation start response did not include an operation_id")
        return operation_id

    def start_operation(
        self,
        *,
        target: str,
        operation: str,
        params: Any = None,
        timeout_ms: int | None = None,
        request_type: str | None = None,
        timing: dict[str, Any] | None = None,
        return_details: bool = False,
        _deadline: float | None = None,
    ) -> str | dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(
            timeout_ms if timeout_ms is not None else 1000,
        )
        return self._start_operation(
            target=target,
            operation=operation,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            timing=timing,
            return_details=return_details,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def _start_typed_operation(
        self,
        *,
        target: str,
        operation: str,
        request_type: str,
        payload: dict[str, Any],
        timeout_ms: int | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(
            timeout_ms if timeout_ms is not None else 1000,
        )
        self._prepare_control_target(
            target, operation=operation, **({"deadline": deadline} if deadline is not None else {}),
        )

        codec = self._get_rust_message_codec()
        request_payload = codec.encode_message(request_type, payload)

        user_header: dict[str, Any] | None = None
        normalized_header: dict[str, Any] | None = None
        accepted_at_sync_ns: int | None = None
        wire_payload = request_payload
        priority_value: int | None = None
        if timing is not None:
            timing_options = dict(timing)
            user_header = self.build_timing_request_header(
                target_subsystem=target,
                execute_after_ns=timing_options.get("execute_after_ns"),
                execute_at_sync_ns=timing_options.get("execute_at_sync_ns"),
                priority=timing_options.get("priority", 128),
                late_policy=timing_options.get("late_policy", "execute_immediately"),
                max_lateness_ns=timing_options.get("max_lateness_ns", 1_000_000),
                correlation_id=timing_options.get("correlation_id"),
                request_id=timing_options.get("request_id"),
                generated_at_sync_ns=timing_options.get("generated_at_sync_ns"),
                valid_for_ns=timing_options.get("valid_for_ns"),
                valid_until_sync_ns=timing_options.get("valid_until_sync_ns"),
            )
            normalized_header, accepted_at_sync_ns = self.normalize_timing_request_header(user_header)
            request_id_u64, _ = _compact_request_id(user_header.get("request_id"))
            sent_at_sync_ns = accepted_at_sync_ns
            priority_value = int(timing_options.get("priority", 128))
            wire_payload = codec.encode_control_request_wire(
                request_id=request_id_u64,
                generated_at_sync_ns=int(user_header["generated_at_sync_ns"]),
                sent_at_sync_ns=sent_at_sync_ns,
                execute_at_sync_ns=int((normalized_header.get("execution") or {}).get("execute_at_sync_ns", sent_at_sync_ns)),
                valid_until_sync_ns=(normalized_header.get("execution") or {}).get("valid_until_sync_ns"),
                max_lateness_ns=(normalized_header.get("execution") or {}).get("max_lateness_ns"),
                priority=priority_value,
                late_policy=(normalized_header.get("execution") or {}).get("late_policy"),
                payload=request_payload,
            )

        result = self._transact_control_packet(
            target=target,
            topic=self._typed_operation_start_topic(operation),
            type="bytes",
            value=wire_payload,
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms or 1000, 1)),
            last_tick_ns=None,
            priority=priority_value,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            raise RuntimeError(str(result.get("error") or "operation transport failed"))
        decoded = self._decode_json_control_payload(result, request_id="", kind="operation start")
        if not decoded.get("ok") or not decoded.get("accepted"):
            error_message = decoded.get("error_message") or "operation start rejected"
            raise RuntimeError(str(error_message))
        operation_id = str(decoded.get("operation_id") or "")
        if not operation_id:
            raise RuntimeError("operation start response did not include an operation_id")
        response = {
            "ok": True,
            "target": target,
            "operation": operation,
            "operation_id": operation_id,
            "payload_type": str(request_type),
            "payload": dict(payload),
        }
        if timing is not None:
            response["user_header"] = user_header
            response["normalized_header"] = normalized_header
            response["runtime_accepted_at_sync_ns"] = accepted_at_sync_ns
        self._check_control_deadline(deadline)
        return response

    def _start_timed_operation(
        self,
        *,
        target: str,
        operation: str,
        payload: dict[str, Any],
        timeout_ms: int | None = None,
        timing: dict[str, Any],
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(
            timeout_ms if timeout_ms is not None else 1000,
        )
        self._prepare_control_target(
            target, operation=operation, **({"deadline": deadline} if deadline is not None else {}),
        )

        timing_options = dict(timing)
        request_id = uuid.uuid4().hex
        user_header = self.build_timing_request_header(
            target_subsystem=target,
            execute_after_ns=timing_options.get("execute_after_ns"),
            execute_at_sync_ns=timing_options.get("execute_at_sync_ns"),
            priority=timing_options.get("priority", 128),
            late_policy=timing_options.get("late_policy", "execute_immediately"),
            max_lateness_ns=timing_options.get("max_lateness_ns", 1_000_000),
            correlation_id=timing_options.get("correlation_id"),
            request_id=timing_options.get("request_id"),
            generated_at_sync_ns=timing_options.get("generated_at_sync_ns"),
            valid_for_ns=timing_options.get("valid_for_ns"),
            valid_until_sync_ns=timing_options.get("valid_until_sync_ns"),
        )
        normalized_header, accepted_at_sync_ns = self.normalize_timing_request_header(user_header)
        timed_params = self.pack_timing_request(
            request_header=user_header,
            payload_ref="inline_args",
            payload=payload,
        )
        envelope = {
            "kind": "operation_start_request",
            "target": target,
            "operation": operation,
            "request_id": request_id,
            "timeout_ms": int(timeout_ms) if timeout_ms is not None else None,
            "params": timed_params,
        }
        result = self._transact_control_packet(
            target=target,
            topic="operation.start",
            type="bytes",
            value=json.dumps(envelope).encode("utf-8"),
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms or 1000, 1)),
            last_tick_ns=None,
            priority=int(timing_options.get("priority", 128)),
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            raise RuntimeError(str(result.get("error") or "operation transport failed"))
        decoded = self._decode_json_control_payload(result, request_id=request_id, kind="operation start")
        if not decoded.get("ok") or not decoded.get("accepted"):
            error_message = decoded.get("error_message") or "operation start rejected"
            raise RuntimeError(str(error_message))
        operation_id = str(decoded.get("operation_id") or "")
        if not operation_id:
            raise RuntimeError("operation start response did not include an operation_id")
        self._check_control_deadline(deadline)
        return {
            "ok": True,
            "target": target,
            "operation": operation,
            "operation_id": operation_id,
            "payload": dict(payload),
            "user_header": user_header,
            "normalized_header": normalized_header,
            "runtime_accepted_at_sync_ns": accepted_at_sync_ns,
        }

    def _cancel_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        deadline = self._control_call_deadline(timeout_ms)
        if not operation_id:
            return {
                "ok": False,
                "request_id": "",
                "error_code": "invalid_argument",
                "error_message": "operation_id is required",
            }
        try:
            self._prepare_control_target(
                target, **({"deadline": deadline} if deadline is not None else {}),
            )
        except Exception as exc:
            return {
                "ok": False,
                "request_id": "",
                "error_code": "unavailable",
                "error_message": str(exc),
            }
        codec = self._get_rust_message_codec()
        payload = codec.encode_message("msg.core.common.primitive.v1/StringValue", {"value": operation_id})
        result = self._client.transact_control(
            target=target,
            topic=_TYPED_OPERATION_CANCEL_TOPIC,
            type="bytes",
            value=payload,
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms, 1)),
            last_tick_ns=None,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            return {
                "ok": False,
                "request_id": "",
                "error_code": str(result.get("error") or "timeout"),
                "error_message": str(result.get("error") or "operation cancel failed"),
            }
        decoded = self._decode_json_control_payload(result, request_id="", kind="operation cancel")
        decoded.setdefault("request_id", "")
        decoded.setdefault("ok", False)
        if decoded.get("ok"):
            self._check_control_deadline(deadline)
        return decoded

    def cancel_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        return self._cancel_operation(target=target, operation_id=operation_id, timeout_ms=timeout_ms)

    def _query_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        deadline = self._control_call_deadline(timeout_ms)
        if not operation_id:
            return {
                "ok": False,
                "request_id": "",
                "error_code": "invalid_argument",
                "error_message": "operation_id is required",
            }
        try:
            self._prepare_control_target(
                target, **({"deadline": deadline} if deadline is not None else {}),
            )
        except Exception as exc:
            return {
                "ok": False,
                "request_id": "",
                "error_code": "unavailable",
                "error_message": str(exc),
            }
        codec = self._get_rust_message_codec()
        payload = codec.encode_message("msg.core.common.primitive.v1/StringValue", {"value": operation_id})
        result = self._client.transact_control(
            target=target,
            topic=_TYPED_OPERATION_QUERY_TOPIC,
            type="bytes",
            value=payload,
            period_ms=0.0,
            deadline_ms=float(max(timeout_ms, 1)),
            last_tick_ns=None,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        if not result.get("ok"):
            self._invalidate_control_target(target)
            return {
                "ok": False,
                "request_id": "",
                "error_code": str(result.get("error") or "timeout"),
                "error_message": str(result.get("error") or "operation query failed"),
            }
        decoded = self._decode_json_control_payload(result, request_id="", kind="operation query")
        decoded.setdefault("request_id", "")
        decoded.setdefault("ok", False)
        if decoded.get("ok"):
            self._check_control_deadline(deadline)
        return decoded

    def query_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        return self._query_operation(target=target, operation_id=operation_id, timeout_ms=timeout_ms)

    def _subscribe_operation_feedback(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._register_operation_subscription(
            topic=self._operation_topic(target=target, operation=operation, kind="feedback"),
            callback=callback,
            size=size,
            operation_id=operation_id,
        )

    def subscribe_operation_feedback(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._subscribe_operation_feedback(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )

    def _subscribe_operation_result(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._register_operation_subscription(
            topic=self._operation_topic(target=target, operation=operation, kind="result"),
            callback=callback,
            size=size,
            operation_id=operation_id,
        )

    def subscribe_operation_result(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._subscribe_operation_result(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )

    def _subscribe_operation_state(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._register_operation_subscription(
            topic=self._operation_topic(target=target, operation=operation, kind="state"),
            callback=callback,
            size=size,
            operation_id=operation_id,
        )

    def subscribe_operation_state(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = OPERATION_EVENT_SIZE,
    ) -> None:
        self._subscribe_operation_state(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )
