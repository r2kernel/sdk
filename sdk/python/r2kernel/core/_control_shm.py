"""Anonymous shared-memory primitives for the reusable local Control transport."""

from __future__ import annotations

import math
import os
import select
import socket
import struct
import sys
import time
from array import array
from typing import Any

from ._control_shm_region import ControlShmProtocolError, normalize_control_shm_lease


_FRAME_MAGIC = b"SRLC"
_FRAME_VERSION = 5
_FRAME_KIND_HANDSHAKE_REQUEST = 1
_FRAME_KIND_HANDSHAKE_RESPONSE = 2
_FRAME_KIND_TRANSACT_REQUEST = 3
_FRAME_KIND_TRANSACT_RESPONSE = 4
_FRAME_KIND_RESPONSE_CONSUMED = 5
_FRAME_KIND_RESPONSE_RELEASED = 6
_FRAME_KIND_ERROR = 255
_MAX_LOCAL_CONTROL_FRAME_BYTES = 64 * 1024
_MAX_ANCILLARY_FDS = 64


def _wait_control_shm_ready(
    local_socket: socket.socket, deadline: float, *, writing: bool,
) -> None:
    # select uses a high-resolution timeval instead of CPython's millisecond
    # poll timeout. Always try nonblocking I/O before reaching this wait.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("local Control v5 frame deadline expired")
        try:
            readable, writable, _ = select.select(
                [] if writing else [local_socket],
                [local_socket] if writing else [],
                [],
                remaining,
            )
        except InterruptedError:
            continue
        if not (readable or writable) or time.monotonic() >= deadline:
            raise socket.timeout("local Control v5 frame deadline expired")
        return


def send_control_shm_frame(
    local_socket: socket.socket,
    payload: bytes,
    file_descriptor: int | None = None,
    *,
    deadline: float | None = None,
) -> None:
    frame = bytes(payload)
    if len(frame) < 6 or len(frame) > _MAX_LOCAL_CONTROL_FRAME_BYTES:
        raise ValueError("local Control v5 frame size is invalid")
    prefix = struct.pack("<I", len(frame))
    previous_timeout = local_socket.gettimeout()
    send_deadline = deadline
    if send_deadline is None and previous_timeout is not None and previous_timeout > 0:
        # sendall's timeout bounds the entire remaining write, not each syscall.
        send_deadline = time.monotonic() + previous_timeout
    prefix_view = memoryview(prefix)
    frame_view = memoryview(frame)
    ancillary = (
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [int(file_descriptor)]))]
        if file_descriptor is not None
        else []
    )

    def check_deadline() -> None:
        if send_deadline is not None and time.monotonic() >= send_deadline:
            raise socket.timeout("local Control v5 frame send deadline expired")

    try:
        if send_deadline is not None:
            local_socket.settimeout(0.0)
        sent_bytes = 0
        total_bytes = len(prefix) + len(frame)
        while sent_bytes < total_bytes:
            if sent_bytes < len(prefix):
                buffers = [prefix_view if sent_bytes == 0 else prefix_view[sent_bytes:], frame_view]
            else:
                buffers = [frame_view[sent_bytes - len(prefix):]]
            check_deadline()
            try:
                sent = local_socket.sendmsg(buffers, ancillary)
            except InterruptedError:
                continue
            except BlockingIOError:
                if send_deadline is None:
                    raise
                _wait_control_shm_ready(local_socket, send_deadline, writing=True)
                continue
            if sent <= 0:
                if file_descriptor is not None:
                    raise EOFError("local Control v5 socket closed while sending an attached descriptor")
                raise EOFError("local Control v5 socket closed while sending a frame")
            sent_bytes += sent
            # SCM_RIGHTS belongs to the first prefix byte, exactly once. An
            # interrupted attempt without progress must retain the descriptor.
            ancillary.clear()
            check_deadline()
    finally:
        local_socket.settimeout(previous_timeout)


