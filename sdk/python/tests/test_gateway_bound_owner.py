from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import call, patch

from r2kernel.wrapper.subsystem import GatewaySubsystem


class FakeRuntime:
    def __init__(self, **_kwargs) -> None:
        pass


class ProbeGatewaySubsystem(GatewaySubsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class GatewayBoundOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        manifest = root / "gateway.yaml"
        manifest.write_text(
            """
kind: r2kernel.subsystem
version: 1
subsystem: {name: gateway.device, role: gateway}
provides:
  device_control:
    contract: tutorial.gateway.control.v1
    visibility: internal
    endpoints:
      controls: [set_value]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
            self.subsystem = ProbeGatewaySubsystem(
                name="gateway.device",
                user_data_path=str(root / "userdata"),
                manifest_path=manifest,
            )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_returns_source_subsystem_for_one_direct_bound_control(self) -> None:
        response = self._status(
            [
                self._binding(),
                self._binding(
                    source="component.other.device_control",
                    source_subsystem="component.other",
                    target="component.other.device_control",
                    target_subsystem="component.other",
                    provider_interface="device_control",
                ),
                {
                    "source": "gateway.device.optional_control",
                    "source_subsystem": "gateway.device",
                    "requirement": "optional_control",
                    "target": "",
                    "target_subsystem": "",
                    "provider_interface": "",
                    "state": "unbound",
                    "runtime": True,
                    "explicit_unbound": True,
                },
            ]
        )
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=response,
        ) as exchange:
            owner = self.subsystem.bound_control_owner("device_control")

        self.assertEqual(owner, "component.cleaner")
        exchange.assert_called_once_with(
            {"cmd": "binding_status", "subsystem": "gateway.device"},
            retries=1,
        )

    def test_lists_multiple_source_requirement_identities_for_one_provider(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(self._two_source_bindings()),
        ):
            source_bindings = self.subsystem.bound_control_sessions("device_control")

        self.assertEqual(
            source_bindings,
            {
                "component.cleaner.device_control": "component.cleaner",
                "component.navigation.device_control": "component.navigation",
            },
        )

    def test_resolves_one_source_subsystem_to_its_binding_reference(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(self._two_source_bindings()),
        ):
            source_reference = self.subsystem.bound_control_session(
                "device_control", "component.navigation"
            )

        self.assertEqual(source_reference, "component.navigation.device_control")

    def test_per_source_lookup_rejects_missing_or_ambiguous_source(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(self._two_source_bindings()),
        ):
            with self.assertRaisesRegex(RuntimeError, "No runtime binding from source subsystem"):
                self.subsystem.bound_control_session("device_control", "component.unknown")

        repeated_subsystem = [
            self._binding(),
            self._binding(
                source="component.cleaner.emergency_control",
                requirement="emergency_control",
            ),
        ]
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(repeated_subsystem),
        ):
            with self.assertRaisesRegex(RuntimeError, "Ambiguous runtime bindings from source subsystem"):
                self.subsystem.bound_control_session("device_control", "component.cleaner")

    def test_missing_binding_fails_closed(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status([]),
        ):
            with self.assertRaisesRegex(RuntimeError, "No active runtime binding"):
                self.subsystem.bound_control_owner("device_control")

    def test_single_source_owner_accessor_rejects_multiple_sources(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(self._two_source_bindings()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Ambiguous runtime bindings"):
                self.subsystem.bound_control_owner("device_control")

    def test_binding_to_another_provider_port_does_not_match(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status(
                [
                    self._binding(
                        target="gateway.device.other_control",
                        provider_interface="other_control",
                    )
                ]
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "No active runtime binding"):
                self.subsystem.bound_control_owner("device_control")

    def test_non_bound_direct_binding_fails_closed(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._status([self._binding(state="target_missing")]),
        ):
            with self.assertRaisesRegex(RuntimeError, "is not bound"):
                self.subsystem.bound_control_session("device_control", "component.cleaner")

    def test_unavailable_runtime_status_fails_closed(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            side_effect=OSError("control socket unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Runtime binding status is unavailable"):
                self.subsystem.bound_control_owner("device_control")

    def test_malformed_runtime_status_and_source_fail_closed(self) -> None:
        malformed_responses = [
            None,
            {"ok": True, "res": "ok", "bindings": {}},
            {"ok": False, "res": "error", "bindings": []},
            self._status(
                [self._binding(source="invalid", source_subsystem="component.cleaner")]
            ),
            self._status(
                [
                    self._binding(
                        source="component.cleaner.control",
                        source_subsystem="component.other",
                    )
                ]
            ),
            self._status(
                [
                    self._binding(
                        provider_interface="other_control",
                    )
                ]
            ),
        ]
        for response in malformed_responses:
            with self.subTest(response=response):
                with patch(
                    "r2kernel.wrapper.subsystem.exchange_control_request",
                    return_value=response,
                ):
                    with self.assertRaises(RuntimeError):
                        self.subsystem.bound_control_owner("device_control")

    def test_queries_fresh_runtime_status_on_each_call(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            side_effect=[
                self._status([self._binding()]),
                self._status(
                    [
                        self._binding(
                            source="component.updated.device_control",
                            source_subsystem="component.updated",
                        )
                    ]
                ),
            ],
        ) as exchange:
            self.assertEqual(
                self.subsystem.bound_control_owner("device_control"), "component.cleaner"
            )
            self.assertEqual(
                self.subsystem.bound_control_owner("device_control"), "component.updated"
            )

        self.assertEqual(
            exchange.call_args_list,
            [
                call({"cmd": "binding_status", "subsystem": "gateway.device"}, retries=1),
                call({"cmd": "binding_status", "subsystem": "gateway.device"}, retries=1),
            ],
        )

    def test_requires_a_declared_provider_interface(self) -> None:
        with patch("r2kernel.wrapper.subsystem.exchange_control_request") as exchange:
            with self.assertRaisesRegex(RuntimeError, "is not declared"):
                self.subsystem.bound_control_owner("missing_control")
        exchange.assert_not_called()

    def test_remote_authority_rejects_other_peer_session_registration_or_rebind(self) -> None:
        binding = self._binding(source_peer_id="compute", source_session_id="session-a", target_session_id="provider-a",
            source_registration_revision="source-reg-a", target_registration_revision="target-reg-a", binding_revision="binding-a")
        authority = {"source":"component.cleaner","source_reference":"component.cleaner.device_control", "peer":"compute",
            "source_session":"session-a","target_session":"provider-a","interface":"device_control",
            "source_registration":"source-reg-a","target_registration":"target-reg-a","binding_revision":"binding-a"}
        with patch("r2kernel.wrapper.subsystem.exchange_control_request", return_value=self._status([binding])):
            context = SimpleNamespace(source_subsystem="component.cleaner", source_authority=authority)
            self.assertEqual(self.subsystem.validate_control_authority("device_control", context), authority)
            for field in ("peer", "source_session", "source_registration", "target_registration", "binding_revision", "interface"):
                with self.subTest(field=field):
                    changed = dict(authority); changed[field] = "another-owner-epoch"
                    with self.assertRaisesRegex(RuntimeError, "registration or binding epoch"):
                        self.subsystem.validate_control_authority("device_control", SimpleNamespace(source_subsystem="component.cleaner", source_authority=changed))
            with self.assertRaisesRegex(RuntimeError, "registration or binding epoch"):
                self.subsystem.validate_control_authority("device_control", SimpleNamespace(source_subsystem="component.cleaner"))

    @staticmethod
    def _status(bindings: list[dict[str, Any]]) -> dict[str, object]:
        return {"ok": True, "res": "ok", "bindings": bindings}

    @staticmethod
    def _two_source_bindings() -> list[dict[str, str]]:
        return [
            GatewayBoundOwnerTests._binding(),
            GatewayBoundOwnerTests._binding(
                source="component.navigation.device_control",
                source_subsystem="component.navigation",
            ),
        ]

    @staticmethod
    def _binding(**overrides: str) -> dict[str, str]:
        binding = {
            "source": "component.cleaner.device_control",
            "source_subsystem": "component.cleaner",
            "requirement": "device_control",
            "target": "gateway.device.device_control",
            "target_subsystem": "gateway.device",
            "provider_interface": "device_control",
            "state": "bound",
        }
        binding.update(overrides)
        return binding


if __name__ == "__main__":
    unittest.main()
