"""Control protocol keywords: request commands and JSON field names.

The identifiers are internal; the string values are the Control wire protocol and must not change.
"""

# Protocol envelope
COMMAND = "cmd"
PARAMETER = "param"
RESULT = "res"
OK = "ok"

# Commands
REGISTER_SUBSYSTEM = "register_subsystem"
UPDATE_SUBSYSTEM_STATUS = "update_subsystem_status"
REQUEST_PUBLISH_SLOTS = "request_publish_slots"
REQUEST_SUBSCRIBE = "register_subscription"
RESOLVE_CONTROL_TARGET = "resolve_control_target"
REQUEST_CLOCK_STATUS = "clock_status"

# Notifications
NOTIFY_PUBLISHER = "notify_publisher"
NOTIFY_SUBSCRIBER = "notify_subscriber"
LIFECYCLE_TRANSITION = "lifecycle_transition"

# Subsystem registration and status fields
SUBSYSTEM_NAME = "subsystem"
SUBSYSTEM_LIFECYCLE_STATUS = "lifecycle"
SUBSYSTEM_PUB_TOPIC_LIST = "publishing"
SUBSYSTEM_SUB_TOPIC_LIST = "subscribing"
SUBSYSTEM_CONTROL_PORT = "control_port"
SUBSYSTEM_CONTROL_ENDPOINT_LIST = "controls"
SUBSYSTEM_PROCEDURE_LIST = "procedures"
SUBSYSTEM_OPERATION_LIST = "operations"
SUBSYSTEM_CONTROL_CONTRACTS = "control_contracts"
SUBSYSTEM_PROCEDURE_CONTRACTS = "procedure_contracts"
SUBSYSTEM_OPERATION_CONTRACTS = "operation_contracts"
EVENT_STARTUP_BARRIER_MS = "event_startup_barrier_ms"

# Event slot allocation and subscription fields
PUBLISHER_NAME = "publisher"
TOPIC_NAME = "topic"
TOPIC_SLOT_SIZE = "size"
PRIORITY = "priority"
MODE = "mode"
QUEUE_DEPTH = "queue_depth"
ENTRY_SLOT_COUNT = "entry_slot_count"
PUBLISH_SLOT_INDEX = "publish_slot_index"
TOPIC_BUFFER_POOL_NAME = "topic_buffer_pool_name"
TOPIC_BUFFER_POOL_SLOT_SIZE = "topic_buffer_pool_slot_size"
TOPIC_BUFFER_POOL_SLOT_COUNT = "topic_buffer_pool_slot_count"
LOCK_NAME = "lock_name"
SUBSCRIBERS = "subscribers"
