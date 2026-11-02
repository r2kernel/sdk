"""Diagnostics for packages started with a brokered network/CAN sandbox.

The launcher exports ``R2KERNEL_SANDBOX_NETWORK=brokered`` (and ``R2KERNEL_SANDBOX_CAN=brokered``) when the
package's seccomp filter denies creating AF_INET/AF_INET6 (AF_CAN) sockets with EPERM. The filter is inherited
by every process the package starts. These helpers run only on error paths: they probe whether this process
is actually denied and describe the supported alternatives. Local Control/Event (AF_UNIX) is not affected.
"""
from __future__ import annotations

import errno
import os
import socket

SANDBOX_NETWORK_ENV = "R2KERNEL_SANDBOX_NETWORK"
SANDBOX_CAN_ENV = "R2KERNEL_SANDBOX_CAN"
SANDBOX_BROKERED = "brokered"
_AF_CAN = getattr(socket, "AF_CAN", 29)
_CAN_RAW = getattr(socket, "CAN_RAW", 1)
_SOCKET_MODULE_FILE = os.path.abspath(socket.__file__)


def _socket_denied(family: int, sock_type: int, proto: int = 0) -> bool:
    try:
        probe = socket.socket(family, sock_type, proto)
    except OSError as exc:
        return exc.errno == errno.EPERM
    probe.close()
    return False


def sandbox_socket_denials() -> list[str]:
    """Describe the socket families this process is denied by a brokered sandbox (empty when none)."""
    messages: list[str] = []
    if os.environ.get(SANDBOX_NETWORK_ENV) == SANDBOX_BROKERED:
        denied = [name for name, family in (("AF_INET", socket.AF_INET), ("AF_INET6", socket.AF_INET6))
                  if _socket_denied(family, socket.SOCK_DGRAM)]
        if denied:
            messages.append(
                f"socket({'/'.join(denied)}) denied by resources.sandbox.network=brokered; request a brokered "
                "socket (ResourceBrokerClient.open_tcp_socket, open_udp_socket or open_frontend_tcp_listener) "
                "or use the leader pass_fds pattern (R2KERNEL_INHERITED_FDS)"
            )
    if os.environ.get(SANDBOX_CAN_ENV) == SANDBOX_BROKERED and _socket_denied(_AF_CAN, socket.SOCK_RAW, _CAN_RAW):
        messages.append(
            "socket(AF_CAN) denied by resources.sandbox.can=brokered; request a brokered socket "
            "(ResourceBrokerClient.open_can_socket) or use the leader pass_fds pattern (R2KERNEL_INHERITED_FDS)"
        )
    return messages


def _raised_by_socket_creation(exc: BaseException) -> bool:
    """True when ``exc`` was raised by the ``socket.socket`` constructor (the socket() system call)."""
    frame = exc.__traceback__
    if frame is None:
        return False
    while frame.tb_next is not None:
        frame = frame.tb_next
    code = frame.tb_frame.f_code
    return code.co_name == "__init__" and os.path.abspath(code.co_filename) == _SOCKET_MODULE_FILE


def _has_socket_eperm(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, OSError) and exc.errno == errno.EPERM and _raised_by_socket_creation(exc):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def explain_permission_error(exc: BaseException) -> str | None:
    """Return the sandbox explanation when ``exc`` (or its cause chain) is an EPERM raised by creating a
    socket (``socket.socket``) and a brokered sandbox denies sockets in this process; otherwise None.
    EPERM from any other call (for example ``os.sched_setscheduler``) is not explained."""
    if not _has_socket_eperm(exc):
        return None
    messages = sandbox_socket_denials()
    return "; ".join(messages) if messages else None
