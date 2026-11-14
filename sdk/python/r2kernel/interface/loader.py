from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from r2kernel.spec_paths import relative_spec_path, resolve_spec_path, read_spec_text, spec_path_exists

from .model import (
    ControlEndpointSpec,
    EventEndpointSpec,
    InterfaceSpec,
    MessageFieldSpec,
    MessageSpec,
    OperationEndpointSpec,
    ProcedureEndpointSpec,
)

def _spec_path_from_id(spec_id: str) -> Path:
    try:
        relative = relative_spec_path("interfaces", spec_id)
    except ValueError as exc:
        raise InterfaceSpecError(str(exc)) from exc
    path = resolve_spec_path(relative)
    if path is None:
        raise InterfaceSpecError(f"Interface spec not found for id: {spec_id}")
    return path


class InterfaceSpecError(ValueError):
    pass


# Every public spec document uses the common `version`/`id`/`kind` header.
SPEC_DOCUMENT_VERSION = 1
MESSAGE_SET_KIND = "message_set"
NAMESPACE_DEFAULTS_KIND = "namespace_defaults"
_LEGACY_HEADER_KEYS = ("schema_version", "namespace")
# frames.policy whose frames.ports binds published message fields to provider spatial ports.
SPATIAL_PORTS_FRAME_POLICY = "spatial_ports"
_STRING_TYPES = ("string", "str")


def _load_default_namespace_versions() -> dict[str, str]:
    defaults_path = resolve_spec_path("defaults.yaml")
    if defaults_path is None:
        return {}
    document = yaml.safe_load(read_spec_text(defaults_path)) or {}
    if not isinstance(document, dict):
        raise InterfaceSpecError(f"defaults must be a mapping in {defaults_path}")
    # Package roots may ship a bare `namespaces` map; a declared header must be
    # the namespace-defaults header.
    if "version" in document and document.get("version") != SPEC_DOCUMENT_VERSION:
        raise InterfaceSpecError(f"defaults version must be {SPEC_DOCUMENT_VERSION} in {defaults_path}")
    if "kind" in document and document.get("kind") != NAMESPACE_DEFAULTS_KIND:
        raise InterfaceSpecError(f"defaults kind must be {NAMESPACE_DEFAULTS_KIND} in {defaults_path}")
    raw = document.get("namespaces") or {}
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"defaults namespaces must be a mapping in {defaults_path}")
    defaults: dict[str, str] = {}
    for namespace, version in raw.items():
        namespace_text = str(namespace or "").strip()
        version_text = str(version or "").strip()
        if namespace_text and version_text:
            defaults[namespace_text] = version_text
    return defaults


def _is_versioned_namespace(text: str) -> bool:
    parts = [part for part in text.split(".") if part]
    if len(parts) < 2:
        return False
    suffix = parts[-1]
    return len(suffix) >= 2 and suffix[0] == "v" and suffix[1:].isdigit()


def load_interface_spec(ref: str | Path | InterfaceSpec) -> InterfaceSpec:
    if isinstance(ref, InterfaceSpec):
        return ref
    if isinstance(ref, Path):
        return _load_interface_spec_from_path(ref)
    try:
        builtin_path = _spec_path_from_id(ref)
    except InterfaceSpecError:
        builtin_path = None
    if builtin_path is not None and spec_path_exists(builtin_path):
        return _load_interface_spec_from_path(builtin_path)
    path = Path(ref)
    if path.suffix in {".yaml", ".yml"}:
        return _load_interface_spec_from_path(path)
    raise InterfaceSpecError(f"Unknown interface spec reference: {ref}")


def load_interface_specs(refs: list[str | Path | InterfaceSpec] | tuple[str | Path | InterfaceSpec, ...]) -> list[InterfaceSpec]:
    return [load_interface_spec(ref) for ref in refs]


def _load_interface_spec_from_path(path: Path) -> InterfaceSpec:
    if not path.is_absolute():
        path = path.resolve()
    if not spec_path_exists(path):
        raise InterfaceSpecError(f"Interface spec file not found: {path}")
    document = yaml.safe_load(read_spec_text(path)) or {}
    if not isinstance(document, dict):
        raise InterfaceSpecError(f"Interface spec must be a mapping: {path}")
    return _parse_interface_spec(document, source=str(path))


