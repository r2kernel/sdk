from __future__ import annotations

import threading
import json
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from r2kernel.core.subsystem_runtime import SubsystemRuntime
from r2kernel.core.codec_lifecycle import CodecLifecycle
from r2kernel.core.native_control import NativeControlClient
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmPolicy
from r2kernel.core._control_shm_transport import ControlShmChannelRegistry, ControlShmCounters
from r2kernel.manifest import BindingReference, InterfaceDeclaration, SubsystemManifest


class _FakeClient:
    def __init__(self, route: dict) -> None:
        self.route = dict(route) if route is not None else None
        self.connects: list[tuple[str, str, int]] = []
        self.transacts: list[dict] = []
        self.ensure_registered_calls = 0

    def resolve_control_target(self, target: str) -> dict | None:
        if self.route is None:
            return None
        route = dict(self.route)
        route["subsystem"] = target
        return route

    def connect_control(self, name: str, host: str, port: int) -> None:
        self.connects.append((name, host, port))

    def transact_control(self, **kwargs):
        self.transacts.append(dict(kwargs))
        return {"ok": True, "response": {"payload": b""}}

    def ensure_control_server_registered(self) -> bool:
        self.ensure_registered_calls += 1
        return True


class _FakeLogger:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass


def _runtime_with_client(client: _FakeClient) -> SubsystemRuntime:
    runtime = object.__new__(SubsystemRuntime)
    runtime._subsystem_name = "component.mobile"
    runtime._client = client
    runtime._control_target_cache = {}
    runtime._rust_message_codec = None
    runtime._codec_lifecycle = CodecLifecycle(runtime)
    runtime._logger = _FakeLogger()
    runtime._control_dispatch_running = False
    runtime._control_registration_check_interval_s = 5.0
    runtime._next_control_registration_check_at = 0.0
    runtime._timestamp_getter = lambda: 1_000_000_000
    return runtime


def _required_binding_manifest() -> SubsystemManifest:
    return SubsystemManifest(
        path=Path("subsystem.yaml"),
        kind="r2kernel.subsystem",
        version=1,
        name="component.mobile",
        role="component",
        requires={
            "service": InterfaceDeclaration(
                name="service",
                contract="tutorial.transport.procedure.v1",
                required=True,
            )
        },
    )


class _PropertyBindingsOwner:
    def __init__(self, launch_bindings) -> None:
        self.subsystem_manifest = _required_binding_manifest()
        self.deployment_manifest = None
        self._launch_bindings = launch_bindings

    @property
    def launch_bindings(self):
        return self._launch_bindings


class _CallableBindingsOwner(_PropertyBindingsOwner):
    @property
    def launch_bindings(self):
        return self._get_launch_bindings

    def _get_launch_bindings(self):
        return self._launch_bindings


def _runtime_with_owner(owner) -> SubsystemRuntime:
    runtime = object.__new__(SubsystemRuntime)
    runtime._owner = owner
    runtime._logger = _FakeLogger()
    return runtime


