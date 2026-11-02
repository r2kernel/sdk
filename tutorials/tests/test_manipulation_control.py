from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

COMPONENT = Path(__file__).resolve().parents[1] / "subsystems" / "component.manipulation"
sys.path.insert(0, str(COMPONENT))
from manipulation_controller import ManipulationControllerSubsystem
from control_cycles import ControlCycleIssuer


def applied(**kwargs):
    return {
        "ok": True,
        "result": {"cycle": kwargs["value"]["cycle"], "status": "applied"},
        "transport": {"ok": True, "timeout": False, "overrun": True},
    }


def domain_status(status):
    def respond(**kwargs):
        return {"ok": True, "result": {"cycle": kwargs["value"]["cycle"], "status": status}}

    return respond


class ManipulationControlTest(unittest.TestCase):
    def component(self, response):
        component = ManipulationControllerSubsystem.__new__(ManipulationControllerSubsystem)
        component._binding = "actuator_servo"
        component._extension = 0.2
        component._paused = False
        component._last_servo_tick_ns = 100
        component._servo_cycles = ControlCycleIssuer(lambda: 7)
        component._logger = Mock()
        component.now_ns = Mock(return_value=200)
        component.transact_bound_control = Mock(
            side_effect=response if callable(response) else None,
            return_value=None if callable(response) else response,
        )
        component._report_tool_transform = Mock()
        component._publish_state = Mock()
        return component

    def test_rejected_or_ambiguous_commands_never_commit_or_replay(self):
        responses = [
            {"ok": False, "error_code": "deadline"},
            {"ok": True, "result": {"value": True}, "transport": {"timeout": True}},
            {"ok": True, "result": {"value": True}, "transport": {"ok": False}},
            {"ok": True, "result": {"value": True}, "transport": {"error": "unavailable"}},
            {"ok": True, "result": {"value": True}, "error_code": "provider_error"},
            {"ok": True, "result": {"value": True}, "transport": []},
            {"ok": True, "result": {"value": True}, "transport": {"ok": "false"}},
            {"result": {"value": True}},
            {"ok": True, "result": {"value": True}},
            {"ok": True, "result": {"status": "applied"}},
            {"ok": True, "result": {
                "cycle": {"generation": 7, "sequence": 9, "intent": "command", "state_revision": 9},
                "status": "applied"}},
            domain_status("rejected"),
            domain_status("device_error"),
            {"ok": True, "result": {}},
            {"ok": True, "transport": []},
            None,
        ]
        for method in ("_move_j", "_move_l", "_move_c", "_joint_velocity"):
            for response in responses:
                with self.subTest(method=method, response=response):
                    component = self.component(response)
                    with self.assertRaises(RuntimeError):
                        getattr(component, method)({"extension": 0.7})
                    self.assertEqual(component._extension, 0.2)
                    self.assertEqual(component._last_servo_tick_ns, 200)
                    component.transact_bound_control.assert_called_once()
                    component._report_tool_transform.assert_not_called()
                    component._publish_state.assert_not_called()

    def test_transport_exception_records_attempt_but_not_confirmation(self):
        component = self.component(None)
        component.transact_bound_control.side_effect = TimeoutError("delivery unknown")
        with self.assertRaises(TimeoutError):
            component._move_j({"extension": 0.7})
        self.assertEqual(component._last_servo_tick_ns, 200)
        self.assertEqual(component._extension, 0.2)
        component.transact_bound_control.assert_called_once()
        component._publish_state.assert_not_called()

    def test_true_ack_commits_command_for_every_shared_caller(self):
        for method in ("_move_j", "_move_l", "_move_c", "_joint_velocity"):
            with self.subTest(method=method):
                component = self.component(applied)
                result = getattr(component, method)({"extension": 0.7})
                self.assertIs(result.get("completed", result.get("value")), True)
                self.assertEqual(component._extension, 0.7)
                component.transact_bound_control.assert_called_once()
                call = component.transact_bound_control.call_args.kwargs
                self.assertEqual(call["topic"], "servo_control")
                self.assertEqual(
                    call["value"],
                    {
                        "cycle": {"generation": 7, "sequence": 1, "intent": "command", "state_revision": 1},
                        "command": {
                            "emergency": False,
                            "servo_cmd": 0,
                            "drive_mode": [],
                            "target": {"names": ["arm_extension"], "target_position": [0.7]},
                        },
                    },
                )
                component._report_tool_transform.assert_called_once()
                component._publish_state.assert_called_once()

    def test_velocity_after_failure_uses_last_confirmed_command(self):
        component = self.component(domain_status("rejected"))
        with self.assertRaises(RuntimeError):
            component._joint_velocity({"extension": 0.7})
        component.transact_bound_control.side_effect = applied
        component._joint_velocity({"velocity": 1.0, "dt": 0.1})
        self.assertAlmostEqual(component._extension, 0.3)
        self.assertEqual(component.transact_bound_control.call_count, 2)
        sequences = [call.kwargs["value"]["cycle"]["sequence"] for call in component.transact_bound_control.call_args_list]
        self.assertEqual(sequences, [1, 2])

    def test_canonical_waypoint_commits_only_acknowledged_target(self):
        component = self.component(applied)
        result = component._move_j({"waypoints": [{"q": {"values": [0.6]}}]})
        self.assertTrue(result["completed"])
        self.assertEqual(component._extension, 0.6)
        component.transact_bound_control.side_effect = domain_status("device_error")
        with self.assertRaises(RuntimeError):
            component._move_j({"waypoints": [{"q": {"values": [0.8]}}]})
        self.assertEqual(component._extension, 0.6)
        self.assertEqual(component.transact_bound_control.call_count, 2)


if __name__ == "__main__":
    unittest.main()