def _parse_interface_spec(document: dict[str, Any], *, source: str) -> InterfaceSpec:
    spec_id = str(document.get("id") or "").strip()
    name = str(document.get("name") or spec_id).strip()
    if not spec_id:
        raise InterfaceSpecError(f"Interface spec is missing id: {source}")
    legacy_keys = [key for key in _LEGACY_HEADER_KEYS if key in document]
    if legacy_keys:
        raise InterfaceSpecError(
            f"Legacy header key(s) {', '.join(legacy_keys)} in {source}; use version, id, and kind"
        )
    version = int(document.get("version") or 1)
    description = str(document.get("description") or "")
    kind = str(document.get("kind") or "").strip()
    if spec_id.startswith("msg.") or kind == MESSAGE_SET_KIND:
        if kind != MESSAGE_SET_KIND:
            raise InterfaceSpecError(f"Message set {spec_id} must declare kind: {MESSAGE_SET_KIND} ({source})")
        if document.get("version") != SPEC_DOCUMENT_VERSION:
            raise InterfaceSpecError(
                f"Message set {spec_id} must declare version: {SPEC_DOCUMENT_VERSION} ({source})"
            )
    imports = _parse_imports(document.get("imports"), source=source)

    raw_messages = document.get("messages") or {}
    if not isinstance(raw_messages, dict):
        raise InterfaceSpecError(f"messages must be a mapping in {source}")
    messages: dict[str, MessageSpec] = {}
    for message_name, raw_spec in raw_messages.items():
        if not isinstance(raw_spec, dict):
            raise InterfaceSpecError(f"message '{message_name}' must be an object in {source}")
        raw_fields = raw_spec.get("fields") or []
        if not isinstance(raw_fields, list):
            raise InterfaceSpecError(f"message '{message_name}' fields must be a list in {source}")
        fields: list[MessageFieldSpec] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                raise InterfaceSpecError(f"message '{message_name}' field must be an object in {source}")
            field_name = str(raw_field.get("name") or "").strip()
            field_type = str(raw_field.get("type") or "").strip()
            if not field_name or not field_type:
                raise InterfaceSpecError(f"message '{message_name}' field is missing name/type in {source}")
            fields.append(
                MessageFieldSpec(
                    name=field_name,
                    type=field_type,
                    optional=bool(raw_field.get("optional", False)),
                    description=str(raw_field.get("description") or ""),
                )
            )
        messages[message_name] = MessageSpec(
            name=message_name,
            fields=tuple(fields),
            description=str(raw_spec.get("description") or ""),
        )

    events = tuple(_parse_event_spec(raw, source=source) for raw in (document.get("events") or []))
    controls = tuple(
        _parse_control_spec(raw, source=source, messages=messages)
        for raw in _endpoint_items(document, "controls", source=source)
    )
    procedures = tuple(
        _parse_procedure_spec(raw, source=source, messages=messages)
        for raw in _endpoint_items(document, "procedures", source=source)
    )
    operations = tuple(
        _parse_operation_spec(raw, source=source, messages=messages)
        for raw in _endpoint_items(document, "operations", source=source)
    )

    normalized_messages = _normalize_message_specs(spec_id=spec_id, messages=messages, imports=imports)
    normalized_events = tuple(_normalize_event_spec(spec_id, event, messages, imports) for event in events)
    normalized_controls = tuple(_normalize_control_spec(spec_id, endpoint, messages, imports) for endpoint in controls)
    normalized_procedures = tuple(_normalize_procedure_spec(spec_id, procedure, messages, imports) for procedure in procedures)
    normalized_operations = tuple(_normalize_operation_spec(spec_id, operation, messages, imports) for operation in operations)

    _validate_type_refs(
        spec_id=spec_id,
        source=source,
        messages=normalized_messages,
        events=normalized_events,
        controls=normalized_controls,
        procedures=normalized_procedures,
        operations=normalized_operations,
    )
    frames = _parse_frames(
        document.get("frames"),
        spec_id=spec_id,
        events=normalized_events,
        messages=normalized_messages,
        source=source,
    )

    return InterfaceSpec(
        id=spec_id,
        name=name,
        version=version,
        description=description,
        kind=kind,
        imports=imports,
        frames=frames,
        timing=_parse_metadata_mapping(document.get("timing"), key="timing", source=source),
        state_model=_parse_metadata_mapping(document.get("state_model"), key="state_model", source=source),
        failure_semantics=_parse_metadata_mapping(
            document.get("failure_semantics"), key="failure_semantics", source=source
        ),
        authority=_parse_metadata_mapping(document.get("authority"), key="authority", source=source),
        limits=_parse_metadata_mapping(document.get("limits"), key="limits", source=source),
        conformance=_parse_metadata_mapping(document.get("conformance"), key="conformance", source=source),
        messages=normalized_messages,
        events=normalized_events,
        controls=normalized_controls,
        procedures=normalized_procedures,
        operations=normalized_operations,
    )


