"""EventTransport wrapper over the Core federation Event transport.

Mocked tests replace the native library; native tests need a Core with acknowledged subscription sync
(remote_subscriptions refused with InvalidArgument) and are skipped on an older Core.
"""
from __future__ import annotations

import ctypes
import json
import threading
import unittest
from types import SimpleNamespace

from r2kernel.core.event_transport import _PEER_ID_CAPACITY, EventTransport

_INVALID_ARGUMENT = -1
_BUFFER_TOO_SMALL = -4


def _set_len(pointer, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = value


def _set_pointer(pointer, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value


class _FakeLib:
    """Mocked native layer: scripted results per call."""

    def __init__(self, report: bytes, required_sizes: list[int]) -> None:
        self.report = report
        self.required_sizes = list(required_sizes)
        self.apply_capacities: list[int] = []
        self.recv_calls = 0
        self.accept_calls = 0

    def r2kernel_event_transport_apply_begin_json(self, handle, data, length, out_plan):
        _set_pointer(out_plan, 1)
        return 0

    def r2kernel_event_apply_plan_connect(self, plan):
        return 0

    def r2kernel_event_apply_plan_close(self, plan):
        return 0

    def r2kernel_event_transport_apply_finish(self, handle, plan, out, cap, out_len):
        self.apply_capacities.append(cap)
        required = self.required_sizes.pop(0) if self.required_sizes else len(self.report)
        _set_len(out_len, required)
        if required > cap:
            return _BUFFER_TOO_SMALL
        ctypes.memmove(out, self.report, len(self.report))
        _set_len(out_len, len(self.report))
        return 0

    def r2kernel_event_transport_recv_event_ex(self, handle, peer, *args):
        self.recv_calls += 1
        _set_len(args[7], 3)          # topic fits
        _set_len(args[10], 4)         # publisher fits
        _set_len(args[13], 70000)     # payload needs more than payload_cap
        return _BUFFER_TOO_SMALL

    def r2kernel_event_transport_session_setup(self, handle, out_setup):
        _set_pointer(out_setup, 1)
        return 0

    def r2kernel_event_session_setup_accept(self, setup, timeout_ms, out_session):
        _set_pointer(out_session, 1)
        return 0

    def r2kernel_event_session_setup_close(self, setup):
        return 0

    def r2kernel_event_session_peer_id(self, session, out, cap, out_len):
        self.accept_calls += 1
        _set_len(out_len, 900)
        return _BUFFER_TOO_SMALL

    def r2kernel_event_transport_install_session(self, handle, session):
        return 0

    def r2kernel_event_session_close(self, session):
        return 0


def _mocked(lib: _FakeLib) -> EventTransport:
    transport = object.__new__(EventTransport)
    transport._core = SimpleNamespace(lib=lib)
    transport._lock = threading.RLock()
    transport._apply_lock = threading.Lock()
    transport._io_timeout_ms = 200
    transport._handle = ctypes.c_void_p(1)
    return transport


class EventTransportMockedTests(unittest.TestCase):
    """Mocked native layer."""

    def test_remote_subscriptions_are_refused_before_the_native_call(self) -> None:
        lib = _FakeLib(b"{}", [])
        with self.assertRaisesRegex(ValueError, "remote_subscriptions is not accepted"):
            _mocked(lib).apply_state(forward_topics={}, peers=[
                {"peer_id": "b", "host": "127.0.0.1", "port": 1, "remote_subscriptions": [{"topic": "t", "mode": "event"}]}])
        self.assertEqual(lib.apply_capacities, [])

    def test_buffer_too_small_retries_once_with_exact_required_size(self) -> None:
        report = json.dumps({"connected": [], "disconnected": [], "subscriptions_synced": ["b"],
                             "subscription_ack_pending": []}).encode()
        lib = _FakeLib(report, [100_000])
        result = _mocked(lib).apply_state(forward_topics={}, peers=[])
        self.assertEqual(result["subscriptions_synced"], ["b"])
        self.assertEqual(lib.apply_capacities, [4096, 100_000])

    def test_no_growth_loop(self) -> None:
        lib = _FakeLib(b"{}", [100_000, 200_000])
        with self.assertRaisesRegex(RuntimeError, "still did not fit after one retry"):
            _mocked(lib).apply_state(forward_topics={}, peers=[])
        self.assertEqual(lib.apply_capacities, [4096, 100_000])
        old_core = _FakeLib(b"{}", [0])  # BufferTooSmall without a larger required size
        old_core.r2kernel_event_transport_apply_finish = (
            lambda handle, plan, out, cap, out_len: (old_core.apply_capacities.append(cap), _BUFFER_TOO_SMALL)[1])
        with self.assertRaisesRegex(RuntimeError, "without a larger required size"):
            _mocked(old_core).apply_state(forward_topics={}, peers=[])
        self.assertEqual(old_core.apply_capacities, [4096])

    def test_payload_above_cap_is_refused_and_left_queued(self) -> None:
        lib = _FakeLib(b"{}", [])
        with self.assertRaisesRegex(RuntimeError, "payload needs 70000 bytes, above payload_cap 1024; Core keeps"):
            _mocked(lib).recv_event("b", payload_cap=1024)
        self.assertEqual(lib.recv_calls, 1)

    def test_short_topic_buffer_retries_once_with_the_reported_size(self) -> None:
        lib = _FakeLib(b"{}", [])
        capacities: list[int] = []

        def recv(handle, peer, *args):
            capacities.append(args[6])
            if args[6] < 700:  # Core keeps the event and reports the topic size
                _set_len(args[7], 700)
                _set_len(args[10], 1)
                _set_len(args[13], 2)
                return _BUFFER_TOO_SMALL
            ctypes.memmove(args[5], b"t" * 700, 700)
            _set_len(args[7], 700)
            ctypes.memmove(args[8], b"p", 1)
            _set_len(args[10], 1)
            ctypes.memmove(args[11], b"xy", 2)
            _set_len(args[13], 2)
            return 0

        lib.r2kernel_event_transport_recv_event_ex = recv
        event = _mocked(lib).recv_event("b", payload_cap=1024)
        self.assertEqual((len(event["topic"]), event["publisher"], event["payload"]), (700, "p", b"xy"))
        self.assertEqual(capacities, [512, 700])

    def test_accept_retries_once_with_the_reported_size(self) -> None:
        lib = _FakeLib(b"{}", [])
        capacities: list[int] = []

        def peer_id(session, out, cap, out_len):
            capacities.append(cap)
            _set_len(out_len, 900)
            if cap < 900:
                return _BUFFER_TOO_SMALL
            ctypes.memmove(out, b"p" * 900, 900)
            return 0

        lib.r2kernel_event_session_peer_id = peer_id
        self.assertEqual(_mocked(lib).accept_peer(), "p" * 900)
        self.assertEqual(capacities, [_PEER_ID_CAPACITY, 900])
        stubborn = _FakeLib(b"{}", [])  # always reports 900 and never fits: no growth loop
        stubborn.r2kernel_event_session_peer_id = (
            lambda session, out, cap, out_len: (stubborn.__dict__.__setitem__("accept_calls", stubborn.accept_calls + 1),
                                                _set_len(out_len, cap + 1), _BUFFER_TOO_SMALL)[2])
        with self.assertRaisesRegex(RuntimeError, "did not fit capacity .* after one retry"):
            _mocked(stubborn).accept_peer()
        self.assertEqual(stubborn.accept_calls, 2)


def _native_core_refuses_remote_subscriptions() -> bool:
    try:
        transport = EventTransport(local_host_id="probe")
    except Exception:
        return False
    try:
        payload = json.dumps({"forward_topics": [], "peers": [{"peer_id": "p", "host": "127.0.0.1", "port": 9,
                              "remote_subscriptions": [{"topic": "t", "mode": "event"}]}]}).encode()
        out_len = ctypes.c_size_t()
        out = (ctypes.c_uint8 * 4096)()
        rc = transport._core.lib.r2kernel_event_transport_apply_state_json(
            transport._handle, payload, len(payload), out, 4096, ctypes.byref(out_len))
        return rc == _INVALID_ARGUMENT
    finally:
        transport.close()


@unittest.skipUnless(_native_core_refuses_remote_subscriptions(),
                     "needs a Core with acknowledged subscription sync (federation change)")
class EventTransportNativeTests(unittest.TestCase):
    """Real Core library."""

    def setUp(self) -> None:
        self.transport = EventTransport(local_host_id="a", connect_timeout_ms=50, io_timeout_ms=50)
        self.addCleanup(self.transport.close)

    def test_accept_only_peer_is_not_connected_and_report_has_ack_fields(self) -> None:
        report = self.transport.apply_state(forward_topics={"t": "event"}, peers=[
            {"peer_id": "b", "host": "127.0.0.1", "port": 9, "should_connect": False,
             "subscriptions": [{"topic": "t", "mode": "event"}]}])
        self.assertEqual(report["connected"], [])
        self.assertEqual(report["subscriptions_synced"], [])
        self.assertIn("subscription_ack_pending", report)

    def test_wrapper_refuses_remote_subscriptions(self) -> None:
        with self.assertRaisesRegex(ValueError, "remote_subscriptions is not accepted"):
            self.transport.apply_state(forward_topics={}, peers=[
                {"peer_id": "b", "host": "127.0.0.1", "port": 9, "remote_subscriptions": [{"topic": "t", "mode": "event"}]}])


if __name__ == "__main__":
    unittest.main()
