"""The Python SDK reads the golden Control wire fixtures (sdk/testdata/wire).

The Control backend builds every fixture with its real code path and fails
when its output drifts (``udp::wire_contract_tests``). These tests parse the
same ``wire`` text with the Python readers, so a value type that changes on
either side fails here and in the C++/Rust bench client fixture tests.
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from r2kernel.core._control_registration import status_refusal
from r2kernel.core._control_shm_transport import configure_policy_from_registration
from r2kernel.core._event_registration import registration_retry_reason
from r2kernel.core.control_client import _admission_refusal
from r2kernel.core.r2kernel_client import R2KernelClient
from r2kernel.core.request_context import RequestContext
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.wrapper._subsystem_lifecycle import _LifecycleMixin

WIRE_DIR = Path(__file__).resolve().parents[2] / "testdata" / "wire"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))


def wire(name: str) -> dict[str, Any]:
    """The message exactly as Control sends it."""
    return json.loads(fixture(name)["wire"])


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def value_types(value: Any, path: str = "") -> dict[str, str]:
    """Same paths as the backend writes: ``a.b`` and ``list[]`` (first element)."""
    out: dict[str, str] = {}
    if path:
        out[path] = _type_name(value)
    prefix = f"{path}." if path else ""
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(value_types(item, f"{prefix}{key}"))
    elif isinstance(value, list) and value:
        out.update(value_types(value[0], f"{path}[]"))
    return out


class FixtureSetTest(unittest.TestCase):
    def test_every_fixture_states_the_types_of_its_wire_text(self) -> None:
        files = sorted(WIRE_DIR.glob("*.json"))
        self.assertGreaterEqual(len(files), 20)
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "wire_hex" in data or "scenarios" in data:
                # binary SDK-to-SDK Operation events and their sequences
                # (test_operation_event_wire_fixture.py, test_operation_event_sequence_fixture.py)
                continue
            with self.subTest(path.name):
                self.assertEqual(json.loads(data["wire"]), data["message"])
                self.assertEqual(value_types(data["message"]), data["types"])
                for volatile in data["volatile"]:
                    self.assertIn(volatile, data["types"])


class _RoutingEvent:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    def request_control(self, request: dict[str, Any], **_: Any) -> dict[str, Any]:
        return dict(self.reply)


class ControlRepliesTest(unittest.TestCase):
    def _client(self, reply: dict[str, Any]) -> R2KernelClient:
        client = R2KernelClient.__new__(R2KernelClient)
        client._event = _RoutingEvent(reply)
        return client

    def test_route_reply_types(self) -> None:
        for name in ("resolve-control-target-reply.json", "resolve-control-target-reply-before-status.json"):
            with self.subTest(name):
                route = self._client(wire(name)).resolve_control_target("component.wire_fixture")
                assert route is not None
                revision = route["registration_revision"]
                self.assertIsInstance(revision, str)
                self.assertEqual(len(revision), 32)
                int(revision, 16)
                self.assertEqual(int(route["control_port"] or 0), 0)
                self.assertEqual(route["route_kind"], "local_control")
                self.assertIsNone(route["peer_id"])
                self.assertIsInstance(route["procedures"], list)
                # Only an explicit False refuses codec context (codec_lifecycle.py).
                self.assertIsNot(route["codec_context_available"], False)
        reported = wire("resolve-control-target-reply.json")
        self.assertEqual(reported["procedure_contracts"]["echo"]["request_type"],
                         "msg.core.common.primitive.v1/Int32Value")
        self.assertIsNone(wire("resolve-control-target-reply-before-status.json")["codec_context_available"])

    def test_refused_route_is_no_route(self) -> None:
        self.assertIsNone(self._client(wire("error-reply.json")).resolve_control_target("component.unknown"))

    def test_registration_reply_types(self) -> None:
        reply = wire("register-subsystem-reply.json")
        self.assertIsInstance(reply["manager_peer_auth_required"], bool)
        self.assertIsNone(reply["resource_service_socket"])
        self.assertIsInstance(int(reply["topic_buffer_pool_slot_size"]), int)
        self.assertIsInstance(int(reply["event_startup_barrier_ms"]), int)
        self.assertIsInstance(reply["control_shared_buffer_slot_size"], int)
        self.assertIsInstance(reply["control_local_endpoint"], str)
        policy = configure_policy_from_registration(reply, 1 << 20)
        self.assertEqual(policy.maximum_channels, reply["control_shm_max_channels"])
        self.assertIsNone(registration_retry_reason(reply))
        handler = wire("register-control-handler-reply.json")
        self.assertIsInstance(int(handler["control_port"] or 0), int)
        configure_policy_from_registration(handler, 1 << 20)

    def test_registration_clock_status(self) -> None:
        from r2kernel.core.clock import configure_clock_from_status, reset_clock

        try:
            snapshot = configure_clock_from_status(wire("register-subsystem-reply.json"))
            self.assertIsNotNone(snapshot)
        finally:
            reset_clock()

    def test_status_acknowledgement_and_errors(self) -> None:
        self.assertIsNone(status_refusal(wire("update-subsystem-status-reply.json")))
        self.assertIn("unknown subsystem", status_refusal(wire("error-reply.json")) or "")
        self.assertEqual(registration_retry_reason(wire("registration-refusal-reply.json")), "registration_gate_busy")
        self.assertIsNone(registration_retry_reason(wire("error-reply.json")))
        refusal = _admission_refusal(fixture("admission-refusal-reply.json")["wire"].encode("utf-8"))
        assert refusal is not None
        self.assertEqual((refusal.reason, refusal.error), ("connection_limit", wire("admission-refusal-reply.json")["error"]))


class _Lib:
    """Records which native subscribe call a subscription used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("r2kernel_event_client_subscribe"):
            raise AttributeError(name)
        return lambda *args: self.calls.append((name, args)) or 0


