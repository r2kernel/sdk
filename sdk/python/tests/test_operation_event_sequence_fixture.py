"""Golden Operation event sequences (sdk/testdata/wire/operation-event-sequences.json).

A Python provider publishes a fixed sequence of feedback, state and result
events for each way an Operation ends: the handler returns, fails, completes
with a failure payload, is canceled by a cancel request, or is canceled by
``stop_operation_server``. ``queued`` and ``running`` are record states only
and are never published. The fixture records each published event (kind,
state, flags, payload_type and payload bytes; not the timestamp or the
operation id) so that the C++ SDK (``tests/operation_typed_payload_test.cpp``)
can check that its provider publishes the same sequence.

``R2KERNEL_WRITE_WIRE_FIXTURES=1`` rewrites the fixture from the Python
provider; a normal run fails when the published sequence differs.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.interface.codec import RustMessageCodec

WIRE_DIR = Path(__file__).resolve().parents[2] / "testdata" / "wire"
FIXTURE = WIRE_DIR / "operation-event-sequences.json"
HEADER_FORMAT = "<4sBBHqHHHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
FLAG_NAMES = ((1, "PAYLOAD"), (2, "JSON"), (4, "TYPED"), (8, "CANCEL_REQUESTED"), (16, "STATE"))
KINDS = {1: "feedback", 2: "result", 3: "state"}

TARGET = "component.wire_fixture"
OPERATION = "move_to_pose"
CONTRACT = {
    "request_type": "bytes",
    "feedback_type": "msg.core.spatial.v1/Pose",
    "result_type": "msg.core.common.result.v1/Result",
    "state_type": "msg.core.common.primitive.v1/Float32Vector",
}
PAYLOADS = {
    "feedback": {"orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.5}, "position": {"x": 1.0, "y": 2.5, "z": -0.25}},
    "state": {"values": [0.25, -1.5, 3.0]},
    "result": {"status": "reached", "success": True},
}

# Each scenario: what the handler does and what ends the Operation.
SCENARIOS: dict[str, str] = {
    "returns": "publish_feedback(feedback); publish_state('approaching', state); return result",
    "raises": "publish_feedback(feedback); raise an exception (no result)",
    "fails_with_payload": "context.fail(error_code='blocked', error_message='blocked', payload=result); return None",
    "cancel_request": (
        "publish_state('approaching', state); wait for a cancel request (operation.cancel); "
        "publish_state('still_running') (suppressed); return result"
    ),
    "stop_operation_server": (
        "publish_state('approaching', state); stop_operation_server() requests cancellation after its grace "
        "period; publish_state('still_running') (suppressed); return result"
    ),
    # A terminal state the handler publishes itself ends the Operation; what follows changes nothing.
    "terminal_state_then_returns": "publish_state('failed'); return result (ignored: no result is stored or published)",
    "terminal_state_then_raises": "publish_state('canceled'); raise an exception (ignored)",
}
# A finished Operation takes no cancel request, also while its handler still runs after publishing its terminal
# state: the handler is held there while the Operation is canceled (or the server stopped) and queried.
HELD_TERMINAL = {
    "terminal_failed_then_cancel_request": ("failed", "cancel"),
    "terminal_canceled_then_cancel_request": ("canceled", "cancel"),
    "terminal_succeeded_then_cancel_request": ("succeeded", "cancel"),
    "terminal_failed_then_stop_operation_server": ("failed", "stop"),
}
for _name, (_state, _ending) in HELD_TERMINAL.items():
    SCENARIOS[_name] = (
        f"publish_state('{_state}'); held until released; "
        + ("operation.cancel, then operation.query, while held" if _ending == "cancel"
           else "stop_operation_server() while held")
        + "; released; return result (ignored)"
    )


def _runtime() -> SubsystemRuntime:
    runtime = SubsystemRuntime.__new__(SubsystemRuntime)
    runtime._subsystem_name = TARGET
    clock = iter(range(1, 1_000_000))
    runtime._timestamp_getter = lambda: next(clock)
    runtime._operation_lock = threading.RLock()
    runtime._operation_quiescence = threading.Condition(runtime._operation_lock)
    runtime._operation_records = {}
    runtime._operation_handlers = {}
    runtime._operation_contracts = {OPERATION: dict(CONTRACT)}
    runtime._operation_publish_topics_lock = threading.Lock()
    runtime._operation_publish_topics_registered = set()
    runtime._operation_workers = []
    runtime._operation_server_enabled = True
    runtime._control_server_enabled = True
    runtime._procedure_server_enabled = False
    runtime._procedure_handler_context = threading.local()
    runtime._codec_lifecycle = SimpleNamespace(require_available=lambda: None)
    runtime._rust_message_codec = RustMessageCodec()
    runtime._logger = SimpleNamespace(error=lambda *args: None)
    runtime.register_event_publish = lambda **_: True
    runtime.start_event_transport = lambda: None
    runtime.published = []
    runtime.publish_event = lambda **event: runtime.published.append(event) or True
    return runtime


def _event(wire: bytes) -> dict[str, Any]:
    _magic, _version, kind_code, flags, _ts, *lengths = struct.unpack(HEADER_FORMAT, wire[:HEADER_SIZE])
    offset = HEADER_SIZE
    chunks = []
    for length in lengths:
        chunks.append(wire[offset : offset + length])
        offset += length
    _operation_id, _operation, _target, payload_type, state, payload = chunks
    return {
        "kind": KINDS[kind_code],
        "state": state.decode("utf-8"),
        "flag_names": [name for bit, name in FLAG_NAMES if flags & bit],
        "payload_type": payload_type.decode("utf-8"),
        "payload_hex": payload.hex(),
    }


def _wait(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the Operation")
        time.sleep(0.001)


def run_scenario(name: str) -> dict[str, Any]:
    runtime = _runtime()
    approaching = threading.Event()
    release = threading.Event()

    def handler(_params: Any, context: Any) -> Any:
        if name in HELD_TERMINAL:
            context.publish_state(HELD_TERMINAL[name][0])
            approaching.set()
            release.wait(5)
            return PAYLOADS["result"]
        if name in ("returns", "raises"):
            context.publish_feedback(PAYLOADS["feedback"])
        if name == "raises":
            raise RuntimeError("fixture failure")
        if name == "fails_with_payload":
            context.fail(error_code="blocked", error_message="blocked", payload=PAYLOADS["result"])
            return None
        if name == "terminal_state_then_returns":
            context.publish_state("failed")
            return PAYLOADS["result"]
        if name == "terminal_state_then_raises":
            context.publish_state("canceled")
            raise RuntimeError("fixture failure after the terminal state")
        context.publish_state("approaching", PAYLOADS["state"])
        approaching.set()
        if name in ("cancel_request", "stop_operation_server"):
            _wait(context.is_cancel_requested)
            context.publish_state("still_running")
        return PAYLOADS["result"]

    runtime._operation_handlers[OPERATION] = handler
    accepted = runtime._accept_operation(
        operation=OPERATION, params={}, created_ts=runtime.now_ns(), request_context=None,
        result_type=CONTRACT["result_type"],
    )
    operation_id = accepted["operation_id"]
    record = runtime._operation_records[operation_id]
    held: dict[str, Any] | None = None
    if name in HELD_TERMINAL:
        _wait(approaching.is_set)
        if HELD_TERMINAL[name][1] == "cancel":
            reply = runtime._build_operation_cancel_response(operation_id=operation_id, request_id="cancel",
                                                             recv_ts=runtime.now_ns())
            query = runtime._build_operation_query_response(operation_id=operation_id, request_id="query")
            held = {"cancel_reply_cancel_requested": reply["cancel_requested"], "query_state": query["state"],
                    "query_cancel_requested": query["cancel_requested"]}
        else:
            runtime._stop_control_dispatch_server = lambda: None
            runtime._stop_operation_server()
            held = {"cancel_requested_after_stop": record.cancel_requested}
        release.set()
    elif name == "cancel_request":
        _wait(approaching.is_set)
        runtime._build_operation_cancel_response(operation_id=operation_id, request_id="cancel", recv_ts=runtime.now_ns())
    elif name == "stop_operation_server":
        _wait(approaching.is_set)
        runtime._stop_control_dispatch_server = lambda: None
        runtime._stop_operation_server()
    # The worker finishes the Operation, publications included, before it
    # marks the queued item done (stop_operation_server joins it instead).
    runtime._operation_queue.join()
    runtime._stop_operation_executor()
    _wait(lambda: not any(worker.is_alive() for worker in runtime._operation_workers))
    return {
        "handler": SCENARIOS[name],
        "start_reply_state": accepted["state"],
        "final_state": record.state,
        "cancel_requested": record.cancel_requested,
        "result_stored": record.result is not None,
        **({"while_held": held} if held is not None else {}),
        "events": [_event(event["value"]) for event in runtime.published],
    }


def build_fixture() -> dict[str, Any]:
    return {
        "description": (
            "Operation event sequences of a Python provider, one per way an Operation ends. Each event is the "
            "SDAE event without its timestamp and operation id. queued and running are never published."
        ),
        "producer": "python: core/_runtime_operation_server.py (_accept_operation, _run_operation, "
                    "complete_operation, _publish_operation_*), _runtime_dispatch.py "
                    "_build_operation_cancel_response, _runtime_procedures.py _stop_operation_server",
        "readers": ["cpp: tests/operation_typed_payload_test.cpp (JSON and encoded-bytes publication)"],
        "target": TARGET,
        "operation": OPERATION,
        "contract": CONTRACT,
        "payloads": PAYLOADS,
        "scenarios": {name: run_scenario(name) for name in SCENARIOS},
    }


def _fixture_text(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class OperationEventSequenceFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.built = build_fixture()
        if os.environ.get("R2KERNEL_WRITE_WIRE_FIXTURES") == "1":
            FIXTURE.write_text(_fixture_text(cls.built), encoding="utf-8")

    def test_fixture_is_the_python_provider_sequence(self) -> None:
        stored = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(stored, self.built)

    def test_queued_and_running_are_not_published(self) -> None:
        for name, scenario in self.built["scenarios"].items():
            with self.subTest(scenario=name):
                self.assertEqual(scenario["start_reply_state"], "queued")
                states = [event["state"] for event in scenario["events"] if event["kind"] == "state"]
                self.assertNotIn("queued", states)
                self.assertNotIn("running", states)

    def test_terminal_state_precedes_its_result(self) -> None:
        for name, scenario in self.built["scenarios"].items():
            with self.subTest(scenario=name):
                events = scenario["events"]
                terminal = [index for index, event in enumerate(events) if event["state"] == scenario["final_state"]]
                self.assertEqual(len(terminal), 1)
                self.assertEqual(events[terminal[0]]["flag_names"], [*(["CANCEL_REQUESTED"] if scenario["cancel_requested"] else []), "STATE"])
                self.assertEqual(events[terminal[0] + 1 :], [event for event in events if event["kind"] == "result"])


if __name__ == "__main__":
    unittest.main()