def _parse_imports(raw: Any, *, source: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"imports must be a mapping in {source}")
    imports: dict[str, str] = {}
    for namespace, version in raw.items():
        namespace_text = str(namespace or "").strip()
        version_text = str(version or "").strip()
        if not namespace_text or not version_text:
            raise InterfaceSpecError(f"imports entries must be non-empty in {source}")
        if _is_versioned_namespace(namespace_text):
            raise InterfaceSpecError(
                f"imports key '{namespace_text}' must omit the version suffix in {source}"
            )
        if not (version_text.startswith("v") and version_text[1:].isdigit()):
            raise InterfaceSpecError(
                f"imports version '{version_text}' must use vN form in {source}"
            )
        imports[namespace_text] = version_text
    return imports


def _parse_metadata_mapping(raw: Any, *, key: str, source: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"{key} must be a mapping in {source}")
    return dict(raw)


def spatial_frame_ports(spec: InterfaceSpec) -> dict[str, str]:
    """Message field -> provider spatial port of a ``spatial_ports`` API; empty for any other policy.

    Every sample of an Event the API publishes carries, in each mapped field,
    exactly the frame of that spatial port of its provider.
    """
    if spec.frames.get("policy") != SPATIAL_PORTS_FRAME_POLICY:
        return {}
    return dict(spec.frames["ports"])


def _parse_frames(
    raw: Any,
    *,
    spec_id: str,
    events: tuple[EventEndpointSpec, ...],
    messages: dict[str, MessageSpec],
    source: str,
) -> dict[str, Any]:
    """Other frame policies remain metadata; ``spatial_ports`` binds published fields to provider ports."""
    frames = _parse_metadata_mapping(raw, key="frames", source=source)
    if frames.get("policy") != SPATIAL_PORTS_FRAME_POLICY:
        return frames
    raw_ports = frames.get("ports")
    if not isinstance(raw_ports, dict) or not raw_ports:
        raise InterfaceSpecError(f"frames.policy spatial_ports requires a non-empty frames.ports mapping in {source}")
    ports: dict[str, str] = {}
    for raw_field, raw_port in raw_ports.items():
        field_name = raw_field.strip() if isinstance(raw_field, str) else ""
        port = raw_port.strip() if isinstance(raw_port, str) else ""
        if not field_name or not port:
            raise InterfaceSpecError(f"frames.ports must map message field names to spatial port names in {source}")
        ports[field_name] = port
    published = [event for event in events if event.direction == "publish"]
    if not published:
        raise InterfaceSpecError(f"frames.policy spatial_ports requires a published event in {source}")
    for event in published:
        message = _published_message(spec_id, event.type, messages)
        if message is None:
            raise InterfaceSpecError(
                f"frames.ports cannot be checked: message {event.type} of event '{event.topic}' is not found in {source}"
            )
        fields = {field.name: field for field in message.fields}
        for field_name in ports:
            field = fields.get(field_name)
            if field is None or field.optional or field.type not in _STRING_TYPES:
                raise InterfaceSpecError(
                    f"frames.ports field '{field_name}' must be a required string field of {event.type} "
                    f"(event '{event.topic}') in {source}"
                )
    return {**frames, "ports": ports}


