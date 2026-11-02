"""Versioned managed-Control transport topic envelope."""

from __future__ import annotations


SOURCE_TOPIC_PREFIX = "__r2kernel_source_v1__:"
RESERVED_TOPIC_PREFIX = "__r2kernel_source_"


def encode_source_topic(source_name: str, topic: str) -> str:
    """Bind a managed-local topic to the SDK's configured subsystem identity.

    This declaration identifies the SDK transport client; it is not an
    authentication boundary. The backend must recognize the versioned envelope
    explicitly and reject malformed or unsupported reserved prefixes.
    """
    if source_name.startswith(("__r2kernel_authority_", "__r2kernel_host_")):
        raise ValueError("source name uses the reserved authenticated-source prefix")
    source_bytes = _validate_text(source_name, "source name", 128)
    _validate_text(topic, "topic", 1024)
    if topic.startswith(RESERVED_TOPIC_PREFIX):
        raise ValueError("original topic uses the reserved managed-source prefix")
    return f"{SOURCE_TOPIC_PREFIX}{source_bytes.hex()}:{topic}"


def _validate_text(value: str, name: str, maximum_bytes: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if not encoded or len(encoded) > maximum_bytes:
        raise ValueError(f"{name} must contain 1..{maximum_bytes} UTF-8 bytes")
    if b"\0" in encoded:
        raise ValueError(f"{name} must not contain NUL")
    return encoded
