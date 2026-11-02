from __future__ import annotations

import array
import errno
import json
import os
import socket
import threading
from contextlib import contextmanager

import pytest

from r2kernel.core import resource


pytestmark = pytest.mark.skipif(not hasattr(socket, "SCM_RIGHTS"), reason="requires Unix FD passing")


@contextmanager
def broker_response(tmp_path, monkeypatch, fragments, *, receive_limit=None, after_receive=None):
    """A private broker with real rights; the probe observes the transferred FD lifetime."""
    endpoint = str(tmp_path / "broker.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    listener.settimeout(2)
    probe, transferred = socket.socketpair()
    probe.settimeout(2)
    requests, failures, received, submissions, timeouts = [], [], [], [], []
    original_socket = socket.socket

    class ReceivingSocket:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.connection.close()

        def sendall(self, data):
            submissions.append(data)
            return self.connection.sendall(data)

        def settimeout(self, timeout):
            timeouts.append(timeout)
            self.connection.settimeout(timeout)

        def recvmsg(self, size, *args):
            if receive_limit is not None:
                size = min(size, receive_limit)
            result = self.connection.recvmsg(size, *args)
            for level, kind, data in result[1]:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    rights = array.array("i")
                    rights.frombytes(data)
                    received.extend(rights)
            if after_receive is not None:
                result = after_receive(result)
            return result

    def client_socket(*args, **kwargs):
        connection = original_socket(*args, **kwargs)
        # accept() constructs its socket with fileno; only wrap the SDK caller.
        if args == (socket.AF_UNIX, socket.SOCK_STREAM) and not kwargs:
            return ReceivingSocket(connection)
        return connection

    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                body = bytearray()
                while b"\n" not in body:
                    chunk = connection.recv(8192)
                    if not chunk:
                        raise EOFError("client did not submit a complete request")
                    body.extend(chunk)
                requests.append(json.loads(body))
                try:
                    for data, count in fragments:
                        if count:
                            connection.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                        array.array("i", [transferred.fileno()] * count))])
                        else:
                            connection.sendall(data)
                except (BrokenPipeError, ConnectionResetError):
                    # Rejection can close the client before all hostile bytes are written.
                    pass
        except BaseException as error:
            failures.append(error)
        finally:
            transferred.close()

    worker = threading.Thread(target=serve)
    worker.start()
    monkeypatch.setattr(resource.socket, "socket", client_socket)
    client = resource.ResourceBrokerClient(lease_id="private-test-lease", socket_path=endpoint)
    try:
        yield client, probe, received, submissions, timeouts
    finally:
        worker.join(3)
        listener.close()
        probe.close()
        assert not worker.is_alive()
        assert failures == []
        assert len(requests) == len(submissions) == 1
        assert requests[0]["op"] == "open_device"
        for fd in received:
            with pytest.raises(OSError) as closed:
                os.fstat(fd)
            assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("rights_fragment", [0, 1])
def test_fragmented_success_transfers_exactly_one_usable_noninheritable_fd(tmp_path, monkeypatch, rights_fragment):
    fragments = [(b'{"ok":t', int(rights_fragment == 0)),
                 (b'rue,"result":{"device":"private"}}\n', int(rights_fragment == 1))]
    with broker_response(tmp_path, monkeypatch, fragments, receive_limit=7) as (client, probe, fds, _, _):
        result = client.open_device("/dev/null")
        try:
            assert result.metadata == {"device": "private"}
            assert fds == [result.fd]
            assert not os.get_inheritable(result.fd)
            with socket.socket(fileno=os.dup(result.fd)) as usable:
                usable.sendall(b"usable")
            assert probe.recv(6) == b"usable"
        finally:
            result.close()
        assert probe.recv(1) == b""


