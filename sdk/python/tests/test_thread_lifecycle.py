from __future__ import annotations

import threading
import types
import unittest

from r2kernel.core.event_client import EventClient
from r2kernel.core.subsystem_runtime import SubsystemRuntime


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, _log_id: str, message: str) -> None:
        self.errors.append(message)


def _bare_event_client() -> EventClient:
    client = object.__new__(EventClient)
    client._notify_stop = threading.Event()
    client._notify_thread = None
    client._rust_recv_stop = threading.Event()
    client._rust_recv_thread = None
    client._core_mode = "python"
    client._rust_event_handle = None
    client._rust_core = None
    client._subscription_threads = {}
    client._wake_notify_loop = lambda: None
    client.inform_subsystem_down = lambda: None
    client._close_notify_endpoint = lambda: None
    return client


class EventClientThreadLifecycleTests(unittest.TestCase):
    def _assert_stop_from_owned_thread_is_safe(self, thread_attribute: str) -> None:
        client = _bare_event_client()
        failures: list[BaseException] = []

        def stop_from_thread() -> None:
            try:
                client.stop()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=stop_from_thread, daemon=True)
        if thread_attribute == "_subscription_threads":
            client._subscription_threads = {("probe", "publisher"): worker}
        else:
            setattr(client, thread_attribute, worker)

        worker.start()
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_stop_from_notify_callback_thread_does_not_join_itself(self) -> None:
        self._assert_stop_from_owned_thread_is_safe("_notify_thread")

    def test_stop_from_python_event_subscription_thread_does_not_join_itself(self) -> None:
        self._assert_stop_from_owned_thread_is_safe("_subscription_threads")

    def test_stop_from_rust_event_receive_callback_does_not_join_itself(self) -> None:
        client = _bare_event_client()
        client._core_mode = "rust"
        event_handle = object()
        client._rust_event_handle = event_handle
        closed_handles: list[object] = []
        client._rust_core = types.SimpleNamespace(
            lib=types.SimpleNamespace(
                r2kernel_event_client_close=lambda handle: closed_handles.append(handle)
            )
        )
        failures: list[BaseException] = []

        def stop_from_thread() -> None:
            try:
                client.stop()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=stop_from_thread, daemon=True)
        client._rust_recv_thread = worker
        worker.start()
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(closed_handles, [event_handle])


class ProcedureServerLifecycleTests(unittest.TestCase):
    def test_procedure_callback_can_stop_its_server_without_self_join(self) -> None:
        server_stopped = threading.Event()
        stop_thread_ids: list[int] = []

        class _Client:
            def stop_control_server(self) -> None:
                stop_thread_ids.append(threading.get_ident())
                server_stopped.set()

        logger = _Logger()
        runtime = object.__new__(SubsystemRuntime)
        runtime._subsystem_name = "component.test"
        runtime._client = _Client()
        runtime._logger = logger
        runtime._procedure_handler_context = threading.local()
        runtime._procedure_server_enabled = True
        runtime._control_server_enabled = False
        runtime._operation_server_enabled = False
        runtime._control_dispatch_running = True

        callback_thread_ids: list[int] = []
        failures: list[BaseException] = []

        def stop_server(_params: object) -> str:
            callback_thread_ids.append(threading.get_ident())
            runtime.stop_procedure_server()
            return "stopping"

        def invoke_procedure() -> None:
            try:
                runtime._invoke_procedure_handler(stop_server, None, None)  # type: ignore[arg-type]
            except BaseException as exc:
                failures.append(exc)

        callback_thread = threading.Thread(target=invoke_procedure, daemon=True)
        callback_thread.start()
        callback_thread.join(timeout=2.0)

        self.assertFalse(callback_thread.is_alive())
        self.assertTrue(server_stopped.wait(timeout=2.0))
        self.assertEqual(failures, [])
        self.assertEqual(logger.errors, [])
        self.assertNotEqual(callback_thread_ids[0], stop_thread_ids[0])
        self.assertFalse(runtime._control_dispatch_running)


if __name__ == "__main__":
    unittest.main()
