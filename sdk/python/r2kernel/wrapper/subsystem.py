from __future__ import annotations

import math
import threading
import time
from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from r2kernel.core import SubsystemRuntime
from r2kernel.core.constants import DEFAULT_HEARTBEAT_PORT
from r2kernel.core.control_client import exchange_control_request
from r2kernel.datatype.common import DeviceState
from r2kernel.helper.config import Config
from r2kernel.helper.logger import Logger, Loggable
from r2kernel.manifest import (
    BindingReference,
    BoundInterface,
    DeploymentManifest,
    InterfaceDeclaration,
    ManifestError,
    SubsystemManifest,
    allowed_runtime_endpoints,
)
from ._subsystem_communication import _CommunicationMixin
from ._subsystem_lifecycle import LOG_ID_LIFECYCLE, _LifecycleMixin
from ._subsystem_self_binding import _SelfBindingMixin


class Subsystem(_CommunicationMixin, _LifecycleMixin, _SelfBindingMixin, Loggable):
    implements: tuple[object, ...] = ()
    declares_apis: tuple[object, ...] = ()
    declares_components: tuple[object, ...] = ()
    expected_role: str | None = None

    def __init__(
        self,
        *,
        name: str,
        log_level: str = "INFO",
        user_data_path: str = "./userdata/",
        manifest_path: str | Path | None = None,
        deployment_manifest_path: str | Path | None = None,
        heartbeat_port: int = DEFAULT_HEARTBEAT_PORT,
        **kwargs,
    ) -> None:
        self._name = name
        self._log_level = log_level
        self._user_data_path = user_data_path
        self._heartbeat_port = heartbeat_port

        self._user_data_root = Path(self._user_data_path).resolve()
        self._logger: Optional[Logger] = Logger(
            name=self._name,
            log_file_folder=self._user_data_root / "log",
            level=self._log_level,
        )
        self._config = Config(config_file_path=self._user_data_root / "config")
        self._config.load_config()
        self._state: DeviceState = DeviceState.BOOTING
        self._initialization_succeeded = False
        self._lifecycle_transition_lock = threading.RLock()
        self._lifecycle_transition_context = threading.local()
        self._termination_lock = threading.RLock()
        self._termination_flight: tuple[int, threading.Event] | None = None
        self._termination_entry = threading.local()
        self._spatial_lock = threading.RLock()
        self._spatial_transforms: dict[str, dict[str, Any]] = {}
        self._subsystem_manifest = self._load_required_subsystem_manifest(manifest_path)
        self._validate_expected_role()
        self._launch_bindings = self._load_launch_bindings()
        self._deployment_manifest = self._load_deployment_manifest_if_needed(deployment_manifest_path)
        self._runtime = SubsystemRuntime(
            subsystem_name=self._name,
            logger=self.logger,
            owner=self,
            heartbeat_port=self._heartbeat_port,
        )
        self._declared_apis: list[str] = list(self._subsystem_manifest.declared_apis)
        self._declared_components: list[str] = list(self._subsystem_manifest.declared_components)
        self._allowed_runtime_endpoints = allowed_runtime_endpoints(self._subsystem_manifest)

    @property
    def name(self) -> str:
        return self._name

    @property
    def logger(self) -> Logger:
        return self._logger

    @property
    def state(self) -> DeviceState:
        return self._state

    def now_ns(self) -> int:
        return self._runtime.now_ns()

    def set_timestamp_getter(self, getter: Callable[[], int]) -> None:
        self._runtime.set_timestamp_getter(getter)

    @property
    def declared_apis(self) -> tuple[str, ...]:
        return tuple(self._declared_apis)

    @property
    def declared_components(self) -> tuple[str, ...]:
        return tuple(self._declared_components)

    @property
    def subsystem_manifest(self) -> SubsystemManifest:
        return self._subsystem_manifest

    @property
    def deployment_manifest(self) -> DeploymentManifest | None:
        return self._deployment_manifest

    @property
    def launch_bindings(self) -> dict[str, BindingReference]:
        return dict(self._launch_bindings)

    def runtime_bindings(self) -> dict[str, BindingReference]:
        try:
            response = exchange_control_request(
                {"cmd": "binding_status", "subsystem": self.name},
                timeout_s=1.0,
                retries=1,
            )
        except Exception as exc:
            self.logger.debug(LOG_ID_LIFECYCLE, f"Runtime binding status unavailable: {exc}")
            return {}
        if not isinstance(response, dict):
            return {}
        bindings: dict[str, BindingReference] = {}
        for item in response.get("bindings") or []:
            if not isinstance(item, dict):
                continue
            if item.get("source_subsystem") != self.name:
                continue
            requirement = str(item.get("requirement") or "").strip()
            target = str(item.get("target") or "").strip()
            if not requirement or not target:
                continue
            if requirement not in self._subsystem_manifest.requires:
                continue
            try:
                bindings[requirement] = BindingReference.parse(
                    target,
                    field_name=f"runtime binding {self.name}.{requirement}",
                )
            except ManifestError as exc:
                self.logger.warning(LOG_ID_LIFECYCLE, f"Ignoring invalid runtime binding: {exc}")
        return bindings

    def effective_bindings(self) -> dict[str, BindingReference]:
        bindings = dict(self._launch_bindings)
        bindings.update(self.runtime_bindings())
        return bindings

    def set_frame_transform(
        self,
        *,
        parent_frame: str,
        child_frame: str,
        translation: tuple[float, float, float] | list[float],
        rotation_xyzw: tuple[float, float, float, float] | list[float],
        observed_at_sync_ns: int | None = None,
        static: bool = False,
        stale_after_ms: int = 3000,
    ) -> None:
        """Report the latest transform owned by this subsystem.

        Frame names are local to the subsystem session. The runtime qualifies
        them and composes them with compound mounts; this is not an Event or a
        user-visible contract endpoint.
        """

        parent = str(parent_frame or "").strip()
        child = str(child_frame or "").strip()
        if not parent or not child or parent == child:
            raise ValueError("parent_frame and child_frame must be distinct non-empty names")
        xyz = self._spatial_vector(translation, 3, "translation")
        quat = self._spatial_vector(rotation_xyzw, 4, "rotation_xyzw")
        norm = math.sqrt(sum(value * value for value in quat))
        if norm < 1e-9:
            raise ValueError("rotation_xyzw must be a non-zero quaternion")
        quat = tuple(value / norm for value in quat)
        if not static and int(stale_after_ms) <= 0:
            raise ValueError("stale_after_ms must be positive for dynamic transforms")
        transform = {
            "parent_frame": parent,
            "child_frame": child,
            "translation": list(xyz),
            "rotation_xyzw": list(quat),
            "observed_at_sync_ns": int(observed_at_sync_ns or self.now_ns()),
            "static": bool(static),
            "stale_after_ms": 0 if static else int(stale_after_ms),
        }
        with self._spatial_lock:
            self._spatial_transforms[child] = transform
        self._publish_frame_transform(child, transform)

    def clear_frame_transform(self, child_frame: str) -> bool:
        child = str(child_frame or "").strip()
        with self._spatial_lock:
            removed = self._spatial_transforms.pop(child, None) is not None
        if removed:
            self._publish_frame_transform(child, None)
        return removed

    def frame_transforms(self) -> tuple[dict[str, Any], ...]:
        with self._spatial_lock:
            return tuple(dict(value) for _, value in sorted(self._spatial_transforms.items()))

    def lookup_frame_transform(self, source_frame: str, target_frame: str) -> dict[str, Any]:
        source = str(source_frame or "").strip()
        target = str(target_frame or "").strip()
        if not source or not target:
            raise ValueError("source_frame and target_frame are required")
        return exchange_control_request(
            {"cmd": "frame_lookup", "source": source, "target": target},
            timeout_s=1.0,
            retries=1,
        )

    def spatial_status_payload(self) -> dict[str, Any]:
        return {"transforms": list(self.frame_transforms())}

    def _publish_frame_transform(self, child_frame: str, transform: dict[str, Any] | None) -> None:
        payload: dict[str, Any] = {
            "cmd": "update_frame_transform",
            "subsystem": self.name,
            "session_id": self._runtime.session_id,
            "child_frame": child_frame,
        }
        if transform is None:
            payload["remove"] = True
        else:
            payload["transform"] = transform
        try:
            exchange_control_request(payload, timeout_s=0.25, retries=1, expect_response=False)
        except Exception as exc:
            # The next heartbeat carries the complete transform snapshot for recovery.
            self.logger.debug(LOG_ID_LIFECYCLE, f"Frame transform update deferred to heartbeat: {exc}")

    @staticmethod
    def _spatial_vector(value: Any, length: int, field_name: str) -> tuple[float, ...]:
        if not isinstance(value, (list, tuple)) or len(value) != length:
            raise ValueError(f"{field_name} must contain exactly {length} numbers")
        try:
            result = tuple(float(item) for item in value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must contain only numbers") from exc
        if not all(math.isfinite(item) for item in result):
            raise ValueError(f"{field_name} must contain finite numbers")
        return result

    def declare_api_contract(self, api_id: str) -> None:
        raise RuntimeError("Code-level declare_api_contract is disabled; declare subsystem contracts in subsystem.yaml")

    def declare_component(self, component_id: str) -> None:
        raise RuntimeError("Code-level declare_component is disabled; declare subsystem contracts in subsystem.yaml")

    def resolve_binding(self, requirement: str, *, deadline: float | None = None) -> BoundInterface:
        if deadline is None:
            runtime = self._resolve_runtime_binding(requirement)
        else:
            runtime = self.resolve_runtime_binding(requirement, deadline=deadline)
            if runtime is None:
                raise RuntimeError(f"Runtime binding is not ready: {self.name}.{requirement}")
        if runtime is not None:
            return runtime
        direct = self._launch_bindings.get(requirement)
        if direct is not None:
            required = self._subsystem_manifest.requires.get(requirement)
            if required is None:
                raise RuntimeError(f"Subsystem {self.name} does not declare required interface: {requirement}")
            if self._deployment_manifest is not None:
                return self._deployment_manifest.resolve(
                    self.name,
                    requirement,
                    target_override=direct,
                )
            return BoundInterface(
                requirement=requirement,
                target_subsystem=direct.subsystem,
                provider_interface=direct.interface,
                contract=required.contract,
            )
        if self._deployment_manifest is None:
            raise RuntimeError(f"Required binding is missing: {self.name}.{requirement}")
        return self._deployment_manifest.resolve(self.name, requirement)

    def resolve_runtime_binding(
        self, requirement_name: str, *, deadline: float | None = None,
    ) -> Optional[BoundInterface]:
        """Resolve an actual runtime binding without consulting launch manifests.

        A missing or not-yet-registered binding returns ``None``. Malformed
        responses and runtime binding errors raise ``RuntimeError``.
        """
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Control binding resolution deadline exceeded")
        required = self._subsystem_manifest.requires.get(requirement_name)
        if required is None:
            raise RuntimeError(
                f"Subsystem {self.name} does not declare required interface: {requirement_name}"
            )
        try:
            response = exchange_control_request(
                {
                    "cmd": "binding_resolve",
                    "subsystem": self.name,
                    "requirement": requirement_name,
                },
                timeout_s=1.0 if deadline is None else deadline - time.monotonic(),
                retries=1,
                **({"deadline": deadline} if deadline is not None else {}),
            )
        except Exception as exc:
            if deadline is not None and isinstance(exc, TimeoutError):
                raise
            raise RuntimeError(
                f"Runtime binding resolve failed for {self.name}.{requirement_name}: {exc}"
            ) from exc
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Control binding resolution deadline exceeded")

        if not isinstance(response, dict):
            raise RuntimeError(
                f"Runtime binding resolve returned a malformed response for {self.name}.{requirement_name}"
            )

        response_ok = response.get("ok")
        response_kind = response.get("res")
        if response_ok is False and response_kind == "unbound" and response.get("runtime") is True:
            self._validate_runtime_binding_response_identity(response, requirement_name)
            raise RuntimeError(f"Runtime binding is explicitly unbound: {self.name}.{requirement_name}")

        if response_ok is False and response_kind == "missing":
            reason = response.get("reason")
            self._validate_runtime_binding_response_identity(response, requirement_name)
            if reason == "not_bound":
                if response.get("binding") is not None:
                    raise RuntimeError(
                        f"Runtime binding resolve returned a malformed not_bound response for "
                        f"{self.name}.{requirement_name}"
                    )
                return None
            if reason == "source_registration_pending":
                binding = response.get("binding")
                if not isinstance(binding, dict):
                    raise RuntimeError(
                        f"Runtime binding resolve returned a malformed pending binding for "
                        f"{self.name}.{requirement_name}"
                    )
                self._validate_runtime_binding_identity(binding, requirement_name)
                state = str(binding.get("state") or "").strip()
                # A restarted source asks before its first status; a binding pinned to
                # its previous registration then reports registration_changed.
                if state not in {"source_missing", "source_role_missing", "registration_changed"}:
                    raise RuntimeError(
                        f"Runtime binding pending response has unexpected state for "
                        f"{self.name}.{requirement_name}: {state or 'unknown'}"
                    )
                return None
            detail = reason or response.get("error") or "unknown missing-binding reason"
            raise RuntimeError(
                f"Runtime binding resolve failed for {self.name}.{requirement_name}: {detail}"
            )

        if response_ok is True and response_kind == "ok":
            binding = response.get("binding")
            if not isinstance(binding, dict):
                raise RuntimeError(
                    f"Runtime binding resolve returned a malformed binding for {self.name}.{requirement_name}"
                )
            self._validate_runtime_binding_identity(binding, requirement_name)
            if binding.get("required_contract") != required.contract:
                raise RuntimeError(
                    f"Runtime binding contract mismatch for {self.name}.{requirement_name}: "
                    f"expected {required.contract}, got {binding.get('required_contract')!r}"
                )
            if binding.get("contract") != required.contract:
                raise RuntimeError(
                    f"Runtime binding contract mismatch for {self.name}.{requirement_name}: "
                    f"expected {required.contract}, got {binding.get('contract')!r}"
                )
            if binding.get("provided_contract") != required.contract:
                raise RuntimeError(
                    f"Runtime binding provider contract mismatch for {self.name}.{requirement_name}: "
                    f"expected {required.contract}, got {binding.get('provided_contract')!r}"
                )
            resolved_target_subsystem = binding.get("resolved_target_subsystem")
            resolved_provider_interface = binding.get("resolved_provider_interface")
            if not isinstance(resolved_target_subsystem, str) or not resolved_target_subsystem.strip():
                raise RuntimeError(
                    f"Runtime binding is not callable: {self.name}.{requirement_name} "
                    "has no resolved provider target"
                )
            if not isinstance(resolved_provider_interface, str) or not resolved_provider_interface.strip():
                raise RuntimeError(
                    f"Runtime binding is not callable: {self.name}.{requirement_name} "
                    "has no resolved provider target"
                )
            return self._bound_interface_from_runtime_binding(requirement_name, required, binding)

        detail = response.get("reason") or response.get("error") or response_kind or "unknown response"
        raise RuntimeError(
            f"Runtime binding resolve returned an invalid response for "
            f"{self.name}.{requirement_name}: {detail}"
        )

    def _validate_runtime_binding_response_identity(self, response: dict[str, Any], requirement: str) -> None:
        expected_source = f"{self.name}.{requirement}"
        if response.get("subsystem") != self.name:
            raise RuntimeError(
                f"Runtime binding response subsystem mismatch for {self.name}.{requirement}"
            )
        if response.get("requirement") != requirement:
            raise RuntimeError(
                f"Runtime binding response requirement mismatch for {self.name}.{requirement}"
            )
        if response.get("source") != expected_source:
            raise RuntimeError(
                f"Runtime binding response source mismatch for {self.name}.{requirement}"
            )

    def _validate_runtime_binding_identity(self, binding: dict[str, Any], requirement: str) -> None:
        expected_source = f"{self.name}.{requirement}"
        if binding.get("runtime") is not True:
            raise RuntimeError(
                f"Runtime binding response is not marked runtime for {self.name}.{requirement}"
            )
        if binding.get("source_subsystem") != self.name:
            raise RuntimeError(
                f"Runtime binding source subsystem mismatch for {self.name}.{requirement}"
            )
        if binding.get("requirement") != requirement:
            raise RuntimeError(
                f"Runtime binding requirement mismatch for {self.name}.{requirement}"
            )
        if binding.get("source") != expected_source:
            raise RuntimeError(
                f"Runtime binding source mismatch for {self.name}.{requirement}"
            )

    def _resolve_runtime_binding(self, requirement: str) -> BoundInterface | None:
        required = self._subsystem_manifest.requires.get(requirement)
        if required is None:
            raise RuntimeError(f"Subsystem {self.name} does not declare required interface: {requirement}")
        try:
            response = exchange_control_request(
                {
                    "cmd": "binding_resolve",
                    "subsystem": self.name,
                    "requirement": requirement,
                },
                timeout_s=1.0,
                retries=1,
            )
        except Exception as exc:
            self.logger.debug(LOG_ID_LIFECYCLE, f"Runtime binding resolve unavailable: {exc}")
            return None
        if not isinstance(response, dict):
            return None
        if response.get("res") == "unbound" and response.get("runtime"):
            raise RuntimeError(f"Runtime binding is explicitly unbound: {self.name}.{requirement}")
        if not response.get("ok"):
            return None
        binding = response.get("binding")
        if not isinstance(binding, dict):
            return None
        return self._bound_interface_from_runtime_binding(requirement, required, binding)

    def _bound_interface_from_runtime_binding(
        self,
        requirement: str,
        required: InterfaceDeclaration,
        binding: dict[str, Any],
    ) -> BoundInterface:
        state = str(binding.get("state") or "").strip()
        if state != "bound":
            raise RuntimeError(
                f"Runtime binding is not callable: {self.name}.{requirement} state={state or 'unknown'}"
            )
        target_subsystem = str(binding.get("resolved_target_subsystem") or "").strip()
        provider_interface = str(binding.get("resolved_provider_interface") or "").strip()
        if not target_subsystem or not provider_interface:
            raise RuntimeError(
                f"Runtime binding is not callable: {self.name}.{requirement} "
                "has no resolved provider target"
            )
        return BoundInterface(
            requirement=requirement,
            target_subsystem=target_subsystem,
            provider_interface=provider_interface,
            contract=required.contract,
        )

    def binding_target(self, requirement: str, *, deadline: float | None = None) -> str:
        return self.resolve_binding(
            requirement, **({"deadline": deadline} if deadline is not None else {}),
        ).target

    @property
    def config_data(self) -> dict[str, Any]:
        return self._config.data

    def config_get(self, *path: str | list[str] | tuple[str, ...], default: Any = None):
        return self._config.get_config_value(*path, default=default)

    def config_set(self, value: Any, *path: str | list[str] | tuple[str, ...], save: bool = False) -> None:
        self._config.set_config_value(value, *path)
        if save:
            self._config.save_config()

    def config_reload(self) -> dict[str, Any]:
        self._config.load_config()
        return self._config.data

    def config_save(self) -> None:
        self._config.save_config()

    @abstractmethod
    def on_initialize(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def on_start(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def on_terminate(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def on_reset(self) -> bool:
        raise NotImplementedError

    def on_pause(self) -> bool:
        return False

    def on_resume(self) -> bool:
        return False

    def on_stop(self) -> bool:
        return False

    def on_fault(self, reason: str | None = None) -> bool:
        del reason
        return True

    def on_degraded(self, reason: str | None = None) -> bool:
        del reason
        return True

    def on_state_changed(self, state: DeviceState) -> None:
        del state


if TYPE_CHECKING:
    class _TypingSubsystem(Subsystem):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
else:
    class _TypingSubsystem:
        def __init__(self, **kwargs):
            super().__init__(**kwargs)


class GatewaySubsystem(Subsystem):
    expected_role = "gateway"

    def _declared_control_target(self, provider_interface: str) -> tuple[str, str]:
        if not isinstance(provider_interface, str) or not provider_interface.strip():
            raise RuntimeError("Gateway Control provider interface must be a non-empty string")
        interface_name = provider_interface.strip()
        if interface_name not in self._subsystem_manifest.provides:
            raise RuntimeError(
                f"Gateway Control provider interface is not declared: "
                f"{self.name}.{interface_name}"
            )
        return interface_name, f"{self.name}.{interface_name}"

    def bound_control_sessions(self, provider_interface: str) -> dict[str, str]:
        """Return ``source_reference -> source_subsystem`` for bound sources.

        Each call queries current runtime binding status. A ``bound`` state
        confirms binding topology and contract checks; it does not guarantee a
        live source session or handler.
        """
        interface_name, target = self._declared_control_target(provider_interface)
        try:
            response = exchange_control_request(
                {"cmd": "binding_status", "subsystem": self.name},
                retries=1,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Runtime binding status is unavailable for gateway Control target {target}: {exc}"
            ) from exc

        if (
            not isinstance(response, dict)
            or response.get("ok") is not True
            or response.get("res") != "ok"
        ):
            raise RuntimeError(
                f"Runtime binding status is unsuccessful or malformed for gateway Control target {target}"
            )

        bindings = response.get("bindings")
        if not isinstance(bindings, list):
            raise RuntimeError(
                f"Runtime binding status has no structured bindings list for gateway Control target {target}"
            )

        source_bindings: dict[str, str] = {}
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                raise RuntimeError(
                    f"Runtime binding status contains a malformed binding at index {index}"
                )
            binding_target = binding.get("target")
            if not isinstance(binding_target, str):
                raise RuntimeError(
                    f"Runtime binding status contains a binding without a valid target at index {index}"
                )
            if not binding_target.strip():
                if binding.get("state") == "unbound" and binding.get("explicit_unbound") is True:
                    continue
                raise RuntimeError(
                    f"Runtime binding status contains a binding without a valid target at index {index}"
                )

            target_metadata_matches = (
                binding.get("target_subsystem") == self.name
                and binding.get("provider_interface") == interface_name
            )
            if binding_target == target or target_metadata_matches:
                if binding.get("target") != target:
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has inconsistent target metadata"
                    )
                if binding.get("target_subsystem") not in (None, self.name):
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has inconsistent target subsystem"
                    )
                if binding.get("provider_interface") != interface_name:
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has inconsistent provider interface"
                    )
                state = binding.get("state")
                if state != "bound":
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} is not bound "
                        f"(state={state!r})"
                    )

                source_subsystem = binding.get("source_subsystem")
                if not isinstance(source_subsystem, str) or not source_subsystem.strip():
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has no valid source subsystem"
                    )
                source_reference = binding.get("source")
                if not isinstance(source_reference, str) or not source_reference.strip():
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has no valid source reference"
                    )
                try:
                    parsed_source = BindingReference.parse(
                        source_reference,
                        field_name=f"runtime binding source for {target}",
                    )
                except ManifestError as exc:
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has an invalid source reference"
                    ) from exc
                normalized_source_subsystem = source_subsystem.strip()
                if parsed_source.subsystem != normalized_source_subsystem:
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has inconsistent source metadata"
                    )
                requirement = binding.get("requirement")
                if requirement is not None and requirement != parsed_source.interface:
                    raise RuntimeError(
                        f"Runtime binding for gateway Control interface {target} has inconsistent source requirement"
                    )

                source_identity = parsed_source.format()
                if source_identity in source_bindings:
                    raise RuntimeError(
                        f"Ambiguous runtime bindings for gateway Control interface {target}: "
                        f"duplicate source reference {source_identity}"
                    )
                source_bindings[source_identity] = normalized_source_subsystem

        return source_bindings

    def bound_control_authority(self, provider_interface: str) -> dict[str, Any]:
        """Return the exact current owner registration and binding epoch."""
        interface_name, target = self._declared_control_target(provider_interface)
        response = exchange_control_request({"cmd": "binding_status", "subsystem": self.name}, retries=1)
        if not isinstance(response, dict) or response.get("ok") is not True or response.get("res") != "ok":
            raise RuntimeError(f"Runtime binding authority is unavailable for {target}")
        bindings = response.get("bindings")
        if not isinstance(bindings, list):
            raise RuntimeError(f"Runtime binding authority has no structured binding list for {target}")
        owners = [binding for binding in bindings if isinstance(binding, dict) and binding.get("target") == target]
        if len(owners) != 1 or owners[0].get("state") != "bound":
            raise RuntimeError(f"Gateway Control authority is missing or ambiguous for {target}")
        binding = owners[0]
        source = binding.get("source_subsystem")
        reference = binding.get("source")
        if not isinstance(source, str) or not isinstance(reference, str) or BindingReference.parse(reference, field_name="binding authority source").subsystem != source:
            raise RuntimeError(f"Gateway Control authority has inconsistent source metadata for {target}")
        return {"source": source, "source_reference": reference,
                "peer": binding.get("source_peer_id"), "source_session": binding.get("source_session_id"),
                "target_session": binding.get("target_session_id"), "interface": interface_name,
                "source_registration": binding.get("source_registration_revision"), "target_registration": binding.get("target_registration_revision"),
                "binding_revision": binding.get("binding_revision")}

    def validate_control_authority(self, provider_interface: str, context: Any) -> dict[str, Any]:
        authority = self.bound_control_authority(provider_interface)
        if getattr(context, "source_subsystem", None) != authority["source"]:
            raise ValueError(f"{provider_interface} source is bound to {authority['source']}; received Control request from {getattr(context, 'source_subsystem', None)}")
        authenticated = getattr(context, "source_authority", None)
        if authority["peer"] is not None:
            if not isinstance(authenticated, dict) or any(authenticated.get(field) != value for field, value in authority.items()):
                raise RuntimeError("Authenticated Control registration or binding epoch does not match the bound owner")
        return authority

    def bound_control_owner(self, provider_interface: str) -> str:
        """Return the owner only when one source requirement is bound to the port."""
        _, target = self._declared_control_target(provider_interface)
        source_bindings = self.bound_control_sessions(provider_interface)
        if not source_bindings:
            raise RuntimeError(f"No active runtime binding targets gateway Control interface {target}")
        if len(source_bindings) != 1:
            raise RuntimeError(
                f"Ambiguous runtime bindings target gateway Control interface {target}: "
                f"found {len(source_bindings)} source references"
            )
        return next(iter(source_bindings.values()))

    def bound_control_session(self, provider_interface: str, source_subsystem: str) -> str:
        """Return one source reference for ``source_subsystem`` or fail closed.

        The returned reference identifies a runtime requirement binding. It
        does not assert that the source process session or handler is live.
        """
        _, target = self._declared_control_target(provider_interface)
        if not isinstance(source_subsystem, str) or not source_subsystem.strip():
            raise RuntimeError("Gateway Control source subsystem must be a non-empty string")
        source_name = source_subsystem.strip()
        source_bindings = self.bound_control_sessions(provider_interface)
        source_references = [
            source_reference
            for source_reference, bound_subsystem in source_bindings.items()
            if bound_subsystem == source_name
        ]
        if not source_references:
            raise RuntimeError(
                f"No runtime binding from source subsystem {source_name} targets gateway Control interface {target}"
            )
        if len(source_references) != 1:
            raise RuntimeError(
                f"Ambiguous runtime bindings from source subsystem {source_name} target gateway Control "
                f"interface {target}: found {len(source_references)} source references"
            )
        return source_references[0]


class ComponentSubsystem(Subsystem):
    expected_role = "component"


class CompoundSubsystem(Subsystem):
    expected_role = "compound"


class AppSubsystem(Subsystem):
    expected_role = "app"
