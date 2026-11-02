from __future__ import annotations

import json
import os
import random
import select
import socket
import time
from pathlib import Path
from typing import Any

DEFAULT_CONTROL_SOCKET = "/run/r2kernel/control.sock"
# Jittered pause before retrying a connection Control refused as over its admission limits.
ADMISSION_RETRY_DELAY_S = (0.01, 0.05)


class ControlRequestError(RuntimeError):
    """The control backend refused a request.

    ``reason`` is the backend's stable refusal code (empty when the request was rejected before dispatch,
    for example by caller authentication); ``error`` is the backend text verbatim. ``binding`` carries the
    evaluated binding row when the backend returned one.
    """

    def __init__(self, reason: str, error: str, *, binding: dict[str, Any] | None = None):
        super().__init__(f"{reason}: {error}" if reason else error)
        self.reason = reason
        self.error = error
        self.binding = binding


class ControlAdmissionRefused(ControlRequestError):
    """Control's Unix listener refused the connection before reading the request.

    Control sends this refusal (``retryable``) when its connection or request-byte limits are reached, for
    example when many local subsystems start together. ``exchange_control_request`` retries it with a short
    jittered pause within the request's time budget and raises it only when that budget runs out.
    """


def _admission_refusal(data: bytes) -> ControlAdmissionRefused | None:
    try:
        reply = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(reply, dict) and reply.get("status") == "error" and reply.get("retryable") is True:
        return ControlAdmissionRefused(str(reply.get("error_code") or ""), str(reply.get("error") or ""))
    return None


def _refusal_after_failed_send(sock: socket.socket, exc: OSError) -> ControlAdmissionRefused:
    """Control closes a refused connection without reading it, so the send can fail (EPIPE) while
    the refusal is already buffered; return that refusal, or re-raise ``exc`` when there is none."""
    # A peer that closed with our request bytes unread also resets the connection; the
    # refusal it wrote is still returned before that reset.
    chunks: list[bytes] = []
    try:
        sock.settimeout(0.5)
        _read_socket_to_eof(sock, chunks=chunks)
    except OSError:
        pass
    refusal = _admission_refusal(b"".join(chunks))
    if refusal is None:
        raise exc
    return refusal


def _retry_admission(call, budget_deadline: float):
    while True:
        try:
            return call()
        except ControlAdmissionRefused:
            delay = random.uniform(*ADMISSION_RETRY_DELAY_S)
            if time.monotonic() + delay >= budget_deadline:
                raise
            time.sleep(delay)


def control_socket_path() -> str:
    return os.getenv("R2KERNEL_CONTROL_SOCKET") or DEFAULT_CONTROL_SOCKET


def should_use_unix_control() -> bool:
    return True


def exchange_control_request(
    payload: dict[str, Any],
    *,
    control_host: str | None = None,
    control_udp_port: int | None = None,
    timeout_s: float = 1.0,
    retries: int = 3,
    retry_delay_s: float = 0.05,
    prefer_unix: bool | None = None,
    expect_response: bool = True,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    del control_host, control_udp_port, prefer_unix
    if deadline is not None:
        if not expect_response:
            _retry_admission(lambda: _send_unix(payload, timeout_s=_remaining(deadline), deadline=deadline), deadline)
            return None
        return _retry_admission(
            lambda: _exchange_unix(payload, timeout_s=_remaining(deadline), deadline=deadline), deadline,
        )
    last_exc: Exception | None = None
    attempts = max(1, int(retries))
    # Admission refusals are retried within the time the attempts could take, without using an attempt.
    admission_deadline = time.monotonic() + attempts * timeout_s
    for attempt in range(1, attempts + 1):
        try:
            if not expect_response:
                _retry_admission(lambda: _send_unix(payload, timeout_s=timeout_s), admission_deadline)
                return None
            return _retry_admission(lambda: _exchange_unix(payload, timeout_s=timeout_s), admission_deadline)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(max(0.0, retry_delay_s))
    if last_exc is not None:
        raise last_exc
    return None


def _send_unix(payload: dict[str, Any], *, timeout_s: float, deadline: float | None = None) -> None:
    path = control_socket_path()
    if not Path(path).exists():
        raise RuntimeError(
            f"R2Kernel control socket is not available at {path}; "
            "start r2kernel-control or set R2KERNEL_CONTROL_SOCKET"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s if deadline is None else _remaining(deadline))
        sock.connect(path)
        from .resource import verify_control_server_connection
        try:
            if deadline is None:
                verify_control_server_connection(sock)
                sock.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            else:
                verify_control_server_connection(sock, deadline=deadline)
                _send_with_deadline(sock, json.dumps(payload, separators=(",", ":")).encode("utf-8"), deadline)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _refusal_after_failed_send(sock, exc) from exc
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        if deadline is not None:
            _remaining(deadline)


def _exchange_unix(
    payload: dict[str, Any], *, timeout_s: float, deadline: float | None = None,
) -> dict[str, Any] | None:
    path = control_socket_path()
    if not Path(path).exists():
        raise RuntimeError(
            f"R2Kernel control socket is not available at {path}; "
            "start r2kernel-control or set R2KERNEL_CONTROL_SOCKET"
        )
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s if deadline is None else _remaining(deadline))
        sock.connect(path)
        from .resource import verify_control_server_connection
        try:
            if deadline is None:
                verify_control_server_connection(sock)
                sock.sendall(encoded)
            else:
                verify_control_server_connection(sock, deadline=deadline)
                _send_with_deadline(sock, encoded, deadline)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _refusal_after_failed_send(sock, exc) from exc
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        received: list[bytes] = []
        try:
            data = _read_socket_to_eof(sock, deadline=deadline, chunks=received)
        except ConnectionResetError as exc:
            # Control refused the connection and closed it with the request unread.
            refusal = _admission_refusal(b"".join(received))
            if refusal is None:
                raise
            raise refusal from exc
    if deadline is not None:
        _remaining(deadline)
    if not data:
        return None
    refusal = _admission_refusal(data)
    if refusal is not None:
        raise refusal
    response = json.loads(data.decode("utf-8"))
    if deadline is not None:
        _remaining(deadline)
    return response


def _read_socket_to_eof(
    sock: socket.socket, *, deadline: float | None = None, chunks: list[bytes] | None = None,
) -> bytes:
    """Read until EOF; ``chunks`` keeps what arrived when the read then fails."""
    chunks = [] if chunks is None else chunks
    while True:
        chunk = sock.recv(8192) if deadline is None else _deadline_io(sock, deadline, lambda: sock.recv(8192))
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("Control request deadline exceeded")
    return remaining


def _deadline_io(sock: socket.socket, deadline: float, operation, *, writing: bool = False):
    sock.settimeout(0.0)
    while True:
        _remaining(deadline)
        try:
            result = operation()
        except InterruptedError:
            continue
        except BlockingIOError:
            try:
                readable, writable, _ = select.select(
                    [] if writing else [sock], [sock] if writing else [], [], _remaining(deadline),
                )
            except InterruptedError:
                continue
            if not (readable or writable):
                raise socket.timeout("Control request deadline exceeded")
            continue
        _remaining(deadline)
        return result


def _send_with_deadline(sock: socket.socket, payload: bytes, deadline: float) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        sent = _deadline_io(sock, deadline, lambda: sock.send(view[offset:]), writing=True)
        if sent <= 0:
            raise EOFError("Control request socket closed during publication")
        offset += sent
