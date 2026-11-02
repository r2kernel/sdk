"""Prepared-lane recovery after one late exchange, over real v5 sockets and mappings.

The counterparty below stands in for Control: it accepts successive caller
connections, answers preparation with its own response region and echoes each
transaction after an adjustable delay. It never replays a request whose caller
has already closed its side.
"""
from __future__ import annotations

import select
import socket
import struct
import threading
import time

import pytest

from r2kernel.core import _control_shm as wire
from r2kernel.core import resource
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmRegion
from r2kernel.core._control_shm_transport import ControlShmChannelRegistry, ControlShmCounters
from r2kernel.core.native_control import NativeControlClient
from control_shm_support import _request_lease, _test_policy, _transact_response_frame

PREPARE_REQUEST, TRANSACT_REQUEST = 0x07, 0x03


def _string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("<I", len(encoded)) + encoded


def _lease_bytes(lease) -> bytes:
    output = bytearray()
    wire._push_lease(output, lease)
    return bytes(output)


def _peer_closed(connection: socket.socket) -> bool:
    readable, _, _ = select.select([connection], [], [], 0)
    return bool(readable) and connection.recv(1, socket.MSG_PEEK) == b""


class EchoControl:
    def __init__(self, tmp_path, monkeypatch):
        self.policy = _test_policy()
        self.budget = ControlShmBudget(self.policy)
        self.endpoint = str(tmp_path / "control.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.endpoint)
        self.listener.listen(4)
        self.listener.settimeout(0.05)
        self.delay_s = 0.0
        # Close the next lane after its preparation: "unread" once the next frame
        # arrived, leaving it unread (the caller's receive gets ECONNRESET),
        # "idle" at once (the caller's next send gets EPIPE).
        self.drop: str | None = None
        self.dropped = threading.Event()
        # Close the lane on the next transaction without answering, once released.
        self.close_on_transact = threading.Event()
        self.release_close = threading.Event()
        self.preparations = 0
        self.callbacks = 0
        self.errors: list[BaseException] = []
        self.stop = threading.Event()
        self.registry = ControlShmChannelRegistry(self.policy, ControlShmBudget(self.policy), ControlShmCounters())
        self.client = object.__new__(NativeControlClient)
        self.client._logger = None
        self.client._identity = "caller"
        self.client._control_shm_negotiated = True
        self.client._control_shm_channel_registry = self.registry
        self.client._local_control_endpoint = self.endpoint
        self.client._routes = {"gateway": ("local_control", 0)}
        self.client._ready = {}
        self.client._last_transport_error = None
        monkeypatch.setattr(resource, "verify_control_server_connection",
                            lambda connection, *, deadline=None: None)
        self.worker = threading.Thread(target=self.accept_loop, daemon=True)
        self.worker.start()

    def accept_loop(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    self.serve(connection)
                except BaseException as error:  # surfaced by close()
                    self.errors.append(error)

    def serve(self, connection: socket.socket):
        connection.settimeout(2)
        region = None
        ticket = 1
        try:
            while True:
                frame, descriptors = wire.receive_control_shm_frame(connection)
                wire.close_control_shm_descriptors(descriptors)
                if frame is None:
                    return
                if frame[5] == PREPARE_REQUEST:
                    position = 6
                    for _ in range(2):
                        _, position = wire._read_string(frame, position)
                    _, position = wire._read_u16(frame, position)
                    _, position = wire._read_string(frame, position)
                    _, position = wire._read_u32(frame, position)
                    _, position = wire._read_u32(frame, position)
                    response_capacity, position = wire._read_u32(frame, position)
                    region = ControlShmRegion.create(self.policy.region_size_for(response_capacity), self.budget)
                    lease = region.write_payload(b"", 0)
                    self.preparations += 1  # Counted before replying: the caller may observe the reply first.
                    wire.send_control_shm_frame(connection, b"SRLC\x05\x08\x01" + _string("") + _lease_bytes(lease),
                                                region.file_descriptor)
                    drop, self.drop = self.drop, None
                    if drop == "unread":
                        select.select([connection], [], [], 2)
                    if drop is not None:
                        connection.close()
                        self.dropped.set()
                        return
                    continue
                assert frame[5] == TRANSACT_REQUEST and region is not None
                _request_lease(frame)
                self.callbacks += 1
                if self.close_on_transact.is_set():
                    self.close_on_transact.clear()
                    self.release_close.wait(2)
                    return
                time.sleep(self.delay_s)
                if _peer_closed(connection):
                    return  # The caller's deadline expired; never replay or answer it.
                lease = region.write_payload(b"echo", self.callbacks)
                wire.send_control_shm_frame(connection, _transact_response_frame(
                    lease, timestamp_ns=self.callbacks, response_ticket=ticket))
                consumed, descriptors = wire.receive_control_shm_frame(connection)
                assert descriptors == [] and wire.decode_response_consumed(consumed)[0] == ticket
                wire.send_control_shm_frame(connection, wire.encode_response_released(ticket))
                ticket += 1
        finally:
            if region is not None:
                region.close()

    def transact(self, deadline_ms: float):
        return self.client._managed_transact_request(
            target="gateway", topic="control", payload=b"request", timestamp=7,
            period_ms=1, deadline_ms=deadline_ms, priority=None)

    def close(self):
        self.registry.close_all()
        self.stop.set()
        self.worker.join(3)
        self.listener.close()
        assert not self.worker.is_alive() and self.errors == []
        assert self.budget.snapshot()["owned_bytes"] == 0


def test_late_exchange_reprepares_prepared_lane(tmp_path, monkeypatch):
    control = EchoControl(tmp_path, monkeypatch)
    try:
        assert control.client.prepare_control("gateway", "control", 8, 16, 1000)
        assert control.transact(1000)["ts"] == 1
        control.delay_s = 0.05
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            control.transact(10)
        assert time.monotonic() - started < 1.0  # Control close plus one preparation.
        control.delay_s = 0.0
        assert control.preparations == 2 and control.callbacks == 2
        recovered = control.transact(1000)
        assert recovered["payload"] == b"echo" and recovered["ts"] == 3  # No replay of the late request.
        counters = control.registry.counters.snapshot()
        assert counters["prepared_lane_recoveries"] == 1 and counters["prepared_lane_recovery_failures"] == 0
    finally:
        control.close()


def test_failed_reprepare_keeps_lane_invalid_and_reports_it(tmp_path, monkeypatch):
    control = EchoControl(tmp_path, monkeypatch)
    try:
        assert control.client.prepare_control("gateway", "control", 8, 16, 1000)
        control.delay_s = 0.05
        control.listener.close()  # Control stops accepting: re-preparation must fail explicitly.
        with pytest.raises(socket.timeout):
            control.transact(10)
        counters = control.registry.counters.snapshot()
        assert counters["prepared_lane_recoveries"] == 0 and counters["prepared_lane_recovery_failures"] == 1
        assert "re-preparation failed" in control.client._last_transport_error
        with pytest.raises(wire.ControlShmProtocolError, match="prepared lane is invalid"):
            control.transact(1000)
    finally:
        control.close()


@pytest.mark.parametrize("drop, error", [("unread", ConnectionResetError), ("idle", BrokenPipeError)])
def test_reset_or_broken_pipe_exchange_reprepares_prepared_lane(tmp_path, monkeypatch, drop, error):
    """Control closed or reset the lane while the caller used it."""
    control = EchoControl(tmp_path, monkeypatch)
    try:
        control.drop = drop
        assert control.client.prepare_control("gateway", "control", 8, 16, 1000)
        if drop == "idle":
            assert control.dropped.wait(2)
        with pytest.raises(error):
            control.transact(1000)
        assert control.dropped.is_set() and control.preparations == 2 and control.callbacks == 0
        recovered = control.transact(1000)
        assert recovered["payload"] == b"echo" and recovered["ts"] == 1  # The failed request never ran.
        counters = control.registry.counters.snapshot()
        assert counters["prepared_lane_recoveries"] == 1 and counters["prepared_lane_recovery_failures"] == 0
    finally:
        control.close()


def test_only_the_call_that_retired_the_lane_recovers_it(tmp_path, monkeypatch):
    """A concurrent call that fails on the same closed lane never takes its retired socket."""
    control = EchoControl(tmp_path, monkeypatch)
    try:
        assert control.client.prepare_control("gateway", "control", 8, 16, 1000)
        channel, _ = control.registry.get_route(control.endpoint, "gateway", "control", lambda: ("local_control", 0))
        recovered_by: list[str] = []
        original_recover = NativeControlClient._recover_prepared_lane

        def recording_recover(client, *args):
            recovered_by.append(threading.current_thread().name)
            return original_recover(client, *args)

        monkeypatch.setattr(NativeControlClient, "_recover_prepared_lane", recording_recover)
        other_done = threading.Event()
        original_take = channel.take_retired_socket

        def take_after_the_other_call(*error):
            # The failing call takes its socket only after the concurrent call
            # failed on the closed lane and ran its own failure handling.
            if threading.current_thread().name == "failing":
                assert other_done.wait(5)
            return original_take(*error)

        monkeypatch.setattr(channel, "take_retired_socket", take_after_the_other_call)
        outcomes: dict[str, BaseException | None] = {}

        def call(name: str) -> None:
            try:
                control.transact(1000)
                outcomes[name] = None
            except BaseException as failure:  # recorded for the assertions below
                outcomes[name] = failure
            if name == "concurrent":
                other_done.set()

        control.close_on_transact.set()
        failing = threading.Thread(target=call, args=("failing",), name="failing")
        failing.start()
        deadline = time.monotonic() + 2
        while control.callbacks == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        concurrent = threading.Thread(target=call, args=("concurrent",), name="concurrent")
        concurrent.start()
        time.sleep(0.05)  # The concurrent call now waits for the lane held by the failing one.
        control.release_close.set()
        failing.join(5)
        concurrent.join(5)
        assert isinstance(outcomes["failing"], EOFError), outcomes
        assert isinstance(outcomes["concurrent"], wire.ControlShmProtocolError), outcomes
        assert recovered_by == ["failing"], recovered_by
        assert control.preparations == 2
        assert control.transact(1000)["payload"] == b"echo"
    finally:
        control.close()
