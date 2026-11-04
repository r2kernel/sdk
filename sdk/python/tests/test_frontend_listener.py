"""The frontend API transfers a Unix listener and keeps TCP in the broker."""
from __future__ import annotations

import array
import errno
import json
import os
import select
import socket
import threading
from contextlib import contextmanager

import pytest

from r2kernel.core.resource import BrokeredSocketResult, ResourceBrokerClient

pytestmark = pytest.mark.skipif(not hasattr(socket, "SCM_RIGHTS"), reason="requires Unix FD passing")

METADATA = {"domain": "unix", "transport": "unix_listener", "bind_host": "127.0.0.1", "bind_port": 18091}


@contextmanager
def frontend_response(tmp_path, monkeypatch, transferred, metadata):
    endpoint = str(tmp_path / "broker.sock")
    received, requests, failures = [], [], []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as broker:
        broker.bind(endpoint)
        broker.listen(1)
        broker.settimeout(3)

        def serve():
            try:
                with broker.accept()[0] as connection:
                    connection.settimeout(3)
                    line = bytearray()
                    while b"\n" not in line:
                        chunk = connection.recv(8192)
                        if not chunk:
                            raise EOFError("frontend request was incomplete")
                        line.extend(chunk)
                    requests.append(json.loads(line))
                    response = json.dumps({"ok": True, "result": metadata}).encode() + b"\n"
                    connection.sendmsg([response], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                     array.array("i", [transferred.fileno()]))])
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=serve)
        worker.start()
        client = ResourceBrokerClient(lease_id="frontend-test", socket_path=endpoint)
        request_fd = client._request_fd

        def record(request):
            result, fd = request_fd(request)
            received.append(fd)
            return result, fd

        monkeypatch.setattr(client, "_request_fd", record)
        try:
            yield client, requests
        finally:
            worker.join(4)
            assert not worker.is_alive()
            assert not failures
            assert len(requests) == 1
            for fd in received:
                with pytest.raises(OSError) as closed:
                    os.fstat(fd)
                assert closed.value.errno == errno.EBADF


def test_frontend_unix_listener_accepts_and_close_shuts_down_broker_copy(tmp_path, monkeypatch):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        endpoint = str(tmp_path / "frontend.sock")
        listener.bind(endpoint)
        listener.listen(2)
        with frontend_response(tmp_path, monkeypatch, listener, METADATA) as (client, requests):
            result = client.open_frontend_tcp_listener()
            try:
                assert result.socket.family == socket.AF_UNIX
                assert result.metadata == METADATA
                assert not os.get_inheritable(result.socket.fileno())
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(2)
                    peer.connect(endpoint)
                    connection, _ = result.socket.accept()
                    with connection:
                        connection.sendall(b"frontend")
                        assert peer.recv(16) == b"frontend"
            finally:
                result.close()
            poller = select.poll()
            poller.register(listener, select.POLLHUP)
            assert any(flags & select.POLLHUP for _, flags in poller.poll(1000))
        assert requests == [{"op": "open_frontend_tcp_listener", "lease_id": "frontend-test", "kind": "api",
                             "bind_host": "", "bind_port": 0, "backlog": 128}]


@pytest.mark.parametrize("metadata", [
    {}, {**METADATA, "domain": "inet"}, {**METADATA, "transport": "tcp_listener"},
    {**METADATA, "bind_host": "0.0.0.0"}, {**METADATA, "bind_port": True},
    {**METADATA, "bind_port": "18091"}, {**METADATA, "bind_port": 1.5},
    {**METADATA, "bind_port": 0}, {**METADATA, "bind_port": 65536},
])
def test_frontend_rejects_metadata_and_closes_received_fd(tmp_path, monkeypatch, metadata):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "frontend.sock"))
        listener.listen()
        with frontend_response(tmp_path, monkeypatch, listener, metadata) as (client, _):
            with pytest.raises(RuntimeError, match="frontend response"):
                client.open_frontend_tcp_listener()


@pytest.mark.parametrize("kind", ["inet_listener", "unix_stream", "unix_dgram", "file"])
def test_frontend_rejects_actual_fd_mismatch_and_closes_received_fd(tmp_path, monkeypatch, kind):
    if kind == "file":
        descriptor = (tmp_path / "regular-file").open("wb")
    else:
        family = socket.AF_INET if kind == "inet_listener" else socket.AF_UNIX
        descriptor = socket.socket(family, socket.SOCK_DGRAM if kind == "unix_dgram" else socket.SOCK_STREAM)
        if kind == "inet_listener":
            descriptor.bind(("127.0.0.1", 0))
            descriptor.listen()
    with descriptor:
        with frontend_response(tmp_path, monkeypatch, descriptor, METADATA) as (client, _):
            with pytest.raises((RuntimeError, OSError)):
                client.open_frontend_tcp_listener()


def test_closing_an_ordinary_socket_does_not_shutdown_other_copies():
    spare, peer = socket.socketpair()
    with spare, peer:
        # An ordinary broker result owns only its duplicate, with no relay shutdown contract.
        duplicate = socket.socket(fileno=os.dup(spare.fileno()))
        BrokeredSocketResult(duplicate, {"domain": "unix"}).close()
        spare.sendall(b"still-open")
        assert peer.recv(16) == b"still-open"