def receive_control_shm_frame(
    local_socket: socket.socket,
    *,
    allow_idle_prefix_wait: bool = False,
    phase: str = "frame",
    deadline: float | None = None,
) -> tuple[bytes | None, list[int]]:
    ancillary_size = socket.CMSG_SPACE(_MAX_ANCILLARY_FDS * array("i").itemsize)
    recv_flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    previous_timeout = local_socket.gettimeout()
    read_started = time.monotonic()
    explicit_deadline = deadline is not None
    if deadline is None and previous_timeout is not None and previous_timeout > 0:
        deadline = read_started + previous_timeout

    def receive(size: int) -> tuple[bytes, list[tuple[int, int, bytes]], int]:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise socket.timeout("local Control v5 frame receive deadline expired")
            try:
                chunk, ancillary, message_flags, _ = local_socket.recvmsg(size, ancillary_size, recv_flags)
            except InterruptedError:
                continue
            except BlockingIOError:
                if deadline is None:
                    raise
                _wait_control_shm_ready(local_socket, deadline, writing=False)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                close_control_shm_descriptors(_extract_rights(ancillary))
                raise socket.timeout("local Control v5 frame receive deadline expired")
            break
        return chunk, ancillary, message_flags

    file_descriptors: list[int] = []
    try:
        if deadline is not None:
            local_socket.settimeout(0.0)
        try:
            prefix, ancillary, message_flags = receive(4)
        except socket.timeout as exc:
            # Preserve timeout classification when no frame byte was received.
            raise socket.timeout(
                "timed out while reading a local Control v5 frame"
                f" phase={phase} section=prefix expected_bytes=4 received_bytes=0"
                f" configured_timeout_s={previous_timeout}"
                f" elapsed_s={time.monotonic() - read_started}"
            ) from exc
        if (
            allow_idle_prefix_wait and not explicit_deadline
            and previous_timeout is not None and previous_timeout > 0 and prefix
        ):
            # Idle waiting and an in-progress frame have separate bounded
            # budgets, unless the caller supplied one absolute deadline.
            read_started = time.monotonic()
            deadline = read_started + previous_timeout
        prefix_elapsed = time.monotonic() - read_started
        file_descriptors = _extract_rights(ancillary)
        if message_flags & getattr(socket, "MSG_CTRUNC", 0):
            raise ControlShmProtocolError("local Control v5 descriptor list was truncated")
        if message_flags & getattr(socket, "MSG_TRUNC", 0):
            raise ControlShmProtocolError("local Control v5 frame prefix was truncated")
        if not prefix:
            if file_descriptors:
                raise ControlShmProtocolError("local Control v5 socket closed with an attached descriptor")
            return None, []
        if len(prefix) < 4:
            prefix += _read_socket_exact_without_descriptors(
                local_socket, 4 - len(prefix), receive, phase=phase,
                section="prefix", already_received=len(prefix),
                read_started=read_started, configured_timeout=previous_timeout,
                prefix_elapsed=prefix_elapsed,
            )
        frame_size = struct.unpack("<I", prefix)[0]
        if frame_size < 6 or frame_size > _MAX_LOCAL_CONTROL_FRAME_BYTES:
            raise ControlShmProtocolError("local Control v5 response frame size is invalid")
        frame = _read_socket_exact_without_descriptors(
            local_socket, frame_size, receive, phase=phase, section="body",
            read_started=read_started, configured_timeout=previous_timeout,
            prefix_elapsed=prefix_elapsed,
        )
        return frame, file_descriptors
    except Exception:
        close_control_shm_descriptors(file_descriptors)
        raise
    finally:
        local_socket.settimeout(previous_timeout)


def close_control_shm_descriptors(file_descriptors: list[int]) -> None:
    for file_descriptor in file_descriptors:
        try:
            os.close(file_descriptor)
        except OSError:
            pass


