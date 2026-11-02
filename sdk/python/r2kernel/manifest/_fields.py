"""YAML loading and field coercion helpers shared by the manifest parsers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable
import yaml

from .model import ManifestError


def _float_tuple(value: Any, *, length: int, field_name: str) -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ManifestError(f"{field_name} must contain exactly {length} numbers")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{field_name} must contain only numbers") from exc
    if not all(math.isfinite(item) for item in result):
        raise ManifestError(f"{field_name} must contain finite numbers")
    return result


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"{label.capitalize()} file not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ManifestError(f"{label.capitalize()} must be a mapping: {path}")
    return document


def _string_mapping(value: Any, *, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    return {str(key).strip(): str(item).strip() for key, item in value.items() if str(key).strip()}


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    return dict(value)


def _nested_mapping(value: Any, *, field_name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        name = str(key or "").strip()
        if not name:
            raise ManifestError(f"{field_name} contains an empty key")
        if item is None:
            result[name] = {}
        elif isinstance(item, dict):
            result[name] = dict(item)
        else:
            raise ManifestError(f"{field_name}.{name} must be a mapping")
    return result


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ManifestError("Expected a string or list of strings")