class SubsystemRuntimeControlTests(unittest.TestCase):
    def test_manifest_status_accepts_read_only_launch_bindings_property_and_snapshots_it(self) -> None:
        source_bindings = {"service": BindingReference("server", "service")}
        owner = _PropertyBindingsOwner(MappingProxyType(source_bindings))
        runtime = _runtime_with_owner(owner)

        from r2kernel.manifest import subsystem_manifest_status_payload

        with patch(
            "r2kernel.core.subsystem_runtime.subsystem_manifest_status_payload",
            wraps=subsystem_manifest_status_payload,
        ) as build_status:
            payload = runtime._manifest_status_payload()

        snapshot = build_status.call_args.args[2]
        self.assertIs(type(snapshot), dict)
        self.assertIsNot(snapshot, owner.launch_bindings)
        self.assertEqual(snapshot, source_bindings)
        self.assertEqual(payload["bindings"][0]["target"], "server.service")
        self.assertNotIn("binding_errors", payload)

    def test_manifest_status_keeps_callable_launch_bindings_compatibility(self) -> None:
        owner = _CallableBindingsOwner({"service": BindingReference("server", "service")})
        runtime = _runtime_with_owner(owner)

        payload = runtime._manifest_status_payload()

        self.assertEqual(payload["bindings"][0]["target"], "server.service")
        self.assertNotIn("binding_errors", payload)

    def test_manifest_status_reports_missing_required_binding(self) -> None:
        runtime = _runtime_with_owner(_PropertyBindingsOwner({}))

        payload = runtime._manifest_status_payload()

        self.assertEqual(
            payload["binding_errors"],
            ["Required binding is missing: component.mobile.service"],
        )
        self.assertEqual(payload["lifecycle"], "WaitingBinding")

    def test_manifest_status_rejects_invalid_launch_bindings_value(self) -> None:
        runtime = _runtime_with_owner(_PropertyBindingsOwner(["server.service"]))

        with self.assertRaisesRegex(TypeError, "launch_bindings must be a Mapping"):
            runtime._manifest_status_payload()

    def test_manifest_status_rejects_invalid_launch_binding_entry(self) -> None:
        runtime = _runtime_with_owner(_PropertyBindingsOwner({"service": "server.service"}))

        with self.assertRaisesRegex(TypeError, "BindingReference values"):
            runtime._manifest_status_payload()

    def test_control_context_uses_transport_sender_as_source_identity(self) -> None:
        runtime = _runtime_with_client(_FakeClient(None))
        runtime._control_endpoint_handlers = {}
        runtime._raw_control_endpoint_handlers = {}
        runtime._control_contracts = {}
        runtime._procedure_handler_context = threading.local()
        runtime._timestamp_getter = lambda: 100
        runtime._decode_optional_json_bytes = lambda payload: payload

        class FakeCodec:
            def decode_control_request_wire(self, _payload):
                return (
                    {
                        "request_id_u64": 4,
                        "generated_at_sync_ns": 90,
                        "sent_at_sync_ns": 95,
                        "execute_at_sync_ns": 0,
                        "valid_until_sync_ns": 150,
                        "priority": 3,
                        "late_policy": "reject",
                        "max_lateness_ns": 0,
                    },
                    b"typed-request",
                )

            def decode_message(self, _message_type, *, payload):
                self.assert_payload = payload
                return {"cycle": "request"}

        runtime._get_rust_message_codec = lambda: FakeCodec()
        seen_sources = []
        seen_authorities = []

        def handler(_payload, context):
            seen_sources.append(context.source_subsystem_name)
            seen_authorities.append(context.source_authority)
            return {"accepted": True}

        runtime._control_endpoint_handlers["velocity_command"] = handler
        runtime._control_contracts["velocity_command"] = {
            "request_type": "msg.example/Request",
            "timing_support": "required",
        }

        with patch("r2kernel.core._runtime_dispatch._is_control_wire", return_value=True):
            response = runtime._handle_registered_control_request(
                {
                    "topic": "velocity_command",
                    "sender": "component.owner",
                    "period_ms": 10.0,
                    "deadline_ms": 50.0,
                    "payload": b"compact-wire",
                }
            )

        self.assertEqual(response["status"], 0)
        self.assertEqual(seen_sources, ["component.owner"])
        authority = {"peer":"compute", "epoch":"epoch-a", "source":"component.owner", "source_session":"session-a",
            "source_registration":"source-reg-a", "source_reference":"component.owner.motion", "target":"gateway.device",
            "target_session":"target-a", "target_registration":"target-reg-a", "interface":"servo", "binding_revision":"binding-a",
            "topic":"velocity_command"}
        with patch("r2kernel.core._runtime_dispatch._is_control_wire", return_value=True):
            runtime._control_server_enabled = True
            response = runtime._dispatch_control_request({"topic":"velocity_command",
                "sender":"__r2kernel_authority_v1__:" + json.dumps(authority), "_manager_peer_verified":True, "period_ms":10.0,"deadline_ms":50.0,"payload":b"compact-wire"})
        self.assertEqual(response["status"],0)
        self.assertEqual(seen_sources,["component.owner","component.owner"])
        self.assertEqual(seen_authorities,[None,authority])

    def test_prepare_allows_runtime_procedure_transport_topics(self) -> None:
        client = _FakeClient(
            {
                "host": "127.0.0.1",
                "control_port": 41001,
                "controls": ["servo_control"],
                "procedures": ["velocity_command"],
                "operations": [],
            }
        )
        runtime = _runtime_with_client(client)

        runtime._prepare_control_target("component.mobile", control="procedure.call")
        runtime._prepare_control_target("component.mobile", control="procedure.call_typed/velocity_command")

        self.assertEqual(client.connects, [("component.mobile", "127.0.0.1", 41001)] * 2)

    def test_transact_control_resolves_and_connects_target(self) -> None:
        client = _FakeClient(
            {
                "host": "127.0.0.1",
                "control_port": 41001,
                "controls": ["velocity_command"],
                "procedures": [],
                "operations": [],
            }
        )
        runtime = _runtime_with_client(client)

        result = runtime._transact_control_packet(
            target="component.mobile",
            topic="velocity_command",
            type="bytes",
            value=b"payload",
            period_ms=20.0,
            deadline_ms=1000.0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(client.connects, [("component.mobile", "127.0.0.1", 41001)])
        self.assertEqual(client.transacts[0]["target"], "component.mobile")
        self.assertEqual(client.transacts[0]["topic"], "velocity_command")

    def test_transact_control_rejects_unadvertised_control(self) -> None:
        client = _FakeClient(
            {
                "host": "127.0.0.1",
                "control_port": 41001,
                "controls": ["velocity_command"],
                "procedures": [],
                "operations": [],
            }
        )
        runtime = _runtime_with_client(client)

        with self.assertRaisesRegex(RuntimeError, "not advertised"):
            runtime._transact_control_packet(
                target="component.mobile",
                topic="servo_control",
                type="bytes",
                value=b"payload",
                period_ms=20.0,
                deadline_ms=1000.0,
            )

    def test_transact_control_uses_timed_wire_when_required(self) -> None:
        client = _FakeClient(
            {
                "host": "127.0.0.1",
                "control_port": 41001,
                "controls": ["velocity_command"],
                "control_contracts": {
                    "velocity_command": {
                        "request_type": "msg.locomotion.velocity.v1/VelocityCommand",
                        "response_type": "msg.locomotion.velocity.v1/CommandAck",
                        "timing_support": "required",
                    }
                },
                "procedures": [],
                "operations": [],
            }
        )
        runtime = _runtime_with_client(client)
        timed_calls: list[dict] = []

        def fake_typed_control(**kwargs):
            timed_calls.append(dict(kwargs))
            return {"ok": True, "timed": True}

        runtime._transact_typed_control = fake_typed_control

        result = runtime._transact_control_packet(
            target="component.mobile",
            topic="velocity_command",
            type="bytes",
            value={"linear": 0.1},
            period_ms=20.0,
            deadline_ms=1000.0,
            request_type="msg.locomotion.velocity.v1/VelocityCommand",
            response_type="msg.locomotion.velocity.v1/CommandAck",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["timed"])
        self.assertEqual(client.connects, [("component.mobile", "127.0.0.1", 41001)])
        self.assertEqual(timed_calls[0]["target"], "component.mobile")
        self.assertEqual(timed_calls[0]["topic"], "velocity_command")

    def test_timed_control_forwards_generation_and_validity_metadata(self) -> None:
        client = _FakeClient(
            {
                "host": "127.0.0.1",
                "control_port": 41001,
                "controls": ["velocity_command"],
                "control_contracts": {
                    "velocity_command": {
                        "request_type": "msg.locomotion.velocity.v1/VelocityCommand",
                        "response_type": "msg.locomotion.velocity.v1/CommandAck",
                        "timing_support": "required",
                    }
                },
                "procedures": [],
                "operations": [],
            }
        )
        runtime = _runtime_with_client(client)
        timed_calls: list[dict] = []

        runtime._transact_control = lambda **kwargs: timed_calls.append(dict(kwargs)) or {"ok": True}

        runtime._transact_control_packet(
            target="component.mobile",
            topic="velocity_command",
            type="bytes",
            value={"linear": 0.1},
            period_ms=20.0,
            deadline_ms=50.0,
            request_type="msg.locomotion.velocity.v1/VelocityCommand",
            response_type="msg.locomotion.velocity.v1/CommandAck",
            timing={
                "generated_at_sync_ns": 900_000_000,
                "valid_for_ns": 25_000_000,
                "late_policy": "reject",
            },
        )

        self.assertEqual(timed_calls[0]["generated_at_sync_ns"], 900_000_000)
        self.assertEqual(timed_calls[0]["valid_for_ns"], 25_000_000)
        self.assertEqual(timed_calls[0]["late_policy"], "reject")

    def test_build_timing_header_resolves_relative_validity(self) -> None:
        runtime = _runtime_with_client(_FakeClient(None))

        header = runtime.build_timing_request_header(
            target_subsystem="component.mobile",
            generated_at_sync_ns=900_000_000,
            valid_for_ns=25_000_000,
        )

        self.assertEqual(header["generated_at_sync_ns"], 900_000_000)
        self.assertEqual(header["sent_at_sync_ns"], 1_000_000_000)
        self.assertEqual(header["execution"]["valid_until_sync_ns"], 925_000_000)

        header_without_generated_timestamp = runtime.build_timing_request_header(
            target_subsystem="component.mobile",
            valid_for_ns=25_000_000,
        )
        self.assertEqual(
            header_without_generated_timestamp["execution"]["valid_until_sync_ns"],
            1_025_000_000,
        )

    def test_control_rejects_relative_validity_expired_during_codec_preparation(self) -> None:
        class Clock:
            current_ns = 1_000

            def now_ns(self) -> int:
                return self.current_ns

            def advance(self, duration_ns: int) -> None:
                self.current_ns += duration_ns

        clock = Clock()
        client = _FakeClient(
            {"host": "127.0.0.1", "control_port": 41001, "controls": ["servo_control"]}
        )
        runtime = _runtime_with_client(client)
        runtime._timestamp_getter = clock.now_ns

        class SlowCodec:
            def encode_message(self, _message_type, _payload):
                clock.advance(70)
                return b"typed-request"

            def encode_control_request_wire(self, **_kwargs):
                return b"timed-wire"

        codec = SlowCodec()

        def get_codec_after_cold_preparation():
            clock.advance(40)
            return codec

        runtime._get_rust_message_codec = get_codec_after_cold_preparation

        result = runtime._transact_control(
            target="actuator.servo",
            topic="servo_control",
            request_type="msg.actuator.servo.v1/ServoControlRequest",
            response_type="msg.actuator.servo.v1/ServoControlResponse",
            payload={"cycle": {"generation": 1, "sequence": 1, "intent": "hold"}},
            period_ms=20.0,
            deadline_ms=50.0,
            last_tick_ns=1_000,
            generated_at_sync_ns=1_000,
            valid_for_ns=100,
            late_policy="reject",
            details=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "expired_timed_request")
        self.assertIn("expired timed request", result["error_message"])
        self.assertEqual(result["user_header"]["sent_at_sync_ns"], 1_110)
        self.assertEqual(result["user_header"]["generated_at_sync_ns"], 1_000)
        self.assertEqual(
            result["normalized_header"]["execution"]["valid_until_sync_ns"],
            1_100,
        )
        self.assertEqual(client.transacts, [])

    def test_control_sends_unexpired_request_with_original_validity(self) -> None:
        class Clock:
            current_ns = 1_000

            def now_ns(self) -> int:
                return self.current_ns

            def advance(self, duration_ns: int) -> None:
                self.current_ns += duration_ns

        clock = Clock()
        client = _FakeClient(
            {"host": "127.0.0.1", "control_port": 41001, "controls": ["servo_control"]}
        )
        runtime = _runtime_with_client(client)
        runtime._timestamp_getter = clock.now_ns
        encoded_request: dict = {}

        class SlowCodec:
            def encode_message(self, _message_type, _payload):
                clock.advance(50)
                return b"typed-request"

            def encode_control_request_wire(self, **kwargs):
                encoded_request.update(kwargs)
                clock.advance(5)
                return b"timed-wire"

        codec = SlowCodec()

        def get_codec_after_cold_preparation():
            clock.advance(40)
            return codec

        runtime._get_rust_message_codec = get_codec_after_cold_preparation

        result = runtime._transact_control(
            target="actuator.servo",
            topic="servo_control",
            request_type="msg.actuator.servo.v1/ServoControlRequest",
            response_type="msg.actuator.servo.v1/ServoControlResponse",
            payload={"cycle": {"generation": 1, "sequence": 1, "intent": "hold"}},
            period_ms=20.0,
            deadline_ms=50.0,
            last_tick_ns=1_000,
            generated_at_sync_ns=1_000,
            valid_for_ns=100,
            late_policy="reject",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(encoded_request["generated_at_sync_ns"], 1_000)
        self.assertEqual(encoded_request["sent_at_sync_ns"], 1_090)
        self.assertEqual(encoded_request["valid_until_sync_ns"], 1_100)
        self.assertEqual(client.transacts[0]["value"], b"timed-wire")

    def test_control_preserves_explicit_absolute_validity(self) -> None:
        runtime = _runtime_with_client(
            _FakeClient(
                {"host": "127.0.0.1", "control_port": 41001, "controls": ["servo_control"]}
            )
        )
        runtime._rust_message_codec = type(
            "Codec",
            (),
            {
                "encode_message": lambda _self, _message_type, _payload: b"typed-request",
                "encode_control_request_wire": lambda _self, **_kwargs: b"timed-wire",
            },
        )()

        result = runtime._transact_control(
            target="actuator.servo",
            topic="servo_control",
            request_type="msg.actuator.servo.v1/ServoControlRequest",
            response_type="msg.actuator.servo.v1/ServoControlResponse",
            payload={"cycle": {"generation": 1, "sequence": 1, "intent": "hold"}},
            period_ms=20.0,
            deadline_ms=50.0,
            last_tick_ns=1_000_000_000,
            generated_at_sync_ns=900_000_000,
            valid_until_sync_ns=950_000_000,
            late_policy="reject",
            details=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "expired_timed_request")
        self.assertEqual(
            result["normalized_header"]["execution"]["valid_until_sync_ns"],
            950_000_000,
        )
        self.assertEqual(runtime._client.transacts, [])

    def test_build_timing_header_rejects_conflicting_validity(self) -> None:
        runtime = _runtime_with_client(_FakeClient(None))

        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            runtime.build_timing_request_header(
                target_subsystem="component.mobile",
                valid_for_ns=25_000_000,
                valid_until_sync_ns=1_100_000_000,
            )

    def test_control_registration_recovery_re_registers_missing_self_route(self) -> None:
        client = _FakeClient(None)
        runtime = _runtime_with_client(client)
        runtime._control_dispatch_running = True

        runtime._maybe_recover_control_registration()

        self.assertEqual(client.ensure_registered_calls, 1)

    def test_control_registration_recovery_is_throttled(self) -> None:
        client = _FakeClient(None)
        runtime = _runtime_with_client(client)
        runtime._control_dispatch_running = True

        runtime._maybe_recover_control_registration()
        runtime._maybe_recover_control_registration()

        self.assertEqual(client.ensure_registered_calls, 1)

    def test_relay_off_route_is_shared_by_preparation_and_registration_recovery(self) -> None:
        client = _FakeClient({"host": "127.0.0.1", "route_kind": "local_control", "control_port": 0})
        transport = object.__new__(NativeControlClient)
        transport._control_shm_negotiated = True
        transport._local_control_endpoint = "/run/control.sock"
        policy = ControlShmPolicy()
        transport._control_shm_channel_registry = ControlShmChannelRegistry(
            policy, ControlShmBudget(policy), ControlShmCounters(),
        )
        transport._routes = {}
        transport._ready = {}
        transport._logger = None
        transport.connect("component.remote", "local_control", 0)
        self.assertEqual(transport._route("component.remote"), ("local_control", 0))
        client._control = transport
        runtime = _runtime_with_client(client)
        runtime._control_dispatch_running = True

        route = runtime._prepare_control_target("component.remote", control="status")
        runtime._maybe_recover_control_registration()

        self.assertEqual(route["host"], "local_control")
        self.assertEqual(client.connects, [("component.remote", "local_control", 0)])
        self.assertEqual(client.ensure_registered_calls, 0)

    def test_unnegotiated_or_generic_zero_port_routes_are_not_endpoints(self) -> None:
        cases = [
            ("local_control", 0, False, "/run/control.sock"),
            ("local_control", 0, True, None),
            ("127.0.0.1", 0, True, "/run/control.sock"),
            ("", 0, True, "/run/control.sock"),
            ("local_control", -1, True, "/run/control.sock"),
            ("local_control", 1234, True, "/run/control.sock"),
        ]
        for host, port, negotiated, endpoint in cases:
            with self.subTest(host=host, port=port, negotiated=negotiated, endpoint=endpoint):
                client = _FakeClient({"host": host, "control_port": port})
                transport = object.__new__(NativeControlClient)
                transport._control_shm_negotiated = negotiated
                transport._local_control_endpoint = endpoint
                client._control = transport
                runtime = _runtime_with_client(client)
                runtime._control_dispatch_running = True

                with self.assertRaisesRegex(RuntimeError, "no Control endpoint"):
                    runtime._prepare_control_target("component.remote")
                with self.assertRaisesRegex(ValueError, "invalid Control route"):
                    transport.connect("component.remote", host, port)
                runtime._maybe_recover_control_registration()

                self.assertEqual(client.connects, [])
                self.assertEqual(client.ensure_registered_calls, 1)

    def test_positive_tcp_route_needs_no_local_transport_negotiation(self) -> None:
        client = _FakeClient({"host": "127.0.0.1", "control_port": 1234})
        runtime = _runtime_with_client(client)
        runtime._control_dispatch_running = True
        runtime._prepare_control_target("component.remote")
        runtime._maybe_recover_control_registration()
        self.assertEqual(client.connects, [("component.remote", "127.0.0.1", 1234)])
        self.assertEqual(client.ensure_registered_calls, 0)

    def test_relay_off_target_does_not_relax_handler_registration_port_contract(self) -> None:
        policy = ControlShmPolicy()
        response = {
            "res": "ok",
            "control_port": 0,
            "control_local_endpoint": "/run/control.sock",
            "control_local_payload_policy": "forwarded_shm_v5",
            "control_local_protocol": "binary_shm_v5",
            "control_shm_min_region_bytes": policy.minimum_region_bytes,
            "control_shm_max_channels": policy.maximum_channels,
            "control_shm_max_owned_bytes": policy.maximum_owned_bytes,
            "control_shm_max_mapped_bytes": policy.maximum_mapped_bytes,
        }
        transport = object.__new__(NativeControlClient)
        transport._control_shm_negotiated = False
        transport._control_shm_policy = policy
        transport._control_shared_buffer_slot_size = policy.maximum_region_bytes
        transport._local_control_endpoint = None
        transport._local_endpoint = "/tmp/test-handler.sock"
        transport._identity = "component.mobile"
        transport._requested_host = "0.0.0.0"
        transport._requested_port = 0
        transport._session_id = "test-session"
        transport._startup_retry_count = 1
        transport._startup_retry_delay_s = 0.0
        transport._exchange_control_request = lambda *_args, **_kwargs: response

        with self.assertRaisesRegex(RuntimeError, "invalid control-managed Control port"):
            transport._register_managed_handler()

        self.assertTrue(transport._route_is_valid("local_control", 0))


class FederationTargetRefreshTest(unittest.TestCase):
    def test_same_name_restart_and_expiry_replace_cached_target_identity(self) -> None:
        class Client:
            def __init__(self): self.routes = iter([{"subsystem":"gateway", "session_id":"session-a"}, {"subsystem":"gateway", "session_id":"session-b"}, None])
            def resolve_control_target(self, _target): return next(self.routes)
        runtime = object.__new__(SubsystemRuntime)
        runtime._client = Client()
        runtime._control_target_cache = {}
        self.assertEqual(runtime._resolve_control_target_cached("gateway")["session_id"],"session-a")
        self.assertEqual(runtime._resolve_control_target_cached("gateway",refresh=True)["session_id"],"session-b")
        runtime._invalidate_control_target("gateway")
        self.assertIsNone(runtime._resolve_control_target_cached("gateway"))
        self.assertNotIn("gateway",runtime._control_target_cache)


class VerifiedTransportContextTest(unittest.TestCase):
    def runtime(self):
        runtime = _runtime_with_client(_FakeClient(None))
        runtime._procedure_handler_context = threading.local()
        runtime._dispatch_control_request_inner = lambda request: {
            "sender":request["sender"],
            "control":getattr(runtime._procedure_handler_context,"authenticated_source",None),
            "host":getattr(runtime._procedure_handler_context,"authenticated_host",None),
        }
        return runtime

    def test_host_context_requires_verified_connection_and_preserves_separate_authority(self):
        runtime = self.runtime()
        authority = {"kind":"protocol","peer":"compute","epoch":"backend-a"}
        request = {"sender":"__r2kernel_host_v1__:"+json.dumps({**authority,"codec_source":""}),"topic":"procedure.call"}
        with self.assertRaisesRegex(ValueError,"verified manager"):
            runtime._dispatch_control_request(request)
        request["_manager_peer_verified"] = True
        result = runtime._dispatch_control_request(request)
        self.assertEqual(result,{"sender":"host:compute","control":None,"host":authority})
        self.assertIsNone(runtime._procedure_handler_context.authenticated_host)
        request["sender"] = "__r2kernel_host_v2__:"+json.dumps(authority)
        with self.assertRaisesRegex(ValueError,"Unsupported"):
            runtime._dispatch_control_request(request)

    def test_async_operation_context_copy_keeps_captured_host_authority(self):
        from r2kernel.core.request_context import RequestContext
        runtime = self.runtime()
        captured = {"peer":"compute","epoch":"backend-a","kind":"protocol"}
        original = RequestContext(kind="operation",name="move",topic="operation.start",accepted_at_sync_ns=1,timed=False,host_authority=captured)
        runtime._procedure_handler_context.authenticated_host = None
        copied = runtime._clone_request_context(original,payload_type="msg.test/Value")
        self.assertEqual(copied.host_authority,captured)

    def test_warm_target_metadata_cache_does_not_issue_rpc_for_each_packet(self):
        class Client:
            calls = 0
            def resolve_control_target(self,target):
                self.calls += 1
                return {"subsystem":target,"session_id":"session-a"}
        runtime = _runtime_with_client(Client())
        with patch("r2kernel.core.subsystem_runtime.time.monotonic",return_value=10):
            for _ in range(1000): runtime._resolve_control_target_cached("gateway")
        self.assertEqual(runtime._client.calls,1)
        runtime._invalidate_control_target("gateway")
        runtime._resolve_control_target_cached("gateway")
        self.assertEqual(runtime._client.calls,2)


if __name__ == "__main__":
    unittest.main()
