from __future__ import annotations

from collections.abc import Mapping
import os
import queue
import threading
import time
from typing import Any, Callable


from r2kernel.datatype import DTypeLiteral
from r2kernel.ffi import R2KernelCore
from r2kernel.helper.logger import Logger
from r2kernel.manifest import BindingReference, subsystem_manifest_status_payload

from . import _keywords as KW
from .r2kernel_client import R2KernelClient
from .constants import DEFAULT_HEARTBEAT_PORT
from .control_client import exchange_control_request
from ._control_registration import HEARTBEAT_PERIOD_S, ControlRegistrationKeeper, status_refusal
from ._event_registration import EventPoolReplacedError
from .event_transport_manager import EventTransportManager
from .request_context import OperationRecord
from ._runtime_control import _ControlTransportMixin
from ._runtime_timing_headers import _TimingHeaderMixin
from ._runtime_procedures import _ProcedureMixin
from ._runtime_operations import _OperationClientMixin
from ._runtime_dispatch import _DispatchMixin
from ._runtime_operation_server import _OperationExecutionMixin
from ._runtime_timing_support import _TimingSupportMixin


LOG_ID_HEARTBEAT = "Heartbeat"


class _LaunchBindingsInterfaceError(TypeError):
    """Raised when a subsystem exposes an invalid launch_bindings interface."""


