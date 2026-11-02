"""Untyped Procedure and Operation JSON messages name their endpoint in ``procedure`` and ``operation``.

Unknown keys are ignored, so a message that names the endpoint only under another key is
answered as if the name were missing.
"""

from __future__ import annotations

import json
import threading
import unittest

from r2kernel.core.request_context import RequestContext
from r2kernel.core.subsystem_runtime import OperationRecord, SubsystemRuntime


def _runtime() -> SubsystemRuntime:
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    runtime._subsystem_name = "provider"
    runtime._timestamp_getter = lambda: 100
    runtime._client = object()
    runtime._operation_lock = threading.RLock()
    runtime._operation_records = {}
    runtime._operation_contracts = {}
    runtime._procedure_contracts = {}
    runtime._operation_server_enabled = True
    runtime._extract_request_context = lambda **kwargs: (
        kwargs["payload"],
        RequestContext(kind=kwargs["kind"], name=kwargs["name"], topic=kwargs["topic"],
                       accepted_at_sync_ns=kwargs["accepted_at_sync_ns"], timed=False),
    )
    runtime._enforce_timing_support = lambda **kwargs: None
    return runtime


def _request(message: dict) -> dict:
    return {"payload": json.dumps(message).encode("utf-8")}


def _decoded(response: dict) -> dict:
    return json.loads(response["payload"].decode("utf-8"))


class ProcedureWireFieldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _runtime()
        self.calls: list[object] = []
        self.runtime._procedure_handlers = {"echo": lambda params: params}
        self.runtime._invoke_procedure_handler = lambda handler, params, context: (
            self.calls.append(context.name) or handler(params)
        )

    def test_request_names_the_procedure_in_procedure(self) -> None:
        response = _decoded(self.runtime._handle_procedure_request(_request(
            {"kind": "procedure_request", "target": "provider", "procedure": "echo", "request_id": "r1", "params": {"x": 1}},
        )))
        self.assertEqual(response, {"request_id": "r1", "ok": True, "result": {"x": 1}})
        self.assertEqual(self.calls, ["echo"])

    def test_request_with_only_a_method_key_is_rejected_as_missing_its_name(self) -> None:
        response = _decoded(self.runtime._handle_procedure_request(_request(
            {"kind": "procedure_request", "target": "provider", "method": "echo", "request_id": "r2", "params": {}},
        )))
        self.assertEqual(response["error_code"], "invalid_argument")
        self.assertEqual(response["error_message"], "missing procedure name")
        self.assertFalse(response["ok"])
        self.assertEqual(self.calls, [])

    def test_client_request_carries_procedure_and_no_method(self) -> None:
        sent: list[dict] = []
        self.runtime._prepare_control_target = lambda target, **kwargs: None
        self.runtime._invalidate_control_target = lambda target: None

        def transact(**kwargs):
            sent.append(json.loads(kwargs["value"].decode("utf-8")))
            return {"ok": True, "response": {"payload": json.dumps({"ok": True, "result": 1}).encode("utf-8")}}

        self.runtime._transact_control_packet = transact
        self.runtime.call_procedure(target="provider", procedure="echo", params={"x": 1}, timeout_ms=100)
        self.assertEqual(sent[0]["procedure"], "echo")
        self.assertNotIn("method", sent[0])


class OperationWireFieldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _runtime()
        self.accepted: list[str] = []
        self.runtime._operation_handlers = {"move": lambda *args: None}

        def accept(**kwargs):
            self.accepted.append(kwargs["operation"])
            return {"ok": True, "accepted": True, "operation_id": "op-1", "state": "queued"}

        self.runtime._accept_operation = accept

    def test_start_names_the_operation_in_operation(self) -> None:
        response = _decoded(self.runtime._handle_operation_start_request(_request(
            {"kind": "operation_start_request", "target": "provider", "operation": "move", "request_id": "s1", "params": {}},
        )))
        self.assertTrue(response["accepted"])
        self.assertEqual(self.accepted, ["move"])

    def test_start_with_only_an_action_key_is_rejected_as_missing_its_name(self) -> None:
        response = _decoded(self.runtime._handle_operation_start_request(_request(
            {"kind": "operation_start_request", "target": "provider", "action": "move", "request_id": "s2", "params": {}},
        )))
        self.assertEqual(response["error_code"], "invalid_argument")
        self.assertEqual(response["error_message"], "missing operation name")
        self.assertFalse(response["accepted"])
        self.assertEqual(self.accepted, [])

    def test_client_start_carries_operation_and_no_action(self) -> None:
        sent: list[dict] = []
        self.runtime._prepare_control_target = lambda target, **kwargs: None
        self.runtime._invalidate_control_target = lambda target: None

        def transact(**kwargs):
            sent.append(json.loads(kwargs["value"].decode("utf-8")))
            reply = {"request_id": sent[0]["request_id"], "ok": True, "accepted": True, "operation_id": "op-2"}
            return {"ok": True, "response": {"payload": json.dumps(reply).encode("utf-8")}}

        self.runtime._transact_control_packet = transact
        self.assertEqual(self.runtime.start_operation(target="provider", operation="move", params={}, timeout_ms=100), "op-2")
        self.assertEqual(sent[0]["operation"], "move")
        self.assertNotIn("action", sent[0])

    def test_query_response_and_event_name_the_operation_in_operation(self) -> None:
        self.runtime._operation_records["op-3"] = OperationRecord(
            operation_id="op-3", operation="move", params={}, created_ts=1, updated_ts=1,
        )
        query = self.runtime._build_operation_query_response(operation_id="op-3", request_id="q1")
        self.assertEqual(query["operation"], "move")
        self.assertNotIn("action", query)
        wire = self.runtime._encode_operation_event_wire(
            kind="state", operation_id="op-3", operation="move", target="provider",
            payload=None, payload_type=None, ts=5, state="running",
        )
        event = self.runtime._decode_operation_event_wire(wire)
        self.assertEqual(event["operation"], "move")
        self.assertNotIn("action", event)


if __name__ == "__main__":
    unittest.main()
