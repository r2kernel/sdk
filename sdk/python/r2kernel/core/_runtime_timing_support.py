"""SubsystemRuntime timing-support enforcement, request context extraction and timed response building."""

from __future__ import annotations

import json
import struct
from typing import Any, TYPE_CHECKING

from .timing import (
    is_timing_wire as _is_timing_wire,
    pack_timing_response_wire as _pack_timing_response_wire,
    resolve_late_policy as _resolve_late_policy,
    unpack_timing_request_wire as _unpack_timing_request_wire,
)
from ._runtime_common import _DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX, _DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX
from .request_context import RequestContext, _current_host_authority

if TYPE_CHECKING:
    from .request_context import OperationRecord


def _timed_execution_succeeded(final_status: str | None) -> bool:
    return str(final_status or "").strip() in {"executed", "executed_late"}
_TIMING_SUPPORT_VALUES = {"none", "optional", "required"}


def _timing_context_fields(header: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(header, dict):
        return {}
    execution = header.get("execution")
    execution_dict = execution if isinstance(execution, dict) else {}
    result: dict[str, Any] = {}
    request_id = header.get("request_id")
    if request_id is not None:
        result["request_id_text"] = str(request_id)
    correlation_id = header.get("correlation_id")
    if correlation_id is not None:
        result["correlation_id_text"] = str(correlation_id)
    sent_at_sync_ns = header.get("sent_at_sync_ns")
    if sent_at_sync_ns is not None:
        result["sent_at_sync_ns"] = int(sent_at_sync_ns)
    generated_at_sync_ns = header.get("generated_at_sync_ns")
    if generated_at_sync_ns is not None:
        result["generated_at_sync_ns"] = int(generated_at_sync_ns)
    priority = header.get("priority")
    if priority is not None:
        result["priority_value"] = int(priority)
    source_subsystem = header.get("source_subsystem")
    if source_subsystem is not None:
        result["source_subsystem_name"] = str(source_subsystem)
    target_subsystem = header.get("target_subsystem")
    if target_subsystem is not None:
        result["target_subsystem_name"] = str(target_subsystem)
    execute_at_sync_ns = execution_dict.get("execute_at_sync_ns")
    if execute_at_sync_ns is not None:
        result["execute_at_sync_ns"] = int(execute_at_sync_ns)
    late_policy = execution_dict.get("late_policy")
    if late_policy is not None:
        result["late_policy_name"] = _resolve_late_policy(late_policy)
    max_lateness_ns = execution_dict.get("max_lateness_ns")
    if max_lateness_ns is not None:
        result["max_lateness_ns_value"] = int(max_lateness_ns)
    valid_until_sync_ns = execution_dict.get("valid_until_sync_ns")
    if valid_until_sync_ns is not None:
        result["valid_until_sync_ns"] = int(valid_until_sync_ns)
    return result


class _TimingSupportMixin:
    """Timing-support enforcement, request context extraction, timed response and wire helpers of SubsystemRuntime."""

    def _normalize_timing_support(self, timing_support: str | None) -> str:
        value = str(timing_support or "none").strip() or "none"
        if value not in _TIMING_SUPPORT_VALUES:
            raise ValueError("timing_support must be one of none|optional|required")
        return value

    def _ensure_endpoint_plane_available(self, name: str, *, plane: str) -> None:
        conflicts: list[str] = []
        if plane != "control" and (name in self._control_endpoint_handlers or name in self._raw_control_endpoint_handlers):
            conflicts.append("control")
        if plane != "procedure" and (
            name in self._procedure_handlers or name in self._typed_procedure_handlers
        ):
            conflicts.append("procedure")
        if plane != "operation" and name in self._operation_handlers:
            conflicts.append("operation")
        if conflicts:
            conflict_text = ", ".join(conflicts)
            raise ValueError(f"{plane} endpoint '{name}' conflicts with existing {conflict_text} endpoint")

    def _enforce_timing_support(
        self,
        *,
        kind: str,
        name: str,
        timing_support: str | None,
        request_context: RequestContext,
    ) -> None:
        resolved = self._normalize_timing_support(timing_support)
        if resolved == "none" and request_context.timed:
            raise ValueError(f"{kind} '{name}' does not accept timed requests")
        if (
            request_context.timed
            and request_context.valid_until_sync_ns is not None
            and self.now_ns() > int(request_context.valid_until_sync_ns)
        ):
            raise ValueError(f"{kind} '{name}' rejected an expired timed request")
        if resolved == "required":
            if kind == "control":
                if float(request_context.period_ms or 0.0) <= 0.0 or float(request_context.deadline_ms or 0.0) <= 0.0:
                    raise ValueError(f"{kind} '{name}' requires positive period_ms and deadline_ms")
                return
            if not request_context.timed:
                raise ValueError(f"{kind} '{name}' requires timing metadata")

    def _extract_request_context(
        self,
        *,
        kind: str,
        name: str,
        topic: str,
        payload: Any,
        accepted_at_sync_ns: int,
    ) -> tuple[Any, RequestContext]:
        if _is_timing_wire(payload):
            codec = self._get_rust_message_codec()
            request_header, payload_ref, inner_payload = _unpack_timing_request_wire(codec, bytes(payload))
            normalized_header, normalized_accepted = self.normalize_timing_request_header(
                request_header,
                accepted_at_sync_ns=accepted_at_sync_ns,
            )
            return inner_payload, RequestContext(
                kind=kind,
                name=name,
                topic=topic,
                accepted_at_sync_ns=normalized_accepted,
                timed=True,
                request_header=normalized_header,
                payload_type=payload_ref,
                **_timing_context_fields(normalized_header),
                runtime=self,
                host_authority=_current_host_authority(self),
            )
        if isinstance(payload, dict) and {"request_header", "payload_ref", "payload"} <= set(payload.keys()):
            request_header, payload_ref, inner_payload = self.unpack_timing_request(payload)
            normalized_header, normalized_accepted = self.normalize_timing_request_header(
                request_header,
                accepted_at_sync_ns=accepted_at_sync_ns,
            )
            return inner_payload, RequestContext(
                kind=kind,
                name=name,
                topic=topic,
                accepted_at_sync_ns=normalized_accepted,
                timed=True,
                request_header=normalized_header,
                payload_type=payload_ref,
                **_timing_context_fields(normalized_header),
                runtime=self,
                host_authority=_current_host_authority(self),
            )
        return payload, RequestContext(
            kind=kind,
            name=name,
            topic=topic,
            accepted_at_sync_ns=accepted_at_sync_ns,
            timed=False,
            runtime=self,
            host_authority=_current_host_authority(self),
        )

    def _decode_optional_json_bytes(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except Exception:
                return payload
        raw = payload or b""
        if not isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw)
            except Exception:
                return payload
        if not raw:
            return {}
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except Exception:
            return bytes(raw)

    def _build_timed_response_wire(
        self,
        *,
        request_context: RequestContext,
        payload: dict[str, Any],
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
        responded_at_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        if not request_context.timed:
            raise ValueError("timed response requires a timed request context")
        resolved_payload_type = str(payload_type or request_context.response_type or request_context.payload_type or "").strip()
        if not resolved_payload_type:
            raise ValueError("timed response requires a payload_type")
        final_status = str(execution_report.get("final_status") or "").strip()
        resolved_ok = bool(ok) if ok is not None else _timed_execution_succeeded(final_status)
        resolved_status_code = int(status_code) if status_code is not None else (0 if resolved_ok else 1)
        resolved_status_message = str(status_message or final_status or ("ok" if resolved_ok else "execution_failed"))
        if request_context.timed_transport == "control_compact" and request_context.timed_request_id_u64 is not None:
            codec = self._get_rust_message_codec()
            payload_bytes = codec.encode_message(resolved_payload_type, payload)
            responded_at = int(responded_at_sync_ns if responded_at_sync_ns is not None else self.now_ns())
            return codec.encode_control_response_wire(
                request_id=request_context.timed_request_id_u64,
                responded_at_sync_ns=responded_at,
                accepted_at_sync_ns=int(execution_report.get("accepted_at_sync_ns") or request_context.accepted_at_sync_ns),
                scheduled_for_sync_ns=int(execution_report.get("scheduled_for_sync_ns") or request_context.accepted_at_sync_ns),
                dispatched_at_sync_ns=int(execution_report.get("dispatched_at_sync_ns") or request_context.accepted_at_sync_ns),
                sampled_at_sync_ns=execution_report.get("sampled_at_sync_ns"),
                completed_at_sync_ns=int(execution_report.get("completed_at_sync_ns") or responded_at),
                lateness_ns=int(execution_report.get("lateness_ns") or 0),
                status_code=resolved_status_code,
                final_status=final_status or ("executed" if resolved_ok else "failed"),
                payload=payload_bytes,
            )
        resolved_request_header = request_context.resolved_request_header()
        if resolved_request_header is None:
            raise ValueError("timed response requires request header metadata")
        response_header = self.build_timing_response_header(
            resolved_request_header,
            execution_report,
            responded_at_sync_ns=responded_at_sync_ns,
            status_code=resolved_status_code,
            status_message=resolved_status_message,
        )
        codec = self._get_rust_message_codec()
        return _pack_timing_response_wire(
            codec,
            response_header=response_header,
            payload_ref=resolved_payload_type,
            payload=payload,
        )

    def _build_timed_response_object(
        self,
        *,
        request_context: RequestContext,
        payload: dict[str, Any],
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
        responded_at_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        if not request_context.timed:
            raise ValueError("timed response requires a timed request context")
        resolved_payload_type = str(payload_type or request_context.response_type or request_context.payload_type or "").strip()
        if not resolved_payload_type:
            raise ValueError("timed response requires a payload_type")
        final_status = str(execution_report.get("final_status") or "").strip()
        resolved_ok = bool(ok) if ok is not None else _timed_execution_succeeded(final_status)
        resolved_status_code = int(status_code) if status_code is not None else (0 if resolved_ok else 1)
        resolved_status_message = str(status_message or final_status or ("ok" if resolved_ok else "execution_failed"))
        resolved_request_header = request_context.resolved_request_header()
        if resolved_request_header is None:
            raise ValueError("timed response requires request header metadata")
        response_header = self.build_timing_response_header(
            resolved_request_header,
            execution_report,
            responded_at_sync_ns=responded_at_sync_ns,
            status_code=resolved_status_code,
            status_message=resolved_status_message,
        )
        return self.pack_timing_response(
            response_header=response_header,
            payload_ref=resolved_payload_type,
            payload=payload,
        )

    def _build_timed_result(
        self,
        *,
        request_context: RequestContext,
        payload: dict[str, Any],
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
        responded_at_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        timing_response = self._build_timed_response_object(
            request_context=request_context,
            payload=payload,
            execution_report=execution_report,
            payload_type=payload_type,
            ok=ok,
            status_code=status_code,
            status_message=status_message,
            responded_at_sync_ns=responded_at_sync_ns,
        )
        resolved_ok = bool(ok) if ok is not None else _timed_execution_succeeded(execution_report.get("final_status"))
        result: dict[str, Any] = {
            "ok": resolved_ok,
            "timing_response": timing_response,
        }
        if extra:
            result.update(dict(extra))
        return result

    def _build_timed_transport_response(
        self,
        *,
        request_context: RequestContext,
        payload: dict[str, Any],
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
        transport_status: int = 0,
        responded_at_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        response_wire = self._build_timed_response_wire(
            request_context=request_context,
            payload=payload,
            execution_report=execution_report,
            payload_type=payload_type,
            ok=ok,
            status_code=status_code,
            status_message=status_message,
            responded_at_sync_ns=responded_at_sync_ns,
        )
        return {
            "ts": int(responded_at_sync_ns if responded_at_sync_ns is not None else self.now_ns()),
            "status": int(transport_status),
            "payload": response_wire,
        }

    def _publish_operation_state_record(self, record: OperationRecord, *, state: str, payload: Any = None) -> None:
        self._publish_operation_state(operation_id=record.operation_id, state=state, payload=payload)

    def _decode_request_payload(self, req: dict) -> dict[str, Any]:
        payload = req.get("payload") or b""
        if not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        decoded = json.loads(payload.decode("utf-8")) if payload else {}
        if not isinstance(decoded, dict):
            raise ValueError("request payload is not an object")
        return decoded

    def _get_rust_message_codec(self):
        self._codec_lifecycle.require_available()
        if self._rust_message_codec is None:
            from r2kernel.interface.codec import RustMessageCodec, warn_unprepared_codec_once

            self._rust_message_codec = RustMessageCodec()
            warn_unprepared_codec_once(self.logger)
        return self._rust_message_codec

    def _encode_named_payload_wire(self, *, name: str, payload: bytes) -> bytes:
        name_bytes = str(name).encode("utf-8")
        return struct.pack(">I", len(name_bytes)) + name_bytes + bytes(payload)

    def _decode_named_payload_wire(self, payload: bytes) -> tuple[str, bytes]:
        if len(payload) < 4:
            raise ValueError("named payload wire is too short")
        name_len = struct.unpack_from(">I", payload, 0)[0]
        if len(payload) < 4 + name_len:
            raise ValueError("named payload wire is truncated")
        name_bytes = payload[4 : 4 + name_len]
        inner_payload = payload[4 + name_len :]
        name = name_bytes.decode("utf-8")
        if not name:
            raise ValueError("named payload wire is missing name")
        return name, inner_payload

    def _encode_typed_procedure_wire(self, *, procedure: str, payload: bytes) -> bytes:
        return self._encode_named_payload_wire(name=procedure, payload=payload)

    def _decode_typed_procedure_wire(self, payload: bytes) -> tuple[str, bytes]:
        return self._decode_named_payload_wire(payload)

    def _typed_procedure_topic(self, procedure: str) -> str:
        return f"{_DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX}{str(procedure).strip()}"

    def _typed_operation_start_topic(self, operation: str) -> str:
        return f"{_DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX}{str(operation).strip()}"

    def _decode_json_control_payload(self, result: dict[str, Any], *, request_id: str, kind: str) -> dict[str, Any]:
        response = result.get("response") or {}
        response_payload = response.get("payload") or b""
        if not isinstance(response_payload, (bytes, bytearray)):
            response_payload = bytes(response_payload)
        try:
            decoded = json.loads(response_payload.decode("utf-8")) if response_payload else {}
        except Exception as exc:
            raise RuntimeError(f"failed to decode {kind} response: {exc}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{kind} response is not an object")
        decoded.setdefault("request_id", request_id)
        decoded.setdefault("ok", False)
        return decoded
