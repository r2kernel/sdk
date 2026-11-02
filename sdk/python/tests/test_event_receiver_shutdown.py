"""stop() must not close the native event client while the receiver thread is still inside recv_ex."""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from r2kernel.core import event_client as event_client_module
from r2kernel.core.event_client import EventClient


class _FakeEventLib:
    def __init__(self, recv_block_s: float | None) -> None:
        self.recv_block_s = recv_block_s
        self.in_recv = False
        self.release = threading.Event()
        self.closed_during_recv = False
        self.close_calls = 0
        self.stop_requested = threading.Event()

    def r2kernel_event_client_recv_ex(self, handle, timeout_ms, *args):
        if not self.stop_requested.is_set():
            time.sleep(0.005)
            return -2
        self.in_recv = True
        if self.recv_block_s is None:
            self.release.wait(timeout=10.0)
        else:
            time.sleep(self.recv_block_s)
        self.in_recv = False
        return -2

    def r2kernel_event_client_close(self, handle):
        self.close_calls += 1
        self.closed_during_recv = self.closed_during_recv or self.in_recv
        return 0


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, _log_id, message) -> None:
        self.errors.append(message)

    def debug(self, *_args, **_kwargs) -> None:
        pass


def _client(lib: _FakeEventLib) -> EventClient:
    client = object.__new__(EventClient)
    client._logger = _Logger()
    client._notify_stop = threading.Event()
    client._notify_thread = None
    client._notify_endpoint = None
    client._notify_socket = None
    client._core_mode = "rust"
    client._rust_core = SimpleNamespace(lib=lib)
    client._rust_event_handle = object()
    client._rust_recv_stop = threading.Event()
    client._rust_recv_thread = None
    client._max_subscribe_payload_size = 1024
    client._subscription_threads = {}
    client._down_sent = True  # inform_subsystem_down is a no-op
    return client


class ReceiverShutdownTests(unittest.TestCase):
    def _stop_while_receiving(self, lib: _FakeEventLib) -> EventClient:
        client = _client(lib)
        client._ensure_rust_recv_thread()
        time.sleep(0.02)
        lib.stop_requested.set()
        while not lib.in_recv:
            time.sleep(0.001)
        client.stop()
        return client

    def test_close_waits_for_a_receive_wait_that_outlasts_the_old_join(self) -> None:
        # A receive wait that began just before stop() returns after up to one full 500 ms timeout (plus
        # scheduling delay); the receiver must be awaited, not abandoned after 0.5 s.
        lib = _FakeEventLib(recv_block_s=0.7)
        client = self._stop_while_receiving(lib)
        self.assertEqual(lib.close_calls, 1)
        self.assertFalse(lib.closed_during_recv)
        self.assertIsNone(client._rust_event_handle)
        self.assertEqual(client._logger.errors, [])
        self.assertGreaterEqual(event_client_module._RUST_RECV_TIMEOUT_MS / 1000.0
                                + event_client_module._RUST_RECV_STOP_MARGIN_S, 0.7 + 0.2)

    def test_stuck_receiver_keeps_native_client_open_and_reports(self) -> None:
        lib = _FakeEventLib(recv_block_s=None)
        client = self._stop_while_receiving(lib)
        try:
            self.assertEqual(lib.close_calls, 0)
            self.assertFalse(lib.closed_during_recv)
            self.assertEqual(len(client._logger.errors), 1)
            self.assertIn("handle is retained until process exit because closing it would free memory "
                          "the receiver thread is still using", client._logger.errors[0])
        finally:
            lib.release.set()
            client._rust_recv_thread.join(timeout=5.0)

    def test_stuck_notify_thread_keeps_native_client_open_and_reports(self) -> None:
        # The notify thread may be inside a native subscribe on the handle; closing it then is a use-after-free.
        lib = _FakeEventLib(recv_block_s=0.0)
        client = _client(lib)
        release = threading.Event()
        notify = threading.Thread(target=lambda: release.wait(timeout=10.0), daemon=True)
        notify.start()
        client._notify_thread = notify
        try:
            client.stop()
            self.assertEqual(lib.close_calls, 0)
            self.assertIsNone(client._rust_event_handle)
            self.assertEqual(len(client._logger.errors), 1)
            self.assertIn("Event notify thread did not stop within 0.5s", client._logger.errors[0])
            self.assertIn("handle is retained until process exit because closing it would free memory "
                          "the notify thread is still using", client._logger.errors[0])
        finally:
            release.set()
            notify.join(timeout=5.0)

    def test_receiver_is_not_restarted_after_stop(self) -> None:
        # A notification processed during or after stop() must not start a receiver on the closed handle.
        lib = _FakeEventLib(recv_block_s=0.0)
        client = _client(lib)
        client.stop()
        self.assertEqual(lib.close_calls, 1)
        client._ensure_rust_recv_thread()
        self.assertIsNone(client._rust_recv_thread)
        self.assertTrue(client._rust_recv_stop.is_set())


if __name__ == "__main__":
    unittest.main()
