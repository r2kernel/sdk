"""Bounded assembly of explicitly framed stream transfer units.

This helper reassembles bytes only. It does not decode media or sensor data and
does not treat receipt of a complete unit as decoder confirmation. The caller
must select/configure the stream identity and call :meth:`confirm_decoded` only
after its decoder has accepted a unit.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


_INT64_MAX = (1 << 63) - 1
_UINT32_MAX = (1 << 32) - 1
_STREAM_IDENTITY_FIELDS = {"session_id", "configuration_revision", "format_revision"}
_TRANSFER_HEADER_FIELDS = {
    "stream", "sample_id", "unit_sequence", "acquired_at_sync_ns", "expires_at_sync_ns", "independent"
}
_ENCODED_CHUNK_FIELDS = {"part_index", "part_count", "total_payload_bytes", "offset_bytes", "payload_bytes"}
_STREAM_LIMIT_FIELDS = {
    "max_codec_config_bytes", "max_encoded_unit_bytes", "max_decoded_unit_bytes", "max_chunk_bytes",
    "max_parts_per_unit", "max_pending_units", "max_pending_bytes", "max_reassembly_age_ns",
}


class StreamAssemblyError(ValueError):
    """A malformed, expired, or unsupported stream transfer part."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PartDisposition(str, Enum):
    INCOMPLETE = "incomplete"
    DUPLICATE = "duplicate"
    COMPLETE = "complete"


@dataclass(frozen=True)
class StreamIdentity:
    session_id: str
    configuration_revision: int
    format_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise StreamAssemblyError("invalid_stream", "stream.session_id must be non-empty")
        object.__setattr__(
            self, "configuration_revision", _integer(self.configuration_revision, "configuration_revision", minimum=1)
        )
        object.__setattr__(self, "format_revision", _integer(self.format_revision, "format_revision", minimum=1))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StreamIdentity":
        _require_exact_fields(value, _STREAM_IDENTITY_FIELDS, "StreamIdentity")
        return cls(value["session_id"], value["configuration_revision"], value["format_revision"])


@dataclass(frozen=True)
class TransferPartHeader:
    stream: StreamIdentity
    sample_id: int
    unit_sequence: int
    acquired_at_sync_ns: int
    expires_at_sync_ns: int
    independent: bool
    reference_unit_sequences: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        stream = self.stream if isinstance(self.stream, StreamIdentity) else StreamIdentity.from_mapping(self.stream)
        object.__setattr__(self, "stream", stream)
        object.__setattr__(self, "sample_id", _integer(self.sample_id, "sample_id", minimum=1))
        object.__setattr__(self, "unit_sequence", _integer(self.unit_sequence, "unit_sequence", minimum=1))
        acquired = _integer(self.acquired_at_sync_ns, "acquired_at_sync_ns")
        expires = _integer(self.expires_at_sync_ns, "expires_at_sync_ns")
        if expires <= acquired:
            raise StreamAssemblyError("invalid_sample_lifetime", "expires_at_sync_ns must follow acquisition")
        if not isinstance(self.independent, bool):
            raise StreamAssemblyError("invalid_transfer_header", "independent must be a boolean")
        if not isinstance(self.reference_unit_sequences, tuple):
            raise StreamAssemblyError("invalid_dependency", "typed reference_unit_sequences must be an immutable tuple")
        references = self.reference_unit_sequences
        for reference in references:
            _integer(reference, "reference_unit_sequence", minimum=1)
        object.__setattr__(self, "reference_unit_sequences", references)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, max_reference_sequences: int | None = None
    ) -> "TransferPartHeader":
        if not isinstance(value, Mapping):
            raise StreamAssemblyError("invalid_transfer_header", "TransferPartHeader must be a mapping")
        keys = set(value)
        missing = _TRANSFER_HEADER_FIELDS - keys
        unknown = keys - (_TRANSFER_HEADER_FIELDS | {"reference_unit_sequences"})
        if missing or unknown:
            raise StreamAssemblyError("invalid_transfer_header", _field_error("TransferPartHeader", missing, unknown))
        stream = value["stream"]
        if not isinstance(stream, StreamIdentity):
            stream = StreamIdentity.from_mapping(stream)
        refs = value.get("reference_unit_sequences", ())
        if not isinstance(refs, (tuple, list)):
            raise StreamAssemblyError("invalid_dependency", "reference_unit_sequences must be a list")
        if max_reference_sequences is not None and len(refs) > max_reference_sequences:
            raise StreamAssemblyError("reference_limit", "reference_unit_sequences exceeds max_pending_units")
        return cls(
            stream=stream,
            sample_id=value["sample_id"],
            unit_sequence=value["unit_sequence"],
            acquired_at_sync_ns=value["acquired_at_sync_ns"],
            expires_at_sync_ns=value["expires_at_sync_ns"],
            independent=value["independent"],
            reference_unit_sequences=tuple(refs),
        )


