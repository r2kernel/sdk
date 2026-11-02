from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import r2kernel.contract as contract_package
from r2kernel.contract import (
    ContractSpecError,
    expand_declared_api_ids,
    load_component_spec,
    load_api_contract_spec,
    validate_declared_contracts,
    validate_runtime_contract_types,
)
from r2kernel.interface.loader import (
    InterfaceSpecError,
    _load_default_namespace_versions,
    _parse_interface_spec,
)
from r2kernel.spec_paths import _PACKAGE_SPEC_ROOT, package_spec_sources, spec_root_candidates


class _FakeRuntime:
    def __init__(self) -> None:
        self._procedures = {
            "get_digital_input": {
                "request_type": "msg.core.common.primitive.v1/UInt32Value",
                "response_type": "msg.core.common.primitive.v1/BoolValue",
                "timing_support": "none",
            },
            "set_digital_output": {
                "request_type": "msg.core.common.primitive.v1/IndexedBoolValue",
                "response_type": "msg.core.common.primitive.v1/BoolValue",
                "timing_support": "none",
            },
            "get_digital_output": {
                "request_type": "msg.core.common.primitive.v1/UInt32Value",
                "response_type": "msg.core.common.primitive.v1/BoolValue",
                "timing_support": "none",
            },
        }

    def get_publish_info(self):
        return []

    def get_subscribe_info(self):
        return []

    def get_control_info(self):
        return ["digital_output_control"]

    def get_procedure_info(self):
        return list(self._procedures.keys())

    def get_operation_info(self):
        return []

    def get_control_contract_info(self):
        return [
            (
                "digital_output_control",
                {
                    "request_type": "msg.io.digital.timed_output.v1/TimedOutputControlRequest",
                    "response_type": "msg.io.digital.timed_output.v1/TimedOutputControlResponse",
                    "timing_support": "required",
                },
            )
        ]

    def get_procedure_contract_info(self):
        return list(self._procedures.items())

    def get_operation_contract_info(self):
        return []


class _LegacyDigitalOutputRuntime(_FakeRuntime):
    def get_control_info(self):
        return []

    def get_control_contract_info(self):
        return []


class _Owner:
    declares_apis = ("api.io.digital.output.v1",)


class _TimedDigitalOutputOwner:
    declares_apis = ("api.io.digital.timed_output.v1",)


class _LegacyCapabilityOwner:
    declares_capabilities = ("capability.r2kernel.mobility.body_motion.v1",)


class _CapabilityApiOwner:
    declares_apis = ("capability.r2kernel.navigation.goal.v1",)


class _GripperProviderOwner:
    declares_components = ("component.gripper.v1",)


class _APIContractLocalRuntime:
    def get_control_info(self):
        return ["servo_control"]

    def get_procedure_info(self):
        return ["open"]

    def get_operation_info(self):
        return []

    def get_control_contract_info(self):
        return [
            (
                "servo_control",
                {
                    "request_type": "msg.actuator.servo.v1/ServoControlRequest",
                    "response_type": "msg.actuator.servo.v1/ServoControlResponse",
                    "timing_support": "required",
                },
            )
        ]

    def get_procedure_contract_info(self):
        return [
            (
                "open",
                {
                    "request_type": "msg.core.common.primitive.v1/Empty",
                    "response_type": "msg.core.common.result.v1/Result",
                    "timing_support": "none",
                },
            )
        ]

    def get_operation_contract_info(self):
        return []


class _GripperProviderRuntime:
    def get_publish_info(self):
        return []

    def get_subscribe_info(self):
        return []

    def get_control_info(self):
        return []

    def get_procedure_info(self):
        return ["open", "close", "get_gripper_state"]

    def get_operation_info(self):
        return []

    def get_control_contract_info(self):
        return []

    def get_procedure_contract_info(self):
        result = "msg.core.common.result.v1/Result"
        empty = "msg.core.common.primitive.v1/Empty"
        return [
            ("open", {"request_type": empty, "response_type": result, "timing_support": "none"}),
            ("close", {"request_type": empty, "response_type": result, "timing_support": "none"}),
            (
                "get_gripper_state",
                {
                    "request_type": empty,
                    "response_type": "msg.core.common.primitive.v1/BoolValue",
                    "timing_support": "none",
                },
            ),
        ]

    def get_operation_contract_info(self):
        return []


