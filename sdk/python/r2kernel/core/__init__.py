"""Subsystem core APIs."""

__all__ = [
    "ControlContractError",
    "ControlRequestError",
    "ControlCycle",
    "ControlGuard",
    "ControlIntent",
    "ControlPermit",
    "ControlPolicy",
    "ControlRefreshMode",
    "PendingEffectHandle",
    "CompletedEncodedUnit",
    "EncodedChunk",
    "PartAddResult",
    "PartDisposition",
    "StreamAssemblyError",
    "StreamIdentity",
    "StreamLimits",
    "StreamTransferAssembler",
    "TransferPartHeader",
    "ClockSnapshot",
    "ClockSource",
    "EventTransport",
    "EventTimingMetadata",
    "GATEWAY_DISPATCH_REPORT_REF",
    "REQUEST_HEADER_REF",
    "RESPONSE_HEADER_REF",
    "BrokeredSocketResult",
    "BrokeredFdResult",
    "ResourceBrokerClient",
    "InheritedFdError",
    "encode_inherited_fds",
    "inherited_socket",
    "R2KernelClient",
    "SubsystemRuntime",
    "clock_now_ns",
    "clock_snapshot",
    "configure_clock",
    "configure_clock_from_status",
    "reset_clock",
]

_COMMON_CONTROL_EXPORTS = {
    "ControlContractError",
    "ControlCycle",
    "ControlGuard",
    "ControlIntent",
    "ControlPermit",
    "ControlPolicy",
    "ControlRefreshMode",
    "PendingEffectHandle",
}

_STREAM_TRANSFER_EXPORTS = {
    "CompletedEncodedUnit",
    "EncodedChunk",
    "PartAddResult",
    "PartDisposition",
    "StreamAssemblyError",
    "StreamIdentity",
    "StreamLimits",
    "StreamTransferAssembler",
    "TransferPartHeader",
}

_CLOCK_EXPORTS = {
    "ClockSnapshot",
    "ClockSource",
    "clock_now_ns",
    "clock_snapshot",
    "configure_clock",
    "configure_clock_from_status",
    "reset_clock",
}

_RESOURCE_EXPORTS = {
    "BrokeredSocketResult",
    "BrokeredFdResult",
    "ResourceBrokerClient",
    "InheritedFdError",
    "encode_inherited_fds",
    "inherited_socket",
}

_TIMING_EXPORTS = {
    "GATEWAY_DISPATCH_REPORT_REF",
    "REQUEST_HEADER_REF",
    "RESPONSE_HEADER_REF",
}


def __getattr__(name):
    if name in _COMMON_CONTROL_EXPORTS:
        from . import common_control

        return getattr(common_control, name)
    if name in _STREAM_TRANSFER_EXPORTS:
        from . import stream_transfer

        return getattr(stream_transfer, name)
    if name in _CLOCK_EXPORTS:
        from . import clock

        return getattr(clock, name)
    if name in _RESOURCE_EXPORTS:
        from . import resource

        return getattr(resource, name)
    if name in _TIMING_EXPORTS:
        from . import timing

        return getattr(timing, name)
    if name == "ControlRequestError":
        from .control_client import ControlRequestError

        return ControlRequestError
    if name == "EventTransport":
        from .event_transport import EventTransport

        return EventTransport
    if name == "EventTimingMetadata":
        from .event_client import EventTimingMetadata

        return EventTimingMetadata
    if name == "R2KernelClient":
        from .r2kernel_client import R2KernelClient

        return R2KernelClient
    if name == "SubsystemRuntime":
        from .subsystem_runtime import SubsystemRuntime

        return SubsystemRuntime
    raise AttributeError(name)
