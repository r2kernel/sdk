"""Control SHM v5 registration negotiation, summaries, registry, production send/recv and frame diagnostics tests."""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time

import pytest

from r2kernel.core._control_shm import (
    ControlShmProtocolError,
    decode_transact_response,
    encode_response_released,
    receive_control_shm_frame,
)
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmRegion
from r2kernel.core._control_shm_transport import (
    ControlShmChannel,
    ControlShmChannelRegistry,
    ControlShmCounters,
    configure_policy_from_registration,
    control_shm_summary,
)
from r2kernel.core.native_control import NativeControlClient
from control_shm_support import _test_policy, _transact_response_frame


def test_registration_negotiation_and_summary_expose_shared_flat_fields() -> None:
    registration = {
        "control_local_payload_policy": "forwarded_shm_v5",
        "control_local_protocol": "binary_shm_v5",
        "control_shm_min_region_bytes": 4096,
        "control_shm_max_channels": 128,
        "control_shm_max_owned_bytes": 64 * 1024 * 1024,
        "control_shm_max_mapped_bytes": 64 * 1024 * 1024,
    }
    policy = configure_policy_from_registration(registration, 16 * 1024 * 1024)
    assert policy.maximum_channels == 128
    with pytest.raises(ControlShmProtocolError, match="policies do not match"):
        configure_policy_from_registration({**registration, "control_local_protocol": "binary_shm_v2"}, 4096)
    for field, obsolete_label in (
        ("control_local_payload_policy", "forwarded_shm_v4"),
        ("control_local_protocol", "binary_shm_v4"),
    ):
        with pytest.raises(ControlShmProtocolError, match="policies do not match"):
            configure_policy_from_registration({**registration, field: obsolete_label}, 4096)

    budget = ControlShmBudget(policy)
    summary = control_shm_summary(ControlShmCounters(), budget)
    assert summary["event"] == "control_shm_summary"
    assert summary["policy"] == "forwarded_shm_v5"
    assert summary["active_channels"] == 0
    assert summary["max_channels"] == 128
    assert "fd_sent" in summary and "fd_received" in summary
    valid_lease = {
        "region_name": "generation-token",
        "slot_index": 0,
        "slot_size": 4096,
        "slot_count": 1,
        "length": 0,
    }
    with pytest.raises(ControlShmProtocolError, match="contains an error message"):
        decode_transact_response(_transact_response_frame(valid_lease, error="invalid-success"))


def test_control_shm_summary_retains_bounded_remote_error_without_counting_sdk_error() -> None:
    policy = _test_policy()
    counters = ControlShmCounters()
    budget = ControlShmBudget(policy)
    error_message = "gateway rejected local Control frame: " + ("x" * 600)

    counters.record_remote_error(error_message)
    summary = control_shm_summary(counters, budget)

    assert summary["last_error"] == error_message[:512]
    assert len(summary["last_error"]) == 512
    assert summary["protocol_errors"] == 0
    assert summary["io_errors"] == 0
    assert counters.snapshot()["errors"] == 0


def test_v5_transact_returns_original_peer_error_instead_of_send_failed() -> None:
    error_message = "gateway rejected a local Control v5 handler frame"
    counters = ControlShmCounters()

    class FailedChannel:
        def exchange(self, **_kwargs):
            counters.record_remote_error(error_message)
            return {
                "ok": False,
                "status": 1,
                "timestamp_ns": 0,
                "error": error_message,
                "payload_lease": None,
            }

    class FailedChannelRegistry:
        def get_route(self, endpoint, target, topic, route_provider):
            return self.get(endpoint, target, topic), route_provider()

        def __init__(self):
            self.channel = FailedChannel()

        def get(self, _endpoint, _target, _topic):
            return self.channel

        def remove(self, _endpoint, _target, _topic, _channel):
            pass

    client = object.__new__(NativeControlClient)
    client._identity = "test-gateway"
    client._routes = {"remote": ("127.0.0.1", 45872)}
    client._ready = {"remote": True}
    client._last_transport_error = None
    client._local_control_endpoint = "/tmp/control-shm-failed-reply.sock"
    client._control_shm_channel_registry = FailedChannelRegistry()
    client._control_shm_counters = counters
    client._require_control_shm_negotiated = lambda: None
    client._now_ns = lambda: 123

    result = client._transact_raw(
        target="remote",
        topic="sample.topic",
        payload=b"request-payload",
        period_ms=10.0,
        deadline_ms=100.0,
    )

    assert result["ok"] is False
    assert result["error"] == error_message
    assert counters.snapshot()["last_error"] == error_message


