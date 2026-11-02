"""SubsystemRuntime timing-header helpers and Control client transactions."""

from __future__ import annotations

from typing import Any

from .codec_lifecycle import codec_activity
from .timing import (
    build_timing_request_header as _build_timing_request_header,
    build_timing_response_header as _build_timing_response_header,
    compact_request_header as _compact_request_header,
    compact_request_id as _compact_request_id,
    compact_response_header as _compact_response_header,
    normalize_timing_request_header as _normalize_timing_request_header,
    pack_timing_request as _pack_timing_request,
    pack_timing_response as _pack_timing_response,
    resolve_late_policy as _resolve_late_policy,
    unpack_timing_request as _unpack_timing_request,
    unpack_timing_response as _unpack_timing_response,
)


class _TimingHeaderMixin:
    """Timing header packing and Control client transactions of SubsystemRuntime."""

    def build_timing_request_header(
        self,
        *,
        target_subsystem: str,
        source_subsystem: str | None = None,
        execute_after_ns: int | None = None,
        execute_at_sync_ns: int | None = None,
        priority: int | None = 128,
        late_policy: str | None = "execute_immediately",
        max_lateness_ns: int | None = 1_000_000,
        correlation_id: str | None = None,
        request_id: str | None = None,
        generated_at_sync_ns: int | None = None,
        valid_for_ns: int | None = None,
        valid_until_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        """Build a timing header, anchoring relative validity at generation time.

        ``valid_for_ns`` is measured from ``generated_at_sync_ns`` when the
        caller supplies it, or from this call's current synchronized time
        otherwise. ``valid_until_sync_ns`` is already absolute and is preserved.
        """
        now_ns = self.now_ns()
        if valid_for_ns is not None and valid_until_sync_ns is not None:
            raise ValueError("valid_for_ns and valid_until_sync_ns are mutually exclusive")
        resolved_valid_until = valid_until_sync_ns
        if valid_for_ns is not None:
            generated_at_value = int(
                generated_at_sync_ns if generated_at_sync_ns is not None else now_ns
            )
            resolved_valid_until = generated_at_value + int(valid_for_ns)
        return _build_timing_request_header(
            now_ns=now_ns,
            target_subsystem=target_subsystem,
            source_subsystem=source_subsystem or self._subsystem_name,
            execute_after_ns=execute_after_ns,
            execute_at_sync_ns=execute_at_sync_ns,
            priority=priority,
            late_policy=late_policy,
            max_lateness_ns=max_lateness_ns,
            correlation_id=correlation_id,
            request_id=request_id,
            generated_at_sync_ns=generated_at_sync_ns,
            valid_until_sync_ns=resolved_valid_until,
        )

    def normalize_timing_request_header(
        self,
        header: dict[str, Any],
        *,
        accepted_at_sync_ns: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        return _normalize_timing_request_header(
            header,
            accepted_at_sync_ns=int(accepted_at_sync_ns if accepted_at_sync_ns is not None else self.now_ns()),
        )

    def build_timing_response_header(
        self,
        request_header: dict[str, Any],
        execution_report: dict[str, Any],
        *,
        responded_at_sync_ns: int | None = None,
        status_code: int,
        status_message: str,
    ) -> dict[str, Any]:
        return _build_timing_response_header(
            request_header,
            execution_report,
            responded_at_sync_ns=int(responded_at_sync_ns if responded_at_sync_ns is not None else self.now_ns()),
            status_code=status_code,
            status_message=status_message,
        )

    def pack_timing_request(
        self,
        *,
        request_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return _pack_timing_request(request_header=request_header, payload_ref=payload_ref, payload=payload)

    def unpack_timing_request(self, wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        return _unpack_timing_request(wire)

    def pack_timing_response(
        self,
        *,
        response_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return _pack_timing_response(response_header=response_header, payload_ref=payload_ref, payload=payload)

    def unpack_timing_response(self, wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        return _unpack_timing_response(wire)

    @codec_activity
    def _transact_control(
        self,
        *,
        target: str,
        topic: str,
        request_type: str,
        response_type: str | None,
        payload: dict[str, Any],
        execute_after_ns: int | None = None,
        execute_at_sync_ns: int | None = None,
        priority: int | None = 128,
        late_policy: str | None = "execute_immediately",
        max_lateness_ns: int | None = 1_000_000,
        period_ms: float = 0.0,
        deadline_ms: float = 1000.0,
        last_tick_ns: int | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        generated_at_sync_ns: int | None = None,
        valid_for_ns: int | None = None,
        valid_until_sync_ns: int | None = None,
        details: bool = False,
        decode_response: bool = True,
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        late_policy_value = _resolve_late_policy(late_policy)
        deadline = _deadline if _deadline is not None else self._control_call_deadline(deadline_ms)
        generated_at_value = int(
            generated_at_sync_ns if generated_at_sync_ns is not None else self.now_ns()
        )
        codec = self._get_rust_message_codec()
        if valid_for_ns is not None and valid_until_sync_ns is not None:
            raise ValueError("valid_for_ns and valid_until_sync_ns are mutually exclusive")
        resolved_valid_until = valid_until_sync_ns
        if valid_for_ns is not None:
            resolved_valid_until = generated_at_value + int(valid_for_ns)
        request_payload = codec.encode_message(request_type, payload)
        sent_at_sync_ns = self.now_ns()
        execute_at_value = int(
            execute_at_sync_ns
            if execute_at_sync_ns is not None
            else sent_at_sync_ns + int(execute_after_ns or 0)
        )
        request_id_u64, request_id_text = _compact_request_id(request_id)
        priority_value = int(priority if priority is not None else 128)
        max_lateness_value = None if max_lateness_ns is None else int(max_lateness_ns)
        detailed = bool(details)
        decode_response = bool(decode_response)
        user_header: dict[str, Any] | None = None
        normalized_header: dict[str, Any] | None = None
        if detailed:
            user_header = {
                "request_id": request_id_text,
                "generated_at_sync_ns": generated_at_value,
                "sent_at_sync_ns": sent_at_sync_ns,
                "priority": priority_value,
                "execution": {
                    "mode": "after_offset" if execute_at_sync_ns is None else "at_time",
                    "execute_at_sync_ns": execute_at_value,
                    "late_policy": late_policy_value,
                },
            }
            if execute_at_sync_ns is None:
                user_header["execution"]["execute_after_ns"] = int(execute_after_ns or 0)
            if max_lateness_value is not None:
                user_header["execution"]["max_lateness_ns"] = max_lateness_value
            if resolved_valid_until is not None:
                user_header["execution"]["valid_until_sync_ns"] = int(resolved_valid_until)
            normalized_header = _compact_request_header(
                request_id_u64=request_id_u64,
                generated_at_sync_ns=generated_at_value,
                sent_at_sync_ns=sent_at_sync_ns,
                execute_at_sync_ns=execute_at_value,
                valid_until_sync_ns=resolved_valid_until,
                priority=priority_value,
                late_policy=late_policy_value,
                max_lateness_ns=max_lateness_value,
            )

        def expired_request_result() -> dict[str, Any]:
            result: dict[str, Any] = {
                "ok": False,
                "topic": topic,
                "target": target,
                "request_type": str(request_type),
                "response_type": response_type,
                "runtime_accepted_at_sync_ns": sent_at_sync_ns,
                "error_code": "expired_timed_request",
                "error_message": f"Control '{topic}' rejected an expired timed request before transmission",
                "transport": {
                    "ok": False,
                    "error": "expired_timed_request",
                    "timeout": False,
                    "overrun": False,
                },
            }
            if detailed:
                result["payload"] = dict(payload)
                result["user_header"] = user_header
                result["normalized_header"] = normalized_header
            return result

        if resolved_valid_until is not None and self.now_ns() > int(resolved_valid_until):
            return expired_request_result()

        wire = codec.encode_control_request_wire(
            request_id=request_id_u64,
            generated_at_sync_ns=generated_at_value,
            sent_at_sync_ns=sent_at_sync_ns,
            execute_at_sync_ns=execute_at_value,
            valid_until_sync_ns=resolved_valid_until,
            max_lateness_ns=max_lateness_value,
            priority=priority_value,
            late_policy=late_policy_value,
            payload=request_payload,
        )
        if resolved_valid_until is not None and self.now_ns() > int(resolved_valid_until):
            return expired_request_result()

        result = (self._client.transact_control_result if result_ready
         else self._client.transact_control)(
            target=target,
            topic=topic,
            type="bytes",
            value=wire,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns if last_tick_ns is not None else self.now_ns(),
            priority=priority_value,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        summary: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "topic": topic,
            "target": target,
            "request_type": str(request_type),
            "response_type": response_type,
            "runtime_accepted_at_sync_ns": sent_at_sync_ns,
            "transport": {
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "timeout": bool(result.get("timeout", False)),
                "overrun": bool(result.get("overrun", False)),
                "rtt_ns": result.get("rtt_ns"),
                "rtt_ms": result.get("rtt_ms"),
            },
        }
        if detailed:
            summary["payload"] = dict(payload)
            summary["user_header"] = user_header
            summary["normalized_header"] = normalized_header
        response = result.get("response")
        if isinstance(response, dict):
            if detailed:
                summary["transport"]["response"] = {key: value for key, value in response.items() if key != "payload"}
            payload_bytes = response.get("payload") or b""
            if not isinstance(payload_bytes, (bytes, bytearray)):
                payload_bytes = bytes(payload_bytes)
            if payload_bytes:
                if detailed:
                    summary["transport"]["payload_hex"] = bytes(payload_bytes).hex()
                try:
                    response_meta, response_payload_bytes = codec.decode_control_response_wire(payload_bytes)
                    response_payload = {}
                    if response_type and decode_response:
                        response_payload = codec.decode_message(response_type, payload=response_payload_bytes)
                    if detailed:
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
                        summary["response_payload"] = {
                            "response_header": response_header,
                            "payload": response_payload,
                        }
                        summary["timing_response"] = {
                            "response_header": response_header,
                            "payload": response_payload,
                        }
                    if response_type:
                        if response_meta["status_code"] != 0:
                            summary["ok"] = False
                            summary["error_code"] = "control_response_error"
                            summary["error_message"] = f"Control response status={response_meta['final_status']}"
                        elif decode_response:
                            summary["result"] = response_payload
                except Exception as exc:
                    if not result.get("ok"):
                        try:
                            response_text = bytes(payload_bytes).decode("utf-8").strip()
                        except Exception:
                            response_text = ""
                        if response_text:
                            summary["ok"] = False
                            summary["error_code"] = "control_response_error"
                            summary["error_message"] = response_text
                        else:
                            summary["ok"] = False
                            summary["error_code"] = "invalid_response"
                            summary["error_message"] = f"failed to decode Control response: {exc}"
                    else:
                        summary["ok"] = False
                        summary["error_code"] = "invalid_response"
                        summary["error_message"] = f"failed to decode Control response: {exc}"
        if summary.get("ok"):
            self._check_control_deadline(deadline)
        return summary

    @codec_activity
    def _transact_typed_control(
        self,
        *,
        target: str,
        topic: str,
        request_type: str,
        response_type: str,
        payload: dict[str, Any],
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(deadline_ms)
        codec = self._get_rust_message_codec()
        request_payload = codec.encode_message(request_type, payload)
        self._check_control_deadline(deadline)
        result = (self._client.transact_control_result if result_ready
         else self._client.transact_control)(
            target=target,
            topic=topic,
            type="bytes",
            value=request_payload,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            **({"_deadline": deadline} if deadline is not None else {}),
        )
        summary: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "topic": topic,
            "target": target,
            "request_type": request_type,
            "response_type": response_type,
            "payload": dict(payload),
            "transport": {
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "timeout": bool(result.get("timeout", False)),
                "overrun": bool(result.get("overrun", False)),
                "rtt_ns": result.get("rtt_ns"),
                "rtt_ms": result.get("rtt_ms"),
            },
        }
        response = result.get("response")
        if isinstance(response, dict):
            summary["transport"]["response"] = {key: value for key, value in response.items() if key != "payload"}
            payload_bytes = response.get("payload") or b""
            if not isinstance(payload_bytes, (bytes, bytearray)):
                payload_bytes = bytes(payload_bytes)
            if payload_bytes:
                summary["transport"]["payload_hex"] = bytes(payload_bytes).hex()
                try:
                    summary["result"] = codec.decode_message(response_type, payload=payload_bytes)
                except Exception as exc:
                    summary["ok"] = False
                    summary["error_code"] = "invalid_response"
                    summary["error_message"] = f"failed to decode Control response: {exc}"
        if summary.get("ok"):
            self._check_control_deadline(deadline)
        return summary
