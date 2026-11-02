from __future__ import annotations

from array import array
import os
import socket
import struct
import tempfile
import threading
import time
from collections import deque
from typing import Callable

import pytest

from r2kernel.core import _control_shm as control_shm


pytestmark = pytest.mark.skipif(os.name == "nt", reason="SCM_RIGHTS requires Unix sockets")


class _BoundedSocketWriter:
    """Limit real sendmsg calls while exposing their untouched source views."""

    def __init__(
        self,
        local_socket: socket.socket,
        outcomes: list[int | BaseException] | None = None,
        after_send: Callable[[], None] | None = None,
    ) -> None:
        self.socket = local_socket
        self.outcomes = deque(outcomes or [])
        self.after_send = after_send
        self.calls: list[tuple[tuple[memoryview, ...], bool, int]] = []
        self.timeouts: list[float | None] = []

    def fileno(self) -> int:
        return self.socket.fileno()

    def gettimeout(self) -> float | None:
        return self.socket.gettimeout()

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)
        self.socket.settimeout(value)

    def sendmsg(self, buffers: list[memoryview], ancillary: list[tuple]) -> int:
        assert all(isinstance(buffer, memoryview) for buffer in buffers)
        outcome = self.outcomes.popleft() if self.outcomes else sum(map(len, buffers))
        if isinstance(outcome, BaseException):
            self.calls.append((tuple(buffers), bool(ancillary), 0))
            raise outcome
        if outcome == 0:
            self.calls.append((tuple(buffers), bool(ancillary), 0))
            return 0
        limited_buffers = []
        remaining = outcome
        for buffer in buffers:
            if remaining <= 0:
                break
            limited_buffers.append(buffer[:remaining])
            remaining -= len(limited_buffers[-1])
        sent = self.socket.sendmsg(limited_buffers, ancillary)
        self.calls.append((tuple(buffers), bool(ancillary), sent))
        if self.after_send is not None:
            self.after_send()
        return sent


def _check_received_descriptor(descriptors: list[int], expected_count: int) -> None:
    try:
        assert len(descriptors) == expected_count
        if descriptors:
            assert not os.get_inheritable(descriptors[0])
            assert os.pread(descriptors[0], 10, 0) == b"descriptor"
    finally:
        control_shm.close_control_shm_descriptors(descriptors)
        descriptors.clear()


@pytest.mark.parametrize("with_descriptor", [False, True])
def test_complete_frame_uses_one_sendmsg_with_two_source_views(with_descriptor: bool) -> None:
    frame = control_shm.encode_error_response("single vectored write")
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        sender, receiver = socket.socketpair()
        try:
            writer = _BoundedSocketWriter(sender)
            control_shm.send_control_shm_frame(
                writer, frame, descriptor_file.fileno() if with_descriptor else None
            )
            assert len(writer.calls) == 1
            buffers, has_rights, sent = writer.calls[0]
            assert len(buffers) == 2
            assert bytes(buffers[0]) == struct.pack("<I", len(frame))
            assert buffers[1].obj is frame
            assert sent == 4 + len(frame)
            assert has_rights is with_descriptor
            received, descriptors = control_shm.receive_control_shm_frame(receiver)
            _check_received_descriptor(descriptors, int(with_descriptor))
            assert received == frame
        finally:
            sender.close()
            receiver.close()


