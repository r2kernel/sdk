from __future__ import annotations

import ctypes
import json
import unittest

from r2kernel.interface.codec import RustMessageCodec


_INNER_SCHEMA = {
    "namespace": "msg.codec.buffer-test.v1",
    "name": "Inner",
    "fields": [
        {"name": "data", "type": "bytes", "optional": False},
        {"name": "label", "type": "string", "optional": False},
    ],
}
_OUTER_SCHEMA = {
    "namespace": "msg.codec.buffer-test.v1",
    "name": "Outer",
    "fields": [
        {"name": "prefix", "type": "bytes", "optional": False},
        {"name": "child", "type": "msg.codec.buffer-test.v1/Inner", "optional": False},
        {
            "name": "children",
            "type": "vector<msg.codec.buffer-test.v1/Inner>",
            "optional": False,
        },
    ],
}


class _FakeCodecLibrary:
    def __init__(self) -> None:
        self.schemas = {
            b"msg.codec.buffer-test.v1/Inner": _INNER_SCHEMA,
            b"msg.codec.buffer-test.v1/Outer": _OUTER_SCHEMA,
        }
        self.message_set_json = json.dumps(
            {
                "namespace": "msg.codec.buffer-test.v1",
                "messages": {"large": "x" * 5000},
            }
        ).encode("utf-8")
        self.message_set_output_capacities: list[int] = []
        self.generic_calls: list[str] = []

    @staticmethod
    def _copy_output(data: bytes, output_buffer, output_capacity: int, output_length) -> int:
        output_length_pointer = ctypes.cast(output_length, ctypes.POINTER(ctypes.c_size_t))
        output_length_pointer.contents.value = len(data)
        if output_buffer is None:
            return 0
        if output_capacity < len(data):
            return -4
        if data:
            ctypes.memmove(output_buffer, data, len(data))
        return 0

    @staticmethod
    def _message_ref_bytes(message_ref) -> bytes:
        if isinstance(message_ref, bytes):
            return message_ref.split(b"\0", 1)[0]
        return ctypes.cast(message_ref, ctypes.c_char_p).value or b""

    def r2kernel_codec_encode_message_json_into(self, _message_ref, data: bytes, length: int, output) -> int:
        self.generic_calls.append("encode")
        output.data = bytes(data[:length])
        return 0

    def r2kernel_codec_decode_message_json_into(self, _message_ref, data: bytes, length: int, output) -> int:
        self.generic_calls.append("decode")
        output.data = bytes(data[:length])
        return 0

    def r2kernel_schema_get_message_json(
        self,
        message_ref,
        output_buffer,
        output_capacity: int,
        output_length,
    ) -> int:
        schema = self.schemas[self._message_ref_bytes(message_ref)]
        return self._copy_output(
            json.dumps(schema).encode("utf-8"),
            output_buffer,
            output_capacity,
            output_length,
        )

    def r2kernel_schema_get_message_set_json(
        self,
        _namespace,
        output_buffer,
        output_capacity: int,
        output_length,
    ) -> int:
        self.message_set_output_capacities.append(output_capacity)
        return self._copy_output(
            self.message_set_json,
            output_buffer,
            output_capacity,
            output_length,
        )


class _FakeCodecOutput:
    def __init__(self) -> None:
        self.data = b""

    @property
    def handle(self):
        return self

    def byte_result(self) -> bytes:
        return self.data


def _codec_with_fake_library() -> RustMessageCodec:
    codec = RustMessageCodec.__new__(RustMessageCodec)
    codec._lib = _FakeCodecLibrary()
    output = _FakeCodecOutput()
    codec._generic_output = lambda: output
    return codec


class RustMessageCodecBufferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = _codec_with_fake_library()

    def test_nested_bytes_round_trip_through_fake_ffi(self) -> None:
        message_ref = "msg.codec.buffer-test.v1/Outer"
        payload = {
            "prefix": b"\x00\xff",
            "child": {"data": b"\x10\x11", "label": "direct"},
            "children": [
                {"data": b"\x20", "label": "first"},
                {"data": b"\x30\x31", "label": "second"},
            ],
        }

        encoded = self.codec.encode_message(message_ref, payload)
        decoded = self.codec.decode_message(message_ref, encoded)

        self.assertEqual(decoded, payload)
        # Exactly one native conversion per generic encode/decode call (no size query + fill pair).
        self.assertEqual(self.codec._lib.generic_calls, ["encode", "decode"])

    def test_large_message_set_uses_reported_size_for_single_retry(self) -> None:
        fake_library = self.codec._lib

        message_set = self.codec.get_message_set("msg.codec.buffer-test.v1")

        self.assertEqual(message_set["namespace"], "msg.codec.buffer-test.v1")
        self.assertEqual(
            fake_library.message_set_output_capacities,
            [4096, len(fake_library.message_set_json)],
        )

    def test_call_json_retries_once_with_reported_required_length(self) -> None:
        expected = b"json-result:" + bytes(range(256)) * 400
        capacities: list[int] = []

        def fake_native_call(output_buffer, output_capacity, output_length) -> int:
            capacities.append(output_capacity)
            return _FakeCodecLibrary._copy_output(
                expected,
                output_buffer,
                output_capacity,
                output_length,
            )

        result = self.codec._call_json(fake_native_call)

        self.assertEqual(result, expected)
        self.assertEqual(capacities, [4096, len(expected)])

    def test_call_json_rejects_old_core_without_required_length(self) -> None:
        calls = 0

        def old_native_call(_output_buffer, _output_capacity, _output_length) -> int:
            nonlocal calls
            calls += 1
            return -4

        with self.assertRaisesRegex(RuntimeError, "without a larger required length"):
            self.codec._call_json(old_native_call)

        self.assertEqual(calls, 1)

    def test_call_json_rejects_output_length_larger_than_capacity(self) -> None:
        def malformed_native_call(_output_buffer, _output_capacity, output_length) -> int:
            output_length_pointer = ctypes.cast(output_length, ctypes.POINTER(ctypes.c_size_t))
            output_length_pointer.contents.value = 4097
            return 0

        with self.assertRaisesRegex(RuntimeError, "larger than buffer capacity"):
            self.codec._call_json(malformed_native_call)

    def test_call_json_stops_after_one_required_size_retry(self) -> None:
        capacities: list[int] = []

        def malformed_native_call(_output_buffer, output_capacity, output_length) -> int:
            capacities.append(output_capacity)
            output_length_pointer = ctypes.cast(output_length, ctypes.POINTER(ctypes.c_size_t))
            output_length_pointer.contents.value = output_capacity + 1
            return -4

        with self.assertRaisesRegex(RuntimeError, "remained buffer-too-small after required-size retry"):
            self.codec._call_json(malformed_native_call)

        self.assertEqual(capacities, [4096, 4097])


if __name__ == "__main__":
    unittest.main()
