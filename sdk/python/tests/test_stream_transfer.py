from __future__ import annotations

import pytest

from r2kernel.core.stream_transfer import (
    EncodedChunk,
    PartDisposition,
    StreamAssemblyError,
    StreamIdentity,
    StreamLimits,
    StreamTransferAssembler,
    TransferPartHeader,
)


class FakeClock:
    def __init__(self):
        self.sync_ns = 100
        self.monotonic_ns = 1000

    def synchronized(self):
        return self.sync_ns

    def monotonic(self):
        return self.monotonic_ns

    def advance(self, amount):
        self.sync_ns += amount
        self.monotonic_ns += amount


def identity(session="session-a", config_revision=1, format_revision=1):
    return StreamIdentity(session, config_revision, format_revision)


def limits(**changes):
    values = {
        "max_codec_config_bytes": 64,
        "max_encoded_unit_bytes": 64,
        "max_decoded_unit_bytes": 256,
        "max_chunk_bytes": 16,
        "max_parts_per_unit": 8,
        "max_pending_units": 3,
        "max_pending_bytes": 128,
        "max_reassembly_age_ns": 50,
    }
    values.update(changes)
    return StreamLimits(**values)


def header(sequence=1, **changes):
    values = {
        "stream": identity(),
        "sample_id": sequence,
        "unit_sequence": sequence,
        "acquired_at_sync_ns": 90,
        "expires_at_sync_ns": 200,
        "independent": True,
        "reference_unit_sequences": (),
    }
    values.update(changes)
    return TransferPartHeader(**values)


def chunk(index, count, total, offset, payload):
    return EncodedChunk(index, count, total, offset, payload)


def assembler(clock=None, selected_limits=None, **kwargs):
    clock = clock or FakeClock()
    return StreamTransferAssembler(
        identity=identity(),
        limits=selected_limits or limits(),
        sync_clock_ns=clock.synchronized,
        monotonic_clock_ns=clock.monotonic,
        **kwargs,
    )


def test_out_of_order_parts_reassemble_exact_bytes_and_exact_retry_is_idempotent():
    guard = assembler()
    unit_header = header()

    first = guard.add_part(unit_header, chunk(1, 2, 6, 3, b"def"))
    assert first.disposition is PartDisposition.INCOMPLETE
    assert guard.add_part(unit_header, chunk(1, 2, 6, 3, b"def")).disposition is PartDisposition.DUPLICATE

    completed = guard.add_part(unit_header, chunk(0, 2, 6, 0, b"abc"))
    assert completed.disposition is PartDisposition.COMPLETE
    assert completed.completed_unit.payload_bytes == b"abcdef"
    assert guard.snapshot()["awaiting_decoder"] == 1
    assert guard.snapshot()["pending_bytes"] == 6

    retry = guard.add_part(unit_header, chunk(0, 2, 6, 0, b"abc"))
    assert retry.disposition is PartDisposition.DUPLICATE
    guard.confirm_decoded(1)
    assert guard.snapshot()["decoder_confirmed_units"] == 1
    assert guard.snapshot()["pending_bytes"] == 0


def test_part_count_is_bounded_before_allocation_and_empty_rules_are_exact():
    guard = assembler(selected_limits=limits(max_parts_per_unit=2, max_chunk_bytes=32))
    with pytest.raises(StreamAssemblyError, match="max_parts_per_unit"):
        guard.add_part(header(), chunk(0, 3, 3, 0, b"a"))
    assert guard.snapshot()["pending_bytes"] == 0

    empty = assembler()
    result = empty.add_part(header(), chunk(0, 1, 0, 0, b""))
    assert result.disposition is PartDisposition.COMPLETE
    assert result.completed_unit.payload_bytes == b""

    nonempty = assembler()
    with pytest.raises(StreamAssemblyError, match="cannot contain empty chunks"):
        nonempty.add_part(header(), chunk(0, 1, 1, 0, b""))