@pytest.mark.parametrize("first_positive_bytes", [1, 2, 3, 4, 5, 17])
@pytest.mark.parametrize("with_descriptor", [False, True])
def test_partial_prefix_body_cursor_and_interrupted_attempts(
    first_positive_bytes: int, with_descriptor: bool
) -> None:
    frame = control_shm.encode_error_response("partial prefix and body cursor")
    wire = struct.pack("<I", len(frame)) + frame
    sender, receiver = socket.socketpair()
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        try:
            writer = _BoundedSocketWriter(
                sender, [InterruptedError(), first_positive_bytes, InterruptedError(), 2]
            )
            control_shm.send_control_shm_frame(
                writer, frame, descriptor_file.fileno() if with_descriptor else None
            )
            cursor = 0
            for buffers, has_rights, sent in writer.calls:
                assert b"".join(buffers) == wire[cursor:]
                assert has_rights == (with_descriptor and cursor == 0)
                assert len(buffers) == (2 if cursor < 4 else 1)
                assert buffers[-1].obj is frame
                cursor += sent
            assert cursor == len(wire)
            received, descriptors = control_shm.receive_control_shm_frame(receiver)
            _check_received_descriptor(descriptors, int(with_descriptor))
            assert received == frame
        finally:
            sender.close()
            receiver.close()