def test_python_wrapper_uses_wire_deadline_and_preserves_live_channel_on_busy_waiter() -> None:
    class Registry:
        def get_route(self, endpoint, target, topic, route_provider):
            return self.get(endpoint, target, topic), route_provider()

        def __init__(self, channel: object):
            self.channel = channel
            self.removed: list[object] = []

        def get(self, *_key: object) -> object:
            return self.channel

        def remove(self, *_key: object) -> None:
            self.removed.append(_key[-1])

    class SuccessfulChannel:
        is_closed = False

        def __init__(self):
            self.timeout_s = 0.0

        def exchange(self, **arguments: object) -> dict[str, object]:
            self.timeout_s = float(arguments["timeout_s"])
            return {
                "ok": True,
                "timestamp_ns": 19,
                "status": 11,
                "payload": b"",
            }

    client = object.__new__(NativeControlClient)
    client._identity = "test-gateway"
    client._routes = {"remote": ("127.0.0.1", 45872)}
    client._last_transport_error = None
    client._local_control_endpoint = "/tmp/wrapper-control.sock"
    client._ready = {"remote": True}
    client._require_control_shm_negotiated = lambda: None
    successful_channel = SuccessfulChannel()
    successful_registry = Registry(successful_channel)
    client._control_shm_channel_registry = successful_registry

    result = client._managed_transact_request(
        target="remote",
        topic="sample.topic",
        payload=b"request",
        timestamp=3,
        period_ms=10.0,
        deadline_ms=75.0,
        priority=None,
    )
    assert result is not None and result["status"] == 11 and result["payload"] == b""
    assert successful_channel.timeout_s == 0.075

    class BusyChannel:
        is_closed = False

        def exchange(self, **_arguments: object) -> dict[str, object]:
            raise socket.timeout("local Control v5 channel is busy past the exchange deadline")

    busy_channel = BusyChannel()
    busy_registry = Registry(busy_channel)
    client._control_shm_channel_registry = busy_registry
    client._ready["remote"] = True
    with pytest.raises(socket.timeout, match="channel is busy"):
        client._managed_transact_request(
            target="remote",
            topic="sample.topic",
            payload=b"not-sent",
            timestamp=4,
            period_ms=10.0,
            deadline_ms=75.0,
            priority=None,
        )
    assert busy_registry.removed == []
    assert client._ready["remote"] is True


def test_registry_prunes_closed_channels_and_removes_only_expected_instance() -> None:
    policy = _test_policy()
    budget = ControlShmBudget(policy)
    registry = ControlShmChannelRegistry(policy, budget, ControlShmCounters())
    first = registry.get("/tmp/control.sock", "remote", "sample.one")
    second = registry.get("/tmp/control.sock", "remote", "sample.two")
    registry.remove("/tmp/control.sock", "remote", "sample.one", second)
    assert len(registry._channels) == 2
    first.close()
    replacement = registry.get("/tmp/control.sock", "remote", "sample.three")
    assert len(registry._channels) == 2
    assert registry._channels[("/tmp/control.sock", "remote", "sample.two")] is second
    assert registry._channels[("/tmp/control.sock", "remote", "sample.three")] is replacement
    second.close()
    replacement.close()
    registry.close_all()
    assert budget.snapshot()["channels"] == 0


def test_failed_region_allocation_rolls_back_owned_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = ControlShmBudget(_test_policy())

    def fail_allocation(_token: str, _size: int) -> tuple[int, None]:
        raise OSError("allocation failed")

    monkeypatch.setattr(ControlShmRegion, "_create_memfd", staticmethod(fail_allocation))
    monkeypatch.setattr(ControlShmRegion, "_create_unlinked_posix_region", staticmethod(fail_allocation))
    with pytest.raises(OSError, match="allocation failed"):
        ControlShmRegion.create(4096, budget)
    assert budget.snapshot()["owned_bytes"] == 0


def test_production_send_recv_rejects_overwriting_unread_target_response() -> None:
    client = object.__new__(NativeControlClient)
    client._local_control_endpoint = "/tmp/test-control.sock"
    client._ready = {"remote": True}
    client._pending_responses = {}
    client._pending_cost_context = {}
    client._pending_target_guard = threading.Lock()
    client._pending_target_reservations = set()
    client._managed_control_ready = lambda: True
    client._managed_transact_request = lambda **_kwargs: {"payload": b"reply"}
    arguments = {
        "target": "remote",
        "topic": "sample.topic",
        "payload": b"request",
        "timestamp": 1,
        "period_ms": 10.0,
        "deadline_ms": 100.0,
    }
    assert client._send_raw(**arguments)
    with pytest.raises(ControlShmProtocolError, match="still pending"):
        client._send_raw(**arguments)
    assert client._recv_raw("remote") == {"payload": b"reply"}
    assert client._send_raw(**arguments)
    assert client._recv_raw("remote") == {"payload": b"reply"}


