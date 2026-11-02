from __future__ import annotations

import copy
import struct
import uuid
from typing import Any

REQUEST_HEADER_REF = "msg.internal.control.v1/RequestHeader"
RESPONSE_HEADER_REF = "msg.internal.control.v1/ResponseHeader"
GATEWAY_DISPATCH_REPORT_REF = "msg.internal.control.v1/GatewayDispatchReport"
_TIMING_WIRE_MAGIC = b"SDTM"
_TIMING_WIRE_VERSION = 1
_TIMING_WIRE_REQUEST = 1
_TIMING_WIRE_RESPONSE = 2
_TIMING_WIRE_HEADER = struct.Struct("<4sBBHIII")
_CONTROL_WIRE_MAGIC = b"SDTR"
DEFAULT_LATE_POLICY = "execute_immediately"
# Wire code of each late policy; a name's position in _LATE_POLICY_NAMES is its code.
_LATE_POLICY_CODES = {
    "drop": 0,
    "reject": 1,
    "execute_immediately": 2,
}
_LATE_POLICY_NAMES = tuple(_LATE_POLICY_CODES)
_FINAL_STATUS_NAMES = {
    0: "executed",
    1: "executed_late",
    2: "dropped",
    3: "rejected",
    4: "failed",
    5: "canceled",
}


def late_policy_code(late_policy: str | None) -> int:
    """Wire code of a Control late policy; ``None`` selects ``execute_immediately``.

    Any other value than ``drop``, ``reject`` or ``execute_immediately`` raises ValueError: an unknown
    policy never degrades to the most permissive one.
    """
    if late_policy is None:
        return _LATE_POLICY_CODES[DEFAULT_LATE_POLICY]
    code = _LATE_POLICY_CODES.get(late_policy) if isinstance(late_policy, str) else None
    if code is None:
        raise ValueError(
            f"late_policy must be one of drop, reject, execute_immediately; found {late_policy!r}"
        )
    return code


def late_policy_name(code: int) -> str:
    """Late policy named by a received wire code; an unknown code raises ValueError."""
    if 0 <= code < len(_LATE_POLICY_NAMES):
        return _LATE_POLICY_NAMES[code]
    raise ValueError(f"unknown late_policy wire code {code}")


def resolve_late_policy(late_policy: str | None) -> str:
    """Validated late policy name; ``None`` selects ``execute_immediately``."""
    if late_policy is None:
        return DEFAULT_LATE_POLICY
    if type(late_policy) is str and late_policy in _LATE_POLICY_CODES:
        return late_policy
    return _LATE_POLICY_NAMES[late_policy_code(late_policy)]


def build_timing_request_header(
    *,
    now_ns: int,
    target_subsystem: str,
    source_subsystem: str,
    execute_after_ns: int | None = None,
    execute_at_sync_ns: int | None = None,
    priority: int | None = 128,
    late_policy: str | None = "execute_immediately",
    max_lateness_ns: int | None = 1_000_000,
    correlation_id: str | None = None,
    request_id: str | None = None,
    generated_at_sync_ns: int | None = None,
    valid_until_sync_ns: int | None = None,
) -> dict[str, Any]:
    target_name = str(target_subsystem).strip()
    source_name = str(source_subsystem).strip()
    if not target_name:
        raise ValueError("target_subsystem is required")
    if not source_name:
        raise ValueError("source_subsystem is required")
    header: dict[str, Any] = {
        "request_id": request_id or f"req-{uuid.uuid4().hex[:8]}",
        "correlation_id": correlation_id or f"corr-{uuid.uuid4().hex[:8]}",
        "source_subsystem": source_name,
        "target_subsystem": target_name,
        "sent_at_sync_ns": int(now_ns),
        "generated_at_sync_ns": int(generated_at_sync_ns if generated_at_sync_ns is not None else now_ns),
    }
    if priority is not None:
        header["priority"] = int(priority)

    execution: dict[str, Any] = {}
    if execute_at_sync_ns is not None:
        execution["mode"] = "at_time"
        execution["execute_at_sync_ns"] = int(execute_at_sync_ns)
    elif execute_after_ns is not None:
        execution["mode"] = "after_offset"
        execution["execute_after_ns"] = int(execute_after_ns)
    else:
        execution["mode"] = "immediate"
    if max_lateness_ns is not None:
        execution["max_lateness_ns"] = int(max_lateness_ns)
    if valid_until_sync_ns is not None:
        execution["valid_until_sync_ns"] = int(valid_until_sync_ns)
    if late_policy is not None:
        execution["late_policy"] = resolve_late_policy(late_policy)
    header["execution"] = execution
    return header



