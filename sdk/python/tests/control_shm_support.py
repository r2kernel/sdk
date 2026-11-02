"""Shared helpers for the local Control SHM v5 tests: test policy and hand-built v5 frames."""

from __future__ import annotations

import struct

from r2kernel.core._control_shm_region import ControlShmPolicy


def _test_policy(**overrides: int) -> ControlShmPolicy:
    values = {
        "minimum_region_bytes": 4096,
        "maximum_channels": 16,
        "maximum_owned_bytes": 4 * 1024 * 1024,
        "maximum_mapped_bytes": 4 * 1024 * 1024,
        "maximum_region_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return ControlShmPolicy(**values)


def _push_bytes(output: bytearray, value: bytes) -> None:
    output.extend(struct.pack("<I", len(value)))
    output.extend(value)


def _push_string(output: bytearray, value: str) -> None:
    _push_bytes(output, value.encode("utf-8"))


def _read_string(payload: bytes, position: int) -> tuple[str, int]:
    size = struct.unpack_from("<I", payload, position)[0]
    position += 4
    return payload[position : position + size].decode("utf-8"), position + size


def _request_lease(payload: bytes) -> dict[str, int | str]:
    assert payload[:6] == b"SRLC\x05\x03"
    position = 6
    _, position = _read_string(payload, position)
    _, position = _read_string(payload, position)
    position += 2
    _, position = _read_string(payload, position)
    position += 8 + 4 + 4 + 4 + 1 + 8
    present = payload[position]
    position += 1
    assert present == 1
    token, position = _read_string(payload, position)
    slot_index, slot_size, slot_count, length = struct.unpack_from("<IIII", payload, position)
    return {
        "region_name": token,
        "slot_index": slot_index,
        "slot_size": slot_size,
        "slot_count": slot_count,
        "length": length,
    }


def _handler_request_frame(lease: dict[str, int | str], payload_length: int) -> bytes:
    output = bytearray(b"SRLC\x05\x0b")
    _push_string(output, "sender")
    _push_string(output, "sample.topic")
    output.extend(struct.pack("<qff", 1234, 10.0, 20.0))
    output.extend(struct.pack("<Q", 1_000_000_000))
    output.append(1)
    _push_string(output, str(lease["region_name"]))
    output.extend(
        struct.pack(
            "<IIII",
            int(lease["slot_index"]),
            int(lease["slot_size"]),
            int(lease["slot_count"]),
            payload_length,
        )
    )
    return bytes(output)


def _transact_response_frame(
    lease: dict[str, int | str] | None,
    *,
    ok: bool = True,
    status: int = 0,
    timestamp_ns: int = 9001,
    error: str = "",
    response_ticket: int = 1,
) -> bytes:
    output = bytearray(b"SRLC\x05\x04")
    output.extend(struct.pack("<BHq", int(ok), status, timestamp_ns))
    _push_string(output, error)
    if lease is None:
        output.append(0)
        _push_bytes(output, b"")
    else:
        output.append(1)
        _push_string(output, str(lease["region_name"]))
        output.extend(
            struct.pack(
                "<IIII",
                int(lease["slot_index"]),
                int(lease["slot_size"]),
                int(lease["slot_count"]),
                int(lease["length"]),
            )
        )
    if ok:
        output.extend(struct.pack("<Q", response_ticket))
    return bytes(output)