class _Snapshot(SimpleNamespace):
    pass


class _Pool:
    """Two published generations in a depth-2 slot ring."""

    def snapshot(self, slot: int, include_payload: bool = True, expected_generation: int | None = None) -> _Snapshot:
        from r2kernel.core.topic_buffer import SEQ_READY

        return _Snapshot(seq=SEQ_READY, timestamp=10 + slot, generation=4 + slot)


def _event_client(core_mode: str) -> Any:
    from r2kernel.core.event_client import EventClient

    client = EventClient.__new__(EventClient)
    client._logger = SimpleNamespace(debug=lambda *a: None, error=lambda *a: None, warning=lambda *a: None)
    client._core_mode = core_mode
    client._rust_core = SimpleNamespace(lib=_Lib())
    client._rust_event_handle = object()
    client._ensure_rust_recv_thread = lambda: None
    client._topic_handlers = {"scan": lambda *args: None}
    client._subscribe_slots = {}
    client._subscribe_topic_generations = {}
    client._subscription_threads = {}
    client._buffer_pool = _Pool()
    client._event_subscription_worker = lambda key: None
    return client


class EventNotificationTest(unittest.TestCase):
    """notify_publisher: only a subscription that preceded the publisher replays its buffer (as C++)."""

    def test_native_subscription_follows_replay_existing(self) -> None:
        for name, expected in (("notify-publisher.json", "r2kernel_event_client_subscribe_ex"),
                               ("notify-publisher-late-subscriber.json", "r2kernel_event_client_subscribe_latest_ex")):
            with self.subTest(name):
                client = _event_client("rust")
                message = wire(name)
                client._process_notify_message(message)
                [(called, args)] = client._rust_core.lib.calls
                self.assertEqual(called, expected)
                self.assertEqual(args[3:6], (message["publish_slot_index"], message["queue_depth"],
                                             message["entry_slot_count"]))
                self.assertEqual(args[6], message["lock_name"].encode("utf-8"))

    def test_python_subscription_cursor_follows_replay_existing(self) -> None:
        for name, cursor in (("notify-publisher.json", -1), ("notify-publisher-late-subscriber.json", 5)):
            with self.subTest(name):
                client = _event_client("python")
                client._process_notify_message(wire(name))
                key = ("scan", "component.wire_publisher")
                self.assertIn(key, client._subscribe_slots)
                self.assertEqual(client._subscribe_topic_generations[key], cursor)

    def test_event_resource_replies(self) -> None:
        slots = wire("request-publish-slots-reply.json")
        self.assertIsInstance(slots["publish_slot_index"], int)
        self.assertTrue(all(isinstance(row["lock_name"], str) and isinstance(row["subscriber"], str)
                            for row in slots["subscribers"]))
        [publisher] = wire("register-subscription-reply.json")["publishers"]
        self.assertIsInstance(publisher["lock_name"], str)
        notice = wire("notify-subscriber.json")
        self.assertEqual((notice["cmd"], notice["topic"]), ("notify_subscriber", "scan"))
        self.assertIsInstance(notice["subsystem"], str)