def normalize_timing_request_header(
    header: dict[str, Any],
    *,
    accepted_at_sync_ns: int,
) -> tuple[dict[str, Any], int]:
    normalized = copy.deepcopy(header)
    execution = normalized.get("execution")
    if not isinstance(execution, dict):
        return normalized, int(accepted_at_sync_ns)
    mode = str(execution.get("mode") or "immediate")
    if "execute_at_sync_ns" not in execution:
        if mode == "after_offset":
            execute_after_ns = int(execution.get("execute_after_ns", 0))
            execution["execute_at_sync_ns"] = int(accepted_at_sync_ns) + execute_after_ns
        elif mode == "immediate":
            execution["execute_at_sync_ns"] = int(accepted_at_sync_ns)
        elif mode != "at_time":
            raise ValueError(f"unsupported timing mode: {mode}")
    return normalized, int(accepted_at_sync_ns)



def build_timing_response_header(
    request_header: dict[str, Any],
    execution_report: dict[str, Any],
    *,
    responded_at_sync_ns: int,
    status_code: int,
    status_message: str,
) -> dict[str, Any]:
    return {
        "request_id": request_header["request_id"],
        "correlation_id": request_header.get("correlation_id"),
        "received_at_sync_ns": execution_report.get("accepted_at_sync_ns"),
        "responded_at_sync_ns": int(responded_at_sync_ns),
        "status_code": int(status_code),
        "status_message": str(status_message),
        "execution_report": execution_report,
    }



def pack_timing_request(
    *,
    request_header: dict[str, Any],
    payload_ref: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "request_header": request_header,
        "payload_ref": str(payload_ref),
        "payload": dict(payload),
    }



