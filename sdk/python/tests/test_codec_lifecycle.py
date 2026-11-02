"""Offline lifecycle acceptance: real prepared codecs, isolated fake resource peer.

Actual backend interoperability is exercised separately by the registered-path
fixtures; these tests force rejection and concurrent callback boundaries.
"""
from __future__ import annotations

import threading
import json
from contextlib import contextmanager
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from r2kernel.core import _keywords as KW

from r2kernel.core.codec_lifecycle import CodecLifecycle, CodecLifecycleBusyError
from r2kernel.core.subsystem_runtime import EventTransportManager, SubsystemRuntime
from r2kernel.datatype.common import DeviceState
from r2kernel.manifest.loader import InterfaceDeclaration, load_subsystem_manifest
from r2kernel.wrapper.subsystem import Subsystem


REFS = ('msg.core.common.primitive.v1/Int32Value',
        'msg.core.common.primitive.v1/IndexedBoolValue')
TOPICS = ('counter', 'indexed')


class Logger:
    def __init__(self):
        self.errors = []

    def error(self, *args, **kwargs):
        self.errors.append(str(args[-1]) if args else str(kwargs))

    def debug(self, *args, **kwargs):
        pass

    info = debug
    warning = debug


class ResourcePeer:
    def __init__(self):
        self.accept = True
        self.reject_topic = None
        self.handlers = {}
        self.calls = []
        self.session_id = 'lifecycle-test-session'
        self.notify_endpoint = None

    def subscribe_event(self, **arguments):
        self.calls.append(arguments['topic'])
        if not self.accept or arguments['topic'] == self.reject_topic:
            return False
        self.handlers[arguments['topic']] = arguments['handler']
        return True


class Subscriber(Subsystem):
    """Use the public initialize phase without starting a network/heartbeat thread."""
    def __init__(self):
        self._logger = Logger()
        self._name = 'lifecycle_subscriber'
        self._declared_apis = []
        self._declared_components = []
        self._subsystem_manifest = None
        self._state = DeviceState.BOOTING
        self._lifecycle_transition_lock = threading.RLock()
        self._lifecycle_transition_context = threading.local()
        self._termination_lock = threading.RLock()
        self._termination_flight = None
        self._termination_entry = threading.local()
        self._initialization_succeeded = False
        self.received = []
        self.statuses = []
        self.reject_status = False
        self.peer = ResourcePeer()
        runtime = object.__new__(SubsystemRuntime)
        runtime._procedure_handler_context = threading.local()
        runtime._owner = self
        runtime._subsystem_name = self._name
        runtime._timestamp_getter = lambda: 1000
        runtime._logger = self._logger
        runtime._client = self.peer
        runtime._rust_message_codec = None
        runtime._control_dispatch_running = False
        runtime._control_target_cache = {}
        runtime._control_contracts = {}
        runtime._procedure_contracts = {}
        runtime._operation_contracts = {}
        runtime._event = EventTransportManager(logger=self._logger, owner=self, client=self.peer)
        runtime._raw_control_endpoint_handlers = {}
        runtime._codec_lifecycle = CodecLifecycle(runtime)
        runtime._event._codec_lifecycle = runtime._codec_lifecycle
        runtime.start_heartbeat = lambda **kwargs: None
        runtime._heartbeat_state_getter = lambda: self._state.value
        runtime._get_control_port = lambda: 0
        runtime.get_control_info = lambda: []
        runtime.get_procedure_info = lambda: []
        runtime.get_operation_info = lambda: []
        runtime._manifest_status_payload = lambda: {}
        runtime.advertise_codec_identity = self.advertise
        self._runtime = runtime

    def advertise(self):
        metadata = self._runtime._codec_lifecycle.metadata()
        # A retained two-topic resource peer requires the complete snapshot.
        if not all(reference in metadata for reference in REFS):
            raise RuntimeError('codec_identity_missing: partial Event snapshot')
        self.statuses.append(metadata)
        if self.reject_status:
            raise RuntimeError('codec_mismatch: rejected candidate')
        self._runtime._codec_lifecycle.acknowledge_identity(metadata, self._runtime._codec_lifecycle.contract_codecs)

    def _change_device_state(self, state):
        self._state = state

    def _validate_declarations(self):
        pass

    def on_initialize(self):
        for topic, reference in zip(TOPICS, REFS):
            self._runtime.register_event_subscribe(topic=topic, type=reference, size=4096,
                                                  callback=lambda value, timestamp: self.received.append(value))
        # No resource requests or partial advertisements may escape the hook.
        self.hook_resource_calls = len(self.peer.calls)
        self.hook_status_calls = len(self.statuses)
        return True

    def on_start(self):
        return True

    def on_terminate(self):
        return True

    def on_reset(self):
        return True


