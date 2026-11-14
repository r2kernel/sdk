from __future__ import annotations

import array
import ipaddress
import json
import math
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Re-exported: the inherited-socket helpers belong to the resource API surface.
from .inherited_fds import INHERITED_FDS_ENV, InheritedFdError, encode_inherited_fds, inherited_socket  # noqa: F401

if TYPE_CHECKING:
    import asyncio
    import ssl


DEFAULT_RESOURCE_SOCKET = "/run/r2kernel/resourced.sock"
_MAX_RESOURCE_RESPONSE_BYTES = 1024 * 1024
_RESOURCE_REQUEST_TIMEOUT_S = 5.0
# open_uplink_tcp: the broker may spend timeout_ms resolving and connecting, so the SDK waits timeout_ms plus
# this margin for its reply instead of the generic request timeout.
_UPLINK_REPLY_MARGIN_S = 2.0
# Protocol bounds of an uplink request: timeout_ms is at most the largest max_connect_timeout_ms a host
# accepts, and host is at most 253 bytes.
_UPLINK_MAX_TIMEOUT_MS = 120_000
_UPLINK_MAX_HOST_BYTES = 253
_DNS_NAME_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")
_IPV6_LITERAL_CHARACTERS = frozenset("0123456789abcdef:.")
_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")
_SO_DOMAIN = getattr(socket, "SO_DOMAIN", 39)


def managed_peer_authentication_required() -> bool:
    """Only an explicit SDK development setting disables manager proof."""
    mode = os.environ.get("R2KERNEL_RESOURCE_SERVICE_MODE", "required").strip().lower()
    if mode not in {"required", "auto", "disabled"}:
        raise RuntimeError("Invalid R2KERNEL_RESOURCE_SERVICE_MODE")
    return mode != "disabled"


def verify_control_server_connection(connection: socket.socket, *, deadline: float | None = None) -> None:
    if managed_peer_authentication_required():
        verify_manager_peer_connection(
            connection, os.environ.get("R2KERNEL_RESOURCE_SERVICE_SOCKET") or DEFAULT_RESOURCE_SOCKET,
            deadline=deadline,
        )



def verify_manager_peer_connection(
    connection: socket.socket, broker_socket: str, *, deadline: float | None = None,
) -> None:
    """Authenticate the actual Unix connection peer through the root broker once."""
    if deadline is None:
        deadline = time.monotonic() + 1.0
    def remaining() -> float:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise socket.timeout("manager peer proof deadline exceeded")
        return timeout
    remaining()
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("Managed handler peer authentication requires Linux Unix credentials")
    if not broker_socket:
        raise RuntimeError("Managed handler has no configured root broker socket")
    pid, _uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if pid <= 0:
        raise RuntimeError("Managed handler peer PID is unavailable")
    with open(f"/proc/{pid}/stat", encoding="utf-8") as process:
        start_ticks = int(process.read().rsplit(")", 1)[1].split()[19])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(remaining())
        control.connect(broker_socket)
        _broker_pid, broker_uid, _broker_gid = struct.unpack("3i", control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if broker_uid != 0:
            raise RuntimeError("Managed handler peer proof requires a root-owned broker process")
        request = {"op":"verify_control_manager_peer", "pid":pid, "process_start_ticks":start_ticks}
        control.settimeout(remaining())
        control.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        control.shutdown(socket.SHUT_WR)
        response_bytes = bytearray()
        while True:
            control.settimeout(remaining())
            chunk = control.recv(8192 - len(response_bytes) + 1)
            if not chunk: break
            response_bytes.extend(chunk)
            if len(response_bytes) > 8192:
                raise RuntimeError("Managed handler peer proof exceeds its response limit")
        response = json.loads(response_bytes)
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict) or response["result"].get("authorized") is not True:
            raise RuntimeError("Managed handler rejected a connection without backend manager authority")
        remaining()


class ResourceBrokerError(RuntimeError):
    """The resource broker refused a request.

    ``code`` is the broker's stable ``error_code`` (``None`` when the reply carries none), for example
    ``destination_blocked`` from :meth:`ResourceBrokerClient.open_uplink_tcp`. ``error`` is the broker's text
    verbatim; it is also the exception message. Decide on ``code``, never on the text.
    """

    def __init__(self, code: str | None, error: str):
        super().__init__(error)
        self.code = code
        self.error = error


