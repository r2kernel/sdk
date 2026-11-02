"""Caller and provider helpers for ``msg.control.v1/ControlCycle`` numbering.

The caller side issues one explicit generation per started Control session and
strictly increasing sequences inside it. The provider side keeps the last
admitted (generation, sequence) per transport source so older or repeated
cycles never advance device state. Neither helper selects an owner: the runtime
binding and the transport source identity do that.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from r2kernel.core.common_control import ControlCycle

MAX_INT64 = (1 << 63) - 1


class ControlCycleIssuer:
    """Issue command/keep cycles for one caller-owned Control session."""

    def __init__(self, clock_ns: Callable[[], int]) -> None:
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._generation = 0
        self._sequence = 0
        self._state_revision = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def restart(self) -> int:
        """Start a new generation; earlier generations can no longer refresh state."""
        with self._lock:
            return self._restart_locked()

    def command(self) -> dict[str, Any]:
        """Return the next command cycle with a new positive state revision."""
        with self._lock:
            if self._generation == 0:
                self._restart_locked()
            self._state_revision = self._next(self._state_revision, "state_revision")
            return self._cycle_locked("command")

    def keep(self) -> dict[str, Any]:
        """Return the next keep cycle that maintains the current state revision."""
        with self._lock:
            if self._generation == 0 or self._state_revision == 0:
                raise RuntimeError("keep requires an established command state revision")
            return self._cycle_locked("keep")

    def _restart_locked(self) -> int:
        generation = max(1, int(self._clock_ns()), self._generation + 1)
        if generation > MAX_INT64:
            raise RuntimeError("Control cycle generation exceeds the positive int64 range")
        self._generation = generation
        self._sequence = 0
        self._state_revision = 0
        return generation

    def _cycle_locked(self, intent: str) -> dict[str, Any]:
        self._sequence = self._next(self._sequence, "sequence")
        return {
            "generation": self._generation,
            "sequence": self._sequence,
            "intent": intent,
            "state_revision": self._state_revision,
        }

    @staticmethod
    def _next(value: int, name: str) -> int:
        if value >= MAX_INT64:
            raise RuntimeError(f"Control cycle {name} exceeds the positive int64 range")
        return value + 1


class ControlCycleOrdering:
    """Provider-side strictly increasing cycle admission per transport source.

    Callers serialize access with their own endpoint lock. A cycle is consumed
    when admitted, before its effect runs, so a timeout or device failure is
    never replayed by repeating the same cycle.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, int]] = {}

    def admit(self, source: str, cycle: ControlCycle) -> str | None:
        """Record ``cycle`` for ``source`` or return why it cannot advance state."""
        last = self._last.get(source)
        if last is not None:
            generation, sequence = last
            if cycle.generation < generation:
                return "ControlCycle generation is stale"
            if cycle.generation == generation and cycle.sequence <= sequence:
                return "ControlCycle sequence must strictly increase within its generation"
        self._last[source] = (cycle.generation, cycle.sequence)
        return None


def parse_control_cycle(value: Any, *, label: str) -> ControlCycle:
    """Parse a request cycle with positive generation and sequence."""
    try:
        cycle = ControlCycle.from_mapping(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label} ControlCycle: {exc}") from exc
    if cycle.generation <= 0 or cycle.sequence <= 0:
        raise ValueError(f"{label} ControlCycle generation and sequence must be positive")
    return cycle


def control_source_identity(context: Any, *, label: str) -> str:
    source = getattr(context, "source_subsystem", None)
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{label} Control request is missing transport source identity")
    return source.strip()


def cycle_echo_matches(payload: Any, cycle: dict[str, Any]) -> bool:
    """Return whether a typed Control response echoes exactly the sent cycle."""
    return isinstance(payload, dict) and payload.get("cycle") == cycle
