"""A Control late_policy is one of drop, reject or execute_immediately.

An unknown name or wire code is an explicit ValueError on every encode and decode path; it never
becomes execute_immediately (the most permissive policy) and is never echoed into a request header.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from r2kernel.core._runtime_timing_support import _timing_context_fields
from r2kernel.core.timing import build_timing_request_header
from r2kernel.interface.codec import RustMessageCodec
from r2kernel.spec_paths import _PACKAGE_SPEC_ROOT
from test_subsystem_runtime import _FakeClient, _runtime_with_client

_VALID_POLICIES = ("drop", "reject", "execute_immediately")
_INVALID_POLICIES = ("bogus", "", " drop", "DROP", "execute-immediately")


def _encode(codec: RustMessageCodec, late_policy) -> bytes:
    return codec.encode_control_request_wire(
        request_id=7,
        sent_at_sync_ns=10,
        execute_at_sync_ns=20,
        max_lateness_ns=5,
        priority=3,
        late_policy=late_policy,
        payload=b"payload",
    )


class LatePolicyCodecTest(unittest.TestCase):
    def setUp(self) -> None:
        spec_root = patch.dict(os.environ, {"R2KERNEL_SPEC_ROOT": str(_PACKAGE_SPEC_ROOT)})
        spec_root.start()
        self.addCleanup(spec_root.stop)
        self.codec = RustMessageCodec()

    def test_valid_policies_round_trip(self) -> None:
        for policy in _VALID_POLICIES:
            with self.subTest(policy=policy):
                header, payload = self.codec.decode_control_request_wire(_encode(self.codec, policy))
                self.assertEqual(header["late_policy"], policy)
                self.assertEqual(payload, b"payload")

    def test_omitted_policy_is_execute_immediately(self) -> None:
        header, _payload = self.codec.decode_control_request_wire(_encode(self.codec, None))
        self.assertEqual(header["late_policy"], "execute_immediately")

    def test_unknown_policy_name_is_rejected_on_encode(self) -> None:
        for policy in _INVALID_POLICIES:
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "late_policy must be one of"):
                _encode(self.codec, policy)

    def test_unknown_wire_code_is_rejected_on_decode(self) -> None:
        # Core refuses to encode an unknown code, so forge a peer's frame at byte 6 of a valid one.
        encode = self.codec._lib.r2kernel_codec_encode_control_request_v2
        with self.assertRaisesRegex(RuntimeError, "rc=-1"):
            self.codec._call_bytes(encode, 7, 10, 10, 20, 0, 5, 3, 3, b"payload", len(b"payload"))
        wire = bytearray(self.codec._call_bytes(encode, 7, 10, 10, 20, 0, 5, 3, 0, b"payload", len(b"payload")))
        wire[6] = 3
        with self.assertRaisesRegex(RuntimeError, "Rust Control decode failed rc=-1"):
            self.codec.decode_control_request_wire(bytes(wire))


class LatePolicyHeaderTest(unittest.TestCase):
    def test_request_header_rejects_unknown_policy(self) -> None:
        for policy in _INVALID_POLICIES:
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "late_policy must be one of"):
                build_timing_request_header(
                    now_ns=1, target_subsystem="target", source_subsystem="source", late_policy=policy,
                )

    def test_request_header_keeps_valid_policy(self) -> None:
        header = build_timing_request_header(
            now_ns=1, target_subsystem="target", source_subsystem="source", late_policy="drop",
        )
        self.assertEqual(header["execution"]["late_policy"], "drop")

    def test_received_header_rejects_unknown_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "late_policy must be one of"):
            _timing_context_fields({"execution": {"mode": "at_time", "late_policy": "bogus"}})
        fields = _timing_context_fields({"execution": {"mode": "at_time", "late_policy": "reject"}})
        self.assertEqual(fields["late_policy_name"], "reject")


class LatePolicyControlTest(unittest.TestCase):
    def _transact(self, runtime, late_policy):
        return runtime._transact_control(
            target="actuator.servo",
            topic="servo_control",
            request_type="msg.actuator.servo.v1/ServoControlRequest",
            response_type="msg.actuator.servo.v1/ServoControlResponse",
            payload={"cycle": {"generation": 1, "sequence": 1, "intent": "hold"}},
            late_policy=late_policy,
            details=True,
        )

    def test_unknown_policy_is_rejected_before_transmission(self) -> None:
        client = _FakeClient({"host": "127.0.0.1", "control_port": 41001, "controls": ["servo_control"]})
        runtime = _runtime_with_client(client)
        encoded: list[dict] = []
        runtime._rust_message_codec = type(
            "Codec",
            (),
            {
                "encode_message": lambda _self, _message_type, _payload: b"typed-request",
                "encode_control_request_wire": lambda _self, **kwargs: encoded.append(kwargs) or b"wire",
            },
        )()
        with self.assertRaisesRegex(ValueError, "late_policy must be one of"):
            self._transact(runtime, "bogus")
        self.assertEqual(encoded, [])
        self.assertEqual(client.transacts, [])

        self._transact(runtime, None)
        self.assertEqual(encoded[-1]["late_policy"], "execute_immediately")
        self.assertEqual(client.transacts[-1]["value"], b"wire")


if __name__ == "__main__":
    unittest.main()
