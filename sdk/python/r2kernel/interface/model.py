from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EndpointDirection = Literal["publish", "subscribe"]
TimingSupport = Literal["none", "optional", "required"]


@dataclass(frozen=True)
class MessageFieldSpec:
    name: str
    type: str
    optional: bool = False
    description: str = ""


@dataclass(frozen=True)
class MessageSpec:
    name: str
    fields: tuple[MessageFieldSpec, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class EventEndpointSpec:
    topic: str
    type: str
    direction: EndpointDirection = "publish"
    description: str = ""
    semantics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcedureEndpointSpec:
    name: str
    request: str
    response: str
    timing_support: TimingSupport = "none"
    description: str = ""
    semantics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlEndpointSpec:
    topic: str
    request: str
    response: str
    timing_support: TimingSupport = "none"
    description: str = ""
    semantics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationEndpointSpec:
    name: str
    request: str
    feedback: str
    result: str
    state: str | None = None
    timing_support: TimingSupport = "none"
    description: str = ""
    semantics: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class InterfaceSpec:
    id: str
    name: str
    version: int = 1
    description: str = ""
    kind: str = ""
    imports: dict[str, str] = field(default_factory=dict)
    frames: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    state_model: dict[str, Any] = field(default_factory=dict)
    failure_semantics: dict[str, Any] = field(default_factory=dict)
    authority: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    conformance: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, MessageSpec] = field(default_factory=dict)
    events: tuple[EventEndpointSpec, ...] = ()
    controls: tuple[ControlEndpointSpec, ...] = ()
    procedures: tuple[ProcedureEndpointSpec, ...] = ()
    operations: tuple[OperationEndpointSpec, ...] = ()

    def published_events(self) -> tuple[EventEndpointSpec, ...]:
        return tuple(event for event in self.events if event.direction == "publish")

    def subscribed_events(self) -> tuple[EventEndpointSpec, ...]:
        return tuple(event for event in self.events if event.direction == "subscribe")

    def qualify_type_ref(self, type_name: str) -> str:
        if type_name in self.messages:
            return f"{self.id}/{type_name}"
        return type_name

    def accepted_type_refs(self, type_name: str) -> tuple[str, ...]:
        qualified = self.qualify_type_ref(type_name)
        if qualified == type_name:
            return (type_name,)
        return (qualified, type_name)