def _receive_exact(local_socket: socket.socket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = local_socket.recv(size - len(output))
        if not chunk:
            raise EOFError("legacy peer received a truncated frame")
        output.extend(chunk)
    return bytes(output)


def test_new_writer_and_legacy_split_prefix_body_peer_are_wire_compatible() -> None:
    frame = control_shm.encode_error_response("legacy peer")
    sender, receiver = socket.socketpair()
    try:
        sender.settimeout(1.0)
        receiver.settimeout(1.0)
        control_shm.send_control_shm_frame(sender, frame)
        assert struct.unpack("<I", _receive_exact(receiver, 4))[0] == len(frame)
        assert _receive_exact(receiver, len(frame)) == frame
        receiver.sendall(struct.pack("<I", len(frame)))
        receiver.sendall(frame)
        received, descriptors = control_shm.receive_control_shm_frame(sender)
        assert descriptors == []
        assert received == frame
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("with_descriptor", [False, True])
def test_maximum_frame_completes_under_real_backpressure(with_descriptor: bool) -> None:
    frame = b"SRLC\x05\xff" + b"x" * (control_shm._MAX_LOCAL_CONTROL_FRAME_BYTES - 6)
    sender, receiver = socket.socketpair()
    received_frames = []
    peer_errors = []

    def read_peer() -> None:
        try:
            received_frames.append(control_shm.receive_control_shm_frame(receiver))
        except BaseException as error:
            peer_errors.append(error)

    thread = threading.Thread(target=read_peer)
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        try:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            receiver.settimeout(2.0)
            thread.start()
            writer = _BoundedSocketWriter(sender, [8192])
            control_shm.send_control_shm_frame(
                writer,
                frame,
                descriptor_file.fileno() if with_descriptor else None,
                deadline=time.monotonic() + 2.0,
            )
            thread.join(3.0)
            assert not thread.is_alive()
            assert peer_errors == []
            assert len(received_frames) == 1
            received, descriptors = received_frames[0]
            _check_received_descriptor(descriptors, int(with_descriptor))
            assert received == frame
            assert len(writer.calls) > 1
            assert sender.gettimeout() is None
        finally:
            sender.close()
            receiver.close()
            if thread.ident is not None:
                thread.join(3.0)
            # Close any rights even if an earlier assertion prevented checking.
            for _, descriptors in received_frames:
                if descriptors:
                    control_shm.close_control_shm_descriptors(descriptors)


@pytest.mark.parametrize("explicit_deadline", [False, True])
def test_backpressure_timeout_restores_socket_setting(explicit_deadline: bool) -> None:
    frame = b"SRLC\x05\xff" + b"x" * (control_shm._MAX_LOCAL_CONTROL_FRAME_BYTES - 6)
    sender, receiver = socket.socketpair()
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        previous_timeout = None if explicit_deadline else 0.05
        sender.settimeout(previous_timeout)
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            control_shm.send_control_shm_frame(
                sender, frame, deadline=started + 0.05 if explicit_deadline else None
            )
        assert time.monotonic() - started < 1.0
        assert sender.gettimeout() == previous_timeout
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("with_descriptor", [False, True])
def test_socket_timeout_is_one_budget_even_for_descriptor_suffix(
    monkeypatch: pytest.MonkeyPatch, with_descriptor: bool
) -> None:
    clock = [0.0]
    increments = iter([0.75, 0.85, 0.05])
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        try:
            sender.settimeout(1.0)
            writer = _BoundedSocketWriter(
                sender, [2, 2], after_send=lambda: clock.__setitem__(0, clock[0] + next(increments))
            )
            frame = control_shm.encode_error_response("legacy timeout")
            with pytest.raises(socket.timeout):
                control_shm.send_control_shm_frame(
                    writer, frame, descriptor_file.fileno() if with_descriptor else None,
                )
            assert len(writer.calls) == 2
            assert writer.timeouts == [0.0, 1.0]
            assert sender.gettimeout() == 1.0
            prefix = bytearray()
            descriptors = []
            while len(prefix) < 4:
                chunk, ancillary, _, _ = receiver.recvmsg(
                    4 - len(prefix), socket.CMSG_SPACE(struct.calcsize("i")),
                )
                assert chunk
                prefix.extend(chunk)
                descriptors.extend(control_shm._extract_rights(ancillary))
            assert prefix == struct.pack("<I", len(frame))
            _check_received_descriptor(descriptors, int(with_descriptor))
        finally:
            sender.close()
            receiver.close()


@pytest.mark.parametrize("previous_timeout", [None, 0.0, 1.25])
@pytest.mark.parametrize("failure", [InterruptedError, BlockingIOError, BrokenPipeError, socket.timeout])
def test_send_errors_restore_previous_timeout(
    previous_timeout: float | None, failure: type[BaseException]
) -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.settimeout(previous_timeout)
        # Interrupted and would-block attempts retry; a later pipe error surfaces.
        outcomes = [failure()]
        if failure in (InterruptedError, BlockingIOError):
            outcomes.append(BrokenPipeError())
        writer = _BoundedSocketWriter(sender, outcomes)
        expected_error = BrokenPipeError if failure in (InterruptedError, BlockingIOError) else failure
        with pytest.raises(expected_error):
            control_shm.send_control_shm_frame(
                writer, control_shm.encode_error_response("failure"), deadline=time.monotonic() + 1.0
            )
        assert sender.gettimeout() == previous_timeout
    finally:
        sender.close()
        receiver.close()


def test_nonblocking_without_deadline_propagates_would_block() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.setblocking(False)
        writer = _BoundedSocketWriter(sender, [BlockingIOError()])
        with pytest.raises(BlockingIOError):
            control_shm.send_control_shm_frame(writer, control_shm.encode_error_response("nonblocking"))
        assert writer.timeouts == [0.0]
        assert sender.gettimeout() == 0.0
    finally:
        sender.close()
        receiver.close()


def test_blocking_without_deadline_does_not_create_a_timeout() -> None:
    sender, receiver = socket.socketpair()
    try:
        writer = _BoundedSocketWriter(sender, [1, 2, 3])
        frame = control_shm.encode_error_response("blocking")
        control_shm.send_control_shm_frame(writer, frame)
        assert writer.timeouts == [None]
        assert sender.gettimeout() is None
        assert control_shm.receive_control_shm_frame(receiver) == (frame, [])
    finally:
        sender.close()
        receiver.close()


def test_zero_progress_is_an_explicit_closed_socket_error() -> None:
    sender, receiver = socket.socketpair()
    try:
        writer = _BoundedSocketWriter(sender, [0])
        with pytest.raises(EOFError, match="socket closed"):
            control_shm.send_control_shm_frame(writer, control_shm.encode_error_response("closed"))
        assert len(writer.calls) == 1
        assert sender.gettimeout() is None
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("completed_at", [1.0, 2.0])
@pytest.mark.parametrize("first_positive_bytes", [2, 1000])
@pytest.mark.parametrize("with_descriptor", [False, True])
def test_explicit_deadline_is_checked_after_positive_send(
    monkeypatch: pytest.MonkeyPatch, first_positive_bytes: int,
    with_descriptor: bool, completed_at: float,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    with tempfile.TemporaryFile() as descriptor_file:
        try:
            sender.settimeout(3.0)
            writer = _BoundedSocketWriter(
                sender, [first_positive_bytes], after_send=lambda: clock.__setitem__(0, completed_at)
            )
            with pytest.raises(socket.timeout, match="deadline expired"):
                control_shm.send_control_shm_frame(
                    writer,
                    control_shm.encode_error_response("expired"),
                    descriptor_file.fileno() if with_descriptor else None,
                    deadline=1.0,
                )
            assert len(writer.calls) == 1
            assert writer.calls[0][2] > 0
            assert sender.gettimeout() == 3.0
            _, ancillary, _, _ = receiver.recvmsg(4, socket.CMSG_SPACE(struct.calcsize("i")))
            descriptors = control_shm._extract_rights(ancillary)
            try:
                assert len(descriptors) == int(with_descriptor)
            finally:
                control_shm.close_control_shm_descriptors(descriptors)
        finally:
            sender.close()
            receiver.close()


def test_expired_deadline_does_not_send_any_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: 2.0)
    sender, receiver = socket.socketpair()
    try:
        writer = _BoundedSocketWriter(sender)
        with pytest.raises(socket.timeout, match="deadline expired"):
            control_shm.send_control_shm_frame(writer, control_shm.encode_error_response("expired"), deadline=1.0)
        assert writer.calls == []
        assert sender.gettimeout() is None
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("timeout_s", [0.0002, 0.0005, 0.0008])
def test_empty_receive_waits_only_for_submillisecond_budget(monkeypatch, timeout_s):
    clock = [10.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    waits = []

    def no_ready(readable, writable, exceptional, remaining):
        assert readable == [receiver] and writable == [] and exceptional == []
        assert receiver.gettimeout() == 0.0
        waits.append(remaining)
        clock[0] = 10.0 + timeout_s
        return [], [], []

    monkeypatch.setattr(control_shm.select, "select", no_ready)
    try:
        receiver.settimeout(timeout_s)
        with pytest.raises(socket.timeout, match="section=prefix.*received_bytes=0"):
            control_shm.receive_control_shm_frame(receiver)
        assert waits == pytest.approx([timeout_s], abs=1e-12)
        assert receiver.gettimeout() == timeout_s
    finally:
        sender.close()
        receiver.close()


def test_prequeued_submillisecond_frame_uses_immediate_io(monkeypatch):
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        control_shm.select, "select",
        lambda *args: pytest.fail("already ready frame must not enter readiness wait"),
    )
    sender, receiver = socket.socketpair()
    try:
        frame = control_shm.encode_error_response("queued")
        sender.settimeout(0.0008)
        control_shm.send_control_shm_frame(sender, frame)
        receiver.settimeout(0.0008)
        assert control_shm.receive_control_shm_frame(receiver) == (frame, [])
        assert sender.gettimeout() == receiver.gettimeout() == 0.0008
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("at_deadline", [False, True])
def test_readiness_at_exact_deadline_does_not_read_queued_frame(monkeypatch, at_deadline):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    frame = control_shm.encode_error_response("boundary")
    wire = struct.pack("<I", len(frame)) + frame

    def become_ready(readable, writable, exceptional, remaining):
        assert remaining == pytest.approx(0.0005)
        sender.sendall(wire)
        clock[0] = 0.0005 if at_deadline else 0.000499
        return readable, writable, []

    monkeypatch.setattr(control_shm.select, "select", become_ready)
    try:
        receiver.settimeout(0.0005)
        if at_deadline:
            with pytest.raises(socket.timeout):
                control_shm.receive_control_shm_frame(receiver)
            assert receiver.recv(len(wire)) == wire
        else:
            assert control_shm.receive_control_shm_frame(receiver) == (frame, [])
        assert receiver.gettimeout() == 0.0005
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("failure", ["timeout", "eof", "pipe"])
@pytest.mark.parametrize("section", ["prefix", "body"])
def test_partial_frame_failure_closes_received_descriptor(monkeypatch, failure, section):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    received_descriptors = []
    frame = control_shm.encode_error_response("partial descriptor")
    prefix = struct.pack("<I", len(frame))

    class Reader:
        def fileno(self):
            return receiver.fileno()

        def gettimeout(self):
            return receiver.gettimeout()

        def settimeout(self, timeout):
            receiver.settimeout(timeout)

        def recvmsg(self, *args):
            result = receiver.recvmsg(*args)
            for level, kind, data in result[1]:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    rights = array("i")
                    rights.frombytes(data)
                    received_descriptors.extend(rights)
            return result

    def failed_wait(readable, writable, exceptional, remaining):
        assert remaining == pytest.approx(0.0005)
        if failure == "pipe":
            raise BrokenPipeError("receive failed")
        clock[0] = 0.0005
        return [], [], []

    monkeypatch.setattr(control_shm.select, "select", failed_wait)
    with tempfile.TemporaryFile() as descriptor_file:
        try:
            initial = prefix[:2] if section == "prefix" else prefix + frame[:2]
            sender.sendmsg(
                [initial],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [descriptor_file.fileno()]))],
            )
            if failure == "eof":
                sender.shutdown(socket.SHUT_WR)
            receiver.settimeout(0.0005)
            expected = {"timeout": control_shm.ControlShmProtocolError,
                        "eof": EOFError, "pipe": BrokenPipeError}[failure]
            with pytest.raises(expected):
                control_shm.receive_control_shm_frame(Reader())
            assert len(received_descriptors) == 1
            for descriptor in received_descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
            assert receiver.gettimeout() == 0.0005
            os.fstat(descriptor_file.fileno())
        finally:
            sender.close()
            receiver.close()