@dataclass(frozen=True)
class EncodedChunk:
    part_index: int
    part_count: int
    total_payload_bytes: int
    offset_bytes: int
    payload_bytes: bytes | bytearray | memoryview

    def __post_init__(self) -> None:
        part_index = _integer(self.part_index, "part_index", maximum=_UINT32_MAX)
        part_count = _integer(self.part_count, "part_count", minimum=1, maximum=_UINT32_MAX)
        if part_index >= part_count:
            raise StreamAssemblyError("invalid_chunk", "part_index must be below part_count")
        object.__setattr__(self, "total_payload_bytes", _integer(self.total_payload_bytes, "total_payload_bytes"))
        object.__setattr__(self, "offset_bytes", _integer(self.offset_bytes, "offset_bytes"))
        if not isinstance(self.payload_bytes, (bytes, bytearray, memoryview)):
            raise StreamAssemblyError("invalid_chunk", "payload_bytes must contain bytes")

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, max_payload_bytes: int | None = None
    ) -> "EncodedChunk":
        _require_exact_fields(value, _ENCODED_CHUNK_FIELDS, "EncodedChunk")
        payload_bytes = value["payload_bytes"]
        payload_size_bytes = _payload_size_bytes(payload_bytes)
        if max_payload_bytes is not None and payload_size_bytes > max_payload_bytes:
            raise StreamAssemblyError("chunk_size_limit", "payload_bytes exceeds max_chunk_bytes")
        return cls(**{field_name: value[field_name] for field_name in _ENCODED_CHUNK_FIELDS})


@dataclass(frozen=True)
class StreamLimits:
    max_codec_config_bytes: int
    max_encoded_unit_bytes: int
    max_decoded_unit_bytes: int
    max_chunk_bytes: int
    max_parts_per_unit: int
    max_pending_units: int
    max_pending_bytes: int
    max_reassembly_age_ns: int

    def __post_init__(self) -> None:
        for field_name in ("max_codec_config_bytes", "max_chunk_bytes", "max_parts_per_unit", "max_pending_units"):
            object.__setattr__(
                self, field_name, _integer(getattr(self, field_name), field_name, minimum=1, maximum=_UINT32_MAX)
            )
        for field_name in (
            "max_encoded_unit_bytes", "max_decoded_unit_bytes", "max_pending_bytes", "max_reassembly_age_ns"
        ):
            object.__setattr__(self, field_name, _integer(getattr(self, field_name), field_name, minimum=1))
        if self.max_chunk_bytes > self.max_encoded_unit_bytes:
            raise StreamAssemblyError("invalid_limits", "max_chunk_bytes exceeds max_encoded_unit_bytes")
        if self.max_encoded_unit_bytes > self.max_pending_bytes:
            raise StreamAssemblyError("invalid_limits", "max_pending_bytes cannot hold one maximum unit")
        if self.max_parts_per_unit * self.max_chunk_bytes < self.max_encoded_unit_bytes:
            raise StreamAssemblyError("invalid_limits", "part and chunk limits cannot carry one maximum unit")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StreamLimits":
        _require_exact_fields(value, _STREAM_LIMIT_FIELDS, "StreamLimits")
        return cls(**{field_name: value[field_name] for field_name in _STREAM_LIMIT_FIELDS})


@dataclass(frozen=True)
class CompletedEncodedUnit:
    """A byte-reassembled unit, not a decoded or decoder-confirmed sample."""

    header: TransferPartHeader
    payload_bytes: bytes


@dataclass(frozen=True)
class PartAddResult:
    disposition: PartDisposition
    completed_unit: CompletedEncodedUnit | None = None


@dataclass
class _PartialUnit:
    header: TransferPartHeader
    part_count: int
    total_payload_bytes: int
    payload: bytearray
    parts: dict[int, tuple[int, int, bytes]] = field(default_factory=dict)
    first_seen_monotonic_ns: int = 0


@dataclass
class _AwaitingDecoder:
    header: TransferPartHeader
    part_count: int
    total_payload_bytes: int
    parts: dict[int, tuple[int, int, bytes]]
    first_seen_monotonic_ns: int


