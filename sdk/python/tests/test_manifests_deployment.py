"""Deployment, robot platform and application manifest composition tests.

Accept/reject decisions live in the cases shared with the C++ SDK
(sdk/testdata/parity/deployment-validation.yaml,
test_sdk_parity_cases.py); these tests check the loaded models of some of them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from r2kernel.manifest import (
    load_application_manifest,
    load_deployment_manifest,
    load_robot_platform_manifest,
    subsystem_manifest_status_payload,
    validate_required_bindings_for_subsystem,
)
from sdk_parity_support import deployment_case


class ManifestTests(unittest.TestCase):
    def test_resolves_deployment_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = load_deployment_manifest(deployment_case("deployment_binding", Path(tmpdir)))
            resolved = deployment.resolve("client", "service")
            self.assertEqual(resolved.target, "server")
            client = deployment.subsystems["client"]
            self.assertEqual(validate_required_bindings_for_subsystem(client, deployment)[0].contract, "tutorial.transport.procedure.v1")
            status = subsystem_manifest_status_payload(client, deployment)
            self.assertEqual(status["interfaces"]["requires"]["service"]["contract"], "tutorial.transport.procedure.v1")
            self.assertEqual(status["bindings"][0]["source"], "client.service")
            self.assertEqual(status["bindings"][0]["target"], "server.service")

    def test_resolves_deployment_binding_with_dotted_subsystem_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "controller.subsystem.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: demo.mobile_base_prototype
  role: component
requires:
  actuator_servo_gateway:
    contract: api.actuator.servo.v1
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "gateway.subsystem.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: demo.isaac_sim_gateway
  role: gateway
provides:
  actuator_servo_gateway:
    contract: api.actuator.servo.v1
""".strip()
                + "\n",
                encoding="utf-8",
            )
            deployment_path = root / "r2kernel.deployment.yaml"
            deployment_path.write_text(
                """
kind: r2kernel.deployment
version: 1
subsystems:
  demo.mobile_base_prototype:
    manifest: ./controller.subsystem.yaml
  demo.isaac_sim_gateway:
    manifest: ./gateway.subsystem.yaml
bindings:
  demo.mobile_base_prototype.actuator_servo_gateway:
    to: demo.isaac_sim_gateway.actuator_servo_gateway
""".strip()
                + "\n",
                encoding="utf-8",
            )
            deployment = load_deployment_manifest(deployment_path)
            resolved = deployment.resolve("demo.mobile_base_prototype", "actuator_servo_gateway")
            self.assertEqual(resolved.target, "demo.isaac_sim_gateway")
            self.assertEqual(resolved.provider_interface, "actuator_servo_gateway")

    def test_resolves_deployment_instances_with_shared_package_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = load_deployment_manifest(
                deployment_case("deployment_instances_with_shared_package_manifest", Path(tmpdir))
            )
            self.assertIn("demo.left_manipulator", deployment.subsystems)
            self.assertEqual(deployment.subsystems["demo.left_manipulator"].name, "demo.left_manipulator")
            self.assertEqual(
                deployment.instances["demo.left_manipulator"].params["controller.robot_id"],
                "left_ps_ca",
            )
            resolved = deployment.resolve("demo.right_manipulator", "actuator_servo_gateway")
            self.assertEqual(resolved.target, "demo.isaac_sim_gateway")

    def test_spatial_import_can_reference_bound_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = load_deployment_manifest(
                deployment_case("deployment_spatial_import_references_bound_export", Path(tmpdir))
            )
            consumer = deployment.subsystems["frame_consumer"]
            self.assertEqual(consumer.spatial.ports["map"].direction, "import")
            self.assertEqual(consumer.spatial.ports["map"].source.port, "map")
            self.assertEqual(consumer.spatial.ports["body"].source.port, "body")

    def test_loads_robot_application_deployment_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            deployment_path = deployment_case("deployment_robot_application_composition", root)

            robot = load_robot_platform_manifest(root / "r2kernel.robot.yaml")
            application = load_application_manifest(root / "subsystem.yaml")
            deployment = load_deployment_manifest(deployment_path)

            self.assertEqual(robot.slots["perception_component"].role, "component")
            self.assertEqual(robot.slots["perception_component"].contract, "api.perception.query.v1")
            self.assertFalse(hasattr(robot, "capabilities"))
            self.assertFalse(hasattr(application, "required_capabilities"))
            self.assertEqual(application.required_robot_platforms, ("tutorial.robot_cleaner.cleaner_2d.v1",))
            self.assertEqual(application.subsystem_manifest, root / "subsystem.yaml")
            self.assertEqual(deployment.robot.name, "cleaner_2d")
            self.assertEqual(deployment.application.name, "app.pick_place")
            self.assertEqual(deployment.instances["component.perception"].slots["perception_component"], "object_detection")
            self.assertEqual(deployment.instances["gateway.arm"].slots["digital_gateway"], "digital")
            self.assertEqual(deployment.instances["app.pick_place"].launch, "external")
            bound = deployment.resolve("app.pick_place", "objects")
            self.assertEqual(bound.target, "component.perception")
            self.assertEqual(bound.contract, "api.perception.query.v1")

    def test_allows_managed_composition_only_compound_without_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = load_deployment_manifest(
                deployment_case("deployment_managed_compound_without_slot", Path(tmpdir))
            )
            self.assertEqual(deployment.instances["compound.platform"].slots, {})


if __name__ == "__main__":
    unittest.main()