class CodecLifecycleTests(unittest.TestCase):
    def test_ready_is_published_only_after_codec_commit_and_failure_never_ready(self):
        owner = Subscriber()
        observed = []

        def publish(state):
            if owner._state == state:
                return
            owner._state = state
            payload = owner._runtime._heartbeat_status_payload(lambda: state.value)[KW.PARAMETER]
            observed.append((state, payload['codec_context_available']))
            if state == DeviceState.READY:
                # The immediate status and user lifecycle callback must both
                # observe an executable, committed codec context.
                with owner._runtime._codec_lifecycle.activity():
                    self.assertIsNotNone(owner._runtime._codec_lifecycle.codec)

        owner._change_device_state = publish
        owner.initialize()
        self.assertEqual(observed, [(DeviceState.REGISTERING, False), (DeviceState.READY, True)])
        self.assertTrue(owner._initialization_succeeded)
        observed.clear()
        owner._state = DeviceState.STOPPED
        owner.reject_status = True
        owner.initialize()
        self.assertNotIn(DeviceState.READY, [state for state, _ in observed])
        self.assertEqual(observed[-1], (DeviceState.ERROR, False))
        self.assertFalse(owner._initialization_succeeded)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            with owner._runtime._codec_lifecycle.activity():
                self.fail('Failed preparation restored a generic codec')

    def test_reinitialize_ready_pairs_status_snapshot_with_preparation_transition(self):
        owner = Subscriber()
        owner.initialize()
        runtime = owner._runtime
        original = owner.on_initialize
        hook_statuses = []

        def reinitialize():
            hook_statuses.append(runtime._heartbeat_status_payload(lambda: owner.state.value)[KW.PARAMETER])
            return original()

        owner.on_initialize = reinitialize
        owner.initialize()
        self.assertEqual(hook_statuses[-1][KW.SUBSYSTEM_LIFECYCLE_STATUS], 'Registering')
        self.assertFalse(hook_statuses[-1]['codec_context_available'])
        self.assertEqual(owner.state, DeviceState.READY)

        # Pause a heartbeat after it reads Ready. Reinitialization cannot revoke
        # the context until that paired snapshot finishes reading its identity.
        read_state = threading.Event()
        initialization_attempted = threading.Event()
        release = threading.Event()
        snapshots = []

        def state_getter():
            state = owner.state.value
            read_state.set()
            if not release.wait(3):
                raise AssertionError('Status snapshot barrier timed out')
            return state

        snapshot = threading.Thread(target=lambda: snapshots.append(runtime._heartbeat_status_payload(state_getter)[KW.PARAMETER]))
        initialization = threading.Thread(target=owner.initialize)
        original_publication = runtime._codec_lifecycle.status_publication

        @contextmanager
        def observed_publication():
            if threading.current_thread() is initialization:
                initialization_attempted.set()
            with original_publication():
                yield

        runtime._codec_lifecycle.status_publication = observed_publication
        snapshot.start()
        try:
            self.assertTrue(read_state.wait(3))
            initialization.start()
            self.assertTrue(initialization_attempted.wait(3))
            self.assertEqual(owner.state, DeviceState.READY)
            self.assertTrue(runtime._codec_lifecycle._available)
        finally:
            release.set()
            snapshot.join(3)
            if initialization.ident is not None:
                initialization.join(3)
            runtime._codec_lifecycle.status_publication = original_publication
        self.assertFalse(snapshot.is_alive())
        self.assertFalse(initialization.is_alive())
        self.assertEqual(snapshots[0][KW.SUBSYSTEM_LIFECYCLE_STATUS], 'Ready')
        self.assertTrue(snapshots[0]['codec_context_available'])
        self.assertEqual(hook_statuses[-1][KW.SUBSYSTEM_LIFECYCLE_STATUS], 'Registering')
        self.assertFalse(hook_statuses[-1]['codec_context_available'])
        self.assertTrue(owner._initialization_succeeded)

    def test_heartbeat_keeps_actual_state_separate_from_static_binding_diagnostics(self):
        owner = Subscriber()
        owner.initialize()
        runtime = owner._runtime
        runtime._manifest_status_payload = lambda: {'lifecycle': 'WaitingBinding',
            'binding_errors': ['Required binding is missing: component.test.vision']}
        owner._state = DeviceState.OPERATING
        payload = runtime._heartbeat_status_payload(lambda: owner.state.value)[KW.PARAMETER]
        self.assertEqual(payload['reported_lifecycle'], 'Operating')
        self.assertEqual(payload[KW.SUBSYSTEM_LIFECYCLE_STATUS], 'WaitingBinding')
        self.assertTrue(payload['codec_context_available'])
        owner._state = DeviceState.ERROR
        payload = runtime._heartbeat_status_payload(lambda: owner.state.value)[KW.PARAMETER]
        self.assertEqual(payload['reported_lifecycle'], 'Error')
        self.assertEqual(payload[KW.SUBSYSTEM_LIFECYCLE_STATUS], 'Error')
        self.assertFalse(payload['codec_context_available'])

    def test_typed_operation_query_and_cancel_prepare_protocol_id_without_domain_dependency(self):
        owner = Subscriber()
        runtime = owner._runtime
        runtime._operation_lock = threading.RLock()
        runtime._operation_quiescence = threading.Condition(runtime._operation_lock)
        runtime._operation_records = {}
        runtime._operation_server_enabled = True
        invoked = []
        runtime._operation_handlers = {'increment': lambda payload: invoked.append(payload) or {'value': payload['value'] + 1}}
        runtime._operation_contracts = {'increment': {
            'request_type': REFS[0], 'result_type': REFS[0], 'timing_support': 'none',
        }}
        queued = []
        runtime._submit_operation = queued.append
        runtime._publish_operation_state = lambda **_kwargs: None
        runtime._publish_operation_result = lambda **_kwargs: None
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY, owner._logger.errors)
        codec = runtime._codec_lifecycle.codec

        def start():
            response = runtime._dispatch_control_request({
                'topic': 'operation.start_typed/increment',
                'payload': codec.encode_message(REFS[0], {'value': 8}),
            })
            result = json.loads(response['payload'])
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['accepted'])
            self.assertEqual(result['state'], 'queued')
            return result['operation_id']

        def request(topic, operation_id):
            response = runtime._dispatch_control_request({
                'topic': topic,
                'payload': codec.encode_message('msg.core.common.primitive.v1/StringValue', {'value': operation_id}),
            })
            result = json.loads(response['payload'])
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['operation_id'], operation_id)
            return result

        succeeded_id = start()
        self.assertEqual(request('operation.query_typed', succeeded_id)['state'], 'queued')
        runtime._run_operation(succeeded_id)
        completed = request('operation.query_typed', succeeded_id)
        self.assertEqual(completed['state'], 'succeeded')
        self.assertEqual(completed['result'], {'value': 9})
        self.assertEqual(request('operation.cancel_typed', succeeded_id)['state'], 'succeeded')
        canceled_id = start()
        self.assertTrue(request('operation.cancel_typed', canceled_id)['cancel_requested'])
        runtime._run_operation(canceled_id)
        self.assertEqual(request('operation.query_typed', canceled_id)['state'], 'canceled')
        self.assertEqual(invoked, [{'value': 8}])
        self.assertEqual(queued, [succeeded_id, canceled_id])

    @staticmethod
    def tutorial_private_owner(package_name):
        tutorial_root = Path(__file__).resolve().parents[3] / 'tutorials'
        package_root = tutorial_root / 'subsystems' / package_name
        owner = Subscriber()
        owner._subsystem_manifest = load_subsystem_manifest(package_root / 'subsystem.yaml')
        owner.on_initialize = lambda: True
        owner.pre_ready_identities = []

        def advertise():
            lifecycle = owner._runtime._codec_lifecycle
            if owner.state == DeviceState.READY:
                raise AssertionError('Codec identity must be advertised before Ready')
            owner.pre_ready_identities.append(lifecycle.status_identity(candidate=True))
            lifecycle.acknowledge_identity(lifecycle.metadata(), lifecycle.contract_codecs)

        owner._runtime.advertise_codec_identity = advertise
        return owner

    def test_package_private_binding_prepares_both_request_types_before_ready_and_reinitialize(self):
        tutorial_root = Path(__file__).resolve().parents[3] / 'tutorials'
        contract = 'api.tutorial.gateway.device_gateway.digital_io.v1'
        references = {
            'msg.core.common.primitive.v1/IndexedBoolValue',
            'msg.core.common.primitive.v1/UInt32Value',
            'msg.core.common.primitive.v1/BoolValue',
        }
        identities = {}
        for package_name in ('component.gripper', 'gateway.device_gateway'):
            with self.subTest(package=package_name), patch.dict('os.environ', {
                    'R2KERNEL_CONTRACT_SPEC_ROOTS': str(tutorial_root / 'subsystems' / package_name / 'spec')}):
                owner = self.tutorial_private_owner(package_name)
                # No local endpoint registration supplies either caller request.
                self.assertEqual(owner._runtime._control_contracts, {})
                owner.initialize()
                self.assertEqual(owner.state, DeviceState.READY, owner._logger.errors)
                self.assertEqual(owner.peer.calls, [])
                lifecycle = owner._runtime._codec_lifecycle
                prepared = lifecycle.contract_codecs[contract]
                self.assertEqual(set(prepared), references)
                identity = owner.pre_ready_identities[-1]
                self.assertEqual(identity['contract_codecs'][contract], prepared)
                self.assertTrue(references.issubset(identity['codec_metadata']))
                self.assertGreater(len(identity['contract_codecs']), 1)
                identities[package_name] = prepared
                old_codec = lifecycle.codec
                owner._state = DeviceState.STOPPED
                owner.initialize()
                self.assertEqual(owner.state, DeviceState.READY, owner._logger.errors)
                self.assertIsNot(lifecycle.codec, old_codec)
                self.assertEqual(lifecycle.contract_codecs[contract], prepared)
                self.assertEqual(len(owner.pre_ready_identities), 2)
                self.assertTrue(lifecycle.status_identity()['codec_context_available'])
        self.assertEqual(identities['component.gripper'], identities['gateway.device_gateway'])

    def test_package_private_control_route_rejects_missing_and_unequal_peer_identities(self):
        tutorial_root = Path(__file__).resolve().parents[3] / 'tutorials'
        with patch.dict('os.environ', {
                'R2KERNEL_CONTRACT_SPEC_ROOTS': str(tutorial_root / 'subsystems' / 'component.gripper' / 'spec')}):
            owner = self.tutorial_private_owner('component.gripper')
            owner.initialize()
            self.assertEqual(owner.state, DeviceState.READY, owner._logger.errors)
            lifecycle = owner._runtime._codec_lifecycle
            for reference in ('msg.core.common.primitive.v1/IndexedBoolValue',
                              'msg.core.common.primitive.v1/UInt32Value',
                              'msg.core.common.primitive.v1/BoolValue'):
                with self.subTest(reference=reference):
                    expected = lifecycle.metadata()
                    route = {'subsystem': 'gateway.device_gateway', 'codec_metadata': expected}
                    lifecycle.validate_route(route, [reference])
                    del expected[reference]
                    with self.assertRaisesRegex(RuntimeError, 'missing codec identity'):
                        lifecycle.validate_route(route, [reference])
                    expected[reference] = {**lifecycle.metadata()[reference], 'fingerprint': 'unequal'}
                    with self.assertRaisesRegex(RuntimeError, 'Prepared schema mismatch'):
                        lifecycle.validate_route(route, [reference])

    @staticmethod
    def with_interface(contract, visibility='internal'):
        owner = Subscriber()
        owner._subsystem_manifest = SimpleNamespace(
            provides={'private': InterfaceDeclaration('private', contract, visibility=visibility)},
            requires={}, exports={})
        return owner

    def test_private_interface_keeps_registered_endpoint_codecs_without_spec_id(self):
        owner = self.with_interface('tutorial.gateway.device_gateway.digital_io.v1')
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertEqual(owner.peer.calls, list(TOPICS))
        lifecycle = owner._runtime._codec_lifecycle
        self.assertTrue(all(ref in lifecycle.metadata() for ref in REFS))
        self.assertNotIn('tutorial.gateway.device_gateway.digital_io.v1', lifecycle.contract_codecs)
        wire = owner._runtime._rust_message_codec.encode_message(REFS[0], {'value': 19})
        owner.peer.handlers[TOPICS[0]](wire, 1)
        self.assertEqual(owner.received, [{'value': 19}])

    def test_private_interface_does_not_hide_invalid_registered_message(self):
        owner = self.with_interface('product.private.control.v1')
        owner._runtime._control_contracts['private'] = {
            'request_type': 'msg.missing.private.v1/Request', 'response_type': REFS[0]}
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(owner.peer.calls, [])
        self.assertIsNone(owner._runtime._rust_message_codec)

    def test_public_non_spec_id_remains_an_error(self):
        owner = self.with_interface('tutorial.gateway.device_gateway.digital_io.v1', 'public')
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(owner.peer.calls, [])
        self.assertTrue(any('Unsupported codec contract' in error for error in owner._logger.errors))

    def test_internal_spec_id_still_requires_a_valid_spec(self):
        owner = self.with_interface('api.missing.private.v1')
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(owner.peer.calls, [])
        self.assertIsNone(owner._runtime._rust_message_codec)

    def test_two_topics_prepare_once_before_registration_and_unchanged_reinitialize(self):
        owner = Subscriber()
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertTrue(owner._initialization_succeeded)
        self.assertEqual(owner.hook_resource_calls, 0)
        self.assertEqual(owner.hook_status_calls, 0)
        self.assertEqual(owner.peer.calls, list(TOPICS))
        self.assertEqual(len(owner.statuses), 1)
        old_codec = owner._runtime._rust_message_codec
        owner._state = DeviceState.STOPPED
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertIsNot(owner._runtime._rust_message_codec, old_codec)
        self.assertEqual(owner.hook_resource_calls, 2)
        self.assertEqual(owner.hook_status_calls, 1)
        self.assertEqual(owner.peer.calls, list(TOPICS) * 2)
        self.assertEqual(len(owner.statuses), 2)
        wire = owner._runtime._rust_message_codec.encode_message(REFS[0], {'value': 12})
        owner.peer.handlers[TOPICS[0]](wire, 1)
        self.assertEqual(owner.received, [{'value': 12}])

    def test_subscription_rejection_blocks_ready_and_retry_registers_again(self):
        owner = Subscriber()
        owner.peer.accept = False
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertFalse(owner._initialization_succeeded)
        self.assertEqual(owner._runtime.get_subscribe_info(), [])
        self.assertIsNone(owner._runtime._rust_message_codec)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            owner._runtime._get_rust_message_codec()
        owner.peer.accept = True
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertEqual(owner.peer.calls, [TOPICS[0], *TOPICS])
        self.assertEqual(set(owner._runtime.get_subscribe_info()), set(zip(TOPICS, REFS)))

    def retained_delivery_during_registration(self, owner):
        """Model the broker receive worker with one retained, consumed sample."""
        lifecycle = owner._runtime._codec_lifecycle
        waiting = threading.Event()
        finished = threading.Event()
        workers = []
        subscribe = owner.peer.subscribe_event
        wait = lifecycle._initialization_finished.wait

        def observe_wait(*args, **kwargs):
            waiting.set()
            return wait(*args, **kwargs)

        def register(**arguments):
            accepted = subscribe(**arguments)
            if accepted and arguments['topic'] == TOPICS[0]:
                codec = lifecycle.codec
                wire = codec.encode_message(REFS[0], {'value': 23})

                def receive():
                    try:
                        arguments['handler'](wire, 101, 'retained-timing')
                    finally:
                        finished.set()

                worker = threading.Thread(target=receive, daemon=True)
                workers.append(worker)
                worker.start()
                self.assertTrue(waiting.wait(2), 'receiver did not reach initialization gate')
                self.assertFalse(finished.is_set())
                self.assertEqual(owner.received, [])
                with self.assertRaisesRegex(RuntimeError, 'during codec initialization'):
                    with lifecycle.activity():
                        self.fail('Control gate waited instead of rejecting')
            return accepted

        owner.peer.subscribe_event = register
        lifecycle._initialization_finished.wait = observe_wait
        return finished, workers

    def test_retained_broker_sample_waits_for_ready_and_preserves_timing(self):
        owner = Subscriber()
        deliveries = []
        original_initialize = owner.on_initialize

        def initialize():
            original_initialize()
            owner._runtime.register_event_subscribe(topic=TOPICS[0], type=REFS[0], size=4096,
                callback=lambda value, timestamp, timing: deliveries.append(
                    (value, timestamp, timing, owner.state, owner._runtime._codec_lifecycle._initializing)))
            return True

        owner.on_initialize = initialize
        finished, workers = self.retained_delivery_during_registration(owner)
        owner.initialize()
        self.assertTrue(finished.wait(2))
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(deliveries, [({'value': 23}, 101, 'retained-timing', DeviceState.READY, False)])
        self.assertEqual(owner._logger.errors, [])
        self.assertEqual(owner._runtime._codec_lifecycle._active_callbacks, 0)

    def test_retained_broker_sample_rejected_if_later_registration_fails(self):
        owner = Subscriber()
        owner.peer.reject_topic = TOPICS[1]
        finished, workers = self.retained_delivery_during_registration(owner)
        owner.initialize()
        self.assertTrue(finished.wait(2))
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(owner.received, [])
        self.assertTrue(any('Codec context unavailable' in error for error in owner._logger.errors))
        self.assertIsNone(owner._runtime._rust_message_codec)
        self.assertEqual(owner._runtime._codec_lifecycle._active_callbacks, 0)

    def test_obsolete_broker_handler_cannot_decode_in_new_initialization(self):
        owner = Subscriber()
        owner.initialize()
        old_handler = owner.peer.handlers[TOPICS[0]]
        old_codec = owner._runtime._codec_lifecycle.codec
        wire = old_codec.encode_message(REFS[0], {'value': 99})
        initialize = owner.on_initialize

        def reinitialize():
            old_handler(wire, 1)
            self.assertEqual(owner.received, [])
            return initialize()

        owner.on_initialize = reinitialize
        owner._state = DeviceState.STOPPED
        finished, workers = self.retained_delivery_during_registration(owner)
        owner.initialize()
        self.assertTrue(finished.wait(2))
        for worker in workers:
            worker.join(2)
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertEqual(owner.received, [{'value': 23}])
        old_handler(wire, 2)
        self.assertEqual(owner.received, [{'value': 23}])
        self.assertTrue(any('obsolete codec initialization' in error for error in owner._logger.errors))

    def test_same_initialization_thread_never_waits_for_broker_callback(self):
        owner = Subscriber()
        subscribe = owner.peer.subscribe_event

        def register(**arguments):
            result = subscribe(**arguments)
            wire = owner._runtime._codec_lifecycle.codec.encode_message(REFS[0], {'value': 4})
            if arguments['topic'] == TOPICS[0]:
                arguments['handler'](wire, 1)
            return result

        owner.peer.subscribe_event = register
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertEqual(owner.received, [])
        self.assertTrue(any('cannot wait on its own codec initialization' in error for error in owner._logger.errors))

    def test_broker_callback_rejects_resetting_state(self):
        owner = Subscriber()
        owner.initialize()
        wire = owner._runtime._codec_lifecycle.codec.encode_message(REFS[0], {'value': 4})
        owner._state = DeviceState.RESETTING
        owner.peer.handlers[TOPICS[0]](wire, 1)
        self.assertEqual(owner.received, [])
        self.assertTrue(any('during subsystem reset' in error for error in owner._logger.errors))
        owner._state = DeviceState.READY
        owner.peer.handlers[TOPICS[0]](wire, 2)
        self.assertEqual(owner.received, [{'value': 4}])

    def test_failed_reinitialize_disables_old_callbacks_and_control_without_fallback(self):
        owner = Subscriber()
        owner.initialize()
        old_codec = owner._runtime._rust_message_codec
        wire = old_codec.encode_message(REFS[0], {'value': 7})
        owner.reject_status = True
        owner._state = DeviceState.STOPPED
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        owner.peer.handlers[TOPICS[0]](wire, 1)
        self.assertEqual(owner.received, [])
        self.assertIsNone(owner._runtime._event._message_codec)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            owner._runtime._dispatch_control_request({'topic': 'anything'})
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            owner._runtime._transact_typed_control(target='peer', topic='counter',
                request_type=REFS[0], response_type=REFS[0], payload={'value': 1}, period_ms=1, deadline_ms=1)
        owner.reject_status = False
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        owner.peer.handlers[TOPICS[0]](wire, 2)
        self.assertEqual(owner.received, [{'value': 7}])

    def test_partial_resource_failure_retry_keeps_complete_identity(self):
        owner = Subscriber()
        owner.peer.reject_topic = TOPICS[1]
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(set(owner.peer.handlers), {TOPICS[0]})
        self.assertIsNone(owner._runtime._rust_message_codec)
        owner.peer.reject_topic = None
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertEqual(owner.peer.calls, list(TOPICS) * 2)
        self.assertEqual(len(owner.statuses), 2)
        self.assertTrue(all(all(ref in status for ref in REFS) for status in owner.statuses))

    def test_context_creation_failure_invalidates_previous_snapshot(self):
        owner = Subscriber()
        owner.initialize()
        with patch('r2kernel.core.codec_lifecycle.PreparedSchemaContext', side_effect=RuntimeError('malformed schema')):
            owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertFalse(owner._initialization_succeeded)
        self.assertIsNone(owner._runtime._rust_message_codec)
        self.assertIsNone(owner._runtime._event._message_codec)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            with owner._runtime._codec_lifecycle.callback():
                self.fail('Failed context allowed callback')
        owner.reset()
        self.assertEqual(owner.state, DeviceState.READY)
        with self.assertRaisesRegex(RuntimeError, 'successful initialization'):
            owner._on_pre_start()

    def test_active_provider_and_caller_block_reinitialization_until_completion(self):
        for role in ('provider', 'caller'):
            with self.subTest(role=role):
                owner = Subscriber()
                owner.initialize()
                runtime = owner._runtime
                entered, release = threading.Event(), threading.Event()
                results, failures = [], []
                def block(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('Test did not release active operation')
                    if role == 'provider':
                        return {'value': args[0]['value']}
                    return {'ok': True, 'response': {'payload': runtime._rust_message_codec.encode_message(REFS[0], {'value': 42})}}
                if role == 'provider':
                    runtime._control_server_enabled = True
                    runtime._control_endpoint_handlers = {'counter': block}
                    runtime._raw_control_endpoint_handlers = {}
                    runtime._control_contracts = {'counter': {'request_type': REFS[0], 'response_type': REFS[0], 'timing_support': 'required'}}
                    runtime._procedure_handler_context = threading.local()
                    codec = runtime._rust_message_codec
                    payload = codec.encode_message(REFS[0], {'value': 42})
                    wire = codec.encode_control_request_wire(request_id=7, sent_at_sync_ns=900,
                        execute_at_sync_ns=1000, max_lateness_ns=1000, priority=128,
                        late_policy='execute_immediately', payload=payload)
                    invoke = lambda: runtime._dispatch_control_request({'topic': 'counter', 'sender': 'caller',
                        'payload': wire, 'period_ms': 1, 'deadline_ms': 1000})
                else:
                    runtime._client.transact_control = block
                    invoke = lambda: runtime._transact_typed_control(target='peer', topic='counter',
                        request_type=REFS[0], response_type=REFS[0], payload={'value': 1}, period_ms=1, deadline_ms=1)
                def run():
                    try:
                        results.append(invoke())
                    except BaseException as error:
                        failures.append(error)
                worker = threading.Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    old_codec = runtime._rust_message_codec
                    with self.assertRaisesRegex(RuntimeError, 'active callback or Control'):
                        with runtime.codec_initialization():
                            self.fail('Active operation allowed schema replacement')
                    self.assertIs(runtime._rust_message_codec, old_codec)
                    with self.assertRaisesRegex(CodecLifecycleBusyError, 'active callback or Control'):
                        owner.initialize()
                    self.assertEqual(owner.state, DeviceState.READY)
                    self.assertTrue(owner._initialization_succeeded)
                    self.assertTrue(runtime._codec_lifecycle._available)
                    self.assertIs(runtime._rust_message_codec, old_codec)
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(results), 1)
                if role == 'provider':
                    self.assertEqual(results[0]['status'], 0)
                    metadata, payload = old_codec.decode_control_response_wire(results[0]['payload'])
                    self.assertEqual(metadata['request_id_u64'], 7)
                    self.assertEqual(old_codec.decode_message(REFS[0], payload=payload), {'value': 42})
                else:
                    self.assertEqual(results[0]['result'], {'value': 42})
                self.assertEqual(runtime._codec_lifecycle._active_callbacks, 0)

    def test_running_dispatch_blocks_even_idle_reinitialization(self):
        owner = Subscriber()
        owner.initialize()
        owner._runtime._control_dispatch_running = True
        owner._state = DeviceState.STOPPED
        old_codec = owner._runtime._rust_message_codec
        with self.assertRaisesRegex(RuntimeError, 'Stop active Control dispatch'):
            with owner._runtime.codec_initialization():
                self.fail('Running dispatch allowed replacement')
        with self.assertRaisesRegex(CodecLifecycleBusyError, 'Stop active Control dispatch'):
            owner.initialize()
        self.assertEqual(owner.state, DeviceState.STOPPED)
        self.assertTrue(owner._initialization_succeeded)
        self.assertTrue(owner._runtime._codec_lifecycle._available)
        self.assertIs(owner._runtime._rust_message_codec, old_codec)
        owner._runtime._control_dispatch_running = False
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        self.assertTrue(owner._initialization_succeeded)

    def test_public_initialize_busy_event_callback_preserves_start_authorization(self):
        owner = Subscriber()
        owner.initialize()
        lifecycle = owner._runtime._codec_lifecycle
        old_codec = lifecycle.codec
        with lifecycle.callback():
            with self.assertRaisesRegex(CodecLifecycleBusyError, 'active callback'):
                owner.initialize()
            self.assertEqual(owner.state, DeviceState.READY)
            self.assertTrue(owner._initialization_succeeded)
            self.assertTrue(lifecycle._available)
            self.assertIs(lifecycle.codec, old_codec)
        wire = old_codec.encode_message(REFS[0], {'value': 31})
        owner.peer.handlers[TOPICS[0]](wire, 1)
        self.assertEqual(owner.received, [{'value': 31}])
        owner._runtime.start_registered_event_transport = lambda: None
        owner._on_pre_start()

    def test_identity_ack_requires_explicit_ok_result(self):
        runtime = object.__new__(SubsystemRuntime)
        runtime._procedure_handler_context = threading.local()
        runtime._raw_control_endpoint_handlers = {}
        runtime._codec_lifecycle = CodecLifecycle(runtime)
        runtime._heartbeat_state_getter = lambda: 'registering'
        runtime._heartbeat_status_payload = lambda getter, **kwargs: {KW.PARAMETER: {'codec_metadata': {}, 'contract_codecs': {}}}
        for response in (None, {'ok': True}, {'ok': True, 'res': 'error'}, {'ok': False, 'res': 'ok'}):
            with self.subTest(response=response), patch('r2kernel.core.subsystem_runtime.exchange_control_request', return_value=response):
                with self.assertRaisesRegex(RuntimeError, 'publication failed'):
                    runtime.advertise_codec_identity()
        with patch('r2kernel.core.subsystem_runtime.exchange_control_request', return_value={'ok': True, 'res': 'ok'}):
            runtime.advertise_codec_identity()

    def test_failed_reinitialize_status_uses_ack_diagnostics_without_execution_fallback(self):
        owner = Subscriber()
        owner.initialize()
        lifecycle = owner._runtime._codec_lifecycle
        expected = lifecycle.metadata()
        owner.reject_status = True
        owner._state = DeviceState.STOPPED
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.ERROR)
        self.assertEqual(lifecycle.metadata(), {})
        payload = owner._runtime._heartbeat_status_payload(lambda: 'Error')[KW.PARAMETER]
        self.assertEqual(payload[KW.SUBSYSTEM_LIFECYCLE_STATUS], 'Error')
        self.assertEqual(payload['codec_metadata'], expected)
        self.assertFalse(payload['codec_context_available'])
        self.assertEqual(set(map(tuple, payload[KW.SUBSYSTEM_SUB_TOPIC_LIST])), set(zip(TOPICS, REFS)))
        self.assertIsNone(lifecycle.codec)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            with lifecycle.callback():
                self.fail('Diagnostics made the failed codec executable')
        owner.reject_status = False
        owner.initialize()
        self.assertEqual(owner.state, DeviceState.READY)
        ready = owner._runtime._heartbeat_status_payload(lambda: 'Ready')[KW.PARAMETER]
        self.assertTrue(ready['codec_context_available'])
        self.assertEqual(ready['codec_metadata'], lifecycle.metadata())

    def test_first_failure_has_no_ack_identity(self):
        owner = Subscriber()
        owner.reject_status = True
        owner.initialize()
        payload = owner._runtime._heartbeat_status_payload(lambda: 'Error')[KW.PARAMETER]
        self.assertEqual(payload['codec_metadata'], {})
        self.assertEqual(payload['contract_codecs'], {})
        self.assertFalse(payload['codec_context_available'])

    def test_only_exact_acknowledged_candidate_can_replace_diagnostics(self):
        owner = Subscriber()
        owner.initialize()
        runtime, lifecycle = owner._runtime, owner._runtime._codec_lifecycle
        previous = lifecycle.status_identity(lifecycle_state='Error')
        candidate = lifecycle.metadata()
        candidate[REFS[0]] = dict(candidate[REFS[0]], fingerprint='b' * 64)
        contracts = {'api.test.v1': {REFS[0]: candidate[REFS[0]]}}
        sent = []
        def exchange(payload, **kwargs):
            sent.append(payload[KW.PARAMETER])
            return {'ok': False, 'res': 'error'}
        with patch.object(lifecycle, 'metadata', return_value=candidate), patch.object(lifecycle, 'contract_codecs', contracts), \
             patch('r2kernel.core.subsystem_runtime.exchange_control_request', side_effect=exchange):
            with self.assertRaisesRegex(RuntimeError, 'publication failed'):
                SubsystemRuntime.advertise_codec_identity(runtime)
        self.assertEqual(sent[-1]['codec_metadata'], candidate)
        self.assertEqual(sent[-1]['contract_codecs'], contracts)
        self.assertEqual(lifecycle.status_identity(lifecycle_state='Error'), previous)
        with patch.object(lifecycle, 'metadata', return_value=candidate), patch.object(lifecycle, 'contract_codecs', contracts), \
             patch('r2kernel.core.subsystem_runtime.exchange_control_request', return_value={'ok': True, 'res': 'ok'}):
            SubsystemRuntime.advertise_codec_identity(runtime)
        approved = lifecycle.status_identity(lifecycle_state='Error')
        self.assertEqual(approved['codec_metadata'], candidate)
        self.assertEqual(approved['contract_codecs'], contracts)
        candidate[REFS[0]]['fingerprint'] = 'c' * 64
        contracts['api.test.v1'].clear()
        approved['codec_metadata'].clear()
        approved['contract_codecs'].clear()
        retained = lifecycle.status_identity(lifecycle_state='Error')
        self.assertEqual(retained['codec_metadata'][REFS[0]]['fingerprint'], 'b' * 64)
        self.assertIn(REFS[0], retained['contract_codecs']['api.test.v1'])


if __name__ == '__main__':
    unittest.main()