def _published_message(spec_id: str, type_ref: str, messages: dict[str, MessageSpec]) -> MessageSpec | None:
    namespace, separator, message_name = type_ref.partition("/")
    if not separator:
        return messages.get(type_ref)
    if namespace == spec_id:
        return messages.get(message_name)
    try:
        path = resolve_spec_path(relative_spec_path("messages", namespace))
    except ValueError:
        return None
    if path is None:
        return None
    document = yaml.safe_load(read_spec_text(path)) or {}
    if not isinstance(document, dict):
        return None
    message_set = _parse_interface_spec(document, source=str(path))
    if message_set.id != namespace:
        return None
    return message_set.messages.get(message_name)


def _endpoint_items(document: dict[str, Any], key: str, *, source: str) -> list[Any]:
    raw = document.get(key) or []
    if not isinstance(raw, list):
        raise InterfaceSpecError(f"{key} must be a list in {source}")
    return raw


def _resolve_external_namespace_version(namespace: str, imports: dict[str, str]) -> str | None:
    if namespace in imports:
        return imports[namespace]
    return _load_default_namespace_versions().get(namespace)


def _parse_event_spec(raw: Any, *, source: str) -> EventEndpointSpec:
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"event spec must be an object in {source}")
    topic = str(raw.get("topic") or "").strip()
    type_name = str(raw.get("type") or "").strip()
    direction = str(raw.get("direction") or "publish").strip()
    if direction not in {"publish", "subscribe"}:
        raise InterfaceSpecError(f"event '{topic}' has invalid direction '{direction}' in {source}")
    if not topic or not type_name:
        raise InterfaceSpecError(f"event spec is missing topic/type in {source}")
    return EventEndpointSpec(
        topic=topic,
        type=type_name,
        direction=direction,
        description=str(raw.get("description") or ""),
        semantics=_parse_metadata_mapping(raw.get("semantics"), key="event semantics", source=source),
    )


def _parse_endpoint_args(raw: Any, *, source: str) -> tuple[MessageFieldSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InterfaceSpecError(f"args must be a list in {source}")
    fields: list[MessageFieldSpec] = []
    for raw_field in raw:
        if not isinstance(raw_field, dict):
            raise InterfaceSpecError(f"endpoint arg must be an object in {source}")
        field_name = str(raw_field.get("name") or "").strip()
        field_type = str(raw_field.get("type") or "").strip()
        if not field_name or not field_type:
            raise InterfaceSpecError(f"endpoint arg is missing name/type in {source}")
        fields.append(
            MessageFieldSpec(
                name=field_name,
                type=field_type,
                optional=bool(raw_field.get("optional", False)),
                description=str(raw_field.get("description") or ""),
            )
        )
    return tuple(fields)


def _to_pascal_case(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text)
    parts = [part for part in cleaned.split() if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Endpoint"


def _allocate_generated_message_name(base_name: str, *, messages: dict[str, MessageSpec]) -> str:
    candidate = f"{_to_pascal_case(base_name)}Args"
    if candidate not in messages:
        return candidate
    index = 2
    while f"{candidate}{index}" in messages:
        index += 1
    return f"{candidate}{index}"


def _parse_procedure_spec(raw: Any, *, source: str, messages: dict[str, MessageSpec]) -> ProcedureEndpointSpec:
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"procedure spec must be an object in {source}")
    name = str(raw.get("name") or "").strip()
    request = str(raw.get("request") or "").strip()
    args = _parse_endpoint_args(raw.get("args"), source=source)
    response = str(raw.get("response") or "").strip()
    timing_support = _parse_timing_support(raw.get("timing_support"), source=source)
    if not name or not response:
        raise InterfaceSpecError(f"procedure spec is missing name/response in {source}")
    if request and args:
        raise InterfaceSpecError(f"procedure '{name}' cannot declare both request and args in {source}")
    if not request and not args:
        raise InterfaceSpecError(f"procedure '{name}' must declare request or args in {source}")
    if args:
        generated_name = _allocate_generated_message_name(name, messages=messages)
        messages[generated_name] = MessageSpec(
            name=generated_name,
            fields=args,
            description=f"Auto-generated argument object for procedure '{name}'.",
        )
        request = generated_name
    return ProcedureEndpointSpec(
        name=name,
        request=request,
        response=response,
        timing_support=timing_support,
        description=str(raw.get("description") or ""),
        semantics=_parse_metadata_mapping(raw.get("semantics"), key="procedure semantics", source=source),
    )


def _parse_control_spec(raw: Any, *, source: str, messages: dict[str, MessageSpec]) -> ControlEndpointSpec:
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"control spec must be an object in {source}")
    topic = str(raw.get("topic") or "").strip()
    request = str(raw.get("request") or "").strip()
    args = _parse_endpoint_args(raw.get("args"), source=source)
    response = str(raw.get("response") or "").strip()
    timing_support = _parse_timing_support(raw.get("timing_support"), source=source)
    if not topic or not response:
        raise InterfaceSpecError(f"control spec is missing topic/response in {source}")
    if request and args:
        raise InterfaceSpecError(f"control '{topic}' cannot declare both request and args in {source}")
    if not request and not args:
        raise InterfaceSpecError(f"control '{topic}' must declare request or args in {source}")
    if args:
        generated_name = _allocate_generated_message_name(topic, messages=messages)
        messages[generated_name] = MessageSpec(
            name=generated_name,
            fields=args,
            description=f"Auto-generated argument object for control endpoint '{topic}'.",
        )
        request = generated_name
    return ControlEndpointSpec(
        topic=topic,
        request=request,
        response=response,
        timing_support=timing_support,
        description=str(raw.get("description") or ""),
        semantics=_parse_metadata_mapping(raw.get("semantics"), key="control semantics", source=source),
    )