def test_early_production_recv_does_not_release_inflight_target_reservation() -> None:
    client = object.__new__(NativeControlClient)
    client._local_control_endpoint = "/tmp/test-control.sock"
    client._ready = {"remote": True}
    client._pending_responses = {}
    client._pending_cost_context = {}
    client._pending_target_guard = threading.Lock()
    client._pending_target_reservations = {"remote"}
    client._managed_control_ready = lambda: True
    client._managed_transact_request = lambda **_kwargs: {"payload": b"reply"}
    arguments = {
        "target": "remote",
        "topic": "sample.topic",
        "payload": b"request",
        "timestamp": 1,
        "period_ms": 10.0,
        "deadline_ms": 100.0,
    }

    assert client._recv_raw("remote") is None
    assert "remote" in client._pending_target_reservations
    with pytest.raises(ControlShmProtocolError, match="still pending"):
        client._send_raw(**arguments)

@pytest.mark.parametrize("section, partial", [("prefix", b"\x10\x00"), ("body", struct.pack("<I", 16)), ("body", struct.pack("<I", 16)+b"abc")])
def test_partial_frame_diagnostic_preserves_counts_and_timeout(section: str, partial: bytes) -> None:
    sender, receiver=socket.socketpair()
    try:
        receiver.settimeout(0.02);sender.sendall(partial)
        with pytest.raises(ControlShmProtocolError) as failure:
            receive_control_shm_frame(receiver, phase="response")
        message=str(failure.value)
        assert "partial local Control v5 frame phase=response" in message
        assert f"section={section}" in message
        assert f"expected_bytes={4 if section=='prefix' else 16}" in message
        assert f"received_bytes={len(partial) if section=='prefix' else len(partial)-4}" in message
        assert "configured_timeout_s=0.02" in message and "elapsed_s=" in message
        assert receiver.gettimeout()==0.02
        if sys.platform.startswith("linux"):
            assert "queued_bytes=0 queued_bytes_errno=0" in message
        assert "abc" not in message
    finally:
        sender.close();receiver.close()


def test_release_frame_timeout_diagnostic_identifies_ack_phase() -> None:
    sender, receiver=socket.socketpair()
    policy=_test_policy();budget=ControlShmBudget(policy)
    channel=ControlShmChannel(endpoint="unused",policy=policy,budget=budget,counters=ControlShmCounters())
    try:
        frame=encode_response_released(1)
        sender.sendall(struct.pack("<I",len(frame))+frame[:3])
        with pytest.raises(ControlShmProtocolError) as failure:
            channel._receive_release(receiver,1,time.monotonic()+0.02)
        message=str(failure.value)
        assert "phase=release section=body" in message
        assert f"expected_bytes={len(frame)} received_bytes=3" in message
        assert "configured_timeout_s=" in message and "elapsed_s=" in message
    finally:
        channel.close();sender.close();receiver.close()
    assert budget.snapshot()["channels"]==0


def test_initial_frame_timeout_reports_zero_prefix_bytes_without_protocol_reclassification() -> None:
    sender, receiver=socket.socketpair()
    try:
        receiver.settimeout(0.02)
        with pytest.raises(socket.timeout) as failure:
            receive_control_shm_frame(receiver,phase="response")
        assert not isinstance(failure.value,ControlShmProtocolError)
        message=str(failure.value)
        assert "phase=response section=prefix expected_bytes=4 received_bytes=0" in message
        assert "configured_timeout_s=0.02" in message and "elapsed_s=" in message
        assert receiver.gettimeout()==0.02
    finally:
        sender.close();receiver.close()


def test_client_registers_only_the_v5_handler_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    import r2kernel.core.native_control as native_control_module

    class StubCore:
        lib = object()

    monkeypatch.setattr(native_control_module, "R2KernelCore", StubCore)
    monkeypatch.setattr(NativeControlClient, "_create_client", lambda self, identity: object())
    monkeypatch.setattr(native_control_module.atexit, "register", lambda _callback: None)
    monkeypatch.setenv("R2KERNEL_CONTROL_VALIDATION_MODE", "binary_inline")
    client = NativeControlClient(identity="component.mobile")
    requests = []
    client._local_endpoint = "/tmp/test-handler.sock"
    client._exchange_control_request = lambda payload, **_kwargs: requests.append(payload) or {"res": "error"}
    with pytest.raises(RuntimeError, match="failed to register"):
        client._register_managed_handler()
    assert requests[0]["local_protocol"] == "binary_shm_v5"
    assert client.control_shm_statistics()["policy"] == "forwarded_shm_v5"
