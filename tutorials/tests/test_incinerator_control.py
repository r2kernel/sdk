from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir())
INCINERATOR_DIR = ROOT / "subsystems" / "component.incinerator"
GATEWAY_DIR = ROOT / "subsystems" / "gateway.device_gateway"
for path in (INCINERATOR_DIR, GATEWAY_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from device_gateway import DeviceGatewaySubsystem  # noqa: E402
from incinerator_controller import IncineratorControllerSubsystem  # noqa: E402
from control_cycles import ControlCycleIssuer, ControlCycleOrdering  # noqa: E402

SOURCE = "component.incinerator"


def drive_result(applied: bool = True, detail: str = "") -> tuple[bool, dict[str, object]]:
    status = "applied" if applied else "rejected"
    result: dict[str, object] = {"status": status}
    if detail:
        result["detail"] = detail
    return applied, {"ok": True, "result": result}


def cycle(sequence: int, intent: str = "command", state_revision: int | None = None, generation: int = 10) -> dict:
    return {
        "generation": generation,
        "sequence": sequence,
        "intent": intent,
        "state_revision": sequence if state_revision is None else state_revision,
    }


def context(valid_until_sync_ns: int = 5_000, source: str = SOURCE) -> SimpleNamespace:
    return SimpleNamespace(source_subsystem=source, valid_until_sync_ns=valid_until_sync_ns)


def output_gateway(now_ns: int = 1_000) -> DeviceGatewaySubsystem:
    gateway = DeviceGatewaySubsystem.__new__(DeviceGatewaySubsystem)
    gateway._name = "gateway.device_gateway"
    gateway._digital_outputs = {}
    gateway._digital_output_expiry_ns = {}
    gateway._digital_output_lock = threading.Lock()
    gateway._digital_output_ordering = ControlCycleOrdering()
    gateway._digital_output_hold = None
    gateway._digital_watchdog_stop = threading.Event()
    gateway._digital_watchdog_thread = None
    gateway._simulator = Mock()
    gateway._simulator.call.return_value = {"ok": True}
    gateway.now_ns = Mock(return_value=now_ns)
    gateway._logger = Mock()
    return gateway


def trigger_component(pulse_ms: float = 5.0) -> IncineratorControllerSubsystem:
    component = IncineratorControllerSubsystem.__new__(IncineratorControllerSubsystem)
    component._burn_lock = threading.Lock()
    component._pulse_ms = pulse_ms
    component._control_period_ms = 5.0
    component._output_index = 1
    component._logger = Mock()
    return component


class IncineratorControlTest(unittest.TestCase):
    def test_trigger_refreshes_output_and_confirms_safe_off(self) -> None:
        component = trigger_component(pulse_ms=12.0)
        component._drive_output = Mock(return_value=drive_result())
        result = component._trigger({})

        values = [call.args[0] for call in component._drive_output.call_args_list]
        self.assertGreaterEqual(values.count(True), 2)
        self.assertEqual(values[-1], False)
        self.assertTrue(result["value"])

    def test_rejected_trigger_still_deasserts_output(self) -> None:
        component = trigger_component()
        component._drive_output = Mock(side_effect=[drive_result(False), drive_result(True)])
        result = component._trigger({})

        self.assertEqual([call.args[0] for call in component._drive_output.call_args_list], [True, False])
        self.assertFalse(result["value"])

    def test_high_transport_timeout_is_reported_and_still_deasserts(self) -> None:
        component = trigger_component()
        component._drive_output = Mock(side_effect=[
            (False, {"ok": False, "transport": {"ok": False, "error": "timeout", "timeout": True},
                     "payload": {"private_marker": "must-not-be-logged"}}),
            drive_result(True),
        ])

        result = component._trigger({})

        self.assertFalse(result["value"])
        self.assertEqual([call.args[0] for call in component._drive_output.call_args_list], [True, False])
        component.logger.warning.assert_called_once_with("Trigger", "HIGH: timeout")
        self.assertFalse(component._burn_lock.locked())

    def test_high_domain_rejection_is_reported_and_still_deasserts(self) -> None:
        component = trigger_component()
        component._drive_output = Mock(side_effect=[
            drive_result(False, detail="keep must name the current output state_revision"),
            drive_result(True),
        ])

        result = component._trigger({})

        self.assertFalse(result["value"])
        self.assertEqual([call.args[0] for call in component._drive_output.call_args_list], [True, False])
        component.logger.warning.assert_called_once_with(
            "Trigger", "HIGH: digital output rejected: keep must name the current output state_revision")
        self.assertFalse(component._burn_lock.locked())

    def test_safe_off_rejection_is_reported_after_successful_high_pulse(self) -> None:
        component = trigger_component()
        component._drive_output = Mock(side_effect=lambda value: drive_result(value))

        result = component._trigger({})

        self.assertFalse(result["value"])
        self.assertTrue(component._drive_output.call_args_list[0].args[0])
        self.assertFalse(component._drive_output.call_args_list[-1].args[0])
        component.logger.warning.assert_called_once_with("Trigger", "safe-off: digital output rejected")
        self.assertFalse(component._burn_lock.locked())

    def test_failed_high_and_safe_off_both_remain_in_warning(self) -> None:
        component = trigger_component()
        component._drive_output = Mock(side_effect=[
            (False, {"ok": False, "transport": {"error": "timeout", "timeout": True}}),
            drive_result(False),
        ])

        result = component._trigger({})

        self.assertFalse(result["value"])
        self.assertEqual([call.args[0] for call in component._drive_output.call_args_list], [True, False])
        component.logger.warning.assert_called_once_with(
            "Trigger", "HIGH: timeout; safe-off: digital output rejected")
        self.assertFalse(component._burn_lock.locked())

    def test_drive_output_commands_changes_and_keeps_a_held_high_output(self) -> None:
        component = trigger_component()
        component._binding = "timed_digital_output"
        component._control_period_ms = 20.0
        component._last_control_tick_ns = None
        component._output_cycles = ControlCycleIssuer(lambda: 10)
        component._held_value = None
        component.now_ns = Mock(return_value=1_000)
        gateway = output_gateway()

        def transact(**kwargs):
            self.assertEqual(kwargs["topic"], "digital_output_control")
            self.assertEqual(kwargs["request_type"], "msg.io.digital.timed_output.v1/TimedOutputControlRequest")
            self.assertEqual(kwargs["response_type"], "msg.io.digital.timed_output.v1/TimedOutputControlResponse")
            return {"ok": True, "result": gateway._digital_output_control(kwargs["value"], context())}

        component.transact_bound_control = Mock(side_effect=transact)

        self.assertTrue(component._drive_output(True)[0])
        self.assertTrue(component._drive_output(True)[0])
        self.assertTrue(component._drive_output(False)[0])

        requests = [call.kwargs["value"] for call in component.transact_bound_control.call_args_list]
        self.assertEqual(requests[0], {"cycle": cycle(1), "output": {"index": 1, "value": True}})
        self.assertEqual(requests[1], {"cycle": cycle(2, "keep", state_revision=1)})
        self.assertEqual(requests[2], {"cycle": cycle(3, state_revision=2), "output": {"index": 1, "value": False}})
        self.assertFalse(gateway._digital_outputs[1])

    def test_drive_output_recommands_after_a_rejected_keep(self) -> None:
        component = trigger_component()
        component._binding = "timed_digital_output"
        component._last_control_tick_ns = None
        component._output_cycles = ControlCycleIssuer(lambda: 10)
        component._held_value = True
        component._output_cycles.command()
        component.now_ns = Mock(return_value=1_000)

        def respond(**kwargs):
            sent = kwargs["value"]["cycle"]
            status = "rejected" if sent["intent"] == "keep" else "applied"
            return {"ok": True, "result": {"cycle": sent, "status": status, "channel_index": 1}}

        component.transact_bound_control = Mock(side_effect=respond)

        self.assertFalse(component._drive_output(True)[0])
        self.assertTrue(component._drive_output(True)[0])
        intents = [call.kwargs["value"]["cycle"]["intent"] for call in component.transact_bound_control.call_args_list]
        self.assertEqual(intents, ["keep", "command"])

    def test_gateway_incinerates_only_on_rising_edge(self) -> None:
        gateway = output_gateway()

        gateway._apply_digital_output(1, True)
        gateway._apply_digital_output(1, True)
        gateway._apply_digital_output(1, False)

        incinerate_calls = [call for call in gateway._simulator.call.call_args_list if call.args[0] == "incinerate"]
        self.assertEqual(len(incinerate_calls), 1)
        self.assertFalse(gateway._digital_outputs[1])

    def test_gateway_command_applies_output_and_arms_the_validity_lease(self) -> None:
        gateway = output_gateway()

        response = gateway._digital_output_control(
            {"cycle": cycle(1), "output": {"index": 1, "value": True}}, context(5_000))

        self.assertEqual(response, {"cycle": cycle(1), "status": "applied", "channel_index": 1})
        self.assertNotIn("observed_value", response)
        self.assertTrue(gateway._digital_outputs[1])
        self.assertEqual(gateway._digital_output_expiry_ns, {1: 5_000})

    def test_gateway_keep_renews_the_lease_without_reapplying_output(self) -> None:
        gateway = output_gateway()
        gateway._digital_output_control({"cycle": cycle(1), "output": {"index": 1, "value": True}}, context(5_000))
        calls_after_command = gateway._simulator.call.call_count

        response = gateway._digital_output_control({"cycle": cycle(2, "keep", state_revision=1)}, context(9_000))

        self.assertEqual(response["status"], "applied")
        self.assertEqual(response["channel_index"], 1)
        self.assertEqual(gateway._digital_output_expiry_ns, {1: 9_000})
        self.assertEqual(gateway._simulator.call.call_count, calls_after_command)

    def test_gateway_rejects_invalid_keep_cycles(self) -> None:
        cases = {
            "no established state": ([], {"cycle": cycle(1, "keep", state_revision=1)}),
            "wrong revision": (
                [{"cycle": cycle(1), "output": {"index": 1, "value": True}}],
                {"cycle": cycle(2, "keep", state_revision=2)},
            ),
            "carries output": (
                [{"cycle": cycle(1), "output": {"index": 1, "value": True}}],
                {"cycle": cycle(2, "keep", state_revision=1), "output": {"index": 1, "value": True}},
            ),
            "other generation": (
                [{"cycle": cycle(1), "output": {"index": 1, "value": True}}],
                {"cycle": cycle(1, "keep", state_revision=1, generation=11)},
            ),
        }
        for label, (setup, keep_request) in cases.items():
            with self.subTest(case=label):
                gateway = output_gateway()
                for request in setup:
                    self.assertEqual(gateway._digital_output_control(request, context())["status"], "applied")
                expiry_before = dict(gateway._digital_output_expiry_ns)

                response = gateway._digital_output_control(keep_request, context(9_000))

                self.assertEqual(response["status"], "rejected")
                self.assertEqual(response["cycle"], keep_request["cycle"])
                self.assertIn("detail", response)
                self.assertEqual(gateway._digital_output_expiry_ns, expiry_before)

    def test_gateway_rejects_out_of_order_and_stale_cycles_without_effect(self) -> None:
        gateway = output_gateway()
        gateway._digital_output_control({"cycle": cycle(2), "output": {"index": 1, "value": True}}, context())
        calls_after_command = gateway._simulator.call.call_count

        for request in (
            {"cycle": cycle(2, state_revision=3), "output": {"index": 1, "value": False}},
            {"cycle": cycle(1, state_revision=3), "output": {"index": 1, "value": False}},
            {"cycle": cycle(9, generation=9), "output": {"index": 1, "value": False}},
            {"cycle": cycle(3, state_revision=2), "output": {"index": 1, "value": False}},
            {"cycle": cycle(4, "sample", state_revision=0)},
        ):
            with self.subTest(request=request):
                response = gateway._digital_output_control(request, context())
                self.assertEqual(response["status"], "rejected")
                self.assertEqual(response["channel_index"], 1)
        self.assertTrue(gateway._digital_outputs[1])
        self.assertEqual(gateway._simulator.call.call_count, calls_after_command)

    def test_gateway_rejects_expired_requests_before_dispatch(self) -> None:
        gateway = output_gateway(now_ns=6_000)

        response = gateway._digital_output_control(
            {"cycle": cycle(1), "output": {"index": 1, "value": True}}, context(5_000))

        self.assertEqual(response["status"], "rejected")
        gateway._simulator.call.assert_not_called()
        self.assertEqual(gateway._digital_output_expiry_ns, {})

    def test_gateway_requires_cycle_and_transport_source(self) -> None:
        gateway = output_gateway()
        with self.assertRaises(ValueError):
            gateway._digital_output_control({"output": {"index": 1, "value": True}}, context())
        with self.assertRaises(ValueError):
            gateway._digital_output_control(
                {"cycle": cycle(1), "output": {"index": 1, "value": True}}, SimpleNamespace(valid_until_sync_ns=5_000))

    def test_gateway_arms_lease_before_driver_acknowledgement(self) -> None:
        gateway = output_gateway(now_ns=100)
        gateway._apply_digital_output = Mock(side_effect=RuntimeError("acknowledgement lost"))

        response = gateway._digital_output_control(
            {"cycle": cycle(1), "output": {"index": 1, "value": True}}, context(123))

        self.assertEqual(response["status"], "device_error")
        self.assertIn("acknowledgement lost", response["detail"])
        self.assertEqual(gateway._digital_output_expiry_ns, {1: 123})
        self.assertIsNone(gateway._digital_output_hold)

    def test_gateway_shutdown_forces_leased_output_off(self) -> None:
        gateway = output_gateway()
        gateway._digital_outputs = {1: True}
        gateway._digital_output_expiry_ns = {1: 123}

        gateway._stop_digital_output_watchdog()

        self.assertFalse(gateway._digital_outputs[1])
        self.assertEqual(gateway._digital_output_expiry_ns, {})

    def test_gateway_watchdog_forces_expired_control_output_off_and_blocks_keep(self) -> None:
        gateway = output_gateway(now_ns=1_000)
        gateway._digital_output_control({"cycle": cycle(1), "output": {"index": 1, "value": True}}, context(1_500))
        self.assertTrue(gateway._digital_outputs[1])
        gateway.now_ns.return_value = 2_000

        gateway._start_digital_output_watchdog()
        deadline = time.monotonic() + 0.5
        while gateway._digital_outputs[1] and time.monotonic() < deadline:
            time.sleep(0.005)
        gateway._stop_digital_output_watchdog()

        self.assertFalse(gateway._digital_outputs[1])
        gateway.logger.warning.assert_called_once()
        keep = gateway._digital_output_control({"cycle": cycle(2, "keep", state_revision=1)}, context(9_000))
        self.assertEqual(keep["status"], "rejected")
        self.assertFalse(gateway._digital_outputs[1])


if __name__ == "__main__":
    unittest.main()
