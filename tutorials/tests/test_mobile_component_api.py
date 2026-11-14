from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
MOBILE_COMPONENT = TUTORIALS / "subsystems" / "component.mobile"
if str(MOBILE_COMPONENT) not in sys.path:
    sys.path.insert(0, str(MOBILE_COMPONENT))

from mobile_controller import MobileControllerSubsystem  # noqa: E402
from control_cycles import ControlCycleIssuer  # noqa: E402


def servo_reply(status: str = "applied", *, transport: dict | None = None, **extra):
    """Return a transact_bound_control fake that echoes the request cycle."""

    def respond(**kwargs):
        response = {"ok": True, "result": {"cycle": kwargs["value"]["cycle"], "status": status, **extra}}
        if transport is not None:
            response["transport"] = transport
        return response

    return respond


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


class RecordingRuntime:
    """Stands in for the SDK runtime; records what reaches the transport."""

    def __init__(self, **_kwargs) -> None:
        self.published: list[tuple[str, dict]] = []

    def now_ns(self) -> int:
        return 1

    def publish_event(self, *, topic: str, value, timestamp=None) -> bool:
        self.published.append((topic, value))
        return True


class MobileComponentApiTest(unittest.TestCase):
    def test_mobile_base_component_contracts_include_control_free_reference_api(self) -> None:
        api_path = ROOT / "spec" / "apis" / "api" / "locomotion" / "velocity_reference" / "v1.yaml"
        api = load_yaml(api_path)
        self.assertEqual(api["id"], "api.locomotion.velocity_reference.v1")
        self.assertNotIn("controls", api)
        self.assertEqual(
            [(item["name"], item["request"], item["response"]) for item in api["procedures"]],
            [
                (
                    "set_velocity_reference",
                    "msg.locomotion.velocity.v1/VelocityCommand",
                    "msg.locomotion.velocity.v1/CommandAck",
                ),
                (
                    "stop",
                    "msg.locomotion.velocity.v1/StopRequest",
                    "msg.locomotion.velocity.v1/CommandAck",
                ),
            ],
        )

        for path in sorted((ROOT / "spec" / "components" / "component" / "mobile_base").glob("**/v*.yaml")):
            with self.subTest(component=path):
                contract = load_yaml(path)
                self.assertIn("api.locomotion.velocity_reference.v1", contract["conformance_apis"])

    def test_odometry_names_the_frames_of_its_reference_and_body_ports(self) -> None:
        manifest = MOBILE_COMPONENT / "subsystem.yaml"
        ports = load_yaml(manifest)["spatial"]["ports"]
        with tempfile.TemporaryDirectory() as user_data, patch(
            "r2kernel.wrapper.subsystem.SubsystemRuntime", RecordingRuntime
        ):
            component = MobileControllerSubsystem(user_data_path=user_data, manifest_path=str(manifest))
            component._publish_odometry()

            topic, sample = component._runtime.published[-1]
            self.assertEqual(topic, "odometry")
            self.assertEqual(
                (sample["frame_id"], sample["child_frame_id"]),
                (ports["reference"]["frame"], ports["body"]["frame"]),
            )
            # api.locomotion.odometry.v2: the SDK refuses a sample that names other frames.
            refused = dict(sample, child_frame_id="base_link")
            self.assertFalse(component.publish_event(topic="odometry", value=refused))
            self.assertEqual(component._runtime.published[-1], (topic, sample))

    def test_procedure_dispatches_timed_control_with_component_generated_bounded_validity(self) -> None:
        component = self._component()
        component._last_servo_tick_ns = 900_000_000
        component.now_ns = Mock(side_effect=[1_000_000_000, 1_000_005_000])
        component.transact_bound_control = Mock(
            side_effect=servo_reply(transport={"ok": True, "timeout": False, "overrun": True})
        )

        acknowledgement = component._set_velocity_reference(
            {"twist": {"linear": {"x": 0.8, "y": 0.0}, "angular": {"z": 0.25}}}
        )

        call = component.transact_bound_control.call_args.kwargs
        self.assertEqual(call["topic"], "servo_control")
        self.assertEqual(call["request_type"], "msg.actuator.servo.v1/ServoControlRequest")
        self.assertEqual(call["response_type"], "msg.actuator.servo.v1/ServoControlResponse")
        self.assertEqual(call["period_ms"], 100.0)
        self.assertEqual(call["timing"]["generated_at_sync_ns"], 1_000_000_000)
        self.assertEqual(call["timing"]["valid_for_ns"], 250_000_000)
        self.assertEqual(call["timing"]["late_policy"], "reject")
        self.assertEqual(
            call["value"]["cycle"],
            {"generation": 10, "sequence": 1, "intent": "command", "state_revision": 1},
        )
        self.assertEqual(
            call["value"]["command"],
            {
                "emergency": False,
                "servo_cmd": 0,
                "drive_mode": [],
                "target": {"names": ["base_vx", "base_vy", "base_wz"], "target_velocity": [0.8, 0.0, 0.25]},
            },
        )
        integrated_command = component._integrate_body_transform.call_args.args[0]
        self.assertEqual(integrated_command["dt"], 0.1)
        self.assertTrue(acknowledgement["accepted"])
        self.assertFalse(acknowledgement["deadline_missed"])

    def test_procedure_caps_timed_validity_to_configured_maximum(self) -> None:
        component = self._component()
        component._limits["max_dt"] = 0.05
        component._last_servo_tick_ns = 2_000_000_000
        component.now_ns = Mock(side_effect=[8_000_000_000, 8_000_005_000])
        component.transact_bound_control = Mock(side_effect=servo_reply())

        acknowledgement = component._set_velocity_reference(
            {"twist": {"linear": {"x": 0.4}, "angular": {"z": 0.0}}}
        )

        call = component.transact_bound_control.call_args.kwargs
        self.assertEqual(call["period_ms"], 500.0)
        self.assertEqual(call["timing"]["generated_at_sync_ns"], 8_000_000_000)
        self.assertEqual(call["timing"]["valid_for_ns"], 50_000_000)
        integrated_command = component._integrate_body_transform.call_args.args[0]
        self.assertEqual(integrated_command["dt"], 0.05)
        self.assertTrue(acknowledgement["accepted"])

    def test_procedure_rejects_gateway_domain_rejection(self) -> None:
        for status in ("rejected", "device_error"):
            with self.subTest(status=status):
                component = self._component()
                component.now_ns = Mock(side_effect=[1_000_000_000, 1_000_005_000])
                component.transact_bound_control = Mock(
                    side_effect=servo_reply(status, detail="ControlCycle sequence must strictly increase")
                )

                acknowledgement = component._set_velocity_reference(
                    {"twist": {"linear": {"x": 0.4}, "angular": {"z": 0.0}}}
                )

                self.assertFalse(acknowledgement["accepted"])
                self.assertEqual(acknowledgement["reason"], "ControlCycle sequence must strictly increase")
                self.assertEqual(acknowledgement["applied_twist"]["linear"]["x"], 0.0)
                self.assertFalse(acknowledgement["deadline_missed"])
                component._integrate_body_transform.assert_not_called()

    def test_procedure_cycles_strictly_increase_within_one_generation(self) -> None:
        component = self._component()
        component.now_ns = Mock(return_value=1_000_000_000)
        component.transact_bound_control = Mock(side_effect=servo_reply())

        for _ in range(3):
            self.assertTrue(
                component._set_velocity_reference({"twist": {"linear": {"x": 0.1}, "angular": {"z": 0.0}}})["accepted"]
            )

        cycles = [call.kwargs["value"]["cycle"] for call in component.transact_bound_control.call_args_list]
        self.assertEqual([cycle["generation"] for cycle in cycles], [10, 10, 10])
        self.assertEqual([cycle["sequence"] for cycle in cycles], [1, 2, 3])
        self.assertTrue(all(cycle["intent"] == "command" and cycle["state_revision"] > 0 for cycle in cycles))

    def test_procedure_rejects_missing_or_malformed_gateway_acknowledgement(self) -> None:
        invalid_responses = (
            {"ok": True, "result": {}},
            {"ok": True, "result": {"value": True}},
            {"ok": True, "result": {"status": "applied"}},
            {
                "ok": True,
                "result": {
                    "cycle": {"generation": 10, "sequence": 99, "intent": "command", "state_revision": 99},
                    "status": "applied",
                },
            },
            {"ok": True, "result": True},
        )

        for response in invalid_responses:
            with self.subTest(response=response):
                component = self._component()
                component.now_ns = Mock(side_effect=[1_000_000_000, 1_000_005_000])
                component.transact_bound_control = Mock(return_value=response)

                acknowledgement = component._set_velocity_reference(
                    {"twist": {"linear": {"x": 0.4}, "angular": {"z": 0.0}}}
                )

                self.assertFalse(acknowledgement["accepted"])
                self.assertEqual(acknowledgement["reason"], "servo command failed")

    def test_stop_expiry_reports_last_accepted_twist_and_missed_deadline(self) -> None:
        component = self._component()
        component._last_command = {"vx": 0.35, "vy": -0.1, "wz": 0.2, "dt": 0.16}
        component.now_ns = Mock(side_effect=[1_000_000_000, 1_005_000_000])
        component.transact_bound_control = Mock(
            return_value={
                "ok": False,
                "error_code": "control_response_error",
                "error_message": "Control 'servo_control' rejected an expired timed request",
                "transport": {"ok": False, "timeout": False, "overrun": False},
            }
        )

        acknowledgement = component._stop_drive({})

        self.assertFalse(acknowledgement["accepted"])
        self.assertEqual(
            acknowledgement["reason"],
            "Control 'servo_control' rejected an expired timed request",
        )
        self.assertEqual(acknowledgement["applied_twist"]["linear"]["x"], 0.35)
        self.assertEqual(acknowledgement["applied_twist"]["linear"]["y"], -0.1)
        self.assertEqual(acknowledgement["applied_twist"]["angular"]["z"], 0.2)
        self.assertTrue(acknowledgement["deadline_missed"])
        self.assertEqual(component._last_command["vx"], 0.35)
        self.assertEqual(component._last_command["wz"], 0.2)

    def test_successful_stop_acknowledges_zero_twist(self) -> None:
        component = self._component()
        component._last_command = {"vx": 0.35, "vy": -0.1, "wz": 0.2, "dt": 0.16}
        component.now_ns = Mock(side_effect=[1_000_000_000, 1_005_000_000])
        component.transact_bound_control = Mock(
            side_effect=servo_reply(transport={"ok": True, "timeout": False, "overrun": True})
        )

        acknowledgement = component._stop_drive({})

        call = component.transact_bound_control.call_args.kwargs
        self.assertEqual(call["topic"], "servo_control")
        self.assertFalse(call["value"]["command"]["emergency"])
        self.assertEqual(call["value"]["command"]["target"]["target_velocity"], [0.0, 0.0, 0.0])
        self.assertTrue(acknowledgement["accepted"])
        self.assertEqual(acknowledgement["applied_twist"]["linear"]["x"], 0.0)
        self.assertEqual(acknowledgement["applied_twist"]["linear"]["y"], 0.0)
        self.assertEqual(acknowledgement["applied_twist"]["angular"]["z"], 0.0)
        self.assertFalse(acknowledgement["deadline_missed"])

    def test_stop_acknowledges_code_only_expired_timed_request(self) -> None:
        component = self._component()
        component._last_command = {"vx": 0.35, "vy": -0.1, "wz": 0.2, "dt": 0.16}
        component.now_ns = Mock(side_effect=[1_000_000_000, 1_005_000_000])
        component.transact_bound_control = Mock(
            return_value={"ok": False, "error_code": "expired_timed_request"}
        )

        acknowledgement = component._stop_drive({})

        self.assertFalse(acknowledgement["accepted"])
        self.assertTrue(acknowledgement["deadline_missed"])
        self.assertEqual(acknowledgement["applied_twist"]["linear"]["x"], 0.35)
        self.assertEqual(acknowledgement["applied_twist"]["angular"]["z"], 0.2)

    def test_successful_control_cycle_overrun_does_not_mark_deadline_missed(self) -> None:
        component = self._component()
        component._last_servo_tick_ns = 1_000_000_000
        component.now_ns = Mock(side_effect=[1_100_000_000, 1_101_000_000])
        component.transact_bound_control = Mock(
            side_effect=servo_reply(transport={"ok": True, "timeout": False, "overrun": True})
        )

        acknowledgement = component._set_velocity_reference(
            {"twist": {"linear": {"x": 0.4}, "angular": {"z": 0.0}}}
        )

        self.assertTrue(acknowledgement["accepted"])
        self.assertFalse(acknowledgement["deadline_missed"])

    def test_deadline_flag_uses_explicit_transport_evidence(self) -> None:
        self.assertTrue(
            MobileControllerSubsystem._control_deadline_missed(
                {"transport": {"timeout": True, "overrun": False}}
            )
        )
        self.assertFalse(
            MobileControllerSubsystem._control_deadline_missed(
                {"transport": {"timeout": False, "overrun": True}}
            )
        )
        self.assertTrue(
            MobileControllerSubsystem._control_deadline_missed(
                {"error_code": "deadline_exceeded"}
            )
        )
        self.assertTrue(
            MobileControllerSubsystem._control_deadline_missed(
                {"error_code": "expired_timed_request"}
            )
        )
        self.assertTrue(
            MobileControllerSubsystem._control_deadline_missed(
                {"error_code": "deadline_missed"}
            )
        )
        self.assertFalse(
            MobileControllerSubsystem._control_deadline_missed(
                {"ok": False, "error_message": "actuator rejected command"}
            )
        )
        self.assertFalse(
            MobileControllerSubsystem._control_deadline_missed(
                {"ok": False, "error_message": "invalid deadline configuration"}
            )
        )
        self.assertFalse(
            MobileControllerSubsystem._control_deadline_missed(
                {"ok": False, "error_message": "invalid timeout configuration"}
            )
        )

    def test_procedure_rejects_motion_when_drive_is_unavailable(self) -> None:
        component = self._component()
        component._enabled = False
        component.transact_bound_control = Mock()

        acknowledgement = component._set_velocity_reference(
            {"twist": {"linear": {"x": 0.4}, "angular": {"z": 0.0}}}
        )

        component.transact_bound_control.assert_not_called()
        self.assertFalse(acknowledgement["accepted"])
        self.assertEqual(acknowledgement["reason"], "drive is disabled")

    @staticmethod
    def _component() -> MobileControllerSubsystem:
        component = MobileControllerSubsystem.__new__(MobileControllerSubsystem)
        component._binding = "actuator_servo"
        component._enabled = True
        component._paused = False
        component._faulted = False
        component._fault_detail = ""
        component._limits = {"max_linear": 1.0, "max_angular": 1.0, "max_dt": 0.5}
        component._last_command = {"vx": 0.0, "vy": 0.0, "wz": 0.0, "dt": 0.16}
        component._last_world = {}
        component._last_servo_tick_ns = None
        component._servo_cycles = ControlCycleIssuer(lambda: 10)
        component._logger = Mock()
        component._publish_state = Mock()
        component._integrate_body_transform = Mock()
        return component


if __name__ == "__main__":
    unittest.main()
