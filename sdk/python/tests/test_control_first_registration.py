"""The first registration retries the refusals with which Control asks for a retry.

Control answers a registration it cannot finish within its reply budget with ``reason``
``registration_gate_busy`` or ``registration_cleanup_pending``. The client start used to fail on
that refusal. Fake Control over a real Unix socket.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from multiprocessing import shared_memory
from unittest.mock import patch

from r2kernel.core._event_registration import REGISTRATION_RETRY_REASONS, registration_retry_reason
from r2kernel.core.subsystem_runtime import SubsystemRuntime

from fake_control_support import SLOT_COUNT, SLOT_SIZE, FakeControl, RecordingLogger


class FirstRegistrationRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="r2k-first-")
        self.socket_path = os.path.join(self.directory, "control.sock")
        self.pool = shared_memory.SharedMemory(name=f"r2k-first-{uuid.uuid4().hex[:12]}", create=True,
                                               size=SLOT_SIZE * SLOT_COUNT)
        self.control = FakeControl(self.socket_path, self.pool.name)
        self.env = patch.dict(os.environ, {
            "R2KERNEL_CONTROL_SOCKET": self.socket_path,
            "R2KERNEL_CONTROL_NOTIFY_DIR": self.directory,
            "R2KERNEL_RESOURCE_SERVICE_MODE": "disabled",
            "R2KERNEL_FORCE_PYTHON": "1",
            "R2KERNEL_STARTUP_RETRY_COUNT": "5",
            "R2KERNEL_STARTUP_RETRY_DELAY_MS": "10",
        })
        self.env.start()
        self.runtimes: list[SubsystemRuntime] = []

    def tearDown(self) -> None:
        for runtime in self.runtimes:
            runtime._client.stop()
        self.env.stop()
        self.control.stop()
        self.pool.close()
        self.pool.unlink()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _registrations(self) -> int:
        return sum(1 for request in self.control.requests_since(0) if request["cmd"] == "register_subsystem")

    def test_reason_codes(self) -> None:
        self.assertEqual(REGISTRATION_RETRY_REASONS, {"registration_gate_busy", "registration_cleanup_pending"})
        self.assertEqual(registration_retry_reason({"status": "error", "error": "x", "reason": "registration_gate_busy"}),
                         "registration_gate_busy")
        self.assertIsNone(registration_retry_reason({"status": "error", "error": "x", "reason": "other"}))
        self.assertIsNone(registration_retry_reason({"res": "ok", "reason": "registration_gate_busy"}))
        self.assertIsNone(registration_retry_reason(None))

    def test_each_retry_reason_is_retried_and_the_start_succeeds(self) -> None:
        self.control.registration_refusals.extend(["registration_gate_busy", "registration_cleanup_pending"])
        logger = RecordingLogger()
        self.runtimes.append(SubsystemRuntime(subsystem_name="qa.first.retry", logger=logger, owner=object()))
        self.assertEqual(self._registrations(), 3)
        self.assertIn("qa.first.retry", self.control.registrations)
        retries = [m for m in logger.messages("info") if "Control asked to retry the registration" in m]
        self.assertEqual(len(retries), 2, logger.records)
        self.assertIn("registration_gate_busy", retries[0])
        self.assertIn("registration_cleanup_pending", retries[1])

    def test_another_refusal_fails_the_start_at_once(self) -> None:
        self.control.registration_refusals.append("registration_rejected_for_test")
        with self.assertRaisesRegex(RuntimeError, "registration_rejected_for_test"):
            self.runtimes.append(SubsystemRuntime(subsystem_name="qa.first.refused", logger=RecordingLogger(),
                                                  owner=object()))
        self.assertEqual(self._registrations(), 1)

    def test_retries_are_bounded_by_the_startup_retry_count(self) -> None:
        self.control.registration_refusals.extend(["registration_gate_busy"] * 10)
        with self.assertRaisesRegex(RuntimeError, "registration_gate_busy"):
            self.runtimes.append(SubsystemRuntime(subsystem_name="qa.first.busy", logger=RecordingLogger(),
                                                  owner=object()))
        self.assertEqual(self._registrations(), 5)


if __name__ == "__main__":
    unittest.main()
