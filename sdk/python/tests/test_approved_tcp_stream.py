"""Approved streams require the new operation and preserve AF_UNIX confinement."""
import array
import json
import os
import socket
import struct
import threading
from contextlib import contextmanager

import pytest
from r2kernel.core import resource


@contextmanager
def broker(tmp_path, monkeypatch, response, passed_socket=None, *, root_peer=True):
    endpoint = str(tmp_path / "approved.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    listener.settimeout(2)
    requests, errors = [], []
    real_socket = socket.socket

    class ClientSocket:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def __enter__(self): return self
        def __exit__(self, *args): self.connection.close()
        def getsockopt(self, level, name, *args):
            if (level, name) == (socket.SOL_SOCKET, socket.SO_PEERCRED):
                return struct.pack("3i", os.getpid(), 0 if root_peer else 1000, os.getgid())
            return self.connection.getsockopt(level, name, *args)

    def create(*args, **kwargs):
        connection = real_socket(*args, **kwargs)
        return ClientSocket(connection) if args == (socket.AF_UNIX, socket.SOCK_STREAM) and not kwargs else connection

    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                data = b""
                while b"\n" not in data:
                    chunk = connection.recv(1024)
                    if not chunk: return
                    data += chunk
                requests.append(json.loads(data))
                encoded = json.dumps(response).encode() + b"\n"
                if passed_socket is not None:
                    connection.sendmsg([encoded], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [passed_socket.fileno()]))])
                else:
                    connection.sendall(encoded)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=serve)
    worker.start()
    monkeypatch.setattr(resource.socket, "socket", create)
    try:
        yield resource.ResourceBrokerClient(lease_id="lease1", socket_path=endpoint), requests
    finally:
        worker.join(3)
        listener.close()
        assert not worker.is_alive()
        assert errors == []


def metadata(**overrides):
    return {"ok": True, "result": {"kind": "approved_tcp_stream", "grant_id": "grant1", "lease_id": "lease1", "execution_id": "1" * 32, **overrides}}


def test_new_operation_returns_connected_unix_stream_and_single_request(tmp_path, monkeypatch):
    app, probe = socket.socketpair()
    with app, probe, broker(tmp_path, monkeypatch, metadata(), app) as (client, requests):
        result = client.open_approved_tcp_stream(grant_id="grant1")
        with result.socket:
            assert result.socket.family == socket.AF_UNIX
            assert not result.socket.get_inheritable()
            result.socket.sendall(b"body")
            assert probe.recv(4) == b"body"
        assert requests == [{"op": "open_approved_tcp_stream", "lease_id": "lease1", "grant_id": "grant1"}]


@pytest.mark.parametrize("overrides", [{"lease_id": "foreign"}, {"grant_id": "foreign"}, {"kind": "tcp_socket"},
                                       {"execution_id": None}, {"execution_id": ""}, {"execution_id": "A" * 32},
                                       {"execution_id": "1" * 31}, {"execution_id": 123}])
def test_foreign_or_wrong_metadata_closes_received_descriptor(tmp_path, monkeypatch, overrides):
    app, probe = socket.socketpair()
    with probe, broker(tmp_path, monkeypatch, metadata(**overrides), app) as (client, requests):
        with pytest.raises(RuntimeError, match="Invalid approved"):
            client.open_approved_tcp_stream(grant_id="grant1")
        app.close()
        probe.settimeout(1)
        assert probe.recv(1) == b""
        assert len(requests) == 1


def test_inet_descriptor_rejected_even_when_metadata_claims_unix(tmp_path, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as inet:
        with broker(tmp_path, monkeypatch, metadata(), inet) as (client, requests):
            with pytest.raises(RuntimeError, match="Invalid approved"):
                client.open_approved_tcp_stream(grant_id="grant1")
            assert len(requests) == 1


@pytest.mark.parametrize("kind", ["listener", "unconnected", "datagram"])
def test_only_connected_unix_streams_are_accepted_and_rejected_rights_are_closed(tmp_path, monkeypatch, kind):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM if kind == "datagram" else socket.SOCK_STREAM) as passed:
        if kind == "listener":
            passed.bind(str(tmp_path / "listener.sock"))
            passed.listen()
        with broker(tmp_path, monkeypatch, metadata(), passed) as (client, requests):
            received = []
            request_fd = client._request_fd

            def record(*args, **kwargs):
                result, fd = request_fd(*args, **kwargs)
                received.append(fd)
                return result, fd

            monkeypatch.setattr(client, "_request_fd", record)
            with pytest.raises(RuntimeError, match="Invalid approved"):
                client.open_approved_tcp_stream(grant_id="grant1")
            assert len(requests) == len(received) == 1
            with pytest.raises(OSError):
                os.fstat(received[0])


def test_nonroot_broker_rejected_before_submitting_grant(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch, metadata(), root_peer=False) as (client, requests):
        with pytest.raises(RuntimeError, match="root-owned"):
            client.open_approved_tcp_stream(grant_id="grant1")
        assert requests == []


def test_rc11_unsupported_operation_is_not_retried_or_downgraded(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch, {"ok": False, "error": "unknown variant open_approved_tcp_stream"}) as (client, requests):
        with pytest.raises(RuntimeError, match="unknown variant"):
            client.open_approved_tcp_stream(grant_id="grant1")
        assert len(requests) == 1
        assert requests[0]["op"] == "open_approved_tcp_stream"
