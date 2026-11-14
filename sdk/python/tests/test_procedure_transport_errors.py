"""A failed Procedure exchange reports the documented ``error_code`` (cross-audit SDK-112-02).

As in the C++ SDK, no reply before the deadline is ``timeout`` and a reply with a
nonzero Control channel status is ``procedure_transport_failed``. Only the lowest
Control exchange and the route lookup are replaced; ``transact`` and the
Procedure calls run unmodified.
"""

from __future__ import annotations

import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from r2kernel.core._control_messaging import _MessagingMixin
from r2kernel.core._runtime_procedures import _procedure_transport_failure
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.interface import PreparedSchemaContext

INT32 = "msg.core.common.primitive.v1/Int32Value"


class _Messaging(_MessagingMixin):
    def __init__(self, response: dict | None, transport_error: str | None) -> None:
        self._response = response
        self._last_transport_error = transport_error
        self._ready: dict[str, bool] = {}
        self._pending_cost_context: dict = {}
        self._control_cost_metrics = SimpleNamespace(enabled=False)

    def _now_ns(self) -> int:
        return time.monotonic_ns()

    def _managed_transact_request(self, **_: object) -> dict | None:
        return self._response

    def _get_encoder(self, _: str) -> None:
        return None

    def _get_decoder(self, _: str) -> None:
        return None


class _Codec:
    def __init__(self) -> None:
        self._context = PreparedSchemaContext([])

    def encode_message(self, reference: str, value: dict) -> bytes:
        return self._context.prepare(reference).encode(value)

    def decode_message(self, reference: str, payload: bytes) -> dict:
        return self._context.prepare(reference).decode(payload)


def _runtime(response: dict | None, transport_error: str | None = None) -> SubsystemRuntime:
    messaging = _Messaging(response, transport_error)
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    runtime._client = SimpleNamespace(transact_control=lambda **kwargs: messaging.transact(**kwargs))
    runtime._prepared_control_routes = {}
    runtime._prepare_control_target = lambda target, **_: {"control_contracts": {}}
    runtime._invalidate_control_target = lambda target: None
    runtime._codec_lifecycle = SimpleNamespace(validate_route=lambda *_: None, activity=nullcontext)
    runtime._get_rust_message_codec = _Codec
    return runtime


def _calls(runtime: SubsystemRuntime) -> dict[str, dict]:
    return {
        "untyped": runtime.call_procedure(target="peer", procedure="echo", params={"value": 7}),
        "typed": runtime.call_typed_procedure(
            target="peer", procedure="echo", request_type=INT32, response_type=INT32, params={"value": 7},
        ),
    }


class ProcedureTransportErrorTest(unittest.TestCase):
    def test_a_failure_status_is_procedure_transport_failed_with_the_reason(self) -> None:
        for name, result in _calls(_runtime({"status": 3, "payload": b"servo fault", "timestamp": 1})).items():
            with self.subTest(call=name):
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "procedure_transport_failed")
                self.assertEqual(result["error_message"], "servo fault")

    def test_no_reply_is_timeout_and_keeps_the_transport_reason(self) -> None:
        for name, result in _calls(_runtime(None, "connection reset")).items():
            with self.subTest(call=name):
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "timeout")
                self.assertEqual(result["error_message"], "connection reset")

    def test_the_timed_call_uses_the_same_codes(self) -> None:
        # The timed call reads the same transact result; its message falls back
        # to its own default when the transport gives no reason.
        self.assertEqual(
            _procedure_transport_failure({"ok": False, "timeout": False, "error": "control_channel_status",
                                          "response": {"status": 5, "payload": b""}}, "timed failed"),
            ("procedure_transport_failed", "control_channel_status"),
        )
        self.assertEqual(
            _procedure_transport_failure({"ok": False, "timeout": True, "error": None}, "timed failed"),
            ("timeout", "timed failed"),
        )


if __name__ == "__main__":
    unittest.main()
