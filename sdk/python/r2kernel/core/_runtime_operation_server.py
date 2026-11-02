"""SubsystemRuntime Operation server: lifecycle, publication and executor."""

from __future__ import annotations

import inspect
import json
import queue
import struct
import threading
import uuid
from typing import Any, Callable, TYPE_CHECKING

from ._runtime_common import OPERATION_EVENT_SIZE, LOG_ID_OPERATION, _OPERATION_TERMINAL_STATES
from .request_context import (
    OperationContext,
    OperationRecord,
    RequestContext,
    ControlRequestContext,
)

if TYPE_CHECKING:
    from .request_context import ProcedureContext


_OPERATION_EXECUTOR_WORKERS = 1


_OPERATION_EVENT_MAGIC = b"SDAE"
_OPERATION_EVENT_VERSION = 1
_OPERATION_EVENT_KIND_CODES = {"feedback": 1, "result": 2, "state": 3}
_OPERATION_EVENT_KIND_NAMES = {value: key for key, value in _OPERATION_EVENT_KIND_CODES.items()}
_OPERATION_EVENT_FLAG_PAYLOAD = 1 << 0
_OPERATION_EVENT_FLAG_JSON = 1 << 1
_OPERATION_EVENT_FLAG_TYPED = 1 << 2
_OPERATION_EVENT_FLAG_CANCEL_REQUESTED = 1 << 3
_OPERATION_EVENT_FLAG_STATE = 1 << 4


def _is_operation_event_wire(payload: bytes | bytearray) -> bool:
    return isinstance(payload, (bytes, bytearray)) and len(payload) >= 4 and bytes(payload[:4]) == _OPERATION_EVENT_MAGIC


