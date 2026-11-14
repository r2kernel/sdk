from __future__ import annotations

import array
import ctypes
import json
import struct
import threading
from typing import Any

from r2kernel.core.timing import late_policy_code, late_policy_name
from r2kernel.ffi import R2KernelCore

_BUFFER_TOO_SMALL = -4
_ARRAY_TYPE_IDS = {
    "int32": 9,
    "uint32": 10,
    "int64": 11,
    "float32": 12,
    "float64": 13,
    "bool": 14,
}
_ARRAY_ITEM_SIZES = {
    "int32": 4,
    "uint32": 4,
    "int64": 8,
    "float32": 4,
    "float64": 8,
    "bool": 1,
}
_ARRAY_TYPECODES = {
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "float32": "f",
    "float64": "d",
}
_FINAL_STATUS_CODES = {
    "executed": 0,
    "executed_late": 1,
    "dropped": 2,
    "rejected": 3,
    "failed": 4,
    "canceled": 5,
}
_FINAL_STATUS_NAMES = {value: key for key, value in _FINAL_STATUS_CODES.items()}
_COMMON_WRAPPER_SCALARS = {
    "msg.core.common.primitive.v1/BoolValue": "bool",
    "msg.core.common.primitive.v1/Int32Value": "int32",
    "msg.core.common.primitive.v1/UInt32Value": "uint32",
    "msg.core.common.primitive.v1/Int64Value": "int64",
    "msg.core.common.primitive.v1/Float32Value": "float32",
    "msg.core.common.primitive.v1/Float64Value": "float64",
    "msg.core.common.primitive.v1/StringValue": "string",
    "msg.core.common.primitive.v1/BytesValue": "bytes",
    "msg.core.common.primitive.v1/TimestampNs": "int64",
    "msg.core.common.primitive.v1/DurationNs": "int64",
}


LOG_ID_CODEC = "Codec"
UNPREPARED_CODEC_WARNING = (
    "Unprepared message codec in use (before initialize() or without a managed Subsystem): every call "
    "re-resolves the message against the spec roots. Initialize the Subsystem, or use "
    "PreparedSchemaContext/PreparedCodec, for repeated use."
)
_unprepared_warning_lock = threading.Lock()
_unprepared_warning_emitted = False


def warn_unprepared_codec_once(logger: Any) -> bool:
    """Log UNPREPARED_CODEC_WARNING once per process; returns True when this call logged it."""
    global _unprepared_warning_emitted
    with _unprepared_warning_lock:
        if _unprepared_warning_emitted:
            return False
        _unprepared_warning_emitted = True
    logger.warning(LOG_ID_CODEC, UNPREPARED_CODEC_WARNING)
    return True


_GENERIC_INTO_FUNCTIONS = (
    "r2kernel_codec_encode_message_json_into",
    "r2kernel_codec_decode_message_json_into",
)


def _generic_codec_output(lib):
    """Bind the single-conversion generic ABI and create a native codec output for the calling thread."""
    from .prepared_codec import _Workspace, _bind

    _bind(lib)
    for name in _GENERIC_INTO_FUNCTIONS:
        try:
            function = getattr(lib, name)
        except AttributeError as exc:
            raise RuntimeError(f"Generic message codec requires the updated r2kernel-comm library ({name})") from exc
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
        function.restype = ctypes.c_int
    return _Workspace(lib)