@pytest.mark.parametrize("fragments", [
    [(b'{"ok":false,"error":"denied"}\n', 1)],
    [(b'{"ok":false,"error":"not attached to a process yet"}\n', 1)],
    [(b'{"ok":false,"error":"not attached to a process yet"}\n', 0)],
    [(b'{"ok":true,"result":{', 1), (b'bad json}\n', 0)],
    [(b'{"ok":true,"result":{"name":"\xff"}}\n', 1)],
    [(b'{"ok":true,"result":{}}', 1)],  # EOF before newline
    [(b'{"ok":true,"result":{}}\n', 2)],
    [(b'{"ok":', 1), (b'true,"result":{}}\n', 1)],
    [(b'{"ok":true,"result":{}}\n', 80)],  # real MSG_CTRUNC
    [(b'{"ok":true,"result":{}}\n', 0)],
    [(b'[]\n', 1)],
    [(b'{"ok":1,"result":{}}\n', 1)],
    [(b'{"ok":true,"result":[]}\n', 1)],
    [(b'{"ok":true,"result":{}}\n{}\n', 1)],
])
def test_rejected_response_closes_all_rights_without_replaying(tmp_path, monkeypatch, fragments):
    with broker_response(tmp_path, monkeypatch, fragments) as (client, probe, _fds, _sent, _timeouts):
        with pytest.raises((RuntimeError, ValueError)):
            client.open_device("/dev/null")
        assert probe.recv(1) == b""


def test_oversize_response_closes_received_rights(tmp_path, monkeypatch):
    fragments = [(b'{"ok":true,"result":{"data":"', 1),
                 (b'x' * resource._MAX_RESOURCE_RESPONSE_BYTES + b'"}}\n', 0)]
    with broker_response(tmp_path, monkeypatch, fragments) as (client, probe, _fds, _sent, _timeouts):
        with pytest.raises(RuntimeError, match="exceeds 1 MiB"):
            client.open_device("/dev/null")
        assert probe.recv(1) == b""


@pytest.mark.parametrize("flag", [socket.MSG_CTRUNC, socket.MSG_TRUNC])
def test_truncation_flags_close_delivered_rights(tmp_path, monkeypatch, flag):
    def truncate(result):
        data, ancillary, flags, address = result
        return data, ancillary, flags | flag, address

    with broker_response(tmp_path, monkeypatch, [(b'{"ok":true,"result":{}}\n', 1)],
                         after_receive=truncate) as (client, probe, _fds, _sent, _timeouts):
        with pytest.raises(RuntimeError, match="ancillary"):
            client.open_device("/dev/null")
        assert probe.recv(1) == b""


@pytest.mark.parametrize("malformed", [False, True])
def test_invalid_ancillary_data_closes_other_delivered_rights(tmp_path, monkeypatch, malformed):
    def invalidate(result):
        data, ancillary, flags, address = result
        if malformed:
            level, kind, rights = ancillary[0]
            ancillary = [(level, kind, rights + b"x")]
        else:
            ancillary = ancillary + [(socket.SOL_SOCKET, 0, b"")]
        return data, ancillary, flags, address

    with broker_response(tmp_path, monkeypatch, [(b'{"ok":true,"result":{}}\n', 1)],
                         after_receive=invalidate) as (client, probe, _fds, _sent, _timeouts):
        with pytest.raises(RuntimeError, match="ancillary"):
            client.open_device("/dev/null")
        assert probe.recv(1) == b""


def test_fragment_progress_does_not_renew_absolute_receive_budget(tmp_path, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(resource.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(resource, "_RESOURCE_REQUEST_TIMEOUT_S", 1.0)

    def advance(result):
        now[0] += 0.6
        return result

    with broker_response(tmp_path, monkeypatch, [(b'{"ok":true,"result":{}}\n', 1)],
                         receive_limit=7, after_receive=advance) as (client, probe, _fds, _sent, timeouts):
        with pytest.raises(TimeoutError, match="deadline"):
            client.open_device("/dev/null")
        assert timeouts == pytest.approx([1.0, 1.0, 1.0, 0.4])
        assert probe.recv(1) == b""
