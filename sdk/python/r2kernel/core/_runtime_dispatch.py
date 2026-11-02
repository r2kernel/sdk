"""SubsystemRuntime inbound Control dispatch and request handlers."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from .codec_lifecycle import codec_activity
from .timing import is_control_wire as _is_control_wire
from ._runtime_common import (
    LOG_ID_OPERATION,
    _OPERATION_TERMINAL_STATES,
    _DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX,
    _DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX,
    _TYPED_OPERATION_CANCEL_TOPIC,
    _TYPED_OPERATION_QUERY_TOPIC,
)
from .request_context import (
    RequestContext,
    ProcedureContext,
    ControlRequestContext,
    _current_host_authority,
)


class _DispatchMixin:
    """Inbound Control dispatch, Procedure and Operation request handlers of SubsystemRuntime."""

    @codec_activity
    def _dispatch_control_request(self, req: dict) -> dict:
        sender = str(req.get("sender") or "")
        source_authority = None
        host_authority = None
        if sender.startswith(("__r2kernel_authority_", "__r2kernel_host_")):
            if req.get("_manager_peer_verified") is not True:
                raise ValueError("Authenticated transport context requires a verified manager connection")
            control = sender.startswith("__r2kernel_authority_")
            prefix = "__r2kernel_authority_v1__:" if control else "__r2kernel_host_v1__:"
            if not sender.startswith(prefix) or len(sender[len(prefix):].encode("utf-8")) > 4096:
                raise ValueError("Unsupported authenticated transport context")
            try:
                authority = json.loads(sender[len(prefix):])
            except ValueError:
                raise ValueError("Malformed authenticated transport context") from None
            fields = ({"peer", "epoch", "source", "source_session", "source_registration", "source_reference", "target", "target_session", "target_registration", "interface", "binding_revision", "topic"}
                      if control else {"kind", "peer", "epoch", "codec_source"})
            if (not isinstance(authority, dict) or set(authority) != fields
                    or any(not isinstance(authority[field], str) or (not authority[field] and field != "codec_source") for field in fields)):
                raise ValueError("Malformed authenticated transport context")
            req = dict(req)
            if control:
                if authority["topic"] != str(req.get("topic") or ""):
                    raise ValueError("Authenticated Control route mismatch")
                source_authority = authority
                req["sender"] = authority["source"]
            else:
                if authority["kind"] != "protocol":
                    raise ValueError("Authenticated host authority kind mismatch")
                host_authority = {field:authority[field] for field in ("kind","peer","epoch")}
                req["sender"] = f"host:{authority['peer']}"
        with self._transport_authority_scope(host=host_authority, source=source_authority):
            return self._dispatch_control_request_inner(req)

    @contextmanager
    def _transport_authority_scope(
        self, *, host: dict[str, Any] | None, source: dict[str, Any] | None,
    ) -> Iterator[None]:
        """Make the verified transport authority of one request current on this thread."""
        context = self._procedure_handler_context
        previous_host = getattr(context, "authenticated_host", None)
        previous_source = getattr(context, "authenticated_source", None)
        context.authenticated_host = host
        context.authenticated_source = source
        try:
            yield
        finally:
            context.authenticated_host = previous_host
            context.authenticated_source = previous_source

    def current_control_source_authority(self) -> dict[str, Any] | None:
        """Verified Control source authority of the request this thread is handling, or ``None``."""
        value = getattr(self._procedure_handler_context, "authenticated_source", None)
        return dict(value) if isinstance(value, dict) else None

    def current_protocol_host_authority(self) -> dict[str, Any] | None:
        """Verified Protocol host authority of the request this thread is handling, or ``None``."""
        return _current_host_authority(self)

    def _dispatch_control_request_inner(self, req: dict) -> dict:
        topic = str(req.get("topic") or "")
        if topic == "procedure.call":
            return self._handle_procedure_request(req)
        if topic.startswith(_DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX):
            return self._handle_typed_procedure_request(req, procedure=topic[len(_DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX) :])
        if topic == "procedure.call_typed":
            return self._handle_typed_procedure_request(req)
        if topic == "operation.start":
            return self._handle_operation_start_request(req)
        if topic.startswith(_DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX):
            return self._handle_operation_start_typed_request(req, operation_name=topic[len(_DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX) :])
        if topic == "operation.start_typed":
            return self._handle_operation_start_typed_request(req)
        if topic == _TYPED_OPERATION_CANCEL_TOPIC:
            return self._handle_operation_cancel_typed_request(req)
        if topic == "operation.cancel":
            return self._handle_operation_cancel_request(req)
        if topic == _TYPED_OPERATION_QUERY_TOPIC:
            return self._handle_operation_query_typed_request(req)
        if topic == "operation.query":
            return self._handle_operation_query_request(req)
        if self._control_server_enabled and topic in self._control_endpoint_handlers:
            return self._handle_registered_control_request(req)
        if self._control_server_enabled and topic in self._raw_control_endpoint_handlers:
            return self._invoke_control_handler(req, handler=self._raw_control_endpoint_handlers[topic], full_request=True)
        if self._control_server_enabled and self._control_handler is not None:
            return self._invoke_control_handler(req)
        return {"ts": self.now_ns(), "status": 1, "payload": b""}

    def _handle_registered_control_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        topic = str(req.get("topic") or "")
        transport_source = str(req.get("sender") or "").strip() or None
        source_authority = getattr(self._procedure_handler_context, "authenticated_source", None)
        if transport_source and transport_source.startswith(("__r2kernel_authority_", "__r2kernel_host_")):
            raise ValueError("Authenticated transport context must pass the verified dispatcher")
        handler = self._control_endpoint_handlers.get(topic)
        contract = self._control_contracts.get(topic) or {}
        if handler is None:
            return {"ts": recv_ts, "status": 1, "payload": f"unknown Control topic: {topic}".encode("utf-8")}
        try:
            expected_request_type = str(contract.get("request_type") or "").strip()
            response_type = str(contract.get("response_type") or "").strip()
            raw_payload = self._decode_optional_json_bytes(req.get("payload"))
            if _is_control_wire(raw_payload):
                if not expected_request_type:
                    raise ValueError(f"Control endpoint '{topic}' requires request_type for timed wire decode")
                codec = self._get_rust_message_codec()
                request_meta, payload_bytes = codec.decode_control_request_wire(bytes(raw_payload))
                payload = codec.decode_message(expected_request_type, payload=payload_bytes)
                context = ControlRequestContext(
                    kind="control",
                    name=topic,
                    topic=topic,
                    accepted_at_sync_ns=recv_ts,
                    timed=True,
                    request_header=None,
                    payload_type=expected_request_type,
                    response_type=response_type or None,
                    timed_transport="control_compact",
                    timed_request_id_u64=request_meta["request_id_u64"],
                    request_id_text=f"{int(request_meta['request_id_u64']) & ((1 << 64) - 1):016x}",
                    generated_at_sync_ns=int(request_meta["generated_at_sync_ns"]),
                    sent_at_sync_ns=int(request_meta["sent_at_sync_ns"]),
                    execute_at_sync_ns=int(request_meta["execute_at_sync_ns"]),
                    valid_until_sync_ns=request_meta.get("valid_until_sync_ns"),
                    priority_value=int(request_meta["priority"]),
                    late_policy_name=str(request_meta["late_policy"]),
                    max_lateness_ns_value=(
                        None
                        if request_meta.get("max_lateness_ns") is None
                        else int(request_meta["max_lateness_ns"])
                    ),
                    source_subsystem_name=transport_source,
                    source_authority=source_authority,
                    period_ms=float(req.get("period_ms") or 0.0),
                    deadline_ms=float(req.get("deadline_ms") or 0.0),
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            else:
                payload, request_context = self._extract_request_context(
                    kind="control",
                    name=topic,
                    topic=topic,
                    payload=raw_payload,
                    accepted_at_sync_ns=recv_ts,
                )
                context = ControlRequestContext(
                    kind=request_context.kind,
                    name=request_context.name,
                    topic=request_context.topic,
                    accepted_at_sync_ns=request_context.accepted_at_sync_ns,
                    timed=request_context.timed,
                    request_header=request_context.request_header,
                    payload_type=request_context.payload_type or (expected_request_type or None),
                    response_type=response_type or None,
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
                    source_subsystem_name=transport_source,
                    source_authority=source_authority,
                    target_subsystem_name=request_context.target_subsystem_name,
                    period_ms=float(req.get("period_ms") or 0.0),
                    deadline_ms=float(req.get("deadline_ms") or 0.0),
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            self._enforce_timing_support(
                kind="control",
                name=topic,
                timing_support=contract.get("timing_support"),
                request_context=context,
            )
            if context.timed and expected_request_type and context.payload_type != expected_request_type:
                raise ValueError(
                    f"Control endpoint '{topic}' timed payload type must be '{expected_request_type}', found '{context.payload_type}'"
                )
            if not context.timed and expected_request_type:
                if isinstance(payload, (bytes, bytearray)):
                    codec = self._get_rust_message_codec()
                    payload = codec.decode_message(expected_request_type, payload=bytes(payload))
                elif not isinstance(payload, dict):
                    raise ValueError(f"Control endpoint '{topic}' requires typed payload '{expected_request_type}'")
            result = self._invoke_procedure_handler(handler, payload, context)
            if isinstance(result, dict) and {"ts", "status", "payload"} <= set(result.keys()):
                return result
            if response_type and isinstance(result, dict):
                if context.timed:
                    completed_ts = self.now_ns()
                    scheduled_ts = int(context.execute_at_sync_ns or context.accepted_at_sync_ns)
                    lateness_ns = max(0, completed_ts - scheduled_ts)
                    final_status = "executed_late" if lateness_ns > 0 else "executed"
                    return self._build_timed_transport_response(
                        request_context=context,
                        payload=result,
                        execution_report={
                            "accepted_at_sync_ns": context.accepted_at_sync_ns,
                            "scheduled_for_sync_ns": scheduled_ts,
                            "dispatched_at_sync_ns": recv_ts,
                            "completed_at_sync_ns": completed_ts,
                            "lateness_ns": lateness_ns,
                            "final_status": final_status,
                        },
                        payload_type=response_type,
                        ok=True,
                        responded_at_sync_ns=completed_ts,
                    )
                codec = self._get_rust_message_codec()
                return {
                    "ts": recv_ts,
                    "status": 0,
                    "payload": codec.encode_message(response_type, result),
                }
            if isinstance(result, dict):
                return {
                    "ts": recv_ts,
                    "status": 0,
                    "payload": json.dumps(result).encode("utf-8"),
                }
            if isinstance(result, (bytes, bytearray)):
                return {"ts": recv_ts, "status": 0, "payload": bytes(result)}
            return {"ts": recv_ts, "status": 0, "payload": json.dumps(result).encode("utf-8")}
        except Exception as exc:
            return {"ts": recv_ts, "status": 1, "payload": str(exc).encode("utf-8")}

    def _handle_procedure_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        request_id = ""
        try:
            payload = req.get("payload") or b""
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            message = json.loads(payload.decode("utf-8")) if payload else {}
            request_id = str(message.get("request_id") or "")
            procedure = str(message.get("procedure") or "").strip()
            if not procedure:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error_code": "invalid_argument",
                    "error_message": "missing procedure name",
                }
            else:
                handler = self._procedure_handlers.get(procedure)
                contract = self._procedure_contracts.get(procedure) or {}
                if handler is None:
                    response = {
                        "request_id": request_id,
                        "ok": False,
                        "error_code": "not_found",
                        "error_message": f"unknown procedure: {procedure}",
                    }
                else:
                    params, request_context = self._extract_request_context(
                        kind="procedure",
                        name=procedure,
                        topic="procedure.call",
                        payload=message.get("params"),
                        accepted_at_sync_ns=recv_ts,
                    )
                    self._enforce_timing_support(
                        kind="procedure",
                        name=procedure,
                        timing_support=contract.get("timing_support"),
                        request_context=request_context,
                    )
                    expected_request_type = str(contract.get("request_type") or "").strip()
                    if request_context.timed and expected_request_type and request_context.payload_type != expected_request_type:
                        raise ValueError(
                            f"procedure '{procedure}' timed payload type must be '{expected_request_type}', found '{request_context.payload_type}'"
                        )
                    result = self._invoke_procedure_handler(
                        handler,
                        params,
                        ProcedureContext(
                            kind=request_context.kind,
                            name=request_context.name,
                            topic=request_context.topic,
                            accepted_at_sync_ns=request_context.accepted_at_sync_ns,
                            timed=request_context.timed,
                            request_header=request_context.request_header,
                            payload_type=request_context.payload_type,
                            response_type=str(contract.get("response_type") or "").strip() or None,
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
                            runtime=self,
                            host_authority=request_context.host_authority,
                        ),
                    )
                    response = {
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_typed_procedure_request(self, req: dict, *, procedure: str | None = None) -> dict:
        recv_ts = self.now_ns()
        try:
            payload = req.get("payload") or b""
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            request_context: RequestContext
            if _is_control_wire(payload):
                codec = self._get_rust_message_codec()
                request_meta, inner_payload = codec.decode_control_request_wire(bytes(payload))
                if procedure is None:
                    procedure, request_payload = self._decode_named_payload_wire(inner_payload)
                else:
                    request_payload = bytes(inner_payload)
                request_context = ProcedureContext(
                    kind="procedure",
                    name=procedure,
                    topic="procedure.call_typed",
                    accepted_at_sync_ns=recv_ts,
                    timed=True,
                    request_header=None,
                    timed_transport="control_compact",
                    timed_request_id_u64=request_meta["request_id_u64"],
                    request_id_text=f"{int(request_meta['request_id_u64']) & ((1 << 64) - 1):016x}",
                    generated_at_sync_ns=int(request_meta["generated_at_sync_ns"]),
                    sent_at_sync_ns=int(request_meta["sent_at_sync_ns"]),
                    execute_at_sync_ns=int(request_meta["execute_at_sync_ns"]),
                    valid_until_sync_ns=request_meta.get("valid_until_sync_ns"),
                    priority_value=int(request_meta["priority"]),
                    late_policy_name=str(request_meta["late_policy"]),
                    max_lateness_ns_value=(
                        None
                        if request_meta.get("max_lateness_ns") is None
                        else int(request_meta["max_lateness_ns"])
                    ),
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            else:
                if procedure is None:
                    procedure, request_payload = self._decode_typed_procedure_wire(payload)
                else:
                    request_payload = bytes(payload)
                request_context = ProcedureContext(
                    kind="procedure",
                    name=procedure,
                    topic="procedure.call_typed",
                    accepted_at_sync_ns=recv_ts,
                    timed=False,
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            contract = self._procedure_contracts.get(procedure) or {}
            request_type = str(contract.get("request_type") or "").strip()
            response_type = str(contract.get("response_type") or "").strip()
            self._enforce_timing_support(
                kind="procedure",
                name=procedure,
                timing_support=contract.get("timing_support"),
                request_context=request_context,
            )
            if not request_type or not response_type:
                return {
                    "ts": recv_ts,
                    "status": 1,
                    "payload": f"typed procedure contract metadata missing: {procedure}".encode("utf-8"),
                }
            handler = self._typed_procedure_handlers.get(procedure) or self._procedure_handlers.get(procedure)
            if handler is None:
                return {
                    "ts": recv_ts,
                    "status": 1,
                    "payload": f"unknown typed procedure: {procedure}".encode("utf-8"),
                }
            codec = self._get_rust_message_codec()
            params = codec.decode_message(request_type, payload=request_payload)
            result = self._invoke_procedure_handler(
                handler,
                params,
                ProcedureContext(
                    kind=request_context.kind,
                    name=request_context.name,
                    topic=request_context.topic,
                    accepted_at_sync_ns=request_context.accepted_at_sync_ns,
                    timed=request_context.timed,
                    request_header=request_context.request_header,
                    payload_type=request_type,
                    response_type=response_type,
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
                    runtime=self,
                    host_authority=request_context.host_authority,
                ),
            )
            if isinstance(result, dict) and {"ts", "status", "payload"} <= set(result.keys()):
                return result
            response_payload = codec.encode_message(response_type, result)
            return {
                "ts": recv_ts,
                "status": 0,
                "payload": response_payload,
            }
        except Exception as exc:
            self._logger.error("Procedure", f"Typed procedure request failed: {exc}")
            return {
                "ts": recv_ts,
                "status": 1,
                "payload": str(exc).encode("utf-8"),
            }

    def _handle_operation_start_typed_request(self, req: dict, *, operation_name: str | None = None) -> dict:
        recv_ts = self.now_ns()
        try:
            payload = req.get("payload") or b""
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            if _is_control_wire(payload):
                codec = self._get_rust_message_codec()
                request_meta, inner_payload = codec.decode_control_request_wire(bytes(payload))
                if operation_name is None:
                    operation_name, request_payload = self._decode_named_payload_wire(inner_payload)
                else:
                    request_payload = bytes(inner_payload)
                request_context = RequestContext(
                    kind="operation",
                    name=operation_name,
                    topic="operation.start_typed",
                    accepted_at_sync_ns=recv_ts,
                    timed=True,
                    request_header=None,
                    timed_transport="control_compact",
                    timed_request_id_u64=request_meta["request_id_u64"],
                    request_id_text=f"{int(request_meta['request_id_u64']) & ((1 << 64) - 1):016x}",
                    generated_at_sync_ns=int(request_meta["generated_at_sync_ns"]),
                    sent_at_sync_ns=int(request_meta["sent_at_sync_ns"]),
                    execute_at_sync_ns=int(request_meta["execute_at_sync_ns"]),
                    valid_until_sync_ns=request_meta.get("valid_until_sync_ns"),
                    priority_value=int(request_meta["priority"]),
                    late_policy_name=str(request_meta["late_policy"]),
                    max_lateness_ns_value=(
                        None
                        if request_meta.get("max_lateness_ns") is None
                        else int(request_meta["max_lateness_ns"])
                    ),
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            else:
                if operation_name is None:
                    operation_name, request_payload = self._decode_named_payload_wire(payload)
                else:
                    request_payload = bytes(payload)
                request_context = RequestContext(
                    kind="operation",
                    name=operation_name,
                    topic="operation.start_typed",
                    accepted_at_sync_ns=recv_ts,
                    timed=False,
                    runtime=self,
                    host_authority=_current_host_authority(self),
                )
            if not self._operation_server_enabled:
                response = {
                    "ok": False,
                    "accepted": False,
                    "error_code": "unavailable",
                    "error_message": "operation server is not running",
                }
            elif not operation_name:
                response = {
                    "ok": False,
                    "accepted": False,
                    "error_code": "invalid_argument",
                    "error_message": "missing operation name",
                }
            elif operation_name not in self._operation_handlers:
                response = {
                    "ok": False,
                    "accepted": False,
                    "error_code": "not_found",
                    "error_message": f"unknown operation: {operation_name}",
                }
            else:
                contract = self._operation_contracts.get(operation_name) or {}
                self._enforce_timing_support(
                    kind="operation",
                    name=operation_name,
                    timing_support=contract.get("timing_support"),
                    request_context=request_context,
                )
                request_type = str(contract.get("request_type") or "").strip()
                if not request_type:
                    raise ValueError(f"typed operation contract metadata missing: {operation_name}")
                codec = self._get_rust_message_codec()
                params = codec.decode_message(request_type, payload=request_payload)
                response = self._accept_operation(
                    operation=operation_name,
                    params=params,
                    created_ts=recv_ts,
                    request_context=self._clone_request_context(request_context, payload_type=request_type),
                    result_type=str(contract.get("result_type") or "").strip() or None,
                )
        except Exception as exc:
            self._logger.error(LOG_ID_OPERATION, f"Typed operation start failed: {exc}")
            response = {
                "ok": False,
                "accepted": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_operation_start_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        request_id = ""
        try:
            message = self._decode_request_payload(req)
            request_id = str(message.get("request_id") or "")
            operation_name = str(message.get("operation") or "").strip()
            if not self._operation_server_enabled:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "accepted": False,
                    "error_code": "unavailable",
                    "error_message": "operation server is not running",
                }
            elif not operation_name:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "accepted": False,
                    "error_code": "invalid_argument",
                    "error_message": "missing operation name",
                }
            elif operation_name not in self._operation_handlers:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "accepted": False,
                    "error_code": "not_found",
                    "error_message": f"unknown operation: {operation_name}",
                }
            else:
                contract = self._operation_contracts.get(operation_name) or {}
                params, request_context = self._extract_request_context(
                    kind="operation",
                    name=operation_name,
                    topic="operation.start",
                    payload=message.get("params"),
                    accepted_at_sync_ns=recv_ts,
                )
                self._enforce_timing_support(
                    kind="operation",
                    name=operation_name,
                    timing_support=contract.get("timing_support"),
                    request_context=request_context,
                )
                expected_request_type = str(contract.get("request_type") or "").strip()
                if request_context.timed and expected_request_type and request_context.payload_type != expected_request_type:
                    raise ValueError(
                        f"operation '{operation_name}' timed payload type must be '{expected_request_type}', found '{request_context.payload_type}'"
                    )
                response = {
                    "request_id": request_id,
                    **self._accept_operation(
                        operation=operation_name,
                        params=params,
                        created_ts=recv_ts,
                        request_context=request_context,
                        result_type=str(contract.get("result_type") or "").strip() or None,
                    ),
                }
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "accepted": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_operation_cancel_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        request_id = ""
        try:
            message = self._decode_request_payload(req)
            request_id = str(message.get("request_id") or "")
            operation_id = str(message.get("operation_id") or "").strip()
            response = self._build_operation_cancel_response(operation_id=operation_id, request_id=request_id, recv_ts=recv_ts)
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_operation_query_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        request_id = ""
        try:
            message = self._decode_request_payload(req)
            request_id = str(message.get("request_id") or "")
            operation_id = str(message.get("operation_id") or "").strip()
            response = self._build_operation_query_response(operation_id=operation_id, request_id=request_id)
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_operation_cancel_typed_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        try:
            payload = req.get("payload") or b""
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            codec = self._get_rust_message_codec()
            message = codec.decode_message("msg.core.common.primitive.v1/StringValue", payload=bytes(payload))
            operation_id = str(message.get("value") or "").strip()
            response = self._build_operation_cancel_response(operation_id=operation_id, request_id="", recv_ts=recv_ts)
        except Exception as exc:
            response = {
                "request_id": "",
                "ok": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _handle_operation_query_typed_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        try:
            payload = req.get("payload") or b""
            if not isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload)
            codec = self._get_rust_message_codec()
            message = codec.decode_message("msg.core.common.primitive.v1/StringValue", payload=bytes(payload))
            operation_id = str(message.get("value") or "").strip()
            response = self._build_operation_query_response(operation_id=operation_id, request_id="")
        except Exception as exc:
            response = {
                "request_id": "",
                "ok": False,
                "error_code": "execution_failed",
                "error_message": str(exc),
            }
        return {
            "ts": recv_ts,
            "status": 0,
            "payload": json.dumps(response).encode("utf-8"),
        }

    def _build_operation_cancel_response(self, *, operation_id: str, request_id: str, recv_ts: int) -> dict[str, Any]:
        if not operation_id:
            return {
                "request_id": request_id,
                "ok": False,
                "error_code": "invalid_argument",
                "error_message": "missing operation_id",
            }
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is None:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error_code": "not_found",
                    "error_message": f"unknown operation: {operation_id}",
                }
            elif record.state in _OPERATION_TERMINAL_STATES:
                response = {
                    "request_id": request_id,
                    "ok": True,
                    "operation_id": operation_id,
                    "state": record.state,
                    "cancel_requested": record.cancel_requested,
                }
            else:
                record.cancel_requested = True
                record.updated_ts = recv_ts
                response = {
                    "request_id": request_id,
                    "ok": True,
                    "operation_id": operation_id,
                    "state": record.state,
                    "cancel_requested": True,
                }
        if response.get("ok") and response.get("cancel_requested"):
            self._publish_operation_state(operation_id=operation_id, state="cancel_requested", payload=None)
        return response

    def _build_operation_query_response(self, *, operation_id: str, request_id: str) -> dict[str, Any]:
        if not operation_id:
            return {
                "request_id": request_id,
                "ok": False,
                "error_code": "invalid_argument",
                "error_message": "missing operation_id",
            }
        with self._operation_lock:
            record = self._operation_records.get(operation_id)
            if record is None:
                return {
                    "request_id": request_id,
                    "ok": False,
                    "error_code": "not_found",
                    "error_message": f"unknown operation: {operation_id}",
                }
            return {"request_id": request_id, "ok": True, **record.to_query_response()}
