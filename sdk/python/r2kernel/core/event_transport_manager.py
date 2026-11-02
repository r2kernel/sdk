"""Event publish/subscribe transport management used by SubsystemRuntime."""

from __future__ import annotations

import builtins
import inspect
import re
import time
from typing import Any, Callable

from r2kernel.datatype import DTypeLiteral
from r2kernel.datatype.basetype import DEFAULT_TOPIC_SIZE, get_decoder, get_encoder, get_type_size
from r2kernel.helper.logger import Logger, Loggable
from .r2kernel_client import R2KernelClient
from .codec_lifecycle import codec_activity


LOG_ID_EVENT_MESSAGE = "EventMessage"


_MESSAGE_REF_RE = re.compile(
    r"^(?P<namespace>(?:msg|api)(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.v[0-9]+)/(?P<message>[A-Za-z_][A-Za-z0-9_]*)$"
)


def _is_spec_message_ref(type_ref: str) -> bool:
    # This classifies a reference. Existence/type validation belongs to the
    # initialization schema context, avoiding stale process-wide file caches.
    return _MESSAGE_REF_RE.fullmatch(str(type_ref or "").strip()) is not None


class EventTransportManager(Loggable):
    def __init__(
        self,
        *,
        logger: Logger,
        owner: Any,
        client: R2KernelClient,
    ) -> None:
        super().__init__()
        self._logger = logger
        self._owner = owner
        self._client = client
        self._published_variables: set[tuple[str, str]] = set()
        self._subscribed_variables: set[tuple[str, str]] = set()
        self._local_vars: dict[str, dict[str, Any]] = {}
        self._message_codec = None
        self._codec_lifecycle = None
        self._timestamp = time.time_ns

    @property
    def logger(self) -> Logger:
        return self._logger

    def set_default_timestamp_getter(self, timestamp: Callable[[], int]) -> None:
        self._timestamp = timestamp

    def start_transport(self) -> None:
        self._client.start_event_transport()

    def stop_transport(self) -> None:
        self._client.stop_event_transport()

    def register_publish(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        src_attr: str | None = None,
        priority: int = 5,
        queue_depth: int | None = None,
    ) -> bool:
        if self._codec_lifecycle is not None and self._codec_lifecycle.stage_event(
            'publishing', topic, type,
            lambda: self.register_publish(topic=topic, type=type, size=size, src_attr=src_attr,
                                          priority=priority, queue_depth=queue_depth),
        ):
            return True
        existing = self._local_vars.get(topic)
        if existing is not None:
            if str(existing.get("dtype") or "") == str(type):
                return True
            self.logger.error(
                LOG_ID_EVENT_MESSAGE,
                f"Event publish topic '{topic}' is already registered with type '{existing.get('dtype')}', not '{type}'",
            )
            return False

        try:
            encoder, is_message_type = self._resolve_encoder(type)
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Unsupported type '{type}' for {topic}: {exc}")
            return False

        if size is None:
            size = self._resolve_topic_size(topic=topic, type=type, is_message_type=is_message_type)
            if size is None:
                return False

        if size <= 0:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Invalid alloc_size for {topic}: {size}")
            return False

        if self._codec_lifecycle is not None:
            self._codec_lifecycle.prepare_event('publishing', topic, type)
        if not self._client.request_publish_slots(
            subsystem=self._owner.name,
            topic=topic,
            size=size,
            mode="event",
            priority=priority,
            queue_depth=queue_depth,
        ):
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to allocate publish buffer for {topic}")
            return False

        self._local_vars[topic] = {
            "encoder": encoder,
            "priority": priority,
            "src_attr": src_attr,
            "dtype": type,
            "is_message_type": is_message_type,
        }
        self._published_variables.add((topic, type))
        return True

    def publish_on_change(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        src_attr: str | None = None,
        priority: int = 5,
        queue_depth: int | None = None,
    ) -> None:
        if self._codec_lifecycle is not None and self._codec_lifecycle.stage_event(
            'publishing', topic, type,
            lambda: self.publish_on_change(topic=topic, type=type, size=size, src_attr=src_attr,
                                           priority=priority, queue_depth=queue_depth),
        ):
            return
        if src_attr is None:
            src_attr = topic

        if not self.register_publish(
            topic=topic,
            type=type,
            size=size,
            src_attr=src_attr,
            priority=priority,
            queue_depth=queue_depth,
        ):
            if self._codec_lifecycle is not None and self._codec_lifecycle._initializing:
                raise RuntimeError(f'Prepared Event publisher registration rejected: {topic}')
            return

        parent_obj, leaf = self._resolve_attr(self._owner, src_attr)
        target_cls = builtins.type(parent_obj) if "." in src_attr else builtins.type(self._owner)
        if not hasattr(target_cls, "__dict__"):
            self.logger.error(
                LOG_ID_EVENT_MESSAGE,
                f"Cannot install on-change hook for {src_attr}: target class has no __dict__",
            )
            return

        storage_name = f"__r2kernel_obs_{leaf}"

        try:
            current_parent, current_leaf = self._resolve_attr(self._owner, src_attr)
            object.__setattr__(current_parent, storage_name, getattr(current_parent, current_leaf))
        except Exception:
            pass

        def prop_getter(obj):
            parent, _ = self._resolve_attr(obj, src_attr) if "." in src_attr else (obj, leaf)
            try:
                return object.__getattribute__(parent, storage_name)
            except Exception:
                return None

        def prop_setter(obj, value):
            if isinstance(value, tuple) and len(value) == 2:
                payload_value, timestamp = value
            else:
                payload_value, timestamp = value, self._timestamp()

            parent, _ = self._resolve_attr(obj, src_attr) if "." in src_attr else (obj, leaf)
            try:
                object.__setattr__(parent, storage_name, payload_value)
            except Exception:
                pass
            self.publish_event(topic=topic, value=payload_value, timestamp=timestamp)

        try:
            setattr(target_cls, leaf, property(prop_getter, prop_setter))
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to install on-change hook for {src_attr}: {exc}")

        self._published_variables.add((topic, type))

    @codec_activity
    def publish_event(self, *, topic: str, value: Any, timestamp: int | None = None) -> bool:
        entry = self._local_vars.get(topic)
        if not entry:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Topic '{topic}' was not registered for publish")
            return False
        if timestamp is None:
            timestamp = self._timestamp()

        dtype = entry.get("dtype")
        if (
            dtype
            and not entry.get("is_message_type")
            and str(dtype).lower() != "bytes"
            and self._client.publish_event_typed(
                topic=topic,
                dtype=dtype,
                value=value,
                timestamp=timestamp,
                priority=entry.get("priority", 0),
            )
        ):
            self.logger.debug(LOG_ID_EVENT_MESSAGE, f"Publish {topic}={str(value)}, ts={timestamp}"[:60])
            return True

        try:
            payload = entry["encoder"](value)
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to encode {topic}: {exc}")
            return False

        try:
            self._client.publish_event(
                topic=topic,
                payload=payload,
                timestamp=timestamp,
                priority=entry.get("priority", 0),
            )
            self.logger.debug(LOG_ID_EVENT_MESSAGE, f"Publish {topic}={str(value)}, ts={timestamp}"[:60])
            return True
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to publish {topic}: {exc}")
            return False

    def register_subscribe(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        dest_attr: str | None = None,
        callback: Callable[[Any, int], None] | None = None,
    ) -> None:
        if self._codec_lifecycle is not None and self._codec_lifecycle.stage_event(
            'subscribing', topic, type,
            lambda: self.register_subscribe(topic=topic, type=type, size=size,
                                            dest_attr=dest_attr, callback=callback),
        ):
            return
        try:
            decoder, is_message_type = self._resolve_decoder(type)
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Unsupported type '{type}' for {topic}: {exc}")
            raise RuntimeError(f"Unsupported Event subscriber type '{type}' for {topic}") from exc

        if size is None:
            size = self._resolve_topic_size(topic=topic, type=type, is_message_type=is_message_type)
            if size is None:
                raise RuntimeError(f'Cannot determine Event subscriber size for {topic}')

        callback_accepts_timing = False
        if callback is not None:
            try:
                parameters = list(inspect.signature(callback).parameters.values())
                callback_accepts_timing = any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
                ) or len(
                    [
                        parameter
                        for parameter in parameters
                        if parameter.kind
                        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    ]
                ) >= 3
            except (TypeError, ValueError):
                callback_accepts_timing = False

        event_generation = self._codec_lifecycle.event_generation() if self._codec_lifecycle is not None else None

        def handler(payload: bytes, timestamp: int, timing=None) -> None:
            try:
                from contextlib import nullcontext
                activity = self._codec_lifecycle.callback(event_generation=event_generation) if self._codec_lifecycle is not None else nullcontext()
                with activity:
                    value = decoder(payload) if decoder else payload
                    if dest_attr is not None:
                        setattr(self._owner, dest_attr, value)
                    if callback is not None:
                        if callback_accepts_timing:
                            callback(value, timestamp, timing)
                        else:
                            callback(value, timestamp)
                self.logger.debug(LOG_ID_EVENT_MESSAGE, f"Subscribe {topic}={value!r:.100}, ts={timestamp}")
            except Exception as exc:
                self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to handle broker payload for {topic}: {exc}")

        if self._codec_lifecycle is not None:
            self._codec_lifecycle.prepare_event('subscribing', topic, type)
        if not self._client.subscribe_event(topic=topic, size=size, handler=handler):
            raise RuntimeError(f'Prepared Event subscriber registration rejected: {topic}')
        self.logger.debug(LOG_ID_EVENT_MESSAGE, f"Registered subscription for {topic}")
        self._subscribed_variables.add((topic, type))

    def get_publish_info(self) -> list[tuple[str, str]]:
        staged = self._codec_lifecycle.event_declarations['publishing'] if self._codec_lifecycle is not None else set()
        return list(self._published_variables | staged)

    def get_subscribe_info(self) -> list[tuple[str, str]]:
        staged = self._codec_lifecycle.event_declarations['subscribing'] if self._codec_lifecycle is not None else set()
        return list(self._subscribed_variables | staged)

    def has_registrations(self) -> bool:
        return bool(self._published_variables or self._subscribed_variables)

    def _resolve_attr(self, obj: Any, path: str) -> tuple[Any, str]:
        parts = path.split(".")
        parent = obj
        for part in parts[:-1]:
            parent = getattr(parent, part)
        return parent, parts[-1]

    def _resolve_encoder(self, type_ref: DTypeLiteral) -> tuple[Callable[[Any], bytes], bool]:
        try:
            return get_encoder(type_ref), False
        except Exception as exc:
            normalized = str(type_ref or "").strip()
            if _is_spec_message_ref(normalized):
                return lambda value, ref=normalized: self._get_message_codec().encode_message(ref, value), True
            raise exc

    def _resolve_decoder(self, type_ref: DTypeLiteral) -> tuple[Callable[[bytes], Any], bool]:
        try:
            return get_decoder(type_ref), False
        except Exception as exc:
            normalized = str(type_ref or "").strip()
            if _is_spec_message_ref(normalized):
                return lambda payload, ref=normalized: self._get_message_codec().decode_message(ref, payload), True
            raise exc

    def _resolve_topic_size(self, *, topic: str, type: DTypeLiteral, is_message_type: bool) -> int | None:
        if is_message_type:
            self.logger.warning(
                LOG_ID_EVENT_MESSAGE,
                f"Using default topic size {DEFAULT_TOPIC_SIZE} for '{topic}' ({type})",
            )
            return DEFAULT_TOPIC_SIZE
        try:
            size = get_type_size(type)
            if size is None:
                size = DEFAULT_TOPIC_SIZE
                self.logger.warning(
                    LOG_ID_EVENT_MESSAGE,
                    f"Using default topic size {DEFAULT_TOPIC_SIZE} for '{topic}' ({type})",
                )
            return size
        except Exception as exc:
            self.logger.error(LOG_ID_EVENT_MESSAGE, f"Failed to determine size for {topic}: {exc}")
            return None

    def _get_message_codec(self):
        if self._codec_lifecycle is not None:
            self._codec_lifecycle.require_available()
        if self._message_codec is None:
            from r2kernel.interface.codec import RustMessageCodec, warn_unprepared_codec_once

            self._message_codec = RustMessageCodec()
            warn_unprepared_codec_once(self.logger)
        return self._message_codec