class _OperationExecutionMixin:
    """Operation acceptance, feedback/result/state publication and executor of SubsystemRuntime."""

    def _clone_request_context(self, request_context: RequestContext, *, payload_type: str | None = None) -> RequestContext:
        return RequestContext(
            kind=request_context.kind,
            name=request_context.name,
            topic=request_context.topic,
            accepted_at_sync_ns=request_context.accepted_at_sync_ns,
            timed=request_context.timed,
            request_header=request_context.request_header,
            payload_type=payload_type if payload_type is not None else request_context.payload_type,
            response_type=request_context.response_type,
            timed_transport=request_context.timed_transport,
            timed_request_id_u64=request_context.timed_request_id_u64,
            request_id_text=request_context.request_id_text,
            correlation_id_text=request_context.correlation_id_text,
            generated_at_sync_ns=request_context.generated_at_sync_ns,
            sent_at_sync_ns=request_context.sent_at_sync_ns,
            execute_at_sync_ns=request_context.execute_at_sync_ns,
            valid_until_sync_ns=request_context.valid_until_sync_ns,
            priority_value=request_context.priority_value,
            late_policy_name=request_context.late_policy_name,
            max_lateness_ns_value=request_context.max_lateness_ns_value,
            source_subsystem_name=request_context.source_subsystem_name,
            source_authority=request_context.source_authority,
            target_subsystem_name=request_context.target_subsystem_name,
            period_ms=request_context.period_ms,
            deadline_ms=request_context.deadline_ms,
            runtime=self,
            host_authority=request_context.host_authority,
        )

    def _accept_operation(
        self,
        *,
        operation: str,
        params: Any,
        created_ts: int,
        request_context: RequestContext | None,
        result_type: str | None,
    ) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex
        record = OperationRecord(
            operation_id=operation_id,
            operation=operation,
            params=params,
            created_ts=created_ts,
            updated_ts=created_ts,
            request_context=request_context,
            result_type=result_type,
        )
        with self._operation_lock:
            self._operation_records[operation_id] = record
            self._operation_quiescence.notify_all()
        self._submit_operation(operation_id)
        return {
            "ok": True,
            "accepted": True,
            "operation_id": operation_id,
            "state": "queued",
        }

    def is_operation_cancel_requested(self, operation_id: str) -> bool:
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            return bool(record and record.cancel_requested)

    def publish_operation_feedback(self, *, operation_id: str, payload: Any) -> None:
        self._publish_operation_feedback(operation_id=operation_id, payload=payload)

    def publish_operation_result(self, *, operation_id: str, payload: Any) -> None:
        self._publish_operation_result(operation_id=operation_id, payload=payload)

    def publish_operation_state(self, *, operation_id: str, state: str, payload: Any = None) -> None:
        self._publish_operation_state(operation_id=operation_id, state=state, payload=payload)

    def complete_operation(
        self,
        *,
        operation_id: str,
        state: str,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is None or record.state in _OPERATION_TERMINAL_STATES:
                return
            record.state = state
            record.updated_ts = self.now_ns()
            record.result = result
            record.error_code = error_code
            record.error_message = error_message
            self._operation_quiescence.notify_all()
        # State events carry lifecycle state only. Result payloads are published
        # separately below; reusing them as state payloads breaks typed Operation
        # contracts whose state_type differs from result_type.
        self._publish_operation_state(operation_id=operation_id, state=state, payload=None)
        if result is not None:
            self._publish_operation_result(operation_id=operation_id, payload=result)

    def _run_operation(self, operation_id: str) -> None:
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is None:
                return
            handler = self._operation_handlers.get(record.operation)
            if record.cancel_requested:
                record.updated_ts = self.now_ns()
                should_cancel = True
            else:
                should_cancel = False
                record.state = "running"
                record.updated_ts = self.now_ns()
        if should_cancel:
            self.complete_operation(operation_id=operation_id, state="canceled", result=None)
            return
        if handler is None:
            self.complete_operation(
                operation_id=operation_id,
                state="failed",
                error_code="not_found",
                error_message=f"unknown operation: {record.operation}",
            )
            return

        context = OperationContext(
            self,
            operation_id,
            request_context=record.request_context,
            result_type=record.result_type,
        )
        captured_host = record.request_context.host_authority if record.request_context is not None else None
        try:
            with self._transport_authority_scope(host=captured_host, source=None):
                result = self._invoke_operation_handler(handler, record.params, context)
            with self._operation_lock:
                latest = self._operation_records.get(operation_id)
                if latest is None or latest.state in _OPERATION_TERMINAL_STATES:
                    return
                cancel_requested = latest.cancel_requested
            if cancel_requested:
                self.complete_operation(operation_id=operation_id, state="canceled", result=result)
            else:
                self.complete_operation(operation_id=operation_id, state="succeeded", result=result)
        except Exception as exc:
            self._logger.error(LOG_ID_OPERATION, f"Operation '{record.operation}' failed: {exc}")
            self.complete_operation(
                operation_id=operation_id,
                state="failed",
                error_code="execution_failed",
                error_message=str(exc),
            )

    def _invoke_control_handler(self, req: dict, *, handler=None, full_request=False) -> dict:
        handler = self._control_handler if handler is None else handler
        if handler is None:
            return {}
        accepted_at_sync_ns = self.now_ns()
        payload, request_context = self._extract_request_context(
            kind="control",
            name=str(req.get("topic") or ""),
            topic=str(req.get("topic") or ""),
            payload=self._decode_optional_json_bytes(req.get("payload")),
            accepted_at_sync_ns=accepted_at_sync_ns,
        )
        try:
            if self._handler_accepts_context(handler):
                primary = req if full_request else (payload if request_context.timed else req)
                result = handler(
                    primary,
                    ControlRequestContext(
                        kind=request_context.kind,
                        name=request_context.name,
                        topic=request_context.topic,
                        accepted_at_sync_ns=request_context.accepted_at_sync_ns,
                        timed=request_context.timed,
                        request_header=request_context.request_header,
                        payload_type=request_context.payload_type,
                        response_type=request_context.response_type,
                        timed_transport=request_context.timed_transport,
                        timed_request_id_u64=request_context.timed_request_id_u64,
                        request_id_text=request_context.request_id_text,
                        correlation_id_text=request_context.correlation_id_text,
                        generated_at_sync_ns=request_context.generated_at_sync_ns,
                        sent_at_sync_ns=request_context.sent_at_sync_ns,
                        execute_at_sync_ns=request_context.execute_at_sync_ns,
                        valid_until_sync_ns=request_context.valid_until_sync_ns,
                        priority_value=request_context.priority_value,
                        late_policy_name=request_context.late_policy_name,
                        max_lateness_ns_value=request_context.max_lateness_ns_value,
                        source_subsystem_name=(str(req.get("sender") or "").strip() or None) if full_request else request_context.source_subsystem_name,
                        source_authority=getattr(self._procedure_handler_context, "authenticated_source", None) if full_request else request_context.source_authority,
                        target_subsystem_name=request_context.target_subsystem_name,
                        runtime=self,
                        host_authority=request_context.host_authority,
                    ),
                )
            else:
                result = handler(req)
        except Exception as exc:
            self._logger.error("Control", f"Control handler failed: {exc}")
            return {"ts": accepted_at_sync_ns, "status": 1, "payload": str(exc).encode("utf-8")}
        return result or {}

    def _invoke_procedure_handler(self, handler: Callable[..., Any], params: Any, context: ProcedureContext) -> Any:
        was_active = getattr(self._procedure_handler_context, "active", False)
        self._procedure_handler_context.active = True
        try:
            if self._handler_accepts_context(handler):
                return handler(params, context)
            return self._invoke_handler_without_context(handler, params)
        finally:
            self._procedure_handler_context.active = was_active

    def _invoke_operation_handler(self, handler: Callable[..., Any], params: Any, context: OperationContext) -> Any:
        if self._handler_accepts_context(handler):
            return handler(params, context)
        return self._invoke_handler_without_context(handler, params)

    def _register_operation_subscription(
        self,
        *,
        topic: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int,
        operation_id: str | None = None,
    ) -> None:
        self.start_event_transport()

        def handler(value: Any, timestamp: int) -> None:
            if isinstance(value, (bytes, bytearray)):
                raw = bytes(value)
            else:
                raw = bytes(value)
            try:
                if raw and _is_operation_event_wire(raw):
                    decoded = self._decode_operation_event_wire(raw)
                else:
                    decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as exc:
                self._logger.error(LOG_ID_OPERATION, f"Failed to decode Operation event '{topic}': {exc}")
                return
            if isinstance(decoded, dict):
                if operation_id is not None and str(decoded.get("operation_id") or "") != str(operation_id):
                    return
                callback(decoded, timestamp)

        self.register_event_subscribe(
            topic=topic,
            type="bytes",
            size=size,
            callback=handler,
        )

    def _operation_payload_contract_type(self, *, operation: str, kind: str) -> str | None:
        contract = self._operation_contracts.get(operation) or {}
        if kind == "feedback":
            value = contract.get("feedback_type")
        elif kind == "result":
            value = contract.get("result_type")
        else:
            value = contract.get("state_type")
        text = str(value or "").strip()
        return text or None

    def _operation_topic(self, *, target: str, operation: str, kind: str) -> str:
        return f"operation/{target}/{operation}/{kind}"

    def _ensure_operation_publish_topics(self, operation: str) -> None:
        # Cold allocation may wait on management/IPC. Keep it independent of
        # operation state so query and cancellation flags remain observable.
        if operation in self._operation_publish_topics_registered:
            return
        with self._operation_publish_topics_lock:
            if operation in self._operation_publish_topics_registered:
                return
            for kind in ("feedback", "result", "state"):
                topic = self._operation_topic(target=self._subsystem_name, operation=operation, kind=kind)
                if not self.register_event_publish(topic=topic, type="bytes", size=OPERATION_EVENT_SIZE):
                    raise RuntimeError(f"failed to allocate publish buffer for Operation topic: {topic}")
            self.start_event_transport()
            self._operation_publish_topics_registered.add(operation)

    def _prepare_operation_publication(self, operation_id: str) -> OperationRecord | None:
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is None:
                return None
            operation = record.operation
        self._ensure_operation_publish_topics(operation)
        return record

    def _publish_operation_feedback(self, *, operation_id: str, payload: Any) -> None:
        prepared_record = self._prepare_operation_publication(operation_id)
        if prepared_record is None:
            return
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is not prepared_record:
                return
            record.feedback_count += 1
            record.updated_ts = self.now_ns()
            message = {
                "operation_id": record.operation_id,
                "operation": record.operation,
                "target": self._subsystem_name,
                "kind": "feedback",
                "payload": payload,
                "ts": record.updated_ts,
            }
            topic = self._operation_topic(
                target=self._subsystem_name,
                operation=record.operation,
                kind="feedback",
            )
            wire = self._encode_operation_event_wire(
                kind="feedback",
                operation_id=record.operation_id,
                operation=record.operation,
                target=self._subsystem_name,
                payload=payload,
                payload_type=self._operation_payload_contract_type(operation=record.operation, kind="feedback"),
                ts=message["ts"],
            )
        self.publish_event(topic=topic, value=wire, timestamp=message["ts"])

    def _publish_operation_result(self, *, operation_id: str, payload: Any) -> None:
        prepared_record = self._prepare_operation_publication(operation_id)
        if prepared_record is None:
            return
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is not prepared_record:
                return
            event_payload, event_payload_type = self._operation_result_event_payload(record, payload)
            message = {
                "operation_id": record.operation_id,
                "operation": record.operation,
                "target": self._subsystem_name,
                "kind": "result",
                "payload": payload,
                "ts": self.now_ns(),
            }
            topic = self._operation_topic(
                target=self._subsystem_name,
                operation=record.operation,
                kind="result",
            )
            wire = self._encode_operation_event_wire(
                kind="result",
                operation_id=record.operation_id,
                operation=record.operation,
                target=self._subsystem_name,
                payload=event_payload,
                payload_type=event_payload_type,
                ts=message["ts"],
            )
        self.publish_event(topic=topic, value=wire, timestamp=message["ts"])

    def _operation_result_event_payload(
        self,
        record: OperationRecord,
        payload: Any,
    ) -> tuple[Any, str | None]:
        payload_type = self._operation_payload_contract_type(operation=record.operation, kind="result")
        if not isinstance(payload, dict):
            return payload, payload_type
        timing_response = payload.get("timing_response")
        if not isinstance(timing_response, dict):
            return payload, payload_type
        inner_payload = timing_response.get("payload")
        inner_payload_type = str(timing_response.get("payload_ref") or "").strip() or payload_type
        if isinstance(inner_payload, dict):
            return inner_payload, inner_payload_type
        return payload, payload_type

    def _publish_operation_state(self, *, operation_id: str, state: str, payload: Any = None) -> None:
        prepared_record = self._prepare_operation_publication(operation_id)
        if prepared_record is None:
            return
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is not prepared_record:
                return
            if record.state in _OPERATION_TERMINAL_STATES and record.state != state:
                return
            if record.cancel_requested and state not in _OPERATION_TERMINAL_STATES and state != "cancel_requested":
                return
            record.state = state
            record.updated_ts = self.now_ns()
            record.state_payload = payload
            message = {
                "operation_id": record.operation_id,
                "operation": record.operation,
                "target": self._subsystem_name,
                "kind": "state",
                "state": state,
                "payload": payload,
                "cancel_requested": record.cancel_requested,
                "ts": record.updated_ts,
            }
            topic = self._operation_topic(
                target=self._subsystem_name,
                operation=record.operation,
                kind="state",
            )
            wire = self._encode_operation_event_wire(
                kind="state",
                operation_id=record.operation_id,
                operation=record.operation,
                target=self._subsystem_name,
                payload=payload,
                payload_type=self._operation_payload_contract_type(operation=record.operation, kind="state"),
                ts=message["ts"],
                state=state,
                cancel_requested=record.cancel_requested,
            )
        self.publish_event(topic=topic, value=wire, timestamp=message["ts"])

    def _encode_operation_event_wire(
        self,
        *,
        kind: str,
        operation_id: str,
        operation: str,
        target: str,
        payload: Any,
        payload_type: str | None,
        ts: int,
        state: str | None = None,
        cancel_requested: bool = False,
    ) -> bytes:
        kind_code = _OPERATION_EVENT_KIND_CODES.get(kind)
        if kind_code is None:
            raise ValueError(f"unsupported Operation event kind: {kind}")
        flags = 0
        payload_bytes = b""
        payload_type_text = str(payload_type or "").strip()
        if payload is not None:
            if payload_type_text and "/" in payload_type_text and not isinstance(payload, (bytes, bytearray)):
                codec = self._get_rust_message_codec()
                payload_bytes = codec.encode_message(payload_type_text, payload)
                flags |= _OPERATION_EVENT_FLAG_TYPED
            elif isinstance(payload, (bytes, bytearray)):
                payload_bytes = bytes(payload)
            else:
                payload_bytes = json.dumps(payload).encode("utf-8")
                flags |= _OPERATION_EVENT_FLAG_JSON
            flags |= _OPERATION_EVENT_FLAG_PAYLOAD
        if cancel_requested:
            flags |= _OPERATION_EVENT_FLAG_CANCEL_REQUESTED
        state_text = str(state or "")
        if state_text:
            flags |= _OPERATION_EVENT_FLAG_STATE

        operation_id_bytes = operation_id.encode("utf-8")
        operation_bytes = operation.encode("utf-8")
        target_bytes = target.encode("utf-8")
        payload_type_bytes = payload_type_text.encode("utf-8")
        state_bytes = state_text.encode("utf-8")
        header = struct.pack(
            "<4sBBHqHHHHHI",
            _OPERATION_EVENT_MAGIC,
            _OPERATION_EVENT_VERSION,
            kind_code,
            flags,
            int(ts),
            len(operation_id_bytes),
            len(operation_bytes),
            len(target_bytes),
            len(payload_type_bytes),
            len(state_bytes),
            len(payload_bytes),
        )
        return b"".join(
            [
                header,
                operation_id_bytes,
                operation_bytes,
                target_bytes,
                payload_type_bytes,
                state_bytes,
                payload_bytes,
            ]
        )

    def _decode_operation_event_wire(self, payload: bytes) -> dict[str, Any]:
        (
            magic,
            version,
            kind_code,
            flags,
            ts,
            operation_id_len,
            operation_len,
            target_len,
            payload_type_len,
            state_len,
            payload_len,
        ) = struct.unpack("<4sBBHqHHHHHI", payload[:30])
        if magic != _OPERATION_EVENT_MAGIC or version != _OPERATION_EVENT_VERSION:
            raise ValueError("invalid Operation event wire header")
        offset = 30
        operation_id = payload[offset : offset + operation_id_len].decode("utf-8")
        offset += operation_id_len
        operation = payload[offset : offset + operation_len].decode("utf-8")
        offset += operation_len
        target = payload[offset : offset + target_len].decode("utf-8")
        offset += target_len
        payload_type = payload[offset : offset + payload_type_len].decode("utf-8") if payload_type_len else ""
        offset += payload_type_len
        state = payload[offset : offset + state_len].decode("utf-8") if state_len else ""
        offset += state_len
        payload_bytes = payload[offset : offset + payload_len] if payload_len else b""
        kind = _OPERATION_EVENT_KIND_NAMES.get(kind_code, "unknown")
        message: dict[str, Any] = {
            "operation_id": operation_id,
            "operation": operation,
            "target": target,
            "kind": kind,
            "ts": int(ts),
        }
        if flags & _OPERATION_EVENT_FLAG_STATE:
            message["state"] = state
        if flags & _OPERATION_EVENT_FLAG_CANCEL_REQUESTED:
            message["cancel_requested"] = True
        if payload_type:
            message["payload_type"] = payload_type
        if flags & _OPERATION_EVENT_FLAG_PAYLOAD:
            if (flags & _OPERATION_EVENT_FLAG_TYPED) and payload_type:
                codec = self._get_rust_message_codec()
                message["payload"] = codec.decode_message(payload_type, payload=payload_bytes)
            elif flags & _OPERATION_EVENT_FLAG_JSON:
                message["payload"] = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else None
            else:
                message["payload"] = payload_bytes
        else:
            message["payload"] = None
        return message

    def _ensure_operation_executor(self) -> None:
        live_workers = [worker for worker in self._operation_workers if worker.is_alive()]
        if live_workers:
            self._operation_workers = live_workers
            return
        self._operation_queue = queue.Queue()
        self._operation_workers = []
        for index in range(_OPERATION_EXECUTOR_WORKERS):
            worker = threading.Thread(
                target=self._operation_worker_loop,
                args=(self._operation_queue,),
                daemon=True,
                name=f"r2kernel-operation-worker-{index}",
            )
            worker.start()
            self._operation_workers.append(worker)

    def _stop_operation_executor(self) -> None:
        workers = [worker for worker in self._operation_workers if worker.is_alive()]
        if not workers:
            self._operation_workers = []
            return
        for _ in workers:
            self._operation_queue.put(None)
        for worker in workers:
            worker.join(timeout=1.0)
        self._operation_queue = queue.Queue()
        self._operation_workers = []

    def _submit_operation(self, operation_id: str) -> None:
        self._ensure_operation_executor()
        self._operation_queue.put(operation_id)

    def _active_operation_count(self) -> int:
        with self._operation_lock:
            return sum(1 for record in self._operation_records.values() if record.state not in _OPERATION_TERMINAL_STATES)

    def _wait_for_operation_quiescence(self, *, timeout_s: float) -> bool:
        timeout = max(timeout_s, 0.0)
        with self._operation_quiescence:
            return self._operation_quiescence.wait_for(
                lambda: all(record.state in _OPERATION_TERMINAL_STATES for record in self._operation_records.values()),
                timeout=timeout,
            )

    def _operation_worker_loop(self, work: queue.Queue[str | None]) -> None:
        # A worker keeps the queue it was started with: stopping the executor
        # replaces the queue when a handler outlives the join timeout, and that
        # worker must still account for its item there and take its own stop.
        while True:
            operation_id = work.get()
            try:
                if operation_id is None:
                    return
                self._run_operation(operation_id)
            finally:
                work.task_done()

    def _handler_accepts_context(self, handler: Callable[..., Any]) -> bool:
        try:
            signature = inspect.signature(handler)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        except (TypeError, ValueError):
            return True
        return has_varargs or len(positional) >= 2

    def _invoke_handler_without_context(self, handler: Callable[..., Any], params: Any) -> Any:
        try:
            signature = inspect.signature(handler)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        except (TypeError, ValueError):
            positional = []
            has_varargs = True
        if has_varargs or len(positional) >= 1:
            return handler(params)
        return handler()