class SubsystemRuntime(
    _ControlTransportMixin,
    _TimingHeaderMixin,
    _ProcedureMixin,
    _OperationClientMixin,
    _DispatchMixin,
    _OperationExecutionMixin,
    _TimingSupportMixin,
):
    def __init__(
        self,
        *,
        subsystem_name: str,
        logger: Logger,
        owner: Any,
        heartbeat_port: int = DEFAULT_HEARTBEAT_PORT,
    ) -> None:
        self._subsystem_name = subsystem_name
        self._logger = logger
        self._owner = owner
        self._heartbeat_port = heartbeat_port
        self._client = R2KernelClient(subsystem_name=subsystem_name, logger=logger)
        self._event = EventTransportManager(logger=logger, owner=owner, client=self._client)
        try:
            self._rust_clock = R2KernelCore()
        except Exception:
            self._rust_clock = None
        self._timestamp_getter = self._default_timestamp
        self._event.set_default_timestamp_getter(self._timestamp_getter)
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_state_getter: Callable[[], str] | None = None
        self._registration_keeper = ControlRegistrationKeeper(logger=logger)
        self._registration_lock = threading.Lock()
        lifecycle_handler = getattr(owner, "_handle_lifecycle_transition_request", None)
        if callable(lifecycle_handler):
            self._client.set_lifecycle_transition_handler(lifecycle_handler)
        self._control_server_enabled = False
        self._control_handler: Callable[[dict], dict] | None = None
        self._control_endpoint_handlers: dict[str, Callable[..., Any]] = {}
        self._raw_control_endpoint_handlers: dict[str, Callable[..., dict]] = {}
        self._control_contracts: dict[str, dict[str, str | None]] = {}
        self._control_dispatch_running = False
        self._procedure_handlers: dict[str, Callable[[Any], Any]] = {}
        self._typed_procedure_handlers: dict[str, Callable[[Any], Any]] = {}
        self._procedure_contracts: dict[str, dict[str, str | None]] = {}
        self._procedure_server_enabled = False
        self._procedure_handler_context = threading.local()
        self._operation_handlers: dict[str, Callable[..., Any]] = {}
        self._operation_contracts: dict[str, dict[str, str | None]] = {}
        self._operation_server_enabled = False
        self._operation_records: dict[str, OperationRecord] = {}
        self._operation_lock = threading.RLock()
        self._operation_quiescence = threading.Condition(self._operation_lock)
        self._operation_queue: queue.Queue[str | None] = queue.Queue()
        self._operation_workers: list[threading.Thread] = []
        self._operation_publish_topics_registered: set[str] = set()
        self._operation_publish_topics_lock = threading.Lock()
        self._rust_message_codec = None
        from .codec_lifecycle import CodecLifecycle
        self._codec_lifecycle = CodecLifecycle(self)
        self._event._codec_lifecycle = self._codec_lifecycle
        self._control_target_cache: dict[str, dict[str, Any]] = {}
        self._control_target_cache_checked_at: dict[str, float] = {}
        self._control_registration_check_interval_s = max(
            1.0,
            float(os.getenv("R2KERNEL_CONTROL_REGISTRATION_CHECK_SEC", "5.0")),
        )
        self._next_control_registration_check_at = 0.0
        self._clock_status_refresh_interval_s = max(
            1.0,
            float(os.getenv("R2KERNEL_CLOCK_STATUS_REFRESH_SEC", "5.0")),
        )
        self._next_clock_status_refresh_at = 0.0

    def now_ns(self) -> int:
        return self._timestamp_getter()

    def codec_initialization(self, *, on_accepted=None):
        return self._codec_lifecycle.initialization(on_accepted=on_accepted)

    def prepare_message_codecs(self) -> None:
        self._codec_lifecycle.prepare_declarations()

    def flush_prepared_event_registrations(self) -> None:
        self._codec_lifecycle.flush_events()

    def set_timestamp_getter(self, getter: Callable[[], int]) -> None:
        self._timestamp_getter = getter
        self._event.set_default_timestamp_getter(getter)

    def register_event_publish(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        src_attr: str | None = None,
        priority: int = 5,
        queue_depth: int | None = None,
    ) -> bool:
        return self._event.register_publish(
            topic=topic,
            type=type,
            size=size,
            src_attr=src_attr,
            priority=priority,
            queue_depth=queue_depth,
        )

    def register_event_publish_on_change(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        src_attr: str | None = None,
        priority: int = 5,
        queue_depth: int | None = None,
    ) -> None:
        self._event.publish_on_change(
            topic=topic,
            type=type,
            size=size,
            src_attr=src_attr,
            priority=priority,
            queue_depth=queue_depth,
        )

    def register_event_subscribe(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        dest_attr: str | None = None,
        callback: Callable[[Any, int], None] | None = None,
    ) -> None:
        self._event.register_subscribe(
            topic=topic,
            type=type,
            size=size,
            dest_attr=dest_attr,
            callback=callback,
        )

    def publish_event(self, *, topic: str, value: Any, timestamp: int | None = None) -> bool:
        return self._event.publish_event(topic=topic, value=value, timestamp=timestamp)

    def start_event_transport(self) -> None:
        self._event.start_transport()

    def stop_event_transport(self) -> None:
        self._event.stop_transport()

    def start_registered_event_transport(self) -> None:
        if self._event.has_registrations():
            self._event.start_transport()

    def get_publish_info(self) -> list[tuple[str, str]]:
        return self._event.get_publish_info()

    def get_subscribe_info(self) -> list[tuple[str, str]]:
        return self._event.get_subscribe_info()

    def _heartbeat_status_payload(self, state_getter: Callable[[], str], *, candidate_codec_identity: bool = False) -> dict[str, Any]:
        with self._codec_lifecycle.status_publication():
            return self._build_heartbeat_status_payload(state_getter, candidate_codec_identity=candidate_codec_identity)

    def _build_heartbeat_status_payload(self, state_getter: Callable[[], str], *, candidate_codec_identity: bool = False) -> dict[str, Any]:
        lifecycle_state = state_getter()
        status_payload = {
            KW.SUBSYSTEM_NAME: self._subsystem_name,
            "session_id": self._client.session_id,
            "pid": os.getpid(),
            "lease_id": os.getenv("R2KERNEL_RESOURCE_LEASE_ID") or None,
            KW.SUBSYSTEM_LIFECYCLE_STATUS: lifecycle_state,
            "reported_lifecycle": lifecycle_state,
            KW.SUBSYSTEM_PUB_TOPIC_LIST: sorted(set(self.get_publish_info()) | self._codec_lifecycle.event_declarations['publishing']),
            KW.SUBSYSTEM_SUB_TOPIC_LIST: sorted(set(self.get_subscribe_info()) | self._codec_lifecycle.event_declarations['subscribing']),
            KW.SUBSYSTEM_CONTROL_PORT: self._get_control_port(),
            KW.SUBSYSTEM_CONTROL_ENDPOINT_LIST: self.get_control_info(),
            KW.SUBSYSTEM_PROCEDURE_LIST: self.get_procedure_info(),
            KW.SUBSYSTEM_OPERATION_LIST: self.get_operation_info(),
            KW.SUBSYSTEM_CONTROL_CONTRACTS: dict(self.get_control_contract_info()),
            KW.SUBSYSTEM_PROCEDURE_CONTRACTS: dict(self.get_procedure_contract_info()),
            KW.SUBSYSTEM_OPERATION_CONTRACTS: dict(self.get_operation_contract_info()),
        }
        status_payload.update(self._codec_lifecycle.status_identity(candidate=candidate_codec_identity,
                                                                  lifecycle_state=lifecycle_state))
        if self._client.notify_endpoint:
            status_payload["notify_endpoint"] = self._client.notify_endpoint
        status_payload.update(self._manifest_status_payload())
        if str(lifecycle_state).lower() == 'error':
            status_payload[KW.SUBSYSTEM_LIFECYCLE_STATUS] = lifecycle_state
        return {
            KW.COMMAND: KW.UPDATE_SUBSYSTEM_STATUS,
            KW.PARAMETER: status_payload,
        }

    def advertise_codec_identity(self) -> None:
        """Commit cold endpoint identity before allocating its Event resources."""
        state_getter = self._heartbeat_state_getter
        if state_getter is None:
            raise RuntimeError('Codec identity publication requires subsystem initialization')
        payload = self._heartbeat_status_payload(state_getter, candidate_codec_identity=True)
        response = exchange_control_request(payload, timeout_s=1.0, retries=1)
        if not response or response.get('ok') is not True or response.get('res') != 'ok':
            raise RuntimeError(f'Codec identity publication failed: {response}')
        identity = payload[KW.PARAMETER]
        self._codec_lifecycle.acknowledge_identity(identity['codec_metadata'], identity['contract_codecs'])

    def send_heartbeat_once(self) -> None:
        state_getter = self._heartbeat_state_getter
        if state_getter is None or self._client.down_reported:
            return
        try:
            payload = self._heartbeat_status_payload(state_getter)
        except Exception as exc:
            self._logger.error(LOG_ID_HEARTBEAT, f"Heartbeat send failed: {exc}")
            return
        # The heartbeat thread and immediate lifecycle updates share one registration state.
        with self._registration_lock:
            self._registration_keeper.heartbeat(
                lambda: self._send_status(payload),
                lambda: self._register_again(state_getter),
            )
            if self._registration_keeper.registration_lost:
                return
        self._maybe_recover_control_registration()
        now = time.monotonic()
        if now < self._next_clock_status_refresh_at:
            return
        self._next_clock_status_refresh_at = now + self._clock_status_refresh_interval_s
        try:
            self._client.refresh_clock_status(timeout_s=1.0)
        except Exception as exc:
            self._logger.debug(LOG_ID_HEARTBEAT, f"Clock refresh failed: {exc}")

    def send_lifecycle_status(self) -> None:
        """Immediate status after a lifecycle change (``fault()``, ``degrade()``, ...).

        Sends the status once and never registers again inline: when a
        re-registration holds the registration state, or the registration is
        already lost, the update is left to the next heartbeat (which carries
        the new state, and registers again when needed). A refused status is
        logged at error level; the next heartbeat finds and handles the loss.
        """
        state_getter = self._heartbeat_state_getter
        if state_getter is None or self._client.down_reported:
            return
        try:
            payload = self._heartbeat_status_payload(state_getter)
        except Exception as exc:
            self._logger.error(LOG_ID_HEARTBEAT, f"Lifecycle status update failed: {exc}")
            return
        state = payload[KW.PARAMETER][KW.SUBSYSTEM_LIFECYCLE_STATUS]
        if not self._registration_lock.acquire(blocking=False):
            self._logger.info(LOG_ID_HEARTBEAT, f"Lifecycle status {state} is left to the next heartbeat: "
                                                "a registration with Control is in progress")
            return
        try:
            if self._registration_keeper.registration_lost:
                self._logger.info(LOG_ID_HEARTBEAT, f"Lifecycle status {state} is left to the next heartbeat: "
                                                    "the registration with Control is being restored")
                return
            failure = self._send_status(payload)
        finally:
            self._registration_lock.release()
        if failure is not None:
            self._logger.error(LOG_ID_HEARTBEAT, f"Lifecycle status {state} not accepted ({failure}); "
                                                 "the next heartbeat registers again if Control lost the registration")

    @staticmethod
    def _send_status(payload: dict[str, Any]) -> str | None:
        try:
            response = exchange_control_request(payload, timeout_s=1.0, retries=1)
        except Exception as exc:
            return f"Control unreachable: {exc}"
        return status_refusal(response)

    def _register_again(self, state_getter: Callable[[], str]) -> str | None:
        """Register, publish the full status and request every endpoint again; None on success."""
        try:
            self._client.reregister_subsystem()
            failure = self._send_status(self._heartbeat_status_payload(state_getter))
            if failure is not None:
                return failure
            try:
                self._client.restore_event_registrations()
            except EventPoolReplacedError as exc:
                self._logger.error(LOG_ID_HEARTBEAT, str(exc))
            if self._control_dispatch_running and not self._client.ensure_control_server_registered():
                return "Control did not accept the Control handler registration"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    @property
    def session_id(self) -> str:
        return self._client.session_id

    def _maybe_recover_control_registration(self) -> None:
        if not self._control_dispatch_running:
            return
        now = time.monotonic()
        if now < self._next_control_registration_check_at:
            return
        self._next_control_registration_check_at = (
            now + self._control_registration_check_interval_s
        )
        try:
            route = self._resolve_control_target_cached(self._subsystem_name, refresh=True)
        except Exception as exc:
            self._logger.debug(LOG_ID_HEARTBEAT, f"Control route check failed: {exc}")
            return
        try:
            route_host = str((route or {}).get("host") or "")
            route_port = int((route or {}).get("control_port") or 0)
            route_ready = self._control_route_is_valid(route_host, route_port)
        except (TypeError, ValueError):
            route_ready = False
        if route_ready:
            return
        try:
            if self._client.ensure_control_server_registered():
                self._logger.info(
                    LOG_ID_HEARTBEAT,
                    "Control handler re-registered with control backend",
                )
        except Exception as exc:
            self._logger.debug(LOG_ID_HEARTBEAT, f"Control re-registration failed: {exc}")

    def start_heartbeat(self, *, state_getter: Callable[[], str]) -> None:
        self._heartbeat_state_getter = state_getter
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._client.start_event_transport()

        def heartbeat() -> None:
            while not self._heartbeat_stop.wait(timeout=HEARTBEAT_PERIOD_S):
                self.send_heartbeat_once()

        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        if not self._heartbeat_thread:
            return
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def inform_subsystem_down(self) -> None:
        self._client.inform_subsystem_down()

    def _manifest_status_payload(self) -> dict[str, Any]:
        try:
            manifest = getattr(self._owner, "subsystem_manifest", None)
            if manifest is None:
                return {}
            deployment = getattr(self._owner, "deployment_manifest", None)
            launch_bindings = self._snapshot_launch_bindings()
            payload = subsystem_manifest_status_payload(manifest, deployment, launch_bindings)
            spatial_getter = getattr(self._owner, "spatial_status_payload", None)
            if callable(spatial_getter):
                runtime_spatial = spatial_getter()
                if isinstance(runtime_spatial, dict):
                    payload["spatial_runtime"] = runtime_spatial
            if payload.get("binding_errors"):
                payload[KW.SUBSYSTEM_LIFECYCLE_STATUS] = "WaitingBinding"
            return payload
        except _LaunchBindingsInterfaceError:
            raise
        except Exception as exc:
            self._logger.debug(LOG_ID_HEARTBEAT, f"Manifest status payload failed: {exc}")
            return {}

    def _snapshot_launch_bindings(self) -> dict[str, BindingReference]:
        missing = object()
        try:
            bindings_value = getattr(self._owner, "launch_bindings", missing)
        except Exception as exc:
            raise _LaunchBindingsInterfaceError(
                "Could not read subsystem launch_bindings"
            ) from exc

        if bindings_value is missing:
            return {}

        if not isinstance(bindings_value, Mapping) and callable(bindings_value):
            try:
                bindings_value = bindings_value()
            except Exception as exc:
                raise _LaunchBindingsInterfaceError(
                    "Could not call subsystem launch_bindings"
                ) from exc

        if not isinstance(bindings_value, Mapping):
            raise _LaunchBindingsInterfaceError(
                "Subsystem launch_bindings must be a Mapping or a callable returning a Mapping"
            )

        try:
            launch_bindings = dict(bindings_value)
        except Exception as exc:
            raise _LaunchBindingsInterfaceError(
                "Could not snapshot subsystem launch_bindings"
            ) from exc

        for requirement, binding in launch_bindings.items():
            if not isinstance(requirement, str) or not isinstance(binding, BindingReference):
                raise _LaunchBindingsInterfaceError(
                    "Subsystem launch_bindings must map requirement names to BindingReference values"
                )
        return launch_bindings

    def _default_timestamp(self) -> int:
        if self._rust_clock:
            return int(self._rust_clock.lib.r2kernel_clock_now_ns())
        return time.time_ns()

    def _allocate_control_port(self) -> int:
        return 0

    def _ensure_control_dispatch_server(self) -> None:
        if self._control_dispatch_running:
            return
        self._client.start_control_server(handler=self._dispatch_control_request)
        self._control_dispatch_running = True

    def _stop_control_dispatch_server(self) -> None:
        if not self._control_dispatch_running:
            return
        self._client.stop_control_server()
        self._control_dispatch_running = False

    def _schedule_control_dispatch_stop(self) -> None:
        def stop_dispatch() -> None:
            try:
                self._stop_control_dispatch_server()
            except Exception as exc:
                self._logger.error(
                    "Control",
                    f"Control server stop failed: {exc}",
                )

        threading.Thread(
            target=stop_dispatch,
            daemon=True,
            name=f"r2kernel-stop-procedure-server-{self._subsystem_name}",
        ).start()
