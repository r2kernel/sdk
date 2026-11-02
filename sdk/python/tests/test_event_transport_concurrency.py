"""EventTransport network waits never hold the transport lock.

``accept_peer`` waits for a connection and its handshake, and ``apply_state`` for outbound connects, without the
lock that serializes calls on the Core handle; ``recv_event(timeout_ms=0)``, ``enqueue_event`` and ``flush_n`` in
other threads do not wait behind them. Native tests need a Core with the detached session calls.
"""
from __future__ import annotations

import socket
import threading
import time
import unittest

from r2kernel.core.event_transport import EventTransport

_IO_TIMEOUT_MS = 1000
_CONNECT_TIMEOUT_MS = 600
# A non-blocking data-path call on an idle transport; far below either network wait.
_DATA_PATH_BOUND_S = 0.05


def _native_core_has_detached_sessions() -> bool:
    try:
        transport = EventTransport(local_host_id="probe")
    except Exception:
        return False
    try:
        return hasattr(transport._core.lib, "r2kernel_event_transport_install_session")
    finally:
        transport.close()


@unittest.skipUnless(_native_core_has_detached_sessions(), "needs a Core with the detached session calls")
class EventTransportLockTests(unittest.TestCase):
    def _transport(self) -> tuple[EventTransport, int]:
        transport = EventTransport(local_host_id="rx", io_timeout_ms=_IO_TIMEOUT_MS,
                                   connect_timeout_ms=_CONNECT_TIMEOUT_MS)
        self.addCleanup(transport.close)
        return transport, transport.bind("127.0.0.1", 0)

    def _probe_data_path(self, transport: EventTransport, busy: threading.Thread) -> list[float]:
        """Times non-blocking data-path calls while ``busy`` runs."""
        durations = []
        while busy.is_alive():
            started = time.monotonic()
            self.assertIsNone(transport.recv_event("nobody", payload_cap=64, timeout_ms=0))
            self.assertFalse(transport.enqueue_event(topic="t", publisher="p", priority=5, payload=b"x",
                                                     timestamp_ns=1, mode="event"))
            self.assertEqual(transport.flush_n(8), 0)
            durations.append(time.monotonic() - started)
            time.sleep(0.005)
        return durations

    def test_accept_blocked_on_a_silent_client_does_not_delay_the_data_path(self) -> None:
        transport, port = self._transport()
        silent = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(silent.close)
        outcome: dict[str, object] = {}

        def accept() -> None:
            started = time.monotonic()
            outcome["peer"] = transport.accept_peer(timeout_ms=0)
            outcome["waited"] = time.monotonic() - started

        accepting = threading.Thread(target=accept)
        accepting.start()
        durations = self._probe_data_path(transport, accepting)
        accepting.join()
        self.assertLess(max(durations), _DATA_PATH_BOUND_S,
                        f"data-path call waited {max(durations):.3f} s behind accept_peer")
        self.assertIsNone(outcome["peer"], "the silent client never completes its handshake")
        self.assertGreaterEqual(outcome["waited"], _IO_TIMEOUT_MS / 1000 * 0.9, "the handshake really waited")
        self.assertGreater(len(durations), 10)

    def test_apply_state_connect_does_not_delay_the_data_path(self) -> None:
        transport, _ = self._transport()
        # A listener whose accept queue is full: a connect to it waits for the connect timeout.
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(0)
        for _ in range(3):
            filler = socket.socket()
            self.addCleanup(filler.close)
            filler.setblocking(False)
            try:
                filler.connect(blocker.getsockname())
            except BlockingIOError:
                pass
        time.sleep(0.05)
        outcome: dict[str, object] = {}

        def apply() -> None:
            started = time.monotonic()
            outcome["report"] = transport.apply_state(forward_topics={}, peers=[
                {"peer_id": "slow", "host": "127.0.0.1", "port": blocker.getsockname()[1]}])
            outcome["waited"] = time.monotonic() - started

        applying = threading.Thread(target=apply)
        applying.start()
        durations = self._probe_data_path(transport, applying)
        applying.join()
        self.assertEqual(outcome["report"]["connected"], [])
        self.assertGreaterEqual(outcome["waited"], _CONNECT_TIMEOUT_MS / 1000 * 0.9, "the connect really waited")
        self.assertLess(max(durations), _DATA_PATH_BOUND_S,
                        f"data-path call waited {max(durations):.3f} s behind apply_state")

    def test_accept_installs_a_handshaken_peer(self) -> None:
        receiver, port = self._transport()
        sender = EventTransport(local_host_id="tx", io_timeout_ms=_IO_TIMEOUT_MS)
        self.addCleanup(sender.close)
        sender.apply_state(forward_topics={}, peers=[{"peer_id": "rx", "host": "127.0.0.1", "port": port}])
        self.assertEqual(receiver.accept_peer(), "tx")
        self.assertIsNone(receiver.accept_peer(timeout_ms=0), "nothing else is pending")
        with self.assertRaisesRegex(ValueError, "timeout_ms"):
            receiver.accept_peer(timeout_ms=-1)


if __name__ == "__main__":
    unittest.main()