class ContractTests(unittest.TestCase):
    def test_loads_canonical_api_contract(self) -> None:
        input_spec = load_api_contract_spec("api.io.digital.input.v1")
        self.assertEqual(input_spec.id, "api.io.digital.input.v1")
        self.assertEqual([procedure.name for procedure in input_spec.procedures], ["get_digital_input"])

        output_spec = load_api_contract_spec("api.io.digital.output.v1")
        self.assertEqual(output_spec.id, "api.io.digital.output.v1")
        self.assertEqual([procedure.name for procedure in output_spec.procedures], ["set_digital_output", "get_digital_output"])

    def test_api_contract_references_public_message_id(self) -> None:
        spec = load_api_contract_spec("api.motion.articulated.trajectory.v1")
        self.assertEqual(spec.id, "api.motion.articulated.trajectory.v1")
        self.assertEqual(spec.operations[0].request, "msg.motion.articulated.v1/MoveJRequest")
        self.assertEqual(spec.messages, {})

    def test_loads_manipulator_cartesian_api_contract(self) -> None:
        spec = load_api_contract_spec("api.motion.manipulator.cartesian.v1")
        self.assertEqual(spec.id, "api.motion.manipulator.cartesian.v1")
        self.assertEqual(spec.operations[0].request, "msg.motion.manipulator.v1/MoveLRequest")
        self.assertEqual(spec.messages, {})

    def test_loads_canonical_component(self) -> None:
        gripper = load_component_spec("component.gripper.v1")
        self.assertEqual(gripper.id, "component.gripper.v1")
        self.assertEqual(gripper.conformance_apis, ("api.end_effector.gripper.v1",))

    def test_loads_mobile_base_controller_component(self) -> None:
        spec = load_component_spec("component.mobile_base.v1")
        self.assertEqual(spec.id, "component.mobile_base.v1")
        self.assertEqual(
            spec.conformance_apis,
            (
                "api.locomotion.velocity.v1",
                "api.locomotion.velocity_reference.v1",
                "api.locomotion.mobile_base.state.v1",
            ),
        )

    def test_capability_contracts_are_removed(self) -> None:
        for name in ("CapabilitySpec", "load_capability_spec", "load_capability_specs"):
            self.assertFalse(hasattr(contract_package, name), name)
        with self.assertRaisesRegex(ContractSpecError, "capability contracts were removed"):
            load_api_contract_spec("capability.r2kernel.mobility.body_motion.v1")
        with self.assertRaisesRegex(ContractSpecError, "capability contracts were removed"):
            load_component_spec("capability.r2kernel.mobility.body_motion.v1")
        with self.assertRaisesRegex(ContractSpecError, "capability contracts were removed"):
            validate_declared_contracts(_CapabilityApiOwner(), _FakeRuntime())
        with self.assertRaises(Exception):
            load_api_contract_spec("api.capability.mobility.body_motion.v1")

    def test_rejects_declared_capabilities_owner(self) -> None:
        with self.assertRaisesRegex(ContractSpecError, "declares_capabilities is not supported"):
            validate_declared_contracts(_LegacyCapabilityOwner(), _FakeRuntime())

    def test_loads_mobile_base_controller_api_contracts(self) -> None:
        velocity = load_api_contract_spec("api.locomotion.velocity.v1")
        self.assertEqual(velocity.controls[0].topic, "velocity_command")
        self.assertEqual(velocity.controls[0].request, "msg.locomotion.velocity.v1/VelocityCommand")
        self.assertEqual(velocity.controls[0].response, "msg.locomotion.velocity.v1/CommandAck")
        self.assertEqual([procedure.name for procedure in velocity.procedures], ["set_velocity_limits", "get_velocity_limits", "stop"])

        state = load_api_contract_spec("api.locomotion.mobile_base.state.v1")
        self.assertEqual([event.topic for event in state.events], ["mobile_base_state", "fault_state"])
        self.assertIn("enable_drive", [procedure.name for procedure in state.procedures])
        self.assertIn("clear_fault", [procedure.name for procedure in state.procedures])

        odometry = load_api_contract_spec("api.locomotion.odometry.v1")
        self.assertEqual(odometry.events[0].topic, "odometry")

        diff_drive = load_api_contract_spec("api.locomotion.diff_drive.v1")
        self.assertEqual([event.topic for event in diff_drive.events], ["wheel_command", "wheel_state"])

    def test_expands_component_apis(self) -> None:
        api_contracts = expand_declared_api_ids(declared_components=("component.gripper.v1",))
        self.assertEqual(api_contracts, ("api.end_effector.gripper.v1",))

    def test_expands_mobile_base_component_apis(self) -> None:
        api_contracts = expand_declared_api_ids(declared_components=("component.mobile_base.v1",))
        self.assertEqual(
            api_contracts,
            (
                "api.locomotion.velocity.v1",
                "api.locomotion.velocity_reference.v1",
                "api.locomotion.mobile_base.state.v1",
            ),
        )

    def test_validates_digital_gateway_contract(self) -> None:
        report = validate_declared_contracts(_Owner(), _FakeRuntime())
        self.assertTrue(report.ok)
        self.assertEqual([component.id for component in report.components], [])
        self.assertEqual([api_contract.id for api_contract in report.api_contracts], ["api.io.digital.output.v1"])

    def test_legacy_digital_output_contract_does_not_require_timed_control(self) -> None:
        contract = load_api_contract_spec("api.io.digital.output.v1")
        self.assertEqual(contract.controls, ())
        self.assertEqual(
            [procedure.name for procedure in contract.procedures],
            ["set_digital_output", "get_digital_output"],
        )
        report = validate_declared_contracts(_Owner(), _LegacyDigitalOutputRuntime())
        self.assertTrue(report.ok, report.format_errors())

    def test_timed_digital_output_contract_requires_timed_control(self) -> None:
        contract = load_api_contract_spec("api.io.digital.timed_output.v1")
        self.assertEqual([control.topic for control in contract.controls], ["digital_output_control"])
        self.assertEqual(contract.controls[0].timing_support, "required")
        report = validate_declared_contracts(_TimedDigitalOutputOwner(), _FakeRuntime())
        self.assertTrue(report.ok, report.format_errors())

    def test_validates_gripper_provider_contracts(self) -> None:
        report = validate_declared_contracts(_GripperProviderOwner(), _GripperProviderRuntime())
        self.assertTrue(report.ok, report.format_errors())
        self.assertEqual([component.id for component in report.components], ["component.gripper.v1"])
        gripper = load_api_contract_spec("api.end_effector.gripper.v1")
        self.assertEqual(gripper.procedures[0].response, "msg.core.common.result.v1/Result")

    def test_runtime_contract_types_accept_external_message_references(self) -> None:
        # The native type check resolves public messages only through a configured spec root; set the
        # SDK's packaged root here instead of depending on an earlier test having configured one.
        with patch.dict(os.environ, {"R2KERNEL_SPEC_ROOT": str(_PACKAGE_SPEC_ROOT)}):
            report = validate_runtime_contract_types(_APIContractLocalRuntime(), policy="strict")
        self.assertTrue(report.ok, report.format_errors())

    def test_loads_api_contract_from_external_spec_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "apis" / "api" / "io" / "digital" / "input" / "v1.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                """
