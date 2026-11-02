from __future__ import annotations

import datetime
import os
import queue
import sys
import threading
from abc import ABC, abstractmethod
from typing import Any

try:
    import picologging as logging
    from picologging.handlers import RotatingFileHandler, QueueHandler, QueueListener
except ImportError:  # pragma: no cover
    import logging  # type: ignore
    from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener  # type: ignore


_LEVEL_MAP = {
    "TRACE": 0,
    "DEBUG": 0,
    "INFO": 1,
    "WARN": 2,
    "WARNING": 2,
    "ERROR": 3,
    "FATAL": 4,
    "CRITICAL": 4,
}

_STDLIB_LOG_KWARGS = {"exc_info", "stack_info", "stacklevel", "extra"}


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_level(level: str) -> str:
    normalized = str(level or "INFO").strip().upper()
    if normalized == "WARNING":
        return "WARN"
    if normalized == "CRITICAL":
        return "FATAL"
    if normalized in _LEVEL_MAP:
        return normalized
    return "INFO"


def _format_stdlib_message(message: Any, args: tuple[Any, ...]) -> str:
    text = _coerce_text(message)
    if not args:
        return text
    try:
        return text % args
    except Exception:
        return " ".join([text, *(_coerce_text(arg) for arg in args)]).strip()


def _normalize_log_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    local_kwargs = dict(kwargs)
    logging_kwargs = {key: local_kwargs.pop(key) for key in list(local_kwargs) if key in _STDLIB_LOG_KWARGS}

    if "subject" in local_kwargs or "message" in local_kwargs:
        subject = _coerce_text(local_kwargs.pop("subject", ""))
        message = _coerce_text(local_kwargs.pop("message", ""))
        return subject, message, logging_kwargs

    if not args:
        return "", "", logging_kwargs

    if len(args) == 1:
        return "", _coerce_text(args[0]), logging_kwargs

    if len(args) == 2 and isinstance(args[0], str) and "%" not in args[0]:
        return _coerce_text(args[0]), _coerce_text(args[1]), logging_kwargs

    return "", _format_stdlib_message(args[0], tuple(args[1:])), logging_kwargs


def _compose_log_line(subject: str, message: str) -> str:
    now = datetime.datetime.now()
    ts = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"
    return f"{ts} [{subject}] {message}" if subject else f"{ts} {message}"


class _OwnedQueueHandler(QueueHandler):
    """Stop accepting records before the owning listener is drained and stopped."""

    def __init__(self, record_queue: queue.Queue) -> None:
        super().__init__(record_queue)
        self._enqueue_lock = threading.Lock()
        self._accepting = True

    def enqueue(self, record) -> None:
        with self._enqueue_lock:
            if self._accepting:
                super().enqueue(record)

    def close(self) -> None:
        with self._enqueue_lock:
            self._accepting = False
        super().close()


