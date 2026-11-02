"""Deterministic msg.actuator.drive.v1 payloads shared by the Python drive fixtures.

The C++ fixtures (``../cpp/drive_payload.hpp``) build the same values, so a
report from either language describes the same request and response content.
Request ``n`` channels for sequence ``seq``:

- ``channels[i] = i``, ``mode[i] = (seq + i) & 0xff``
- ``target[i] = seq + 0.25 * i``, ``feedforward[i] = 0.5 * i``

The gateway answers with ``channels``, ``position = target``,
``velocity = feedforward`` and ``enabled[i] = (seq + i) % 2 == 0``; the
optional ``effort`` and ``fault_code`` fields are omitted.
"""
from __future__ import annotations

from typing import Any

CONTRACT = "api.actuator.drive.v1"
TOPIC = "drive_command"
REQUEST_TYPE = "msg.actuator.drive.v1/DriveCommand"
RESPONSE_TYPE = "msg.actuator.drive.v1/DriveState"
# Request data bytes per channel: uint32 channel + uint8 mode + two float64 values.
REQUEST_BYTES_PER_CHANNEL = 21
# seq must stay exactly representable as float64 target[0].
MAX_SEQUENCE = 2**53


def channels_for_payload(payload_size: int) -> int:
    """Vector length whose request data bytes approximate ``payload_size``."""
    return max(int(payload_size) // REQUEST_BYTES_PER_CHANNEL, 1)


def make_command(seq: int, channels: int) -> dict[str, Any]:
    return {
        "channels": list(range(channels)),
        "mode": [(seq + index) & 0xFF for index in range(channels)],
        "target": [float(seq) + 0.25 * index for index in range(channels)],
        "feedforward": [0.5 * index for index in range(channels)],
    }


def make_state(seq: int, channels: int) -> dict[str, Any]:
    command = make_command(seq, channels)
    return {
        "channels": command["channels"],
        "position": command["target"],
        "velocity": command["feedforward"],
        "enabled": [(seq + index) % 2 == 0 for index in range(channels)],
    }


def _vector(message: dict[str, Any], field: str) -> list[Any] | None:
    value = message.get(field)
    if value is None:
        return None
    return list(value)  # vector<uint8> may decode as bytes


def command_sequence(command: dict[str, Any]) -> tuple[int, int] | None:
    """(seq, channels) of a request built by ``make_command``, or None when it differs."""
    channels = _vector(command, "channels")
    target = _vector(command, "target")
    if not channels or not target:
        return None
    first = float(target[0])
    if not first.is_integer() or not 0 <= first < MAX_SEQUENCE:
        return None
    seq = int(first)
    expected = make_command(seq, len(channels))
    if any(_vector(command, field) != expected[field] for field in expected):
        return None
    return seq, len(channels)


def state_matches(state: Any, seq: int, channels: int) -> bool:
    if not isinstance(state, dict):
        return False
    expected = make_state(seq, channels)
    if any(_vector(state, field) != expected[field] for field in expected):
        return False
    # Omitted optional vectors may decode as absent or empty.
    return not state.get("effort") and not state.get("fault_code")