def _extract_rights(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    file_descriptors: list[int] = []
    unsupported_ancillary = False
    malformed_rights = False
    for level, message_type, data in ancillary:
        if level != socket.SOL_SOCKET or message_type != socket.SCM_RIGHTS:
            unsupported_ancillary = True
            continue
        rights = array("i")
        usable_length = len(data) - (len(data) % rights.itemsize)
        malformed_rights = malformed_rights or usable_length != len(data)
        rights.frombytes(data[:usable_length])
        file_descriptors.extend(int(descriptor) for descriptor in rights)
    if unsupported_ancillary or malformed_rights:
        close_control_shm_descriptors(file_descriptors)
        raise ControlShmProtocolError("local Control v5 frame has malformed ancillary data")
    try:
        for file_descriptor in file_descriptors:
            os.set_inheritable(file_descriptor, False)
    except Exception:
        close_control_shm_descriptors(file_descriptors)
        raise
    return file_descriptors


def _queued_socket_bytes_on_failure(local_socket: socket.socket) -> tuple[int | None, int | None]:
    """Failure-only nonconsuming queue observation; no frame or descriptor read."""
    import errno
    if not sys.platform.startswith("linux"):
        return None, errno.ENOTSUP
    try:
        import fcntl
        import termios
        queued = array("i", [0])
        fcntl.ioctl(local_socket.fileno(), termios.FIONREAD, queued, True)
        return queued[0], 0
    except (OSError, ImportError, AttributeError) as error:
        return None, getattr(error, "errno", None) or errno.ENOTSUP


def _read_socket_exact_without_descriptors(
    local_socket: socket.socket,
    size: int,
    receive: Any | None = None,
    *,
    phase: str = "frame",
    section: str = "body",
    already_received: int = 0,
    read_started: float | None = None,
    configured_timeout: float | None = None,
    prefix_elapsed: float | None = None,
) -> bytes:
    data = bytearray()
    while len(data) < size:
        try:
            if receive is None:
                chunk, ancillary, message_flags, _ = local_socket.recvmsg(
                    size - len(data),
                    socket.CMSG_SPACE(_MAX_ANCILLARY_FDS * array("i").itemsize),
                    getattr(socket, "MSG_CMSG_CLOEXEC", 0),
                )
            else:
                chunk, ancillary, message_flags = receive(size - len(data))
        except socket.timeout as exc:
            elapsed = time.monotonic() - read_started if read_started is not None else None
            queued_bytes, queued_errno = _queued_socket_bytes_on_failure(local_socket)
            # Failure-only numeric context: never retain frame bytes, tokens,
            # descriptors, keys or subsystem identities.
            raise ControlShmProtocolError(
                "timed out while reading a partial local Control v5 frame"
                f" phase={phase} section={section}"
                f" expected_bytes={size + already_received}"
                f" received_bytes={len(data) + already_received}"
                f" configured_timeout_s={configured_timeout} elapsed_s={elapsed}"
                f" prefix_elapsed_s={prefix_elapsed}"
                f" queued_bytes={queued_bytes} queued_bytes_errno={queued_errno}"
            ) from exc
        received_fds = _extract_rights(ancillary)
        if message_flags & getattr(socket, "MSG_CTRUNC", 0) or received_fds:
            close_control_shm_descriptors(received_fds)
            raise ControlShmProtocolError("local Control v5 descriptor is attached outside the frame prefix")
        if message_flags & getattr(socket, "MSG_TRUNC", 0):
            raise ControlShmProtocolError("local Control v5 frame data was truncated")
        if not chunk:
            raise EOFError("truncated local Control v5 socket frame")
        data.extend(chunk)
    return bytes(data)


def encode_prepare_request(
    target: str, host: str, port: int, topic: str, timeout_ms: int,
    request_capacity: int, response_capacity: int, payload_lease: dict[str, Any],
) -> bytes:
    lease = normalize_control_shm_lease(payload_lease)
    if lease["length"] != 0:
        raise ControlShmProtocolError("preparation request lease is not empty")
    output = bytearray(_FRAME_MAGIC + bytes((_FRAME_VERSION, 7)))
    _push_string(output, target)
    _push_string(output, host)
    _push_u16(output, port)
    _push_string(output, topic)
    _push_u32(output, timeout_ms)
    _push_u32(output, request_capacity)
    _push_u32(output, response_capacity)
    _push_lease(output, lease)
    return bytes(output)


def decode_prepare_response(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, 8)
    ok, position = _read_u8(payload, position)
    error, position = _read_string(payload, position)
    lease, position = _read_lease(payload, position)
    if (ok not in (0, 1) or position != len(payload) or
            (ok and (error or lease is None or lease["length"] != 0)) or
            (not ok and lease is not None)):
        raise ControlShmProtocolError("invalid local Control v5 preparation response")
    return {"ok": bool(ok), "error": error, "payload_lease": lease}


def decode_handler_prepare_request(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, 13)
    sender, position = _read_string(payload, position)
    topic, position = _read_string(payload, position)
    request_capacity, position = _read_u32(payload, position)
    response_capacity, position = _read_u32(payload, position)
    remaining_ns, position = _read_u64(payload, position)
    if remaining_ns == 0:
        raise ControlShmProtocolError("handler preparation remaining time must be positive")
    lease, position = _read_lease(payload, position)
    if lease is None or lease["length"] != 0 or position != len(payload):
        raise ControlShmProtocolError("invalid local Control v5 handler preparation request")
    return {"kind": "prepare", "sender": sender, "topic": topic,
            "request_capacity": request_capacity, "response_capacity": response_capacity,
            "payload_lease": lease, "remaining_ns": remaining_ns}


def encode_handler_prepare_response(payload_lease: dict[str, Any]) -> bytes:
    lease = normalize_control_shm_lease(payload_lease)
    if lease["length"] != 0:
        raise ControlShmProtocolError("preparation response lease is not empty")
    output = bytearray(_FRAME_MAGIC + bytes((_FRAME_VERSION, 14)))
    _push_lease(output, lease)
    return bytes(output)


def encode_handshake_request(target: str, host: str, port: int, timeout_ms: int) -> bytes:
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, _FRAME_KIND_HANDSHAKE_REQUEST)
    _push_string(output, target)
    _push_string(output, host)
    _push_u16(output, int(port))
    _push_u32(output, int(timeout_ms))
    return bytes(output)