class Logger:
    """Lightweight logger using a queue-based handler/listener for MP safety."""

    def __init__(
        self,
        *,
        name: str,
        level: str = "INFO",
        flag_use_console: bool = True,
        flag_use_file: bool = True,
        log_file_folder: str = "./",
        log_file_max_size: int = 1024 * 50,
        log_file_max_count: int = 1,
    ) -> None:
        self._logger = logging.getLogger(name)
        self._logger.propagate = False

        self._use_console = flag_use_console
        self._use_file = flag_use_file
        self._level_name = _normalize_level(level)
        self._level = _LEVEL_MAP[self._level_name]

        self._log_file_folder = log_file_folder
        self._log_file_name = f"{name}.log"
        self._log_file_max_size = log_file_max_size
        self._log_file_max_count = log_file_max_count

        self._error_sequence = 0
        self._last_error = ""
        # Per-thread record of the last warning or error, as the C++ SDK keeps it: a lifecycle hook reports
        # only what its own thread logged, never another thread's problem.
        self._problems = threading.local()
        self._queue: queue.Queue = queue.Queue(-1)
        self._handlers = []
        self._listener: QueueListener | None = None
        self._queue_handler: _OwnedQueueHandler | None = None
        self._close_lock = threading.RLock()
        self._closed = False

        self._setup_handlers()

    def _setup_handlers(self):
        # Configure destination handlers.
        if self._use_file:
            if not os.path.exists(self._log_file_folder):
                try:
                    os.makedirs(self._log_file_folder)
                except OSError as e:
                    print(f"Failed to create log folder {self._log_file_folder}: {e}")
                    self._use_file = False
            if self._use_file:
                file_handler = RotatingFileHandler(
                    os.path.join(self._log_file_folder, self._log_file_name),
                    maxBytes=self._log_file_max_size,
                    backupCount=self._log_file_max_count,
                )
                self._handlers.append(file_handler)

        # if self._use_console:
        #     console_handler = logging.StreamHandler(sys.stdout)
        #     self._handlers.append(console_handler)

        # Base logger level low; handlers enforce effective level.
        self._logger.setLevel(logging.DEBUG)
        self._queue_handler = _OwnedQueueHandler(self._queue)
        self._logger.addHandler(self._queue_handler)

        for h in self._handlers:
            if self._level <= _LEVEL_MAP["DEBUG"]:
                h.setLevel(logging.DEBUG)
            elif self._level <= _LEVEL_MAP["INFO"]:
                h.setLevel(logging.INFO)
            elif self._level <= _LEVEL_MAP["WARNING"]:
                h.setLevel(logging.WARNING)
            else:
                h.setLevel(logging.ERROR)

        self._listener = QueueListener(self._queue, *self._handlers, respect_handler_level=True)
        self._listener.start()

    def _log(self, level_name: str, *args: Any, **kwargs: Any):
        subject, message, logging_kwargs = _normalize_log_call(args, kwargs)
        full = _compose_log_line(subject, message)
        if level_name == "DEBUG":
            lvl = logging.DEBUG
        elif level_name == "INFO":
            lvl = logging.INFO
        elif level_name == "WARNING":
            lvl = logging.WARNING
        else:
            lvl = logging.ERROR

        with self._close_lock:
            if self._closed:
                return
            if self._use_console:
                sys.stdout.write(full + "\n")
                sys.stdout.flush()

            self._logger.log(lvl, full, **logging_kwargs)

    def debug(self, *args: Any, **kwargs: Any):
        if self._level <= _LEVEL_MAP["DEBUG"]:
            self._log("DEBUG", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any):
        if self._level <= _LEVEL_MAP["INFO"]:
            self._log("INFO", *args, **kwargs)

    def _record_problem(self, args: tuple, kwargs: dict) -> str:
        subject, message, _ = _normalize_log_call(args, kwargs)
        text = f"[{subject}] {message}" if subject else message
        problems = self._problems
        problems.last = text
        problems.sequence = getattr(problems, "sequence", 0) + 1
        return text

    def warning(self, *args: Any, **kwargs: Any):
        self._record_problem(args, kwargs)
        if self._level <= _LEVEL_MAP["WARNING"]:
            self._log("WARNING", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any):
        # Remembered for lifecycle diagnostics (a hook that returns False reports the SDK error behind it).
        self._last_error = self._record_problem(args, kwargs)
        self._error_sequence += 1
        if self._level <= _LEVEL_MAP["ERROR"]:
            self._log("ERROR", *args, **kwargs)

    @property
    def error_sequence(self) -> int:
        """Number of error records logged so far (including ones below the configured level)."""
        return self._error_sequence

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def problem_sequence(self) -> int:
        """Number of warning and error records logged so far by the calling thread (any level filter)."""
        return getattr(self._problems, "sequence", 0)

    @property
    def last_problem(self) -> str:
        """The last warning or error logged by the calling thread, or an empty string."""
        return getattr(self._problems, "last", "")

    def close(self):
        """Drain accepted records and release only this instance's logging resources."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._queue_handler is not None:
                self._logger.removeHandler(self._queue_handler)
                # A named logger may already have selected this handler on another
                # thread. Disable enqueue before QueueListener.stop adds its sentinel.
                self._queue_handler.close()
            listener, self._listener = self._listener, None
            try:
                if listener is not None:
                    listener.stop()
            finally:
                for handler in self._handlers:
                    handler.close()


class CompatibleLogger:
    """Adapter that lets stdlib loggers accept the project's two-arg log style."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def __getattr__(self, item: str) -> Any:
        return getattr(self._logger, item)

    def _log(self, level_name: str, *args: Any, **kwargs: Any) -> None:
        subject, message, logging_kwargs = _normalize_log_call(args, kwargs)
        log_method = getattr(self._logger, level_name.lower())
        if subject:
            log_method(f"[{subject}] {message}", **logging_kwargs)
            return
        log_method(message, **logging_kwargs)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._log("DEBUG", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._log("INFO", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._log("WARNING", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._log("ERROR", *args, **kwargs)


def coerce_logger(logger: Any | None = None, *, default_name: str = "r2kernel_client") -> Logger | CompatibleLogger:
    if isinstance(logger, (Logger, CompatibleLogger)):
        return logger
    if logger is None:
        logger = logging.getLogger(default_name)
    return CompatibleLogger(logger)


class Loggable(ABC):
    @abstractmethod
    def logger(self) -> Any:
        ...
