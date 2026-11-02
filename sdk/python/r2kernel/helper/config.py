"""Lightweight YAML config helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

import yaml

PathLike = Union[str, Path]


class _MyDumper(yaml.SafeDumper):
    def write_line_break(self, data=None):
        super().write_line_break(data)
        if len(self.indents) == 1:
            super().write_line_break()


class Config:
    """Load/save YAML config with optional defaults and nested key access."""

    def __init__(self, config_file_path: PathLike, default_config_path: PathLike | None = None) -> None:
        self._config_file_path = Path(config_file_path)
        self._default_config_path = Path(default_config_path) if default_config_path else None
        self._config_data: dict[str, Any] = {}
        self._default_config_data: dict[str, Any] = {}
        if self._default_config_path and self._default_config_path.is_file():
            self._default_config_data = yaml.safe_load(self._default_config_path.read_text()) or {}

    def load_config(self):
        if self._config_file_path.is_file():
            self._config_data = yaml.safe_load(self._config_file_path.read_text()) or {}
        else:
            self._config_data = self._default_config_data.copy()
            self.save_config()

    def save_config(self):
        self._config_file_path.parent.mkdir(parents=True, exist_ok=True)
        with self._config_file_path.open("w") as file:
            yaml.dump(self._config_data, file, Dumper=_MyDumper, default_flow_style=False, sort_keys=False)

    def set_config_value(self, value: Any, *keys: str | list[str] | tuple[str, ...]):
        """
        Set a nested value using either:
          - variadic keys: set_config_value(123, "a", "b")
          - dotted path: set_config_value(123, "a.b")
          - iterable path: set_config_value(123, ["a", "b"])
        """
        path = _normalize_path(keys)
        if not path:
            return
        _set_dict_value(self._config_data, value, *path)

    def get_config_value(self, *keys: str | list[str] | tuple[str, ...], default: Any = None):
        """
        Retrieve a nested value. Missing keys return `default` (None by default) instead of raising.
        Accepts the same path formats as set_config_value.
        """
        path = _normalize_path(keys)
        if not path:
            return default
        val = _get_dict_value(self._config_data, *path)
        return default if val is None else val

    @property
    def data(self) -> dict[str, Any]:
        return self._config_data

    def get_root_path(self) -> str | None:
        return _get_dict_value(self._config_data, "path", "root")

    def get_userdata_path(self) -> str | None:
        return _get_dict_value(self._config_data, "path", "userdata")


def _get_dict_value(dictionary: dict[str, Any], *keys: str):
    if not keys:
        return None
    current: Any = dictionary
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return None
        current = current[k]
    return current


def _set_dict_value(dictionary: dict[str, Any], value: Any, *keys: str):
    if not keys:
        return
    current = dictionary
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def _normalize_path(keys: tuple[str | list[str] | tuple[str, ...], ...]) -> list[str]:
    if len(keys) == 1:
        single = keys[0]
        if isinstance(single, (list, tuple)):
            return [str(k) for k in single if k is not None and str(k) != ""]
        if isinstance(single, str):
            return _split_path(single)
    return [str(k) for k in keys if k is not None and str(k) != ""]


def _split_path(path: str) -> list[str]:
    """Split dotted or slash-delimited paths into components."""
    if not path:
        return []
    normalized = path.replace("/", ".")
    return [p for p in normalized.split(".") if p]