def _parse_operation_spec(raw: Any, *, source: str, messages: dict[str, MessageSpec]) -> OperationEndpointSpec:
    if not isinstance(raw, dict):
        raise InterfaceSpecError(f"operation spec must be an object in {source}")
    name = str(raw.get("name") or "").strip()
    request = str(raw.get("request") or "").strip()
    args = _parse_endpoint_args(raw.get("args"), source=source)
    feedback = str(raw.get("feedback") or "").strip()
    result = str(raw.get("result") or "").strip()
    state = raw.get("state")
    state_name = str(state).strip() if state is not None else None
    timing_support = _parse_timing_support(raw.get("timing_support"), source=source)
    if not name or not feedback or not result:
        raise InterfaceSpecError(f"operation spec is missing name/feedback/result in {source}")
    if request and args:
        raise InterfaceSpecError(f"operation '{name}' cannot declare both request and args in {source}")
    if not request and not args:
        raise InterfaceSpecError(f"operation '{name}' must declare request or args in {source}")
    if args:
        generated_name = _allocate_generated_message_name(name, messages=messages)
        messages[generated_name] = MessageSpec(
            name=generated_name,
            fields=args,
            description=f"Auto-generated argument object for operation '{name}'.",
        )
        request = generated_name
    return OperationEndpointSpec(
        name=name,
        request=request,
        feedback=feedback,
        result=result,
        state=state_name,
        timing_support=timing_support,
        description=str(raw.get("description") or ""),
        semantics=_parse_metadata_mapping(raw.get("semantics"), key="operation semantics", source=source),
    )


def _parse_timing_support(raw: Any, *, source: str) -> str:
    value = str(raw or "none").strip() or "none"
    if value not in {"none", "optional", "required"}:
        raise InterfaceSpecError(f"timing_support must be one of none|optional|required in {source}")
    return value


