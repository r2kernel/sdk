"""Real Unix sockets and memfd mappings: result delivery versus ownership release."""
from contextlib import contextmanager
import socket
import threading
import time

import pytest

from r2kernel.core._control_shm import (
    ControlShmProtocolError,
    decode_response_consumed,
    decode_transact_response,
    encode_response_released,
    encode_transact_request,
    receive_control_shm_frame,
    send_control_shm_frame,
)
from r2kernel.core._control_shm_region import ControlShmBudget, ControlShmRegion
from r2kernel.core._control_shm_transport import ControlShmChannelRegistry, ControlShmCounters
from r2kernel.core.native_control import NativeControlClient
from test_control_shm import _test_policy, _request_lease, _transact_response_frame


@pytest.fixture(autouse=True)
def development_peer_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake Control servers here have no root broker: use the documented development mode."""
    monkeypatch.setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled")


def exchange(channel, payload=b'first', timeout_s=1, result_ready=True):
    return channel.exchange(
        request_builder=lambda lease: encode_transact_request(
            target='gateway', host='127.0.0.1', port=45872, topic='control',
            timestamp_ns=22, period_ms=1, deadline_ms=timeout_s * 1000,
            timeout_ms=int(timeout_s * 1000), priority=0, payload_lease=lease),
        response_decoder=decode_transact_response, timeout_s=timeout_s,
        request_payload=payload, response_has_payload=lambda response: bool(response['ok']),
        response_ticket_required=True, result_ready=result_ready,
    )


@contextmanager
def peer(tmp_path, server):
    endpoint=str(tmp_path/'control.sock')
    listener=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen(2)
    errors=[]
    def serve():
        try:
            connection, _=listener.accept()
            with connection:
                connection.settimeout(2)
                server(connection)
        except BaseException as error:
            errors.append(error)
    worker=threading.Thread(target=serve)
    worker.start()
    policy=_test_policy()
    budget=ControlShmBudget(policy)
    registry=ControlShmChannelRegistry(policy, budget, ControlShmCounters())
    channel=registry.get(endpoint, 'gateway', 'control')
    try:
        yield channel, registry, budget
    finally:
        registry.close_all()
        worker.join(3)
        listener.close()
        assert not worker.is_alive()
        assert not errors, errors
        assert budget.snapshot()['channels']==0
        assert budget.snapshot()['owned_bytes']==0
        assert budget.snapshot()['mapped_bytes']==0


def response(connection, payload=b'response', ticket=1):
    frame, descriptors=receive_control_shm_frame(connection)
    lease=_request_lease(frame)
    assert len(descriptors)==1
    mapping=ControlShmRegion.from_received_descriptor(lease['region_name'], lease['slot_size'], descriptors[0])
    owner=ControlShmRegion.create(4096)
    response_lease=owner.write_payload(payload, 901)
    send_control_shm_frame(connection, _transact_response_frame(response_lease, response_ticket=ticket), owner.file_descriptor)
    consumed, descriptors=receive_control_shm_frame(connection)
    assert descriptors==[]
    assert decode_response_consumed(consumed)[0]==ticket
    return mapping, lease, owner


def test_response_ready_is_owned_and_next_write_waits_for_reclamation(tmp_path):
    allow_release=threading.Event()
    original_checked=threading.Event()
    def server(connection):
        mapping, lease, owner=response(connection)
        try:
            assert allow_release.wait(2)
            assert mapping.read_payload(lease)==b'first'
            original_checked.set()
            # Reusing the producer region must not alter the caller's owned copy.
            owner.write_payload(b'changed!', 902)
            send_control_shm_frame(connection, encode_response_released(1))
            frame, descriptors=receive_control_shm_frame(connection)
            assert descriptors==[]
            next_lease=_request_lease(frame)
            assert mapping.read_payload(next_lease)==b'second'
            reply_lease=owner.write_payload(b'next-response',903)
            send_control_shm_frame(connection,_transact_response_frame(reply_lease,response_ticket=2))
            consumed, descriptors=receive_control_shm_frame(connection)
            assert not descriptors
            assert decode_response_consumed(consumed)[0]==2
            send_control_shm_frame(connection,encode_response_released(2))
        finally:
            mapping.close()
            owner.close()
    with peer(tmp_path,server) as (channel, registry, budget):
        result=exchange(channel)
        worker=channel._reclaimer
        assert result['payload']==b'response'
        with pytest.raises(socket.timeout): channel.wait_reclaimed(.01)
        with pytest.raises(socket.timeout): exchange(channel,b'forbidden',.01)
        assert not channel.is_closed
        allow_release.set()
        channel.wait_reclaimed(1)
        assert original_checked.is_set()
        assert result['payload']==b'response'
        assert exchange(channel,b'second')['payload']==b'next-response'
        channel.wait_reclaimed(1)
        assert channel._reclaimer is worker and worker.is_alive()


@pytest.mark.parametrize('fault', ['missing','timeout','wrong_ticket','trailing','descriptor','partial'])
def test_release_fault_is_sticky_and_never_replays_the_request(tmp_path,fault):
    def server(connection):
        mapping, lease, owner=response(connection)
        try:
            if fault=='missing': return
            if fault=='timeout':
                # Caller shutdown, rather than a new request, must arrive.
                assert connection.recv(1)==b''
                return
            frame=encode_response_released(2 if fault=='wrong_ticket' else 1)
            if fault=='trailing': frame+=b'!'
            if fault=='partial':
                import struct
                connection.sendall(struct.pack('<I',len(frame))+frame[:7])
                assert connection.recv(1)==b''
                return
            send_control_shm_frame(connection,frame,owner.file_descriptor if fault=='descriptor' else None)
            assert connection.recv(1)==b''
        finally:
            mapping.close()
            owner.close()
    with peer(tmp_path,server) as (channel,registry,budget):
        result=exchange(channel,timeout_s=.1)
        assert result['payload']==b'response'
        with pytest.raises(ControlShmProtocolError,match='reclamation failed'):
            channel.wait_reclaimed(1)
        assert registry.get(channel.endpoint,'gateway','control') is channel
        with pytest.raises(ControlShmProtocolError,match='reclamation failed'):
            exchange(channel)
        assert result['payload']==b'response'


def test_close_cancels_pending_release_and_joins_worker(tmp_path):
    def server(connection):
        mapping,lease,owner=response(connection)
        try: assert connection.recv(1)==b''
        finally:
            mapping.close()
            owner.close()
    with peer(tmp_path,server) as (channel,registry,budget):
        assert exchange(channel,timeout_s=10)['payload']==b'response'
        worker=channel._reclaimer
        start=time.monotonic()
        registry.close_all()
        assert time.monotonic()-start<1
        assert not worker.is_alive()


def test_existing_synchronous_api_waits_for_release(tmp_path):
    ack_received=threading.Event()
    allow_release=threading.Event()
    completed=threading.Event()
    def server(connection):
        mapping,lease,owner=response(connection)
        try:
            ack_received.set()
            assert allow_release.wait(2)
            send_control_shm_frame(connection,encode_response_released(1))
        finally:
            mapping.close()
            owner.close()
    with peer(tmp_path,server) as (channel,registry,budget):
        results=[]
        def caller():
            results.append(exchange(channel,result_ready=False))
            completed.set()
        worker=threading.Thread(target=caller)
        worker.start()
        assert ack_received.wait(2)
        assert not completed.wait(.02)
        allow_release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert completed.is_set() and results[0]['payload']==b'response'
        assert channel._reclaimer is None


def test_independent_gateway_and_interface_lane_is_not_blocked(tmp_path):
    first=tmp_path/'first'; first.mkdir()
    second=tmp_path/'second'; second.mkdir()
    allow_release=threading.Event()
    def slow(connection):
        mapping,lease,owner=response(connection)
        try:
            assert allow_release.wait(2)
            send_control_shm_frame(connection,encode_response_released(1))
        finally:
            mapping.close()
            owner.close()
    def fast(connection):
        mapping,lease,owner=response(connection,b'other-gateway')
        try: send_control_shm_frame(connection,encode_response_released(1))
        finally:
            mapping.close()
            owner.close()
    with peer(first,slow) as (slow_channel,_,_):
        assert exchange(slow_channel)['payload']==b'response'
        try:
            with peer(second,fast) as (fast_channel,_,_):
                assert exchange(fast_channel)['payload']==b'other-gateway'
                fast_channel.wait_reclaimed(1)
                assert slow_channel._pending_release is not None
        finally:
            allow_release.set()
        slow_channel.wait_reclaimed(1)


def test_python_client_facade_exposes_result_and_reclamation_calls():
    from r2kernel.core.r2kernel_client import R2KernelClient
    from unittest.mock import Mock
    client=object.__new__(R2KernelClient)
    client._control=Mock()
    options=dict(target='gateway',topic='control',type='bytes',value=b'x',period_ms=1,deadline_ms=20)
    result=client.transact_control_result(**options)
    assert result is client._control.transact_result_ready.return_value
    client._control.transact_result_ready.assert_called_once_with(**options,last_tick_ns=None,priority=None)
    client.wait_control_reclaimed('gateway','control',100)
    client._control.wait_control_reclaimed.assert_called_once_with('gateway','control',100)


def test_public_client_preserves_sticky_failure_across_repeated_transactions(tmp_path):
    def server(connection):
        mapping,lease,owner=response(connection)
        mapping.close(); owner.close()
    with peer(tmp_path,server) as (channel,registry,budget):
        client=object.__new__(NativeControlClient)
        client._control_shm_negotiated=True
        client._identity='caller'
        client._routes={'gateway':('127.0.0.1',45872)}
        client._ready={'gateway':True}
        client._local_control_endpoint=channel.endpoint
        client._control_shm_channel_registry=registry
        arguments=dict(target='gateway',topic='control',payload=b'first',timestamp=22,
                       period_ms=1,deadline_ms=1000,priority=None)
        result=client._managed_transact_request(**arguments,result_ready=True)
        with pytest.raises(ControlShmProtocolError,match='reclamation failed'):
            client.wait_control_reclaimed('gateway','control',1000)
        for _ in range(2):
            with pytest.raises(ControlShmProtocolError,match='reclamation failed'):
                client._managed_transact_request(**arguments)
            assert registry.find(channel.endpoint,'gateway','control') is channel
        assert result['payload']==b'response'


def test_normal_process_exit_runs_registered_cleanup_for_idle_reclaimer():
    import os
    import subprocess
    import sys
    code='''import atexit
from r2kernel.core._control_shm_region import ControlShmBudget,ControlShmPolicy
from r2kernel.core._control_shm_transport import ControlShmChannel,ControlShmCounters
policy=ControlShmPolicy()
channel=ControlShmChannel(endpoint='/tmp/unused',policy=policy,budget=ControlShmBudget(policy),counters=ControlShmCounters())
atexit.register(channel.close)
with channel._lock: channel._ensure_reclaimer_locked()
print('idle worker started',flush=True)
'''
    result=subprocess.run([sys.executable,'-c',code],env=os.environ,
                          capture_output=True,text=True,timeout=3)
    assert result.returncode==0,result.stderr
    assert 'idle worker started' in result.stdout


def test_rebind_and_stop_block_new_lane_admission_and_join_retiring_channels(tmp_path):
    policy=_test_policy(); budget=ControlShmBudget(policy)
    registry=ControlShmChannelRegistry(policy,budget,ControlShmCounters())
    channel=registry.get(str(tmp_path/'unused'),'gateway','control')
    closing=threading.Event(); permit_close=threading.Event(); stopped=threading.Event()
    original_close=channel.close
    def close():
        closing.set()
        assert permit_close.wait(2)
        original_close()
    channel.close=close
    errors=[]
    route={'value':('127.0.0.1',1)}
    def rebind():
        try: registry.close_target('gateway',lambda: route.update(value=('127.0.0.1',2)))
        except ControlShmProtocolError as error: errors.append(error)
    rebinder=threading.Thread(target=rebind); rebinder.start()
    assert closing.wait(1)
    with pytest.raises(ControlShmProtocolError,match='being rebound'):
        registry.get_route(channel.endpoint,'gateway','control',lambda:route['value'])
    def stop():
        registry.close_all(); stopped.set()
    stopper=threading.Thread(target=stop); stopper.start()
    try:
        assert not stopped.wait(.02)
        with pytest.raises(ControlShmProtocolError,match='stopped'):
            registry.get(channel.endpoint,'other-gateway','other-interface')
    finally:
        permit_close.set(); rebinder.join(2); stopper.join(2)
    assert stopped.is_set() and not rebinder.is_alive() and not stopper.is_alive()
    assert len(errors)==1 and 'stopped during rebind' in str(errors[0])
    assert route['value']==('127.0.0.1',1)
    assert budget.snapshot()['channels']==0


def test_explicit_start_reopens_only_after_shutdown_has_drained_workers(tmp_path):
    policy=_test_policy(); budget=ControlShmBudget(policy)
    registry=ControlShmChannelRegistry(policy,budget,ControlShmCounters())
    original=registry.get(str(tmp_path/'unused'),'gateway','control')
    client=object.__new__(NativeControlClient)
    client._control_shm_channel_registry=registry
    registry.close_all()
    with pytest.raises(ControlShmProtocolError,match='stopped'):
        registry.get(original.endpoint,'gateway','control')
    client.start()
    replacement=registry.get(original.endpoint,'gateway','control')
    assert replacement is not original and original.is_closed
    registry.close_all()
    assert budget.snapshot()['channels']==0
