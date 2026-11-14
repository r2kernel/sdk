"""Provider-side local Control v5 handler connections: persistence, manager proof and authority checks."""
from __future__ import annotations

import socket
import threading
import time

from r2kernel.core._control_shm import (
    decode_error_response,
    decode_handler_response,
    receive_control_shm_frame,
    send_control_shm_frame,
)
from r2kernel.core._control_shm_region import ControlShmRegion
from r2kernel.core.control_cost_metrics import ControlCostMetrics
from r2kernel.core.native_control import NativeControlClient
from control_shm_support import _install_provider_shm_state, _push_string


def _provider(*, auth_required: bool, identity: str) -> NativeControlClient:
    client = object.__new__(NativeControlClient)
    client._managed_peer_auth_required = auth_required
    client._managed_peer_auth_socket = "/run/root-proof.sock"
    client._server_stop = threading.Event()
    client._control_cost_metrics = ControlCostMetrics(enabled=False)
    client._identity = identity
    client._server_connection_threads = set()
    client._server_connection_threads_lock = threading.Lock()
    _install_provider_shm_state(client)
    client._now_ns = time.time_ns
    return client


def _handler_request(lease: dict, *, topic: str, sender: str) -> bytes:
    output = bytearray(b"SRLC\x05\x0b")
    _push_string(output, sender)
    _push_string(output, topic)
    output.extend(int(time.time_ns()).to_bytes(8, "little", signed=True))
    output.extend(bytes(8))  # period_ms / deadline_ms (float32 zero)
    output.extend((2_000_000_000).to_bytes(8, "little"))
    output.append(1)
    _push_string(output, str(lease["region_name"]))
    for field in ("slot_index", "slot_size", "slot_count", "length"):
        output.extend(int(lease[field]).to_bytes(4, "little"))
    return bytes(output)


class _Caller:
    """Plays Control: sends handler requests on one persistent provider connection."""

    def __init__(self, provider: NativeControlClient):
        self.provider = provider
        self.peer, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.peer.settimeout(2.0)
        self.request_owner = ControlShmRegion.create(4096)
        self.response_mapping: ControlShmRegion | None = None
        self.descriptor_sent = False
        self.generation = 0
        self.worker = threading.Thread(target=provider._local_connection_loop, args=(server,), daemon=True)
        provider._server_connection_threads.add(self.worker)
        self.worker.start()

    def call(self, payload: bytes, *, topic: str = "body_motion", sender: str = "test.sender") -> bytes | str:
        self.generation += 1
        lease = self.request_owner.write_payload(payload, self.generation)
        frame = _handler_request(lease, topic=topic, sender=sender)
        send_control_shm_frame(self.peer, frame, None if self.descriptor_sent else self.request_owner.file_descriptor)
        self.descriptor_sent = True
        response, descriptors = receive_control_shm_frame(self.peer)
        if response[5] == 0xFF:
            return decode_error_response(response)
        decoded = decode_handler_response(response)
        if descriptors:
            self.response_mapping = ControlShmRegion.from_received_descriptor(
                decoded["payload_lease"]["region_name"], decoded["payload_lease"]["slot_size"], descriptors.pop(),
            )
        assert self.response_mapping is not None
        return self.response_mapping.read_payload(decoded["payload_lease"])

    def close(self) -> None:
        self.provider._server_stop.set()
        self.peer.close()
        self.worker.join(timeout=2.0)
        if self.response_mapping is not None:
            self.response_mapping.close()
        self.request_owner.close()
        assert not self.worker.is_alive()


def test_persistent_handler_survives_idle_interval() -> None:
    provider = _provider(auth_required=False, identity="test-handler")
    provider._handler = lambda request: {"ts": time.time_ns(), "status": 0, "payload": request["payload"]}
    caller = _Caller(provider)
    try:
        assert caller.call(b"first") == b"first"
        time.sleep(1.15)
        assert caller.call(b"second") == b"second"
    finally:
        caller.close()


def test_manager_connection_is_verified_once_before_any_handler_dispatch(monkeypatch) -> None:
    provider = _provider(auth_required=True, identity="managed-handler")
    proofs, dispatched = [], []
    monkeypatch.setattr("r2kernel.core.resource.verify_manager_peer_connection",
                        lambda connection, path, **_kwargs: proofs.append(path))
    provider._handler = lambda request: dispatched.append(request) or {
        "ts": time.time_ns(), "status": 0, "payload": request["payload"],
    }
    caller = _Caller(provider)
    try:
        for payload in [b"first", b"second"]:
            assert caller.call(payload) == payload
    finally:
        caller.close()
    assert proofs == ["/run/root-proof.sock"]
    assert len(dispatched) == 2
    assert all(request["_manager_peer_verified"] is True for request in dispatched)


def test_unverified_development_handler_rejects_authority_context_before_callback() -> None:
    provider = _provider(auth_required=False, identity="development-handler")
    dispatched = []
    provider._handler = lambda request: dispatched.append(request) or {}
    caller = _Caller(provider)
    try:
        error = caller.call(b"request", sender="__r2kernel_authority_v1__:{}")
    finally:
        caller.close()
    assert "verified manager connection" in error
    assert dispatched == []