def encode_transact_request(
    *,
    target: str,
    host: str,
    port: int,
    topic: str,
    timestamp_ns: int,
    period_ms: float,
    deadline_ms: float,
    timeout_ms: int,
    priority: int,
    payload_lease: dict[str, Any],
    remaining_ns: int | None = None,
) -> bytes:
    lease = normalize_control_shm_lease(payload_lease)
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, _FRAME_KIND_TRANSACT_REQUEST)
    _push_string(output, target)
    _push_string(output, host)
    _push_u16(output, int(port))
    _push_string(output, topic)
    _push_i64(output, int(timestamp_ns))
    _push_f32(output, float(period_ms))
    _push_f32(output, float(deadline_ms))
    _push_u32(output, int(timeout_ms))
    _push_u8(output, int(priority))
    _push_u64(output, int(timeout_ms) * 1_000_000 if remaining_ns is None else remaining_ns)
    _push_lease(output, lease)
    return bytes(output)


def decode_handshake_response(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, _FRAME_KIND_HANDSHAKE_RESPONSE)
    ok, position = _read_u8(payload, position)
    if ok not in (0, 1):
        raise ControlShmProtocolError("local Control v5 handshake status is invalid")
    error, position = _read_string(payload, position)
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 handshake response has trailing bytes")
    if ok and error:
        raise ControlShmProtocolError("successful local Control v5 handshake contains an error message")
    return {"ok": bool(ok), "error": error}


def decode_transact_response(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, _FRAME_KIND_TRANSACT_RESPONSE)
    ok, position = _read_u8(payload, position)
    if ok not in (0, 1):
        raise ControlShmProtocolError("local Control v5 transaction status is invalid")
    status, position = _read_u16(payload, position)
    timestamp_ns, position = _read_i64(payload, position)
    error, position = _read_string(payload, position)
    lease, position = _read_lease(payload, position)
    response_ticket = None
    if ok and error:
        raise ControlShmProtocolError("successful local Control v5 response contains an error message")
    if ok and lease is None:
        raise ControlShmProtocolError("successful local Control v5 response omitted its shared-memory lease")
    if not ok and lease is not None:
        raise ControlShmProtocolError("failed local Control v5 response unexpectedly contains a lease")
    if ok:
        response_ticket, position = _read_u64(payload, position)
        if response_ticket == 0:
            raise ControlShmProtocolError("local Control v5 response ticket must be positive")
    else:
        empty_payload, position = _read_bytes(payload, position)
        if empty_payload:
            raise ControlShmProtocolError("failed local Control v5 response contains an unexpected inline payload")
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 transaction response has trailing bytes")
    return {
        "ok": bool(ok),
        "status": status,
        "timestamp_ns": timestamp_ns,
        "error": error,
        "payload_lease": lease,
        "response_ticket": response_ticket,
    }


def encode_response_consumed(response_ticket: int, payload_lease: dict[str, Any]) -> bytes:
    lease = normalize_control_shm_lease(payload_lease)
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, _FRAME_KIND_RESPONSE_CONSUMED)
    _push_u64(output, response_ticket)
    _push_lease(output, lease)
    return bytes(output)


def decode_response_consumed(payload: bytes) -> tuple[int, dict[str, Any]]:
    position = _validate_header(payload, _FRAME_KIND_RESPONSE_CONSUMED)
    response_ticket, position = _read_u64(payload, position)
    lease, position = _read_lease(payload, position)
    if response_ticket == 0 or lease is None:
        raise ControlShmProtocolError("local Control v5 consumed frame has an invalid ticket or lease")
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 consumed frame has trailing bytes")
    return response_ticket, lease


