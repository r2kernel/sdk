"""open_uplink_tcp against a private fake broker that passes real descriptors (SCM_RIGHTS).

The broker resolves and connects; the app receives a connected AF_UNIX stream. A probe socketpair stands in for
the broker's relay end: the probe reads EOF only once every copy of the passed descriptor is closed.
"""
from __future__ import annotations

import array
import asyncio
import errno
import gc
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import warnings

import pytest

from r2kernel.core import ResourceBrokerError, resource
from r2kernel.core.r2kernel_client import R2KernelClient

pytestmark = pytest.mark.skipif(not hasattr(socket, "SCM_RIGHTS"), reason="requires Unix FD passing")

LEASE = "lease1"
CONNECTION_ID = "0123456789abcdef" * 2
UPLINK_ERROR_CODES = ["invalid_request", "uplink_role_refused", "app_uplink_off", "uplink_unavailable",
                      "destination_not_allowed", "destination_blocked", "resolve_failed", "connect_timeout",
                      "connect_failed", "uplink_limit"]
MISSING = object()


def uplink_reply(**overrides):
    result = {"kind": "uplink_tcp", "connection_id": CONNECTION_ID, "lease_id": LEASE, "host": "api.example.com",
              "port": 443, "remote_address": "203.0.113.7", "interface": "eth0"}
    for key, value in overrides.items():
        if value is MISSING:
            result.pop(key)
        else:
            result[key] = value
    return {"ok": True, "result": result}


class FakeBroker:
    """Answers one request with ``reply`` and passes the ``passed`` descriptor; ``reply=None`` never answers."""

    def __init__(self, tmp_path, reply, passed=None, *, hold=None):
        self.path = str(tmp_path / "broker.sock")
        self.reply, self.passed, self.hold = reply, passed, hold
        self.requests, self.errors, self.sent = [], [], threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(1)
        self._listener.settimeout(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def client(self):
        return resource.ResourceBrokerClient(lease_id=LEASE, socket_path=self.path)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        if self.hold is not None:
            self.hold.set()
        self._thread.join(5)
        self._listener.close()
        assert not self._thread.is_alive()
        assert self.errors == []

    def _serve(self):
        try:
            with self._listener.accept()[0] as connection:
                connection.settimeout(5)
                line = bytearray()
                while b"\n" not in line:
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise EOFError("client did not submit a complete request")
                    line.extend(chunk)
                self.requests.append(json.loads(line))
                if self.hold is not None:
                    self.hold.wait(5)
                if self.reply is None:
                    assert connection.recv(1) == b"", "a client that gives up closes its request socket"
                    return
                encoded = json.dumps(self.reply).encode() + b"\n"
                if self.passed is None:
                    connection.sendall(encoded)
                else:
                    connection.sendmsg([encoded], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [self.passed]))])
                self.sent.set()
        except BaseException as error:
            self.errors.append(error)


def closed(probe):
    """True when the probe reads EOF: every copy of its peer descriptor is closed."""
    probe.settimeout(2)
    return probe.recv(1) == b""


def recorded_fds(client, monkeypatch):
    received = []
    request_fd = client._request_fd

    def record(*args, **kwargs):
        metadata, fd = request_fd(*args, **kwargs)
        received.append(fd)
        return metadata, fd

    monkeypatch.setattr(client, "_request_fd", record)
    return received


def assert_closed_fd(fd):
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def test_success_round_trips_bytes_through_a_connected_unix_stream(tmp_path):
    app, probe = socket.socketpair()
    with probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        result = broker.client().open_uplink_tcp("api.example.com", 443)
        app.close()
        try:
            assert broker.requests == [{"op": "open_uplink_tcp", "lease_id": LEASE, "host": "api.example.com",
                                        "port": 443, "timeout_ms": 10000}]
            assert result.metadata == uplink_reply()["result"]
            assert result.socket.getsockopt(socket.SOL_SOCKET, resource._SO_DOMAIN) == socket.AF_UNIX
            assert not result.socket.get_inheritable()
            result.socket.sendall(b"request")
            probe.settimeout(2)
            assert probe.recv(7) == b"request"
            probe.sendall(b"response")
            assert result.socket.recv(8) == b"response"
        finally:
            result.close()
        assert closed(probe)


