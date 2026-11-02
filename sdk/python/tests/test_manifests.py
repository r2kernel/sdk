from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r2kernel.manifest import (
    ManifestError,
    allowed_runtime_endpoints,
    load_deployment_manifest,
    load_subsystem_manifest,
    subsystem_manifest_status_payload,
    validate_runtime_endpoints,
)


class _Runtime:
    def get_procedure_info(self):
        return []

    def get_operation_info(self):
        return []

    def get_control_info(self):
        return []

    def get_publish_info(self):
        return []

    def get_subscribe_info(self):
        return []


class _ProcedureRuntime(_Runtime):
    def get_procedure_info(self):
        return ["increment"]


class _ExtraRuntime(_Runtime):
    def get_publish_info(self):
        return [("fixed", "bytes"), ("dynamic.1", "bytes")]


class _ConsumerRuntime(_Runtime):
    def get_subscribe_info(self):
        return [("fixed", "bytes")]


class ManifestTests(unittest.TestCase):
    def test_compound_declaratively_exports_component_conformance_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "compound.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: compound.platform
  role: compound
requires:
  mobility:
    contract: component.mobile_base.v1
exports:
  body_motion:
    contract: api.locomotion.mobile_base.state.v1
    from:
      requirement: mobility
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_subsystem_manifest(path)
            self.assertEqual(manifest.exports["body_motion"].requirement, "mobility")
            self.assertEqual(manifest.exports["body_motion"].contract, "api.locomotion.mobile_base.state.v1")
            status = subsystem_manifest_status_payload(manifest)
            self.assertEqual(
                status["interfaces"]["exports"]["body_motion"]["source"]["requirement"],
                "mobility",
            )
            self.assertEqual(manifest.declared_apis, ())

    def test_app_rejects_control_bearing_api_and_component_contracts(self) -> None:
        declarations = (
            ("requires", "api.locomotion.velocity.v1"),
            ("requires", "component.mobile_base.v1"),
            ("provides", "api.locomotion.velocity.v1"),
        )
        for section, contract in declarations:
            with self.subTest(section=section, contract=contract), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "app.subsystem.yaml"
                path.write_text(
                    f"""
kind: r2kernel.subsystem
version: 1
subsystem: {{name: app.control_policy, role: app}}
{section}:
  interface:
    contract: {contract}
""".strip() + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ManifestError, "cannot declare or consume Control endpoints"):
                    load_subsystem_manifest(path)

    def test_rejects_capability_contracts_and_fields(self) -> None:
        capability = "capability.r2kernel.mobility.body_motion.v1"
        cases = (
            (
                "app",
                f"requires:\n  body: {{contract: {capability}}}\n",
                "requires.body contract 'capability.r2kernel.mobility.body_motion.v1' is not supported; "
                "capability contracts were removed",
            ),
            (
                "component",
                f"provides:\n  body: {capability}\n",
                "provides.body contract .* capability contracts were removed",
            ),
            (
                "component",
                f"provides:\n  body: {{contract: {capability}, visibility: internal}}\n",
                "provides.body contract .* capability contracts were removed",
            ),
            (
                "component",
                f"provides:\n  body: {{capability: {capability}}}\n",
                "provides.body field `capability` is not supported",
            ),
            (
                "component",
                f"provides:\n  body: {{contract: api.locomotion.velocity.v1, capability: {capability}}}\n",
                "provides.body field `capability` is not supported",
            ),
            (
                "compound",
                "requires:\n  base: {contract: component.mobile_base.v1}\n"
                f"exports:\n  body:\n    contract: {capability}\n    from: {{requirement: base}}\n",
                "exports.body contract .* capability contracts were removed",
            ),
        )
        for role, body, message in cases:
            with self.subTest(role=role, body=body), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "subsystem.yaml"
                path.write_text(
                    "kind: r2kernel.subsystem\nversion: 1\n"
                    f"subsystem: {{name: {role}.legacy, role: {role}}}\n" + body,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ManifestError, message):
                    load_subsystem_manifest(path)

    def test_app_and_compound_reject_internal_control_endpoints(self) -> None:
        declarations = (("app", "requires"), ("app", "provides"), ("compound", "requires"))
        for role, section in declarations:
            with self.subTest(role=role, section=section), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / f"{role}.subsystem.yaml"
                contract = "private.control.v1" if role == "app" else "component.navigation.v1"
                path.write_text(
                    f"""
kind: r2kernel.subsystem
version: 1
subsystem: {{name: {role}.control_policy, role: {role}}}
{section}:
  internal_control:
    contract: {contract}
    visibility: internal
    endpoints:
      controls: [stream_control]
""".strip() + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ManifestError, "internal Control endpoints"):
                    load_subsystem_manifest(path)

    def test_compound_cannot_provide_interfaces_or_export_controls(self) -> None:
        cases = (
            (
                "provides",
                """
provides:
  implementation: {contract: api.io.digital.output.v1}
""",
                "declarative and cannot declare provides",
            ),
            (
                "exports",
                """
requires:
  mobile_base: {contract: component.mobile_base.v1}
exports:
  velocity: {contract: api.locomotion.velocity.v1, from: {requirement: mobile_base}}
""",
                "cannot export Control endpoints",
            ),
        )
        for section, body, error in cases:
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "compound.subsystem.yaml"
                path.write_text(
                    f"""
kind: r2kernel.subsystem
version: 1
subsystem: {{name: compound.control_policy, role: compound}}
{body}
""".strip() + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ManifestError, error):
                    load_subsystem_manifest(path)

    def test_compound_requires_component_contracts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "compound.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: compound.direct_api, role: compound}