class RustMessageCodec:
    """Generic (unprepared) message codec: every call re-resolves the message against the spec roots.

    Each generic encode/decode performs exactly one native conversion
    (`r2kernel_codec_{encode,decode}_message_json_into`). For repeated use prefer an initialized
    Subsystem or `PreparedSchemaContext`/`PreparedCodec`, which resolve the schema once.
    """

    def __init__(self) -> None:
        self._core = R2KernelCore()
        self._lib = self._core.lib
        self._generic_local = threading.local()

    def get_message_schema(self, message_ref: str) -> dict[str, Any]:
        api_schema = self._api_local_message_schema(str(message_ref))
        if api_schema is not None:
            return api_schema
        encoded = self._call_json(
            self._lib.r2kernel_schema_get_message_json,
            str(message_ref).encode("utf-8"),
        )
        return json.loads(encoded.decode("utf-8"))

    def get_message_set(self, namespace: str) -> dict[str, Any]:
        encoded = self._call_json(
            self._lib.r2kernel_schema_get_message_set_json,
            str(namespace).encode("utf-8"),
        )
        return json.loads(encoded.decode("utf-8"))

    def encode_message(self, message_ref: str, payload: dict[str, Any]) -> bytes:
        if str(message_ref).strip().lower() == "bytes":
            return json.dumps(self._json_ready(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        fast = self._try_encode_common_wrapper(str(message_ref), payload)
        if fast is not None:
            return fast
        if self._api_local_message_schema(str(message_ref)) is not None:
            return json.dumps(self._json_ready(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded_json = json.dumps(self._json_ready(payload)).encode("utf-8")
        return self._convert_generic(
            "r2kernel_codec_encode_message_json_into", str(message_ref), encoded_json, "Rust message encode"
        )

    def decode_message(self, message_ref: str, payload: bytes) -> dict[str, Any]:
        if str(message_ref).strip().lower() == "bytes":
            if not payload:
                return {}
            decoded = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(decoded, dict):
                raise RuntimeError("bytes payload must decode to a JSON object")
            return decoded
        fast = self._try_decode_common_wrapper(str(message_ref), bytes(payload))
        if fast is not None:
            return fast
        if self._api_local_message_schema(str(message_ref)) is not None:
            if not payload:
                return {}
            decoded = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(decoded, dict):
                raise RuntimeError(f"{message_ref} payload must decode to a JSON object")
            return decoded
        encoded = self._convert_generic(
            "r2kernel_codec_decode_message_json_into", str(message_ref), bytes(payload), "Rust message decode"
        )
        decoded = json.loads(encoded.decode("utf-8"))
        return self._restore_bytes(str(message_ref), decoded)

    def _convert_generic(self, function_name: str, message_ref: str, data: bytes, operation: str) -> bytes:
        """One native generic conversion into this thread's codec output; the result is copied out."""
        output = self._generic_output()
        function = getattr(self._lib, function_name)
        rc = function(message_ref.encode("utf-8"), data, len(data), output.handle)
        if rc != 0:
            raise RuntimeError(f"{operation} failed rc={rc}")
        return output.byte_result()

    def _generic_output(self):
        local = self._generic_local
        output = getattr(local, "output", None)
        if output is None:
            output = local.output = _generic_codec_output(self._lib)
        return output

    def encode_timing_request_wire(
        self,
        *,
        request_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> bytes:
        header_json = json.dumps(request_header).encode("utf-8")
        payload_json = json.dumps(self._json_ready(payload)).encode("utf-8")
        return self._call_bytes(
            self._lib.r2kernel_codec_encode_timing_request_json,
            header_json,
            len(header_json),
            str(payload_ref).encode("utf-8"),
            payload_json,
            len(payload_json),
        )

    def decode_timing_request_wire(self, wire: bytes) -> tuple[dict[str, Any], str, dict[str, Any]]:
        header_json, payload_ref, payload_json = self._call_timing_decode(
            self._lib.r2kernel_codec_decode_timing_request_json,
            bytes(wire),
            len(wire),
        )
        return json.loads(header_json.decode("utf-8")), payload_ref.decode("utf-8"), json.loads(payload_json.decode("utf-8"))

    def encode_timing_response_wire(
        self,
        *,
        response_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> bytes:
        header_json = json.dumps(response_header).encode("utf-8")
        payload_json = json.dumps(self._json_ready(payload)).encode("utf-8")
        return self._call_bytes(
            self._lib.r2kernel_codec_encode_timing_response_json,
            header_json,
            len(header_json),
            str(payload_ref).encode("utf-8"),
            payload_json,
            len(payload_json),
        )

    def decode_timing_response_wire(self, wire: bytes) -> tuple[dict[str, Any], str, dict[str, Any]]:
        header_json, payload_ref, payload_json = self._call_timing_decode(
            self._lib.r2kernel_codec_decode_timing_response_json,
            bytes(wire),
            len(wire),
        )
        return json.loads(header_json.decode("utf-8")), payload_ref.decode("utf-8"), json.loads(payload_json.decode("utf-8"))

    def encode_control_request_wire(
        self,
        *,
        request_id: int,
        sent_at_sync_ns: int,
        execute_at_sync_ns: int,
        max_lateness_ns: int | None,
        priority: int,
        late_policy: str | None,
        payload: bytes,
        generated_at_sync_ns: int | None = None,
        valid_until_sync_ns: int | None = None,
    ) -> bytes:
        resolved_policy = late_policy_code(late_policy)
        return self._call_bytes(
            self._lib.r2kernel_codec_encode_control_request_v2,
            int(request_id),
            int(generated_at_sync_ns if generated_at_sync_ns is not None else sent_at_sync_ns),
            int(sent_at_sync_ns),
            int(execute_at_sync_ns),
            int(valid_until_sync_ns if valid_until_sync_ns is not None else 0),
            int(max_lateness_ns if max_lateness_ns is not None else -1),
            int(priority),
            int(resolved_policy),
            bytes(payload),
            len(payload),
        )

    def decode_control_request_wire(self, wire: bytes) -> tuple[dict[str, Any], bytes]:
        request_id = ctypes.c_uint64()
        generated_at_sync_ns = ctypes.c_int64()
        sent_at_sync_ns = ctypes.c_int64()
        execute_at_sync_ns = ctypes.c_int64()
        valid_until_sync_ns = ctypes.c_int64()
        max_lateness_ns = ctypes.c_int64()
        priority = ctypes.c_uint16()
        late_policy = ctypes.c_uint8()
        payload = self._call_control_decode(
            self._lib.r2kernel_codec_decode_control_request_v2,
            bytes(wire),
            request_id,
            generated_at_sync_ns,
            sent_at_sync_ns,
            execute_at_sync_ns,
            valid_until_sync_ns,
            max_lateness_ns,
            priority,
            late_policy,
        )
        header = {
            "request_id_u64": int(request_id.value),
            "generated_at_sync_ns": int(generated_at_sync_ns.value),
            "sent_at_sync_ns": int(sent_at_sync_ns.value),
            "execute_at_sync_ns": int(execute_at_sync_ns.value),
            "valid_until_sync_ns": None if int(valid_until_sync_ns.value) <= 0 else int(valid_until_sync_ns.value),
            "max_lateness_ns": None if int(max_lateness_ns.value) < 0 else int(max_lateness_ns.value),
            "priority": int(priority.value),
            "late_policy": late_policy_name(late_policy.value),
        }
        return header, payload

    def encode_control_response_wire(
        self,
        *,
        request_id: int,
        responded_at_sync_ns: int,
        accepted_at_sync_ns: int,
        scheduled_for_sync_ns: int,
        dispatched_at_sync_ns: int,
        completed_at_sync_ns: int,
        lateness_ns: int,
        status_code: int,
        final_status: str,
        payload: bytes,
        sampled_at_sync_ns: int | None = None,
    ) -> bytes:
        resolved_status = _FINAL_STATUS_CODES.get(str(final_status).strip(), _FINAL_STATUS_CODES["failed"])
        return self._call_bytes(
            self._lib.r2kernel_codec_encode_control_response_v2,
            int(request_id),
            int(responded_at_sync_ns),
            int(accepted_at_sync_ns),
            int(scheduled_for_sync_ns),
            int(dispatched_at_sync_ns),
            int(sampled_at_sync_ns if sampled_at_sync_ns is not None else 0),
            int(completed_at_sync_ns),
            int(lateness_ns),
            int(status_code),
            int(resolved_status),
            bytes(payload),
            len(payload),
        )

    def decode_control_response_wire(self, wire: bytes) -> tuple[dict[str, Any], bytes]:
        request_id = ctypes.c_uint64()
        responded_at_sync_ns = ctypes.c_int64()
        accepted_at_sync_ns = ctypes.c_int64()
        scheduled_for_sync_ns = ctypes.c_int64()
        dispatched_at_sync_ns = ctypes.c_int64()
        sampled_at_sync_ns = ctypes.c_int64()
        completed_at_sync_ns = ctypes.c_int64()
        lateness_ns = ctypes.c_int64()
        status_code = ctypes.c_int32()
        final_status = ctypes.c_uint8()
        payload = self._call_control_decode(
            self._lib.r2kernel_codec_decode_control_response_v2,
            bytes(wire),
            request_id,
            responded_at_sync_ns,
            accepted_at_sync_ns,
            scheduled_for_sync_ns,
            dispatched_at_sync_ns,
            sampled_at_sync_ns,
            completed_at_sync_ns,
            lateness_ns,
            status_code,
            final_status,
        )
        header = {
            "request_id_u64": int(request_id.value),
            "responded_at_sync_ns": int(responded_at_sync_ns.value),
            "accepted_at_sync_ns": int(accepted_at_sync_ns.value),
            "scheduled_for_sync_ns": int(scheduled_for_sync_ns.value),
            "dispatched_at_sync_ns": int(dispatched_at_sync_ns.value),
            "sampled_at_sync_ns": None if int(sampled_at_sync_ns.value) <= 0 else int(sampled_at_sync_ns.value),
            "completed_at_sync_ns": int(completed_at_sync_ns.value),
            "lateness_ns": int(lateness_ns.value),
            "status_code": int(status_code.value),
            "final_status": _FINAL_STATUS_NAMES.get(int(final_status.value), "failed"),
        }
        return header, payload

    def encode_primitive_vector(self, element_type: str, values: Any) -> bytes:
        normalized = self._normalize_array_type(element_type)
        raw, count = self._coerce_primitive_vector_input(normalized, values)
        return self._call_with_output_buffer(
            self._lib.r2kernel_codec_encode_primitive_vector,
            _ARRAY_TYPE_IDS[normalized],
            raw,
            len(raw),
            count,
            initial_capacity=max(256, len(raw) + 4),
            operation="Rust primitive vector encode",
        )

    def decode_primitive_vector_buffer(self, element_type: str, payload: bytes) -> tuple[bytes, int]:
        normalized = self._normalize_array_type(element_type)
        out_len = ctypes.c_size_t()
        out_count = ctypes.c_size_t()
        out_cap = max(1, len(payload) - 4)
        for attempt in range(2):
            out_buf = (ctypes.c_uint8 * out_cap)()
            rc = self._lib.r2kernel_codec_decode_primitive_vector(
                _ARRAY_TYPE_IDS[normalized],
                bytes(payload),
                len(payload),
                out_buf,
                ctypes.sizeof(out_buf),
                ctypes.byref(out_len),
                ctypes.byref(out_count),
            )
            if rc == _BUFFER_TOO_SMALL:
                required_len = self._required_output_capacity(
                    out_len,
                    out_cap,
                    "Rust primitive vector decode",
                )
                if attempt == 1:
                    raise RuntimeError(
                        "Rust primitive vector decode remained buffer-too-small "
                        f"after retry (required={required_len})"
                    )
                out_cap = required_len
                continue
            if rc != 0:
                raise RuntimeError(f"failed to decode primitive vector via Rust codec rc={rc}")
            return (
                self._copy_native_output(out_buf, out_cap, out_len, "Rust primitive vector decode"),
                int(out_count.value),
            )
        raise RuntimeError("Rust primitive vector decode did not complete")

    def decode_primitive_vector(self, element_type: str, payload: bytes) -> list[Any]:
        normalized = self._normalize_array_type(element_type)
        raw, count = self.decode_primitive_vector_buffer(normalized, payload)
        if normalized == "bool":
            return [bool(value) for value in raw[:count]]
        data = array.array(_ARRAY_TYPECODES[normalized])
        data.frombytes(raw)
        return data.tolist()

    def _normalize_array_type(self, element_type: str) -> str:
        normalized = str(element_type).strip().lower()
        if normalized == "double":
            return "float64"
        if normalized == "boolean":
            return "bool"
        if normalized not in _ARRAY_TYPE_IDS:
            raise ValueError(f"unsupported primitive vector element type: {element_type}")
        return normalized

    def _try_encode_common_wrapper(self, message_ref: str, payload: dict[str, Any]) -> bytes | None:
        if message_ref == "msg.core.common.primitive.v1/Empty":
            if payload and any(value is not None for value in payload.values()):
                raise RuntimeError("msg.core.common.primitive.v1/Empty does not accept fields")
            return b""
        scalar_type = _COMMON_WRAPPER_SCALARS.get(message_ref)
        if scalar_type is None:
            return None
        if not isinstance(payload, dict) or "value" not in payload:
            raise RuntimeError(f"{message_ref} requires a 'value' field")
        scalar = self._encode_common_scalar(scalar_type, payload.get("value"))
        return len(scalar).to_bytes(4, byteorder="little", signed=False) + scalar

    def _try_decode_common_wrapper(self, message_ref: str, payload: bytes) -> dict[str, Any] | None:
        if message_ref == "msg.core.common.primitive.v1/Empty":
            if payload:
                raise RuntimeError("msg.core.common.primitive.v1/Empty payload must be empty")
            return {}
        scalar_type = _COMMON_WRAPPER_SCALARS.get(message_ref)
        if scalar_type is None:
            return None
        raw = bytes(payload)
        if len(raw) < 4:
            raise RuntimeError(f"{message_ref} payload is truncated")
        field_len = int.from_bytes(raw[:4], byteorder="little", signed=False)
        if len(raw) != 4 + field_len:
            raise RuntimeError(f"{message_ref} payload length mismatch")
        return {"value": self._decode_common_scalar(scalar_type, raw[4:])}

    def _api_local_message_schema(self, message_ref: str) -> dict[str, Any] | None:
        namespace, separator, message_name = str(message_ref or "").partition("/")
        if not separator or not namespace.startswith("api.") or not message_name:
            return None
        try:
            from r2kernel.contract.loader import load_api_contract_spec

            api_contract = load_api_contract_spec(namespace)
        except Exception:
            return None
        message = api_contract.messages.get(message_name)
        if message is None:
            return None
        return {
            "namespace": namespace,
            "name": message.name,
            "fields": [
                {
                    "name": field.name,
                    "type": field.type,
                    "optional": field.optional,
                    "description": field.description,
                }
                for field in message.fields
            ],
            "encoding": "json",
        }

    @classmethod
    def _json_ready(cls, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).hex()
        if isinstance(value, dict):
            return {str(key): cls._json_ready(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_ready(item) for item in value]
        return value

    def _restore_bytes(self, message_ref: str, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        try:
            schema = self.get_message_schema(message_ref)
        except Exception:
            return payload
        restored = dict(payload)
        for field in schema.get("fields") or []:
            name = str(field.get("name") or "")
            type_name = str(field.get("type_name") or field.get("type") or "")
            if not name or name not in restored:
                continue
            restored[name] = self._restore_field_bytes(type_name, restored[name])
        return restored

    def _restore_field_bytes(self, type_name: str, value: Any) -> Any:
        normalized = str(type_name).strip()
        if normalized == "bytes" and isinstance(value, str):
            try:
                return bytes.fromhex(value)
            except ValueError:
                return value
        if normalized.startswith("vector<") and normalized.endswith(">") and isinstance(value, list):
            inner = normalized[7:-1].strip()
            return [self._restore_field_bytes(inner, item) for item in value]
        if "/" in normalized and isinstance(value, dict):
            return self._restore_bytes(normalized, value)
        return value

    def _encode_common_scalar(self, scalar_type: str, value: Any) -> bytes:
        if scalar_type == "bool":
            return b"\x01" if bool(value) else b"\x00"
        if scalar_type == "int32":
            return struct.pack("<i", int(value))
        if scalar_type == "uint32":
            return struct.pack("<I", int(value))
        if scalar_type == "int64":
            return struct.pack("<q", int(value))
        if scalar_type == "float32":
            return struct.pack("<f", float(value))
        if scalar_type == "float64":
            return struct.pack("<d", float(value))
        if scalar_type == "string":
            return str(value).encode("utf-8")
        if scalar_type == "bytes":
            if isinstance(value, str):
                return bytes.fromhex(value)
            return bytes(value)
        raise RuntimeError(f"unsupported common wrapper scalar type: {scalar_type}")

    def _decode_common_scalar(self, scalar_type: str, payload: bytes) -> Any:
        raw = bytes(payload)
        if scalar_type == "bool":
            if len(raw) != 1:
                raise RuntimeError("bool wrapper payload length mismatch")
            return raw[0] != 0
        if scalar_type == "int32":
            if len(raw) != 4:
                raise RuntimeError("int32 wrapper payload length mismatch")
            return struct.unpack("<i", raw)[0]
        if scalar_type == "uint32":
            if len(raw) != 4:
                raise RuntimeError("uint32 wrapper payload length mismatch")
            return struct.unpack("<I", raw)[0]
        if scalar_type == "int64":
            if len(raw) != 8:
                raise RuntimeError("int64 wrapper payload length mismatch")
            return struct.unpack("<q", raw)[0]
        if scalar_type == "float32":
            if len(raw) != 4:
                raise RuntimeError("float32 wrapper payload length mismatch")
            return struct.unpack("<f", raw)[0]
        if scalar_type == "float64":
            if len(raw) != 8:
                raise RuntimeError("float64 wrapper payload length mismatch")
            return struct.unpack("<d", raw)[0]
        if scalar_type == "string":
            return raw.decode("utf-8")
        if scalar_type == "bytes":
            return raw.hex()
        raise RuntimeError(f"unsupported common wrapper scalar type: {scalar_type}")

    def _coerce_primitive_vector_input(self, element_type: str, values: Any) -> tuple[bytes, int]:
        item_size = _ARRAY_ITEM_SIZES[element_type]
        if element_type == "bool":
            if isinstance(values, (bytes, bytearray, memoryview)):
                raw = bytes(memoryview(values).cast("B"))
                return raw, len(raw)
            raw = bytes(1 if bool(value) else 0 for value in values)
            return raw, len(raw)

        try:
            mv = memoryview(values)
        except TypeError:
            mv = None
        if mv is not None:
            try:
                raw = mv.cast("B")
            except TypeError:
                raw = None
            if raw is not None and mv.ndim == 1 and mv.itemsize == item_size:
                return bytes(raw), mv.nbytes // item_size

        packed = array.array(_ARRAY_TYPECODES[element_type], values)
        return packed.tobytes(), len(packed)

    @staticmethod
    def _copy_native_output(output_buffer, output_capacity: int, output_length, operation: str) -> bytes:
        actual_length = int(output_length.value)
        if actual_length > output_capacity:
            raise RuntimeError(
                f"{operation} returned output length {actual_length} "
                f"larger than buffer capacity {output_capacity}"
            )
        return ctypes.string_at(output_buffer, actual_length)

    @staticmethod
    def _required_output_capacity(output_length, output_capacity: int, operation: str) -> int:
        required_length = int(output_length.value)
        if required_length <= output_capacity:
            raise RuntimeError(
                f"{operation} returned BUFFER_TOO_SMALL without a larger required length "
                f"(required={required_length}, capacity={output_capacity}); "
                "the native core must report the required output length"
            )
        return required_length

    def _call_with_output_buffer(self, func, *args, initial_capacity: int, operation: str) -> bytes:
        out_len = ctypes.c_size_t()
        out_cap = max(1, int(initial_capacity))
        for attempt in range(2):
            out_buf = (ctypes.c_uint8 * out_cap)()
            rc = func(*args, out_buf, ctypes.sizeof(out_buf), ctypes.byref(out_len))
            if rc == _BUFFER_TOO_SMALL:
                required_len = self._required_output_capacity(out_len, out_cap, operation)
                if attempt == 1:
                    raise RuntimeError(
                        f"{operation} remained buffer-too-small after required-size retry "
                        f"(required={required_len})"
                    )
                out_cap = required_len
                continue
            if rc != 0:
                raise RuntimeError(f"{operation} failed rc={rc}")
            return self._copy_native_output(out_buf, out_cap, out_len, operation)
        raise RuntimeError(f"{operation} did not complete")

    def _call_json(self, func, *args) -> bytes:
        return self._call_with_output_buffer(
            func,
            *args,
            initial_capacity=4096,
            operation="Rust codec/schema call",
        )

    def _call_bytes(self, func, *args) -> bytes:
        out_len = ctypes.c_size_t()
        rc = func(*args, None, 0, ctypes.byref(out_len))
        if rc not in (0, _BUFFER_TOO_SMALL):
            raise RuntimeError(f"Rust codec call failed rc={rc}")
        out_cap = max(256, int(out_len.value))
        for attempt in range(2):
            out_buf = (ctypes.c_uint8 * out_cap)()
            rc = func(*args, out_buf, ctypes.sizeof(out_buf), ctypes.byref(out_len))
            if rc == _BUFFER_TOO_SMALL:
                required_len = self._required_output_capacity(out_len, out_cap, "Rust codec call")
                if attempt == 1:
                    raise RuntimeError(
                        "Rust codec call remained buffer-too-small after required-size retry "
                        f"(required={required_len})"
                    )
                out_cap = required_len
                continue
            if rc != 0:
                raise RuntimeError(f"Rust codec call failed rc={rc}")
            return self._copy_native_output(out_buf, out_cap, out_len, "Rust codec call")
        raise RuntimeError("Rust codec call did not complete")

    def _call_timing_decode(self, func, wire: bytes, wire_len: int) -> tuple[bytes, bytes, bytes]:
        header_len = ctypes.c_size_t()
        payload_ref_len = ctypes.c_size_t()
        payload_len = ctypes.c_size_t()
        rc = func(
            wire,
            wire_len,
            None,
            0,
            ctypes.byref(header_len),
            None,
            0,
            ctypes.byref(payload_ref_len),
            None,
            0,
            ctypes.byref(payload_len),
        )
        if rc not in (0, _BUFFER_TOO_SMALL):
            raise RuntimeError(f"Rust timing decode failed rc={rc}")
        output_capacities = [
            max(1, int(header_len.value)),
            max(1, int(payload_ref_len.value)),
            max(1, int(payload_len.value)),
        ]
        for attempt in range(2):
            header_buf = (ctypes.c_uint8 * output_capacities[0])()
            payload_ref_buf = (ctypes.c_uint8 * output_capacities[1])()
            payload_buf = (ctypes.c_uint8 * output_capacities[2])()
            rc = func(
                wire,
                wire_len,
                header_buf,
                output_capacities[0],
                ctypes.byref(header_len),
                payload_ref_buf,
                output_capacities[1],
                ctypes.byref(payload_ref_len),
                payload_buf,
                output_capacities[2],
                ctypes.byref(payload_len),
            )
            if rc == _BUFFER_TOO_SMALL:
                reported_lengths = [int(header_len.value), int(payload_ref_len.value), int(payload_len.value)]
                larger_capacities = [
                    max(capacity, length) if length > capacity else capacity
                    for capacity, length in zip(output_capacities, reported_lengths)
                ]
                if larger_capacities == output_capacities:
                    raise RuntimeError(
                        "Rust timing decode returned BUFFER_TOO_SMALL without reporting a larger "
                        "required output length; the native core must report required lengths"
                    )
                if attempt == 1:
                    raise RuntimeError("Rust timing decode remained buffer-too-small after retry")
                output_capacities = larger_capacities
                continue
            if rc != 0:
                raise RuntimeError(f"Rust timing decode failed rc={rc}")
            return (
                self._copy_native_output(header_buf, output_capacities[0], header_len, "Rust timing decode header"),
                self._copy_native_output(
                    payload_ref_buf,
                    output_capacities[1],
                    payload_ref_len,
                    "Rust timing decode payload reference",
                ),
                self._copy_native_output(payload_buf, output_capacities[2], payload_len, "Rust timing decode payload"),
            )
        raise RuntimeError("Rust timing decode did not complete")

    def _call_control_decode(self, func, wire: bytes, *scalar_outs) -> bytes:
        payload_len = ctypes.c_size_t()
        rc = func(
            wire,
            len(wire),
            *(ctypes.byref(value) for value in scalar_outs),
            None,
            0,
            ctypes.byref(payload_len),
        )
        if rc not in (0, _BUFFER_TOO_SMALL):
            raise RuntimeError(f"Rust Control decode failed rc={rc}")
        output_capacity = max(1, int(payload_len.value))
        for attempt in range(2):
            payload_buf = (ctypes.c_uint8 * output_capacity)()
            rc = func(
                wire,
                len(wire),
                *(ctypes.byref(value) for value in scalar_outs),
                payload_buf,
                output_capacity,
                ctypes.byref(payload_len),
            )
            if rc == _BUFFER_TOO_SMALL:
                required_len = self._required_output_capacity(
                    payload_len,
                    output_capacity,
                    "Rust Control decode",
                )
                if attempt == 1:
                    raise RuntimeError(
                        "Rust Control decode remained buffer-too-small after retry "
                        f"(required={required_len})"
                    )
                output_capacity = required_len
                continue
            if rc != 0:
                raise RuntimeError(f"Rust Control decode failed rc={rc}")
            return self._copy_native_output(
                payload_buf,
                output_capacity,
                payload_len,
                "Rust Control decode",
            )
        raise RuntimeError("Rust Control decode did not complete")
