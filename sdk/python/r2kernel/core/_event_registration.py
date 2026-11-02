"""EventClient control-plane registration, publish slot/subscription requests and shared-memory/lock setup."""

from __future__ import annotations

import ctypes
import os
import time
from typing import Any

from .topic_buffer import PAYLOAD_OFFSET, TopicBufferPool
from . import _keywords as KW
from .constants import LOG_ID_SHM
from .control_client import exchange_control_request
from .lock_backend import LockHandle


def _event_queue_depth_for_size(size: int, explicit_depth: int | None = None) -> int:
    if explicit_depth is not None and explicit_depth > 0:
        return max(1, int(explicit_depth))
    raw = os.getenv("R2KERNEL_EVENT_QUEUE_DEPTH")
    if raw:
        try:
            return max(1, int(raw))
        except Exception:
            pass
    if size <= 4 * 1024:
        return 64
    if size <= 64 * 1024:
        return 16
    return 4


def _shared_memory_identity(name: str) -> tuple[int, int] | None:
    """Device and inode of a POSIX shared-memory object, or None where it has no /dev/shm path."""
    try:
        status = os.stat(os.path.join("/dev/shm", str(name).lstrip("/")))
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _entry_slot_count(slot_size: int, size: int) -> int:
    return max(1, (max(0, int(size)) + PAYLOAD_OFFSET + max(1, int(slot_size)) - 1) // max(1, int(slot_size)))


# Machine-readable reasons of the registration refusals with which Control asks the client to
# register again: replacing an expired registration of the same name still waits for
# other Event resource operations, or its cleanup still runs.
REGISTRATION_RETRY_REASONS = frozenset({"registration_gate_busy", "registration_cleanup_pending"})


def registration_retry_reason(response: dict | None) -> str | None:
    """The reason of a registration refusal that asks for a retry, else None."""
    if isinstance(response, dict) and response.get(KW.RESULT) != KW.OK:
        reason = response.get("reason")
        if reason in REGISTRATION_RETRY_REASONS:
            return str(reason)
    return None


class EventPoolReplacedError(RuntimeError):
    """Control serves a different Event shared-memory pool than the one this process mapped."""


class _EventRegistrationMixin:
    """Subsystem registration, publish slot and subscription requests, shared-memory and publish-lock setup of EventClient."""

    def _exchange_control_request(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float = 1.0,
        retries: int = 3,
        retry_delay_s: float = 0.05,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        if deadline is not None:
            return exchange_control_request(payload, timeout_s=timeout_s, retries=1, deadline=deadline)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return exchange_control_request(
                    payload,
                    timeout_s=timeout_s,
                    retries=1,
                    retry_delay_s=retry_delay_s,
                )
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(max(0.0, retry_delay_s))
        if last_exc is not None:
            raise last_exc
        return None

    def _registration_payload(self) -> dict[str, Any]:
        payload = {
            KW.COMMAND: KW.REGISTER_SUBSYSTEM,
            KW.SUBSYSTEM_NAME: self._subsystem_name,
            "session_id": self._session_id,
            "pid": self._pid,
        }
        if self._notify_endpoint:
            payload["notify_endpoint"] = self._notify_endpoint
        if self._lease_id:
            payload["lease_id"] = self._lease_id
        return payload

    def _accept_registration_response(self, response: dict | None) -> None:
        self._apply_startup_barrier_config(response)
        if isinstance(response, dict) and isinstance(response.get("manager_peer_auth_required"), bool):
            from .resource import managed_peer_authentication_required
            self._manager_peer_auth_required = managed_peer_authentication_required()
            if self._manager_peer_auth_required and not response["manager_peer_auth_required"]:
                raise RuntimeError("Production SDK cannot accept disabled manager authentication")
            self._manager_peer_auth_socket = str(response.get("resource_service_socket") or "")
        self._last_registration_response = response

    def register_subsystem(self) -> dict | None:
        """The first registration of this client; returns Control's reply or None after a failure.

        A transport failure, or a refusal with which Control asks for a retry
        (``REGISTRATION_RETRY_REASONS``), is tried again: at most ``R2KERNEL_STARTUP_RETRY_COUNT``
        attempts in total, ``R2KERNEL_STARTUP_RETRY_DELAY_MS`` apart. Any other refusal is returned
        at once.
        """
        payload = self._registration_payload()
        attempts = self._startup_retry_count
        try:
            for attempt in range(1, attempts + 1):
                try:
                    response = self._exchange_control_request(
                        payload, timeout_s=self._startup_request_timeout_s, retries=1,
                    )
                except Exception:
                    if attempt == attempts:
                        raise
                    time.sleep(self._startup_retry_delay_s)
                    continue
                reason = registration_retry_reason(response)
                if reason is None or attempt == attempts:
                    break
                self._logger.info(
                    LOG_ID_SHM,
                    f"Control asked to retry the registration ({reason}); attempt {attempt + 1} of {attempts} "
                    f"in {self._startup_retry_delay_s * 1000:.0f} ms",
                )
                time.sleep(self._startup_retry_delay_s)
            self._accept_registration_response(response)
            return response
        except Exception as exc:
            self._logger.error(LOG_ID_SHM, f'"{KW.REGISTER_SUBSYSTEM}" failed via control: {exc}')
            return None

    def reregister_subsystem(self) -> dict[str, Any]:
        """Register this client again, once, with the same session, PID, lease and notify endpoint.

        Raises on any refusal or transport failure. Used after Control lost the registration (for
        example after a Control restart); the caller retries with backoff.
        """
        if self._down_sent:
            raise RuntimeError("the subsystem already reported itself down; it does not register again")
        response = self._exchange_control_request(
            self._registration_payload(), timeout_s=self._startup_request_timeout_s, retries=1,
        )
        if not isinstance(response, dict) or response.get(KW.RESULT) != KW.OK:
            detail = (response or {}).get("error") if isinstance(response, dict) else None
            raise RuntimeError(f"Control refused the registration: {detail or response!r}")
        self._accept_registration_response(response)
        self._event_pool_matches_registration(response)
        return response

    def _event_pool_matches_registration(self, response: dict[str, Any]) -> None:
        """Record whether Control still serves the Event pool this process has mapped."""
        pool = self._buffer_pool
        mapped = (str(getattr(pool, "name", "")), int(getattr(pool, "slot_size", 0)), int(getattr(pool, "slot_count", 0)))
        offered = (
            str(response.get(KW.TOPIC_BUFFER_POOL_NAME) or ""),
            int(response.get(KW.TOPIC_BUFFER_POOL_SLOT_SIZE) or 0),
            int(response.get(KW.TOPIC_BUFFER_POOL_SLOT_COUNT) or 0),
        )
        identity = _shared_memory_identity(offered[0])
        if mapped != offered or (self._event_pool_identity is not None and identity != self._event_pool_identity):
            self._event_pool_replaced = True

    def inform_subsystem_down(self) -> None:
        if self._down_sent:
            return
        self._down_sent = True
        payload = {
            KW.COMMAND: "inform_subsystem_down",
            KW.SUBSYSTEM_NAME: self._subsystem_name,
            "session_id": self._session_id,
            "pid": self._pid,
        }
        if self._notify_endpoint:
            payload["notify_endpoint"] = self._notify_endpoint
        if self._lease_id:
            payload["lease_id"] = self._lease_id
        try:
            exchange_control_request(
                payload,
                timeout_s=0.2,
                retries=1,
                expect_response=False,
            )
        except Exception:
            pass

    @property
    def down_reported(self) -> bool:
        return self._down_sent

    @property
    def last_registration_response(self) -> dict[str, Any] | None:
        return self._last_registration_response

    def request_control(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float = 1.0,
        retries: int = 3,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        return self._exchange_control_request(
            payload, timeout_s=timeout_s, retries=retries,
            **({"deadline": deadline} if deadline is not None else {}),
        )

    def request_publish_slots(
        self,
        subsystem: str,
        topic: str,
        size: int,
        mode: str = "event",
        priority: int | None = None,
        queue_depth: int | None = None,
    ) -> bool:
        try:
            self._request_publish_slots(
                subsystem, topic, size, mode, priority, queue_depth, retries=self._startup_retry_count,
            )
            return True
        except Exception as exc:
            self._logger.error(LOG_ID_SHM, f'"{KW.REQUEST_PUBLISH_SLOTS}" failed via control: {exc}')
            return False

    def _request_publish_slots(
        self,
        subsystem: str,
        topic: str,
        size: int,
        mode: str,
        priority: int | None,
        queue_depth: int | None,
        *,
        retries: int,
    ) -> None:
        requested_queue_depth = _event_queue_depth_for_size(int(size), queue_depth) if mode == "event" else 1
        payload = {
            KW.COMMAND: KW.REQUEST_PUBLISH_SLOTS,
            KW.SUBSYSTEM_NAME: subsystem,
            KW.TOPIC_NAME: topic,
            KW.TOPIC_SLOT_SIZE: size,
            KW.MODE: mode,
            "session_id": self._session_id,
        }
        if priority is not None:
            payload[KW.PRIORITY] = int(priority)
        if mode == "event":
            payload[KW.QUEUE_DEPTH] = int(requested_queue_depth)
        response = self._exchange_control_request(
            payload,
            timeout_s=self._startup_request_timeout_s,
            retries=retries,
            retry_delay_s=self._startup_retry_delay_s,
        ) or {}
        self._apply_startup_barrier_config(response)
        slot_index = response.get(KW.PUBLISH_SLOT_INDEX)
        if slot_index is None:
            raise RuntimeError(f"Control returned no publish slot: {response.get('error') or response!r}")
        self._publish_slots[str(topic)] = int(slot_index)
        self._publish_queue_depths[str(topic)] = max(1, int(response.get(KW.QUEUE_DEPTH) or requested_queue_depth))
        self._publish_entry_slot_counts[str(topic)] = max(
            1,
            int(response.get(KW.ENTRY_SLOT_COUNT) or _entry_slot_count(self._buffer_pool.slot_size, int(size))),
        )
        self._topic_max_sizes[str(topic)] = int(size)
        if mode == "event":
            subs = response.get(KW.SUBSCRIBERS) or []
            locks: dict[str, LockHandle] = {}
            for info in subs:
                sub_name = info.get(KW.SUBSYSTEM_NAME) or info.get("subscriber")
                lock_name = info.get(KW.LOCK_NAME)
                if info.get("internal") is True and sub_name == "__ws_event_bridge__":
                    continue
                lock = self._open_lock(lock_name) if lock_name else None
                if sub_name and lock:
                    locks[str(sub_name)] = lock
            with self._startup_barrier_cv:
                # Replaced handles are closed under the condition the publisher posts under.
                previous = self._publish_locks.get(str(topic)) or {}
                self._publish_locks[str(topic)] = locks
                for lock in previous.values():
                    if all(lock is not kept for kept in locks.values()):
                        self._lock_backend.close(lock)
                if locks:
                    self._startup_barrier_cv.notify_all()
        self._publish_requests[str(topic)] = (subsystem, int(size), mode, priority, queue_depth)
        self._logger.debug(
            LOG_ID_SHM,
            f"Succeed to allocate publisher buffer for {topic}, slot index is {slot_index}",
        )

    def register_publish_slot(
        self,
        *,
        topic: str,
        size: int,
        slot_index: int,
        lock_infos: list[dict] | None = None,
        mode: str = "event",
        queue_depth: int = 1,
        entry_slot_count: int = 1,
    ) -> None:
        """Register a publish slot without contacting the control backend (used by the in-process stamped stream runtime)."""
        self._publish_slots[str(topic)] = int(slot_index)
        self._publish_queue_depths[str(topic)] = max(1, int(queue_depth))
        self._publish_entry_slot_counts[str(topic)] = max(1, int(entry_slot_count))
        self._topic_max_sizes[str(topic)] = int(size)
        if mode == "event" and lock_infos:
            locks: dict[str, LockHandle] = {}
            for info in lock_infos:
                sub_name = info.get(KW.SUBSYSTEM_NAME) or info.get("subscriber")
                lock_name = info.get(KW.LOCK_NAME)
                lock = self._open_lock(lock_name) if lock_name else None
                if sub_name and lock:
                    locks[str(sub_name)] = lock
            with self._startup_barrier_cv:
                self._publish_locks[str(topic)] = locks
                if locks:
                    self._startup_barrier_cv.notify_all()

    def _register_subscription(
        self,
        subscriber: str,
        topic: str,
        size: int,
        mode: str = "event",
    ):
        try:
            return self._request_subscription(subscriber, topic, size, mode, retries=self._startup_retry_count)
        except Exception as exc:
            self._logger.error(LOG_ID_SHM, f'"{KW.REQUEST_SUBSCRIBE}" failed via control: {exc}')
            return False

    def _request_subscription(self, subscriber: str, topic: str, size: int, mode: str, *, retries: int) -> bool:
        payload = {
            KW.COMMAND: KW.REQUEST_SUBSCRIBE,
            KW.SUBSYSTEM_NAME: subscriber,
            KW.TOPIC_NAME: topic,
            KW.TOPIC_SLOT_SIZE: size,
            KW.MODE: mode,
            "session_id": self._session_id,
        }
        response = self._exchange_control_request(
            payload,
            timeout_s=self._startup_request_timeout_s,
            retries=retries,
            retry_delay_s=self._startup_retry_delay_s,
        ) or {}
        accepted = response.get(KW.RESULT) == KW.OK
        if accepted:
            self._subscription_requests[str(topic)] = (subscriber, int(size), mode)
        return accepted

    def restore_event_registrations(self) -> int:
        """Request every Event publication and subscription of this client again, once each.

        Control keeps publish slots and subscriptions in memory; after it lost this client's registration
        they must be requested again on the new registration. Raises on the first refusal. Returns the
        number of restored registrations.
        """
        publications = dict(self._publish_requests)
        subscriptions = dict(self._subscription_requests)
        if not publications and not subscriptions:
            return 0
        if self._event_pool_replaced:
            raise EventPoolReplacedError(
                "Control serves a different Event shared-memory pool than the one this process mapped; "
                "this process's Event publications and subscriptions cannot be restored until it restarts"
            )
        for topic, (subsystem, size, mode, priority, queue_depth) in publications.items():
            self._request_publish_slots(subsystem, topic, size, mode, priority, queue_depth, retries=1)
        for topic, (subscriber, size, mode) in subscriptions.items():
            if not self._request_subscription(subscriber, topic, size, mode, retries=1):
                raise RuntimeError(f"Control refused the Event subscription to {topic}")
        return len(publications) + len(subscriptions)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_share_memory(self):
        if self._buffer_pool:
            return

        result = self.register_subsystem()
        if not result:
            raise RuntimeError("Failed to register subsystem with control backend over Unix socket")
        if result.get(KW.RESULT) != KW.OK:
            detail = result.get("error") or result.get("message") or repr(result)
            raise RuntimeError(
                f'Failed to register subsystem "{self._subsystem_name}" with control: {detail}'
            )
        self._apply_startup_barrier_config(result)

        try:
            topic_buffer_pool_name = str(result[KW.TOPIC_BUFFER_POOL_NAME])
            topic_buffer_pool_slot_size = int(result[KW.TOPIC_BUFFER_POOL_SLOT_SIZE])
            topic_buffer_pool_slot_count = int(result[KW.TOPIC_BUFFER_POOL_SLOT_COUNT])
        except Exception as exc:
            raise RuntimeError("ShmManager did not return required shared memory info") from exc

        try:
            self._event_pool_identity = _shared_memory_identity(topic_buffer_pool_name)
            self._buffer_pool = TopicBufferPool(
                name=topic_buffer_pool_name,
                slot_size=topic_buffer_pool_slot_size,
                slot_count=topic_buffer_pool_slot_count,
            )
            if self._prefer_rust and hasattr(self._buffer_pool, "_core"):
                self._core_mode = "rust"
            else:
                self._core_mode = "python"
        except Exception as exc:
            raise RuntimeError(f"[{LOG_ID_SHM}] Failed to attach shm_manager shared memory: {exc}") from exc

        if self._core_mode == "rust":
            try:
                from r2kernel.ffi import R2KernelCore  # type: ignore
                self._rust_core = R2KernelCore()
                out = ctypes.c_void_p()
                rc = self._rust_core.lib.r2kernel_event_client_create(
                    str(topic_buffer_pool_name).encode("utf-8"),
                    int(topic_buffer_pool_slot_size),
                    int(topic_buffer_pool_slot_count),
                    ctypes.byref(out),
                )
                if rc != 0 or not out.value:
                    raise RuntimeError(f"r2kernel_event_client_create rc={rc}")
                self._rust_event_handle = out
            except Exception as exc:
                # Existing Python-mode fallback, kept but never silent: the mode change is an error-level log.
                self._logger.error(
                    LOG_ID_SHM,
                    f"Native Event client creation failed ({exc}); this client publishes and receives Events "
                    "in the Python mode",
                )
                self._core_mode = "python"
                self._rust_core = None
                self._rust_event_handle = None

    def _open_lock(self, name: str):
        if not name:
            return None
        lock = self._lock_backend.open(name)
        if lock is None:
            self._logger.error(LOG_ID_SHM, f"Failed to open lock {name}")
        return lock

    def _apply_startup_barrier_config(self, response: dict[str, Any] | None) -> None:
        if self._startup_barrier_env_override or not isinstance(response, dict):
            return
        value = response.get(KW.EVENT_STARTUP_BARRIER_MS)
        if value is None:
            return
        try:
            barrier_ms = max(0, int(value))
        except Exception:
            return
        if barrier_ms == self._startup_barrier_ms:
            return
        self._startup_barrier_ms = barrier_ms
        self._logger.debug(
            LOG_ID_SHM,
            f"Configured publish startup barrier timeout_ms={self._startup_barrier_ms}",
        )

    def _release_publish_locks(self, topic: str) -> None:
        # Post while holding the condition: the notify thread replaces and closes a subscriber's
        # handle under the same condition, so a handle is never closed while it is being posted
        # (closing frees the native handle; a concurrent post was a use-after-free).
        with self._startup_barrier_cv:
            locks = self._publish_locks.get(str(topic))
            if locks:
                for lock in locks.values():
                    self._lock_backend.release(lock)

    def _has_publish_locks(self, topic: str) -> bool:
        return bool(self._publish_locks.get(str(topic)))
