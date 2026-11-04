from __future__ import annotations

import array
import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any

# Re-exported: the inherited-socket helpers belong to the resource API surface.
from .inherited_fds import INHERITED_FDS_ENV, InheritedFdError, encode_inherited_fds, inherited_socket  # noqa: F401


DEFAULT_RESOURCE_SOCKET = "/run/r2kernel/resourced.sock"
_MAX_RESOURCE_RESPONSE_BYTES = 1024 * 1024
_RESOURCE_REQUEST_TIMEOUT_S = 5.0


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

    def open_approved_tcp_stream(self, *, grant_id: str) -> BrokeredSocketResult:
        """Open one root-approved app byte stream; unsupported runtimes refuse.

        The returned AF_UNIX stream supports normal read/write and TLS wrapping.
        The broker retains the TCP connection and shuts the stream down when its
        exact execution or grant expires. Metadata includes the broker's execution_id.
        This operation never opens a NIC claim.
        """
        if not isinstance(grant_id, str) or not grant_id or len(grant_id.encode("utf-8")) > 256:
            raise ValueError("grant_id must be a nonempty identifier of at most 256 bytes")
        metadata, fd = self._request_fd(
            {"op": "open_approved_tcp_stream", "lease_id": self.lease_id, "grant_id": grant_id},
            require_root=True,
        )
        sock = None
        try:
            sock = socket.socket(fileno=fd)
            execution_id = metadata.get("execution_id")
            if (sock.family != socket.AF_UNIX
                    or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                    or sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 0
                    or metadata.get("kind") != "approved_tcp_stream"
                    or metadata.get("grant_id") != grant_id
                    or metadata.get("lease_id") != self.lease_id
                    or not isinstance(execution_id, str) or len(execution_id) != 32
                    or any(char not in "0123456789abcdef" for char in execution_id)):
                raise RuntimeError("Invalid approved TCP stream descriptor or authority metadata")
            try:
                sock.getpeername()
            except OSError:
                raise RuntimeError("Invalid approved TCP stream descriptor or authority metadata") from None
            return BrokeredSocketResult(socket=sock, metadata=metadata)
        except BaseException:
            if sock is None:
                os.close(fd)
            else:
                sock.close()
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

    def _request_fd(self, request: dict[str, Any], *, require_root: bool = False) -> tuple[dict[str, Any], int]:
        # One submission: a failed receive cannot establish whether the broker
        # already performed the operation, so it must never trigger a replay.
        deadline = time.monotonic() + _RESOURCE_REQUEST_TIMEOUT_S

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
                if require_root:
                    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
                        raise RuntimeError("Approved streams require Linux root broker authentication")
                    _pid, uid, _gid = struct.unpack("3i", control.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    if uid != 0:
                        raise RuntimeError("Approved streams require a root-owned broker process")
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
                        raise RuntimeError(response.get("error") or "R2Kernel resource request failed")
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