def decode_response_released(payload: bytes) -> int:
    position = _validate_header(payload, _FRAME_KIND_RESPONSE_RELEASED)
    response_ticket, position = _read_u64(payload, position)
    if response_ticket == 0:
        raise ControlShmProtocolError("local Control v5 released ticket must be positive")
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 released frame has trailing bytes")
    return response_ticket


def encode_response_released(response_ticket: int) -> bytes:
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, _FRAME_KIND_RESPONSE_RELEASED)
    _push_u64(output, response_ticket)
    return bytes(output)


def decode_error_response(payload: bytes) -> str:
    position = _validate_header(payload, _FRAME_KIND_ERROR, allow_error=True)
    message, position = _read_string(payload, position)
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 error response has trailing bytes")
    return message


def local_control_frame_kind(payload: bytes) -> int:
    if len(payload) < 6 or payload[:4] != _FRAME_MAGIC or payload[4] != _FRAME_VERSION:
        raise ControlShmProtocolError("invalid local Control v5 frame header")
    return payload[5]


def encode_error_response(message: str) -> bytes:
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, _FRAME_KIND_ERROR)
    _push_string(output, message)
    return bytes(output)


def decode_handler_request(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, 11)
    sender, position = _read_string(payload, position)
    topic, position = _read_string(payload, position)
    timestamp_ns, position = _read_i64(payload, position)
    period_ms, position = _read_f32(payload, position)
    deadline_ms, position = _read_f32(payload, position)
    if not math.isfinite(period_ms) or not math.isfinite(deadline_ms):
        raise ControlShmProtocolError("local Control v5 handler timing value is not finite")
    remaining_ns, position = _read_u64(payload, position)
    if remaining_ns == 0:
        raise ControlShmProtocolError("handler remaining time must be positive")
    payload_lease, position = _read_lease(payload, position)
    if payload_lease is None:
        raise ControlShmProtocolError("local Control v5 handler request omitted its shared-memory lease")
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 handler request has trailing bytes")
    return {
        "kind": "request",
        "sender": sender,
        "topic": topic,
        "timestamp_ns": timestamp_ns,
        "period_ms": period_ms,
        "deadline_ms": deadline_ms,
        "remaining_ns": remaining_ns,
        "payload_lease": payload_lease,
    }


def encode_handler_response(
    status: int,
    timestamp_ns: int,
    payload_lease: dict[str, Any],
) -> bytes:
    lease = normalize_control_shm_lease(payload_lease)
    output = bytearray(_FRAME_MAGIC)
    _push_u8(output, _FRAME_VERSION)
    _push_u8(output, 12)
    _push_u16(output, int(status))
    _push_i64(output, int(timestamp_ns))
    _push_lease(output, lease)
    return bytes(output)


def decode_handler_response(payload: bytes) -> dict[str, Any]:
    position = _validate_header(payload, 12)
    status, position = _read_u16(payload, position)
    timestamp_ns, position = _read_i64(payload, position)
    payload_lease, position = _read_lease(payload, position)
    if payload_lease is None:
        raise ControlShmProtocolError("successful local Control v5 handler response omitted its lease")
    if position != len(payload):
        raise ControlShmProtocolError("local Control v5 handler response has trailing bytes")
    return {"status": status, "timestamp_ns": timestamp_ns, "payload_lease": payload_lease}


def _validate_header(payload: bytes, expected_kind: int, *, allow_error: bool = False) -> int:
    if len(payload) < 6 or len(payload) > _MAX_LOCAL_CONTROL_FRAME_BYTES:
        raise ControlShmProtocolError("local Control v5 response frame size is invalid")
    if payload[:4] != _FRAME_MAGIC or payload[4] != _FRAME_VERSION:
        raise ControlShmProtocolError("invalid local Control v5 response header")
    kind = payload[5]
    if kind == _FRAME_KIND_ERROR and not allow_error:
        message, position = _read_string(payload, 6)
        if position != len(payload):
            raise ControlShmProtocolError("local Control v5 error response has trailing bytes")
        raise ControlShmProtocolError(message or "runtime returned a local Control v5 error")
    if kind != expected_kind:
        raise ControlShmProtocolError(f"unexpected local Control v5 response kind: {kind}")
    return 6


def _push_u8(output: bytearray, value: int) -> None:
    number = int(value)
    if number < 0 or number > 0xFF:
        raise ValueError("local Control v5 u8 value is out of range")
    output.extend(struct.pack("<B", number))