requires:
  navigation: {contract: api.navigation.v1}
""".strip() + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManifestError, "must reference a component.* contract"):
                load_subsystem_manifest(path)

    def test_app_can_bind_directly_to_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "app.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.reader, role: app}
requires:
  state: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "component.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: component.base, role: component}
provides:
  state: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            deployment_path = root / "deployment.yaml"
            deployment_path.write_text(
                """
kind: r2kernel.deployment
version: 1
subsystems:
  app.reader: {manifest: app.yaml}
  component.base: {manifest: component.yaml}
bindings:
  app.reader.state: {to: component.base.state}
""".strip() + "\n",
                encoding="utf-8",
            )

            resolved = load_deployment_manifest(deployment_path).resolve("app.reader", "state")
            self.assertEqual(resolved.target_subsystem, "component.base")

    def test_control_policy_fails_closed_when_contract_specs_cannot_be_loaded(self) -> None:
        declarations = (
            (
                "app",
                "requires",
                "api.locomotion.velocity.v1",
                "visibility: internal",
            ),
            (
                "compound",
                "requires",
                "component.mobile_base.v1",
                "",
            ),
        )
        for role, section, contract, options in declarations:
            with self.subTest(role=role, contract=contract), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / f"{role}.subsystem.yaml"
                path.write_text(
                    f"""
kind: r2kernel.subsystem
version: 1
subsystem: {{name: {role}.bad_spec, role: {role}}}
{section}:
  surface:
    contract: {contract}
    {options}
""".strip() + "\n",
                    encoding="utf-8",
                )

                with patch(
                    "r2kernel.manifest._contracts.load_api_contract_spec",
                    side_effect=ValueError("malformed referenced API spec"),
                ):
                    with self.assertRaisesRegex(ManifestError, "Cannot inspect contract"):
                        load_subsystem_manifest(path)

    def test_app_cannot_bind_to_another_apps_provided_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "consumer.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.consumer, role: app}
requires:
  state: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "provider.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.provider, role: app}
provides:
  state: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            deployment_path = root / "deployment.yaml"
            deployment_path.write_text(
                """
kind: r2kernel.deployment
version: 1
subsystems:
  app.consumer: {manifest: consumer.yaml}
  app.provider: {manifest: provider.yaml}
bindings:
  app.consumer.state: {to: app.provider.state}
""".strip() + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManifestError, "may bind directly only to component-provided"):
                load_deployment_manifest(deployment_path)

    def test_compound_export_rejects_api_outside_component_conformance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "compound.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: compound.platform
  role: compound
requires:
  mobility:
    contract: component.mobile_base.v1
exports:
  navigation:
    contract: api.navigation.v1
    from:
      requirement: mobility
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManifestError, "does not guarantee"):
                load_subsystem_manifest(path)

    def test_deployment_resolves_export_to_physical_component_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "app.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.cleaner, role: app}
requires:
  mobility: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "compound.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: compound.cleaner, role: compound}
requires:
  mobility: {contract: component.mobile_base.v1}
exports:
  body_motion:
    contract: api.locomotion.mobile_base.state.v1
    from: {requirement: mobility}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "component.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: component.mobile, role: component}
spatial:
  participation: required
  ports:
    reference: {direction: export, semantic: odometry_reference, frame: odom}
    body: {direction: export, semantic: mobile_body, frame: base_link}
provides:
  mobile_base: {contract: component.mobile_base.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            deployment_path = root / "deployment.yaml"
            deployment_path.write_text(
                """
kind: r2kernel.deployment
version: 1
subsystems:
  app.cleaner: {manifest: app.yaml}
  compound.cleaner: {manifest: compound.yaml}
  component.mobile: {manifest: component.yaml}
