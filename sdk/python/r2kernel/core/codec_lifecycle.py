"""Prepare all declared message codecs at the subsystem initialization boundary."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from functools import wraps
from copy import deepcopy
import threading
from r2kernel.contract.loader import load_api_contract_spec, load_component_spec
from r2kernel.interface.prepared_codec import PreparedSchemaContext, PreparedMessageCodec
from r2kernel.spec_paths import spec_root_candidates, frozen_spec_sources


def codec_activity(method):
    """Keep a caller/dispatch codec snapshot alive through its whole operation."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        lifecycle = getattr(self, '_codec_lifecycle', None)
        with lifecycle.activity() if lifecycle is not None else nullcontext():
            return method(self, *args, **kwargs)
    return guarded


class CodecLifecycleBusyError(RuntimeError):
    """Preparation was rejected before changing any previously valid context."""


class CodecLifecycle:
    def __init__(self, runtime):
        self.runtime = runtime
        self.codec = None
        self.generation = 0
        self.contract_codecs = {}
        self.event_declarations = {'publishing': set(), 'subscribing': set()}
        self._activity_lock = threading.Lock()
        # Pair cold lifecycle transitions with heartbeat identity snapshots.
        # Packet execution continues to use the separate activity lock.
        self._status_publication_lock = threading.RLock()
        self._initialization_finished = threading.Condition(self._activity_lock)
        self._initializing = False
        self._initialization_thread = None
        self._event_generation = 0
        self._active_callbacks = 0
        self._initialization_attempted = False
        self._available = False
        self._flushing_events = False
        self._staged_events = {}
        # Plain diagnostics only: never retain executable codec/context handles.
        self._acknowledged_identity = ({}, {})

    @contextmanager
    def initialization(self, *, on_accepted=None):
        accepted = False
        try:
            with self.status_publication():
                with self._activity_lock:
                    if self._initializing or self._active_callbacks:
                        raise CodecLifecycleBusyError('Cannot reinitialize codecs during an active callback or Control transaction')
                    if getattr(self.runtime, '_control_dispatch_running', False):
                        raise CodecLifecycleBusyError('Stop active Control dispatch before reinitializing schema snapshots')
                    self._initializing = True
                    self._initialization_thread = threading.get_ident()
                    self._event_generation += 1
                    self._initialization_attempted = True
                    self._available = False
                    self._staged_events = {}
                    accepted = True
                if on_accepted is not None:
                    on_accepted()
            with self._capture_context():
                yield
            with self.status_publication():
                with self._activity_lock:
                    self._available = True
        except BaseException:
            if accepted:
                self.invalidate()
            raise
        finally:
            if accepted:
                with self._activity_lock:
                    self._initializing = False
                    self._initialization_thread = None
                    self._flushing_events = False
                    self._staged_events = {}
                    self._initialization_finished.notify_all()

    @contextmanager
    def status_publication(self):
        with self._status_publication_lock:
            yield

    def invalidate(self):
        """A rejected context must never fall back to a generic or old codec."""
        with self._activity_lock:
            self._available = False
            self.codec = None
            self.runtime._rust_message_codec = None
            self.runtime._event._message_codec = None
            self.contract_codecs = {}
            self.event_declarations = {'publishing': set(), 'subscribing': set()}

    def require_available(self):
        if self._initialization_attempted and not self._available and not self._initializing:
            raise RuntimeError('Codec context unavailable after failed initialization')

    @contextmanager
    def _capture_context(self):
        roots = spec_root_candidates()
        context = PreparedSchemaContext(roots)
        candidate = PreparedMessageCodec(context)
        self.codec = candidate
        self.runtime._rust_message_codec = candidate
        self.runtime._event._message_codec = candidate
        self.contract_codecs = {}
        self.event_declarations = {'publishing': set(), 'subscribing': set()}
        self.runtime._control_target_cache.clear()
        with frozen_spec_sources(context, roots):
            yield

    @contextmanager
    def callback(self, *, event_generation=None):
        with self.activity(event_generation=event_generation):
            yield

    def event_generation(self):
        """Bind a broker handler to its registration's initialization attempt."""
        with self._activity_lock:
            return self._event_generation

    @contextmanager
    def activity(self, *, event_generation=None):
        with self._activity_lock:
            if event_generation is not None:
                # Broker receive workers already own the retained payload. Wait
                # before decoding it instead of consuming it on a rejected gate.
                # Condition.wait releases this lock; registration/ack never wait
                # for receiver completion. Controls retain the immediate gate.
                while self._initializing and event_generation == self._event_generation:
                    if self._initialization_thread == threading.get_ident():
                        raise RuntimeError('Broker Event delivery cannot wait on its own codec initialization')
                    self._initialization_finished.wait()
                if event_generation != self._event_generation:
                    raise RuntimeError('Broker Event handler belongs to an obsolete codec initialization')
            if self._initializing:
                raise RuntimeError('Event callback or Control transaction unavailable during codec initialization')
            self.require_available()
            owner_state = getattr(getattr(self.runtime, '_owner', None), '_state', None)
            if event_generation is not None and str(getattr(owner_state, 'value', owner_state)).lower() == 'resetting':
                raise RuntimeError('Broker Event callback unavailable during subsystem reset')
            self._active_callbacks += 1
        try:
            yield
        finally:
            with self._activity_lock:
                self._active_callbacks -= 1

    def stage_event(self, direction, topic, reference, register):
        if not self._initializing or self._flushing_events:
            return False
        if any(name == topic and ref != str(reference) for name, ref in self.event_declarations[direction]):
            raise RuntimeError(f'Conflicting staged Event type for {topic}')
        self.event_declarations[direction].add((topic, str(reference)))
        self._staged_events[(direction, topic)] = register
        return True

    def flush_events(self):
        if not self._initializing:
            raise RuntimeError('Event registration flush requires codec initialization')
        self._flushing_events = True
        try:
            for (direction, topic), register in list(self._staged_events.items()):
                if register() is False:
                    raise RuntimeError(f'Prepared Event {direction} registration rejected: {topic}')
            self._staged_events.clear()
        finally:
            self._flushing_events = False

    def prepare_event(self, direction, topic, reference):
        self.require_available()
        if self.codec is None:
            return
        if '/' in str(reference):
            self.codec.prepare_message(str(reference))
        self.event_declarations[direction].add((topic, str(reference)))
        if not self._initializing:
            self.runtime.advertise_codec_identity()

    @staticmethod
    def _api_references(api_id):
        spec = load_api_contract_spec(api_id)
        references = {spec.qualify_type_ref(endpoint.type) for endpoint in spec.events}
        for endpoint in (*spec.controls, *spec.procedures):
            references.update((spec.qualify_type_ref(endpoint.request), spec.qualify_type_ref(endpoint.response)))
        for endpoint in spec.operations:
            references.update(spec.qualify_type_ref(ref) for ref in (endpoint.request, endpoint.feedback, endpoint.result, endpoint.state) if ref)
        return references

    @classmethod
    def _contract_references(cls, contract):
        if contract.startswith('api.'):
            apis = (contract,)
        elif contract.startswith('component.'):
            apis = load_component_spec(contract).conformance_apis
        else:
            raise RuntimeError(f'Unsupported codec contract: {contract}')
        return {ref for api in apis for ref in cls._api_references(api) if '/' in ref}

    def validate_raw_control_declaration(self, topic):
        manifest = getattr(self.runtime._owner, '_subsystem_manifest', None)
        if manifest is None:
            raise ValueError('Named raw Control requires a declared private interface')
        private_declared = False
        for declaration in manifest.provides.values():
            explicit = topic in (declaration.endpoints.get('controls') or ())
            def require_raw_labels():
                labels = declaration.labels
                if ('request_type' in labels or 'response_type' in labels) and not (
                    labels.get('request_type') == 'bytes' and labels.get('response_type') == 'bytes'
                ):
                    raise ValueError(f'Raw Control {topic!r} contradicts declared request/response type labels')
            if explicit:
                require_raw_labels()
            if declaration.visibility == 'internal' and explicit:
                private_declared = True
            contract = declaration.contract
            if contract.startswith('api.'):
                apis = (contract,)
            elif contract.startswith('component.'):
                apis = load_component_spec(contract).conformance_apis
            else:
                continue
            for api_id in apis:
                api = load_api_contract_spec(api_id)
                for endpoint in api.controls:
                    if endpoint.topic != topic:
                        continue
                    require_raw_labels()
                    if api.qualify_type_ref(endpoint.request) != 'bytes' or api.qualify_type_ref(endpoint.response) != 'bytes':
                        raise ValueError(f'Raw Control {topic!r} cannot replace a typed contract endpoint')
                    if declaration.visibility == 'internal':
                        private_declared = True
        if not private_declared:
            raise ValueError(f'Raw Control {topic!r} is not a declared private raw endpoint')

    def prepare_declarations(self):
        if self.codec is None:
            raise RuntimeError('Codec preparation requires an initialization context')
        runtime, owner = self.runtime, self.runtime._owner
        references = set()
        for _, reference in (*runtime.get_publish_info(), *runtime.get_subscribe_info()):
            references.add(str(reference))
        for contracts in (runtime._control_contracts, runtime._procedure_contracts, runtime._operation_contracts):
            for contract in contracts.values():
                references.update(str(value) for key, value in contract.items() if key.endswith('_type') and value)
        api_ids = set(getattr(owner, 'declared_apis', ()) or ())
        manifest = getattr(owner, '_subsystem_manifest', None)
        declarations = [] if manifest is None else [*manifest.provides.values(), *manifest.requires.values(), *manifest.exports.values()]
        contracts = set()
        for declaration in declarations:
            # Private wiring names have no spec graph. Its registered endpoint
            # types above still participate in the same prepared snapshot.
            # Spec-shaped IDs must resolve even when the interface is internal.
            if (getattr(declaration, 'visibility', 'public') == 'internal'
                    and not declaration.contract.startswith(('api.', 'component.'))):
                continue
            contracts.add(declaration.contract)
        contracts.update(getattr(owner, 'declared_components', ()) or ())
        for contract in contracts:
            if contract.startswith('api.'):
                api_ids.add(contract)
            elif contract.startswith('component.'):
                api_ids.update(load_component_spec(contract).conformance_apis)
        contract_refs = {contract: self._contract_references(contract) for contract in contracts | api_ids}
        references.update(ref for refs in contract_refs.values() for ref in refs)
        # Operation query/cancel IDs and timing envelopes are SDK-owned messages,
        # independent of the domain contract's payload types.
        references.update(('msg.core.common.primitive.v1/StringValue',
                           'msg.internal.control.v1/RequestHeader', 'msg.internal.control.v1/ResponseHeader'))
        for reference in sorted(references):
            if '/' in reference:
                self.codec.prepare_message(reference)
        self.codec.seal()
        self.contract_codecs = {contract: {ref: self.codec.metadata(ref) for ref in sorted(refs)}
                                for contract, refs in contract_refs.items()}
        self.generation += 1
        for contracts in (runtime._control_contracts, runtime._procedure_contracts, runtime._operation_contracts):
            for contract in contracts.values():
                contract['codec'] = {key.removesuffix('_type'): self.codec.metadata(str(value))
                                     for key, value in list(contract.items()) if key.endswith('_type') and value and '/' in str(value)}

    def metadata(self):
        if self.codec is None:
            return {}
        return {reference: codec.metadata() for reference, codec in list(self.codec._prepared.items())}

    def acknowledge_identity(self, metadata, contract_codecs):
        """Cache the exact identity accepted by a strict backend status response."""
        self._acknowledged_identity = (deepcopy(metadata), deepcopy(contract_codecs))

    def status_identity(self, *, candidate=False, lifecycle_state=None):
        available = self._available and not self._initializing and self.codec is not None
        failed = str(lifecycle_state).lower() == 'error'
        if candidate or (available and not failed):
            metadata, contracts = self.metadata(), self.contract_codecs
        else:
            metadata, contracts = self._acknowledged_identity
        return {'codec_metadata': deepcopy(metadata), 'contract_codecs': deepcopy(contracts),
                'codec_context_available': bool(available and not failed)}

    def validate_route(self, route, references):
        self.require_available()
        if self.codec is None:
            return  # A directly used, uninitialized runtime retains generic APIs.
        if route.get('codec_context_available') is False:
            raise RuntimeError(f"Prepared codec context unavailable for {route.get('subsystem', 'target')}")
        peer = route.get('codec_metadata')
        for reference in references:
            if not reference or '/' not in reference:
                continue
            local = self.codec.metadata(reference)
            remote = peer.get(reference) if isinstance(peer, dict) else None
            if not isinstance(remote, dict) or remote.get('fingerprint') != local['fingerprint']:
                raise RuntimeError(f"Prepared schema mismatch for {route.get('subsystem', 'target')}: {reference}; expected {local['fingerprint']}, received {remote.get('fingerprint') if isinstance(remote, dict) else 'missing codec identity'}")
