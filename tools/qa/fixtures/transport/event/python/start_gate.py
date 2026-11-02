"""Startup-only Event fixture gate; never removes or reuses a caller's path.

Implements the StartGate v1 contract (fixture README) shared with
cpp/start_gate.hpp: the run directory is pinned at construction, every
create/open/stat is relative to the pinned directory, every created file is
checked through its descriptor, and the run directory identity is rechecked
after each create. Errors carry the contract code in ``StartGateError.code``.
"""
from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import Callable

ACK_CONTENT_VERSION = "r2kernel-event-start-v1"
_MAX_ACK_BYTES = 1024


class StartGateError(ValueError):
    """A StartGate v1 refusal; ``code`` is the contract error class."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_normal_absolute(raw: str) -> bool:
    """True for an absolute path without empty (``//``), ``.`` or trailing-slash components."""
    return raw.startswith("/") and all(part not in ("", ".") for part in raw.split("/")[1:])


class StartGate:
    def __init__(self, path: str | Path):
        raw = os.fspath(path)
        self.path = Path(raw)
        self._directory = self.path.parent
        self._dir_fd: int | None = None
        self._ack_fd: int | None = None
        self._after_ack_open: Callable[[], None] | None = None
        self._after_ack_create: Callable[[], None] | None = None
        if not _is_normal_absolute(raw) or self.path.name in {"", ".", ".."}:
            raise StartGateError("invalid_path", "start gate requires an absolute path in a fresh private run directory")
        self._name = self.path.name
        self._ack_name = self._name + ".ack"
        self._ack_path = self.path.with_name(self._ack_name)
        parent = self._check_parent()
        try:
            dir_fd = os.open(self._directory, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise StartGateError("parent_unsafe", "start gate parent must be a real owner-only directory (0700)") from exc
        pinned = os.fstat(dir_fd)
        if (pinned.st_dev, pinned.st_ino) != (parent.st_dev, parent.st_ino):
            os.close(dir_fd)
            raise StartGateError("directory_changed", "start gate run directory changed")
        self._dir_fd = dir_fd
        self._identity = (pinned.st_dev, pinned.st_ino)
        try:
            os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            pass
        self.close()
        raise StartGateError("gate_exists", "start gate already exists or is inaccessible; use a fresh private run directory")

    def _check_parent(self) -> os.stat_result:
        try:
            metadata = self._directory.lstat()
            resolved = self._directory.resolve(strict=True)
        except OSError as exc:
            raise StartGateError("parent_unsafe", "start gate parent must be a real owner-only directory (0700)") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or resolved != self._directory
        ):
            raise StartGateError("parent_unsafe", "start gate parent must be a real owner-only directory (0700)")
        return metadata

    def _check_identity(self) -> None:
        if self._dir_fd is None:
            raise StartGateError("gate_closed", "start gate was closed; construct a new gate")
        current = self._check_parent()
        if (current.st_dev, current.st_ino) != self._identity:
            raise StartGateError("directory_changed", "start gate run directory changed")

    def _stat_entry(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StartGateError("gate_inaccessible", "start gate inaccessible") from exc

    @staticmethod
    def _require_private_file(fd: int, code: str, message: str, *, empty: bool = False) -> os.stat_result:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or (empty and metadata.st_size != 0)
        ):
            raise StartGateError(code, message)
        return metadata

    @staticmethod
    def _ack_content(session: str, publisher_name: str) -> bytes:
        if not all(value and len(value) <= 256 and all(
            char.isascii() and (char.isalnum() or char in "_.-") for char in value
        ) for value in (session, publisher_name)):
            raise StartGateError(
                "invalid_identity",
                "start gate requires a unique session and publisher name (1-256 ASCII letters, digits, _, . or -)",
            )
        return f"{ACK_CONTENT_VERSION}\n{session}\n{publisher_name}\n".encode("ascii")

    def set_after_ack_open_for_test(self, hook: Callable[[], None] | None) -> None:
        """Test-only: runs right after the exclusive create, before the acknowledgement is locked."""
        self._after_ack_open = hook

    def set_after_ack_create_for_test(self, hook: Callable[[], None] | None) -> None:
        """Test-only: runs after the acknowledgement is written, before the final recheck."""
        self._after_ack_create = hook

    def acknowledge(self, session: str, publisher_name: str) -> None:
        """Publisher side, after absent-gate validation and before any registration."""
        content = self._ack_content(session, publisher_name)
        self._check_identity()
        if self._stat_entry(self._name) is not None:
            raise StartGateError("gate_exists", "start gate already exists; use a fresh private run directory")
        try:
            fd = os.open(self._ack_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self._dir_fd)
        except OSError as exc:
            raise StartGateError("ack_exists", "start gate acknowledgement already exists or is inaccessible") from exc
        try:
            if self._after_ack_open is not None:
                self._after_ack_open()
            # Locked first, before any other check or the content: a helper treats the
            # acknowledgement as live only once it has content, which follows the lock.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StartGateError("ack_write_failed", "start gate acknowledgement could not be locked") from exc
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise StartGateError("ack_unsafe", "start gate acknowledgement mode could not be set") from exc
            self._require_private_file(
                fd, "ack_unsafe", "start gate acknowledgement must be a private regular file (0600), not a link")
            view = memoryview(content)
            while view:
                try:
                    written = os.write(fd, view)
                except InterruptedError:
                    continue
                except OSError as exc:
                    raise StartGateError("ack_write_failed",
                                         "start gate acknowledgement could not be written safely") from exc
                if written <= 0:
                    raise StartGateError("ack_write_failed", "start gate acknowledgement could not be written safely")
                view = view[written:]
            if self._after_ack_create is not None:
                self._after_ack_create()
            self._check_identity()
        except BaseException:
            os.close(fd)
            raise
        self._ack_fd = fd

    def close(self) -> None:
        if self._ack_fd is not None:
            os.close(self._ack_fd)
            self._ack_fd = None
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None

    def __del__(self) -> None:
        if getattr(self, "_ack_fd", None) is not None or getattr(self, "_dir_fd", None) is not None:
            self.close()

    def acknowledged(self, session: str, publisher_name: str) -> bool:
        """Helper side: True while the named current-run publisher holds its acknowledgement."""
        expected = self._ack_content(session, publisher_name)
        self._check_identity()
        try:
            fd = os.open(self._ack_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=self._dir_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StartGateError("ack_unsafe",
                                 "start gate acknowledgement must be a private regular file (0600), not a link") from exc
        with os.fdopen(fd, "rb") as source:
            metadata = self._require_private_file(
                fd, "ack_unsafe", "start gate acknowledgement must be a private regular file (0600), not a link")
            entry = self._stat_entry(self._ack_name)
            if metadata.st_size > _MAX_ACK_BYTES or entry is None or (entry.st_dev, entry.st_ino) != (
                metadata.st_dev, metadata.st_ino
            ):
                raise StartGateError("ack_unsafe",
                                     "start gate acknowledgement must be a private regular file (0600), not a link")
            if metadata.st_size == 0:
                # Created but not written yet: the publisher writes only while it holds the
                # lock, so an empty acknowledgement is never live and is never lock-probed
                # (a probe could take the lock before the publisher does).
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The current publisher retains the exclusive lock until it exits.
                return source.read(_MAX_ACK_BYTES + 1) == expected
            raise StartGateError("publisher_exited", "start gate publisher exited before gate authorization")

    def opened(self) -> bool:
        """Publisher side: True once the helper created an empty private gate."""
        self._check_identity()
        metadata = self._stat_entry(self._name)
        if metadata is None:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != 0
            or metadata.st_nlink != 1
        ):
            raise StartGateError("gate_unsafe", "start gate must be an empty owner-only regular file (0600), not a link")
        return True

    def open(self, session: str, publisher_name: str) -> None:
        """Helper side: creates the empty gate after a matching live acknowledgement."""
        if not self.acknowledged(session, publisher_name):
            raise StartGateError("not_acknowledged",
                                 "start gate has no matching current-run publisher acknowledgement")
        try:
            fd = os.open(self._name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self._dir_fd)
        except FileExistsError as exc:
            raise StartGateError("gate_exists", "start gate already exists; use a fresh private run directory") from exc
        except OSError as exc:
            raise StartGateError("gate_inaccessible", "start gate inaccessible") from exc
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise StartGateError("gate_unsafe", "start gate mode could not be set") from exc
            self._require_private_file(
                fd, "gate_unsafe", "start gate must be an empty owner-only regular file (0600), not a link",
                empty=True)
            self._check_identity()
        finally:
            os.close(fd)