@pytest.mark.parametrize(("host", "sent", "reported"), [
    ("API.Example.COM.", "api.example.com", "api.example.com"),
    ("203.0.113.7", "203.0.113.7", "203.0.113.7"),
    ("2001:DB8:0::7", "2001:db8:0::7", "2001:db8:0::7"),
    ("2001:DB8:0::7", "2001:db8:0::7", "2001:db8::7"),  # the same address in canonical text
])
def test_request_carries_the_normalized_host_and_the_reply_must_name_it(tmp_path, host, sent, reported):
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(host=reported, port=8001), app.fileno()) as broker:
        broker.client().open_uplink_tcp(host, 8001, timeout_s=2.5).close()
        assert broker.requests[0]["host"] == sent
        assert broker.requests[0]["timeout_ms"] == 2500


@pytest.mark.parametrize("overrides", [
    {"kind": "approved_tcp_stream"}, {"kind": None}, {"kind": MISSING},
    {"lease_id": "foreign"}, {"lease_id": None}, {"lease_id": MISSING},
    {"connection_id": None}, {"connection_id": ""}, {"connection_id": "A" * 32}, {"connection_id": "1" * 31},
    {"connection_id": "1" * 33}, {"connection_id": "g" * 32}, {"connection_id": 123}, {"connection_id": MISSING},
    {"port": 444}, {"port": "443"}, {"port": 443.0}, {"port": True}, {"port": None}, {"port": MISSING},
    {"host": "other.example.com"}, {"host": "API.example.com"}, {"host": "api.example.com."},
    {"host": "203.0.113.7"}, {"host": None}, {"host": 1}, {"host": MISSING},
])
def test_metadata_mismatch_closes_the_descriptor_and_raises(tmp_path, overrides):
    app, probe = socket.socketpair()
    with probe, FakeBroker(tmp_path, uplink_reply(**overrides), app.fileno()) as broker:
        with pytest.raises(RuntimeError, match="Invalid app uplink response: metadata"):
            broker.client().open_uplink_tcp("api.example.com", 443)
        app.close()
        assert closed(probe)
        assert len(broker.requests) == 1


@pytest.mark.parametrize("kind", ["inet_stream", "inet_connected", "unix_listener", "unix_unconnected",
                                  "unix_datagram", "regular_file"])
def test_descriptor_that_is_not_a_connected_unix_stream_is_refused_and_closed(tmp_path, monkeypatch, kind):
    resources = []
    if kind in ("inet_stream", "inet_connected"):
        passed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if kind == "inet_connected":
            server = socket.create_server(("127.0.0.1", 0))
            passed.connect(server.getsockname())
            resources.append(server)
    elif kind == "unix_listener":
        passed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        passed.bind(str(tmp_path / "listener.sock"))
        passed.listen()
    elif kind == "unix_unconnected":
        passed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    elif kind == "unix_datagram":
        passed, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        resources.append(peer)
    else:
        passed = (tmp_path / "regular-file").open("wb")
    resources.append(passed)
    try:
        with FakeBroker(tmp_path, uplink_reply(), passed.fileno()) as broker:
            client = broker.client()
            received = recorded_fds(client, monkeypatch)
            with pytest.raises(RuntimeError, match="Invalid app uplink response"):
                client.open_uplink_tcp("api.example.com", 443)
            assert len(broker.requests) == len(received) == 1
            assert_closed_fd(received[0])
    finally:
        for item in resources:
            item.close()