id: api.io.digital.input.v1
name: api.io.digital.input.v1
version: 1
procedures:
  - name: ping
    request: msg.core.common.primitive.v1/Empty
    response: msg.core.common.primitive.v1/BoolValue
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "R2KERNEL_CONTRACT_SPEC_ROOTS": "",
                    "R2KERNEL_SPEC_PATH": "",
                    "R2KERNEL_SPEC_ROOT": str(root),
                },
            ):
                spec = load_api_contract_spec("api.io.digital.input.v1")
            self.assertEqual(spec.id, "api.io.digital.input.v1")
            self.assertEqual([procedure.name for procedure in spec.procedures], ["ping"])

    def test_loads_component_from_external_spec_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "components" / "component" / "user" / "digital_io" / "v1.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                """
id: component.user.digital_io.v1
name: component.user.digital_io.v1
version: 1
conformance_apis:
  - api.io.digital.input.v1
  - api.io.digital.output.v1
spatial:
  participation: none
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "R2KERNEL_CONTRACT_SPEC_ROOTS": "",
                    "R2KERNEL_SPEC_PATH": "",
                    "R2KERNEL_SPEC_ROOT": str(root),
                },
            ):
                spec = load_component_spec("component.user.digital_io.v1")
            self.assertEqual(spec.id, "component.user.digital_io.v1")
            self.assertEqual(spec.conformance_apis, ("api.io.digital.input.v1", "api.io.digital.output.v1"))

    def test_spec_path_precedes_spec_root(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir, tempfile.TemporaryDirectory() as root_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            root = Path(root_dir)
            env = {
                "R2KERNEL_SPEC_PATH": os.pathsep.join([str(first), str(second)]),
                "R2KERNEL_SPEC_ROOT": str(root),
            }
            with patch.dict(os.environ, env):
                candidates = spec_root_candidates()
            self.assertLess(candidates.index(first), candidates.index(second))
            self.assertLess(candidates.index(second), candidates.index(root))

    def test_discovers_composition_spec_root_from_nested_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            composition = Path(tmpdir) / "tutorials"
            shared_spec = composition / "spec"
            package_dir = composition / "subsystems" / "component.mobile"
            shared_spec.mkdir(parents=True)
            package_dir.mkdir(parents=True)

            with patch("r2kernel.spec_paths.Path.cwd", return_value=package_dir):
                candidates = spec_root_candidates()

            self.assertIn(shared_spec, candidates)

    def test_loads_component_and_api_contract_from_spec_path_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as component_dir, tempfile.TemporaryDirectory() as api_contract_dir:
            component_root = Path(component_dir)
            api_contract_root = Path(api_contract_dir)
            component_path = component_root / "components" / "component" / "user" / "demo" / "v1.yaml"
            component_path.parent.mkdir(parents=True, exist_ok=True)
            component_path.write_text(
                """
id: component.user.demo.v1
name: component.user.demo.v1
version: 1
conformance_apis:
  - api.user.demo.v1
spatial:
  participation: none
""".strip()
                + "\n",
                encoding="utf-8",
            )
            api_contract_path = api_contract_root / "apis" / "api" / "user" / "demo" / "v1.yaml"
            api_contract_path.parent.mkdir(parents=True, exist_ok=True)
            api_contract_path.write_text(
                """
id: api.user.demo.v1
name: api.user.demo.v1
version: 1
procedures:
  - name: custom_ping
    request: msg.core.common.primitive.v1/Empty
    response: msg.core.common.primitive.v1/BoolValue
""".strip()
                + "\n",
                encoding="utf-8",
            )
            env = {
                "R2KERNEL_SPEC_PATH": os.pathsep.join([str(component_root), str(api_contract_root)]),
                "R2KERNEL_SPEC_ROOT": "",
            }
            with patch.dict(os.environ, env):
                component = load_component_spec("component.user.demo.v1")
                api_contract = load_api_contract_spec("api.user.demo.v1")
            self.assertEqual(component.conformance_apis, ("api.user.demo.v1",))
            self.assertEqual([procedure.name for procedure in api_contract.procedures], ["custom_ping"])



class MessageSetHeaderTests(unittest.TestCase):
    _MESSAGES = {"Sample": {"fields": [{"name": "value", "type": "uint32"}]}}

    def test_accepts_version_id_kind_header(self) -> None:
        spec = _parse_interface_spec(
            {"version": 1, "id": "msg.vendor.sample.v1", "kind": "message_set", "messages": self._MESSAGES},
            source="inline",
        )
        self.assertEqual(spec.kind, "message_set")
        self.assertEqual(list(spec.messages), ["Sample"])

    def test_rejects_legacy_or_incomplete_message_set_headers(self) -> None:
        cases = (
            ({"schema_version": 1, "id": "msg.vendor.sample.v1", "kind": "message_set", "version": 1},
             "Legacy header key\\(s\\) schema_version"),
            ({"namespace": "msg.vendor.sample.v1", "id": "msg.vendor.sample.v1", "kind": "message_set",
              "version": 1}, "Legacy header key\\(s\\) namespace"),
            ({"version": 1, "id": "msg.vendor.sample.v1"}, "must declare kind: message_set"),
            ({"version": 1, "id": "msg.vendor.sample.v1", "kind": "api_contract"}, "must declare kind: message_set"),
            ({"id": "msg.vendor.sample.v1", "kind": "message_set"}, "must declare version: 1"),
            ({"version": 2, "id": "msg.vendor.sample.v1", "kind": "message_set"}, "must declare version: 1"),
        )
        for header, message in cases:
            with self.subTest(header=header):
                with self.assertRaisesRegex(InterfaceSpecError, message):
                    _parse_interface_spec({**header, "messages": self._MESSAGES}, source="inline")

    def test_namespace_defaults_header(self) -> None:
        cases = (
            ("version: 1\nkind: namespace_defaults\nnamespaces: {msg.vendor.sample: v2}\n", None),
            ("namespaces: {msg.vendor.sample: v2}\n", None),
            ("version: 1\nkind: message_set\nnamespaces: {msg.vendor.sample: v2}\n",
             "defaults kind must be namespace_defaults"),
            ("version: 2\nkind: namespace_defaults\nnamespaces: {msg.vendor.sample: v2}\n",
             "defaults version must be 1"),
        )
        for document, message in cases:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                (root / "defaults.yaml").write_text(document, encoding="utf-8")
                with package_spec_sources([root]):
                    if message is None:
                        self.assertEqual(_load_default_namespace_versions()["msg.vendor.sample"], "v2")
                    else:
                        with self.assertRaisesRegex(InterfaceSpecError, message):
                            _load_default_namespace_versions()

    def test_packaged_defaults_use_namespace_defaults_header(self) -> None:
        defaults = _load_default_namespace_versions()
        self.assertEqual(defaults["msg.core.common.result"], "v1")
        self.assertNotIn("msg.capability.common", defaults)


if __name__ == "__main__":
    unittest.main()
