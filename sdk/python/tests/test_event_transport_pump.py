"""EventTransport.accept_peer duplicate outcome (F2) and recv_event pump/buffers (F5).

Mocked tests replace the native library; native tests need a Core with
``r2kernel_event_transport_recv_event_timeout`` and are skipped on an older Core.
"""
from __future__ import annotations

import ctypes
import json
import threading
import time
import unittest
from types import SimpleNamespace

from r2kernel.core.event_transport import EventTransport

_NOT_FOUND = -2
_ALREADY_CONNECTED = 1


def _set_len(pointer, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = value


def _mocked(lib) -> EventTransport:
    transport = object.__new__(EventTransport)
    transport._core = SimpleNamespace(lib=lib)
    transport._lock = threading.RLock()
    transport._apply_lock = threading.Lock()
    transport._io_timeout_ms = 200
    transport._handle = ctypes.c_void_p(1)
    return transport


class _PumpLib:
    """Mocked native layer recording the receive calls."""

    def __init__(self) -> None:
        self.ex_calls = 0
        self.timeout_calls: list[int] = []
        self.payload_buffers: list[int] = []

    def r2kernel_event_transport_session_setup(self, handle, out_setup):
        ctypes.cast(out_setup, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1
        return 0

    def r2kernel_event_session_setup_accept(self, setup, timeout_ms, out_session):
        ctypes.cast(out_session, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1
        return 0

    def r2kernel_event_session_setup_close(self, setup):
        return 0

    def r2kernel_event_session_peer_id(self, session, out, cap, out_len):
        ctypes.memmove(out, b"p", 1)
        _set_len(out_len, 1)
        return 0

    def r2kernel_event_transport_install_session(self, handle, session):
        return _ALREADY_CONNECTED

    def r2kernel_event_transport_recv_event_ex(self, handle, peer, *args):
        self.ex_calls += 1
        self.payload_buffers.append(ctypes.addressof(args[11]))
        return _NOT_FOUND

    def r2kernel_event_transport_recv_event_timeout(self, handle, peer, timeout_ms, *args):
        self.timeout_calls.append(timeout_ms.value)
        self.payload_buffers.append(ctypes.addressof(args[11]))
        ctypes.memmove(args[5], b"t", 1)
        _set_len(args[7], 1)
        ctypes.memmove(args[8], b"p", 1)
        _set_len(args[10], 1)
        ctypes.memmove(args[11], b"xy", 2)
        _set_len(args[13], 2)
        return 0


class EventTransportPumpMockedTests(unittest.TestCase):
    def test_duplicate_closed_by_resolution_is_not_an_error(self) -> None:
        self.assertIsNone(_mocked(_PumpLib()).accept_peer())

    def test_timeout_selects_the_explicit_wait_entry_point(self) -> None:
        lib = _PumpLib()
        transport = _mocked(lib)
        self.assertIsNone(transport.recv_event("b", payload_cap=1024))
        event = transport.recv_event("b", payload_cap=1024, timeout_ms=0)
        self.assertEqual((event["topic"], event["publisher"], event["payload"]), ("t", "p", b"xy"))
        self.assertEqual((lib.ex_calls, lib.timeout_calls), (1, [0]))
        with self.assertRaisesRegex(ValueError, "timeout_ms"):
            transport.recv_event("b", timeout_ms=-1)

    def test_receive_buffers_are_reused_across_calls(self) -> None:
        lib = _PumpLib()
        transport = _mocked(lib)
        for _ in range(3):
            transport.recv_event("b", payload_cap=4096, timeout_ms=0)
        transport.recv_event("b", payload_cap=1024, timeout_ms=0)  # a smaller cap reuses the larger buffer
        self.assertEqual(len(set(lib.payload_buffers)), 1, "no per-call payload buffer allocation")


def _native_core_has_timeout_receive() -> bool:
    try:
        transport = EventTransport(local_host_id="probe")
    except Exception:
        return False
    try:
        return hasattr(transport._core.lib, "r2kernel_event_transport_recv_event_timeout")
    finally:
        transport.close()


@unittest.skipUnless(_native_core_has_timeout_receive(), "needs a Core with recv_event_timeout (F5)")
class EventTransportPumpNativeTests(unittest.TestCase):
    def _transport(self, host_id: str) -> tuple[EventTransport, int]:
        transport = EventTransport(local_host_id=host_id, connect_timeout_ms=500, io_timeout_ms=300)
        self.addCleanup(transport.close)
        return transport, transport.bind("127.0.0.1", 0)

    def test_mutual_default_dial_accepts_once_and_returns_none_for_the_duplicate(self) -> None:
        a, port_a = self._transport("host-a")
        b, port_b = self._transport("host-b")
        a.apply_state(forward_topics={}, peers=[{"peer_id": "host-b", "host": "127.0.0.1", "port": port_b}])
        b.apply_state(forward_topics={}, peers=[{"peer_id": "host-a", "host": "127.0.0.1", "port": port_a}])
        self.assertEqual(b.accept_peer(), "host-a")
        self.assertIsNone(a.accept_peer(), "duplicate closed by resolution is not an error")

    def test_zero_timeout_pump_never_waits_and_applies_the_peer_request(self) -> None:
        sender, sender_port = self._transport("sender")
        receiver, _ = self._transport("receiver")
        sender.apply_state(forward_topics={"t": "event"}, peers=[
            {"peer_id": "receiver", "host": "127.0.0.1", "port": 1, "should_connect": False}])
        receiver.apply_state(forward_topics={}, peers=[
            {"peer_id": "sender", "host": "127.0.0.1", "port": sender_port,
             "subscriptions": [{"topic": "t", "mode": "event"}]}])
        self.assertEqual(sender.accept_peer(), "receiver")
        deadline = time.monotonic() + 2.0
        pumps = []
        while not sender.enqueue_event(topic="t", publisher="p", priority=5, payload=b"x", timestamp_ns=1,
                                       mode="event"):
            started = time.monotonic()
            self.assertIsNone(sender.recv_event("receiver", payload_cap=64, timeout_ms=0))
            pumps.append(time.monotonic() - started)
            self.assertLess(time.monotonic(), deadline, "the pump never applied the subscription request")
            time.sleep(0.001)
        self.assertTrue(pumps, "the forward set needs the pumped request")
        self.assertLess(max(pumps), 0.05, f"zero-timeout pump waited {max(pumps):.3f} s")
        self.assertEqual(sender.flush_n(8), 1)
        event = receiver.recv_event("sender", payload_cap=64)
        self.assertEqual((event["topic"], event["payload"]), ("t", b"x"))


if __name__ == "__main__":
    unittest.main()