class StreamTransferAssembler:
    """Reassemble bounded transfer parts for one explicitly configured stream.

    `configure` and `reset_for_resync` are trusted local lifecycle operations;
    packet contents cannot change the active identity. A completed unit occupies
    one bounded pending slot until the caller explicitly confirms decoder success.
    """

    def __init__(
        self,
        *,
        identity: StreamIdentity,
        limits: StreamLimits,
        sync_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
        validate_complete_unit: Callable[[TransferPartHeader, bytes], Any] | None = None,
    ) -> None:
        if not isinstance(identity, StreamIdentity) or not isinstance(limits, StreamLimits):
            raise TypeError("identity and limits must be validated StreamIdentity and StreamLimits values")
        if not callable(sync_clock_ns) or not callable(monotonic_clock_ns):
            raise TypeError("clock providers must be callable")
        if validate_complete_unit is not None and not callable(validate_complete_unit):
            raise TypeError("validate_complete_unit must be callable")
        self._identity = identity
        self._limits = limits
        self._sync_clock_ns = sync_clock_ns
        self._monotonic_clock_ns = monotonic_clock_ns
        self._validate_complete_unit = validate_complete_unit
        self._lock = threading.RLock()
        self._partial_units: dict[int, _PartialUnit] = {}
        self._awaiting_decoder: dict[int, _AwaitingDecoder] = {}
        self._pending_bytes = 0
        self._last_started_unit_sequence = 0
        self._decoded_unit_sequences: set[int] = set()
        self._decoded_sequence_order: deque[int] = deque()
        self._resync_required = False

    @property
    def identity(self) -> StreamIdentity:
        return self._identity

    @property
    def resync_required(self) -> bool:
        with self._lock:
            return self._resync_required

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "identity": self._identity,
                "pending_units": len(self._partial_units) + len(self._awaiting_decoder),
                "partial_units": len(self._partial_units),
                "awaiting_decoder": len(self._awaiting_decoder),
                "pending_bytes": self._pending_bytes,
                "last_started_unit_sequence": self._last_started_unit_sequence,
                "decoder_confirmed_units": len(self._decoded_unit_sequences),
                "resync_required": self._resync_required,
            }

    def configure(self, identity: StreamIdentity) -> None:
        """Install an explicitly selected identity and clear prior stream state."""
        if not isinstance(identity, StreamIdentity):
            raise TypeError("identity must be a validated StreamIdentity")
        with self._lock:
            if identity == self._identity:
                return
            self._identity = identity
            self._clear_locked()

    def reset_for_resync(self) -> None:
        """Clear buffered/dependency state after an explicit provider/decoder reset."""
        with self._lock:
            self._clear_locked()

    def add_part(
        self,
        header: TransferPartHeader | Mapping[str, Any],
        chunk: EncodedChunk | Mapping[str, Any],
    ) -> PartAddResult:
        resolved_header = (
            header
            if isinstance(header, TransferPartHeader)
            else TransferPartHeader.from_mapping(
                header, max_reference_sequences=self._limits.max_pending_units
            )
        )
        resolved_chunk = (
            chunk
            if isinstance(chunk, EncodedChunk)
            else EncodedChunk.from_mapping(chunk, max_payload_bytes=self._limits.max_chunk_bytes)
        )
        now_sync_ns = _clock(self._sync_clock_ns, "synchronized")
        now_monotonic_ns = _clock(self._monotonic_clock_ns, "monotonic")
        with self._lock:
            self._expire_locked(now_sync_ns, now_monotonic_ns)
            if self._resync_required:
                raise StreamAssemblyError("resync_required", "stream must be reset before accepting more parts")
            self._validate_header_locked(resolved_header, resolved_chunk, now_sync_ns)
            if not isinstance(resolved_chunk.payload_bytes, bytes):
                resolved_chunk = EncodedChunk(
                    resolved_chunk.part_index,
                    resolved_chunk.part_count,
                    resolved_chunk.total_payload_bytes,
                    resolved_chunk.offset_bytes,
                    bytes(resolved_chunk.payload_bytes),
                )
            sequence = resolved_header.unit_sequence
            partial = self._partial_units.get(sequence)
            if partial is None:
                completed = self._awaiting_decoder.get(sequence)
                if completed is not None:
                    return self._check_duplicate_completed_locked(completed, resolved_header, resolved_chunk)
                if sequence <= self._last_started_unit_sequence:
                    raise StreamAssemblyError("stale_unit_sequence", "unit_sequence must increase for new units")
                if len(self._partial_units) + len(self._awaiting_decoder) >= self._limits.max_pending_units:
                    raise StreamAssemblyError("pending_unit_limit", "max_pending_units is exhausted")
                if self._pending_bytes + resolved_chunk.total_payload_bytes > self._limits.max_pending_bytes:
                    raise StreamAssemblyError("pending_byte_limit", "max_pending_bytes would be exceeded")
                self._validate_dependencies_locked(resolved_header)
                try:
                    buffer = bytearray(resolved_chunk.total_payload_bytes)
                except (MemoryError, OverflowError) as exc:
                    raise StreamAssemblyError("allocation_failed", "selected unit size could not be allocated") from exc
                partial = _PartialUnit(
                    header=resolved_header,
                    part_count=resolved_chunk.part_count,
                    total_payload_bytes=resolved_chunk.total_payload_bytes,
                    payload=buffer,
                    first_seen_monotonic_ns=now_monotonic_ns,
                )
                self._partial_units[sequence] = partial
                self._pending_bytes += resolved_chunk.total_payload_bytes
                self._last_started_unit_sequence = sequence
            else:
                self._validate_consistent_unit(partial, resolved_header, resolved_chunk)

            self._validate_chunk_bounds(resolved_chunk, partial.total_payload_bytes)
            disposition = self._record_part(partial, resolved_chunk)
            if disposition is PartDisposition.DUPLICATE:
                return PartAddResult(disposition)
            if len(partial.parts) != partial.part_count:
                return PartAddResult(PartDisposition.INCOMPLETE)

            try:
                payload_bytes = bytes(partial.payload)
                self._validate_exact_coverage(partial)
                if self._validate_complete_unit is not None:
                    self._validate_complete_unit(partial.header, payload_bytes)
            except Exception as exc:
                self._drop_partial_locked(sequence)
                self._resync_required = True
                if isinstance(exc, StreamAssemblyError):
                    raise
                raise StreamAssemblyError("invalid_complete_unit", str(exc)) from exc

            completed_header = partial.header
            self._partial_units.pop(sequence)
            self._awaiting_decoder[sequence] = _AwaitingDecoder(
                completed_header,
                partial.part_count,
                partial.total_payload_bytes,
                dict(partial.parts),
                partial.first_seen_monotonic_ns,
            )
            return PartAddResult(
                PartDisposition.COMPLETE,
                CompletedEncodedUnit(completed_header, payload_bytes),
            )

    def confirm_decoded(self, unit_sequence: int) -> None:
        """Record explicit decoder acceptance so later dependents may reference it."""
        sequence = _integer(unit_sequence, "unit_sequence", minimum=1)
        now_sync_ns = _clock(self._sync_clock_ns, "synchronized")
        now_monotonic_ns = _clock(self._monotonic_clock_ns, "monotonic")
        with self._lock:
            expired_sequences = self._expire_locked(now_sync_ns, now_monotonic_ns)
            if sequence in expired_sequences:
                raise StreamAssemblyError("expired_sample", "unit expired before decoder confirmation")
            if self._resync_required:
                raise StreamAssemblyError("resync_required", "stream must be reset before decoder confirmation")
            if sequence in self._decoded_unit_sequences:
                return
            awaiting = self._awaiting_decoder.pop(sequence, None)
            if awaiting is None:
                raise StreamAssemblyError("unit_not_awaiting_decoder", "unit has not been completed for decoder confirmation")
            if now_sync_ns >= awaiting.header.expires_at_sync_ns:
                self._pending_bytes -= awaiting.total_payload_bytes
                self._resync_required = True
                raise StreamAssemblyError("expired_sample", "unit expired before decoder confirmation")
            self._pending_bytes -= awaiting.total_payload_bytes
            self._remember_decoded_locked(sequence)

    def expire(self) -> tuple[int, ...]:
        """Drop units past original synchronized expiry or bounded reassembly age."""
        now_sync_ns = _clock(self._sync_clock_ns, "synchronized")
        now_monotonic_ns = _clock(self._monotonic_clock_ns, "monotonic")
        with self._lock:
            return self._expire_locked(now_sync_ns, now_monotonic_ns)

    def _validate_header_locked(
        self,
        header: TransferPartHeader,
        chunk: EncodedChunk,
        now_sync_ns: int,
    ) -> None:
        if header.stream != self._identity:
            raise StreamAssemblyError("stream_identity_mismatch", "part does not match the explicitly configured stream")
        if header.acquired_at_sync_ns > now_sync_ns:
            raise StreamAssemblyError("future_acquisition", "acquired_at_sync_ns cannot be in the future")
        if now_sync_ns >= header.expires_at_sync_ns:
            raise StreamAssemblyError("expired_sample", "part's original sample lifetime has expired")
        if len(header.reference_unit_sequences) > self._limits.max_pending_units:
            raise StreamAssemblyError("reference_limit", "reference_unit_sequences exceeds max_pending_units")
        if len(set(header.reference_unit_sequences)) != len(header.reference_unit_sequences):
            raise StreamAssemblyError("invalid_dependency", "reference_unit_sequences contains duplicates")
        if header.independent and header.reference_unit_sequences:
            raise StreamAssemblyError("invalid_dependency", "independent units cannot declare dependencies")
        if not header.independent and not header.reference_unit_sequences:
            raise StreamAssemblyError("invalid_dependency", "dependent units require explicit references")
        if any(reference >= header.unit_sequence for reference in header.reference_unit_sequences):
            raise StreamAssemblyError("invalid_dependency", "a unit may reference only an earlier unit")
        if chunk.part_count > self._limits.max_parts_per_unit:
            raise StreamAssemblyError("part_count_limit", "part_count exceeds max_parts_per_unit")
        if chunk.total_payload_bytes > self._limits.max_encoded_unit_bytes:
            raise StreamAssemblyError("unit_size_limit", "total_payload_bytes exceeds max_encoded_unit_bytes")
        if _payload_size_bytes(chunk.payload_bytes) > self._limits.max_chunk_bytes:
            raise StreamAssemblyError("chunk_size_limit", "payload_bytes exceeds max_chunk_bytes")
        self._validate_chunk_bounds(chunk, chunk.total_payload_bytes)

    def _validate_dependencies_locked(self, header: TransferPartHeader) -> None:
        if not header.independent:
            missing = [reference for reference in header.reference_unit_sequences if reference not in self._decoded_unit_sequences]
            if missing:
                self._resync_required = True
                raise StreamAssemblyError(
                    "resync_required", f"dependent unit references unconfirmed or unavailable units: {missing}"
                )

    def _validate_consistent_unit(
        self,
        partial: _PartialUnit,
        header: TransferPartHeader,
        chunk: EncodedChunk,
    ) -> None:
        if (
            partial.header != header
            or partial.part_count != chunk.part_count
            or partial.total_payload_bytes != chunk.total_payload_bytes
        ):
            raise StreamAssemblyError("unit_header_changed", "unit identity, expiry, or framing changed between parts")

    def _validate_chunk_bounds(self, chunk: EncodedChunk, total_bytes: int) -> None:
        if chunk.part_count > self._limits.max_parts_per_unit:
            raise StreamAssemblyError("part_count_limit", "part_count exceeds max_parts_per_unit")
        if total_bytes != chunk.total_payload_bytes:
            raise StreamAssemblyError("unit_header_changed", "total_payload_bytes changed between parts")
        payload_length = _payload_size_bytes(chunk.payload_bytes)
        if total_bytes == 0:
            if (chunk.part_count, chunk.part_index, chunk.offset_bytes, payload_length) != (1, 0, 0, 0):
                raise StreamAssemblyError("invalid_empty_unit", "empty units require one canonical empty part")
            return
        if payload_length == 0:
            raise StreamAssemblyError("empty_chunk", "non-empty units cannot contain empty chunks")
        if chunk.offset_bytes + payload_length > total_bytes:
            raise StreamAssemblyError("chunk_out_of_bounds", "part extends beyond total_payload_bytes")

    def _record_part(self, partial: _PartialUnit, chunk: EncodedChunk) -> PartDisposition:
        digest = hashlib.sha256(chunk.payload_bytes).digest()
        existing = partial.parts.get(chunk.part_index)
        record = (chunk.offset_bytes, len(chunk.payload_bytes), digest)
        if existing is not None:
            if existing == record:
                return PartDisposition.DUPLICATE
            raise StreamAssemblyError("conflicting_duplicate", "same part_index has different offset or bytes")
        start = chunk.offset_bytes
        end = start + len(chunk.payload_bytes)
        for other_start, other_length, _ in partial.parts.values():
            other_end = other_start + other_length
            if start < other_end and other_start < end:
                raise StreamAssemblyError("overlapping_parts", "part byte range overlaps another part")
        partial.payload[start:end] = chunk.payload_bytes
        partial.parts[chunk.part_index] = record
        return PartDisposition.INCOMPLETE

    def _check_duplicate_completed_locked(
        self,
        completed: _AwaitingDecoder,
        header: TransferPartHeader,
        chunk: EncodedChunk,
    ) -> PartAddResult:
        if completed.header != header or completed.part_count != chunk.part_count or completed.total_payload_bytes != chunk.total_payload_bytes:
            raise StreamAssemblyError("unit_header_changed", "completed unit retry changed its original header")
        existing = completed.parts.get(chunk.part_index)
        if existing is None:
            raise StreamAssemblyError("conflicting_duplicate", "completed unit received an undeclared part index")
        record = (chunk.offset_bytes, len(chunk.payload_bytes), hashlib.sha256(chunk.payload_bytes).digest())
        if existing != record:
            raise StreamAssemblyError("conflicting_duplicate", "completed unit retry changed part offset or bytes")
        return PartAddResult(PartDisposition.DUPLICATE)

    def _validate_exact_coverage(self, partial: _PartialUnit) -> None:
        intervals = sorted((offset, length) for offset, length, _ in partial.parts.values())
        expected_offset = 0
        for offset, length in intervals:
            if offset != expected_offset:
                raise StreamAssemblyError("incomplete_coverage", "assembled parts do not exactly cover the unit")
            expected_offset += length
        if expected_offset != partial.total_payload_bytes:
            raise StreamAssemblyError("incomplete_coverage", "assembled parts do not exactly cover the unit")

    def _remember_decoded_locked(self, sequence: int) -> None:
        if sequence in self._decoded_unit_sequences:
            return
        self._decoded_unit_sequences.add(sequence)
        self._decoded_sequence_order.append(sequence)
        while len(self._decoded_sequence_order) > self._limits.max_pending_units:
            expired_sequence = self._decoded_sequence_order.popleft()
            self._decoded_unit_sequences.discard(expired_sequence)

    def _expire_locked(self, now_sync_ns: int, now_monotonic_ns: int) -> tuple[int, ...]:
        expired_sequences: list[int] = []
        max_age_ns = self._limits.max_reassembly_age_ns
        for sequence, partial in tuple(self._partial_units.items()):
            expired_by_sync = now_sync_ns >= partial.header.expires_at_sync_ns
            expired_by_age = now_monotonic_ns - partial.first_seen_monotonic_ns >= max_age_ns
            if expired_by_sync or expired_by_age:
                self._drop_partial_locked(sequence)
                expired_sequences.append(sequence)
        for sequence, awaiting in tuple(self._awaiting_decoder.items()):
            expired_by_sync = now_sync_ns >= awaiting.header.expires_at_sync_ns
            expired_by_age = now_monotonic_ns - awaiting.first_seen_monotonic_ns >= max_age_ns
            if expired_by_sync or expired_by_age:
                self._awaiting_decoder.pop(sequence, None)
                self._pending_bytes -= awaiting.total_payload_bytes
                expired_sequences.append(sequence)
        if expired_sequences:
            self._resync_required = True
        return tuple(sorted(expired_sequences))

    def _drop_partial_locked(self, sequence: int) -> None:
        partial = self._partial_units.pop(sequence, None)
        if partial is not None:
            self._pending_bytes -= partial.total_payload_bytes

    def _clear_locked(self) -> None:
        self._partial_units.clear()
        self._awaiting_decoder.clear()
        self._pending_bytes = 0
        self._last_started_unit_sequence = 0
        self._decoded_unit_sequences.clear()
        self._decoded_sequence_order.clear()
        self._resync_required = False


def _require_exact_fields(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise StreamAssemblyError("invalid_mapping", f"{name} must be a mapping")
    keys = set(value)
    missing = fields - keys
    unknown = keys - fields
    if missing or unknown:
        raise StreamAssemblyError("invalid_mapping", _field_error(name, missing, unknown))


def _field_error(name: str, missing: set[Any], unknown: set[Any]) -> str:
    details = []
    if missing:
        details.append("missing " + ", ".join(sorted(map(str, missing))))
    if unknown:
        details.append("unknown " + ", ".join(sorted(map(str, unknown))))
    return f"{name} has " + " and ".join(details)


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int = _INT64_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StreamAssemblyError("invalid_integer", f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _clock(provider: Callable[[], int], name: str) -> int:
    return _integer(provider(), f"{name}_clock_ns")


def _payload_size_bytes(payload: Any) -> int:
    if isinstance(payload, memoryview):
        return payload.nbytes
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    raise StreamAssemblyError("invalid_chunk", "payload_bytes must contain bytes")