class _Lifecycle(_LifecycleMixin):
    def __init__(self) -> None:
        self.requested: list[tuple[str, str | None]] = []
        self._logger = SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)

    @property
    def logger(self) -> Any:
        return self._logger

    def request_lifecycle_transition(self, transition: str, reason: str | None = None) -> bool:
        self.requested.append((transition, reason))
        return True


class LifecycleMessageTest(unittest.TestCase):
    def test_transition_and_reason(self) -> None:
        subject = _Lifecycle()
        subject._handle_lifecycle_transition_request(wire("lifecycle-transition-notify.json"))
        subject._handle_lifecycle_transition_request(wire("lifecycle-transition-notify-without-reason.json"))
        self.assertEqual(subject.requested, [("start", "operator request"), ("start", None)])

    def test_only_transition_names_the_transition(self) -> None:
        # Control sends only `transition` (the C++ SDK reads only it); `action` is no alias.
        subject = _Lifecycle()
        message = wire("lifecycle-transition-notify.json")
        message["action"] = message.pop("transition")
        subject._handle_lifecycle_transition_request(message)
        self.assertEqual(subject.requested, [("", "operator request")])


def _runtime() -> SubsystemRuntime:
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    runtime._subsystem_name = "component.wire_fixture"
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


def _request(name: str) -> dict[str, Any]:
    return {"payload": fixture(name)["wire"].encode("utf-8")}


def _decoded(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["payload"].decode("utf-8"))


class ProxiedRequestTest(unittest.TestCase):
    """The provider reads the request's own members, never those inside ``params``."""

    def test_procedure_request(self) -> None:
        runtime = _runtime()
        seen: list[tuple[str, Any]] = []
        runtime._procedure_handlers = {"echo": lambda params: params}
        runtime._invoke_procedure_handler = lambda handler, params, context: (
            seen.append((context.name, params)) or {"value": params["value"]}
        )
        response = _decoded(runtime._handle_procedure_request(_request("procedure-request.json")))
        self.assertEqual(response, {"request_id": "req-1", "ok": True, "result": {"value": 7}})
        self.assertEqual(seen[0][0], "echo")
        self.assertEqual(seen[0][1]["request_id"], "inner")

    def test_operation_start_request(self) -> None:
        runtime = _runtime()
        accepted: list[tuple[str, Any]] = []
        runtime._operation_handlers = {"run": lambda *args: None}

        def accept(**kwargs: Any) -> dict[str, Any]:
            accepted.append((kwargs["operation"], kwargs["params"]))
            return {"ok": True, "accepted": True, "operation_id": "op-1", "state": "queued"}

        runtime._accept_operation = accept
        response = _decoded(runtime._handle_operation_start_request(_request("operation-start-request.json")))
        self.assertEqual(response["request_id"], "req-2")
        self.assertTrue(response["accepted"])
        self.assertEqual(accepted[0][0], "run")
        self.assertEqual(accepted[0][1]["operation"], "other")

    def test_operation_query_and_cancel_requests(self) -> None:
        runtime = _runtime()
        query = _decoded(runtime._handle_operation_query_request(_request("operation-query-request.json")))
        self.assertEqual((query["request_id"], query["ok"], query["error_code"]), ("req-4", False, "not_found"))
        runtime._build_operation_cancel_response = lambda **kwargs: {
            "request_id": kwargs["request_id"], "ok": False, "operation_id": kwargs["operation_id"]}
        cancel = _decoded(runtime._handle_operation_cancel_request(_request("operation-cancel-request.json")))
        self.assertEqual((cancel["request_id"], cancel["operation_id"]), ("req-3", "op-1"))


if __name__ == "__main__":
    unittest.main()