@pytest.mark.parametrize("with_descriptor", [False, True])
def test_submillisecond_send_wait_preserves_partial_cursor_and_rights(monkeypatch, with_descriptor):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    waits = []

    def ready(readable, writable, exceptional, remaining):
        assert readable == [] and len(writable) == 1
        waits.append(remaining)
        clock[0] += 0.0001
        return [], writable, []

    monkeypatch.setattr(control_shm.select, "select", ready)
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        try:
            sender.settimeout(0.0008)
            writer = _BoundedSocketWriter(
                sender, [1, BlockingIOError(), InterruptedError(), 2],
                after_send=lambda: clock.__setitem__(0, clock[0] + 0.0001),
            )
            frame = control_shm.encode_error_response("submillisecond partial write")
            control_shm.send_control_shm_frame(
                writer, frame, descriptor_file.fileno() if with_descriptor else None,
            )
            assert waits == pytest.approx([0.0007])
            assert writer.calls[0][1] is with_descriptor
            assert all(not has_rights for _, has_rights, _ in writer.calls[1:])
            received, descriptors = control_shm.receive_control_shm_frame(receiver)
            assert received == frame
            _check_received_descriptor(descriptors, int(with_descriptor))
            assert sender.gettimeout() == 0.0008
        finally:
            sender.close()
            receiver.close()


