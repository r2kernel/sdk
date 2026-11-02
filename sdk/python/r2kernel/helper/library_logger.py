"""Shared logger utilities for r2kernel internals (launcher, control backend, etc.)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict

_LOGGER_CACHE: Dict[str, logging.Logger] = {}


def _find_runtime_prefix(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / "config.yaml").is_file() and (candidate / "bin").is_dir():
            return candidate
    return None


def get_library_log_path() -> Path:
    """
    Return the path to the shared r2kernel library log file.

    Resolution order:
    1. `R2KERNEL_LOG_DIR`
    2. installed runtime prefix `<prefix>/log`
    3. user-local fallback `~/.r2kernel/log`
    """
    override = os.environ.get("R2KERNEL_LOG_DIR")
    if override:
        log_dir = Path(override).expanduser()
    else:
        runtime_prefix = _find_runtime_prefix(Path(__file__).resolve())
        if runtime_prefix is not None:
            log_dir = runtime_prefix / "log"
        else:
            log_dir = Path.home() / ".r2kernel" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "r2kernel.log"


def get_library_logger(name: str = "r2kernel", level: int | str = logging.INFO) -> logging.Logger:
    """
    Return a shared logger for r2kernel internals.

    Logs are written to the resolved R2Kernel log path and also emitted to stderr.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if name in _LOGGER_CACHE:
        logger = _LOGGER_CACHE[name]
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)
        return logger

    log_file = get_library_log_path()

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    _LOGGER_CACHE[name] = logger
    return logger