@dataclass(frozen=True)
class BrokeredSocketResult:
    socket: socket.socket
    metadata: dict[str, Any]

    def close(self) -> None:
        """Release this descriptor; shut down a frontend relay before closing its listener."""
        if self.metadata.get("transport") == "unix_listener":
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.socket.close()


@dataclass(frozen=True)
class BrokeredFdResult:
    fd: int
    metadata: dict[str, Any]

    def close(self) -> None:
        os.close(self.fd)


class ResourceBrokerClient:
    """Client for R2Kernel resource FDs issued by r2kernel-resourced."""

    # Validate a socket inherited from the package leader (R2KERNEL_INHERITED_FDS); no lease or broker request
    # is involved, so it is callable on the class: ResourceBrokerClient.inherited_fd(kind, expect).
    inherited_fd = staticmethod(inherited_socket)

    def __init__(
        self,
        *,
        lease_id: str | None = None,
        socket_path: str | None = None,
    ) -> None:
        self.lease_id = lease_id or os.environ.get("R2KERNEL_RESOURCE_LEASE_ID", "")
        self.socket_path = (
            socket_path
            or os.environ.get("R2KERNEL_RESOURCE_SERVICE_SOCKET")
            or DEFAULT_RESOURCE_SOCKET
        )
        if not self.lease_id:
            raise RuntimeError(
                "R2KERNEL_RESOURCE_LEASE_ID is not set; resource broker access requires r2kernel-launch"
            )

    def open_udp_socket(
        self,
        *,
        interface: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
    ) -> BrokeredSocketResult:
        request = {
            "op": "open_udp_socket",
            "lease_id": self.lease_id,
            "interface": str(interface),
            "bind_host": str(bind_host),
            "bind_port": int(bind_port),
        }
        metadata, fd = self._request_fd(request)
        sock = socket.socket(fileno=fd)
        return BrokeredSocketResult(socket=sock, metadata=metadata)

    def open_uplink_tcp(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        tls: bool = False,
        server_hostname: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> BrokeredSocketResult:
        """Open an outbound TCP connection through the host's app uplink (managed ``role: app`` packages).

        The broker resolves ``host`` (a DNS name or an IP literal without brackets), applies the host's uplink
        policy and connects within ``timeout_s``, which it shortens to the host's ``max_connect_timeout_ms``
        (30 s by default); this call waits ``timeout_s`` plus 2 s for its reply. The
        returned socket is a connected AF_UNIX stream that the broker relays to the TCP connection, so pass it
        to libraries that accept a connected socket. ``metadata`` has ``kind`` (``uplink_tcp``),
        ``connection_id``, ``lease_id``, the normalized ``host``, ``port``, ``remote_address`` and
        ``interface``.

        With ``tls=True`` the socket is wrapped with ``ssl_context`` (default ``ssl.create_default_context()``)
        and the TLS handshake completes within ``timeout_s``; ``server_hostname`` defaults to the normalized
        ``host``. The result's ``socket`` is then the ``ssl.SSLSocket``.

        Invalid arguments raise ``ValueError`` before any request. A broker refusal raises
        :class:`ResourceBrokerError` with its ``code`` (for example ``app_uplink_off`` or
        ``destination_blocked``). A reply that fails the metadata or descriptor checks raises
        ``RuntimeError``; a missing reply raises ``socket.timeout`` (``TimeoutError``). The request is sent once
        and never replayed; every received descriptor is closed when the call raises.
        """
        request = _UplinkRequest.of(host, port, timeout_s, tls, server_hostname, ssl_context)
        opened = self._open_uplink(request)
        if request.ssl_context is None:
            return opened
        raw = opened.socket
        try:
            previous_timeout = raw.gettimeout()
            raw.settimeout(request.timeout_ms / 1000)
            wrapped = request.ssl_context.wrap_socket(raw, server_hostname=request.server_hostname)
            wrapped.settimeout(previous_timeout)
        except BaseException:
            # wrap_socket detaches raw first and closes its own socket when the handshake fails; this
            # closes the descriptor when the failure came before that.
            raw.close()
            raise
        return BrokeredSocketResult(socket=wrapped, metadata=opened.metadata)

    async def open_uplink_connection(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        tls: bool = False,
        server_hostname: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        limit: int = 2**16,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Asyncio variant of :meth:`open_uplink_tcp`: returns ``asyncio.open_connection(sock=...)`` streams.

        The broker request runs in the loop's default executor. With ``tls=True`` asyncio performs the TLS
        handshake (``ssl_handshake_timeout`` is ``timeout_s``). ``limit`` is the StreamReader buffer limit.
        Cancelling the call closes a connection the broker delivers afterwards.
        """
        import asyncio

        request = _UplinkRequest.of(host, port, timeout_s, tls, server_hostname, ssl_context)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        pending = asyncio.get_running_loop().run_in_executor(None, self._open_uplink, request)
        try:
            opened = await asyncio.shield(pending)
        except asyncio.CancelledError:
            pending.add_done_callback(_close_abandoned_uplink)
            raise
        sock = opened.socket
        try:
            if request.ssl_context is None:
                return await asyncio.open_connection(sock=sock, limit=limit)
            return await asyncio.open_connection(
                sock=sock, limit=limit, ssl=request.ssl_context, server_hostname=request.server_hostname,
                ssl_handshake_timeout=request.timeout_ms / 1000,
            )
        except BaseException:
            sock.close()
            raise

    def _open_uplink(self, request: _UplinkRequest) -> BrokeredSocketResult:
        metadata, fd = self._request_fd(
            {"op": "open_uplink_tcp", "lease_id": self.lease_id, "host": request.host, "port": request.port,
             "timeout_ms": request.timeout_ms},
            timeout_s=request.timeout_ms / 1000 + _UPLINK_REPLY_MARGIN_S,
        )
        sock = None
        try:
            try:
                sock = socket.socket(fileno=fd)
            except OSError:
                raise RuntimeError("Invalid app uplink response: the descriptor is not a socket") from None
            problem = _uplink_problem(sock, metadata, lease_id=self.lease_id, request=request)
            if problem is not None:
                raise RuntimeError(f"Invalid app uplink response: {problem}")
            return BrokeredSocketResult(socket=sock, metadata=metadata)
        except BaseException:
            if sock is not None:
                sock.close()
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def open_tcp_socket(
        self,
        *,
        interface: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        connect_host: str | None = None,
        connect_port: int | None = None,
        listen: bool = False,
        backlog: int = 128,
    ) -> BrokeredSocketResult:
        request = {
            "op": "open_tcp_socket",
            "lease_id": self.lease_id,
            "interface": str(interface),
            "bind_host": str(bind_host),
            "bind_port": int(bind_port),
            "connect_host": connect_host,
            "connect_port": connect_port,
            "listen": bool(listen),
            "backlog": int(backlog),
        }
        metadata, fd = self._request_fd(request)
        sock = socket.socket(fileno=fd)
        return BrokeredSocketResult(socket=sock, metadata=metadata)

    def open_frontend_tcp_listener(
        self,
        *,
        kind: str = "api",
        bind_host: str | None = None,
        bind_port: int | None = None,
        backlog: int = 128,
    ) -> BrokeredSocketResult:
        """Return the Unix listener of a broker-owned loopback TCP frontend relay.

        ``metadata.bind_host``/``bind_port`` identify the TCP endpoint. The returned socket accepts
        AF_UNIX streams; call ``BrokeredSocketResult.close()`` when stopping the frontend.
        """
        request = {
            "op": "open_frontend_tcp_listener",
            "lease_id": self.lease_id,
            "kind": str(kind),
            "bind_host": "" if bind_host is None else str(bind_host),
            "bind_port": 0 if bind_port is None else int(bind_port),
            "backlog": int(backlog),
        }
        metadata, fd = self._request_fd(request)
        sock = None
        try:
            sock = socket.socket(fileno=fd)
            if metadata.get("domain") != "unix" or metadata.get("transport") != "unix_listener":
                raise RuntimeError("R2Kernel frontend response must describe a Unix listener relay")
            host, port = metadata.get("bind_host"), metadata.get("bind_port")
            if (host not in ("127.0.0.1", "::1") or isinstance(port, bool)
                    or not isinstance(port, int) or not 1 <= port <= 65535):
                raise RuntimeError("R2Kernel frontend response has an invalid loopback TCP endpoint")
            if (sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_DOMAIN", 39)) != socket.AF_UNIX
                    or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                    or sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
                raise RuntimeError("R2Kernel frontend descriptor must be a listening Unix stream socket")
            return BrokeredSocketResult(socket=sock, metadata=metadata)
        except BaseException:
            if sock is not None:
                sock.close()
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def open_can_socket(
        self,
        *,
        interface: str,
        receive_own_messages: bool = False,
        can_fd_frames: bool = False,
        nonblocking: bool = True,
    ) -> BrokeredSocketResult:
        request = {
            "op": "open_can_socket",
            "lease_id": self.lease_id,
            "interface": str(interface),
            "receive_own_messages": bool(receive_own_messages),
            "can_fd_frames": bool(can_fd_frames),
            "nonblocking": bool(nonblocking),
        }
        metadata, fd = self._request_fd(request)
        sock = socket.socket(fileno=fd)
        return BrokeredSocketResult(socket=sock, metadata=metadata)

    def open_device(
        self,
        path: str,
        *,
        read: bool = True,
        write: bool = True,
        nonblocking: bool = True,
    ) -> BrokeredFdResult:
        request = {
            "op": "open_device",
            "lease_id": self.lease_id,
            "path": str(path),
            "read": bool(read),
            "write": bool(write),
            "nonblocking": bool(nonblocking),
        }
        metadata, fd = self._request_fd(request)
        return BrokeredFdResult(fd=fd, metadata=metadata)

    def _request_fd(self, request: dict[str, Any], *, timeout_s: float | None = None) -> tuple[dict[str, Any], int]:
        # One submission: a failed receive cannot establish whether the broker
        # already performed the operation, so it must never trigger a replay.
        # timeout_s is the operation's absolute budget (default: the generic request timeout).
        deadline = time.monotonic() + (_RESOURCE_REQUEST_TIMEOUT_S if timeout_s is None else timeout_s)

        def remaining() -> float:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise socket.timeout("R2Kernel resource request deadline exceeded")
            return timeout

        fds: list[int] = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            try:
                control.settimeout(remaining())
                control.connect(self.socket_path)
                payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
                control.settimeout(remaining())
                control.sendall(payload)
                response_bytes = bytearray()
                while True:
                    control.settimeout(remaining())
                    data, ancillary, flags, _addr = control.recvmsg(
                        min(8192, _MAX_RESOURCE_RESPONSE_BYTES - len(response_bytes) + 1),
                        socket.CMSG_SPACE(64 * array.array("i").itemsize),
                        getattr(socket, "MSG_CMSG_CLOEXEC", 0),
                    )
                    # Adopt every delivered right before checking the frame,
                    # deadline, truncation flags, or response status.
                    invalid_ancillary = False
                    for level, kind, cdata in ancillary:
                        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                            invalid_ancillary = True
                            continue
                        rights = array.array("i")
                        usable = len(cdata) - len(cdata) % rights.itemsize
                        rights.frombytes(cdata[:usable])
                        fds.extend(rights)
                        invalid_ancillary |= not cdata or usable != len(cdata)
                    if (invalid_ancillary or len(fds) > 1
                            or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)):
                        raise RuntimeError("Invalid R2Kernel resource response ancillary descriptors")
                    for fd in fds:
                        os.set_inheritable(fd, False)
                    remaining()
                    if not data:
                        raise RuntimeError("R2Kernel resource response ended before a complete line")
                    response_bytes.extend(data)
                    if len(response_bytes) > _MAX_RESOURCE_RESPONSE_BYTES:
                        raise RuntimeError("R2Kernel resource response exceeds 1 MiB")
                    newline = data.find(b"\n")
                    if newline < 0:
                        continue
                    if newline != len(data) - 1:
                        raise RuntimeError("R2Kernel resource connection permits exactly one response")
                    response = json.loads(response_bytes[:-1])
                    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                        raise RuntimeError("Malformed R2Kernel resource response")
                    if response["ok"] is not True:
                        error, code = response.get("error"), response.get("error_code")
                        raise ResourceBrokerError(
                            code if isinstance(code, str) and code else None,
                            str(error) if error else "R2Kernel resource request failed",
                        )
                    metadata = response.get("result")
                    if not isinstance(metadata, dict):
                        raise RuntimeError("Malformed R2Kernel resource response result")
                    if len(fds) != 1:
                        raise RuntimeError("R2Kernel resource response did not include a file descriptor")
                    remaining()
                    return metadata, fds.pop()
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


@dataclass(frozen=True)
class _UplinkRequest:
    """Validated arguments of one open_uplink_tcp request; ssl_context is set only with tls."""

    host: str
    port: int
    timeout_ms: int
    ssl_context: ssl.SSLContext | None
    server_hostname: str | None

    @classmethod
    def of(cls, host: object, port: object, timeout_s: object, tls: object, server_hostname: object,
           ssl_context: object) -> _UplinkRequest:
        normalized = _normalized_uplink_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in 1..65535")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s):
            raise ValueError("timeout_s must be a finite number of seconds")
        timeout_ms = round(timeout_s * 1000)
        if not 1 <= timeout_ms <= _UPLINK_MAX_TIMEOUT_MS:
            raise ValueError("timeout_s must be between 0.001 and 120 seconds")
        if not tls:
            if server_hostname is not None or ssl_context is not None:
                raise ValueError("server_hostname and ssl_context require tls=True")
            return cls(normalized, port, timeout_ms, None, None)
        import ssl

        if server_hostname is not None and (not isinstance(server_hostname, str) or not server_hostname):
            raise ValueError("server_hostname must be a nonempty string")
        if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
            raise ValueError("ssl_context must be an ssl.SSLContext")
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        return cls(normalized, port, timeout_ms, context, normalized if server_hostname is None else server_hostname)


def _normalized_uplink_host(host: object) -> str:
    """Return host as the broker normalizes it: lowercase, without one trailing dot."""
    if not isinstance(host, str) or not host.isascii() or not 1 <= len(host) <= _UPLINK_MAX_HOST_BYTES:
        raise ValueError("host must be an ASCII DNS name or IP literal of 1 to 253 bytes")
    name = (host[:-1] if host.endswith(".") else host).lower()
    if ":" in name:
        if not set(name) <= _IPV6_LITERAL_CHARACTERS or not _is_ipv6_literal(name):
            raise ValueError(f"host {host!r} is not an IPv6 literal (write it without brackets or a zone)")
    elif not set(name) <= _DNS_NAME_CHARACTERS or "" in name.split("."):
        raise ValueError(f"host {host!r} is not a DNS name or an IPv4 literal")
    return name


def _is_ipv6_literal(name: str) -> bool:
    try:
        ipaddress.IPv6Address(name)
    except ValueError:
        return False
    return True


def _uplink_problem(sock: socket.socket, metadata: dict[str, Any], *, lease_id: str,
                    request: _UplinkRequest) -> str | None:
    """Name the first failed check of an uplink reply, or None when the reply is valid."""
    connection_id, port = metadata.get("connection_id"), metadata.get("port")
    if metadata.get("kind") != "uplink_tcp":
        return "metadata kind is not uplink_tcp"
    if metadata.get("lease_id") != lease_id:
        return "metadata lease_id is not this client's lease"
    if (not isinstance(connection_id, str) or len(connection_id) != 32
            or not set(connection_id) <= _LOWERCASE_HEX_DIGITS):
        return "metadata connection_id is not 32 lowercase hexadecimal digits"
    if isinstance(port, bool) or not isinstance(port, int) or port != request.port:
        return "metadata port does not match the request"
    if not _same_uplink_host(metadata.get("host"), request.host):
        return "metadata host does not match the request"
    if (sock.getsockopt(socket.SOL_SOCKET, _SO_DOMAIN) != socket.AF_UNIX
            or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
            or sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 0):
        return "the descriptor is not a nonlistening AF_UNIX stream socket"
    try:
        sock.getpeername()
    except OSError:
        return "the descriptor is not connected"
    return None


def _same_uplink_host(reported: object, requested: str) -> bool:
    """The reply names the requested host: the same text, or the same address for an IP literal."""
    if not isinstance(reported, str):
        return False
    if reported == requested:
        return True
    try:
        return ipaddress.ip_address(reported) == ipaddress.ip_address(requested)
    except ValueError:
        return False


def _close_abandoned_uplink(pending: asyncio.Future[BrokeredSocketResult]) -> None:
    """Close a connection the broker delivered after open_uplink_connection was cancelled."""
    if not pending.cancelled() and pending.exception() is None:
        pending.result().close()