def test_mapping_payload_is_size_checked_before_copy_and_memoryview_uses_byte_count():
    guard = assembler(selected_limits=limits(max_chunk_bytes=8))
    mapping = {
        "part_index": 0,
        "part_count": 1,
        "total_payload_bytes": 8,
        "offset_bytes": 0,
        "payload_bytes": memoryview(bytearray(16)).cast("I", shape=(4,)),
    }
    assert len(mapping["payload_bytes"]) == 4
    assert mapping["payload_bytes"].nbytes == 16
    with pytest.raises(StreamAssemblyError, match="exceeds max_chunk_bytes"):
        guard.add_part(header(), mapping)
    assert guard.snapshot()["pending_bytes"] == 0

    with pytest.raises(StreamAssemblyError, match="exceeds max_chunk_bytes"):
        guard.add_part(header(), chunk(0, 1, 16, 0, bytearray(16)))
    assert guard.snapshot()["pending_bytes"] == 0


def test_overlapping_parts_and_inexact_coverage_reject_and_require_explicit_resync():
    overlapping = assembler()
    unit_header = header()
    overlapping.add_part(unit_header, chunk(0, 2, 6, 0, b"abcd"))
    with pytest.raises(StreamAssemblyError, match="overlaps"):
        overlapping.add_part(unit_header, chunk(1, 2, 6, 3, b"def"))

    gap = assembler()
    gap.add_part(unit_header, chunk(0, 2, 6, 0, b"ab"))
    with pytest.raises(StreamAssemblyError, match="exactly cover"):
        gap.add_part(unit_header, chunk(1, 2, 6, 3, b"def"))
    assert gap.resync_required
    with pytest.raises(StreamAssemblyError, match="must be reset"):
        gap.add_part(header(2), chunk(0, 1, 1, 0, b"x"))
    gap.reset_for_resync()
    assert not gap.resync_required


def test_duplicate_part_with_changed_bytes_or_original_expiry_is_rejected():
    guard = assembler()
    original = header()
    guard.add_part(original, chunk(0, 2, 6, 0, b"abc"))
    with pytest.raises(StreamAssemblyError, match="different offset or bytes"):
        guard.add_part(original, chunk(0, 2, 6, 0, b"abd"))
    with pytest.raises(StreamAssemblyError, match="expiry"):
        guard.add_part(header(expires_at_sync_ns=201), chunk(1, 2, 6, 3, b"def"))


def test_pending_unit_and_byte_limits_are_enforced_without_fallback():
    selected = limits(max_pending_units=1, max_pending_bytes=64)
    guard = assembler(selected_limits=selected)
    guard.add_part(header(1), chunk(0, 2, 32, 0, b"a"))
    with pytest.raises(StreamAssemblyError, match="max_pending_units"):
        guard.add_part(header(2), chunk(0, 1, 1, 0, b"x"))
    assert guard.snapshot()["pending_bytes"] == 32

    byte_limited = assembler(selected_limits=limits(max_pending_bytes=64))
    byte_limited.add_part(header(1), chunk(0, 2, 48, 0, b"a"))
    with pytest.raises(StreamAssemblyError, match="max_pending_bytes"):
        byte_limited.add_part(header(2), chunk(0, 1, 32, 0, b"x"))


def test_expiry_and_max_reassembly_age_discard_partial_units_without_extending_lifetime():
    clock = FakeClock()
    guard = assembler(clock=clock, selected_limits=limits(max_reassembly_age_ns=10))
    guard.add_part(header(expires_at_sync_ns=500), chunk(0, 2, 6, 0, b"abc"))
    clock.advance(10)
    assert guard.expire() == (1,)
    assert guard.snapshot()["pending_bytes"] == 0
    assert guard.resync_required

    guard.reset_for_resync()
    with pytest.raises(StreamAssemblyError, match="original sample lifetime has expired"):
        guard.add_part(header(expires_at_sync_ns=105), chunk(0, 1, 1, 0, b"x"))


def test_completed_bytes_remain_bounded_until_confirmation_or_expiry():
    clock = FakeClock()
    guard = assembler(clock=clock, selected_limits=limits(max_reassembly_age_ns=10))
    result = guard.add_part(header(expires_at_sync_ns=500), chunk(0, 1, 4, 0, b"data"))
    assert result.disposition is PartDisposition.COMPLETE
    assert guard.snapshot()["pending_bytes"] == 4

    clock.advance(10)
    assert guard.expire() == (1,)
    assert guard.snapshot()["pending_bytes"] == 0
    assert guard.resync_required