def test_no_ready_send_expires_without_publishing_descriptor(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()

    def no_ready(readable, writable, exceptional, remaining):
        assert remaining == pytest.approx(0.0002)
        clock[0] = 0.0002
        return [], [], []

    monkeypatch.setattr(control_shm.select, "select", no_ready)
    with tempfile.TemporaryFile() as descriptor_file:
        try:
            sender.settimeout(0.0002)
            writer = _BoundedSocketWriter(sender, [BlockingIOError()])
            with pytest.raises(socket.timeout):
                control_shm.send_control_shm_frame(
                    writer, control_shm.encode_error_response("not ready"), descriptor_file.fileno(),
                )
            assert len(writer.calls) == 1 and writer.calls[0][2] == 0
            receiver.settimeout(0.0)
            with pytest.raises(BlockingIOError):
                receiver.recvmsg(4, socket.CMSG_SPACE(struct.calcsize("i")))
            os.fstat(descriptor_file.fileno())
            assert sender.gettimeout() == 0.0002
        finally:
            sender.close()
            receiver.close()


@pytest.mark.parametrize("expire_on_receive", [1, 2])
def test_positive_receive_at_deadline_closes_prefix_descriptor(monkeypatch, expire_on_receive):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    received_descriptors = []

    class Reader:
        receive_count = 0

        def gettimeout(self):
            return receiver.gettimeout()

        def settimeout(self, timeout):
            receiver.settimeout(timeout)

        def recvmsg(self, *args):
            result = receiver.recvmsg(*args)
            for level, kind, data in result[1]:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    rights = array("i")
                    rights.frombytes(data)
                    received_descriptors.extend(rights)
            self.receive_count += 1
            if self.receive_count == expire_on_receive:
                clock[0] = 0.0005
            return result

    with tempfile.TemporaryFile() as descriptor_file:
        try:
            frame = control_shm.encode_error_response("received too late")
            control_shm.send_control_shm_frame(sender, frame, descriptor_file.fileno())
            receiver.settimeout(0.0005)
            expected = socket.timeout if expire_on_receive == 1 else control_shm.ControlShmProtocolError
            with pytest.raises(expected):
                control_shm.receive_control_shm_frame(Reader())
            assert len(received_descriptors) == 1
            with pytest.raises(OSError):
                os.fstat(received_descriptors[0])
            assert receiver.gettimeout() == 0.0005
        finally:
            sender.close()
            receiver.close()


def test_explicit_receive_deadline_never_resets_after_idle_prefix(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    frame = control_shm.encode_error_response("absolute receive")

    class Reader:
        receive_count = 0

        def gettimeout(self):
            return receiver.gettimeout()

        def settimeout(self, timeout):
            receiver.settimeout(timeout)

        def recvmsg(self, *args):
            result = receiver.recvmsg(*args)
            self.receive_count += 1
            clock[0] = 0.0004 if self.receive_count == 1 else 0.0005
            return result

    try:
        sender.sendall(struct.pack("<I", len(frame)) + frame)
        receiver.settimeout(10.0)
        with pytest.raises(control_shm.ControlShmProtocolError, match="partial"):
            control_shm.receive_control_shm_frame(
                Reader(), allow_idle_prefix_wait=True, deadline=0.0005,
            )
        assert receiver.gettimeout() == 10.0
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("failure", [BrokenPipeError("partial pipe"), 0])
def test_partial_descriptor_send_failure_preserves_sender_ownership(monkeypatch, failure):
    monkeypatch.setattr(control_shm.time, "monotonic", lambda: 0.0)
    sender, receiver = socket.socketpair()
    with tempfile.TemporaryFile() as descriptor_file:
        descriptor_file.write(b"descriptor")
        descriptor_file.flush()
        try:
            sender.settimeout(0.0008)
            writer = _BoundedSocketWriter(sender, [1, failure])
            expected = BrokenPipeError if isinstance(failure, BaseException) else EOFError
            with pytest.raises(expected):
                control_shm.send_control_shm_frame(
                    writer, control_shm.encode_error_response("partial failure"),
                    descriptor_file.fileno(),
                )
            assert [has_rights for _, has_rights, _ in writer.calls] == [True, False]
            prefix, ancillary, _, _ = receiver.recvmsg(4, socket.CMSG_SPACE(struct.calcsize("i")))
            assert len(prefix) == 1
            descriptors = control_shm._extract_rights(ancillary)
            _check_received_descriptor(descriptors, 1)
            os.fstat(descriptor_file.fileno())
            assert sender.gettimeout() == 0.0008
        finally:
            sender.close()
            receiver.close()
