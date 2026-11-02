"""Module-level constants and helpers shared by the SubsystemRuntime implementation modules."""

from __future__ import annotations


LOG_ID_OPERATION = "Operation"
OPERATION_EVENT_SIZE = 64 * 1024
_OPERATION_TERMINAL_STATES = {"succeeded", "failed", "canceled", "rejected"}
_DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX = "procedure.call_typed/"
_DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX = "operation.start_typed/"
_TYPED_OPERATION_QUERY_TOPIC = "operation.query_typed"
_TYPED_OPERATION_CANCEL_TOPIC = "operation.cancel_typed"


def _is_runtime_transport_topic(topic: str) -> bool:
    return (
        topic
        in {
            "procedure.call",
            "procedure.call_typed",
            "operation.start",
            "operation.start_typed",
            "operation.cancel",
            "operation.query",
            _TYPED_OPERATION_CANCEL_TOPIC,
            _TYPED_OPERATION_QUERY_TOPIC,
        }
        or topic.startswith(_DIRECT_TYPED_PROCEDURE_TOPIC_PREFIX)
        or topic.startswith(_DIRECT_TYPED_OPERATION_START_TOPIC_PREFIX)
    )