def test_identity_changes_require_explicit_configuration():
    guard = assembler()
    with pytest.raises(StreamAssemblyError, match="explicitly configured stream"):
        guard.add_part(header(stream=identity(session="session-b")), chunk(0, 1, 1, 0, b"x"))
    guard.configure(identity(session="session-b", config_revision=2, format_revision=3))
    current = TransferPartHeader(
        identity(session="session-b", config_revision=2, format_revision=3), 2, 2, 90, 200, True
    )
    result = guard.add_part(current, chunk(0, 1, 1, 0, b"x"))
    assert result.completed_unit.header.stream == guard.identity


def test_configuring_same_identity_preserves_parts_and_decoder_references():
    guard = assembler()
    independent = guard.add_part(header(1), chunk(0, 1, 1, 0, b"a")).completed_unit
    assert independent is not None
    guard.confirm_decoded(1)

    dependent_header = header(2, independent=False, reference_unit_sequences=(1,))
    guard.add_part(dependent_header, chunk(0, 2, 2, 0, b"a"))
    assert guard.snapshot()["pending_bytes"] == 2
    assert guard.snapshot()["partial_units"] == 1

    guard.configure(identity())
    assert guard.snapshot()["pending_bytes"] == 2
    assert guard.snapshot()["partial_units"] == 1
    assert guard.snapshot()["decoder_confirmed_units"] == 1

    completed = guard.add_part(dependent_header, chunk(1, 2, 2, 1, b"b")).completed_unit
    assert completed is not None and completed.payload_bytes == b"ab"
    guard.confirm_decoded(2)

    replacement_identity = identity(session="session-b", config_revision=2, format_revision=3)
    guard.configure(replacement_identity)
    replacement_snapshot = guard.snapshot()
    assert replacement_snapshot["identity"] == replacement_identity
    assert replacement_snapshot["pending_bytes"] == 0
    assert replacement_snapshot["partial_units"] == 0
    assert replacement_snapshot["decoder_confirmed_units"] == 0
    assert replacement_snapshot["last_started_unit_sequence"] == 0

    replacement_header = header(
        1,
        stream=replacement_identity,
        sample_id=1,
        unit_sequence=1,
    )
    guard.add_part(replacement_header, chunk(0, 2, 2, 0, b"x"))
    assert guard.snapshot()["pending_bytes"] == 2
    guard.reset_for_resync()
    reset_snapshot = guard.snapshot()
    assert reset_snapshot["pending_bytes"] == 0
    assert reset_snapshot["partial_units"] == 0
    assert reset_snapshot["decoder_confirmed_units"] == 0
    assert reset_snapshot["last_started_unit_sequence"] == 0


def test_stream_identity_preserves_opaque_session_id_exactly():
    exact_identity = identity(session=" session-a ")
    assert exact_identity.session_id == " session-a "


def test_dependent_unit_requires_explicit_decoder_confirmation_and_missing_dependency_resyncs():
    guard = assembler()
    independent_unit = guard.add_part(header(1), chunk(0, 1, 1, 0, b"a")).completed_unit
    assert independent_unit.payload_bytes == b"a"

    dependent_header = header(2, independent=False, reference_unit_sequences=(1,))
    with pytest.raises(StreamAssemblyError) as error:
        guard.add_part(dependent_header, chunk(0, 1, 1, 0, b"b"))
    assert error.value.code == "resync_required"
    assert guard.resync_required

    guard.reset_for_resync()
    independent_unit = guard.add_part(header(1), chunk(0, 1, 1, 0, b"a")).completed_unit
    assert independent_unit is not None
    guard.confirm_decoded(1)
    dependent = guard.add_part(dependent_header, chunk(0, 1, 1, 0, b"b")).completed_unit
    assert dependent.payload_bytes == b"b"
    assert guard.snapshot()["decoder_confirmed_units"] == 1
    guard.confirm_decoded(2)


def test_malformed_mappings_and_noninteger_counters_are_rejected():
    with pytest.raises(StreamAssemblyError, match="must be an integer"):
        StreamIdentity("session-a", True, 1)
    with pytest.raises(StreamAssemblyError, match="missing"):
        StreamIdentity.from_mapping({"session_id": "session-a", "configuration_revision": 1})
    with pytest.raises(StreamAssemblyError, match="part_index"):
        EncodedChunk(True, 1, 0, 0, b"")
