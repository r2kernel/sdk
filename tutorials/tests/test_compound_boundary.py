from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
STANDARD_SPEC_ROOT = ROOT / "spec"
DEPLOYMENTS = TUTORIALS / "deployments"
SDK_PYTHON = ROOT / "sdk" / "python"
if str(SDK_PYTHON) not in sys.path:
    sys.path.insert(0, str(SDK_PYTHON))

from r2kernel.manifest.loader import load_deployment_manifest  # noqa: E402

APP_NAMES = {
    "teleop": "app.cleaner.teleop",
    "radar": "app.cleaner.radar",
    "learned-radar": "app.cleaner.learned_radar",
}


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def standard_contract_path(contract: str) -> Path:
    category = "components" if contract.startswith("component.") else "apis"
    parts = contract.split(".")
    return STANDARD_SPEC_ROOT / category / Path(*parts[:-1]) / f"{parts[-1]}.yaml"


class CompoundBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec_environment = patch.dict("os.environ", {"R2KERNEL_CONTRACT_SPEC_ROOTS": str(TUTORIALS / "spec")})
        cls.spec_environment.start()
        cls.addClassCleanup(cls.spec_environment.stop)
        cls.deployments = {
            name: load_yaml(DEPLOYMENTS / f"{name}.deployment.yaml")
            for name in APP_NAMES
        }

    def test_tutorials_share_one_platform_and_differ_only_by_app(self) -> None:
        reference_name = "teleop"
        reference = self.deployments[reference_name]
        reference_instances = {
            name: value
            for name, value in reference["instances"].items()
            if name != APP_NAMES[reference_name]
        }
        reference_bindings = {
            source: value
            for source, value in reference["bindings"].items()
            if not source.startswith(f"{APP_NAMES[reference_name]}.")
        }

        for name, deployment in self.deployments.items():
            with self.subTest(deployment=name):
                non_app_instances = {
                    key: value
                    for key, value in deployment["instances"].items()
                    if key != APP_NAMES[name]
                }
                non_app_bindings = {
                    source: value
                    for source, value in deployment["bindings"].items()
                    if not source.startswith(f"{APP_NAMES[name]}.")
                }
                self.assertEqual(reference_instances, non_app_instances)
                self.assertEqual(reference_bindings, non_app_bindings)

    def test_apps_use_only_compound_exported_apis(self) -> None:
        for name, deployment in self.deployments.items():
            app = APP_NAMES[name]
            app_bindings = {
                source: value["to"]
                for source, value in deployment["bindings"].items()
                if source.startswith(f"{app}.")
            }
            with self.subTest(deployment=name):
                self.assertTrue(app_bindings)
                self.assertTrue(
                    all(target.startswith("compound.cleaner.") for target in app_bindings.values())
                )

    def test_compound_never_binds_to_a_gateway(self) -> None:
        for name, deployment in self.deployments.items():
            compound_targets = [
                value["to"]
                for source, value in deployment["bindings"].items()
                if source.startswith("compound.cleaner.")
            ]
            with self.subTest(deployment=name):
                self.assertTrue(compound_targets)
                self.assertTrue(
                    all(target.startswith("component.") for target in compound_targets)
                )
                self.assertFalse(
                    any(target.startswith("gateway.") for target in compound_targets)
                )

    def test_compound_requires_components_and_declaratively_exports_apis(self) -> None:
        compound = load_yaml(TUTORIALS / "subsystems" / "compound.cleaner" / "subsystem.yaml")
        self.assertNotIn("provides", compound)
        self.assertTrue(compound["requires"])
        self.assertTrue(
            all(item["contract"].startswith("component.") for item in compound["requires"].values())
        )
        exported = compound["exports"]
        self.assertTrue(exported)
        for declaration in exported.values():
            self.assertNotIn("capability", declaration)
            self.assertTrue(declaration["contract"].startswith("api."))
            self.assertNotIn(".capability.", declaration["contract"])
            self.assertIn(declaration["from"]["requirement"], compound["requires"])

        compound_code = (TUTORIALS / "subsystems" / "compound.cleaner" / "compound.py").read_text()
        for forbidden in (
            "register_control_endpoint",
            "register_procedure",
            "register_operation",
            "transact_bound_control",
            "call_bound_procedure",
            "start_bound_operation",
        ):
            self.assertNotIn(forbidden, compound_code)

    def test_components_provide_component_contracts_not_tutorial_api_aliases(self) -> None:
        component_names = (
            "component.mobile",
            "component.localizer",
            "component.navigation",
            "component.manipulation",
            "component.gripper",
            "component.perception",
            "component.incinerator",
        )
        for name in component_names:
            manifest = load_yaml(TUTORIALS / "subsystems" / name / "subsystem.yaml")
            provided = list(manifest.get("provides", {}).values())
            with self.subTest(component=name):
                component_contracts = [item for item in provided if item["contract"].startswith("component.")]
                self.assertEqual(len(component_contracts), 1)
                if name == "component.localizer":
                    self.assertEqual(
                        [item["contract"] for item in provided if item not in component_contracts],
                        ["api.positioning.absolute_pose.v1"],
                    )
                else:
                    self.assertEqual(len(provided), 1)

    def test_only_private_digital_io_contract_is_package_local(self) -> None:
        relative = Path("apis/api/tutorial/gateway/device_gateway/digital_io/v1.yaml")
        self.assertEqual(
            [path.relative_to(TUTORIALS / "spec") for path in (TUTORIALS / "spec").rglob("*.yaml")],
            [relative],
        )
        self.assertFalse((STANDARD_SPEC_ROOT / relative).exists())
        self.assertFalse((SDK_PYTHON / "r2kernel" / "specs" / relative).exists())

    def test_apps_use_only_standard_dictionary_exports(self) -> None:
        robot = load_yaml(TUTORIALS / "robot" / "r2kernel.robot.yaml")
        self.assertNotIn("capabilities", robot)

        for app_name in APP_NAMES.values():
            manifest = load_yaml(TUTORIALS / "subsystems" / app_name / "subsystem.yaml")
            self.assertNotIn("capabilities", manifest["application"]["requires_robot"])
            for declaration in manifest["requires"].values():
                self.assertNotIn("capability", declaration)
                self.assertTrue(declaration["contract"].startswith("api."))
                self.assertNotIn(".capability.", declaration["contract"])
                self.assertTrue(
                    standard_contract_path(declaration["contract"]).is_file(),
                    declaration["contract"],
                )

    def test_body_motion_app_api_uses_procedure_and_component_export(self) -> None:
        contract = "api.locomotion.velocity_reference.v1"
        expected_deployments = (
            DEPLOYMENTS / "teleop.deployment.yaml",
            DEPLOYMENTS / "learned-radar.deployment.yaml",
            DEPLOYMENTS / "dev" / "teleop.deployment.yaml",
            DEPLOYMENTS / "dev" / "learned-radar.deployment.yaml",
        )
        api = load_yaml(standard_contract_path(contract))
        component = load_yaml(
            STANDARD_SPEC_ROOT / "components" / "component" / "mobile_base" / "odometry" / "v1.yaml"
        )
        self.assertEqual(api["id"], contract)
        self.assertFalse(api.get("controls"))
        procedures = {item["name"]: item for item in api["procedures"]}
        self.assertEqual(
            procedures["set_velocity_reference"]["request"],
            "msg.locomotion.velocity.v1/VelocityCommand",
        )
        self.assertEqual(
            procedures["set_velocity_reference"]["response"],
            "msg.locomotion.velocity.v1/CommandAck",
        )
        self.assertEqual(procedures["stop"]["request"], "msg.locomotion.velocity.v1/StopRequest")
        self.assertIn(contract, component["conformance_apis"])

        for deployment_path in expected_deployments:
            with self.subTest(deployment=deployment_path.name, path=deployment_path.parent.name):
                deployment = load_deployment_manifest(deployment_path)
                for app_name in ("app.cleaner.teleop", "app.cleaner.learned_radar"):
                    if app_name not in deployment.subsystems:
                        continue
                    app = deployment.subsystems[app_name]
                    self.assertEqual(app.requires["body_motion"].contract, contract)
                    resolved = deployment.resolve(app_name, "body_motion")
                    self.assertEqual(resolved.contract, contract)
                    self.assertEqual(resolved.target_subsystem, "component.mobile")
                    self.assertEqual(resolved.provider_interface, "mobile_base")

    def test_app_drive_uses_procedure_api_without_control_facade(self) -> None:
        app_code = (TUTORIALS / "lib" / "app_base.py").read_text()
        self.assertNotIn("def control(", app_code)
        self.assertNotIn("transact_bound_control", app_code)
        self.assertIn('"set_velocity_reference"', app_code)
        self.assertIn('request_type=VELOCITY_COMMAND', app_code)
        self.assertNotIn("period_ms=", app_code)
        self.assertNotIn("timing={", app_code)

    def test_incinerator_and_exported_gripper_controls_have_installed_typed_specs(self) -> None:
        component = load_yaml(TUTORIALS / "subsystems" / "component.incinerator" / "subsystem.yaml")
        gateway = load_yaml(TUTORIALS / "subsystems" / "gateway.device_gateway" / "subsystem.yaml")
        gripper = load_yaml(TUTORIALS / "subsystems" / "component.gripper" / "subsystem.yaml")
        self.assertEqual(
            component["provides"]["incinerator"]["contract"],
            "component.trigger_actuator.v1",
        )
        self.assertEqual(
            component["requires"]["timed_digital_output"]["contract"],
            "api.io.digital.timed_output.v1",
        )
        self.assertEqual(
            gateway["provides"]["digital_io"]["contract"],
            "api.tutorial.gateway.device_gateway.digital_io.v1",
        )
        self.assertEqual(gateway["provides"]["digital_io"]["visibility"], "public")
        self.assertNotIn("endpoints", gateway["provides"]["digital_io"])
        self.assertEqual(
            gateway["provides"]["timed_digital_output"]["contract"],
            "api.io.digital.timed_output.v1",
        )
        self.assertEqual(
            gripper["requires"]["digital_io"]["contract"],
            "api.tutorial.gateway.device_gateway.digital_io.v1",
        )
        self.assertEqual(gripper["requires"]["digital_io"]["visibility"], "public")
        contract = gateway["provides"]["digital_io"]["contract"]
        relative = Path("apis/api/tutorial/gateway/device_gateway/digital_io/v1.yaml")
        contract_path = TUTORIALS / "spec" / relative
        api = load_yaml(contract_path)
        self.assertEqual(api["id"], contract)
        self.assertEqual(api["conformance"]["level"], "public")
        self.assertFalse(api.get("procedures"))
        controls = {item["topic"]: item for item in api["controls"]}
        self.assertEqual(set(controls), {"digital_output_set", "digital_output_readback"})
        self.assertEqual(controls["digital_output_set"]["request"], "msg.core.common.primitive.v1/IndexedBoolValue")
        self.assertEqual(controls["digital_output_readback"]["request"], "msg.core.common.primitive.v1/UInt32Value")
        for control in controls.values():
            self.assertEqual(control["response"], "msg.core.common.primitive.v1/BoolValue")
            self.assertEqual(control["timing_support"], "required")
        for package_name in ("component.gripper", "gateway.device_gateway"):
            package_root = TUTORIALS / "subsystems" / package_name
            packaged_path = package_root / "spec" / relative
            self.assertEqual(contract_path.read_bytes(), packaged_path.read_bytes())
            package = load_yaml(package_root / "package.yaml")
            self.assertEqual(package["contracts"]["spec_roots"], ["spec"])
        self.assertTrue(standard_contract_path("component.trigger_actuator.v1").is_file())
        self.assertTrue(standard_contract_path("api.actuation.trigger.v1").is_file())
        self.assertTrue(standard_contract_path("api.io.digital.timed_output.v1").is_file())

        for deployment in self.deployments.values():
            self.assertEqual(
                deployment["bindings"]["component.gripper.digital_io"]["to"],
                "gateway.device_gateway.digital_io",
            )
            self.assertEqual(
                deployment["bindings"]["component.incinerator.timed_digital_output"]["to"],
                "gateway.device_gateway.timed_digital_output",
            )


if __name__ == "__main__":
    unittest.main()
