"""Provider-ready preparation over real v5 sockets and descriptor mappings."""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

import pytest

from r2kernel.core import _control_shm as wire
from r2kernel.core import resource
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmCapacityError, ControlShmRegion
from r2kernel.core._control_shm_transport import ControlShmChannelRegistry, ControlShmCounters
from r2kernel.core.control_cost_metrics import ControlCostMetrics
from r2kernel.core.control_source_topic import encode_source_topic
from r2kernel.core.native_control import NativeControlClient
from control_shm_support import _install_provider_shm_state
from test_control_shm import _request_lease, _test_policy, _transact_response_frame


def string(value):
    encoded = value.encode()
    return struct.pack("<I", len(encoded)) + encoded


def lease_bytes(lease):
    output = bytearray()
    wire._push_lease(output, lease)
    return bytes(output)


class PreparedPeer:
    """Test intermediary forwards actual provider replies, never echoes a request."""

    def __init__(self, tmp_path, monkeypatch, *, response_capacity=16):
        self.errors = []
        self.calls = []
        self.proofs = []
        self.policy = _test_policy()
        self.caller_budget = ControlShmBudget(self.policy)
        self.registry = ControlShmChannelRegistry(self.policy, self.caller_budget, ControlShmCounters())
        self.endpoint = str(tmp_path / "prepare.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.endpoint)
        self.listener.listen(1)
        self.client = object.__new__(NativeControlClient)
        self.client._logger = None
        self.client._identity = "caller"
        self.client._control_shm_negotiated = True
        self.client._control_shm_channel_registry = self.registry
        self.client._local_control_endpoint = self.endpoint
        self.client._routes = {"gateway": ("local_control", 0)}
        self.client._ready = {}
        self.client._last_transport_error = None
        self.provider = object.__new__(NativeControlClient)
        self.provider._managed_peer_auth_required = True
        self.provider._managed_peer_auth_socket = "test-proof"
        self.provider._server_stop = threading.Event()
        self.provider._server_connection_threads = set()
        self.provider._server_connection_threads_lock = threading.Lock()
        _install_provider_shm_state(self.provider, self.policy)
        self.provider._control_cost_metrics = ControlCostMetrics(enabled=False)
        self.provider._now_ns = time.time_ns
        self.provider._handler = lambda request: self.calls.append(request) or {
            "status": 0, "ts": 123, "payload": b"provider-result"[:response_capacity],
        }
        monkeypatch.setattr(resource, "verify_control_server_connection",
                            lambda connection, *, deadline=None: self.proofs.append(("caller", deadline)))
        monkeypatch.setattr(resource, "verify_manager_peer_connection",
                            lambda connection, path, **kwargs: self.proofs.append(("provider", path)))
        self.manager_peer, provider_socket = socket.socketpair()
        self.manager_peer.settimeout(2)
        self.provider_worker = threading.Thread(target=self.provider._local_connection_loop,
                                                args=(provider_socket,), daemon=True)
        self.provider._server_connection_threads.add(self.provider_worker)
        self.provider_worker.start()
        self.manager_worker = threading.Thread(target=self.serve, daemon=True)
        self.manager_worker.start()

    def serve(self):
        try:
            with self.listener.accept()[0] as connection:
                connection.settimeout(2)
                frame, descriptors = wire.receive_control_shm_frame(connection)
                assert frame[:6] == b"SRLC\x05\x07" and len(descriptors) == 1
                target, position = wire._read_string(frame, 6)
                host, position = wire._read_string(frame, position)
                port, position = wire._read_u16(frame, position)
                topic, position = wire._read_string(frame, position)
                timeout, position = wire._read_u32(frame, position)
                request_capacity, position = wire._read_u32(frame, position)
                response_capacity, position = wire._read_u32(frame, position)
                lease, position = wire._read_lease(frame, position)
                assert (target, host, port) == ("gateway", "local_control", 0)
                assert topic == encode_source_topic("caller", "control")
                assert timeout > 0 and position == len(frame) and lease["length"] == 0
                provider_frame = (b"SRLC\x05\x0d" + string("caller") + string("control") +
                                  struct.pack("<IIQ", request_capacity, response_capacity, timeout * 1_000_000) +
                                  lease_bytes(lease))
                try:
                    wire.send_control_shm_frame(self.manager_peer, provider_frame, descriptors[0])
                finally:
                    wire.close_control_shm_descriptors(descriptors)
                response, descriptors = wire.receive_control_shm_frame(self.manager_peer)
                assert response[:6] == b"SRLC\x05\x0e" and len(descriptors) == 1
                response_lease, position = wire._read_lease(response, 6)
                assert position == len(response) and response_lease["length"] == 0
                try:
                    wire.send_control_shm_frame(connection,
                        b"SRLC\x05\x08\x01" + string("") + lease_bytes(response_lease), descriptors[0])
                finally:
                    wire.close_control_shm_descriptors(descriptors)
                ticket = 1
                while True:
                    frame, descriptors = wire.receive_control_shm_frame(connection)
                    if frame is None:
                        return
                    assert descriptors == []
                    lease = _request_lease(frame)
                    # Preserve application timing metadata, omit caller routing and remaining_ns.
                    _, position = wire._read_string(frame, 6)
                    _, position = wire._read_string(frame, position)
                    position += 2
                    _, position = wire._read_string(frame, position)
                    timing = frame[position:position + 16]
                    remaining_ns = struct.unpack_from("<Q", frame, position + 21)[0]
                    assert remaining_ns > 0
                    handler = (b"SRLC\x05\x0b" + string("caller") + string("control") + timing +
                               struct.pack("<Q", remaining_ns) + lease_bytes(lease))
                    wire.send_control_shm_frame(self.manager_peer, handler)
                    response, descriptors = wire.receive_control_shm_frame(self.manager_peer)
                    assert response[:6] == b"SRLC\x05\x0c" and descriptors == []
                    status, position = wire._read_u16(response, 6)
                    timestamp, position = wire._read_i64(response, position)
                    response_lease, position = wire._read_lease(response, position)
                    assert position == len(response)
                    wire.send_control_shm_frame(connection, _transact_response_frame(
                        response_lease, status=status, timestamp_ns=timestamp, response_ticket=ticket))
                    consumed, descriptors = wire.receive_control_shm_frame(connection)
                    assert descriptors == []
                    consumed_ticket, consumed_lease = wire.decode_response_consumed(consumed)
                    assert (consumed_ticket, consumed_lease) == (ticket, response_lease)
                    wire.send_control_shm_frame(connection, wire.encode_response_released(ticket))
                    ticket += 1
        except BaseException as error:
            self.errors.append(error)

    def close(self):
        self.registry.close_all()
        self.listener.close()
        self.manager_worker.join(2)
        self.manager_peer.close()
        self.provider_worker.join(2)
        assert not self.manager_worker.is_alive() and not self.provider_worker.is_alive()
        assert self.errors == []
        for budget in (self.caller_budget, self.provider._control_shm_budget):
            assert budget.snapshot()["channels"] == 0
            assert budget.snapshot()["owned_bytes"] == budget.snapshot()["mapped_bytes"] == 0


@pytest.mark.parametrize("result_ready", [False, True])
def test_prepare_maps_provider_before_first_tx_and_preserves_owned_payloads(tmp_path, monkeypatch, result_ready):
    peer = PreparedPeer(tmp_path, monkeypatch)
    try:
        assert peer.client.prepare_control("gateway", "control", 8, 16, 1000, result_ready)
        channel = peer.registry.find(peer.endpoint, "gateway", "control")
        assert channel._outgoing_region is not None and channel._incoming_region is not None
        assert channel._next_response_ticket == 1 and peer.calls == []
        assert (channel._reclaimer is not None) is result_ready
        before = peer.registry.counters.snapshot()
        provider_before = peer.provider._control_shm_counters.snapshot()
        proofs = list(peer.proofs)
        responses = []
        for payload in (b"first", b"second"):
            response = peer.client._managed_transact_request(
                target="gateway", topic="control", payload=payload, timestamp=7,
                period_ms=1, deadline_ms=1000, priority=None, result_ready=result_ready)
            responses.append(response["payload"])
            peer.client.wait_control_reclaimed("gateway", "control")
        assert responses == [b"provider-result", b"provider-result"]
        assert all(isinstance(payload, bytes) for payload in responses)
        assert [request["payload"] for request in peer.calls] == [b"first", b"second"]
        assert peer.proofs == proofs and len(proofs) == 2
        after = peer.registry.counters.snapshot()
        provider_after = peer.provider._control_shm_counters.snapshot()
        for key in ("request_region_creates", "request_region_grows", "received_mapping_opens", "sockets_opened", "fd_sent"):
            assert after[key] == before[key]
        for key in ("response_region_creates", "response_region_grows", "received_mapping_opens"):
            assert provider_after[key] == provider_before[key]
        peer.client.connect("gateway", "127.0.0.1", 1234)
        assert channel.is_closed and not peer.registry._prepared_routes
    finally:
        peer.close()


def test_prepared_capacity_rejects_page_padding_before_callback(tmp_path, monkeypatch):
    peer = PreparedPeer(tmp_path, monkeypatch)
    try:
        assert peer.client.prepare_control("gateway", "control", 4, 16)
        with pytest.raises(ControlShmCapacityError, match="prepared payload capacity"):
            peer.client._managed_transact_request(
                target="gateway", topic="control", payload=b"12345", timestamp=7,
                period_ms=1, deadline_ms=1000, priority=None)
        assert peer.calls == []
        with pytest.raises(wire.ControlShmProtocolError, match="invalid"):
            peer.registry.get_route(peer.endpoint, "gateway", "control", lambda: ("local_control", 0))
    finally:
        peer.close()


def test_failed_memory_lock_preparation_has_no_callback_or_live_lane(tmp_path, monkeypatch):
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    registry = ControlShmChannelRegistry(policy, budget, ControlShmCounters())
    client = object.__new__(NativeControlClient)
    client._identity = "caller"
    client._control_shm_negotiated = True
    client._control_shm_channel_registry = registry
    client._local_control_endpoint = "unused"
    client._routes = {"gateway": ("local_control", 0)}
    client._ready = {}
    def fail_lock(region):
        raise PermissionError("memory locking denied")
    monkeypatch.setattr(ControlShmRegion, "prepare_memory", fail_lock)
    try:
        with pytest.raises(PermissionError, match="memory locking denied"):
            client.prepare_control("gateway", "control", 8, 16)
        assert client._ready["gateway"] is False
        assert not registry._pending_preparations and not registry._channels
        assert budget.snapshot()["channels"] == budget.snapshot()["owned_bytes"] == 0
    finally:
        registry.close_all()


def test_control_worker_profile_applies_to_actual_handler_tid(monkeypatch):
    provider = object.__new__(NativeControlClient)
    provider._server_connection_threads_lock = threading.Lock()
    provider._server_connection_threads = set()
    cpus = sorted(os.sched_getaffinity(0))
    provider.configure_control_worker("OTHER", 0, cpus)
    worker = threading.Thread(target=provider._configure_current_control_worker)
    worker.start()
    worker.join(2)
    status = provider.control_worker_status()
    assert len(status) == 1 and status[0]["tid"] == worker.native_id
    assert status[0]["policy"] == os.SCHED_OTHER and status[0]["priority"] == 0
    assert status[0]["cpus"] == cpus and status[0]["ready"] is True


@pytest.mark.parametrize("expired_stage", ["encode", "decode"])
def test_typed_caller_uses_one_deadline_through_codec_work(tmp_path, monkeypatch, expired_stage):
    from r2kernel.core.r2kernel_client import R2KernelClient
    from r2kernel.core.subsystem_runtime import SubsystemRuntime
    from types import SimpleNamespace

    peer = PreparedPeer(tmp_path, monkeypatch)
    try:
        assert peer.client.prepare_control("gateway", "control", 8, 16)
        peer.client._control_cost_metrics = ControlCostMetrics(enabled=False)
        peer.client._get_encoder = lambda dtype: lambda value: value
        peer.client._get_decoder = lambda dtype: lambda value: value
        peer.client._now_ns = time.time_ns
        peer.client._pending_cost_context = {}
        client = object.__new__(R2KernelClient)
        client._control = peer.client
        runtime = object.__new__(SubsystemRuntime)
        runtime._client = client
        now = [time.monotonic()]
        monkeypatch.setattr("r2kernel.core._runtime_control.time",
                            SimpleNamespace(monotonic=lambda: now[0], time_ns=time.time_ns))

        class Codec:
            def encode_message(self, reference, value):
                if expired_stage == "encode":
                    now[0] += 2.0
                return b"request"

            def decode_message(self, reference, *, payload):
                if expired_stage == "decode":
                    now[0] += 2.0
                return {"payload": payload}

        runtime._get_rust_message_codec = lambda: Codec()
        with pytest.raises(TimeoutError, match="deadline"):
            runtime._transact_typed_control(
                target="gateway", topic="control", request_type="request", response_type="response",
                payload={}, period_ms=1, deadline_ms=1000,
            )
        assert len(peer.calls) == (0 if expired_stage == "encode" else 1)
    finally:
        peer.close()


def test_ordinary_handler_proof_uses_wire_remaining_before_any_mapping(monkeypatch):
    from r2kernel.core._control_shm_server import ControlShmServerConnection

    policy = _test_policy()
    budget = ControlShmBudget(policy)
    server = ControlShmServerConnection(policy, budget, ControlShmCounters())
    owner = ControlShmRegion.create(policy.minimum_region_bytes)
    sender, receiver = socket.socketpair()
    sender.settimeout(1)
    receiver.settimeout(1)
    lease = owner.write_payload(b"request", 1)
    frame = (b"SRLC\x05\x0b" + string("caller") + string("control") +
             struct.pack("<qffQ", 1, 1, 0.5, 500_000) + lease_bytes(lease))
    wire.send_control_shm_frame(sender, frame, owner.file_descriptor)
    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    proofs = []
    def proof(deadline):
        proofs.append(deadline)
        assert budget.snapshot()["mapped_bytes"] == budget.snapshot()["owned_bytes"] == 0
        now[0] += 0.001
        raise socket.timeout("proof expired")
    try:
        with pytest.raises(wire.ControlShmProtocolError, match="manager proof exceeded"):
            server.receive_request(receiver, authenticate=proof)
        assert proofs == [pytest.approx(10.0005)]
        assert server._request_region is None and server._response_region is None
    finally:
        server.close()
        owner.close()
        sender.close()
        receiver.close()
    assert budget.snapshot()["channels"] == budget.snapshot()["mapped_bytes"] == 0


def test_worker_profile_failure_is_visible_before_proof_or_callback(monkeypatch):
    provider = object.__new__(NativeControlClient)
    provider._server_connection_threads_lock = threading.Lock()
    provider._server_connection_threads = set()
    provider.configure_control_worker("OTHER", 0, sorted(os.sched_getaffinity(0)))
    def deny_affinity(tid, cpus):
        raise PermissionError("worker affinity denied")
    monkeypatch.setattr(os, "sched_setaffinity", deny_affinity)
    sender, receiver = socket.socketpair()
    sender.settimeout(1)
    worker = threading.Thread(target=provider._local_connection_loop, args=(receiver,))
    provider._server_connection_threads.add(worker)
    worker.start()
    try:
        frame, descriptors = wire.receive_control_shm_frame(sender)
        assert descriptors == []
        assert "worker affinity denied" in wire.decode_error_response(frame)
        worker.join(2)
        assert not worker.is_alive()
        status = provider.control_worker_status()
        assert status[0]["ready"] is False and status[0]["phase"] == "profile_failed"
        assert status[0]["tid"] == worker.native_id
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("entrypoint", ["control", "procedure", "operation", "transact"])
def test_stalled_route_resolution_uses_one_cold_or_exchange_deadline(tmp_path, monkeypatch, entrypoint):
    """Real request publication; deterministic stalled-read deadline, not an RT benchmark."""
    import json
    from r2kernel.core import control_client
    from r2kernel.core.event_client import EventClient
    from r2kernel.core.r2kernel_client import R2KernelClient
    from r2kernel.core.subsystem_runtime import SubsystemRuntime

    endpoint = str(tmp_path / "resolve.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    listener.settimeout(2)
    published = threading.Event()
    stop = threading.Event()
    requests, errors = [], []
    def stalled_resolver():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                body = bytearray()
                while True:
                    chunk = connection.recv(8192)
                    if not chunk:
                        break
                    body.extend(chunk)
                requests.append(json.loads(body))
                published.set()
                stop.wait(2)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=stalled_resolver)
    worker.start()
    monkeypatch.setenv("R2KERNEL_CONTROL_SOCKET", endpoint)
    now = [10.0]
    waits, proofs, attempts = [], [], []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    def no_response(readable, writable, exceptional, timeout):
        assert readable and not writable
        waits.append(timeout)
        now[0] += timeout
        return [], [], []
    monkeypatch.setattr(control_client.select, "select", no_response)
    monkeypatch.setattr(resource, "verify_control_server_connection",
                        lambda connection, *, deadline=None: proofs.append(deadline))
    original_exchange = control_client._exchange_unix
    def counted_exchange(payload, **kwargs):
        attempts.append(kwargs)
        return original_exchange(payload, **kwargs)
    monkeypatch.setattr(control_client, "_exchange_unix", counted_exchange)
    transport = object.__new__(NativeControlClient)
    client = object.__new__(R2KernelClient)
    client._event = object.__new__(EventClient)
    client._control = transport
    runtime = object.__new__(SubsystemRuntime)
    runtime._client = client
    runtime._control_target_cache = {}
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            if entrypoint == "control":
                runtime.prepare_control("gateway", "control", 8, 16, timeout_ms=20)
            elif entrypoint == "procedure":
                runtime.prepare_procedure("gateway", "read", 8, 16, timeout_ms=20)
            elif entrypoint == "operation":
                runtime.prepare_operation("gateway", "move", 8, 16, timeout_ms=20)
            else:
                runtime.transact_control(target="gateway", topic="control", type="bytes",
                                         value=b"request", period_ms=1, deadline_ms=20)
        assert published.wait(2)
        assert len(attempts) == len(requests) == len(proofs) == len(waits) == 1
        assert attempts[0]["deadline"] == proofs[0] == pytest.approx(10.02)
        assert attempts[0]["timeout_s"] == waits[0] == pytest.approx(0.02)
        assert runtime._control_target_cache == {}
    finally:
        stop.set()
        worker.join(2)
        listener.close()
    assert not worker.is_alive() and errors == []
