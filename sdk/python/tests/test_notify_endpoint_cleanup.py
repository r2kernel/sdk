"""Control notify sockets (`r2kernel-notify-<pid>-*.sock`) do not outlive their process.

Regression for the installed rehearsal finding: a subsystem stopped by SIGTERM left its notify socket in
/run/r2kernel, and nothing removed sockets of ended processes.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from multiprocessing import shared_memory
from pathlib import Path

from r2kernel.core import _notify_endpoints

from fake_control_support import SLOT_COUNT, SLOT_SIZE, FakeControl, RecordingLogger

SDK_ROOT = Path(__file__).resolve().parents[1]

# A subsystem client process: registers with the fake Control, reports its notify endpoint, then waits.
CLIENT = textwrap.dedent('''
    import os, signal, sys, time
    from r2kernel.core.event_client import EventClient
    if sys.argv[1] == "own-handler":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(7))
    client = EventClient(subsystem_name="qa.notify")
    print(client._notify_endpoint, flush=True)
    if sys.argv[1] == "exit":
        sys.exit(0)
    while True:
        time.sleep(0.2)
''')


# A subsystem client process that forks a child in one of several ways and reports, as JSON, whether its
# own notify endpoint survived the child's end. The parent's endpoint must still go at the parent's exit.
FORK_PARENT = textwrap.dedent('''
    import json, multiprocessing, os, signal, sys, time
    from r2kernel.core.event_client import EventClient

    def sleeper(_=None):
        time.sleep(30)

    if __name__ == "__main__":
        scenario = sys.argv[1]
        client = EventClient(subsystem_name="qa.notify.fork")
        endpoint = client._notify_endpoint
        context = multiprocessing.get_context("fork")
        if scenario == "process-terminate":
            child = context.Process(target=sleeper)
            child.start()
            time.sleep(0.3)
            child.terminate()
            child.join()
            child_end = child.exitcode
        elif scenario == "pool-exit-running":
            with context.Pool(2) as pool:
                pool.map_async(sleeper, range(2))
                time.sleep(0.5)
            time.sleep(0.3)
            child_end = "pool terminated"
        elif scenario == "fork-sys-exit":
            pid = os.fork()
            if pid == 0:
                sys.exit(0)
            child_end = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        elif scenario == "fork-close-client":
            pid = os.fork()
            if pid == 0:
                client._close_notify_endpoint()
                os._exit(0)
            child_end = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        print(json.dumps({"endpoint": endpoint, "exists": os.path.exists(endpoint), "child_end": child_end,
                          "sigterm_is_sdk": signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL}), flush=True)
''')

def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class NotifyEndpointLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="r2k-notify-")
        self.pool = shared_memory.SharedMemory(name=f"r2k-notify-{uuid.uuid4().hex[:12]}", create=True,
                                               size=SLOT_SIZE * SLOT_COUNT)
        self.control = FakeControl(os.path.join(self.directory, "control.sock"), self.pool.name)
        self.env = dict(os.environ, R2KERNEL_CONTROL_SOCKET=self.control.socket_path,
                        R2KERNEL_CONTROL_NOTIFY_DIR=self.directory, R2KERNEL_RESOURCE_SERVICE_MODE="disabled",
                        R2KERNEL_FORCE_PYTHON="1", R2KERNEL_STARTUP_RETRY_COUNT="1",
                        PYTHONPATH=os.pathsep.join([str(SDK_ROOT), os.environ.get("PYTHONPATH", "")]))

    def tearDown(self) -> None:
        self.control.stop()
        self.pool.close()
        self.pool.unlink()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _start_client(self, mode: str) -> tuple[subprocess.Popen, str]:
        process = subprocess.Popen([sys.executable, "-c", CLIENT, mode], env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        endpoint = process.stdout.readline().strip()
        self.assertTrue(endpoint, process.stderr.read() if not endpoint else endpoint)
        self.assertTrue(mode == "exit" or os.path.exists(endpoint), endpoint)  # "exit" may already be gone
        return process, endpoint

    def test_sigterm_removes_the_endpoint_and_still_terminates_by_sigterm(self) -> None:
        process, endpoint = self._start_client("wait")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(endpoint))

    def test_normal_exit_removes_the_endpoint(self) -> None:
        process, endpoint = self._start_client("exit")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(os.path.exists(endpoint))

    def test_an_application_sigterm_handler_is_left_in_place(self) -> None:
        process, endpoint = self._start_client("own-handler")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 7)  # the application's handler ran (sys.exit -> atexit)
        self.assertFalse(os.path.exists(endpoint))

    def test_next_client_removes_endpoints_of_killed_processes_only(self) -> None:
        process, killed_endpoint = self._start_client("wait")
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        self.assertTrue(os.path.exists(killed_endpoint))  # SIGKILL leaves it behind
        live, live_endpoint = self._start_client("wait")
        try:
            self.assertFalse(os.path.exists(killed_endpoint))  # removed by the next client at its start
            # A dead PID whose socket still accepts connections (another PID namespace) is kept.
            listening = os.path.join(self.directory, _notify_endpoints.endpoint_name(_dead_pid(), "abcdef"))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(listening)
                server.listen(1)
                unrelated = os.path.join(self.directory, "r2kernel-notify-unrelated.sock")
                Path(unrelated).touch()
                logger = RecordingLogger()
                stale = os.path.join(self.directory, _notify_endpoints.endpoint_name(_dead_pid(), "fedcba"))
                Path(stale).touch()
                removed = _notify_endpoints.remove_stale(self.directory, logger)
                self.assertEqual(removed, 1)
                self.assertFalse(os.path.exists(stale))
                self.assertTrue(os.path.exists(live_endpoint))
                self.assertTrue(os.path.exists(listening))
                self.assertTrue(os.path.exists(unrelated))
                self.assertTrue(any("Removed 1 stale notify endpoint" in m for m in logger.messages("info")))
        finally:
            live.send_signal(signal.SIGTERM)
            live.wait(timeout=10)

    def test_client_start_sweeps_stale_endpoints(self) -> None:
        stale = os.path.join(self.directory, _notify_endpoints.endpoint_name(_dead_pid(), "0123456789ab"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(stale)  # bound, then closed without unlink: a killed process's leftover
        process, endpoint = self._start_client("exit")
        process.wait(timeout=10)
        self.assertFalse(os.path.exists(stale))


    def _run_fork_parent(self, scenario: str) -> dict:
        process = subprocess.run([sys.executable, "-c", FORK_PARENT, scenario], env=self.env, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout.strip().splitlines()[-1])
        self.assertFalse(os.path.exists(report["endpoint"]), "the parent removes its endpoint at its own exit")
        return report

    def test_a_terminated_multiprocessing_fork_child_leaves_the_parent_endpoint(self) -> None:
        report = self._run_fork_parent("process-terminate")
        self.assertEqual(report["child_end"], -signal.SIGTERM)
        self.assertTrue(report["exists"], report)
        self.assertTrue(report["sigterm_is_sdk"], "the parent keeps its SIGTERM cleanup")

    def test_a_pool_exit_with_running_tasks_leaves_the_parent_endpoint(self) -> None:
        self.assertTrue(self._run_fork_parent("pool-exit-running")["exists"])

    def test_an_os_fork_child_that_exits_leaves_the_parent_endpoint(self) -> None:
        report = self._run_fork_parent("fork-sys-exit")
        self.assertEqual(report["child_end"], 0)
        self.assertTrue(report["exists"], report)

    def test_a_forked_child_closing_the_inherited_client_leaves_the_parent_endpoint(self) -> None:
        report = self._run_fork_parent("fork-close-client")
        self.assertEqual(report["child_end"], 0)
        self.assertTrue(report["exists"], report)

if __name__ == "__main__":
    unittest.main()
