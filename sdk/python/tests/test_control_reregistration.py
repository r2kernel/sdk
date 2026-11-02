"""A running subsystem registers again after Control restarts (fake Control over a real Unix socket).

Regression for the installed rehearsal finding: after `systemctl restart r2kernel-control` the Python
heartbeat only re-registered its Control handler, Control refused every status update (the
registration was gone) and the subsystem never became visible again.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from multiprocessing import shared_memory
from unittest.mock import patch

from r2kernel.core import _control_registration as registration
from r2kernel.core._control_registration import ControlRegistrationKeeper, status_refusal
from r2kernel.core.subsystem_runtime import SubsystemRuntime

from fake_control_support import MISSING_REGISTRATION, SLOT_COUNT, SLOT_SIZE, FakeControl, RecordingLogger


class KeeperBackoffTests(unittest.TestCase):
    def test_status_refusal_accepts_only_explicit_acknowledgement(self) -> None:
        self.assertIsNone(status_refusal({"ok": True, "res": "ok"}))
        self.assertIn(MISSING_REGISTRATION, status_refusal({"status": "error", "error": MISSING_REGISTRATION}))
        self.assertIsNotNone(status_refusal(None))
        self.assertIsNotNone(status_refusal({"res": "ok"}))

    def test_retries_back_off_to_the_documented_bound_and_log_every_failure(self) -> None:
        now = [100.0]
        logger = RecordingLogger()
        keeper = ControlRegistrationKeeper(logger=logger, clock=lambda: now[0])
        attempts: list[float] = []

        def reregister():
            attempts.append(now[0])
            return "Control unreachable" if len(attempts) < 6 else None

        while len(attempts) < 6:
            keeper.heartbeat(lambda: "Control unreachable", reregister)
            now[0] += registration.HEARTBEAT_PERIOD_S
        gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        self.assertEqual(attempts[0], 100.0)  # first attempt in the heartbeat that saw the failure
        self.assertEqual(gaps, [1.0, 2.0, 4.0, 4.0, 4.0])
        self.assertLessEqual(max(gaps), registration.REREGISTRATION_START_BOUND_S)
        self.assertEqual(registration.REREGISTRATION_START_BOUND_S, 5.0)
        self.assertFalse(keeper.registration_lost)
        failures = [m for m in logger.messages("error") if m.startswith("Registration attempt")]
        self.assertEqual(len(failures), 5)
        self.assertTrue(any("Registered with Control again" in m for m in logger.messages("info")))

    def test_accepted_heartbeat_never_reregisters(self) -> None:
        keeper = ControlRegistrationKeeper(logger=RecordingLogger())
        calls = []
        for _ in range(3):
            keeper.heartbeat(lambda: None, lambda: calls.append(1))
        self.assertEqual(calls, [])


class FakeControlCase(unittest.TestCase):
    """A SubsystemRuntime registered with a restartable fake Control (no tests of its own)."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="r2k-rereg-")
        self.socket_path = os.path.join(self.directory, "control.sock")
        self.pool = shared_memory.SharedMemory(name=f"r2k-rereg-{uuid.uuid4().hex[:12]}", create=True,
                                               size=SLOT_SIZE * SLOT_COUNT)
        self.control = FakeControl(self.socket_path, self.pool.name)
        self.env = patch.dict(os.environ, {
            "R2KERNEL_CONTROL_SOCKET": self.socket_path,
            "R2KERNEL_CONTROL_NOTIFY_DIR": self.directory,
            "R2KERNEL_RESOURCE_SERVICE_MODE": "disabled",
            "R2KERNEL_RESOURCE_LEASE_ID": "lease-qa-1",
            "R2KERNEL_FORCE_PYTHON": "1",
            "R2KERNEL_STARTUP_RETRY_COUNT": "1",
        })
        self.env.start()
        self.logger = RecordingLogger()
        self.runtime = SubsystemRuntime(subsystem_name="qa.rereg", logger=self.logger, owner=object())

    def tearDown(self) -> None:
        self.runtime.stop_heartbeat()
        self.runtime._client.stop()
        self.env.stop()
        self.control.stop()
        self.pool.close()
        self.pool.unlink()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _wait_for(self, predicate, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return predicate()


class ControlRestartTests(FakeControlCase):
    def test_subsystem_registers_again_with_its_identity_and_endpoints_after_control_restart(self) -> None:
        client = self.runtime._client
        self.assertTrue(client.request_publish_slots("qa.rereg", "qa.pub", 64))
        self.assertTrue(client.subscribe_event("qa.sub", 64, lambda payload, timestamp: None))
        first_registration = self.control.registrations["qa.rereg"]
        self.runtime.start_heartbeat(state_getter=lambda: "Running")
        self.assertTrue(self._wait_for(
            lambda: any(r["cmd"] == "update_subsystem_status" for r in self.control.requests_since(0)), 3.0))

        down_s = 2.5
        self.control.stop()  # Control stops: socket gone, every registration lost
        time.sleep(down_s)
        mark = self.control.request_count()
        restarted_at = time.monotonic()
        self.control.start()

        def reregistered():
            requests = self.control.requests_since(mark)
            names = [r["cmd"] for r in requests]
            if "register_subscription" not in names:
                return None
            return requests
        requests = self._wait_for(reregistered, registration.REREGISTRATION_START_BOUND_S + 3.0)
        elapsed = time.monotonic() - restarted_at
        self.assertIsNotNone(requests, self.logger.records)
        commands = [r["cmd"] for r in requests]
        register = requests[commands.index("register_subsystem")]
        for field in ("session_id", "pid", "lease_id", "notify_endpoint"):
            self.assertEqual(register[field], first_registration[field], field)
        self.assertEqual(register["lease_id"], "lease-qa-1")
        accepted_status = [r for r in requests[commands.index("register_subsystem"):]
                           if r["cmd"] == "update_subsystem_status"]
        self.assertTrue(accepted_status)
        self.assertEqual(accepted_status[0]["param"]["lease_id"], "lease-qa-1")
        self.assertEqual(accepted_status[0]["param"]["session_id"], first_registration["session_id"])
        # Order: registration, full status, then the endpoints on the new registration.
        self.assertLess(commands.index("register_subsystem"), commands.index("request_publish_slots"))
        self.assertLess(commands.index("update_subsystem_status", commands.index("register_subsystem")),
                        commands.index("request_publish_slots"))
        publish = requests[commands.index("request_publish_slots")]
        self.assertEqual((publish["topic"], publish["size"], publish["session_id"]),
                         ("qa.pub", 64, first_registration["session_id"]))
        self.assertEqual(requests[commands.index("register_subscription")]["topic"], "qa.sub")
        self.assertEqual(client._event._publish_slots["qa.pub"], 4 + self.control.generation)
        self.assertLessEqual(elapsed, registration.REREGISTRATION_START_BOUND_S + 1.0)
        self.assertTrue(self._wait_for(
            lambda: any("Registered with Control again" in m for m in self.logger.messages("info")), 2.0))
        errors = self.logger.messages("error")
        self.assertTrue(any("registering with Control again" in m for m in errors), errors)
        self.assertGreaterEqual(len([m for m in errors if m.startswith("Registration attempt")]), 1, errors)

    def test_control_restart_between_heartbeats_is_detected_by_the_refused_status(self) -> None:
        self.runtime.start_heartbeat(state_getter=lambda: "Running")
        self.control.stop()
        mark = self.control.request_count()
        self.control.start()  # restarted before the next heartbeat: only the refused status shows it
        requests = self._wait_for(
            lambda: [r for r in self.control.requests_since(mark) if r["cmd"] == "register_subsystem"], 4.0)
        self.assertTrue(requests, self.logger.records)
        self.assertTrue(any(MISSING_REGISTRATION in m for m in self.logger.messages("error")))

    def test_no_registration_after_the_subsystem_reported_itself_down(self) -> None:
        self.runtime.start_heartbeat(state_getter=lambda: "Running")
        self.runtime.stop_heartbeat()
        self.runtime.inform_subsystem_down()
        mark = self.control.request_count()
        self.runtime.send_heartbeat_once()
        self.assertEqual(self.control.requests_since(mark), [])


class LifecycleStatusTests(FakeControlCase):
    """fault()/degrade() status updates never wait behind a re-registration."""

    def _lose_registration(self) -> int:
        self.control.stop()
        mark = self.control.request_count()
        self.control.start()
        return mark

    def test_lifecycle_status_does_not_wait_for_a_registration_in_progress(self) -> None:
        state = ["Running"]
        self.runtime.start_heartbeat(state_getter=lambda: state[0])
        self.assertTrue(self._wait_for(
            lambda: any(r["cmd"] == "update_subsystem_status" for r in self.control.requests_since(0)), 3.0))
        self.control.register_delay_s = 3.0
        self._lose_registration()
        # The heartbeat finds the loss and registers again; Control answers slowly.
        self.assertTrue(self.control.register_started.wait(4.0), self.logger.records)
        self.assertTrue(self._wait_for(lambda: self.runtime._registration_lock.locked(), 1.0))
        state[0] = "Faulted"
        mark = self.control.request_count()
        started = time.monotonic()
        self.runtime.send_lifecycle_status()
        self.assertLess(time.monotonic() - started, 0.5, "the lifecycle status waited for the re-registration")
        self.assertEqual([r["cmd"] for r in self.control.requests_since(mark)], [])
        self.assertTrue(any("Lifecycle status Faulted is left to the next heartbeat" in m
                            for m in self.logger.messages("info")), self.logger.records)
        self.control.register_delay_s = 0.0
        # The heartbeat after the registration carries the new state.
        self.assertTrue(self._wait_for(lambda: any(
            r["cmd"] == "update_subsystem_status" and r["param"].get("lifecycle") == "Faulted"
            for r in self.control.requests_since(mark)), 6.0), self.logger.records)

    def test_refused_lifecycle_status_is_logged_and_left_to_the_heartbeat(self) -> None:
        self.runtime.start_heartbeat(state_getter=lambda: "Degraded")
        self.runtime.stop_heartbeat()
        mark = self._lose_registration()
        started = time.monotonic()
        self.runtime.send_lifecycle_status()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual([r["cmd"] for r in self.control.requests_since(mark)], ["update_subsystem_status"],
                         "the lifecycle status registered again inline")
        errors = self.logger.messages("error")
        self.assertTrue(any("Lifecycle status Degraded not accepted" in m and MISSING_REGISTRATION in m
                            for m in errors), errors)
        # The next heartbeat registers again.
        self.runtime.send_heartbeat_once()
        self.assertIn("register_subsystem", [r["cmd"] for r in self.control.requests_since(mark)])

    def test_lifecycle_changes_use_the_non_blocking_status(self) -> None:
        from r2kernel.datatype.common import DeviceState
        from r2kernel.wrapper._subsystem_lifecycle import _LifecycleMixin
        calls = []
        owner = object.__new__(_LifecycleMixin)
        owner._state = None
        owner._logger = self.logger
        owner.on_state_changed = lambda state: None
        owner._runtime = type("Runtime", (), {
            "send_lifecycle_status": lambda _self: calls.append("lifecycle"),
            "send_heartbeat_once": lambda _self: calls.append("heartbeat"),
        })()
        owner._change_device_state(DeviceState.FAULTED)
        self.assertEqual(calls, ["lifecycle"])

if __name__ == "__main__":
    unittest.main()