def _validate_type_refs(
    *,
    spec_id: str,
    source: str,
    messages: dict[str, MessageSpec],
    events: tuple[EventEndpointSpec, ...],
    controls: tuple[ControlEndpointSpec, ...],
    procedures: tuple[ProcedureEndpointSpec, ...],
    operations: tuple[OperationEndpointSpec, ...],
    known_messages: dict[str, MessageSpec] | None = None,
) -> None:
    local_message_names = set(messages.keys())
    visited: set[str] = set()
    visiting: set[str] = set()

    def ensure_message(reference: str, *, location: str) -> None:
        if known_messages is None:
            return
        message = known_messages.get(reference)
        if message is None:
            raise InterfaceSpecError(f"Unresolved message dependency '{reference}' in {location} ({source})")
        if reference in visiting:
            raise InterfaceSpecError(f"Cyclic message dependency '{reference}' in {location} ({source})")
        if reference in visited:
            return
        visiting.add(reference)
        for field in message.fields:
            ensure_known(field.type, location=f"{reference}.{field.name}")
        visiting.remove(reference)
        visited.add(reference)

    def ensure_known(type_name: str, *, location: str) -> None:
        if not type_name:
            raise InterfaceSpecError(f"Empty type reference in {location} ({source})")
        if type_name.endswith("_array"):
            ensure_known(type_name[:-6], location=location)
            return
        if type_name.startswith("vector<") and type_name.endswith(">"):
            ensure_known(type_name[7:-1].strip(), location=location)
            return
        if type_name.startswith("array<") and type_name.endswith(">"):
            inner = type_name[6:-1]
            element, separator, length = inner.rpartition(",")
            if not separator or not element.strip() or not length.strip().isdigit() or int(length) <= 0:
                raise InterfaceSpecError(f"Invalid array type '{type_name}' in {location} ({source})")
            ensure_known(element.strip(), location=location)
            return
        if type_name in local_message_names:
            ensure_message(f"{spec_id}/{type_name}", location=location)
            return
        if "/" in type_name:
            ref_spec_id, _, message_name = type_name.partition("/")
            if ref_spec_id == spec_id and message_name in local_message_names:
                ensure_message(type_name, location=location)
                return
            if _is_versioned_namespace(ref_spec_id) and message_name:
                ensure_message(type_name, location=location)
                return
        # Builtin datatype names remain plain scalar/vector aliases for now.
        from r2kernel.datatype.basetype import get_encoder

        try:
            get_encoder(type_name)
            return
        except Exception:
            pass
        raise InterfaceSpecError(f"Unknown type '{type_name}' in {location} of {spec_id} ({source})")

    for message in messages.values():
        for field in message.fields:
            ensure_known(field.type, location=f"message {message.name}.{field.name}")
    for event in events:
        ensure_known(event.type, location=f"event {event.topic}")
    for endpoint in controls:
        ensure_known(endpoint.request, location=f"control {endpoint.topic} request")
        ensure_known(endpoint.response, location=f"control {endpoint.topic} response")
    for procedure in procedures:
        ensure_known(procedure.request, location=f"procedure {procedure.name} request")
        ensure_known(procedure.response, location=f"procedure {procedure.name} response")
    for operation in operations:
        ensure_known(operation.request, location=f"operation {operation.name} request")
        ensure_known(operation.feedback, location=f"operation {operation.name} feedback")
        ensure_known(operation.result, location=f"operation {operation.name} result")
        if operation.state:
            ensure_known(operation.state, location=f"operation {operation.name} state")


