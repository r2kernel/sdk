"""Request-local verified transport authority accessors (current_control_source_authority / current_protocol_host_authority)."""
import json
import threading
import unittest
from types import SimpleNamespace

from r2kernel.core.codec_lifecycle import CodecLifecycle
from r2kernel.core.request_context import OperationRecord, RequestContext
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.manifest import InterfaceDeclaration
from r2kernel.wrapper.subsystem import Subsystem

_SOURCE_FIELDS = ("peer", "epoch", "source", "source_session", "source_registration", "source_reference", "target",
                  "target_session", "target_registration", "interface", "binding_revision", "topic")


def _runtime() -> SubsystemRuntime:
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    declaration = InterfaceDeclaration(name="private", contract="fixture.private.v1", visibility="internal",
                                       endpoints={"controls": ("raw",)})
    runtime._owner = SimpleNamespace(_subsystem_manifest=SimpleNamespace(provides={"private": declaration}))
    runtime._control_endpoint_handlers = {}
    runtime._raw_control_endpoint_handlers = {}
    runtime._control_contracts = {}
    runtime._procedure_handlers = {}
    runtime._typed_procedure_handlers = {}
    runtime._operation_handlers = {}
    runtime._procedure_handler_context = threading.local()
    runtime._control_handler = None
    runtime._control_server_enabled = True
    runtime._timestamp_getter = lambda: 100
    runtime._logger = SimpleNamespace(error=lambda *args: None)
    runtime._codec_lifecycle = CodecLifecycle(runtime)
    return runtime


def _observe(runtime: SubsystemRuntime, observed: list) -> None:
    observed.append((runtime.current_control_source_authority(), runtime.current_protocol_host_authority()))


class TransportAuthorityAccessorTest(unittest.TestCase):
    def test_no_authority_outside_a_request(self):
        runtime = _runtime()
        self.assertIsNone(runtime.current_control_source_authority())
        self.assertIsNone(runtime.current_protocol_host_authority())

    def test_control_source_authority_is_current_only_while_the_callback_runs(self):
        runtime = _runtime()
        observed = []

        def handler(request, _context):
            runtime.current_control_source_authority()["source"] = "mutated.copy"
            _observe(runtime, observed)
            return {"ts": request["ts"], "status": 0, "payload": b""}

        runtime.register_raw_control_endpoint("raw", handler)
        authority = {field: "value" for field in _SOURCE_FIELDS}
        authority.update(source="source.actual", topic="raw")
        runtime._dispatch_control_request({
            "topic": "raw", "ts": 1, "payload": b"", "_manager_peer_verified": True,
            "sender": "__r2kernel_authority_v1__:" + json.dumps(authority),
        })
        self.assertEqual(observed, [(authority, None)])
        self.assertIsNone(runtime.current_control_source_authority())

    def test_protocol_host_authority_is_current_for_an_unverified_name_free_request(self):
        runtime = _runtime()
        observed = []

        def handler(request):
            _observe(runtime, observed)
            return {"ts": 1, "status": 0, "payload": b""}

        runtime._control_handler = handler
        host = {"kind": "protocol", "peer": "compute", "epoch": "backend-a"}
        runtime._dispatch_control_request({
            "topic": "anything", "ts": 1, "payload": b"", "_manager_peer_verified": True,
            "sender": "__r2kernel_host_v1__:" + json.dumps({**host, "codec_source": ""}),
        })
        self.assertEqual(observed, [(None, host)])
        self.assertIsNone(runtime.current_protocol_host_authority())
        runtime._dispatch_control_request({"topic": "anything", "ts": 1, "payload": b"", "sender": "client.declared"})
        self.assertEqual(observed[-1], (None, None))

    def test_operation_worker_keeps_the_host_authority_captured_at_acceptance(self):
        runtime = _runtime()
        runtime._operation_lock = threading.RLock()
        runtime._operation_quiescence = threading.Condition(runtime._operation_lock)
        runtime._publish_operation_state = lambda **_kwargs: None
        runtime._publish_operation_result = lambda **_kwargs: None
        observed = []
        runtime._operation_handlers["move"] = lambda _params: _observe(runtime, observed)
        host = {"kind": "protocol", "peer": "compute", "epoch": "backend-a"}
        context = RequestContext(kind="operation", name="move", topic="operation.start", accepted_at_sync_ns=1,
                                 host_authority=host)
        runtime._operation_records = {"op": OperationRecord(operation_id="op", operation="move", params={}, created_ts=1,
                                                            updated_ts=1, request_context=context)}
        worker = threading.Thread(target=runtime._run_operation, args=("op",))
        worker.start()
        worker.join(5)
        self.assertEqual(observed, [(None, host)])
        self.assertEqual(runtime._operation_records["op"].state, "succeeded")
        self.assertIsNone(runtime.current_protocol_host_authority())

    def test_subsystem_delegates_to_its_runtime(self):
        concrete = type("Concrete", (Subsystem,), {name: lambda self: True for name in Subsystem.__abstractmethods__})
        subsystem = concrete.__new__(concrete)
        subsystem._runtime = SimpleNamespace(current_control_source_authority=lambda: {"source": "a"},
                                             current_protocol_host_authority=lambda: None)
        self.assertEqual(subsystem.current_control_source_authority(), {"source": "a"})
        self.assertIsNone(subsystem.current_protocol_host_authority())


if __name__ == "__main__":
    unittest.main()