bindings:
  app.cleaner.mobility: {to: compound.cleaner.body_motion}
  compound.cleaner.mobility: {to: component.mobile.mobile_base}
""".strip() + "\n",
                encoding="utf-8",
            )

            deployment = load_deployment_manifest(deployment_path)
            resolved = deployment.resolve("app.cleaner", "mobility")
            self.assertEqual(resolved.target_subsystem, "component.mobile")
            self.assertEqual(resolved.provider_interface, "mobile_base")
            self.assertEqual(resolved.contract, "api.locomotion.mobile_base.state.v1")

    def test_deployment_rejects_mismatched_export_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "app.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.cleaner, role: app}
requires:
  mobility: {contract: api.locomotion.mobile_base.state.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "compound.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: compound.cleaner, role: compound}
requires:
  mobility: {contract: component.mobile_base.v1}
exports:
  body_motion:
    contract: api.locomotion.mobile_base.state.v1
    from: {requirement: mobility}
""".strip() + "\n",
                encoding="utf-8",
            )
            (root / "wrong.yaml").write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: component.wrong, role: component}
provides:
  device: {contract: component.trigger_actuator.v1}
""".strip() + "\n",
                encoding="utf-8",
            )
            deployment_path = root / "deployment.yaml"
            deployment_path.write_text(
                """
kind: r2kernel.deployment
version: 1
subsystems:
  app.cleaner: {manifest: app.yaml}
  compound.cleaner: {manifest: compound.yaml}
  component.wrong: {manifest: wrong.yaml}
bindings:
  app.cleaner.mobility: {to: compound.cleaner.body_motion}
  compound.cleaner.mobility: {to: component.wrong.device}
""".strip() + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManifestError, "Binding contract mismatch"):
                load_deployment_manifest(deployment_path)

    def test_app_and_compound_cannot_bind_directly_to_gateways(self) -> None:
        for role in ("app", "compound"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                contract = "api.navigation.v1" if role == "app" else "component.navigation.v1"
                (root / "consumer.yaml").write_text(
                    f"""
kind: r2kernel.subsystem
version: 1
subsystem: {{name: {role}.legacy, role: {role}}}
requires:
  digital_io: {{contract: {contract}}}
""".strip() + "\n",
                    encoding="utf-8",
                )
                (root / "gateway.yaml").write_text(
                    """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.legacy, role: gateway}
provides:
  digital_io: {contract: api.actuator.servo.v1}
""".strip() + "\n",
                    encoding="utf-8",
                )
                deployment_path = root / "deployment.yaml"
                deployment_path.write_text(
                    f"""
kind: r2kernel.deployment
version: 1
subsystems:
  {role}.legacy: {{manifest: consumer.yaml}}
  gateway.legacy: {{manifest: gateway.yaml}}
bindings:
  {role}.legacy.digital_io: {{to: gateway.legacy.digital_io}}
""".strip() + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ManifestError, "cannot bind directly to gateway"):
                    load_deployment_manifest(deployment_path)

    def test_compound_can_bind_only_to_component_providers(self) -> None:
        for provider_role in ("app", "compound"):
            with self.subTest(provider_role=provider_role), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                provider_name = f"{provider_role}.provider"
                provider_manifest = (
                    """
kind: r2kernel.subsystem
version: 1
subsystem: {name: app.provider, role: app}
provides:
  navigation: {contract: component.navigation.v1}
"""
                    if provider_role == "app"
                    else """
kind: r2kernel.subsystem
version: 1
subsystem: {name: compound.provider, role: compound}
requires:
  navigation_component: {contract: component.navigation.v1}
exports:
  navigation: {contract: api.navigation.v1, from: {requirement: navigation_component}}
"""
                )
                (root / "consumer.yaml").write_text(
                    """
kind: r2kernel.subsystem
version: 1
subsystem: {name: compound.consumer, role: compound}
requires:
  navigation: {contract: component.navigation.v1}
""".strip() + "\n",
                    encoding="utf-8",
                )
                (root / "provider.yaml").write_text(provider_manifest.strip() + "\n", encoding="utf-8")
                (root / "component.yaml").write_text(
                    """
kind: r2kernel.subsystem
version: 1
subsystem: {name: component.base, role: component}
provides:
  navigation: {contract: component.navigation.v1}
""".strip() + "\n",
                    encoding="utf-8",
                )
                provider_binding = (
                    "  compound.provider.navigation_component: {to: component.base.navigation}\n"
                    if provider_role == "compound"
                    else ""
                )
                deployment_path = root / "deployment.yaml"
                deployment_path.write_text(
                    f"""