def _normalize_message_specs(
    spec_id: str,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> dict[str, MessageSpec]:
    normalized: dict[str, MessageSpec] = {}
    for message_name, message in messages.items():
        normalized[message_name] = MessageSpec(
            name=message.name,
            description=message.description,
            fields=tuple(
                MessageFieldSpec(
                    name=field.name,
                    type=_normalize_type_ref(spec_id, field.type, messages, imports),
                    optional=field.optional,
                    description=field.description,
                )
                for field in message.fields
            ),
        )
    return normalized


def _normalize_event_spec(
    spec_id: str,
    event: EventEndpointSpec,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> EventEndpointSpec:
    return EventEndpointSpec(
        topic=event.topic,
        direction=event.direction,
        type=_normalize_type_ref(spec_id, event.type, messages, imports),
        description=event.description,
        semantics=event.semantics,
    )


def _normalize_procedure_spec(
    spec_id: str,
    procedure: ProcedureEndpointSpec,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> ProcedureEndpointSpec:
    return ProcedureEndpointSpec(
        name=procedure.name,
        request=_normalize_type_ref(spec_id, procedure.request, messages, imports),
        response=_normalize_type_ref(spec_id, procedure.response, messages, imports),
        timing_support=procedure.timing_support,
        description=procedure.description,
        semantics=procedure.semantics,
    )


def _normalize_control_spec(
    spec_id: str,
    endpoint: ControlEndpointSpec,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> ControlEndpointSpec:
    return ControlEndpointSpec(
        topic=endpoint.topic,
        request=_normalize_type_ref(spec_id, endpoint.request, messages, imports),
        response=_normalize_type_ref(spec_id, endpoint.response, messages, imports),
        timing_support=endpoint.timing_support,
        description=endpoint.description,
        semantics=endpoint.semantics,
    )


def _normalize_operation_spec(
    spec_id: str,
    operation: OperationEndpointSpec,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> OperationEndpointSpec:
    return OperationEndpointSpec(
        name=operation.name,
        request=_normalize_type_ref(spec_id, operation.request, messages, imports),
        feedback=_normalize_type_ref(spec_id, operation.feedback, messages, imports),
        result=_normalize_type_ref(spec_id, operation.result, messages, imports),
        state=_normalize_type_ref(spec_id, operation.state, messages, imports) if operation.state else None,
        timing_support=operation.timing_support,
        description=operation.description,
        semantics=operation.semantics,
    )


def _normalize_type_ref(
    spec_id: str,
    type_name: str | None,
    messages: dict[str, MessageSpec],
    imports: dict[str, str],
) -> str | None:
    if type_name is None:
        return None
    ref = str(type_name).strip()
    if not ref:
        return ref
    if ref.endswith("_array"):
        return f"{_normalize_type_ref(spec_id, ref[:-6], messages, imports)}_array"
    if ref.startswith("vector<") and ref.endswith(">"):
        inner = _normalize_type_ref(spec_id, ref[7:-1].strip(), messages, imports)
        return f"vector<{inner}>"
    if ref.startswith("array<") and ref.endswith(">"):
        inner = ref[6:-1]
        element, separator, length = inner.rpartition(",")
        if not separator:
            return ref
        normalized_element = _normalize_type_ref(spec_id, element.strip(), messages, imports)
        return f"array<{normalized_element}, {length.strip()}>"
    if "/" in ref:
        namespace, _, message_name = ref.partition("/")
        if _is_versioned_namespace(namespace):
            return ref
        if namespace == spec_id:
            return ref
        version = _resolve_external_namespace_version(namespace, imports)
        if version:
            return f"{namespace}.{version}/{message_name}"
        return ref
    if ref in messages:
        return f"{spec_id}/{ref}"
    return ref


def interface_spec_to_document(spec: InterfaceSpec, *, include_imports: bool = False) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": spec.version,
        "id": spec.id,
        **({"kind": spec.kind} if spec.kind else {}),
        "name": spec.name,
        "description": spec.description,
        "messages": {},
    }
    if include_imports and spec.imports:
        document["imports"] = dict(spec.imports)
    messages = document["messages"]
    for message in spec.messages.values():
        messages[message.name] = {
            "description": message.description,
            "fields": [
                {
                    "name": field.name,
                    "type": field.type,
                    **({"optional": True} if field.optional else {}),
                    **({"description": field.description} if field.description else {}),
                }
                for field in message.fields
            ],
        }
    if spec.events:
        document["events"] = [
            {
                "topic": event.topic,
                "type": event.type,
                **({"direction": event.direction} if event.direction != "publish" else {}),
                **({"description": event.description} if event.description else {}),
            }
            for event in spec.events
        ]
    if spec.controls:
        document["controls"] = [
            {
                "topic": endpoint.topic,
                "request": endpoint.request,
                "response": endpoint.response,
                **({"timing_support": endpoint.timing_support} if endpoint.timing_support != "none" else {}),
                **({"description": endpoint.description} if endpoint.description else {}),
            }
            for endpoint in spec.controls
        ]
    if spec.procedures:
        document["procedures"] = [
            {
                "name": procedure.name,
                "request": procedure.request,
                "response": procedure.response,
                **({"timing_support": procedure.timing_support} if procedure.timing_support != "none" else {}),
                **({"description": procedure.description} if procedure.description else {}),
            }
            for procedure in spec.procedures
        ]
    if spec.operations:
        document["operations"] = [
            {
                "name": operation.name,
                "request": operation.request,
                "feedback": operation.feedback,
                "result": operation.result,
                **({"state": operation.state} if operation.state else {}),
                **({"timing_support": operation.timing_support} if operation.timing_support != "none" else {}),
                **({"description": operation.description} if operation.description else {}),
            }
            for operation in spec.operations
        ]
    return document