@pytest.mark.parametrize("code", UPLINK_ERROR_CODES + ["not_found"])
def test_refusal_raises_resource_broker_error_with_its_code_and_closes_any_descriptor(tmp_path, code):
    reply = {"ok": False, "result": None, "error_code": code, "error": f"refused: {code}"}
    app, probe = socket.socketpair()
    with probe, FakeBroker(tmp_path, reply, app.fileno()) as broker:
        with pytest.raises(ResourceBrokerError) as refused:
            broker.client().open_uplink_tcp("api.example.com", 443)
        app.close()
        assert isinstance(refused.value, RuntimeError)
        assert (refused.value.code, refused.value.error, str(refused.value)) == (code, f"refused: {code}", f"refused: {code}")
        assert closed(probe)
        assert len(broker.requests) == 1


@pytest.mark.parametrize(("reply", "code", "message"), [
    ({"ok": False, "error": "denied"}, None, "denied"),
    ({"ok": False, "error_code": "", "error": "denied"}, None, "denied"),
    ({"ok": False, "error_code": 7, "error": "denied"}, None, "denied"),
    ({"ok": False}, None, "R2Kernel resource request failed"),
])
def test_refusal_without_a_usable_code_keeps_the_text(tmp_path, reply, code, message):
    with FakeBroker(tmp_path, reply) as broker:
        with pytest.raises(ResourceBrokerError) as refused:
            broker.client().open_uplink_tcp("api.example.com", 443)
        assert (refused.value.code, str(refused.value)) == (code, message)


def test_other_broker_operations_also_carry_the_code(tmp_path):
    reply = {"ok": False, "result": None, "error_code": "policy", "error": "denied by policy"}
    with FakeBroker(tmp_path, reply) as broker:
        with pytest.raises(ResourceBrokerError) as refused:
            broker.client().open_device("/dev/null")
        assert (refused.value.code, str(refused.value)) == ("policy", "denied by policy")


class RecordingSocket:
    """Wraps the SDK's request socket to record its timeouts; optionally advances a fake clock per receive."""

    def __init__(self, connection, timeouts, on_receive):
        self.connection, self.timeouts, self.on_receive = connection, timeouts, on_receive

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.connection.close()

    def settimeout(self, timeout):
        self.timeouts.append(timeout)
        self.connection.settimeout(timeout)

    def recvmsg(self, *args):
        self.on_receive()
        return self.connection.recvmsg(*args)


def record_request_socket(monkeypatch, on_receive=lambda: None):
    """Install after creating the FakeBroker, so only the SDK's request socket is wrapped."""
    timeouts, real_socket = [], socket.socket

    def create(*args, **kwargs):
        connection = real_socket(*args, **kwargs)
        if args == (socket.AF_UNIX, socket.SOCK_STREAM) and not kwargs:
            return RecordingSocket(connection, timeouts, on_receive)
        return connection

    monkeypatch.setattr(resource.socket, "socket", create)
    return timeouts


@pytest.mark.parametrize(("timeout_s", "budget"), [(0.25, 2.25), (30, 32.0)])
def test_reply_budget_is_the_connect_timeout_plus_two_seconds(tmp_path, monkeypatch, timeout_s, budget):
    monkeypatch.setattr(resource.time, "monotonic", lambda: 100.0)
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        timeouts = record_request_socket(monkeypatch)
        broker.client().open_uplink_tcp("api.example.com", 443, timeout_s=timeout_s).close()
        assert broker.requests[0]["timeout_ms"] == round(timeout_s * 1000)
        assert timeouts and timeouts == pytest.approx([budget] * len(timeouts))


