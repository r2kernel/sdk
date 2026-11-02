"""Keeps a running subsystem registered with Control across a Control restart.

Every heartbeat sends the subsystem status and waits for Control's answer. When Control does not accept it
(Control unreachable, restarted, or no longer holding this registration), the subsystem registers again
through the normal registration request with its unchanged session, PID, resource lease and notify
endpoint, publishes its full status (manifest, provides, requires, endpoints), and requests its Event
publications, Event subscriptions and Control handler again. Control verifies the lease with the resource
broker, so a process that is not a member of its lease is refused.

Attempts repeat with a bounded backoff: the first attempt runs at the heartbeat that found the failure; after a
failed attempt the next one is due RETRY_INITIAL_S later, doubling up to RETRY_MAX_S, and runs in the first
heartbeat after that time. A heartbeat follows HEARTBEAT_PERIOD_S after the previous one finished, and while the
registration is lost a heartbeat that is not due does no I/O, so an attempt starts at most
REREGISTRATION_START_BOUND_S after the previous attempt failed. Every failed attempt is logged with its reason;
the subsystem keeps trying while it runs.
"""
from __future__ import annotations

import time
from typing import Any, Callable

HEARTBEAT_PERIOD_S = 1.0
RETRY_INITIAL_S = 1.0
RETRY_MAX_S = 4.0
REREGISTRATION_START_BOUND_S = RETRY_MAX_S + HEARTBEAT_PERIOD_S

LOG_ID = "Heartbeat"


def status_refusal(response: Any) -> str | None:
    """None when Control accepted a status update, otherwise the reason it did not."""
    if isinstance(response, dict) and response.get("ok") is True and response.get("res") == "ok":
        return None
    if isinstance(response, dict) and response.get("error"):
        return f"Control refused the status: {response['error']}"
    return f"Control returned no status acknowledgement: {response!r}"


class ControlRegistrationKeeper:
    """Heartbeat outcome tracking and bounded re-registration backoff (no I/O of its own)."""

    def __init__(self, *, logger: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._logger = logger
        self._clock = clock
        self._lost_since: float | None = None
        self._attempts = 0
        self._retry_s = RETRY_INITIAL_S
        self._next_attempt_at = 0.0

    @property
    def registration_lost(self) -> bool:
        return self._lost_since is not None

    def heartbeat(self, send_status: Callable[[], str | None], reregister: Callable[[], str | None]) -> None:
        """One heartbeat. Each callable returns None on success or the failure reason."""
        now = self._clock()
        if self._lost_since is None:
            failure = send_status()
            if failure is None:
                return
            self._lost_since = now
            self._attempts = 0
            self._retry_s = RETRY_INITIAL_S
            self._logger.error(LOG_ID, f"Heartbeat not accepted ({failure}); registering with Control again")
        elif now < self._next_attempt_at:
            return
        self._attempts += 1
        failure = reregister()
        finished = self._clock()
        elapsed = finished - self._lost_since
        if failure is None:
            self._logger.info(
                LOG_ID,
                f"Registered with Control again after {elapsed:.1f}s ({self._attempts} attempt(s))",
            )
            self._lost_since = None
            return
        delay = self._retry_s
        self._next_attempt_at = finished + delay
        self._retry_s = min(self._retry_s * 2.0, RETRY_MAX_S)
        self._logger.error(
            LOG_ID,
            f"Registration attempt {self._attempts} failed {elapsed:.1f}s after the registration was lost: "
            f"{failure}; next attempt in {delay:.0f}s",
        )
