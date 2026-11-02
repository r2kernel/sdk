from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from r2kernel.wrapper.subsystem import AppSubsystem, ComponentSubsystem, GatewaySubsystem, Subsystem


class FakeRuntime:
    def __init__(self, **_kwargs) -> None:
        pass


class ProbeSubsystem(Subsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class ProbeComponentSubsystem(ComponentSubsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class ProbeGatewaySubsystem(GatewaySubsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class ProbeAppSubsystem(AppSubsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class LaunchBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_bindings = os.environ.pop("R2KERNEL_BINDINGS", None)

    def tearDown(self) -> None:
        if self._old_bindings is None:
            os.environ.pop("R2KERNEL_BINDINGS", None)
        else:
            os.environ["R2KERNEL_BINDINGS"] = self._old_bindings

    def test_launch_binding_resolves_without_deployment_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)
            os.environ["R2KERNEL_BINDINGS"] = '{"service":"server.service"}'

            with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
                subsystem = ProbeSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )

            resolved = subsystem.resolve_binding("service")
            self.assertEqual(resolved.target_subsystem, "server")
            self.assertEqual(resolved.provider_interface, "service")
            self.assertEqual(resolved.contract, "tutorial.transport.procedure.v1")
            self.assertEqual(subsystem.binding_target("service"), "server")

    def test_runtime_unbind_blocks_launch_binding_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)
            os.environ["R2KERNEL_BINDINGS"] = '{"service":"server.service"}'

            with (
                patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime),
                patch(
                    "r2kernel.wrapper.subsystem.exchange_control_request",
                    return_value={
                        "ok": False,
                        "res": "unbound",
                        "runtime": True,
                    },
                ),
            ):
                subsystem = ProbeSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )
                with self.assertRaisesRegex(RuntimeError, "explicitly unbound"):
                    subsystem.resolve_binding("service")

    def test_unresolved_runtime_export_blocks_static_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)
            os.environ["R2KERNEL_BINDINGS"] = '{"service":"compound.platform.service"}'

            with (
                patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime),
                patch(
                    "r2kernel.wrapper.subsystem.exchange_control_request",
                    return_value={
                        "ok": True,
                        "res": "ok",
                        "binding": {
                            "state": "export_unbound",
                            "target_subsystem": "compound.platform",
                            "provider_interface": "service",
                            "resolved_target_subsystem": "",
                            "resolved_provider_interface": "",
                        },
                    },
                ),
            ):
                subsystem = ProbeSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )
                with self.assertRaisesRegex(RuntimeError, "state=export_unbound"):
                    subsystem.resolve_binding("service")

    def test_missing_launch_binding_for_required_interface_waits_for_runtime_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)

            with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
                subsystem = ProbeSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )
            with self.assertRaisesRegex(RuntimeError, "Required binding is missing"):
                subsystem.resolve_binding("service")

    def test_unknown_launch_binding_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)
            os.environ["R2KERNEL_BINDINGS"] = '{"unknown":"server.service"}'

            with self.assertRaisesRegex(RuntimeError, "unknown required interface"):
                ProbeSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )

    def test_role_specific_subclass_validates_manifest_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_client_manifest(root)
            os.environ["R2KERNEL_BINDINGS"] = '{"service":"server.service"}'

            with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
                ProbeComponentSubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )

            with self.assertRaisesRegex(RuntimeError, "requires subsystem.role 'gateway'"):
                ProbeGatewaySubsystem(
                    name="client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )

    def test_app_cannot_invoke_raw_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "app.yaml"
            manifest.write_text(
                "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: app.client, role: app}\n",
                encoding="utf-8",
            )
            with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
                subsystem = ProbeAppSubsystem(
                    name="app.client",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )
            with self.assertRaisesRegex(RuntimeError, "Control is not available to app"):
                subsystem.transact_control(
                    target="gateway.device",
                    topic="velocity_command",
                    type="example",
                    value={},
                    period_ms=10.0,
                    deadline_ms=10.0,
                )

    def test_gateway_wrapper_blocks_non_control_registration_and_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "gateway.yaml"
            manifest.write_text(
                """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.policy, role: gateway}
provides:
  private_control:
    contract: tutorial.gateway.control.v1
    visibility: internal
    endpoints:
      controls: [read_sample]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
                subsystem = ProbeGatewaySubsystem(
                    name="gateway.policy",
                    user_data_path=str(root / "userdata"),
                    manifest_path=manifest,
                )

        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.register_event_publish(topic="sample", type="bytes")
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.register_procedure("query", lambda _params: {})
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.publish_event(topic="sample", value={})
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.call_procedure(target="component.sensor", procedure="query")
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.call_typed_procedure(
                target="component.sensor",
                procedure="query",
                request_type="bytes",
                response_type="bytes",
                params={},
            )
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.start_operation(target="component.sensor", operation="scan")
        with self.assertRaisesRegex(RuntimeError, "may use only Control endpoints"):
            subsystem.cancel_operation(target="component.sensor", operation_id="operation-1")

    def _write_client_manifest(self, root: Path) -> Path:
        path = root / "subsystem.yaml"
        path.write_text(
            """
kind: r2kernel.subsystem
version: 1
subsystem:
  name: client
  role: component
requires:
  service:
    contract: tutorial.transport.procedure.v1
    visibility: internal
    required: true
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
