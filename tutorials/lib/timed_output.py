"""Provider-side ``api.io.digital.timed_output.v1`` maintain_state cycle rules.

A ``command`` cycle carries an IndexedBoolValue and establishes or replaces the
output state under a new positive ``state_revision``. A ``keep`` cycle carries
no output and must name the current revision of the same source and generation.
The lease itself (forcing a high output low on expiry) stays with the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from r2kernel.core.common_control import ControlCycle

UINT32_MAX = 0xFFFF_FFFF


@dataclass(frozen=True)
class TimedOutputHold:
    """Output state established by the last applied command cycle."""

    source: str
    generation: int
    state_revision: int
    channel_index: int
    value: bool


def command_check(
    cycle: ControlCycle,
    output: Any,
    hold: TimedOutputHold | None,
    source: str,
) -> tuple[str | None, int, bool]:
    """Return ``(rejection, channel_index, value)`` for a command cycle."""
    fallback_channel = hold.channel_index if hold is not None else 0
    if (
        not isinstance(output, dict)
        or set(output) != {"index", "value"}
        or isinstance(output.get("index"), bool)
        or not isinstance(output.get("index"), int)
        or not 0 <= output["index"] <= UINT32_MAX
        or not isinstance(output.get("value"), bool)
    ):
        return "command cycle requires an IndexedBoolValue output", fallback_channel, False
    channel, value = int(output["index"]), bool(output["value"])
    if cycle.state_revision <= 0:
        return "command state_revision must be positive", channel, value
    if (
        hold is not None
        and hold.source == source
        and hold.generation == cycle.generation
        and cycle.state_revision <= hold.state_revision
    ):
        return "command state_revision must increase within its generation", channel, value
    return None, channel, value


def keep_check(
    cycle: ControlCycle,
    output: Any,
    hold: TimedOutputHold | None,
    source: str,
    *,
    lease_expiry_ns: int | None,
    now_ns: int,
) -> str | None:
    """Return why a keep cycle cannot maintain the current output state."""
    if output is not None:
        return "keep cycle must not carry an output payload"
    if hold is None or hold.source != source or hold.generation != cycle.generation:
        return "keep requires an established output state in the same generation"
    if cycle.state_revision != hold.state_revision:
        return "keep must name the current output state_revision"
    if hold.value and (lease_expiry_ns is None or lease_expiry_ns <= now_ns):
        return "output lease already ended; a new command is required"
    return None


def timed_output_response(
    cycle: ControlCycle,
    status: str,
    channel_index: int,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a TimedOutputControlResponse without a hardware readback value."""
    response: dict[str, Any] = {"cycle": cycle.to_mapping(), "status": status, "channel_index": channel_index}
    if detail:
        response["detail"] = detail
    return response