def _push_u16(output: bytearray, value: int) -> None:
    number = int(value)
    if number < 0 or number > 0xFFFF:
        raise ValueError("local Control v5 u16 value is out of range")
    output.extend(struct.pack("<H", number))


def _push_u32(output: bytearray, value: int) -> None:
    number = int(value)
    if number < 0 or number > 0xFFFFFFFF:
        raise ValueError("local Control v5 u32 value is out of range")
    output.extend(struct.pack("<I", number))


def _push_u64(output: bytearray, value: int) -> None:
    number = int(value)
    if number < 0 or number > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("local Control v5 u64 value is out of range")
    output.extend(struct.pack("<Q", number))


def _push_i64(output: bytearray, value: int) -> None:
    number = int(value)
    if number < -(2**63) or number >= 2**63:
        raise ValueError("local Control v5 i64 value is out of range")
    output.extend(struct.pack("<q", number))


def _push_f32(output: bytearray, value: float) -> None:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("local Control v5 float value must be finite")
    output.extend(struct.pack("<f", number))


def _push_bytes(output: bytearray, value: bytes) -> None:
    data = bytes(value)
    if len(data) > _MAX_LOCAL_CONTROL_FRAME_BYTES:
        raise ValueError("local Control v5 field exceeds the frame size limit")
    _push_u32(output, len(data))
    output.extend(data)


def _push_string(output: bytearray, value: str) -> None:
    _push_bytes(output, str(value).encode("utf-8"))


def _push_lease(output: bytearray, lease: dict[str, Any] | None) -> None:
    if lease is None:
        _push_u8(output, 0)
        return
    _push_u8(output, 1)
    _push_string(output, lease["region_name"])
    _push_u32(output, lease["slot_index"])
    _push_u32(output, lease["slot_size"])
    _push_u32(output, lease["slot_count"])
    _push_u32(output, lease["length"])


def _read_exact(payload: bytes, position: int, size: int) -> tuple[bytes, int]:
    if size < 0 or size > _MAX_LOCAL_CONTROL_FRAME_BYTES:
        raise ControlShmProtocolError("local Control v5 field size is invalid")
    end = position + size
    if end > len(payload):
        raise ControlShmProtocolError("truncated local Control v5 response")
    return payload[position:end], end


def _read_u8(payload: bytes, position: int) -> tuple[int, int]:
    raw, position = _read_exact(payload, position, 1)
    return raw[0], position


def _read_u16(payload: bytes, position: int) -> tuple[int, int]:
    raw, position = _read_exact(payload, position, 2)
    return struct.unpack("<H", raw)[0], position


def _read_u32(payload: bytes, position: int) -> tuple[int, int]:
    raw, position = _read_exact(payload, position, 4)
    return struct.unpack("<I", raw)[0], position


def _read_u64(payload: bytes, position: int) -> tuple[int, int]:
    raw, position = _read_exact(payload, position, 8)
    return struct.unpack("<Q", raw)[0], position


def _read_i64(payload: bytes, position: int) -> tuple[int, int]:
    raw, position = _read_exact(payload, position, 8)
    return struct.unpack("<q", raw)[0], position


def _read_f32(payload: bytes, position: int) -> tuple[float, int]:
    raw, position = _read_exact(payload, position, 4)
    return struct.unpack("<f", raw)[0], position


def _read_bytes(payload: bytes, position: int) -> tuple[bytes, int]:
    length, position = _read_u32(payload, position)
    return _read_exact(payload, position, length)


def _read_string(payload: bytes, position: int) -> tuple[str, int]:
    raw, position = _read_bytes(payload, position)
    try:
        return raw.decode("utf-8"), position
    except UnicodeDecodeError as exc:
        raise ControlShmProtocolError("invalid UTF-8 in local Control v5 response") from exc


def _read_lease(payload: bytes, position: int) -> tuple[dict[str, Any] | None, int]:
    present, position = _read_u8(payload, position)
    if present == 0:
        return None, position
    if present != 1:
        raise ControlShmProtocolError("local Control v5 lease flag is invalid")
    token, position = _read_string(payload, position)
    slot_index, position = _read_u32(payload, position)
    slot_size, position = _read_u32(payload, position)
    slot_count, position = _read_u32(payload, position)
    length, position = _read_u32(payload, position)
    lease = normalize_control_shm_lease(
        {
            "region_name": token,
            "slot_index": slot_index,
            "slot_size": slot_size,
            "slot_count": slot_count,
            "length": length,
        }
    )
    return lease, position
