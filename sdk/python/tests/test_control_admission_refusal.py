"""Control's Unix listener refusals are read and retried, not reported as a broken pipe.

Over its per-UID or total connection limits, Control writes a ``retryable`` refusal and closes the
connection without reading it. A client whose send comes after that close fails with EPIPE while the
refusal is already buffered; 64 subsystems starting together reported "Control unreachable: Broken
pipe". The fake listener below refuses exactly that way: reply, then close before the request.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from r2kernel.core.control_client import ControlAdmissionRefused, exchange_control_request

REFUSAL = {"status": "error", "error": "Control Unix socket refused the connection: per-UID connection limit "
           "reached; retry later", "error_code": "per_uid_connection_limit", "retryable": True}


class RefusingControl:
    """Refuses the first ``refusals`` connections as Control does, then answers each request."""

    def __init__(self, path: str, refusals: int, *, reply: dict | None = None):
        self.refusals = refusals
        self.reply = reply or {"res": "ok"}
        self.connections = 0
        self.requests: list[dict] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            with connection:
                self.connections += 1
                try:
                    if self.connections <= self.refusals:
                        connection.sendall(json.dumps(REFUSAL).encode())
                        continue
                    data = b""
                    while chunk := connection.recv(4096):
                        data += chunk
                    self.requests.append(json.loads(data))
                    connection.sendall(json.dumps(self.reply).encode())
                except OSError:
                    continue  # the client gave up on this connection

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join()
        self.listener.close()


class AdmissionRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="r2k-admission-")
        self.path = os.path.join(self.directory, "control.sock")
        self.env = patch.dict(os.environ, {"R2KERNEL_CONTROL_SOCKET": self.path,
                                           "R2KERNEL_RESOURCE_SERVICE_MODE": "disabled"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(os.rmdir, self.directory)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def control(self, refusals: int, **kwargs) -> RefusingControl:
        control = RefusingControl(self.path, refusals, **kwargs)
        self.addCleanup(control.stop)
        return control

    def test_refused_connections_are_retried_until_admitted(self) -> None:
        # Large enough that the send is still under way when the refused connection closes.
        payload = {"cmd": "update_subsystem_status", "param": {"pad": "x" * 512 * 1024}}
        control = self.control(refusals=3)
        self.assertEqual(exchange_control_request(payload, timeout_s=1.0, retries=1), {"res": "ok"})
        self.assertEqual(control.connections, 4)
        self.assertEqual(len(control.requests), 1)

    def test_a_retry_does_not_use_one_of_the_requested_attempts(self) -> None:
        control = self.control(refusals=2)
        deadline = time.monotonic() + 1.0
        self.assertEqual(exchange_control_request({"cmd": "clock_status"}, deadline=deadline), {"res": "ok"})
        self.assertEqual(control.connections, 3)
        sent = exchange_control_request({"cmd": "x"}, timeout_s=1.0, retries=1, expect_response=False)
        self.assertIsNone(sent)

    def test_a_refusal_that_outlasts_the_time_budget_is_raised_with_its_code(self) -> None:
        self.control(refusals=10_000)
        started = time.monotonic()
        with self.assertRaises(ControlAdmissionRefused) as raised:
            exchange_control_request({"cmd": "clock_status"}, timeout_s=0.2, retries=1)
        self.assertEqual(raised.exception.reason, "per_uid_connection_limit")
        self.assertIn("retry later", raised.exception.error)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_other_error_replies_are_returned_unchanged(self) -> None:
        reply = {"status": "error", "error": "unknown command", "reason": "unknown_command"}
        self.control(refusals=0, reply=reply)
        self.assertEqual(exchange_control_request({"cmd": "nope"}, timeout_s=1.0, retries=1), reply)


if __name__ == "__main__":
    unittest.main()
