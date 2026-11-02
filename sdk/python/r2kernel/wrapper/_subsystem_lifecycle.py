"""Subsystem lifecycle transitions, declaration validation and manifest loading."""

from __future__ import annotations

import json
import os
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from r2kernel.core.codec_lifecycle import CodecLifecycleBusyError
from r2kernel.core.sandbox import explain_permission_error
from r2kernel.datatype.common import DeviceState
from r2kernel.manifest import (
    BindingReference,
    DeploymentManifest,
    ManifestError,
    SubsystemManifest,
    load_deployment_manifest,
    load_subsystem_manifest,
    validate_required_bindings_for_subsystem,
    validate_runtime_endpoints,
)


LOG_ID_LIFECYCLE = "Lifecycle"


class _LifecycleMixin:
    """Lifecycle transitions, declaration validation and manifest/binding loading of Subsystem."""

    def initialize(self) -> None:
        with self._transition_context():
            self._initialize_phase()

    def _initialize_phase(self) -> None:
        if self._state in {DeviceState.OPERATING, DeviceState.DEGRADED}:
            raise RuntimeError('Stop the subsystem before preparing a new schema context')
        try:
            with self._runtime.codec_initialization(on_accepted=self._on_codec_initialization_accepted):
                self._run_phase(
                    label="initialize",
                    hook=self.on_initialize,
                    success_state=DeviceState.REGISTERING,
                    failure_state=DeviceState.ERROR,
                    pre=self._pre_initialize,
                    on_success=self._on_initialize_success,
                )
                if self._state != DeviceState.REGISTERING:
                    raise RuntimeError('Subsystem initialization did not complete registration')
            # Ready is externally observable, including its immediate heartbeat.
            # Publish it only after the codec context has committed availability.
            self._initialization_succeeded = True
            self._change_device_state(DeviceState.READY)
        except CodecLifecycleBusyError:
            raise
        except Exception as exc:
            self._initialization_succeeded = False
            self.logger.error(LOG_ID_LIFECYCLE, f'Codec initialization failed: {exc}')
            self._change_device_state(DeviceState.ERROR)

    def _on_codec_initialization_accepted(self) -> None:
        self._initialization_succeeded = False
        self._change_device_state(DeviceState.REGISTERING)

    def start(self) -> None:
        with self._transition_context():
            self._run_phase(
                label="start",
                hook=self.on_start,
                success_state=DeviceState.OPERATING,
                failure_state=DeviceState.ERROR,
                pre=self._on_pre_start,
            )

    def pause(self) -> bool:
        return self.request_lifecycle_transition("pause")

    def resume(self) -> bool:
        return self.request_lifecycle_transition("resume")

    def stop(self) -> bool:
        return self.request_lifecycle_transition("stop")

    def fault(self, reason: str | None = None) -> bool:
        return self.request_lifecycle_transition("fault", reason=reason)

    def degrade(self, reason: str | None = None) -> bool:
        return self.request_lifecycle_transition("degrade", reason=reason)

    def terminate(self) -> None:
        # Guard the claim itself, not only an already-published flight: a
        # Python signal handler can reenter on this thread between those steps.
        entry = self._termination_entry.__dict__
        token = object()
        try:
            if entry.setdefault("active", token) is not token:
                return
            self._terminate_once()
        finally:
            if entry.get("active") is token:
                del entry["active"]

    def _terminate_once(self) -> None:
        # A lifecycle hook may own the transition lock or be a dependency of
        # the flight owner. It must not wait for that flight.
        if getattr(self._lifecycle_transition_context, "depth", 0):
            return

        owner = threading.get_ident()
        mine = None
        try:
            with self._termination_lock:
                flight = self._termination_flight
                if flight is None:
                    done = threading.Event()
                    mine = (owner, done)
                    self._termination_flight = mine
                else:
                    flight_owner, done = flight
                    if flight_owner == owner:
                        return

            if mine is None:
                done.wait()
                return

            # Claim the flight before admission, so callers arriving while a
            # transition is still running join this flight. Never retain the
            # transition lock across the hook or teardown.
            with self._lifecycle_transition_lock:
                pass

            cleanup_started = False

            def cleanup(success: bool) -> None:
                nonlocal cleanup_started
                if cleanup_started:
                    return
                cleanup_started = True
                self._runtime.stop_heartbeat()
                self._runtime.inform_subsystem_down()

            self._run_phase(
                label="terminate",
                hook=self.on_terminate,
                success_state=DeviceState.READY,
                failure_state=DeviceState.ERROR,
                post=cleanup,
            )
        finally:
            if mine is not None:
                with self._termination_lock:
                    if self._termination_flight is mine:
                        self._termination_flight = None
                        mine[1].set()

    @contextmanager
    def _transition_context(self):
        context = self._lifecycle_transition_context
        depth = getattr(context, "depth", 0)
        try:
            context.depth = depth + 1
            yield
        finally:
            context.depth = depth

    def reset(self) -> None:
        self.request_lifecycle_transition("reset")

    def request_lifecycle_transition(self, transition: str, *, reason: str | None = None) -> bool:
        requested = str(transition or "").strip().lower().replace("-", "_")
        if requested == "start":
            self.start()
            return self._state == DeviceState.OPERATING
        if requested == "reset":
            return self._run_lifecycle_transition(
                label="reset",
                allowed_states={
                    DeviceState.FAULTED,
                    DeviceState.ERROR,
                    DeviceState.DEGRADED,
                    DeviceState.PAUSED,
                    DeviceState.STOPPED,
                },
                entering_state=DeviceState.RESETTING,
                hook=self.on_reset,
                success_state=DeviceState.READY,
                failure_state=DeviceState.FAULTED,
            )
        if requested == "pause":
            return self._run_lifecycle_transition(
                label="pause",
                allowed_states={DeviceState.READY, DeviceState.OPERATING, DeviceState.DEGRADED},
                entering_state=DeviceState.PAUSING,
                hook=self.on_pause,
                success_state=DeviceState.PAUSED,
                restore_on_false=True,
            )
        if requested == "resume":
            return self._run_lifecycle_transition(
                label="resume",
                allowed_states={DeviceState.PAUSED},
                entering_state=DeviceState.RESUMING,
                hook=self.on_resume,
                success_state=DeviceState.OPERATING,
                restore_on_false=True,
            )
        if requested == "stop":
            return self._run_lifecycle_transition(
                label="stop",
                allowed_states={
                    DeviceState.BOOTING,
                    DeviceState.REGISTERING,
                    DeviceState.WAITING_BINDING,
                    DeviceState.WAITING_TARGET,
                    DeviceState.READY,
                    DeviceState.OPERATING,
                    DeviceState.PAUSED,
                    DeviceState.DEGRADED,
                    DeviceState.FAULTED,
                    DeviceState.ERROR,
                },
                entering_state=DeviceState.STOPPING,
                hook=self.on_stop,
                success_state=DeviceState.STOPPED,
                restore_on_false=True,
            )
        if requested == "fault":
            return self._run_lifecycle_transition(
                label="fault",
                allowed_states=set(DeviceState) - {DeviceState.STOPPED, DeviceState.STOPPING},
                entering_state=DeviceState.FAULTED,
                hook=lambda: self.on_fault(reason),
                success_state=DeviceState.FAULTED,
                failure_state=DeviceState.FAULTED,
            )
        if requested == "degrade":
            return self._run_lifecycle_transition(
                label="degrade",
                allowed_states={DeviceState.READY, DeviceState.OPERATING, DeviceState.PAUSED},
                entering_state=DeviceState.DEGRADED,
                hook=lambda: self.on_degraded(reason),
                success_state=DeviceState.DEGRADED,
                restore_on_false=True,
            )
        self.logger.warning(LOG_ID_LIFECYCLE, f"Unsupported lifecycle transition requested: {transition}")
        return False

    def _handle_lifecycle_transition_request(self, message: dict[str, Any]) -> None:
        # Control names the requested transition only in "transition" (as C++ reads it).
        transition = message.get("transition")
        request_id = message.get("request_id")
        reason = message.get("reason")
        ok = self.request_lifecycle_transition(str(transition or ""), reason=str(reason) if reason is not None else None)
        outcome = "accepted" if ok else "rejected"
        self.logger.info(
            LOG_ID_LIFECYCLE,
            f"Lifecycle transition {outcome}: transition={transition} request_id={request_id}",
        )

    def _run_phase(
        self,
        *,
        label: str,
        hook: Callable[[], bool],
        success_state: DeviceState,
        failure_state: DeviceState,
        pre: Optional[Callable[[], None]] = None,
        on_success: Optional[Callable[[], None]] = None,
        post: Optional[Callable[[bool], None]] = None,
        success_msg: Optional[str] = None,
        failure_msg: Optional[str] = None,
    ) -> None:
        try:
            if pre:
                pre()
            problems_before = self._problems_logged()
            if hook():
                if on_success:
                    on_success()
                self.logger.info(LOG_ID_LIFECYCLE, success_msg or f"{label.capitalize()} succeeded")
                self._change_device_state(success_state)
                if post:
                    post(True)
            else:
                self.logger.error(LOG_ID_LIFECYCLE, failure_msg or self._with_last_problem(
                    f"{label.capitalize()} failed: {label} hook returned False (no exception)", label, problems_before))
                self._change_device_state(failure_state)
                if post:
                    post(False)
        except Exception as exc:
            self.logger.error(LOG_ID_LIFECYCLE, exc)
            explanation = explain_permission_error(exc)
            if explanation:
                self.logger.error(LOG_ID_LIFECYCLE, f"{label.capitalize()} failed with EPERM: {explanation}")
            self.logger.debug(message=traceback.format_exc())
            self._change_device_state(failure_state)
            if post:
                post(False)

    def _problems_logged(self) -> int | None:
        """Warnings and errors the calling thread has logged so far (None for a logger without the count)."""
        return getattr(self.logger, "problem_sequence", None)

    def _with_last_problem(self, message: str, label: str, problems_before: int | None) -> str:
        """``message`` plus the last warning or error this thread logged while the hook ran, if any."""
        if problems_before is not None and self._problems_logged() != problems_before:
            message += f"; last warning or error logged during {label}: {self.logger.last_problem}"
        return message

    def _run_lifecycle_transition(
        self,
        *,
        label: str,
        allowed_states: set[DeviceState],
        entering_state: DeviceState,
        hook: Callable[[], bool],
        success_state: DeviceState,
        failure_state: DeviceState | None = None,
        restore_on_false: bool = False,
    ) -> bool:
        with self._transition_context(), self._lifecycle_transition_lock:
            previous_state = self._state
            if previous_state not in allowed_states:
                self.logger.warning(
                    LOG_ID_LIFECYCLE,
                    f"Lifecycle transition {label} rejected from {previous_state.value}",
                )
                return False
            self._change_device_state(entering_state)
            try:
                problems_before = self._problems_logged()
                if hook():
                    self.logger.info(LOG_ID_LIFECYCLE, f"Lifecycle transition {label} succeeded")
                    self._change_device_state(success_state)
                    return True
                next_state = previous_state if restore_on_false else failure_state or previous_state
                self.logger.warning(LOG_ID_LIFECYCLE, self._with_last_problem(
                    f"Lifecycle transition {label} was not applied", label, problems_before))
                self._change_device_state(next_state)
                return False
            except Exception as exc:
                self.logger.error(LOG_ID_LIFECYCLE, exc)
                self.logger.debug(message=traceback.format_exc())
                self._change_device_state(failure_state or DeviceState.FAULTED)
                return False

    def _change_device_state(self, state: DeviceState) -> None:
        if self._state != state:
            self._state = state
            self._logger.debug(LOG_ID_LIFECYCLE, f"State changed to {[state.value]}")
            try:
                self.on_state_changed(state)
            except Exception as exc:
                self._logger.error(LOG_ID_LIFECYCLE, f"on_state_changed failed: {exc}")
            try:
                self._runtime.send_lifecycle_status()
            except Exception as exc:
                self._logger.error(LOG_ID_LIFECYCLE, f"Immediate lifecycle status update failed: {exc}")

    def _pre_initialize(self) -> None:
        self._runtime.start_heartbeat(state_getter=lambda: self._state.value)

    def _on_initialize_success(self) -> None:
        self._runtime.prepare_message_codecs()
        self._validate_declarations()
        self._runtime.start_heartbeat(state_getter=lambda: self._state.value)
        self._runtime.advertise_codec_identity()
        self._runtime.flush_prepared_event_registrations()
        self._change_device_state(DeviceState.REGISTERING)

    def _on_pre_start(self) -> None:
        if not self._initialization_succeeded or self._state not in {DeviceState.READY, DeviceState.STOPPED}:
            raise RuntimeError('Start requires successful initialization and Ready or Stopped state')
        self._runtime.start_registered_event_transport()

    def _validate_declarations(self) -> None:
        endpoint_issues = validate_runtime_endpoints(self._subsystem_manifest, self._runtime)
        if endpoint_issues:
            raise RuntimeError("; ".join(issue.message for issue in endpoint_issues))
        if self._deployment_manifest is not None:
            resolved = validate_required_bindings_for_subsystem(self._subsystem_manifest, self._deployment_manifest)
            if resolved:
                summary = ", ".join(f"{item.requirement}->{item.target_subsystem}.{item.provider_interface}" for item in resolved)
                self.logger.info(LOG_ID_LIFECYCLE, f"Binding validation passed [{summary}]")
        elif self._launch_bindings:
            summary = ", ".join(f"{name}->{target.format()}" for name, target in self._launch_bindings.items())
            self.logger.info(LOG_ID_LIFECYCLE, f"Launch binding validation passed [{summary}]")

        if self.declared_apis or self.declared_components:
            from r2kernel.contract import validate_declared_contracts

            report = validate_declared_contracts(self, self._runtime)
            if report.ok:
                api_ids = ", ".join(spec.id for spec in report.api_contracts)
                component_ids = ", ".join(component.id for component in report.components) if report.components else "-"
                self.logger.info(
                    LOG_ID_LIFECYCLE,
                    f"Contract validation passed [components={component_ids}; api_contracts={api_ids}]",
                )
            else:
                raise RuntimeError(report.format_errors())
        elif getattr(self, "implements", ()):
            from r2kernel.interface import validate_declared_interfaces

            report = validate_declared_interfaces(self, self._runtime)
            if report.ok:
                interface_ids = ", ".join(spec.id for spec in report.specs)
                self.logger.info(LOG_ID_LIFECYCLE, f"Interface validation passed [{interface_ids}]")
            else:
                raise RuntimeError(report.format_errors())

        from r2kernel.contract import validate_runtime_contract_types

        runtime_contract_report = validate_runtime_contract_types(self._runtime, codec=self._runtime._get_rust_message_codec())
        if runtime_contract_report.ok:
            if runtime_contract_report.issues:
                messages = "; ".join(issue.message for issue in runtime_contract_report.issues)
                self.logger.warning(LOG_ID_LIFECYCLE, f"Runtime contract type validation warnings: {messages}")
                return
            if runtime_contract_report.policy == "strict":
                self.logger.info(LOG_ID_LIFECYCLE, "Runtime contract type validation passed [policy=strict]")
            return
        message = runtime_contract_report.format_errors()
        if runtime_contract_report.policy == "strict":
            raise RuntimeError(message)
        self.logger.warning(LOG_ID_LIFECYCLE, f"Runtime contract type validation warnings: {message}")

    def _load_required_subsystem_manifest(self, manifest_path: str | Path | None) -> SubsystemManifest:
        path = self._resolve_subsystem_manifest_path(manifest_path)
        if path is None:
            raise RuntimeError(
                f"Subsystem manifest is required for {self._name}. Pass manifest_path, set "
                f"R2KERNEL_SUBSYSTEM_MANIFEST, or place {self._name}.subsystem.yaml or subsystem.yaml in the "
                "working directory or two directories above the user data path."
            )
        try:
            manifest = load_subsystem_manifest(path)
        except ManifestError as exc:
            raise RuntimeError(str(exc)) from exc
        if manifest.name != self._name:
            raise RuntimeError(
                f"Subsystem name {self._name!r} does not match manifest subsystem.name {manifest.name!r}: {manifest.path}"
            )
        return manifest

    def _validate_expected_role(self) -> None:
        if self.expected_role is None:
            return
        if self._subsystem_manifest.role != self.expected_role:
            raise RuntimeError(
                f"{self.__class__.__name__} requires subsystem.role {self.expected_role!r}, "
                f"but manifest {self._subsystem_manifest.path} declares {self._subsystem_manifest.role!r}"
            )

    def _ensure_control_role_allowed(self) -> None:
        role = self._subsystem_manifest.role
        if role in {"app", "compound"}:
            raise RuntimeError(
                f"Control is not available to {role} subsystems; use Event, Procedure, or Operation"
            )

    def _load_deployment_manifest_if_needed(self, deployment_manifest_path: str | Path | None) -> DeploymentManifest | None:
        path = self._resolve_deployment_manifest_path(deployment_manifest_path)
        if path is None:
            required_interfaces = [
                name
                for name, requirement in self._subsystem_manifest.requires.items()
                if requirement.required and name not in self._launch_bindings
            ]
            if required_interfaces:
                required = ", ".join(required_interfaces)
                self.logger.warning(
                    LOG_ID_LIFECYCLE,
                    f"Subsystem {self.name} is waiting for runtime binding(s): {required}",
                )
                self._change_device_state(DeviceState.WAITING_BINDING)
            return None
        try:
            manifest = load_deployment_manifest(path)
            validate_required_bindings_for_subsystem(self._subsystem_manifest, manifest)
            return manifest
        except ManifestError as exc:
            raise RuntimeError(str(exc)) from exc

    def _load_launch_bindings(self) -> dict[str, BindingReference]:
        raw = os.getenv("R2KERNEL_BINDINGS")
        if raw is None or not raw.strip():
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"R2KERNEL_BINDINGS must be a JSON object: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("R2KERNEL_BINDINGS must be a JSON object")
        bindings: dict[str, BindingReference] = {}
        for requirement, target in value.items():
            requirement_text = str(requirement or "").strip()
            if not requirement_text:
                raise RuntimeError("R2KERNEL_BINDINGS contains an empty requirement name")
            if requirement_text not in self._subsystem_manifest.requires:
                raise RuntimeError(
                    f"R2KERNEL_BINDINGS references unknown required interface for {self.name}: {requirement_text}"
                )
            target_text = str(target or "").strip()
            bindings[requirement_text] = BindingReference.parse(
                target_text,
                field_name=f"R2KERNEL_BINDINGS.{requirement_text}",
            )
        return bindings

    def _resolve_subsystem_manifest_path(self, manifest_path: str | Path | None) -> Path | None:
        candidates: list[Path] = []
        explicit = manifest_path or os.getenv("R2KERNEL_SUBSYSTEM_MANIFEST")
        if explicit:
            candidates.append(Path(explicit).expanduser())
        candidates.extend(
            [
                self._user_data_root.parent.parent / f"{self._name}.subsystem.yaml",
                self._user_data_root.parent.parent / "subsystem.yaml",
                Path.cwd() / f"{self._name}.subsystem.yaml",
                Path.cwd() / "subsystem.yaml",
            ]
        )
        for candidate in candidates:
            path = candidate.resolve()
            if path.exists():
                return path
        return None

    def _resolve_deployment_manifest_path(self, deployment_manifest_path: str | Path | None) -> Path | None:
        candidates: list[Path] = []
        explicit = deployment_manifest_path or os.getenv("R2KERNEL_DEPLOYMENT_MANIFEST")
        if explicit:
            candidates.append(Path(explicit).expanduser())
        manifest_parent = self._subsystem_manifest.path.parent
        candidates.extend(
            [
                manifest_parent / "r2kernel.deployment.yaml",
                manifest_parent.parent / "r2kernel.deployment.yaml",
                Path.cwd() / "r2kernel.deployment.yaml",
            ]
        )
        for candidate in candidates:
            path = candidate.resolve()
            if path.exists():
                return path
        return None
