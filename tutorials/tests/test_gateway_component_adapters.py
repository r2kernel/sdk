from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
for _relative in (
    "subsystems/gateway.device_gateway",
    "subsystems/component.gripper",
    "subsystems/component.perception",
    "subsystems/component.localizer",
    "lib",
):
    _path = str(TUTORIALS / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from device_gateway import DeviceGatewaySubsystem  # noqa: E402
from control_cycles import ControlCycleOrdering  # noqa: E402
from gripper_controller import GripperControllerSubsystem  # noqa: E402
from localizer import LocalizerSubsystem  # noqa: E402
from perception_controller import PerceptionControllerSubsystem, RadarObservationTracker  # noqa: E402
from r2kernel.core.codec_lifecycle import CodecLifecycle  # noqa: E402
from r2kernel.manifest.loader import load_subsystem_manifest  # noqa: E402


def _servo_gateway(now_ns: int = 1_000_000_000) -> DeviceGatewaySubsystem:
    gateway = DeviceGatewaySubsystem.__new__(DeviceGatewaySubsystem)
    gateway._simulator = Mock()
    gateway._simulator.call.return_value = {"robot": {"vx": 0.3, "vy": 0.0, "wz": 0.1, "arm_extension": 0.4}}
    gateway._servo_lock = threading.Lock()
    gateway._servo_ordering = ControlCycleOrdering()
    gateway.now_ns = Mock(return_value=now_ns)
    return gateway


def _servo_request(sequence: int, target: dict, *, generation: int = 10, emergency: bool = False) -> dict:
    return {
        "cycle": {"generation": generation, "sequence": sequence, "intent": "command", "state_revision": sequence},
        "command": {"emergency": emergency, "servo_cmd": 0, "drive_mode": [], "target": target},
    }


BASE_TARGET = {"names": ["base_vx", "base_vy", "base_wz"], "target_velocity": [0.3, 0.0, 0.1]}
ARM_TARGET = {"names": ["arm_extension"], "target_position": [0.4]}


def _servo_context(source: str, valid_until_sync_ns: int = 1_200_000_000) -> SimpleNamespace:
    return SimpleNamespace(source_subsystem=source, valid_until_sync_ns=valid_until_sync_ns)


def _gripper() -> GripperControllerSubsystem:
    component = GripperControllerSubsystem.__new__(GripperControllerSubsystem)
    component._binding = "digital_io"
    component.now_ns = Mock(return_value=100)
    return component


class GatewayComponentAdaptersTest(unittest.TestCase):
    def test_actual_gripper_and_gateway_hooks_prepare_the_same_private_control_identity(self) -> None:
        contract = "api.tutorial.gateway.device_gateway.digital_io.v1"
        caller_requests = {
            "msg.core.common.primitive.v1/IndexedBoolValue",
            "msg.core.common.primitive.v1/UInt32Value",
        }
        identities = {}
        for package_name, subsystem_type in (
            ("component.gripper", GripperControllerSubsystem),
            ("gateway.device_gateway", DeviceGatewaySubsystem),
        ):
            package_root = TUTORIALS / "subsystems" / package_name
            with self.subTest(package=package_name), patch.dict("os.environ", {
                    "R2KERNEL_CONTRACT_SPEC_ROOTS": str(package_root / "spec")}):
                subsystem = subsystem_type.__new__(subsystem_type)
                subsystem._logger = Mock()
                subsystem._subsystem_manifest = load_subsystem_manifest(package_root / "subsystem.yaml")
                manifest = subsystem._subsystem_manifest
                subsystem._declared_apis = list(manifest.declared_apis)
                subsystem._declared_components = list(manifest.declared_components)
                runtime = SimpleNamespace(
                    _owner=subsystem, _rust_message_codec=None,
                    _event=SimpleNamespace(_message_codec=None),
                    _control_dispatch_running=False,
                    _control_target_cache={}, _control_contracts={},
                    _procedure_contracts={}, _operation_contracts={},
                    get_publish_info=lambda: (), get_subscribe_info=lambda: (),
                )

                def register(contracts, name, _handler, **declaration):
                    contracts[name] = declaration

                subsystem.register_procedure = lambda name, handler, **declaration: register(
                    runtime._procedure_contracts, name, handler, **declaration)
                subsystem.register_control_endpoint = lambda name, handler, **declaration: register(
                    runtime._control_contracts, name, handler, **declaration)
                lifecycle = CodecLifecycle(runtime)
                with lifecycle.initialization():
                    self.assertTrue(subsystem.on_initialize())
                    if package_name == "component.gripper":
                        self.assertFalse(runtime._control_contracts)
                        local_types = {
                            value for declaration in runtime._procedure_contracts.values()
                            for key, value in declaration.items() if key.endswith("_type")
                        }
                        self.assertTrue(caller_requests.isdisjoint(local_types))
                    lifecycle.prepare_declarations()
                self.assertTrue(caller_requests.issubset(lifecycle.metadata()))
                identities[package_name] = lifecycle.contract_codecs[contract]
                if package_name == "gateway.device_gateway":
                    for topic in ("digital_output_set", "digital_output_readback"):
                        declaration = runtime._control_contracts[topic]
                        self.assertEqual(declaration["timing_support"], "required")
                        self.assertEqual(declaration["codec"]["request"],
                                         lifecycle.metadata()[declaration["request_type"]])
        self.assertEqual(identities["component.gripper"], identities["gateway.device_gateway"])

    def test_one_servo_control_handler_accepts_mobile_and_manipulation_commands(self) -> None:
        gateway = _servo_gateway()

        mobile_result = gateway._servo_control(_servo_request(1, BASE_TARGET), _servo_context("component.mobile"))
        manipulation_result = gateway._servo_control(
            _servo_request(1, ARM_TARGET), _servo_context("component.manipulation"))

        self.assertEqual(mobile_result["status"], "applied")
        self.assertEqual(mobile_result["cycle"], _servo_request(1, BASE_TARGET)["cycle"])
        self.assertEqual(
            mobile_result["state"],
            {
                "link_alive": True,
                "servo_on": True,
                "state": {"names": ["base_vx", "base_vy", "base_wz"], "position": [], "velocity": [0.3, 0.0, 0.1]},
            },
        )
        self.assertEqual(manipulation_result["status"], "applied")
        self.assertEqual(manipulation_result["state"]["state"], {"names": ["arm_extension"], "position": [0.4]})
        self.assertEqual(gateway._simulator.call.call_count, 2)
        drive_call = gateway._simulator.call.call_args_list[0]
        self.assertEqual(drive_call.args[0], "apply_drive")
        self.assertEqual(drive_call.args[1]["vx"], 0.3)
        self.assertEqual(drive_call.args[1]["wz"], 0.1)
        self.assertAlmostEqual(drive_call.args[1]["dt"], 0.2)
        self.assertFalse(drive_call.args[1]["immediate_stop"])
        self.assertEqual(gateway._simulator.call.call_args_list[1].args[0], "set_arm")
        self.assertEqual(gateway._simulator.call.call_args_list[1].args[1]["extension"], 0.4)

    def test_servo_control_rejects_out_of_order_and_stale_cycles_per_source(self) -> None:
        gateway = _servo_gateway()
        mobile = _servo_context("component.mobile")
        self.assertEqual(gateway._servo_control(_servo_request(5, BASE_TARGET), mobile)["status"], "applied")

        for request in (
            _servo_request(5, BASE_TARGET),
            _servo_request(4, BASE_TARGET),
            _servo_request(9, BASE_TARGET, generation=9),
        ):
            with self.subTest(cycle=request["cycle"]):
                response = gateway._servo_control(request, mobile)
                self.assertEqual(response["status"], "rejected")
                self.assertEqual(response["cycle"], request["cycle"])
                self.assertIn("detail", response)
        self.assertEqual(gateway._simulator.call.call_count, 1)

        # Another bound source has its own ordering; a newer generation restarts it.
        self.assertEqual(
            gateway._servo_control(_servo_request(1, ARM_TARGET), _servo_context("component.manipulation"))["status"],
            "applied",
        )
        self.assertEqual(
            gateway._servo_control(_servo_request(1, BASE_TARGET, generation=11), mobile)["status"], "applied")

    def test_servo_control_rejects_non_command_intent_expired_and_unknown_targets(self) -> None:
        gateway = _servo_gateway()
        keep = _servo_request(1, BASE_TARGET)
        keep["cycle"]["intent"] = "keep"
        cases = (
            (keep, _servo_context("component.mobile")),
            (_servo_request(2, {"names": ["unknown"], "target_position": [1.0]}), _servo_context("component.mobile")),
            (_servo_request(3, BASE_TARGET), _servo_context("component.mobile", valid_until_sync_ns=1)),
        )
        for request, context in cases:
            with self.subTest(cycle=request["cycle"]):
                self.assertEqual(gateway._servo_control(request, context)["status"], "rejected")
        gateway._simulator.call.assert_not_called()
        with self.assertRaises(ValueError):
            gateway._servo_control({"cycle": keep["cycle"]}, _servo_context("component.mobile"))
        with self.assertRaises(ValueError):
            gateway._servo_control(_servo_request(4, BASE_TARGET), SimpleNamespace(valid_until_sync_ns=2))

    def test_servo_control_emergency_stops_immediately_and_reports_device_errors(self) -> None:
        gateway = _servo_gateway()
        response = gateway._servo_control(
            _servo_request(1, ARM_TARGET, emergency=True), _servo_context("component.mobile"))
        self.assertEqual(response["status"], "applied")
        self.assertFalse(response["state"]["servo_on"])
        stop_call = gateway._simulator.call.call_args
        self.assertEqual(stop_call.args[0], "apply_drive")
        self.assertTrue(stop_call.args[1]["immediate_stop"])
        self.assertEqual((stop_call.args[1]["vx"], stop_call.args[1]["vy"], stop_call.args[1]["wz"]), (0.0, 0.0, 0.0))

        gateway._simulator.call.side_effect = RuntimeError("simulator request failed")
        failed = gateway._servo_control(_servo_request(2, BASE_TARGET), _servo_context("component.mobile"))
        self.assertEqual(failed["status"], "device_error")
        self.assertIn("simulator request failed", failed["detail"])

    def test_gripper_readback_uses_validated_extracted_payload(self) -> None:
        component = _gripper()
        component.transact_bound_control = Mock(
            return_value={"ok": True, "result": {"value": True}}
        )

        self.assertEqual(component._get_gripper_state({}), {"value": True})

    def test_gripper_readback_rejects_failed_or_malformed_control(self) -> None:
        invalid_responses = (
            {"ok": False, "error_message": "provider unavailable"},
            {"ok": True, "result": {}},
            {"ok": True, "result": {"value": "true"}},
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                component = _gripper()
                component.transact_bound_control = Mock(return_value=response)

                with self.assertRaises(RuntimeError):
                    component._get_gripper_state({})

    def test_gripper_open_and_close_reject_failed_or_malformed_control(self) -> None:
        for method_name in ("_open", "_close"):
            for response in (
                {"ok": False, "error_message": "provider unavailable"},
                {"ok": True, "result": {"value": "false"}},
                {"ok": True, "result": {}},
            ):
                with self.subTest(method=method_name, response=response):
                    component = _gripper()
                    component.transact_bound_control = Mock(return_value=response)

                    with self.assertRaises(RuntimeError):
                        getattr(component, method_name)({})

    def test_gripper_open_and_close_report_domain_negative_acknowledgement(self) -> None:
        for method_name in ("_open", "_close"):
            with self.subTest(method=method_name):
                component = _gripper()
                component.transact_bound_control = Mock(
                    return_value={"ok": True, "result": {"value": False}}
                )

                result = getattr(component, method_name)({})

                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "failed")

    def test_perception_poll_uses_typed_radar_cycle_and_queries_local_state(self) -> None:
        component = PerceptionControllerSubsystem.__new__(PerceptionControllerSubsystem)
        component._binding = "radar_source"
        component.now_ns = Mock(return_value=100)
        component._poll_period_ms = 100.0
        component._poll_deadline_ms = 250.0
        component._generation = 10
        component._sequence = 0
        component._poll_cycle_lock = threading.Lock()
        component._poller_stop = threading.Event()
        component._runtime_binding_ready = False
        component._state_lock = threading.RLock()
        component._tracker = RadarObservationTracker(track_id_prefix="adapter")
        component._logger = Mock()
        component.resolve_runtime_binding = Mock(return_value=object())
        component.transact_bound_control = Mock(
            return_value={
                "ok": True,
                "result": {
                    "cycle": {"generation": 10, "sequence": 1, "intent": "sample", "state_revision": 0},
                    "session_id": "radar-session-1",
                    "status": "sample_available",
                    "sector": {
                        "session_id": "radar-session-1",
                        "sample_id": 1,
                        "revolution_id": 1,
                        "azimuth_start_rad": 0.0,
                        "azimuth_span_rad": 1.0,
                        "scan_started_at_sync_ns": 90,
                        "scan_ended_at_sync_ns": 100,
                        "frame_id": "cleaner_radar",
                        "revolution_complete": False,
                        "returns": [
                            {
                                "range_m": 0.2,
                                "azimuth_rad": 0.5,
                                "radial_velocity_mps": 0.0,
                                "signal_strength": 0.8,
                                "observed_at_sync_ns": 100,
                            }
                        ],
                    },
                },
            }
        )

        component._poll_once()

        component.transact_bound_control.assert_called_once_with(
            binding="radar_source",
            topic="radar_cycle",
            type="bytes",
            value={
                "cycle": {"generation": 10, "sequence": 1, "intent": "sample", "state_revision": 0},
                "after_sample_id": 0,
            },
            period_ms=100.0,
            deadline_ms=250.0,
            request_type="msg.sensor.radar.v1/RadarCycleRequest",
            response_type="msg.sensor.radar.v1/RadarCycleResponse",
            timing={
                "generated_at_sync_ns": 100,
                "valid_for_ns": 250_000_000,
                "late_policy": "reject",
            },
        )
        result = component._query_local(mode="nearest", max_range_m=0.7, fov_deg=360.0)
        self.assertEqual(len(result["detections"]), 1)
        self.assertEqual(result["source_frame_id"], "cleaner_radar")
        self.assertNotIn("state", result)

        component.transact_bound_control.return_value = {
            "ok": True,
            "result": {
                "cycle": {"generation": 10, "sequence": 2, "intent": "sample", "state_revision": 0},
                "session_id": "radar-session-2",
                "status": "resync_required",
            },
        }
        component._poll_once()
        component.transact_bound_control.return_value = {
            "ok": True,
            "result": {
                "cycle": {"generation": 10, "sequence": 3, "intent": "sample", "state_revision": 0},
                "session_id": "radar-session-2",
                "status": "no_new_sample",
            },
        }
        component._poll_once()

        calls = component.transact_bound_control.call_args_list
        self.assertEqual(
            calls[1].kwargs["value"],
            {
                "cycle": {"generation": 10, "sequence": 2, "intent": "sample", "state_revision": 0},
                "after_sample_id": 1,
                "expected_session_id": "radar-session-1",
            },
        )
        self.assertEqual(
            calls[2].kwargs["value"],
            {
                "cycle": {"generation": 10, "sequence": 3, "intent": "sample", "state_revision": 0},
                "after_sample_id": 0,
                "expected_session_id": "radar-session-2",
            },
        )

if __name__ == "__main__":
    unittest.main()