def test_reply_after_the_generic_request_timeout_is_still_accepted(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(resource.time, "monotonic", lambda: now[0])
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        def slow_broker():
            now[0] += resource._RESOURCE_REQUEST_TIMEOUT_S + 1.0

        record_request_socket(monkeypatch, slow_broker)
        broker.client().open_uplink_tcp("api.example.com", 443, timeout_s=10.0).close()


def test_missing_reply_raises_timeout_after_the_budget_and_closes_the_request(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "_UPLINK_REPLY_MARGIN_S", 0.05)
    with FakeBroker(tmp_path, None) as broker:
        started = time.monotonic()
        with pytest.raises(socket.timeout):  # TimeoutError on Python 3.10+
            broker.client().open_uplink_tcp("api.example.com", 443, timeout_s=0.05)
        assert time.monotonic() - started < 2.0
        assert len(broker.requests) == 1 and broker.requests[0]["timeout_ms"] == 50


@pytest.mark.parametrize("host", ["", "a" * 254, "exa mple.com", "under_score.example", "[2001:db8::1]",
                                  "fe80::1%eth0", "2001:db8::zz", "a..b", ".", "..", "example.com..", ".example.com",
                                  "héllo.example", 123, None, b"api.example.com"])
def test_invalid_host_is_refused_before_any_request(host):
    client = resource.ResourceBrokerClient(lease_id=LEASE, socket_path="/nonexistent/broker.sock")
    with pytest.raises(ValueError, match="host"):
        client.open_uplink_tcp(host, 443)


@pytest.mark.parametrize(("arguments", "match"), [
    ({"port": 0}, "port"), ({"port": 65536}, "port"), ({"port": -1}, "port"), ({"port": True}, "port"),
    ({"port": "443"}, "port"), ({"port": 443.0}, "port"), ({"port": None}, "port"),
    ({"timeout_s": 0}, "timeout_s"), ({"timeout_s": 0.0004}, "timeout_s"), ({"timeout_s": -1}, "timeout_s"),
    ({"timeout_s": 120.001}, "timeout_s"), ({"timeout_s": float("nan")}, "timeout_s"),
    ({"timeout_s": float("inf")}, "timeout_s"), ({"timeout_s": True}, "timeout_s"), ({"timeout_s": "10"}, "timeout_s"),
    ({"server_hostname": "api.example.com"}, "tls=True"), ({"ssl_context": ssl.create_default_context()}, "tls=True"),
    ({"tls": True, "server_hostname": ""}, "server_hostname"), ({"tls": True, "ssl_context": object()}, "ssl_context"),
])
def test_invalid_arguments_are_refused_before_any_request(arguments, match):
    client = resource.ResourceBrokerClient(lease_id=LEASE, socket_path="/nonexistent/broker.sock")
    arguments = dict(arguments)
    port = arguments.pop("port", 443)
    with pytest.raises(ValueError, match=match):
        client.open_uplink_tcp("api.example.com", port, **arguments)


@pytest.mark.parametrize(("timeout_s", "timeout_ms"), [(0.001, 1), (120, 120000), (0.1, 100)])
def test_timeout_bounds_are_sent_in_milliseconds(tmp_path, timeout_s, timeout_ms):
    host = "a" * 126 + "." + "b" * 126  # the longest accepted host, 253 bytes
    with FakeBroker(tmp_path, {"ok": False, "error_code": "invalid_request", "error": "x"}) as broker:
        with pytest.raises(ResourceBrokerError):
            broker.client().open_uplink_tcp(host, 443, timeout_s=timeout_s)
        assert broker.requests[0]["timeout_ms"] == timeout_ms
        assert broker.requests[0]["host"] == host


class RecordingContext(ssl.SSLContext):
    """Records the wrap and returns the socket unchanged (no handshake)."""

    def wrap_socket(self, sock, server_hostname=None, **kwargs):
        self.wraps = getattr(self, "wraps", []) + [(server_hostname, sock.gettimeout(), kwargs)]
        return sock


@pytest.mark.parametrize(("host", "server_hostname", "expected"), [
    ("API.Example.com.", None, "api.example.com"),
    ("api.example.com", "service.example.org", "service.example.org"),
])
def test_tls_wraps_the_stream_with_the_given_context_and_server_hostname(tmp_path, host, server_hostname, expected):
    context = RecordingContext(ssl.PROTOCOL_TLS_CLIENT)
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        result = broker.client().open_uplink_tcp(host, 443, timeout_s=3.0, tls=True, server_hostname=server_hostname,
                                                 ssl_context=context)
        with result.socket:
            assert context.wraps == [(expected, 3.0, {})]
            assert result.socket.gettimeout() is None  # the handshake timeout is not kept
            assert result.metadata == uplink_reply()["result"]


def test_tls_without_a_context_uses_the_default_context(tmp_path, monkeypatch):
    context = RecordingContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(ssl, "create_default_context", lambda *args, **kwargs: context)
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        broker.client().open_uplink_tcp("api.example.com", 443, tls=True).close()
        assert [wrap[0] for wrap in context.wraps] == ["api.example.com"]


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    """A throwaway self-signed certificate for uplink.test, created with the openssl command."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("requires the openssl command to create a test certificate")
    directory = tmp_path_factory.mktemp("uplink-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=uplink.test",
                    "-addext", "subjectAltName=DNS:uplink.test"], check=True, capture_output=True, timeout=60)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(cert), str(key))
    client = ssl.create_default_context(cafile=str(cert))
    client.verify_flags &= ~ssl.VERIFY_X509_STRICT  # a test-only trust anchor, not a CA-issued chain
    return server, client


def tls_echo_server(context, probe):
    """Serve one TLS session on the probe: echo the first 4 bytes in upper case; record failures."""
    outcome = []

    def serve():
        try:
            with context.wrap_socket(probe, server_side=True) as session:
                session.sendall(session.recv(4).upper())
            outcome.append("served")
        except (OSError, ssl.SSLError) as error:
            outcome.append(error)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return worker, outcome


def test_tls_handshake_runs_over_the_uplink_stream(tmp_path, certificate):
    server_context, client_context = certificate
    app, probe = socket.socketpair()
    probe.settimeout(5)
    worker, outcome = tls_echo_server(server_context, probe)
    with app, FakeBroker(tmp_path, uplink_reply(host="uplink.test"), app.fileno()) as broker:
        result = broker.client().open_uplink_tcp("uplink.test", 443, tls=True, ssl_context=client_context)
        with result.socket as session:
            assert isinstance(session, ssl.SSLSocket) and session.version().startswith("TLS")
            session.sendall(b"ping")
            assert session.recv(4) == b"PING"
    worker.join(5)
    assert outcome == ["served"]


def test_tls_verification_failure_closes_the_uplink(tmp_path, certificate):
    server_context, client_context = certificate
    app, probe = socket.socketpair()
    probe.settimeout(5)
    worker, outcome = tls_echo_server(server_context, probe)
    with app, FakeBroker(tmp_path, uplink_reply(host="uplink.test"), app.fileno()) as broker:
        client = broker.client()
        with pytest.raises(ssl.SSLCertVerificationError):
            client.open_uplink_tcp("uplink.test", 443, tls=True, server_hostname="other.test",
                                   ssl_context=client_context)
        app.close()
    worker.join(5)
    assert not worker.is_alive() and outcome and outcome != ["served"]


def echo_upper(probe):
    def serve():
        data = probe.recv(4)
        probe.sendall(data.upper())

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return worker


def test_asyncio_variant_returns_streams_over_the_uplink(tmp_path):
    app, probe = socket.socketpair()
    probe.settimeout(5)
    worker = echo_upper(probe)

    async def exchange(client):
        reader, writer = await client.open_uplink_connection("api.example.com", 443, limit=1024)
        writer.write(b"ping")
        await writer.drain()
        data = await reader.readexactly(4)
        writer.close()
        await writer.wait_closed()
        return data

    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        assert asyncio.run(exchange(broker.client())) == b"PING"
        assert broker.requests[0]["op"] == "open_uplink_tcp"
    worker.join(5)


def test_asyncio_variant_performs_tls(tmp_path, certificate):
    server_context, client_context = certificate
    app, probe = socket.socketpair()
    probe.settimeout(5)
    worker, outcome = tls_echo_server(server_context, probe)

    async def exchange(client):
        reader, writer = await client.open_uplink_connection("uplink.test", 443, tls=True, ssl_context=client_context)
        assert writer.get_extra_info("ssl_object").server_hostname == "uplink.test"
        writer.write(b"ping")
        data = await reader.readexactly(4)
        writer.close()
        try:
            await writer.wait_closed()
        except (ssl.SSLError, ConnectionError):
            pass  # the server may close without its own TLS close_notify
        return data

    with app, FakeBroker(tmp_path, uplink_reply(host="uplink.test"), app.fileno()) as broker:
        assert asyncio.run(exchange(broker.client())) == b"PING"
    worker.join(5)
    assert outcome == ["served"]


def test_asyncio_variant_raises_refusals_and_timeouts(tmp_path, monkeypatch):
    reply = {"ok": False, "result": None, "error_code": "app_uplink_off", "error": "app uplink is off"}
    with FakeBroker(tmp_path, reply) as broker:
        with pytest.raises(ResourceBrokerError) as refused:
            asyncio.run(broker.client().open_uplink_connection("api.example.com", 443))
        assert refused.value.code == "app_uplink_off"
    monkeypatch.setattr(resource, "_UPLINK_REPLY_MARGIN_S", 0.05)
    (tmp_path / "silent").mkdir()
    with FakeBroker(tmp_path / "silent", None) as broker:
        with pytest.raises(socket.timeout):
            asyncio.run(broker.client().open_uplink_connection("api.example.com", 443, timeout_s=0.05))
    client = resource.ResourceBrokerClient(lease_id=LEASE, socket_path="/nonexistent/broker.sock")
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(client.open_uplink_connection("api.example.com", 443, limit=0))


def test_cancelled_asyncio_call_closes_a_connection_delivered_later(tmp_path):
    """The abandoned connection is closed explicitly, not left to garbage collection (no ResourceWarning)."""
    app, probe = socket.socketpair()
    hold = threading.Event()

    async def cancel_then_deliver(broker):
        task = asyncio.ensure_future(broker.client().open_uplink_connection("api.example.com", 443))
        while not broker.requests:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hold.set()
        while not broker.sent.is_set():
            await asyncio.sleep(0.01)
        app.close()
        probe.setblocking(False)
        for _ in range(300):
            try:
                if probe.recv(1) == b"":
                    return True
            except BlockingIOError:
                await asyncio.sleep(0.01)
        return False

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with probe, FakeBroker(tmp_path, uplink_reply(), app.fileno(), hold=hold) as broker:
            assert asyncio.run(cancel_then_deliver(broker))
        gc.collect()
    assert [str(item.message) for item in caught if issubclass(item.category, ResourceWarning)] == []


def test_r2kernel_client_exposes_the_uplink_and_not_the_retired_stream(tmp_path, monkeypatch):
    assert not hasattr(resource.ResourceBrokerClient, "open_approved_tcp_stream")
    assert not hasattr(R2KernelClient, "open_approved_tcp_stream")
    app, probe = socket.socketpair()
    with app, probe, FakeBroker(tmp_path, uplink_reply(), app.fileno()) as broker:
        monkeypatch.setenv("R2KERNEL_RESOURCE_LEASE_ID", LEASE)
        monkeypatch.setenv("R2KERNEL_RESOURCE_SERVICE_SOCKET", broker.path)
        client = object.__new__(R2KernelClient)  # the pass-through needs no running Control
        result = client.open_uplink_tcp("api.example.com", 443, timeout_s=4.0)
        result.close()
        assert broker.requests == [{"op": "open_uplink_tcp", "lease_id": LEASE, "host": "api.example.com",
                                    "port": 443, "timeout_ms": 4000}]
    assert asyncio.iscoroutinefunction(R2KernelClient.open_uplink_connection)
