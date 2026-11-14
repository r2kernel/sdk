"""The local Control handler socket (`r2kernel-control-handler-<name>-<pid>.sock`) does not outlive its process.

Regression for the 1.0.0 installed regression finding: Python handler servers stopped by SIGTERM left their
handler sockets in /tmp, while the notify sockets of the same processes were removed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

# A provider process: starts the local handler server (registration with Control stubbed), reports the
# handler socket, then waits or exits. A handler-thread exception is recorded and reported instead of being
# printed, so it fails the test rather than racing interpreter shutdown on stderr.
PROVIDER = textwrap.dedent('''
    import signal, sys, threading, time
    from r2kernel.core.native_control import NativeControlClient
    from control_shm_support import _install_provider_shm_state

    thread_errors = []
    threading.excepthook = lambda args: thread_errors.append(f"{args.exc_type.__name__}: {args.exc_value}")
    mode, identity = sys.argv[1], sys.argv[2]
    if mode == "own-handler":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(7))
    client = object.__new__(NativeControlClient)
    client._identity = identity
    client._handler = None
    client._server_thread = None
    client._server_stop = threading.Event()
    client._server_ready = threading.Event()
    client._server_connection_threads = set()
    client._server_connection_threads_lock = threading.Lock()
    _install_provider_shm_state(client)
    client._local_endpoint = None
    client._apply_local_ipc_socket_policy = lambda path: None
    client._register_managed_handler = lambda: 1
    client.start_server(lambda request: {})
    print(client._local_endpoint, flush=True)
    if mode == "exit":
        sys.exit(0)
    if mode == "fork-stop":
        import json, os, socket
        from r2kernel.core.control_cost_metrics import ControlCostMetrics
        client._control_cost_metrics = ControlCostMetrics(enabled=False)
        pid = os.fork()
        if pid == 0:
            client.stop_server()  # the child stops its inherited copy of the client
            os._exit(0)
        child = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(2.0)
            probe.connect(client._local_endpoint)
        print(json.dumps({"child": child, "exists": os.path.exists(client._local_endpoint),
                          "server_alive": client._server_thread.is_alive()}), flush=True)
        client.stop_server()
        print(json.dumps({"exists_after_parent_stop": os.path.exists(client._local_endpoint or ""),
                          "thread_errors": thread_errors}), flush=True)
        sys.exit(0)
    while True:
        time.sleep(0.2)
''')

# The first registration comes from a worker thread (which cannot install the SIGTERM handler); a later
# main-thread registration must still install it.
WORKER_FIRST = textwrap.dedent('''
    import socket, sys, threading, time
    from r2kernel.core import _notify_endpoints

    paths = [sys.argv[1] + suffix for suffix in ("-worker.sock", "-main.sock")]
    sockets = []
    def bind_and_register(path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        sockets.append(listener)
        _notify_endpoints.register(path)
    worker = threading.Thread(target=bind_and_register, args=(paths[0],))
    worker.start()
    worker.join()
    bind_and_register(paths[1])
    print("\\n".join(paths), flush=True)
    while True:
        time.sleep(0.2)
''')


class ControlHandlerSocketLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = dict(os.environ, R2KERNEL_FORCE_PYTHON="1",
                        PYTHONPATH=os.pathsep.join([str(SDK_ROOT), str(TESTS_DIR), os.environ.get("PYTHONPATH", "")]))
        self.endpoints: list[str] = []

    def tearDown(self) -> None:
        for endpoint in self.endpoints:
            try:
                os.unlink(endpoint)
            except FileNotFoundError:
                pass

    def _start_provider(self, mode: str) -> tuple[subprocess.Popen, str]:
        identity = f"qa_handler_{uuid.uuid4().hex[:8]}"
        process = subprocess.Popen([sys.executable, "-c", PROVIDER, mode, identity], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        endpoint = process.stdout.readline().strip()
        self.assertTrue(endpoint, process.stderr.read() if not endpoint else endpoint)
        self.assertIn(f"r2kernel-control-handler-{identity}-", endpoint)
        self.endpoints.append(endpoint)
        self.assertTrue(mode == "exit" or os.path.exists(endpoint), endpoint)  # "exit" may already be gone
        return process, endpoint

    def test_sigterm_removes_the_handler_socket_and_still_terminates_by_sigterm(self) -> None:
        process, endpoint = self._start_provider("wait")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(endpoint))

    def test_normal_exit_removes_the_handler_socket(self) -> None:
        process, endpoint = self._start_provider("exit")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(os.path.exists(endpoint))

    def test_an_application_sigterm_handler_is_left_in_place(self) -> None:
        process, endpoint = self._start_provider("own-handler")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 7)  # the application's handler ran (sys.exit -> atexit)
        self.assertFalse(os.path.exists(endpoint))

    def test_a_forked_child_stopping_the_inherited_client_leaves_the_parent_socket(self) -> None:
        import json
        process, endpoint = self._start_provider("fork-stop")
        after_child = process.stdout.readline().strip()
        self.assertTrue(after_child, process.stderr.read() if not after_child else after_child)
        self.assertEqual(json.loads(after_child), {"child": 0, "exists": True, "server_alive": True})
        after_parent = process.stdout.readline().strip()
        self.assertTrue(after_parent, process.stderr.read() if not after_parent else after_parent)
        self.assertEqual(json.loads(after_parent), {"exists_after_parent_stop": False, "thread_errors": []})
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(os.path.exists(endpoint), "the parent still removes its own socket")

    def test_a_worker_thread_registration_does_not_disable_the_sigterm_cleanup(self) -> None:
        base = f"/tmp/r2k-qa-{uuid.uuid4().hex[:8]}"
        process = subprocess.Popen([sys.executable, "-c", WORKER_FIRST, base], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        endpoints = [process.stdout.readline().strip() for _ in range(2)]
        self.endpoints.extend(endpoints)
        self.assertTrue(all(endpoints), process.stderr.read() if not all(endpoints) else endpoints)
        self.assertTrue(all(os.path.exists(endpoint) for endpoint in endpoints), endpoints)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual([endpoint for endpoint in endpoints if os.path.exists(endpoint)], [])


if __name__ == "__main__":
    unittest.main()
