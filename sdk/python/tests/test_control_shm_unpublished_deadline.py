"""No-wire deadline cleanup and real descriptor/mapping reuse regressions."""
from __future__ import annotations

from array import array
import os
import socket
import threading

import pytest

from r2kernel.core import _control_shm_transport as transport
from r2kernel.core import resource
from r2kernel.core._control_shm import (
    decode_response_consumed,
    encode_response_released,
    receive_control_shm_frame,
    send_control_shm_frame,
)
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmRegion
from test_control_shm import (
    _perform_channel_test_exchange,
    _request_lease,
    _test_policy,
    _transact_response_frame,
)


def make_channel(endpoint="unused"):
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = transport.ControlShmCounters()
    channel = transport.ControlShmChannel(endpoint=endpoint, policy=policy, budget=budget, counters=counters)
    return channel, budget, counters


def attach_socket(monkeypatch, channel, connection):
    monkeypatch.setattr(channel, "_connect", lambda timeout: setattr(channel, "_socket", connection))


def test_authenticated_unpublished_timeout_reuses_socket_mapping_and_ticket(monkeypatch, tmp_path):
    endpoint = str(tmp_path / "control.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    channel, budget, counters = make_channel(endpoint)
    now = [10.0]
    proof_peers, publications, received, errors = [], [], [], []
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    def verified_proof(connection, *, deadline=None):
        proof_peers.append(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        now[0] += 2.0  # Deterministic proof/setup cost; no real-time benchmark claim.
    monkeypatch.setattr(resource, "verify_control_server_connection", verified_proof)
    original_send = transport.send_control_shm_frame
    def tracked_send(*args, **kwargs):
        publications.append(args[2] if len(args) > 2 else None)
        return original_send(*args, **kwargs)
    monkeypatch.setattr(transport, "send_control_shm_frame", tracked_send)
    server_budget = ControlShmBudget(_test_policy())
    publication_started = threading.Event()
    def serve():
        request_region = response_region = None
        try:
            with listener.accept()[0] as peer:
                # The first exchange intentionally publishes nothing. Its
                # proof/setup assertions must not consume the peer's I/O guard.
                assert publication_started.wait(timeout=5.0)
                peer.settimeout(1.0)
                frame, descriptors = receive_control_shm_frame(peer)
                assert frame is not None and len(descriptors) == 1
                lease = _request_lease(frame)
                request_region = ControlShmRegion.from_received_descriptor(
                    lease["region_name"], lease["slot_size"], descriptors[0], server_budget)
                received.append((lease["region_name"], request_region.read_payload(lease)))
                response_region = ControlShmRegion.create(4096, server_budget)
                response_lease = response_region.write_payload(b"reply", 9001)
                send_control_shm_frame(peer, _transact_response_frame(response_lease, response_ticket=1),
                                       response_region.file_descriptor)
                acknowledgement, descriptors = receive_control_shm_frame(peer)
                assert acknowledgement is not None and descriptors == []
                ticket, acknowledged_lease = decode_response_consumed(acknowledgement)
                assert ticket == 1 and acknowledged_lease == response_lease
                send_control_shm_frame(peer, encode_response_released(ticket))
        except BaseException as error:
            errors.append(error)
        finally:
            if request_region is not None:
                request_region.close()
            if response_region is not None:
                response_region.close()
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        with pytest.raises(socket.timeout, match="exchange deadline exceeded") as failed:
            _perform_channel_test_exchange(channel, b"first-unsent", timeout_s=1.0)
        assert not channel.is_closed and channel._socket is not None
        assert channel.retains_unpublished_timeout(failed.value)
        assert not channel.retains_unpublished_timeout(socket.timeout("different error"))
        assert channel._outgoing_region is not None
        token, descriptor = channel._outgoing_region.token, channel._outgoing_region.file_descriptor
        assert os.fstat(descriptor).st_size == 4096
        assert publications == [] and len(proof_peers) == 1
        assert channel._outgoing_descriptor_sent is False and channel._next_response_ticket == 1
        publication_started.set()
        response = _perform_channel_test_exchange(channel, b"latest-payload", timeout_s=1.0)
        assert response["ok"] is True and response["payload"] == b"reply"
        assert not channel.retains_unpublished_timeout(failed.value)
        assert received == [(token, b"latest-payload")]
        assert channel._outgoing_region.token == token and channel._outgoing_region.file_descriptor == descriptor
        assert len(proof_peers) == 1 and publications == [descriptor, None]
        assert counters.snapshot()["request_region_creates"] == 1
        assert counters.snapshot()["request_region_reuses"] == 1
        assert counters.snapshot()["fd_sent"] == 1 and channel._next_response_ticket == 2
    finally:
        publication_started.set()
        channel.close()
        listener.close()
        worker.join(timeout=2.0)
    assert not worker.is_alive() and errors == []
    assert budget.snapshot()["channels"] == budget.snapshot()["owned_bytes"] == budget.snapshot()["mapped_bytes"] == 0
    assert server_budget.snapshot()["owned_bytes"] == server_budget.snapshot()["mapped_bytes"] == 0


def test_shutdown_releases_unpublished_retained_mapping(monkeypatch):
    channel, budget, _counters = make_channel()
    sender, receiver = socket.socketpair()
    attach_socket(monkeypatch, channel, sender)
    now = [10.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    def builder(lease):
        now[0] += 2.0
        return b"SRLC\x05\x03"
    try:
        with pytest.raises(socket.timeout):
            channel.exchange(request_builder=builder, response_decoder=lambda frame: {},
                             timeout_s=1.0, request_payload=b"unsent")
        assert not channel.is_closed and budget.snapshot()["owned_bytes"] == 4096
        channel.close()
        assert sender.fileno() == -1 and receiver.recv(1) == b""
        assert budget.snapshot()["channels"] == budget.snapshot()["owned_bytes"] == 0
    finally:
        channel.close()
        receiver.close()


@pytest.mark.parametrize("failure", [ValueError("builder rejected"), TimeoutError("not an expired deadline")])
def test_builder_error_without_expired_deadline_still_closes(monkeypatch, failure):
    channel, budget, _counters = make_channel()
    sender, receiver = socket.socketpair()
    attach_socket(monkeypatch, channel, sender)
    def reject(lease):
        raise failure
    try:
        with pytest.raises(type(failure)) as caught:
            channel.exchange(request_builder=reject, response_decoder=lambda frame: {}, timeout_s=1.0)
        assert caught.value is failure and channel.is_closed
        assert budget.snapshot()["channels"] == 0 and sender.fileno() == -1
    finally:
        channel.close()
        receiver.close()


def test_failed_manager_proof_never_retains_connection(monkeypatch, tmp_path):
    endpoint = str(tmp_path / "proof.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    channel, budget, _counters = make_channel(endpoint)
    failure = TimeoutError("manager proof timed out")
    def reject(connection, *, deadline=None):
        raise failure
    monkeypatch.setattr(resource, "verify_control_server_connection", reject)
    try:
        with pytest.raises(TimeoutError) as caught:
            channel.exchange(request_builder=lambda lease: b"SRLC\x05\x01", response_decoder=lambda frame: {}, timeout_s=1.0)
        assert caught.value is failure and channel.is_closed and channel._socket is None
        assert budget.snapshot()["channels"] == 0
    finally:
        channel.close()
        listener.close()


def test_attempted_partial_fd_send_still_closes_without_replay(monkeypatch):
    channel, budget, _counters = make_channel()
    sender, receiver = socket.socketpair()
    attach_socket(monkeypatch, channel, sender)
    now = [10.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    failure = TimeoutError("deadline after partial descriptor send")
    calls = []
    def partial_send(connection, frame, descriptor=None, *, deadline=None):
        calls.append(descriptor)
        assert descriptor is not None
        connection.sendmsg([b"\x01"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [descriptor]))])
        now[0] += 2.0
        raise failure
    monkeypatch.setattr(transport, "send_control_shm_frame", partial_send)
    received_descriptors = []
    try:
        with pytest.raises(TimeoutError) as caught:
            _perform_channel_test_exchange(channel, b"partially-published", timeout_s=1.0)
        assert caught.value is failure and len(calls) == 1 and channel.is_closed
        prefix, ancillary, _flags, _address = receiver.recvmsg(1, socket.CMSG_SPACE(array("i").itemsize))
        assert prefix == b"\x01" and len(ancillary) == 1
        rights = array("i")
        rights.frombytes(ancillary[0][2])
        received_descriptors.extend(rights)
        assert len(received_descriptors) == 1
        assert budget.snapshot()["channels"] == budget.snapshot()["owned_bytes"] == 0
        assert sender.fileno() == -1
    finally:
        for descriptor in received_descriptors:
            os.close(descriptor)
        channel.close()
        receiver.close()
