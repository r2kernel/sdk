"""Common Control cycle validation and best-effort ownership watchdog.

This module coordinates a single, runtime-bound Control owner. It is a Python
SDK helper, not a hardware safety controller. The runtime must provide the
transport-provided source identity and synchronized time from its request
context. Lease expiry uses a separate monotonic clock.
"""

from __future__ import annotations

import threading
import time
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


_INT64_MAX = (1 << 63) - 1
_CYCLE_FIELDS = {"generation", "sequence", "intent", "state_revision"}


class ControlContractError(ValueError):
    """A Control request or local policy violates the common contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ControlIntent(str, Enum):
    COMMAND = "command"
    KEEP = "keep"
    SAMPLE = "sample"


class ControlRefreshMode(str, Enum):
    COMMAND_REFRESH = "command_refresh"
    MAINTAIN_STATE = "maintain_state"
    SAMPLE_EXCHANGE = "sample_exchange"


@dataclass(frozen=True)
class ControlCycle:
    """Versioned cycle metadata embedded in a new Control request.

    Native message codecs represent these values as signed int64, constrained
    here to the non-negative range 0 through 2^63-1.
    """

    generation: int
    sequence: int
    intent: ControlIntent | str
    state_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _require_nonnegative_int64(self.generation, "generation"))
        object.__setattr__(self, "sequence", _require_nonnegative_int64(self.sequence, "sequence"))
        object.__setattr__(self, "state_revision", _require_nonnegative_int64(self.state_revision, "state_revision"))
        try:
            resolved_intent = ControlIntent(self.intent)
        except (ValueError, TypeError) as exc:
            raise ControlContractError(
                "invalid_intent", "ControlCycle.intent must be command, keep, or sample"
            ) from exc
        object.__setattr__(self, "intent", resolved_intent)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControlCycle":
        if not isinstance(value, Mapping):
            raise ControlContractError("invalid_cycle", "ControlCycle must be a mapping")
        keys = set(value)
        missing = _CYCLE_FIELDS - keys
        unknown = keys - _CYCLE_FIELDS
        if missing or unknown:
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown {', '.join(sorted(map(str, unknown)))}")
            raise ControlContractError("invalid_cycle", "ControlCycle has " + " and ".join(details))
        return cls(
            generation=value["generation"],
            sequence=value["sequence"],
            intent=value["intent"],
            state_revision=value["state_revision"],
        )

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "generation": self.generation,
            "sequence": self.sequence,
            "intent": self.intent.value,
            "state_revision": self.state_revision,
        }


@dataclass(frozen=True)
class ControlPolicy:
    """Concrete limits selected by the binding and checked against provider support."""

    refresh_mode: ControlRefreshMode | str
    max_update_gap_ns: int
    max_command_age_ns: int
    max_request_bytes: int
    max_response_bytes: int
    max_pending_units: int | None = None

    def __post_init__(self) -> None:
        try:
            resolved_mode = ControlRefreshMode(self.refresh_mode)
        except (ValueError, TypeError) as exc:
            raise ControlContractError(
                "invalid_policy", "refresh_mode must be command_refresh, maintain_state, or sample_exchange"
            ) from exc
        object.__setattr__(self, "refresh_mode", resolved_mode)
        for field_name in (
            "max_update_gap_ns",
            "max_command_age_ns",
            "max_request_bytes",
            "max_response_bytes",
        ):
            value = _require_nonnegative_int64(getattr(self, field_name), field_name)
            if value == 0:
                raise ControlContractError("invalid_policy", f"{field_name} must be positive")
            object.__setattr__(self, field_name, value)
        if self.max_pending_units is not None:
            value = _require_nonnegative_int64(self.max_pending_units, "max_pending_units")
            if value == 0:
                raise ControlContractError("invalid_policy", "max_pending_units must be positive")
            object.__setattr__(self, "max_pending_units", value)

    @classmethod
    def from_common_control(
        cls,
        common_metadata: Mapping[str, Any],
        *,
        selected_limits: Mapping[str, Any],
        provider_limits: Mapping[str, Any],
    ) -> "ControlPolicy":
        """Resolve an API declaration against concrete binding/provider limits.

        The API declaration contains policy names and required field names,
        never example numeric values. Every required concrete value must be
        selected by the binding and bounded by the provider.
        """
        if not isinstance(common_metadata, Mapping):
            raise ControlContractError("invalid_metadata", "common_control metadata must be a mapping")
        allowed_metadata = {
            "version",
            "request_effect",
            "response_scope",
            "period_policy",
            "execution_policy",
            "refresh_mode",
            "ownership_policy",
            "ordering",
            "repeat_policy",
            "failure",
            "limits",
        }
        if set(common_metadata) != allowed_metadata:
            raise ControlContractError("invalid_metadata", "common_control metadata has missing or unknown fields")
        if (
            isinstance(common_metadata.get("version"), bool)
            or not isinstance(common_metadata.get("version"), int)
            or common_metadata.get("version") != 1
        ):
            raise ControlContractError("invalid_metadata", "common_control.version must be the integer 1")
        request_effect = common_metadata.get("request_effect")
        if not isinstance(request_effect, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", request_effect):
            raise ControlContractError("invalid_metadata", "request_effect must be a stable snake_case identifier")
        fixed_values = {
            "period_policy": "binding_required",
            "execution_policy": "finite_expiry_commit_check",
            "ownership_policy": "explicit_binding_generation",
            "ordering": "strictly_increasing_cycle",
            "repeat_policy": "no_automatic_replay",
            "failure": "report_domain_status",
        }
        if any(common_metadata.get(key) != value for key, value in fixed_values.items()):
            raise ControlContractError("invalid_metadata", "common_control contains an unsupported fixed policy")
        response_scope = common_metadata.get("response_scope")
        if not isinstance(response_scope, str) or response_scope not in {
            "endpoint_effect_completed",
            "sample_observed",
        }:
            raise ControlContractError("invalid_metadata", "response_scope is unsupported")
        if response_scope == "sample_observed" and common_metadata.get("refresh_mode") != "sample_exchange":
            raise ControlContractError("invalid_metadata", "sample_observed requires sample_exchange")

        limits = common_metadata.get("limits")
        if not isinstance(limits, Mapping) or set(limits) != {"source", "required_fields", "provider_bounds"}:
            raise ControlContractError("invalid_metadata", "common_control.limits has an invalid shape")
        if limits.get("source") != "binding" or limits.get("provider_bounds") != "constrain_selected_values":
            raise ControlContractError("invalid_metadata", "common_control.limits policy is unsupported")
        required_fields = limits.get("required_fields")
        if not isinstance(required_fields, list) or any(not isinstance(item, str) for item in required_fields):
            raise ControlContractError("invalid_metadata", "limits.required_fields must be a list of names")
        required_set = set(required_fields)
        base_fields = {
            "max_update_gap_ns",
            "max_command_age_ns",
            "max_request_bytes",
            "max_response_bytes",
        }
        permitted_fields = base_fields | {"max_pending_units"}
        if len(required_set) != len(required_fields) or not base_fields <= required_set or not required_set <= permitted_fields:
            raise ControlContractError("invalid_metadata", "limits.required_fields has duplicates or unsupported names")
        if not isinstance(selected_limits, Mapping) or set(selected_limits) != required_set:
            raise ControlContractError("missing_binding_limits", "binding limits must exactly match required_fields")
        if not isinstance(provider_limits, Mapping) or not required_set <= set(provider_limits):
            raise ControlContractError("missing_provider_limits", "provider limits must bound every required field")

        resolved_limits: dict[str, int] = {}
        for field_name in required_fields:
            selected = _require_nonnegative_int64(selected_limits[field_name], field_name)
            provider_maximum = _require_nonnegative_int64(provider_limits[field_name], f"provider.{field_name}")
            if selected == 0 or provider_maximum == 0:
                raise ControlContractError("invalid_policy", f"{field_name} and its provider bound must be positive")
            if selected > provider_maximum:
                raise ControlContractError(
                    "provider_limit_exceeded", f"binding {field_name} exceeds the provider bound"
                )
            resolved_limits[field_name] = selected
        return cls(
            refresh_mode=common_metadata.get("refresh_mode"),
            max_update_gap_ns=resolved_limits["max_update_gap_ns"],
            max_command_age_ns=resolved_limits["max_command_age_ns"],
            max_request_bytes=resolved_limits["max_request_bytes"],
            max_response_bytes=resolved_limits["max_response_bytes"],
            max_pending_units=resolved_limits.get("max_pending_units"),
        )


@dataclass(frozen=True)
class ControlPermit:
    """A validated request permit. It cannot activate or change ownership."""

    cycle: ControlCycle
    source_identity: str
    generated_at_sync_ns: int
    valid_until_sync_ns: int
    accepted_at_monotonic_ns: int
    _guard_identity: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class PendingEffectHandle:
    """Handle for an effect queued by the application for later dispatch."""

    token: int
    permit: ControlPermit


@dataclass
class _PendingEffect:
    handle: PendingEffectHandle
    cancel: Callable[[str], Any]
    expiry_monotonic_ns: int


class ControlGuard:
    """Validate cycles for one trusted binding and recheck them at dispatch.

    Call :meth:`activate` only from the provider's trusted binding lifecycle.
    The owner is fixed at construction from that binding. A packet can neither
    select an owner nor activate a generation. The optional background thread
    is a best-effort SDK watchdog; it provides no hard real-time or hardware
    safety guarantee.
    """

    def __init__(
        self,
        *,
        bound_owner_id: str,
        policy: ControlPolicy,
        sync_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
        on_held_command_invalidated: Callable[[int, str], Any] | None = None,
    ) -> None:
        owner_id = str(bound_owner_id).strip()
        if not owner_id:
            raise ControlContractError("invalid_owner", "bound_owner_id must be non-empty")
        if not isinstance(policy, ControlPolicy):
            raise TypeError("policy must be a ControlPolicy")
        if not callable(sync_clock_ns) or not callable(monotonic_clock_ns):
            raise TypeError("clock providers must be callable")
        if on_held_command_invalidated is not None and not callable(on_held_command_invalidated):
            raise TypeError("on_held_command_invalidated must be callable")

        self._bound_owner_id = owner_id
        self._policy = policy
        self._sync_clock_ns = sync_clock_ns
        self._monotonic_clock_ns = monotonic_clock_ns
        self._on_held_command_invalidated = on_held_command_invalidated
        self._guard_identity = object()
        self._condition = threading.Condition(threading.RLock())
        self._active_generation: int | None = None
        self._last_generation = 0
        self._last_sequence: int | None = None
        self._last_dispatched_sequence: int | None = None
        self._last_registered_sequence: int | None = None
        self._last_command_revision = 0
        self._lease_deadline_monotonic_ns: int | None = None
        self._held_state_revision: int | None = None
        self._held_command_generated_at_sync_ns: int | None = None
        self._held_command_expires_monotonic_ns: int | None = None
        self._reserved_command: tuple[int, int] | None = None
        self._reserved_command_permit: ControlPermit | None = None
        self._next_pending_token = 1
        self._pending: dict[int, _PendingEffect] = {}
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stopping = False
        self._closed = False
        self._last_cancellation_errors: tuple[str, ...] = ()

    @property
    def bound_owner_id(self) -> str:
        return self._bound_owner_id

    @property
    def policy(self) -> ControlPolicy:
        return self._policy

    @property
    def last_cancellation_errors(self) -> tuple[str, ...]:
        with self._condition:
            return self._last_cancellation_errors

    def activate(self, *, generation: int) -> None:
        """Activate a fresh generation from a trusted provider lifecycle event."""
        generation = _require_nonnegative_int64(generation, "generation")
        if generation == 0:
            raise ControlContractError("invalid_generation", "generation must be positive")
        self.watchdog_tick()
        with self._condition:
            if self._closed:
                raise ControlContractError("guard_closed", "a closed ControlGuard cannot be activated")
            if self._active_generation is not None:
                raise ControlContractError("already_active", "revoke the active generation before activating another")
            if generation <= self._last_generation:
                raise ControlContractError(
                    "stale_generation", "provider activation generation must increase monotonically"
                )
            now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
            self._active_generation = generation
            self._last_generation = generation
            self._last_sequence = None
            self._last_dispatched_sequence = None
            self._last_registered_sequence = None
            self._last_command_revision = 0
            self._lease_deadline_monotonic_ns = now_monotonic_ns + self._policy.max_update_gap_ns
            self._held_state_revision = None
            self._held_command_generated_at_sync_ns = None
            self._held_command_expires_monotonic_ns = None
            self._reserved_command = None
            self._reserved_command_permit = None
            self._condition.notify_all()

    def revoke(self, *, generation: int, reason: str = "provider_revoked") -> tuple[str, ...]:
        """Revoke the active generation and best-effort cancel pending work."""
        generation = _require_nonnegative_int64(generation, "generation")
        if not str(reason).strip():
            raise ControlContractError("invalid_reason", "revoke reason must be non-empty")
        with self._condition:
            if self._active_generation != generation:
                raise ControlContractError("stale_generation", "revoke generation is not the active generation")
            callbacks = self._invalidate_locked(str(reason))
            self._condition.notify_all()
        return self._run_cancellations(callbacks, str(reason))

    def accept(
        self,
        cycle: ControlCycle | Mapping[str, Any],
        *,
        source_identity: str | None,
        generated_at_sync_ns: int | None,
        valid_until_sync_ns: int | None,
        request_size_bytes: int,
        execute_at_sync_ns: int | None = None,
    ) -> ControlPermit:
        """Accept a well-formed current cycle and renew only its control lease.

        ``source_identity`` must come from the transport/runtime request context,
        never from the ControlCycle payload. ``valid_until_sync_ns`` is required
        for every new-profile request. Future scheduled execution is rejected;
        this helper only commits immediate effects.
        """
        self.watchdog_tick()
        resolved_cycle = cycle if isinstance(cycle, ControlCycle) else ControlCycle.from_mapping(cycle)
        source = str(source_identity or "").strip()
        request_size = _require_nonnegative_int64(request_size_bytes, "request_size_bytes")
        if request_size > self._policy.max_request_bytes:
            raise ControlContractError("request_too_large", "Control request exceeds the selected byte limit")
        generated_at = _require_timestamp(generated_at_sync_ns, "generated_at_sync_ns")
        valid_until = _require_timestamp(valid_until_sync_ns, "valid_until_sync_ns")
        execute_at = None if execute_at_sync_ns is None else _require_timestamp(execute_at_sync_ns, "execute_at_sync_ns")

        expiry_callbacks: list[Callable[[str], Any]] = []
        superseded_callbacks: list[Callable[[str], Any]] = []
        permit: ControlPermit | None = None
        admission_error: ControlContractError | None = None
        expiry_reason: str | None = None
        with self._condition:
            expiry = self._expire_due_locked()
            if expiry is not None:
                expiry_callbacks.extend(expiry[0])
                expiry_reason = "lease_expired" if expiry[1] else "command_expired"
            else:
                try:
                    if self._closed:
                        raise ControlContractError("guard_closed", "ControlGuard is closed")
                    if source != self._bound_owner_id:
                        raise ControlContractError("wrong_owner", "Control request source does not match the bound owner")
                    if self._active_generation is None:
                        raise ControlContractError("inactive_generation", "Control generation is not active")
                    if resolved_cycle.generation != self._active_generation:
                        raise ControlContractError("stale_generation", "ControlCycle generation is not active")
                    now_sync_ns = _clock_value(self._sync_clock_ns, "synchronized")
                    now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
                    if valid_until <= now_sync_ns:
                        raise ControlContractError("expired_request", "valid_until_sync_ns must be in the future")
                    if generated_at > now_sync_ns:
                        raise ControlContractError("future_generated_at", "generated_at_sync_ns cannot be in the future")
                    if generated_at >= valid_until:
                        raise ControlContractError("invalid_expiry", "valid_until_sync_ns must be later than generated_at_sync_ns")
                    if execute_at is not None:
                        if execute_at > now_sync_ns:
                            raise ControlContractError(
                                "future_execution_unsupported", "ControlGuard accepts immediate execution only"
                            )
                        if execute_at > valid_until:
                            raise ControlContractError("invalid_expiry", "execution time cannot be later than valid_until_sync_ns")
                    if self._last_sequence is not None and resolved_cycle.sequence <= self._last_sequence:
                        raise ControlContractError("out_of_order_cycle", "ControlCycle.sequence must strictly increase")
                    if resolved_cycle.sequence == 0:
                        raise ControlContractError("invalid_sequence", "ControlCycle.sequence must be positive")
                    self._validate_intent_locked(resolved_cycle, generated_at, now_sync_ns)

                    self._last_sequence = resolved_cycle.sequence
                    self._lease_deadline_monotonic_ns = now_monotonic_ns + self._policy.max_update_gap_ns
                    permit = ControlPermit(
                        cycle=resolved_cycle,
                        source_identity=source,
                        generated_at_sync_ns=generated_at,
                        valid_until_sync_ns=valid_until,
                        accepted_at_monotonic_ns=now_monotonic_ns,
                        _guard_identity=self._guard_identity,
                    )
                    if resolved_cycle.intent is ControlIntent.COMMAND:
                        self._last_command_revision = resolved_cycle.state_revision
                        superseded_callbacks.extend(
                            self._take_superseded_command_pending_locked(resolved_cycle.sequence)
                        )
                        self._reserved_command = (resolved_cycle.sequence, resolved_cycle.state_revision)
                        self._reserved_command_permit = permit
                    self._condition.notify_all()
                except ControlContractError as exc:
                    admission_error = exc

        if expiry_reason is not None:
            self._run_cancellations(expiry_callbacks, expiry_reason or "command_expired")
            if expiry_reason == "lease_expired":
                raise ControlContractError(
                    "lease_expired", "Control owner lease expired; explicit reactivation is required"
                )
            raise ControlContractError(
                "command_expired", "held command expired; the next cycle must retry against current state"
            )
        if admission_error is not None:
            raise admission_error
        if superseded_callbacks:
            self._run_cancellations(superseded_callbacks, "command_superseded")
        assert permit is not None
        return permit

    def validate_response_size(self, response_size_bytes: int) -> None:
        """Enforce the selected response byte limit without changing lease state."""
        response_size = _require_nonnegative_int64(response_size_bytes, "response_size_bytes")
        if response_size > self._policy.max_response_bytes:
            raise ControlContractError("response_too_large", "Control response exceeds the selected byte limit")

    def dispatch_if_valid(self, permit: ControlPermit, effect: Callable[[], Any]) -> Any:
        """Recheck owner, generation, lease and expiry immediately before effect submission.

        The callback is the commit boundary and must be a short synchronous
        submit operation. Do not wait on a device, perform bulk encoding, or
        block in this callback. Holding the guard lock across it serializes a
        concurrent revoke with that submission; Python cannot cancel an effect
        that has already crossed the callback boundary.
        """
        if not callable(effect):
            raise TypeError("effect must be callable")
        cancellation_callbacks: list[Callable[[str], Any]] = []
        dispatch_error: BaseException | None = None
        contract_error: ControlContractError | None = None
        with self._condition:
            try:
                self._ensure_sequence_not_dispatched_locked(permit)
                self._ensure_permit_current_locked(permit)
            except ControlContractError as exc:
                contract_error = exc
            else:
                try:
                    result = effect()
                except BaseException as exc:
                    dispatch_error = exc
                    cancellation_callbacks = self._invalidate_locked("effect_dispatch_error")
                    self._condition.notify_all()
                else:
                    self._record_dispatch_locked(permit)
                    return result
        if contract_error is not None:
            self.watchdog_tick()
            raise contract_error
        self._run_cancellations(cancellation_callbacks, "effect_dispatch_error")
        assert dispatch_error is not None
        raise dispatch_error

    def register_pending(
        self,
        permit: ControlPermit,
        *,
        cancel: Callable[[str], Any],
    ) -> PendingEffectHandle:
        """Register a bounded queued effect so lease loss can cancel it."""
        if not callable(cancel):
            raise TypeError("cancel must be callable")
        permit_error: ControlContractError | None = None
        handle: PendingEffectHandle | None = None
        with self._condition:
            try:
                self._ensure_sequence_not_dispatched_locked(permit)
                self._ensure_permit_current_locked(permit)
                if (
                    self._last_registered_sequence is not None
                    and permit.cycle.sequence <= self._last_registered_sequence
                ):
                    raise ControlContractError(
                        "replayed_cycle", "a Control cycle can be registered for deferred dispatch only once"
                    )
            except ControlContractError as exc:
                permit_error = exc
            else:
                if self._policy.max_pending_units is None:
                    raise ControlContractError(
                        "pending_limit_unconfigured",
                        "ControlGuard requires a selected max_pending_units limit for deferred effects",
                    )
                if len(self._pending) >= self._policy.max_pending_units:
                    raise ControlContractError("pending_limit", "Control pending effect limit is full")
                token = self._next_pending_token
                self._next_pending_token += 1
                handle = PendingEffectHandle(token=token, permit=permit)
                now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
                now_sync_ns = _clock_value(self._sync_clock_ns, "synchronized")
                expiry_remaining_ns = permit.valid_until_sync_ns - now_sync_ns
                if permit.cycle.intent is ControlIntent.COMMAND:
                    command_remaining_ns = (
                        self._policy.max_command_age_ns
                        - (now_sync_ns - permit.generated_at_sync_ns)
                        + 1
                    )
                    expiry_remaining_ns = min(expiry_remaining_ns, command_remaining_ns)
                expiry_monotonic_ns = now_monotonic_ns + max(0, expiry_remaining_ns)
                self._pending[token] = _PendingEffect(
                    handle=handle,
                    cancel=cancel,
                    expiry_monotonic_ns=expiry_monotonic_ns,
                )
                self._last_registered_sequence = permit.cycle.sequence
                self._condition.notify_all()
        if permit_error is not None:
            self.watchdog_tick()
            raise permit_error
        assert handle is not None
        return handle

    def dispatch_pending(self, handle: PendingEffectHandle, effect: Callable[[], Any]) -> Any:
        """Commit one queued effect after a fresh generation/expiry check."""
        if not callable(effect):
            raise TypeError("effect must be callable")
        cancellation: Callable[[str], Any] | None = None
        cancellation_reason = "pending_effect_rejected"
        dispatch_error: BaseException | None = None
        permit_error: ControlContractError | None = None
        with self._condition:
            if (
                not isinstance(handle, PendingEffectHandle)
                or handle.permit._guard_identity is not self._guard_identity
            ):
                raise ControlContractError("unknown_pending_effect", "pending effect handle is not active")
            pending = self._pending.get(handle.token)
            if pending is None or pending.handle.permit is not handle.permit:
                raise ControlContractError("unknown_pending_effect", "pending effect handle is not active")
            try:
                self._ensure_sequence_not_dispatched_locked(handle.permit)
                self._ensure_permit_current_locked(handle.permit)
            except ControlContractError as exc:
                permit_error = exc
                if exc.code in {"expired_request", "lease_expired"}:
                    cancellation_reason = "request_expired" if exc.code == "expired_request" else "lease_expired"
                self._pending.pop(handle.token, None)
                cancellation = pending.cancel
            else:
                self._pending.pop(handle.token, None)
                try:
                    result = effect()
                except BaseException as exc:
                    dispatch_error = exc
                    cancellation_reason = "effect_dispatch_error"
                    callbacks = self._invalidate_locked(cancellation_reason)
                    self._condition.notify_all()
                else:
                    self._record_dispatch_locked(handle.permit)
                    return result
        if cancellation is not None:
            self._run_cancellations([cancellation], cancellation_reason)
            self.watchdog_tick()
            assert permit_error is not None
            raise permit_error
        if dispatch_error is not None:
            self._run_cancellations(callbacks, cancellation_reason)
            raise dispatch_error
        raise ControlContractError("unknown_pending_effect", "pending effect could not be dispatched")

    def cancel_pending(self, handle: PendingEffectHandle, *, reason: str = "application_cancelled") -> bool:
        """Cancel a queued effect without retrying or replaying it."""
        if (
            not isinstance(handle, PendingEffectHandle)
            or handle.permit._guard_identity is not self._guard_identity
        ):
            return False
        with self._condition:
            pending = self._pending.get(handle.token)
            if pending is None or pending.handle.permit is not handle.permit:
                return False
            self._pending.pop(handle.token)
        self._run_cancellations([pending.cancel], str(reason))
        return True

    def watchdog_tick(self) -> bool:
        """Run one deterministic watchdog check; return whether it expired."""
        with self._condition:
            expiry = self._expire_due_locked()
            if expiry is None:
                return False
            self._condition.notify_all()
        callbacks, lease_expired = expiry
        self._run_cancellations(callbacks, "lease_expired" if lease_expired else "command_expired")
        return True

    def start_watchdog(self) -> None:
        """Start an optional best-effort daemon watchdog thread."""
        with self._condition:
            if self._closed:
                raise ControlContractError("guard_closed", "a closed ControlGuard cannot start a watchdog")
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_stopping = False
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name=f"r2kernel-control-watchdog-{self._bound_owner_id}",
                daemon=True,
            )
            self._watchdog_thread.start()

    def close(self) -> tuple[str, ...]:
        """Stop the watchdog and revoke the active generation."""
        with self._condition:
            self._closed = True
            self._watchdog_stopping = True
            callbacks = self._invalidate_locked("guard_closed") if self._active_generation is not None else []
            watchdog_thread = self._watchdog_thread
            self._condition.notify_all()
        errors = self._run_cancellations(callbacks, "guard_closed")
        if watchdog_thread is not None and watchdog_thread is not threading.current_thread():
            watchdog_thread.join(timeout=1.0)
        return errors

    def __enter__(self) -> "ControlGuard":
        self.start_watchdog()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def snapshot(self) -> dict[str, Any]:
        """Return a bounded diagnostic snapshot without exposing callback data."""
        with self._condition:
            return {
                "owner_id": self._bound_owner_id,
                "active_generation": self._active_generation,
                "last_sequence": self._last_sequence,
                "last_dispatched_sequence": self._last_dispatched_sequence,
                "last_registered_sequence": self._last_registered_sequence,
                "lease_deadline_monotonic_ns": self._lease_deadline_monotonic_ns,
                "held_state_revision": self._held_state_revision,
                "pending_effects": len(self._pending),
            }

    def _validate_intent_locked(
        self,
        cycle: ControlCycle,
        generated_at_sync_ns: int,
        now_sync_ns: int,
    ) -> None:
        mode = self._policy.refresh_mode
        if mode is ControlRefreshMode.COMMAND_REFRESH:
            if cycle.intent is not ControlIntent.COMMAND:
                raise ControlContractError(
                    "intent_mode_mismatch", "command_refresh accepts command intent only"
                )
        elif mode is ControlRefreshMode.MAINTAIN_STATE:
            if cycle.intent not in {ControlIntent.COMMAND, ControlIntent.KEEP}:
                raise ControlContractError(
                    "intent_mode_mismatch", "maintain_state accepts command or keep intent"
                )
        elif cycle.intent is not ControlIntent.SAMPLE:
            raise ControlContractError("intent_mode_mismatch", "sample_exchange accepts sample intent only")

        if cycle.intent is ControlIntent.COMMAND:
            if cycle.state_revision == 0:
                raise ControlContractError("invalid_state_revision", "command state_revision must be positive")
            if now_sync_ns - generated_at_sync_ns > self._policy.max_command_age_ns:
                raise ControlContractError("stale_command", "command exceeds max_command_age_ns")
            previous_revision = max(
                self._last_command_revision,
                self._held_state_revision or 0,
                self._reserved_command[1] if self._reserved_command is not None else 0,
            )
            if cycle.state_revision <= previous_revision:
                raise ControlContractError(
                    "stale_state_revision", "command state_revision must increase monotonically"
                )
        elif cycle.intent is ControlIntent.KEEP:
            if cycle.state_revision == 0 or self._held_state_revision is None:
                raise ControlContractError("missing_held_state", "keep requires an established held command revision")
            if self._reserved_command is not None:
                raise ControlContractError("command_pending", "keep cannot renew while a replacement command is pending")
            if cycle.state_revision != self._held_state_revision:
                raise ControlContractError("state_revision_mismatch", "keep must name the current held command revision")
            held_generated_at = self._held_command_generated_at_sync_ns
            if held_generated_at is None or now_sync_ns - held_generated_at > self._policy.max_command_age_ns:
                raise ControlContractError("stale_held_command", "keep cannot renew an expired held command")
        elif cycle.state_revision != 0:
            raise ControlContractError("invalid_state_revision", "sample state_revision must be zero")

    def _ensure_permit_current_locked(self, permit: ControlPermit) -> None:
        if not isinstance(permit, ControlPermit) or permit._guard_identity is not self._guard_identity:
            raise ControlContractError("foreign_permit", "ControlPermit was not issued by this ControlGuard")
        now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
        if (
            self._active_generation is None
            or self._lease_deadline_monotonic_ns is None
            or now_monotonic_ns >= self._lease_deadline_monotonic_ns
        ):
            raise ControlContractError("lease_expired", "Control owner lease expired")
        if self._active_generation != permit.cycle.generation:
            raise ControlContractError("stale_generation", "ControlPermit generation is no longer active")
        if permit.source_identity != self._bound_owner_id:
            raise ControlContractError("wrong_owner", "ControlPermit source is not the bound owner")
        now_sync_ns = _clock_value(self._sync_clock_ns, "synchronized")
        if permit.valid_until_sync_ns <= now_sync_ns:
            raise ControlContractError("expired_request", "Control request expired before effect dispatch")
        if permit.cycle.intent is ControlIntent.COMMAND:
            if self._reserved_command != (permit.cycle.sequence, permit.cycle.state_revision):
                raise ControlContractError("superseded_command", "a newer command superseded this pending command")
            if now_sync_ns - permit.generated_at_sync_ns > self._policy.max_command_age_ns:
                raise ControlContractError("stale_command", "command exceeded max_command_age_ns before dispatch")
        elif permit.cycle.intent is ControlIntent.KEEP:
            if (
                self._held_state_revision != permit.cycle.state_revision
                or self._held_command_generated_at_sync_ns is None
                or now_sync_ns - self._held_command_generated_at_sync_ns > self._policy.max_command_age_ns
            ):
                raise ControlContractError("stale_held_command", "held command is no longer valid at dispatch")

    def _expire_due_locked(
        self,
        now_monotonic_ns: int | None = None,
    ) -> tuple[list[Callable[[str], Any]], bool] | None:
        if self._active_generation is None or self._lease_deadline_monotonic_ns is None:
            return None
        now = _clock_value(self._monotonic_clock_ns, "monotonic") if now_monotonic_ns is None else now_monotonic_ns
        callbacks: list[Callable[[str], Any]] = []
        held_revision = self._held_state_revision
        held_age_expired = False
        now_sync: int | None = None
        if held_revision is not None:
            generated_at = self._held_command_generated_at_sync_ns
            held_deadline = self._held_command_expires_monotonic_ns
            if generated_at is not None:
                now_sync = _clock_value(self._sync_clock_ns, "synchronized")
                held_age_expired = now_sync - generated_at > self._policy.max_command_age_ns
            if held_deadline is not None and now >= held_deadline:
                held_age_expired = True
        if held_age_expired and held_revision is not None:
            callbacks.extend(self._clear_held_command_locked(held_revision, "command_expired"))
        if self._pending:
            if now_sync is None:
                now_sync = _clock_value(self._sync_clock_ns, "synchronized")
            for token, pending in tuple(self._pending.items()):
                permit = pending.handle.permit
                expired = now_sync >= permit.valid_until_sync_ns or now >= pending.expiry_monotonic_ns
                if (
                    permit.cycle.intent is ControlIntent.COMMAND
                    and now_sync - permit.generated_at_sync_ns > self._policy.max_command_age_ns
                ):
                    expired = True
                if expired:
                    self._pending.pop(token, None)
                    callbacks.append(
                        lambda reason, cancel=pending.cancel: cancel("request_expired")
                    )
        reserved_permit = self._reserved_command_permit
        if reserved_permit is not None:
            now_sync = _clock_value(self._sync_clock_ns, "synchronized")
            reservation_expired = (
                now_sync - reserved_permit.generated_at_sync_ns > self._policy.max_command_age_ns
                or now_sync >= reserved_permit.valid_until_sync_ns
            )
            if reservation_expired:
                callbacks.extend(self._take_pending_command_callbacks_locked(reserved_permit.cycle.sequence))
                self._reserved_command = None
                self._reserved_command_permit = None
        lease_expired = now >= self._lease_deadline_monotonic_ns
        if lease_expired:
            callbacks.extend(self._invalidate_locked("lease_expired"))
            return callbacks, True
        if callbacks:
            return callbacks, False
        return None

    def _invalidate_locked(self, reason: str) -> list[Callable[[str], Any]]:
        callbacks = [pending.cancel for pending in self._pending.values()]
        self._pending.clear()
        if self._held_state_revision is not None:
            callbacks.extend(
                self._held_state_invalidation_callbacks(self._held_state_revision, reason)
            )
        self._active_generation = None
        self._last_sequence = None
        self._last_dispatched_sequence = None
        self._last_registered_sequence = None
        self._last_command_revision = 0
        self._lease_deadline_monotonic_ns = None
        self._held_state_revision = None
        self._held_command_generated_at_sync_ns = None
        self._held_command_expires_monotonic_ns = None
        self._reserved_command = None
        self._reserved_command_permit = None
        self._condition.notify_all()
        return callbacks

    def _clear_held_command_locked(
        self,
        state_revision: int,
        reason: str,
    ) -> list[Callable[[str], Any]]:
        callbacks: list[Callable[[str], Any]] = []
        for token, pending in tuple(self._pending.items()):
            cycle = pending.handle.permit.cycle
            if cycle.intent is ControlIntent.KEEP and cycle.state_revision == state_revision:
                self._pending.pop(token, None)
                callbacks.append(pending.cancel)
        callbacks.extend(self._held_state_invalidation_callbacks(state_revision, reason))
        self._held_state_revision = None
        self._held_command_generated_at_sync_ns = None
        self._held_command_expires_monotonic_ns = None
        return callbacks

    def _held_state_invalidation_callbacks(
        self,
        state_revision: int,
        reason: str,
    ) -> list[Callable[[str], Any]]:
        if self._on_held_command_invalidated is None:
            return []
        callback = self._on_held_command_invalidated
        return [lambda _ignored_reason: callback(state_revision, reason)]

    def _ensure_sequence_not_dispatched_locked(self, permit: ControlPermit) -> None:
        if (
            self._last_dispatched_sequence is not None
            and permit.cycle.sequence <= self._last_dispatched_sequence
        ):
            raise ControlContractError("replayed_cycle", "a Control cycle can be committed only once")

    def _record_dispatch_locked(self, permit: ControlPermit) -> None:
        self._last_dispatched_sequence = permit.cycle.sequence
        if permit.cycle.intent is ControlIntent.COMMAND:
            now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
            now_sync_ns = _clock_value(self._sync_clock_ns, "synchronized")
            age_ns = max(0, now_sync_ns - permit.generated_at_sync_ns)
            remaining_ns = max(0, self._policy.max_command_age_ns - age_ns)
            self._held_state_revision = permit.cycle.state_revision
            self._held_command_generated_at_sync_ns = permit.generated_at_sync_ns
            self._held_command_expires_monotonic_ns = now_monotonic_ns + remaining_ns + 1
            if self._reserved_command == (permit.cycle.sequence, permit.cycle.state_revision):
                self._reserved_command = None
                self._reserved_command_permit = None
            self._condition.notify_all()

    def _take_superseded_command_pending_locked(self, new_sequence: int) -> list[Callable[[str], Any]]:
        callbacks: list[Callable[[str], Any]] = []
        for token, pending in tuple(self._pending.items()):
            cycle = pending.handle.permit.cycle
            if cycle.intent is ControlIntent.COMMAND and cycle.sequence < new_sequence:
                self._pending.pop(token, None)
                callbacks.append(pending.cancel)
        return callbacks

    def _take_pending_command_callbacks_locked(self, sequence: int) -> list[Callable[[str], Any]]:
        callbacks: list[Callable[[str], Any]] = []
        for token, pending in tuple(self._pending.items()):
            cycle = pending.handle.permit.cycle
            if cycle.intent is ControlIntent.COMMAND and cycle.sequence == sequence:
                self._pending.pop(token, None)
                callbacks.append(pending.cancel)
        return callbacks

    def _run_cancellations(
        self,
        callbacks: list[Callable[[str], Any]],
        reason: str,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        for callback in callbacks:
            try:
                callback(reason)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        with self._condition:
            self._last_cancellation_errors = tuple(errors)
        return tuple(errors)

    def _watchdog_loop(self) -> None:
        while True:
            with self._condition:
                if self._watchdog_stopping or self._closed:
                    return
                if self._active_generation is None or self._lease_deadline_monotonic_ns is None:
                    self._condition.wait()
                    continue
                now_monotonic_ns = _clock_value(self._monotonic_clock_ns, "monotonic")
                deadlines = [self._lease_deadline_monotonic_ns]
                if self._held_command_expires_monotonic_ns is not None:
                    deadlines.append(self._held_command_expires_monotonic_ns)
                deadlines.extend(pending.expiry_monotonic_ns for pending in self._pending.values())
                if self._reserved_command_permit is not None:
                    reserved = self._reserved_command_permit
                    reserved_remaining_ns = min(
                        reserved.valid_until_sync_ns - _clock_value(self._sync_clock_ns, "synchronized"),
                        self._policy.max_command_age_ns
                        - (_clock_value(self._sync_clock_ns, "synchronized") - reserved.generated_at_sync_ns)
                        + 1,
                    )
                    deadlines.append(now_monotonic_ns + max(0, reserved_remaining_ns))
                remaining_ns = min(deadlines) - now_monotonic_ns
                if remaining_ns > 0:
                    self._condition.wait(remaining_ns / 1_000_000_000)
                    continue
                expiry = self._expire_due_locked()
            if expiry is not None:
                callbacks, lease_expired = expiry
                self._run_cancellations(
                    callbacks,
                    "lease_expired" if lease_expired else "command_expired",
                )


def _require_nonnegative_int64(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _INT64_MAX:
        raise ControlContractError("invalid_int64", f"{name} must be a non-negative signed 64-bit integer")
    return value


def _require_timestamp(value: Any, name: str) -> int:
    timestamp = _require_nonnegative_int64(value, name)
    if timestamp == 0:
        raise ControlContractError("invalid_timestamp", f"{name} must be a finite positive synchronized timestamp")
    return timestamp


def _clock_value(clock: Callable[[], int], name: str) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlContractError("invalid_clock", f"{name} clock must return a non-negative integer nanosecond value")
    return value