def unpack_timing_request(wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not isinstance(wire, dict):
        raise ValueError("timing request wire payload must be an object")
    request_header = wire.get("request_header")
    payload_ref = wire.get("payload_ref")
    payload = wire.get("payload")
    if not isinstance(request_header, dict):
        raise ValueError("timing request is missing request_header")
    if not str(payload_ref or "").strip():
        raise ValueError("timing request is missing payload_ref")
    if not isinstance(payload, dict):
        raise ValueError("timing request is missing payload object")
    return request_header, str(payload_ref).strip(), dict(payload)



def pack_timing_response(
    *,
    response_header: dict[str, Any],
    payload_ref: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "response_header": response_header,
        "payload_ref": str(payload_ref),
        "payload": dict(payload),
    }



def unpack_timing_response(wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not isinstance(wire, dict):
        raise ValueError("timing response wire payload must be an object")
    response_header = wire.get("response_header")
    payload_ref = wire.get("payload_ref")
    payload = wire.get("payload")
    if not isinstance(response_header, dict):
        raise ValueError("timing response is missing response_header")
    if not str(payload_ref or "").strip():
        raise ValueError("timing response is missing payload_ref")
    if not isinstance(payload, dict):
        raise ValueError("timing response is missing payload object")
    return response_header, str(payload_ref).strip(), dict(payload)


def is_timing_wire(payload: Any) -> bool:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return False
    raw = bytes(payload)
    return len(raw) >= _TIMING_WIRE_HEADER.size and raw[:4] == _TIMING_WIRE_MAGIC


def is_control_wire(payload: Any) -> bool:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return False
    raw = bytes(payload)
    return len(raw) >= 8 and raw[:4] == _CONTROL_WIRE_MAGIC


def compact_request_id(request_id: str | None = None) -> tuple[int, str]:
    text = str(request_id or "").strip()
    if not text:
        value = uuid.uuid4().int & ((1 << 64) - 1)
        return value, f"{value:016x}"
    try:
        if text.startswith("0x"):
            value = int(text, 16)
        elif text.isdigit():
            value = int(text, 10)
        else:
            value = int(text, 16)
    except Exception:
        value = uuid.uuid5(uuid.NAMESPACE_OID, text).int & ((1 << 64) - 1)
    return value, f"{value & ((1 << 64) - 1):016x}"


def compact_request_header(
    *,
    request_id_u64: int,
    sent_at_sync_ns: int,
    execute_at_sync_ns: int,
    priority: int,
    late_policy: str,
    max_lateness_ns: int | None,
    generated_at_sync_ns: int | None = None,
    valid_until_sync_ns: int | None = None,
) -> dict[str, Any]:
    execution: dict[str, Any] = {
        "mode": "at_time",
        "execute_at_sync_ns": int(execute_at_sync_ns),
        "late_policy": resolve_late_policy(late_policy),
    }
    if max_lateness_ns is not None:
        execution["max_lateness_ns"] = int(max_lateness_ns)
    if valid_until_sync_ns is not None:
        execution["valid_until_sync_ns"] = int(valid_until_sync_ns)
    return {
        "request_id": f"{int(request_id_u64) & ((1 << 64) - 1):016x}",
        "sent_at_sync_ns": int(sent_at_sync_ns),
        "generated_at_sync_ns": int(generated_at_sync_ns if generated_at_sync_ns is not None else sent_at_sync_ns),
        "priority": int(priority),
        "execution": execution,
    }


def compact_response_header(
    *,
    request_id_u64: int,
    responded_at_sync_ns: int,
    status_code: int,
    accepted_at_sync_ns: int,
    scheduled_for_sync_ns: int,
    dispatched_at_sync_ns: int,
    completed_at_sync_ns: int,
    lateness_ns: int,
    final_status: str,
    sampled_at_sync_ns: int | None = None,
) -> dict[str, Any]:
    execution_report = {
        "accepted_at_sync_ns": int(accepted_at_sync_ns),
        "scheduled_for_sync_ns": int(scheduled_for_sync_ns),
        "dispatched_at_sync_ns": int(dispatched_at_sync_ns),
        "completed_at_sync_ns": int(completed_at_sync_ns),
        "lateness_ns": int(lateness_ns),
        "final_policy": str(final_status),
        "final_status": str(final_status),
    }
    if sampled_at_sync_ns is not None:
        execution_report["sampled_at_sync_ns"] = int(sampled_at_sync_ns)
    return {
        "request_id": f"{int(request_id_u64) & ((1 << 64) - 1):016x}",
        "received_at_sync_ns": int(accepted_at_sync_ns),
        "responded_at_sync_ns": int(responded_at_sync_ns),
        "status_code": int(status_code),
        "status_message": str(final_status),
        "execution_report": execution_report,
    }


def pack_timing_request_wire(
    codec: Any,
    *,
    request_header: dict[str, Any],
    payload_ref: str,
    payload: dict[str, Any],
) -> bytes:
    encode_native = getattr(codec, "encode_timing_request_wire", None)
    if callable(encode_native):
        return encode_native(request_header=request_header, payload_ref=payload_ref, payload=payload)
    return _pack_timing_wire(
        codec,
        kind=_TIMING_WIRE_REQUEST,
        header_ref=REQUEST_HEADER_REF,
        header=request_header,
        payload_ref=payload_ref,
        payload=payload,
    )


def unpack_timing_request_wire(codec: Any, wire: bytes) -> tuple[dict[str, Any], str, dict[str, Any]]:
    decode_native = getattr(codec, "decode_timing_request_wire", None)
    if callable(decode_native):
        return decode_native(wire)
    return _unpack_timing_wire(codec, wire, expected_kind=_TIMING_WIRE_REQUEST, header_ref=REQUEST_HEADER_REF)


def pack_timing_response_wire(
    codec: Any,
    *,
    response_header: dict[str, Any],
    payload_ref: str,
    payload: dict[str, Any],
) -> bytes:
    encode_native = getattr(codec, "encode_timing_response_wire", None)
    if callable(encode_native):
        return encode_native(response_header=response_header, payload_ref=payload_ref, payload=payload)
    return _pack_timing_wire(
        codec,
        kind=_TIMING_WIRE_RESPONSE,
        header_ref=RESPONSE_HEADER_REF,
        header=response_header,
        payload_ref=payload_ref,
        payload=payload,
    )


def unpack_timing_response_wire(codec: Any, wire: bytes) -> tuple[dict[str, Any], str, dict[str, Any]]:
    decode_native = getattr(codec, "decode_timing_response_wire", None)
    if callable(decode_native):
        return decode_native(wire)
    return _unpack_timing_wire(codec, wire, expected_kind=_TIMING_WIRE_RESPONSE, header_ref=RESPONSE_HEADER_REF)


def _pack_timing_wire(
    codec: Any,
    *,
    kind: int,
    header_ref: str,
    header: dict[str, Any],
    payload_ref: str,
    payload: dict[str, Any],
) -> bytes:
    header_bytes = codec.encode_message(header_ref, header)
    payload_ref_bytes = str(payload_ref).encode("utf-8")
    payload_bytes = codec.encode_message(str(payload_ref), payload)
    prefix = _TIMING_WIRE_HEADER.pack(
        _TIMING_WIRE_MAGIC,
        _TIMING_WIRE_VERSION,
        int(kind),
        0,
        len(header_bytes),
        len(payload_ref_bytes),
        len(payload_bytes),
    )
    return prefix + header_bytes + payload_ref_bytes + payload_bytes


def _unpack_timing_wire(
    codec: Any,
    wire: bytes,
    *,
    expected_kind: int,
    header_ref: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    raw = bytes(wire)
    if len(raw) < _TIMING_WIRE_HEADER.size:
        raise ValueError("timing wire payload is too short")
    magic, version, kind, _reserved, header_len, ref_len, payload_len = _TIMING_WIRE_HEADER.unpack_from(raw, 0)
    if magic != _TIMING_WIRE_MAGIC:
        raise ValueError("timing wire payload has invalid magic")
    if version != _TIMING_WIRE_VERSION:
        raise ValueError(f"unsupported timing wire version: {version}")
    if kind != expected_kind:
        raise ValueError(f"unexpected timing wire kind: {kind}")
    cursor = _TIMING_WIRE_HEADER.size
    end_header = cursor + int(header_len)
    end_ref = end_header + int(ref_len)
    end_payload = end_ref + int(payload_len)
    if end_payload > len(raw):
        raise ValueError("timing wire payload is truncated")
    header_bytes = raw[cursor:end_header]
    payload_ref_bytes = raw[end_header:end_ref]
    payload_bytes = raw[end_ref:end_payload]
    payload_ref = payload_ref_bytes.decode("utf-8")
    if not payload_ref:
        raise ValueError("timing wire payload is missing payload_ref")
    header = codec.decode_message(header_ref, header_bytes)
    payload = codec.decode_message(payload_ref, payload_bytes)
    if not isinstance(header, dict):
        raise ValueError("timing wire header must decode to an object")
    if not isinstance(payload, dict):
        raise ValueError("timing wire payload must decode to an object")
    return header, payload_ref, payload


__all__ = [
    "REQUEST_HEADER_REF",
    "RESPONSE_HEADER_REF",
    "GATEWAY_DISPATCH_REPORT_REF",
    "build_timing_request_header",
    "normalize_timing_request_header",
    "build_timing_response_header",
    "compact_request_header",
    "compact_request_id",
    "compact_response_header",
    "is_timing_wire",
    "is_control_wire",
    "late_policy_code",
    "late_policy_name",
    "resolve_late_policy",
    "pack_timing_request",
    "pack_timing_request_wire",
    "unpack_timing_request",
    "unpack_timing_request_wire",
    "pack_timing_response",
    "pack_timing_response_wire",
    "unpack_timing_response",
    "unpack_timing_response_wire",
]
