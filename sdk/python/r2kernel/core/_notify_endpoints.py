"""Lifetime of the per-process Control notification sockets (``r2kernel-notify-<pid>-<id>.sock``).

The local Control handler socket (``r2kernel-control-handler-<name>-<pid>.sock``) is registered here too, so the
exit and ``SIGTERM`` rules below apply to it; the stale sweep covers notify endpoints only.

The socket file of a process that ends must not stay behind:

* a process that exits normally (including an unhandled ``KeyboardInterrupt``) removes its endpoints at exit;
* a process that receives ``SIGTERM`` while ``SIGTERM`` still has its default action removes them and then
  terminates by ``SIGTERM`` as before (the SDK installs that handler only when the main thread creates the
  first endpoint and no other handler is installed);
* endpoints of processes that ended without either (``SIGKILL``, crash) are removed by the next client that
  creates an endpoint in the same directory: an endpoint is stale when its PID is not running and nothing
  accepts connections on it.

A process removes only the endpoints it created. A forked child (``os.fork``, ``multiprocessing`` with the
``fork`` start method) inherits none of its parent's endpoints and gets back the ``SIGTERM`` disposition the
SDK replaced, so its exit or termination leaves the parent's sockets in place.

Nothing here runs per message.
"""
from __future__ import annotations

import atexit
import errno
import os
import re
import signal
import socket
import threading
from typing import Any

_ENDPOINT_NAME = re.compile(r"^r2kernel-notify-(\d+)-[0-9a-f]+\.sock$")
_LOG_ID = "NotifyEndpoint"

# Reentrant: the SIGTERM handler runs on the main thread, possibly while it holds the lock.
_lock = threading.RLock()
# Endpoint path -> PID of the process that created it.
_endpoints: dict[str, int] = {}
_atexit_installed = False
_sigterm_installed = False


def endpoint_name(pid: int, token: str) -> str:
    return f"r2kernel-notify-{pid}-{token}.sock"


def register(path: str) -> None:
    """Remove ``path`` when this process exits or is terminated by ``SIGTERM``."""
    global _atexit_installed, _sigterm_installed
    # Only the main thread can install the SIGTERM handler; a registration from another thread leaves it to the
    # next main-thread registration.
    on_main_thread = threading.current_thread() is threading.main_thread()
    with _lock:
        _endpoints[path] = os.getpid()
        install_atexit = not _atexit_installed
        install_sigterm = not _sigterm_installed and on_main_thread
        _atexit_installed = True
        _sigterm_installed = _sigterm_installed or on_main_thread
    if install_atexit:
        atexit.register(_remove_registered)
    if install_sigterm:
        _install_sigterm_cleanup()


def unregister(path: str) -> bool:
    """Forget ``path``. Returns whether this process created it (and so may remove the file)."""
    with _lock:
        return _endpoints.pop(path, None) == os.getpid()


def _remove_registered() -> None:
    pid = os.getpid()
    with _lock:
        paths = [path for path, owner in _endpoints.items() if owner == pid]
        _endpoints.clear()
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _install_sigterm_cleanup() -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
            return
        signal.signal(signal.SIGTERM, _terminate_after_cleanup)
    except (ValueError, OSError):
        return


def _terminate_after_cleanup(signum: int, _frame: Any) -> None:
    _remove_registered()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _after_fork_in_child() -> None:
    """A forked child owns none of its parent's endpoints and gets back the SIGTERM default the SDK replaced."""
    global _lock, _sigterm_installed
    _lock = threading.RLock()  # another parent thread may have held it at the fork
    _endpoints.clear()
    _sigterm_installed = False
    try:
        if signal.getsignal(signal.SIGTERM) is _terminate_after_cleanup:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except (ValueError, OSError):
        pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def remove_stale(directory: str, logger: Any) -> int:
    """Remove notify endpoints in ``directory`` left by processes that no longer run. Returns the count."""
    try:
        names = os.listdir(directory)
    except OSError as error:
        logger.warning(_LOG_ID, f"Could not list {directory} for stale notify endpoints: {error}")
        return 0
    removed = 0
    kept: list[str] = []
    for name in names:
        match = _ENDPOINT_NAME.match(name)
        if match is None or int(match.group(1)) == os.getpid() or _process_exists(int(match.group(1))):
            continue
        path = os.path.join(directory, name)
        if _accepts_connections(path):
            continue
        try:
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            kept.append(f"{name} ({error.strerror})")
    if removed:
        logger.info(_LOG_ID, f"Removed {removed} stale notify endpoint(s) of ended processes from {directory}")
    if kept:
        logger.warning(_LOG_ID, f"Could not remove stale notify endpoint(s) in {directory}: {', '.join(kept)}")
    return removed


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _accepts_connections(path: str) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.05)
        try:
            probe.connect(path)
        except OSError as error:
            return error.errno not in (errno.ECONNREFUSED, errno.ENOENT)
    return True
