from __future__ import annotations

import os
import mmap
import socket
import struct
import sys
import tempfile
import threading
import time
import types

import pytest

from r2kernel.core._control_shm import (
    ControlShmProtocolError,
    decode_error_response,
    decode_handler_response,
    decode_response_consumed,
    decode_transact_response,
    encode_error_response,
    encode_response_released,
    encode_transact_request,
    receive_control_shm_frame,
    send_control_shm_frame,
)
from r2kernel.core._control_shm_region import (
    CONTROL_SHM_MAX_REGION_BYTES,
    ControlShmBudget,
    ControlShmCapacityError,
    ControlShmRegion,
)
from r2kernel.core import _control_shm as control_shm_module
from r2kernel.core._control_shm_transport import (
    ControlShmChannel,
    ControlShmChannelRegistry,
    ControlShmCounters,
)
from r2kernel.core._control_shm_server import ControlShmServerConnection
from control_shm_support import (
    _test_policy,
    _request_lease,
    _handler_request_frame,
    _transact_response_frame,
)


@pytest.fixture
def portable_test_region_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise common mapping logic on macOS without replacing the production backend."""
    if os.name == "nt":
        pytest.skip("local Control v5 descriptor passing is Unix-only")
    if __import__("sys").platform.startswith("linux"):
        return

    def create_unlinked_test_file(_token: str, size: int) -> tuple[int, None]:
        descriptor, path = tempfile.mkstemp(prefix="r2kernel-control-test-")
        os.unlink(path)
        try:
            os.ftruncate(descriptor, size)
            os.set_inheritable(descriptor, False)
            return descriptor, None
        except Exception:
            os.close(descriptor)
            raise

    monkeypatch.setattr(
        ControlShmRegion,
        "_create_unlinked_posix_region",
        staticmethod(create_unlinked_test_file),
    )


def test_v5_policy_uses_page_rounded_geometric_growth_and_hard_cap() -> None:
    page_size = mmap.PAGESIZE
    maximum_region_size = page_size * 2 + 1000
    policy = _test_policy(maximum_region_bytes=maximum_region_size)
    assert policy.region_size_for(0) == page_size
    assert policy.region_size_for(page_size - 63, page_size) == page_size * 2
    assert policy.region_size_for(page_size * 2 - 63, page_size * 2) == maximum_region_size
    with pytest.raises(ControlShmCapacityError):
        policy.region_size_for(maximum_region_size - 63)
    with pytest.raises(ValueError, match="4096-byte minimum"):
        _test_policy(minimum_region_bytes=2048)
    with pytest.raises(ValueError, match="16 MiB"):
        _test_policy(maximum_region_bytes=CONTROL_SHM_MAX_REGION_BYTES + 1)


def test_macos_posix_region_is_unlinked_and_close_on_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    created_regions = []

    class FakePosixSharedMemory:
        def __init__(self, name: str, flags: int, mode: int, size: int):
            del name, flags, mode
            descriptor, path = tempfile.mkstemp(prefix="r2kernel-control-posix-test-")
            os.unlink(path)
            os.ftruncate(descriptor, size)
            self.fd = descriptor
            self.unlinked = False
            created_regions.append(self)

        def unlink(self) -> None:
            self.unlinked = True

        def close_fd(self) -> None:
            os.close(self.fd)

    fake_posix_ipc = types.SimpleNamespace(O_CREX=1, SharedMemory=FakePosixSharedMemory)
    monkeypatch.setitem(sys.modules, "posix_ipc", fake_posix_ipc)
    descriptor, owner = ControlShmRegion._create_unlinked_posix_region("generation", 4096)
    try:
        assert descriptor == owner.fd
        assert not os.get_inheritable(descriptor)
        assert created_regions[0].unlinked
    finally:
        owner.close_fd()


def test_region_mapping_and_descriptor_passing_reuses_one_generation(
    portable_test_region_backend: None,
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    owner = ControlShmRegion.create(4096, budget)
    sender, receiver = socket.socketpair()
    mapped: ControlShmRegion | None = None
    try:
        first_lease = owner.write_payload(b"first", 1)
        send_control_shm_frame(sender, encode_error_response("test-frame"), owner.file_descriptor)
        frame, descriptors = receive_control_shm_frame(receiver)
        assert decode_error_response(frame or b"") == "test-frame"
        assert len(descriptors) == 1
        mapped = ControlShmRegion.from_received_descriptor(
            first_lease["region_name"], first_lease["slot_size"], descriptors.pop(), budget
        )
        assert mapped.read_payload(first_lease) == b"first"
        with pytest.raises(ControlShmProtocolError, match="read-only"):
            mapped.write_payload(b"invalid", 2)

        second_lease = owner.write_payload(b"next payload", 3)
        send_control_shm_frame(sender, encode_error_response("reuse"))
        frame, descriptors = receive_control_shm_frame(receiver)
        assert decode_error_response(frame or b"") == "reuse"
        assert descriptors == []
        assert mapped.read_payload(second_lease) == b"next payload"
    finally:
        if mapped is not None:
            mapped.close()
        owner.close()
        sender.close()
        receiver.close()
    assert budget.snapshot()["owned_bytes"] == 0
    assert budget.snapshot()["mapped_bytes"] == 0


def test_server_rejects_obsolete_v4_before_authentication_or_mapping(
    portable_test_region_backend: None,
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    owner = ControlShmRegion.create(4096, budget)
    server_socket, client_socket = socket.socketpair()
    server = ControlShmServerConnection(policy, budget, counters)
    authentication_calls: list[float] = []
    try:
        lease = owner.write_payload(b"must-not-dispatch", 1)
        current_frame = _handler_request_frame(lease, lease["length"])
        obsolete_frame = current_frame[:4] + b"\x04" + current_frame[5:]
        send_control_shm_frame(client_socket, obsolete_frame, owner.file_descriptor)
        with pytest.raises(ControlShmProtocolError, match="invalid local Control v5 frame header"):
            server.receive_request(server_socket, authenticate=authentication_calls.append)
        assert authentication_calls == []
        assert counters.snapshot()["received_mapping_opens"] == 0
        assert counters.snapshot()["payload_reads"] == 0
        assert budget.snapshot()["mapped_bytes"] == 0
    finally:
        server.close()
        owner.close()
        server_socket.close()
        client_socket.close()
    assert budget.snapshot()["channels"] == 0
    assert budget.snapshot()["owned_bytes"] == 0


def test_transaction_decoder_rejects_obsolete_v4_with_response_kind_four() -> None:
    frame = _transact_response_frame(None, ok=False, error="obsolete runtime")
    assert frame[:6] == b"SRLC\x05\x04"
    obsolete_frame = frame[:4] + b"\x04" + frame[5:]
    with pytest.raises(ControlShmProtocolError, match="invalid local Control v5 response header"):
        decode_transact_response(obsolete_frame)


def test_server_connection_reads_and_returns_persistent_mappings(
    portable_test_region_backend: None,
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    request_owner = ControlShmRegion.create(4096, budget)
    server_socket, client_socket = socket.socketpair()
    server = ControlShmServerConnection(policy, budget, counters)
    response_mapping: ControlShmRegion | None = None
    try:
        request_lease = request_owner.write_payload(b"request-one", 10)
        send_control_shm_frame(
            client_socket,
            _handler_request_frame(request_lease, len(b"request-one")),
            request_owner.file_descriptor,
        )
        request = server.receive_request(server_socket)
        assert request is not None
        assert request["payload"] == b"request-one"
        assert request["topic"] == "sample.topic"
        server.send_response(server_socket, status=7, timestamp_ns=11, payload=b"reply-one")
        frame, descriptors = receive_control_shm_frame(client_socket)
        assert len(descriptors) == 1
        decoded = decode_handler_response(frame or b"")
        response_mapping = ControlShmRegion.from_received_descriptor(
            decoded["payload_lease"]["region_name"],
            decoded["payload_lease"]["slot_size"],
            descriptors.pop(),
            budget,
        )
        assert decoded["status"] == 7
        assert response_mapping.read_payload(decoded["payload_lease"]) == b"reply-one"

        next_request_lease = request_owner.write_payload(b"request-two", 12)
        send_control_shm_frame(
            client_socket,
            _handler_request_frame(next_request_lease, len(b"request-two")),
        )
        request = server.receive_request(server_socket)
        assert request is not None and request["payload"] == b"request-two"
        server.send_response(server_socket, status=0, timestamp_ns=13, payload=b"reply-two")
        frame, descriptors = receive_control_shm_frame(client_socket)
        assert descriptors == []
        decoded = decode_handler_response(frame or b"")
        assert response_mapping.read_payload(decoded["payload_lease"]) == b"reply-two"
        assert counters.snapshot()["received_mapping_reuses"] == 1
    finally:
        if response_mapping is not None:
            response_mapping.close()
        server.close()
        request_owner.close()
        server_socket.close()
        client_socket.close()
    assert budget.snapshot()["channels"] == 0
    assert budget.snapshot()["owned_bytes"] == 0
    assert budget.snapshot()["mapped_bytes"] == 0


def test_channel_reuses_request_and_response_regions_and_closes_failed_reply(
    portable_test_region_backend: None,
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    endpoint = f"/tmp/r2k-{os.getpid()}-{threading.get_ident()}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    server_errors: list[BaseException] = []
    requests: list[bytes] = []
    received_request_mappings: list[ControlShmRegion] = []
    response_owner: list[ControlShmRegion | None] = [None]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                for response_ticket, (response_status, reply) in enumerate(
                    ((0, b"response-one"), (9, b"response-two")), start=1
                ):
                    request_frame, descriptors = receive_control_shm_frame(connection)
                    assert request_frame is not None
                    requests.append(request_frame)
                    lease = _request_lease(request_frame)
                    if received_request_mappings:
                        assert descriptors == []
                        request_mapping = received_request_mappings[0]
                    else:
                        assert len(descriptors) == 1
                        request_mapping = ControlShmRegion.from_received_descriptor(
                            str(lease["region_name"]), int(lease["slot_size"]), descriptors.pop(),
                        )
                        received_request_mappings.append(request_mapping)
                    assert request_mapping.read_payload(lease) in (b"request-one", b"request-two")
                    if response_owner[0] is None:
                        response_owner[0] = ControlShmRegion.create(4096)
                    response_lease = response_owner[0].write_payload(reply, response_status)
                    send_control_shm_frame(
                        connection,
                        _transact_response_frame(
                            response_lease,
                            status=response_status,
                            response_ticket=response_ticket,
                        ),
                        response_owner[0].file_descriptor if response_status == 0 else None,
                    )
                    acknowledgement, acknowledgement_descriptors = receive_control_shm_frame(connection)
                    assert acknowledgement is not None and acknowledgement_descriptors == []
                    acknowledged_ticket, acknowledged_lease = decode_response_consumed(acknowledgement)
                    assert acknowledged_ticket == response_ticket
                    assert acknowledged_lease == response_lease
                    send_control_shm_frame(connection, encode_response_released(acknowledged_ticket))
        except BaseException as exc:
            server_errors.append(exc)
        finally:
            listener.close()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    channel = ControlShmChannel(endpoint=endpoint, policy=policy, budget=budget, counters=counters)
    try:
        responses = []
        for payload in (b"request-one", b"request-two"):
            responses.append(
                channel.exchange(
                    request_builder=lambda lease: encode_transact_request(
                        target="remote",
                        host="127.0.0.1",
                        port=45872,
                        topic="sample.topic",
                        timestamp_ns=22,
                        period_ms=10.0,
                        deadline_ms=100.0,
                        timeout_ms=100,
                        priority=0,
                        payload_lease=lease or {},
                    ),
                    response_decoder=decode_transact_response,
                    timeout_s=2.0,
                    request_payload=payload,
                    request_timestamp_ns=22,
                    response_has_payload=lambda response: bool(response["ok"]),
                    response_ticket_required=True,
                )
            )
        assert [response["payload"] for response in responses] == [b"response-one", b"response-two"]
        assert [response["status"] for response in responses] == [0, 9]
        assert _request_lease(requests[0])["region_name"] == _request_lease(requests[1])["region_name"]
        assert counters.snapshot()["request_region_reuses"] == 1
        assert counters.snapshot()["received_mapping_reuses"] == 1
    finally:
        channel.close()
        server_thread.join(timeout=3.0)
        for mapping in received_request_mappings:
            mapping.close()
        if response_owner[0] is not None:
            response_owner[0].close()
        try:
            os.unlink(endpoint)
        except FileNotFoundError:
            pass
    assert not server_thread.is_alive()
    assert server_errors == []
    assert budget.snapshot()["channels"] == 0


def _perform_channel_test_exchange(
    channel: ControlShmChannel,
    payload: bytes,
    *,
    timeout_s: float,
) -> dict[str, object]:
    return channel.exchange(
        request_builder=lambda lease: encode_transact_request(
            target="remote",
            host="127.0.0.1",
            port=45872,
            topic="sample.topic",
            timestamp_ns=22,
            period_ms=10.0,
            deadline_ms=100.0,
            timeout_ms=max(1, round(timeout_s * 1000)),
            priority=0,
            payload_lease=lease or {},
        ),
        response_decoder=decode_transact_response,
        timeout_s=timeout_s,
        request_payload=payload,
        request_timestamp_ns=22,
        response_has_payload=lambda response: bool(response["ok"]),
        response_ticket_required=True,
    )


def _receive_test_request(connection: socket.socket, expected_payload: bytes) -> dict[str, int | str]:
    frame, descriptors = receive_control_shm_frame(connection)
    assert frame is not None
    lease = _request_lease(frame)
    assert len(descriptors) == 1
    mapping = ControlShmRegion.from_received_descriptor(
        str(lease["region_name"]), int(lease["slot_size"]), descriptors.pop()
    )
    try:
        assert mapping.read_payload(lease) == expected_payload
    finally:
        mapping.close()
    return lease


def _send_empty_test_response(
    connection: socket.socket,
    *,
    response_ticket: int = 1,
    status: int = 17,
) -> tuple[ControlShmRegion, dict[str, int | str]]:
    owner = ControlShmRegion.create(4096)
    lease = owner.write_payload(b"", 901)
    send_control_shm_frame(
        connection,
        _transact_response_frame(lease, status=status, response_ticket=response_ticket),
        owner.file_descriptor,
    )
    return owner, lease


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("wrong_ticket", ControlShmProtocolError),
        ("trailing_bytes", ControlShmProtocolError),
        ("descriptor", ControlShmProtocolError),
        ("missing", EOFError),
        ("partial_deadline", ControlShmProtocolError),
    ],
)
def test_channel_closes_after_invalid_missing_or_partial_release(
    portable_test_region_backend: None,
    failure: str,
    expected_error: type[BaseException],
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    endpoint = f"/tmp/r2k-{os.getpid()}-{threading.get_ident()}-{failure}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(1)
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                _receive_test_request(connection, b"request")
                response_owner, response_lease = _send_empty_test_response(connection)
                try:
                    acknowledgement, descriptors = receive_control_shm_frame(connection)
                    assert acknowledgement is not None and descriptors == []
                    ticket, acknowledged_lease = decode_response_consumed(acknowledgement)
                    assert ticket == 1
                    assert acknowledged_lease == response_lease
                    if failure == "missing":
                        return
                    if failure == "wrong_ticket":
                        send_control_shm_frame(connection, encode_response_released(2))
                    elif failure == "trailing_bytes":
                        send_control_shm_frame(connection, encode_response_released(1) + b"\x00")
                    elif failure == "descriptor":
                        with tempfile.TemporaryFile() as temporary:
                            send_control_shm_frame(
                                connection,
                                encode_response_released(1),
                                temporary.fileno(),
                            )
                    elif failure == "partial_deadline":
                        released = encode_response_released(1)
                        connection.sendall(struct.pack("<I", len(released)))
                        connection.sendall(released[:7])
                        time.sleep(0.35)
                        try:
                            connection.sendall(released[7:])
                        except OSError:
                            pass
                finally:
                    response_owner.close()
        except BaseException as exc:
            server_errors.append(exc)
        finally:
            listener.close()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    channel = ControlShmChannel(endpoint=endpoint, policy=policy, budget=budget, counters=counters)
    try:
        with pytest.raises(expected_error):
            _perform_channel_test_exchange(channel, b"request", timeout_s=0.15)
        assert channel.is_closed
    finally:
        channel.close()
        server_thread.join(timeout=2.0)
        try:
            os.unlink(endpoint)
        except FileNotFoundError:
            pass
    assert not server_thread.is_alive()
    assert server_errors == []
    assert budget.snapshot()["channels"] == 0


def test_failed_release_requires_explicit_fresh_channel_and_ticket_for_next_call(
    portable_test_region_backend: None,
) -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    registry = ControlShmChannelRegistry(policy, budget, counters)
    endpoint = f"/tmp/r2k-{os.getpid()}-{threading.get_ident()}-fresh.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(2)
    request_tokens: list[str] = []
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            for call_index, request_payload in enumerate((b"first-call", b"second-call")):
                connection, _ = listener.accept()
                with connection:
                    lease = _receive_test_request(connection, request_payload)
                    request_tokens.append(str(lease["region_name"]))
                    response_owner, response_lease = _send_empty_test_response(connection, status=29)
                    try:
                        acknowledgement, descriptors = receive_control_shm_frame(connection)
                        assert acknowledgement is not None and descriptors == []
                        ticket, acknowledged_lease = decode_response_consumed(acknowledgement)
                        assert ticket == 1
                        assert acknowledged_lease == response_lease
                        release_ticket = 2 if call_index == 0 else 1
                        send_control_shm_frame(connection, encode_response_released(release_ticket))
                    finally:
                        response_owner.close()
        except BaseException as exc:
            server_errors.append(exc)
        finally:
            listener.close()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    try:
        first_channel = registry.get(endpoint, "remote", "sample.topic")
        with pytest.raises(ControlShmProtocolError, match="released ticket"):
            _perform_channel_test_exchange(first_channel, b"first-call", timeout_s=1.0)
        assert first_channel.is_closed

        second_channel = registry.get(endpoint, "remote", "sample.topic")
        assert second_channel is not first_channel
        response = _perform_channel_test_exchange(second_channel, b"second-call", timeout_s=1.0)
        assert response["payload"] == b""
        assert response["status"] == 29
        assert len(request_tokens) == 2
        assert request_tokens[0] != request_tokens[1]
    finally:
        registry.close_all()
        server_thread.join(timeout=3.0)
        try:
            os.unlink(endpoint)
        except FileNotFoundError:
            pass
    assert not server_thread.is_alive()
    assert server_errors == []
    assert budget.snapshot()["channels"] == 0


def test_receive_rejects_fds_outside_prefix_and_partial_frame_timeout() -> None:
    sender, receiver = socket.socketpair()
    temporary = tempfile.TemporaryFile()
    try:
        frame = encode_error_response("misplaced")
        sender.sendall(struct.pack("<I", len(frame)))
        sender.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", temporary.fileno()))])
        with pytest.raises(ControlShmProtocolError, match="outside the frame prefix"):
            receive_control_shm_frame(receiver)
    finally:
        sender.close()
        receiver.close()
        temporary.close()

    sender, receiver = socket.socketpair()
    try:
        receiver.settimeout(0.05)
        sender.sendall(b"\x10\x00")
        with pytest.raises(ControlShmProtocolError, match="partial local Control v5 frame"):
            receive_control_shm_frame(receiver)
    finally:
        sender.close()
        receiver.close()


def test_frame_send_obeys_one_absolute_deadline_under_backpressure() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        frame = b"SRLC\x05\xff" + b"x" * (64 * 1024 - 6)
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            send_control_shm_frame(sender, frame, deadline=started + 0.05)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert sender.gettimeout() is None
    finally:
        sender.close()
        receiver.close()


def test_timed_out_waiter_does_not_close_channel_owned_by_another_exchange() -> None:
    channel = ControlShmChannel(
        endpoint="/tmp/control-channel-lock-test.sock",
        policy=_test_policy(),
        budget=ControlShmBudget(_test_policy()),
        counters=ControlShmCounters(),
    )
    lock_held = threading.Event()
    release_lock = threading.Event()

    def active_exchange_owner() -> None:
        with channel._lock:
            lock_held.set()
            release_lock.wait(timeout=1.0)

    active_thread = threading.Thread(target=active_exchange_owner, daemon=True)
    active_thread.start()
    try:
        assert lock_held.wait(timeout=1.0)
        started = time.monotonic()
        with pytest.raises(socket.timeout, match="channel is busy"):
            _perform_channel_test_exchange(channel, b"not-sent", timeout_s=0.05)
        assert time.monotonic() - started < 0.5
        assert not channel.is_closed
    finally:
        release_lock.set()
        active_thread.join(timeout=1.0)
        channel.close()
    assert not active_thread.is_alive()


@pytest.mark.parametrize("allow_idle_prefix_wait", [False, True])
def test_server_frame_body_gets_fresh_budget_only_after_idle_prefix(
    monkeypatch: pytest.MonkeyPatch, allow_idle_prefix_wait: bool,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(control_shm_module.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    frame = encode_error_response("complete frame")

    class DelayedReader:
        receive_count = 0

        def gettimeout(self):
            return receiver.gettimeout()

        def settimeout(self, timeout):
            receiver.settimeout(timeout)

        def recvmsg(self, *args):
            result = receiver.recvmsg(*args)
            self.receive_count += 1
            clock[0] += 0.8 if self.receive_count == 1 else 0.4
            return result

    try:
        sender.sendall(struct.pack("<I", len(frame)) + frame)
        receiver.settimeout(1.0)
        reader = DelayedReader()
        if allow_idle_prefix_wait:
            assert receive_control_shm_frame(reader, allow_idle_prefix_wait=True) == (frame, [])
        else:
            with pytest.raises(ControlShmProtocolError, match="partial local Control v5 frame"):
                receive_control_shm_frame(reader)
        assert reader.receive_count == 2
        assert receiver.gettimeout() == 1.0
    finally:
        sender.close()
        receiver.close()


def test_server_frame_deadline_is_not_reset_for_each_body_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(control_shm_module.time, "monotonic", lambda: clock[0])
    sender, receiver = socket.socketpair()
    frame = encode_error_response("body spans multiple chunks")

    class PartialReader:
        receive_count = 0

        def gettimeout(self):
            return receiver.gettimeout()

        def settimeout(self, timeout):
            receiver.settimeout(timeout)

        def recvmsg(self, size, *args):
            self.receive_count += 1
            if self.receive_count == 2:
                size = max(1, len(frame) // 2)
            result = receiver.recvmsg(size, *args)
            clock[0] += {1: 0.8, 2: 0.6, 3: 0.5}[self.receive_count]
            return result

    try:
        sender.sendall(struct.pack("<I", len(frame)) + frame)
        receiver.settimeout(1.0)
        reader = PartialReader()
        with pytest.raises(ControlShmProtocolError, match="partial local Control v5 frame"):
            receive_control_shm_frame(reader, allow_idle_prefix_wait=True)
        assert reader.receive_count == 3
        assert receiver.gettimeout() == 1.0
    finally:
        sender.close()
        receiver.close()


def test_server_idle_timeout_does_not_count_as_transport_failure() -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    counters = ControlShmCounters()
    server = ControlShmServerConnection(policy, budget, counters)
    server_socket, peer_socket = socket.socketpair()
    server_socket.settimeout(0.01)
    try:
        with pytest.raises(socket.timeout):
            server.receive_request(server_socket)
        snapshot = counters.snapshot()
        assert snapshot["errors"] == 0
        assert snapshot["io_errors"] == 0
        assert snapshot["protocol_errors"] == 0
    finally:
        server.close()
        server_socket.close()
        peer_socket.close()
    assert budget.snapshot()["channels"] == 0