kind: r2kernel.deployment
version: 1
subsystems:
  compound.consumer: {{manifest: consumer.yaml}}
  {provider_name}: {{manifest: provider.yaml}}
  component.base: {{manifest: component.yaml}}
bindings:
  compound.consumer.navigation: {{to: {provider_name}.navigation}}
{provider_binding}""".strip() + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ManifestError, "may bind only to component-provided"):
                    load_deployment_manifest(deployment_path)

    def test_compound_spatial_participation_allows_mounts_without_exported_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: compound.internal
  role: compound
spatial:
  participation: required
  mounts:
    - name: tool_on_body
      parent: {requirement: body, port: body}
      child: {requirement: tool, port: base}
      transform:
        translation: [0, 0, 0]
        rotation_xyzw: [0, 0, 0, 1]
requires:
  body:
    contract: component.mobile_base.v1
  tool:
    contract: component.trigger_actuator.v1
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest = load_subsystem_manifest(path)
            self.assertEqual(manifest.spatial.participation, "required")
            self.assertEqual(len(manifest.spatial.mounts), 1)
            self.assertEqual(manifest.spatial.ports, {})

    def test_loads_subsystem_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "server.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: server
  role: component
provides:
  service:
    contract: tutorial.transport.procedure.v1
    visibility: internal
    endpoints:
      procedures:
        - increment
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest = load_subsystem_manifest(path)
            self.assertEqual(manifest.name, "server")
            self.assertEqual(manifest.provides["service"].contract, "tutorial.transport.procedure.v1")
            self.assertEqual(manifest.provides["service"].endpoints["procedures"], ("increment",))
            self.assertEqual(validate_runtime_endpoints(manifest, _ProcedureRuntime()), ())

    def test_rejects_runtime_endpoint_not_declared_by_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "server.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: server
  role: component
provides:
  events:
    contract: tutorial.transport.events.v1
    visibility: internal
    endpoints:
      publishes:
        - fixed
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest = load_subsystem_manifest(path)
            issues = validate_runtime_endpoints(manifest, _ExtraRuntime())
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].endpoint_kind, "publishes")
            self.assertEqual(issues[0].endpoint, "dynamic.1")
            self.assertIn("not declared", issues[0].message)

    def test_required_provider_publish_allows_local_subscribe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "client.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: client
  role: component
requires:
  events:
    contract: tutorial.transport.events.v1
    visibility: internal
    endpoints:
      publishes:
        - fixed
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest = load_subsystem_manifest(path)
            self.assertEqual(allowed_runtime_endpoints(manifest)["subscribes"], ("fixed",))
            self.assertEqual(validate_runtime_endpoints(manifest, _ConsumerRuntime()), ())

    def test_rejects_public_manifest_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "gateway.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: gateway
  role: gateway
provides:
  servo:
    contract: api.actuator.servo.v1
    endpoints:
      controls:
        - servo_control
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "public interface endpoints are defined"):
                load_subsystem_manifest(path)

    def test_validates_public_contract_endpoint_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "gateway.subsystem.yaml"
            path.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: gateway
  role: gateway
provides:
  servo:
    contract: api.actuator.servo.v1
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest = load_subsystem_manifest(path)
            issues = validate_runtime_endpoints(manifest, _Runtime())
            self.assertEqual(len(issues), 1)
            self.assertEqual(
                {(issue.endpoint_kind, issue.endpoint) for issue in issues},
                {("controls", "servo_control")},
            )

    def test_gateway_surfaces_must_be_control_only(self) -> None:
        valid_manifest = """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.policy, role: gateway}
provides:
  servo:
    contract: api.actuator.servo.v1
  private_sensor:
    contract: tutorial.gateway.sensor.v1
    visibility: internal
    endpoints:
      controls: [read_sample]
"""
        invalid_manifests = (
            (
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.policy, role: gateway}
provides:
  mixed:
    contract: api.locomotion.velocity.v1
""",
                "Gateway may use only Control endpoints",
            ),
            (
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.policy, role: gateway}
provides:
  private_sensor:
    contract: tutorial.gateway.sensor.v1
    visibility: internal
    endpoints:
      procedures: [read_sample]
""",
                "Gateway may use only Control endpoints",
            ),
            (
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.policy, role: gateway}
provides:
  mixed:
    contract: api.actuator.servo.v1
    visibility: internal
    endpoints:
      events: [unexpected_event]
""",
                "Gateway may use only Control endpoints",
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "gateway.yaml"
            path.write_text(valid_manifest, encoding="utf-8")
            self.assertEqual(load_subsystem_manifest(path).role, "gateway")
            for document, message in invalid_manifests:
                with self.subTest(message=message):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(ManifestError, message):
                        load_subsystem_manifest(path)


if __name__ == "__main__":
    unittest.main()
