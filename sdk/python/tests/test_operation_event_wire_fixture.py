"""Golden Operation event wire (``SDAE``) fixtures (sdk/testdata/wire/operation-event-*.json).

Operation feedback, result and state events travel on Event topics in the
binary ``SDAE`` layout of ``core/_runtime_operation_server.py``. The Python
encoder is the reference: each fixture holds the encoder input, the exact wire
bytes (``wire_hex``), the field-by-field breakdown and the message the Python
decoder returns. The C++ SDK (``tests/operation_event_wire_test.cpp``) and the
Rust bench client (``operation_event_wire_tests.rs``) encode the same input to
the same bytes and decode the same bytes to the same message. Typed cases
(a declared message type, TYPED) are reproduced by the C++ and Rust publishers
from the same payload value through their prepared codecs; this test checks
that the prepared codec of an initialized Python runtime writes them too.

``R2KERNEL_WRITE_WIRE_FIXTURES=1`` rewrites the fixtures from the Python
encoder; a normal run fails when the encoder output or the decoder result
differs from the stored fixture.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from r2kernel.core._runtime_operation_server import _OPERATION_EVENT_KIND_NAMES
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.interface.codec import RustMessageCodec
from r2kernel.interface.prepared_codec import PreparedMessageCodec, PreparedSchemaContext

WIRE_DIR = Path(__file__).resolve().parents[2] / "testdata" / "wire"
HEADER_FORMAT = "<4sBBHqHHHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
FLAG_NAMES = ((1, "PAYLOAD"), (2, "JSON"), (4, "TYPED"), (8, "CANCEL_REQUESTED"), (16, "STATE"))
STRING_FIELDS = ("operation_id", "operation", "target", "payload_type", "state")

PRODUCER = "python: core/_runtime_operation_server.py _OperationExecutionMixin._encode_operation_event_wire"
READERS = [
    "python: core/_runtime_operation_server.py _register_operation_subscription -> _decode_operation_event_wire",
    "cpp: src/wrapper/operation_event_wire.hpp decode + to_message (subsystem.cpp subscribe_operation_*_impl)",
    "rust bench client: src/operation_event.rs decode_operation_event_with (Subsystem::operation_event)",
]
OPERATION_ID = "6f1c2a4b9d8e4f0a8b7c6d5e4f3a2b1c"
TARGET = "component.wire_fixture"
OPERATION = "move_to_pose"

# Encoder input per fixture. ``payload_bytes_hex`` stands for a ``bytes`` payload.
CASES: dict[str, dict[str, Any]] = {
    "operation-event-feedback-json.json": {
        "description": "Operation feedback event with a JSON payload and no contract type",
        "input": {
            "kind": "feedback", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {"progress": 0.5, "step": "approach"}, "payload_type": None,
            "ts": 1793463571833069599, "state": None, "cancel_requested": False,
        },
    },
    "operation-event-state-cancel-requested.json": {
        "description": (
            "Operation state event after a cancel request: state text, CANCEL_REQUESTED, no payload, and the "
            "default state_type 'string' as payload_type (not a message reference, so nothing is typed)"
        ),
        "input": {
            "kind": "state", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": None, "payload_type": "string",
            "ts": 1793463571833069700, "state": "cancel_requested", "cancel_requested": True,
        },
    },
    "operation-event-result-json.json": {
        "description": (
            "Operation result event with a JSON payload; the payload text is Python json.dumps output "
            "(', ' and ': ' separators, non-ASCII escaped as \\uXXXX)"
        ),
        "input": {
            "kind": "result", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {"label": "arm-α", "pose": [1.0, 2.5, -0.25], "reached": True}, "payload_type": None,
            "ts": 1793463571833069800, "state": None, "cancel_requested": False,
        },
    },
    "operation-event-result-typed.json": {
        "description": "Operation result event whose result_type is a message reference: the payload is codec-encoded (TYPED)",
        "input": {
            "kind": "result", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {"value": "done"}, "payload_type": "msg.core.common.primitive.v1/StringValue",
            "ts": 1793463571833069900, "state": None, "cancel_requested": False,
        },
    },
    "operation-event-feedback-typed.json": {
        "description": (
            "Operation feedback event whose feedback_type is a message reference: the payload (nested float32 "
            "fields) is codec-encoded (TYPED)"
        ),
        "input": {
            "kind": "feedback", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {
                "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.5},
                "position": {"x": 1.0, "y": 2.5, "z": -0.25},
            },
            "payload_type": "msg.core.spatial.v1/Pose",
            "ts": 1793463571833070100, "state": None, "cancel_requested": False,
        },
    },
    "operation-event-result-typed-message.json": {
        "description": (
            "Operation result event whose result_type is a message with bool, string and an absent optional "
            "bytes field: the payload is codec-encoded (TYPED)"
        ),
        "input": {
            "kind": "result", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {"status": "reached", "success": True},
            "payload_type": "msg.core.common.result.v1/Result",
            "ts": 1793463571833070200, "state": None, "cancel_requested": False,
        },
    },
    "operation-event-state-typed.json": {
        "description": (
            "Operation state event with a payload whose state_type is a message reference: state text, STATE and "
            "a codec-encoded (TYPED) payload"
        ),
        "input": {
            "kind": "state", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": {"values": [0.25, -1.5, 3.0]},
            "payload_type": "msg.core.common.primitive.v1/Float32Vector",
            "ts": 1793463571833070300, "state": "approaching", "cancel_requested": False,
        },
    },
    "operation-event-state-json-string-type.json": {
        "description": (
            "Operation state event with a payload whose state_type is the default 'string': not a message "
            "reference, so the payload is JSON and 'string' is still written as payload_type"
        ),
        "input": {
            "kind": "state", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": "warming up", "payload_type": "string",
            "ts": 1793463571833070400, "state": "warming", "cancel_requested": False,
        },
    },
    "operation-event-state-terminal.json": {
        "description": (
            "Terminal Operation state event (complete_operation): the state carries no payload even when the "
            "Operation has a result, whatever the state_type; the result travels in its own result event"
        ),
        "input": {
            "kind": "state", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": None, "payload_type": "msg.core.common.primitive.v1/StringValue",
            "ts": 1793463571833070500, "state": "succeeded", "cancel_requested": False,
        },
    },
    "operation-event-feedback-raw.json": {
        "description": (
            "Operation feedback event with a bytes payload (PAYLOAD without JSON or TYPED). Python decodes it to "
            "bytes; C++ and the Rust bench client expose it as a JSON string of lowercase hex, as the codec "
            "writes bytes fields in JSON"
        ),
        "input": {
            "kind": "feedback", "operation_id": OPERATION_ID, "operation": OPERATION, "target": TARGET,
            "payload": None, "payload_bytes_hex": "0001feff", "payload_type": None,
            "ts": 1793463571833070000, "state": None, "cancel_requested": False,
        },
    },
}


def _runtime() -> SubsystemRuntime:
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    runtime._subsystem_name = TARGET
    runtime._operation_lock = threading.RLock()
    runtime._codec_lifecycle = SimpleNamespace(require_available=lambda: None)
    runtime._rust_message_codec = RustMessageCodec()
    return runtime


def _encode(runtime: SubsystemRuntime, case_input: dict[str, Any]) -> bytes:
    payload = case_input["payload"]
    if case_input.get("payload_bytes_hex") is not None:
        payload = bytes.fromhex(case_input["payload_bytes_hex"])
    return runtime._encode_operation_event_wire(
        kind=case_input["kind"],
        operation_id=case_input["operation_id"],
        operation=case_input["operation"],
        target=case_input["target"],
        payload=payload,
        payload_type=case_input["payload_type"],
        ts=case_input["ts"],
        state=case_input["state"],
        cancel_requested=case_input["cancel_requested"],
    )


def _layout(wire: bytes) -> dict[str, Any]:
    magic, version, kind_code, flags, ts, *lengths = struct.unpack(HEADER_FORMAT, wire[:HEADER_SIZE])
    names = (*STRING_FIELDS, "payload")
    offset = HEADER_SIZE
    fields: dict[str, Any] = {}
    for name, length in zip(names, lengths):
        chunk = wire[offset : offset + length]
        offset += length
        fields[name] = chunk
    payload = fields.pop("payload")
    layout: dict[str, Any] = {
        "header_format": f"{HEADER_FORMAT} (little-endian, {HEADER_SIZE} bytes), then the fields in this order",
        "magic": magic.decode("ascii"),
        "version": version,
        "kind_code": kind_code,
        "kind": _OPERATION_EVENT_KIND_NAMES[kind_code],
        "flags": flags,
        "flag_names": [name for bit, name in FLAG_NAMES if flags & bit],
        "ts": ts,
        "lengths": dict(zip(names, lengths)),
        "fields": {name: value.decode("utf-8") for name, value in fields.items()},
        "payload_hex": payload.hex(),
    }
    if flags & 2:
        layout["payload_text"] = payload.decode("utf-8")
    assert offset == len(wire), "the wire has trailing bytes"
    return layout


def _decoded_for_fixture(decoded: dict[str, Any]) -> dict[str, Any]:
    out = dict(decoded)
    if isinstance(out.get("payload"), (bytes, bytearray)):
        out["payload"] = bytes(out["payload"]).hex()
    return out


def build_fixture(runtime: SubsystemRuntime, name: str) -> dict[str, Any]:
    case = CASES[name]
    wire = _encode(runtime, case["input"])
    decoded = runtime._decode_operation_event_wire(wire)
    fixture: dict[str, Any] = {
        "description": case["description"],
        "producer": PRODUCER,
        "readers": READERS,
        "input": case["input"],
        "wire_hex": wire.hex(),
        "layout": _layout(wire),
        "decoded": _decoded_for_fixture(decoded),
    }
    if isinstance(decoded.get("payload"), (bytes, bytearray)):
        fixture["decoded_payload_representation"] = "hex (Python returns bytes)"
    return fixture


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))


def _fixture_text(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class OperationEventWireFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = _runtime()
        if os.environ.get("R2KERNEL_WRITE_WIRE_FIXTURES") == "1":
            for name in CASES:
                (WIRE_DIR / name).write_text(_fixture_text(build_fixture(cls.runtime, name)), encoding="utf-8")

    def test_every_fixture_is_the_python_encoder_output(self) -> None:
        for name in CASES:
            with self.subTest(fixture=name):
                stored = json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))
                self.assertEqual(stored, build_fixture(self.runtime, name))

    def test_decoder_returns_the_fixture_message(self) -> None:
        for name in CASES:
            with self.subTest(fixture=name):
                stored = json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))
                decoded = self.runtime._decode_operation_event_wire(bytes.fromhex(stored["wire_hex"]))
                expected = dict(stored["decoded"])
                if "decoded_payload_representation" in stored:
                    expected["payload"] = bytes.fromhex(expected["payload"])
                self.assertEqual(decoded, expected)
                self.assertEqual(decoded["operation_id"], stored["layout"]["fields"]["operation_id"])

    def test_breakdown_matches_the_bytes(self) -> None:
        for name in CASES:
            with self.subTest(fixture=name):
                stored = json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))
                layout = stored["layout"]
                wire = bytes.fromhex(stored["wire_hex"])
                self.assertEqual(wire[:4], b"SDAE")
                self.assertEqual(layout["version"], 1)
                self.assertEqual(len(wire), HEADER_SIZE + sum(layout["lengths"].values()))
                fields = layout["fields"]
                case_input = stored["input"]
                for field in ("operation_id", "operation", "target"):
                    self.assertEqual(fields[field], case_input[field])
                self.assertEqual(fields["payload_type"], case_input["payload_type"] or "")
                self.assertEqual(fields["state"], case_input["state"] or "")
                self.assertEqual("STATE" in layout["flag_names"], bool(case_input["state"]))
                self.assertEqual("CANCEL_REQUESTED" in layout["flag_names"], case_input["cancel_requested"])

    def test_prepared_codec_writes_the_fixture_bytes(self) -> None:
        # An initialized runtime encodes typed payloads with its PreparedMessageCodec
        # (codec_lifecycle.py); it must write and read the generic codec's bytes.
        runtime = _runtime()
        runtime._rust_message_codec = PreparedMessageCodec(PreparedSchemaContext())
        typed = [name for name in CASES if "TYPED" in _fixture(name)["layout"]["flag_names"]]
        self.assertGreaterEqual(len(typed), 4)
        for name in typed:
            with self.subTest(fixture=name):
                stored = _fixture(name)
                self.assertEqual(_encode(runtime, stored["input"]).hex(), stored["wire_hex"])
                decoded = runtime._decode_operation_event_wire(bytes.fromhex(stored["wire_hex"]))
                self.assertEqual(decoded, stored["decoded"])

    def test_typed_payloads_follow_the_declared_type(self) -> None:
        # TYPED exactly when the declared type names a message and a payload is
        # present; any declared type is written as payload_type.
        for name, case in CASES.items():
            with self.subTest(fixture=name):
                case_input = case["input"]
                layout = _fixture(name)["layout"]
                declared = str(case_input["payload_type"] or "")
                has_payload = case_input["payload"] is not None or case_input.get("payload_bytes_hex") is not None
                self.assertEqual("TYPED" in layout["flag_names"], "/" in declared and has_payload and
                                 case_input.get("payload_bytes_hex") is None)
                self.assertEqual(layout["fields"]["payload_type"], declared)

    def test_terminal_state_carries_no_payload(self) -> None:
        stored = _fixture("operation-event-state-terminal.json")
        self.assertEqual(stored["layout"]["flag_names"], ["STATE"])
        self.assertEqual(stored["layout"]["lengths"]["payload"], 0)
        self.assertIsNone(stored["decoded"]["payload"])

    def test_json_payload_is_json_dumps_text(self) -> None:
        stored = json.loads((WIRE_DIR / "operation-event-result-json.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["layout"]["payload_text"], json.dumps(stored["input"]["payload"]))
        self.assertEqual(stored["layout"]["flag_names"], ["PAYLOAD", "JSON"])


if __name__ == "__main__":
    unittest.main()
