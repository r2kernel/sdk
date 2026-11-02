"""Native Event receive never degrades silently.

A native subscription that cannot be established is an error-level log and never switches the client to the
Python mode mid-run; a native client that cannot be created is an error-level log; start() after stop() is an
explicit error instead of a notify loop on a closed endpoint.
"""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from r2kernel.core.event_client import EventClient


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, _log_id, message) -> None:
        self.errors.append(message)

    def debug(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass


class _SubscribeLib:
    def __init__(self, result) -> None:
        self.result = result

    def r2kernel_event_client_subscribe_ex(self, *_args):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _native_client(lib) -> EventClient:
    client = object.__new__(EventClient)
    client._logger = _Logger()
    client._core_mode = "rust"
    client._rust_core = SimpleNamespace(lib=lib)
    client._rust_event_handle = object()
    client._rust_recv_stop = threading.Event()
    client._rust_recv_thread = None
    client._subscribe_slots = {}
    client._subscription_threads = {}
    return client


class NativeSubscriptionFailureTests(unittest.TestCase):
    def _subscribe(self, client: EventClient, lock_name: str | None = "/lock") -> None:
        client._add_event_subscription("topic/a", "publisher", 3, lambda *_args: None, lock_name)

    def test_refused_native_subscription_is_an_error(self) -> None:
        client = _native_client(_SubscribeLib(-1))
        self._subscribe(client)
        self.assertEqual(client._core_mode, "rust")
        self.assertEqual(len(client._logger.errors), 1)
        self.assertIn("Native Event subscription failed for topic topic/a from publisher publisher (rc=-1)",
                      client._logger.errors[0])
        self.assertEqual(client._subscription_threads, {})

    def test_native_subscription_exception_keeps_the_native_mode(self) -> None:
        client = _native_client(_SubscribeLib(OSError("native fault")))
        self._subscribe(client)
        self.assertEqual(client._core_mode, "rust", "a subscribe exception switched the client to the Python mode")
        self.assertEqual(client._subscription_threads, {}, "a Python subscription worker was started")
        self.assertEqual(len(client._logger.errors), 1)
        self.assertIn("native fault", client._logger.errors[0])

    def test_native_subscription_without_lock_is_an_error(self) -> None:
        client = _native_client(_SubscribeLib(0))
        self._subscribe(client, lock_name=None)
        self.assertEqual(len(client._logger.errors), 1)
        self.assertIn("without a notification lock", client._logger.errors[0])


class _CreateFailureCore:
    def __init__(self) -> None:
        self.lib = SimpleNamespace(r2kernel_event_client_create=lambda *_args: -3)


class _NativePool:
    _core = object()

    def __init__(self, **_kwargs) -> None:
        pass


class NativeCreateFailureTests(unittest.TestCase):
    def test_native_event_client_create_failure_is_reported(self) -> None:
        client = object.__new__(EventClient)
        client._logger = _Logger()
        client._buffer_pool = None
        client._prefer_rust = True
        client._core_mode = None
        client._startup_barrier_env_override = True
        client.register_subsystem = lambda: {
            "res": "ok",
            "topic_buffer_pool_name": "/pool",
            "topic_buffer_pool_slot_size": 4096,
            "topic_buffer_pool_slot_count": 4,
        }
        with patch("r2kernel.core._event_registration.TopicBufferPool", _NativePool), \
                patch("r2kernel.ffi.R2KernelCore", _CreateFailureCore):
            client._ensure_share_memory()
        self.assertEqual(client._core_mode, "python")
        self.assertEqual(len(client._logger.errors), 1)
        self.assertIn("Native Event client creation failed", client._logger.errors[0])
        self.assertIn("rc=-3", client._logger.errors[0])


class RestartAfterStopTests(unittest.TestCase):
    def test_start_after_stop_is_an_explicit_error(self) -> None:
        client = object.__new__(EventClient)
        client._logger = _Logger()
        client._notify_stop = threading.Event()
        client._notify_thread = None
        client._notify_endpoint = None
        client._notify_socket = None
        client._core_mode = "python"
        client._rust_event_handle = None
        client._rust_recv_stop = threading.Event()
        client._subscription_threads = {}
        client._down_sent = True
        client.stop()
        with self.assertRaisesRegex(RuntimeError, "cannot be restarted after stop"):
            client.start()
        self.assertIsNone(client._notify_thread, "a notify loop was started on the closed endpoint")


if __name__ == "__main__":
    unittest.main()
