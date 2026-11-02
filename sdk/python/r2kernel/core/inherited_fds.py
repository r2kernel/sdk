"""Brokered sockets inherited from a package leader (R2KERNEL_INHERITED_FDS).

A package leader that received brokered sockets from r2kernel-resourced may pass them to its own child
processes. The leader encodes the mapping with :func:`encode_inherited_fds`, puts the value in the child's
``R2KERNEL_INHERITED_FDS`` environment variable and passes the descriptors itself, for example::

    mapping = {"frontend.api": listener.fileno()}
    env = {**os.environ, INHERITED_FDS_ENV: encode_inherited_fds(mapping)}
    subprocess.Popen(argv, env=env, pass_fds=tuple(mapping.values()))

The child validates each descriptor with :func:`inherited_socket` before use. There is no broker fallback:
a missing or mismatching descriptor is an :class:`InheritedFdError`.
"""
from __future__ import annotations

import json
import os
import re
import socket
import stat
from collections.abc import Mapping
from typing import Any

INHERITED_FDS_ENV = "R2KERNEL_INHERITED_FDS"
_KIND_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MIN_INHERITED_FD = 3
_MAX_INHERITED_FD = 2**31 - 1  # a C int
_FAMILIES = {"inet": socket.AF_INET, "inet6": socket.AF_INET6, "can": getattr(socket, "AF_CAN", 29)}
_TYPES = {"stream": socket.SOCK_STREAM, "dgram": socket.SOCK_DGRAM, "raw": socket.SOCK_RAW}
_SO_DOMAIN = getattr(socket, "SO_DOMAIN", 39)
_SO_ACCEPTCONN = getattr(socket, "SO_ACCEPTCONN", 30)


class InheritedFdError(RuntimeError):
    """An inherited descriptor failed validation; ``step`` names the failed check."""

    def __init__(self, step: str, kind: str, detail: str):
        super().__init__(f"inherited fd {kind}: {step}: {detail}")
        self.step = step
        self.kind = kind
        self.detail = detail


def _validated_mapping(mapping: Any, kind_for_errors: str) -> dict[str, int]:
    if not isinstance(mapping, Mapping):
        raise InheritedFdError("env_invalid", kind_for_errors, "value must be a JSON object of kind to fd")
    seen: dict[int, str] = {}
    result: dict[str, int] = {}
    for kind, fd in mapping.items():
        if not isinstance(kind, str) or not _KIND_PATTERN.fullmatch(kind):
            raise InheritedFdError("env_invalid", kind_for_errors, f"invalid kind {kind!r}")
        # bool is an int subclass (JSON true/false); floats and out-of-range values are not descriptors.
        if isinstance(fd, bool) or not isinstance(fd, int) or not _MIN_INHERITED_FD <= fd <= _MAX_INHERITED_FD:
            raise InheritedFdError("env_invalid", kind_for_errors,
                                   f"kind {kind!r} has fd {fd!r}; an integer in {_MIN_INHERITED_FD}.."
                                   f"{_MAX_INHERITED_FD} is required")
        if fd in seen:
            raise InheritedFdError("env_invalid", kind_for_errors, f"kinds {seen[fd]!r} and {kind!r} name the same fd {fd}")
        seen[fd] = kind
        result[kind] = fd
    return result


def encode_inherited_fds(mapping: Mapping[str, int]) -> str:
    """Validate ``{kind: fd}`` and return the ``R2KERNEL_INHERITED_FDS`` value; it does not spawn or pass fds."""
    return json.dumps(_validated_mapping(mapping, "<encode>"), sort_keys=True, separators=(",", ":"))


def _expectation(expect: Mapping[str, Any]) -> tuple[int, int, bool, int | None]:
    if not isinstance(expect, Mapping):
        raise ValueError("expect must be a mapping with family, type and listening")
    unknown = set(expect) - {"family", "type", "listening", "port"}
    if unknown:
        raise ValueError(f"unknown expect field(s): {', '.join(sorted(unknown))}")
    family_name, type_name, listening = expect.get("family"), expect.get("type"), expect.get("listening")
    if family_name not in _FAMILIES:
        raise ValueError(f"expect.family must be one of {sorted(_FAMILIES)}")
    if type_name not in _TYPES:
        raise ValueError(f"expect.type must be one of {sorted(_TYPES)}")
    if not isinstance(listening, bool):
        raise ValueError("expect.listening must be a bool")
    if listening and type_name != "stream":
        raise ValueError("expect.listening may be true only for type=stream")
    port = expect.get("port")
    if port is not None:
        if family_name not in ("inet", "inet6"):
            raise ValueError("expect.port applies only to family inet or inet6")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 0xFFFF:
            raise ValueError("expect.port must be an integer in 0..65535")
    return _FAMILIES[family_name], _TYPES[type_name], listening, port


def inherited_socket(kind: str, expect: Mapping[str, Any]) -> int:
    """Return the validated inherited socket fd for ``kind``; ownership passes to the caller.

    ``expect``: ``family`` (inet|inet6|can), ``type`` (stream|dgram|raw), ``listening`` (bool) and optional
    ``port`` (inet/inet6). Every call validates again. On failure the fd is neither closed nor modified; on
    success FD_CLOEXEC is set so the fd does not leak further unless the caller passes it on explicitly.
    """
    family, sock_type, listening, port = _expectation(expect)
    raw = os.environ.get(INHERITED_FDS_ENV)
    if raw is None:
        raise InheritedFdError("env_missing", kind, f"{INHERITED_FDS_ENV} is not set")
    try:
        decoded = json.loads(raw)  # a duplicate key keeps its last value, as in the C++ helper
    except ValueError as exc:
        raise InheritedFdError("env_invalid", kind, f"{INHERITED_FDS_ENV} is not valid JSON ({exc})") from None
    mapping = _validated_mapping(decoded, kind)
    if kind not in mapping:
        raise InheritedFdError("kind_missing", kind, f"{INHERITED_FDS_ENV} has no entry for this kind")
    fd = mapping[kind]
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        raise InheritedFdError("not_socket", kind, f"fstat(fd {fd}) failed: {exc.strerror}") from None
    if not stat.S_ISSOCK(mode):
        raise InheritedFdError("not_socket", kind, f"fd {fd} is not a socket")
    probe = socket.socket(fileno=os.dup(fd))  # inspect a duplicate; the inherited fd itself stays untouched
    try:
        actual_family = probe.getsockopt(socket.SOL_SOCKET, _SO_DOMAIN)
        if actual_family != family:
            raise InheritedFdError("family_mismatch", kind, f"fd {fd} has address family {actual_family}, expected {family}")
        actual_type = probe.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        if actual_type != sock_type:
            raise InheritedFdError("type_mismatch", kind, f"fd {fd} has socket type {actual_type}, expected {sock_type}")
        actual_listening = bool(probe.getsockopt(socket.SOL_SOCKET, _SO_ACCEPTCONN))
        if actual_listening != listening:
            raise InheritedFdError("listening_mismatch", kind,
                                   f"fd {fd} is {'' if actual_listening else 'not '}listening, expected "
                                   f"{'listening' if listening else 'not listening'}")
        if port is not None:
            actual_port = probe.getsockname()[1]
            if actual_port != port:
                raise InheritedFdError("port_mismatch", kind, f"fd {fd} is bound to port {actual_port}, expected {port}")
    finally:
        probe.close()
    os.set_inheritable(fd, False)
    return fd
