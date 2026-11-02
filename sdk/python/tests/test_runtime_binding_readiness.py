from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from r2kernel.manifest import BoundInterface
from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.wrapper.subsystem import Subsystem

WIRE_DIR = Path(__file__).resolve().parents[2] / "testdata" / "wire"


def _prepare_bound(subsystem, requirement, entrypoint, name, request_capacity, response_capacity, *, timeout_ms):
    """Spend one budget on strict binding resolution, route lookup and provider preparation."""
    deadline = subsystem._runtime._control_preparation_deadline(timeout_ms)
    target = subsystem.binding_target(requirement, deadline=deadline)
    return getattr(subsystem, entrypoint)(
        target, name, request_capacity, response_capacity, timeout_ms, False, _deadline=deadline,
    )


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


class RuntimeBindingReadinessTests(unittest.TestCase):
    requirement = "service"
    contract = "tutorial.transport.procedure.v1"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self._old_bindings = os.environ.get("R2KERNEL_BINDINGS")
        os.environ["R2KERNEL_BINDINGS"] = '{"service":"server.service"}'
        manifest = root / "subsystem.yaml"
        manifest.write_text(
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
""".lstrip(),
            encoding="utf-8",
        )
        with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", FakeRuntime):
            self.subsystem = ProbeSubsystem(
                name="client",
                user_data_path=str(root / "userdata"),
                manifest_path=manifest,
            )

    def tearDown(self) -> None:
        if self._old_bindings is None:
            os.environ.pop("R2KERNEL_BINDINGS", None)
        else:
            os.environ["R2KERNEL_BINDINGS"] = self._old_bindings
        self._temporary_directory.cleanup()

    def test_returns_only_the_resolved_runtime_target_when_bound(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value={"ok": True, "res": "ok", "binding": self._binding()},
        ) as exchange:
            resolved = self.subsystem.resolve_runtime_binding(self.requirement)

        self.assertEqual(
            resolved,
            BoundInterface(
                requirement=self.requirement,
                target_subsystem="server",
                provider_interface="service",
                contract=self.contract,
            ),
        )
        exchange.assert_called_once_with(
            {"cmd": "binding_resolve", "subsystem": "client", "requirement": "service"},
            timeout_s=1.0,
            retries=1,
        )

    def test_normal_missing_and_source_registration_pending_return_none(self) -> None:
        responses = (
            self._missing_response("not_bound"),
            self._missing_response(
                "source_registration_pending",
                binding=self._binding(state="source_missing"),
            ),
            self._missing_response(
                "source_registration_pending",
                binding=self._binding(state="source_role_missing"),
            ),
        )
        for response in responses:
            with self.subTest(reason=response["reason"]), patch(
                "r2kernel.wrapper.subsystem.exchange_control_request",
                return_value=response,
            ):
                self.assertIsNone(self.subsystem.resolve_runtime_binding(self.requirement))

    def test_pending_reply_of_a_restarted_source_returns_none(self) -> None:
        # Control's real reply (sdk/testdata/wire): the restarted source asks before its
        # first status and the binding pinned to its old registration is registration_changed.
        fixture = json.loads((WIRE_DIR / "binding-resolve-pending-registration-changed.json").read_text(encoding="utf-8"))
        response = json.loads(fixture["wire"])
        self.assertEqual(response["binding"]["state"], "registration_changed")
        for message in (response, response["binding"]):
            message["source"] = "client.service"
            message["requirement"] = "service"
        response["subsystem"] = "client"
        response["binding"]["source_subsystem"] = "client"
        with patch("r2kernel.wrapper.subsystem.exchange_control_request", return_value=response):
            self.assertIsNone(self.subsystem.resolve_runtime_binding(self.requirement))

    def test_runtime_readiness_does_not_fall_back_to_launch_binding(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._missing_response("not_bound"),
        ):
            launch_binding = self.subsystem.resolve_binding(self.requirement)
            self.assertEqual(launch_binding.target_subsystem, "server")
            self.assertEqual(launch_binding.provider_interface, "service")
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            return_value=self._missing_response("not_bound"),
        ):
            self.assertIsNone(self.subsystem.resolve_runtime_binding(self.requirement))

    def test_explicit_unbind_and_noncallable_binding_states_raise(self) -> None:
        unbound = {
            "ok": False,
            "res": "unbound",
            "source": "client.service",
            "subsystem": "client",
            "requirement": "service",
            "reason": "runtime_unbound",
            "runtime": True,
        }
        with patch("r2kernel.wrapper.subsystem.exchange_control_request", return_value=unbound):
            with self.assertRaisesRegex(RuntimeError, "explicitly unbound"):
                self.subsystem.resolve_runtime_binding(self.requirement)

        for state in ("contract_mismatch", "invalid_registered_role", "target_missing"):
            with self.subTest(state=state), patch(
                "r2kernel.wrapper.subsystem.exchange_control_request",
                return_value={"ok": True, "res": "ok", "binding": self._binding(state=state)},
            ):
                with self.assertRaisesRegex(RuntimeError, f"state={state}"):
                    self.subsystem.resolve_runtime_binding(self.requirement)

    def test_transport_and_protocol_errors_raise_instead_of_becoming_pending(self) -> None:
        with patch(
            "r2kernel.wrapper.subsystem.exchange_control_request",
            side_effect=OSError("control socket unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "control socket unavailable"):
                self.subsystem.resolve_runtime_binding(self.requirement)

        malformed_responses = (
            None,
            {"ok": True, "res": "ok", "binding": []},
            {
                "ok": True,
                "res": "ok",
                "binding": self._binding(resolved_target_subsystem={"name": "server"}),
            },
            {
                "ok": True,
                "res": "ok",
                "binding": self._binding(resolved_provider_interface=7),
            },
            {
                "ok": True,
                "res": "ok",
                "binding": {**self._binding(), "provided_contract": "component.wrong.v1"},
            },
            self._missing_response("unknown_reason"),
            self._missing_response(
                "source_registration_pending",
                binding=self._binding(state="contract_mismatch"),
            ),
            self._missing_response("not_bound", source="another.source"),
        )
        for response in malformed_responses:
            with self.subTest(response=response), patch(
                "r2kernel.wrapper.subsystem.exchange_control_request",
                return_value=response,
            ):
                with self.assertRaises(RuntimeError):
                    self.subsystem.resolve_runtime_binding(self.requirement)

    def test_undeclared_requirement_is_rejected_before_control_exchange(self) -> None:
        with patch("r2kernel.wrapper.subsystem.exchange_control_request") as exchange:
            with self.assertRaisesRegex(RuntimeError, "does not declare required interface"):
                self.subsystem.resolve_runtime_binding("typo")
        exchange.assert_not_called()


    def test_bounded_call_never_falls_back_or_dispatches_after_binding_failure(self) -> None:
        for entrypoint, endpoint, runtime_method in (
            ("call_bound_procedure", "procedure", "call_procedure"),
            ("start_bound_operation", "operation", "start_operation"),
        ):
            for failure in ("expired_before_lookup", "expired_reply", "timeout", "unavailable", "missing"):
                now = [10.02 if failure == "expired_before_lookup" else 10.0]
                timeout_error = TimeoutError("binding lookup expired")

                def resolve(_payload, **_kwargs):
                    if failure == "expired_reply":
                        now[0] = 10.02
                    elif failure == "timeout":
                        raise timeout_error
                    elif failure == "unavailable":
                        raise OSError("control unavailable")
                    elif failure == "missing":
                        return self._missing_response("not_bound")
                    return {"ok": True, "res": "ok", "binding": self._binding()}

                expected = RuntimeError if failure in ("unavailable", "missing") else TimeoutError
                with self.subTest(entrypoint=entrypoint, failure=failure), \
                     patch.object(self.subsystem, "_new_control_deadline", return_value=10.02), \
                     patch("r2kernel.wrapper.subsystem.time.monotonic", side_effect=lambda: now[0]), \
                     patch("r2kernel.wrapper.subsystem.exchange_control_request", side_effect=resolve) as exchange, \
                     patch.object(self.subsystem._runtime, runtime_method, create=True) as dispatch:
                    with self.assertRaises(expected) as caught:
                        getattr(self.subsystem, entrypoint)(
                            binding=self.requirement, **{endpoint: "read"}, timeout_ms=20,
                        )
                    if failure == "timeout":
                        self.assertIs(caught.exception, timeout_error)
                    if failure == "expired_before_lookup":
                        exchange.assert_not_called()
                    else:
                        exchange.assert_called_once()
                    dispatch.assert_not_called()

    def test_bound_preparation_keeps_binding_route_and_provider_in_one_budget(self) -> None:
        for entrypoint in ("prepare_control", "prepare_procedure", "prepare_operation"):
            for stage in ("success", "binding_expired", "binding_timeout", "binding_missing",
                          "route_expired", "provider_expired"):
                with self.subTest(entrypoint=entrypoint, stage=stage):
                    now = [10.0]
                    routes, preparations, callbacks = [], [], []
                    runtime = object.__new__(SubsystemRuntime)
                    runtime._control_target_cache = {}

                    def resolve_binding(_payload, **kwargs):
                        self.assertAlmostEqual(kwargs["deadline"], 10.02)
                        self.assertAlmostEqual(kwargs["timeout_s"], 0.02)
                        now[0] += 0.01
                        if stage == "binding_expired":
                            now[0] = 10.02
                        if stage == "binding_timeout":
                            raise TimeoutError("binding deadline exceeded")
                        if stage == "binding_missing":
                            return self._missing_response("not_bound")
                        return {"ok": True, "res": "ok", "binding": self._binding()}

                    def resolve_route(target, *, deadline):
                        routes.append(target)
                        self.assertAlmostEqual(deadline, 10.02)
                        self.assertAlmostEqual(deadline - now[0], 0.01)
                        now[0] = 10.02 if stage == "route_expired" else now[0] + 0.002
                        return {"host": "127.0.0.1", "control_port": 45678}

                    def prepare_provider(target, topic, request_capacity, response_capacity,
                                         timeout_ms, result_ready, *, _deadline):
                        preparations.append((target, topic))
                        self.assertAlmostEqual(_deadline, 10.02)
                        self.assertAlmostEqual(_deadline - now[0], 0.008)
                        if stage == "provider_expired":
                            # A late successful preflight must not publish readiness.
                            now[0] = 10.02
                        return True

                    runtime._client = SimpleNamespace(
                        resolve_control_target=resolve_route,
                        connect_control=lambda **kwargs: None,
                        prepare_control=prepare_provider,
                        transact_control=lambda **kwargs: callbacks.append(kwargs),
                    )
                    self.subsystem._runtime = runtime
                    with patch("r2kernel.wrapper.subsystem.time.monotonic", side_effect=lambda: now[0]), \
                         patch("r2kernel.wrapper.subsystem.exchange_control_request",
                               side_effect=resolve_binding) as exchange, \
                         patch.object(self.subsystem, "_resolve_runtime_binding",
                                      side_effect=AssertionError("legacy binding fallback")):
                        invoke = lambda: _prepare_bound(self.subsystem, self.requirement, entrypoint,
                                                        "read", 8, 16, timeout_ms=20)
                        if stage == "success":
                            self.assertTrue(invoke())
                        else:
                            expected = RuntimeError if stage == "binding_missing" else TimeoutError
                            with self.assertRaises(expected):
                                invoke()
                    exchange.assert_called_once()
                    self.assertEqual(len(routes), 0 if stage.startswith("binding_") else 1)
                    self.assertEqual(len(preparations),
                                     1 if stage in ("success", "provider_expired") else 0)
                    self.assertEqual(callbacks, [])
                    self.assertEqual(bool(getattr(runtime, "_prepared_control_routes", {})),
                                     stage == "success")

    def test_bound_preparation_rejects_invalid_budget_before_lookup(self) -> None:
        self.subsystem._runtime = object.__new__(SubsystemRuntime)
        for entrypoint in ("prepare_control", "prepare_procedure", "prepare_operation"):
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(entrypoint=entrypoint, timeout=timeout), \
                     patch("r2kernel.wrapper.subsystem.exchange_control_request") as exchange:
                    with self.assertRaises(ValueError):
                        _prepare_bound(self.subsystem, self.requirement, entrypoint,
                                       "read", 8, 16, timeout_ms=timeout)
                    exchange.assert_not_called()

    def _binding(
        self,
        *,
        state: str = "bound",
        resolved_target_subsystem: object = "server",
        resolved_provider_interface: object = "service",
    ) -> dict[str, object]:
        return {
            "requirement": "service",
            "source": "client.service",
            "source_subsystem": "client",
            "target": "server.service",
            "target_subsystem": "server",
            "provider_interface": "service",
            "contract": self.contract,
            "required_contract": self.contract,
            "provided_contract": self.contract,
            "resolved_target": "server.service",
            "resolved_target_subsystem": resolved_target_subsystem,
            "resolved_provider_interface": resolved_provider_interface,
            "state": state,
            "runtime": True,
        }

    def _missing_response(
        self,
        reason: str,
        *,
        binding: dict[str, object] | None = None,
        source: str = "client.service",
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": False,
            "res": "missing",
            "source": source,
            "subsystem": "client",
            "requirement": "service",
            "reason": reason,
        }
        if binding is not None:
            response["binding"] = binding
        return response


if __name__ == "__main__":
    unittest.main()
