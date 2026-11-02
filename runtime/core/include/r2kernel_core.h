#ifndef R2KERNEL_CORE_H
#define R2KERNEL_CORE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define R2KERNEL_SEQ_READY 0
#define R2KERNEL_SEQ_WRITING 1

/* Primitive type ids. The array ids (9..14) select the element type of
 * r2kernel_codec_{encode,decode}_primitive_vector. */
#define R2KERNEL_TYPE_INT32 1
#define R2KERNEL_TYPE_UINT32 2
#define R2KERNEL_TYPE_INT64 3
#define R2KERNEL_TYPE_FLOAT32 4
#define R2KERNEL_TYPE_FLOAT64 5
#define R2KERNEL_TYPE_BOOL 6
#define R2KERNEL_TYPE_STRING 7
#define R2KERNEL_TYPE_BYTES 8
#define R2KERNEL_TYPE_INT32_ARRAY 9
#define R2KERNEL_TYPE_UINT32_ARRAY 10
#define R2KERNEL_TYPE_INT64_ARRAY 11
#define R2KERNEL_TYPE_FLOAT32_ARRAY 12
#define R2KERNEL_TYPE_FLOAT64_ARRAY 13
#define R2KERNEL_TYPE_BOOL_ARRAY 14

// Opaque handles
typedef struct R2KernelShmHandle R2KernelShmHandle;
typedef struct R2KernelLockHandle R2KernelLockHandle;

// Event client (Rust core)
typedef struct R2KernelEventClient R2KernelEventClient;
typedef struct R2KernelEventTransport R2KernelEventTransport;
typedef struct R2KernelEventSessionSetup R2KernelEventSessionSetup;
typedef struct R2KernelEventSession R2KernelEventSession;
typedef struct R2KernelEventApplyPlan R2KernelEventApplyPlan;

// Immutable prepared codecs. Context snapshots all effective schema sources once.
// roots_json NULL captures normal effective roots; "[]" uses shipped standards only.
// Explicit JSON array roots preserve precedence and do not append ambient roots.
// Initialization rejects cyclic/repeated canonical directories within each root,
// directories
// deeper than 128 levels, mismatched YAML path IDs, and prepared message graphs
// or type expressions deeper than 128 levels (-1; filesystem errors return -3).
#define R2KERNEL_MAX_PREPARED_SCHEMA_DEPTH 128
typedef struct R2KernelSchemaContext R2KernelSchemaContext;
typedef struct R2KernelPreparedCodec R2KernelPreparedCodec;
typedef struct R2KernelCodecOutput R2KernelCodecOutput;
#define R2KERNEL_NODE_ABSENT 0
#define R2KERNEL_NODE_MESSAGE 1
#define R2KERNEL_NODE_ARRAY 2
#define R2KERNEL_NODE_UINT8 3
#define R2KERNEL_NODE_UINT16 4
#define R2KERNEL_NODE_INT32 5
#define R2KERNEL_NODE_UINT32 6
#define R2KERNEL_NODE_INT64 7
#define R2KERNEL_NODE_FLOAT32 8
#define R2KERNEL_NODE_FLOAT64 9
#define R2KERNEL_NODE_BOOL 10
#define R2KERNEL_NODE_STRING 11
#define R2KERNEL_NODE_BYTES 12
// Node 0 is the message root. Message children are fields in schema order,
// including ABSENT optional fields. Array children are elements in order.
// Child indexes identify a contiguous run in the flat node buffer.
// bits stores scalar wire bits (signed integers use two's complement).
// STRING/BYTES use data/data_len; string must be valid UTF-8. No JSON involved.
// Packet inputs (including node spans) must not overlap output allocation;
// detected overlap returns -1 before mutating output.
typedef struct R2KernelCodecNode {
    uint32_t kind;
    uint32_t children_start;
    size_t children_count;
    uint64_t bits;
    const uint8_t* data;
    size_t data_len;
} R2KernelCodecNode;
int r2kernel_schema_context_create(const char* roots_json, R2KernelSchemaContext** out);
void r2kernel_schema_context_free(R2KernelSchemaContext* context);
// Cold-path immutable selected YAML (all spec directories), -2 if absent.
int r2kernel_schema_context_spec_text(const R2KernelSchemaContext* context,
    const char* relative_path, R2KernelCodecOutput* output);
int r2kernel_codec_prepare(const R2KernelSchemaContext* context, const char* message_ref,
                          R2KernelPreparedCodec** out);
void r2kernel_codec_prepared_free(R2KernelPreparedCodec* codec);
// Owns reusable per-interface scratch; never share concurrently. Handles/plans
// are immutable and may be shared concurrently. Codec survives context free.
int r2kernel_codec_output_create(R2KernelCodecOutput** out);
void r2kernel_codec_output_free(R2KernelCodecOutput* output);
int r2kernel_codec_prepared_describe(const R2KernelPreparedCodec* codec,
                                   R2KernelCodecOutput* output);
// describe JSON: schema, graph, fingerprint (SHA-256 effective wire graph),
// source ("standard" for build-prepared equivalent graph, else "dynamic").
int r2kernel_codec_prepared_encode_json(const R2KernelPreparedCodec* codec,
    const uint8_t* json, size_t json_len, R2KernelCodecOutput* output);
int r2kernel_codec_prepared_decode_json(const R2KernelPreparedCodec* codec,
    const uint8_t* payload, size_t payload_len, R2KernelCodecOutput* output);
int r2kernel_codec_prepared_encode_nodes(const R2KernelPreparedCodec* codec,
    const R2KernelCodecNode* nodes, size_t node_count, R2KernelCodecOutput* output);
int r2kernel_codec_prepared_decode_nodes(const R2KernelPreparedCodec* codec,
    const uint8_t* payload, size_t payload_len, R2KernelCodecOutput* output);
// Results are valid until the next operation/free on this output. Byte output
// is owned by output; decoded STRING/BYTES spans borrow caller payload, which
// must remain alive while nodes are read. Accessors perform no transformation.
const uint8_t* r2kernel_codec_output_bytes(const R2KernelCodecOutput* output, size_t* len);
const R2KernelCodecNode* r2kernel_codec_output_nodes(const R2KernelCodecOutput* output, size_t* count);

// Canonical schema / codec
/*
 * For schema and codec output buffers, out_len reports the required byte count
 * on success and on a buffer-too-small result. A null out_buf queries the
 * required size and returns success without copying. A non-null undersized
 * output buffer is left untouched.
 */
int r2kernel_schema_get_message_json(
    const char* message_ref,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_schema_get_message_set_json(
    const char* namespace_ref,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_encode_message_json(
    const char* message_ref,
    const uint8_t* json,
    size_t json_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_message_json(
    const char* message_ref,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
/*
 * Single-conversion forms of the two calls above. Each call resolves
 * message_ref against the effective spec roots again (edits are observed on
 * the next call) and performs exactly one conversion, writing the result into
 * a caller-owned R2KernelCodecOutput (read it with r2kernel_codec_output_bytes;
 * valid until the next operation on that output). The input is consumed before
 * the output changes, so it may point at the output's previous bytes. On
 * failure the output is emptied and the same negative codes are returned.
 */
int r2kernel_codec_encode_message_json_into(
    const char* message_ref,
    const uint8_t* json,
    size_t json_len,
    R2KernelCodecOutput* output
);
int r2kernel_codec_decode_message_json_into(
    const char* message_ref,
    const uint8_t* payload,
    size_t payload_len,
    R2KernelCodecOutput* output
);
int r2kernel_codec_encode_primitive_vector(
    uint8_t type_id,
    const uint8_t* data,
    size_t data_len,
    size_t count,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_primitive_vector(
    uint8_t type_id,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len,
    size_t* out_count
);
int r2kernel_codec_encode_timing_request_json(
    const uint8_t* header_json,
    size_t header_json_len,
    const char* payload_ref,
    const uint8_t* payload_json,
    size_t payload_json_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_timing_request_json(
    const uint8_t* wire,
    size_t wire_len,
    uint8_t* out_header_json,
    size_t out_header_json_cap,
    size_t* out_header_json_len,
    uint8_t* out_payload_ref,
    size_t out_payload_ref_cap,
    size_t* out_payload_ref_len,
    uint8_t* out_payload_json,
    size_t out_payload_json_cap,
    size_t* out_payload_json_len
);
int r2kernel_codec_encode_timing_response_json(
    const uint8_t* header_json,
    size_t header_json_len,
    const char* payload_ref,
    const uint8_t* payload_json,
    size_t payload_json_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_timing_response_json(
    const uint8_t* wire,
    size_t wire_len,
    uint8_t* out_header_json,
    size_t out_header_json_cap,
    size_t* out_header_json_len,
    uint8_t* out_payload_ref,
    size_t out_payload_ref_cap,
    size_t* out_payload_ref_len,
    uint8_t* out_payload_json,
    size_t out_payload_json_cap,
    size_t* out_payload_json_len
);
/* SDTR Control envelopes. Wire version 2 is the only encoded and accepted
 * version; decoding any other version returns InvalidArgument (-1). */
int r2kernel_codec_encode_control_request_v2(
    uint64_t request_id,
    int64_t generated_at_sync_ns,
    int64_t sent_at_sync_ns,
    int64_t execute_at_sync_ns,
    int64_t valid_until_sync_ns,
    int64_t max_lateness_ns,
    uint16_t priority,
    uint8_t late_policy,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_control_request_v2(
    const uint8_t* wire,
    size_t wire_len,
    uint64_t* out_request_id,
    int64_t* out_generated_at_sync_ns,
    int64_t* out_sent_at_sync_ns,
    int64_t* out_execute_at_sync_ns,
    int64_t* out_valid_until_sync_ns,
    int64_t* out_max_lateness_ns,
    uint16_t* out_priority,
    uint8_t* out_late_policy,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);
int r2kernel_codec_encode_control_response_v2(
    uint64_t request_id,
    int64_t responded_at_sync_ns,
    int64_t accepted_at_sync_ns,
    int64_t scheduled_for_sync_ns,
    int64_t dispatched_at_sync_ns,
    int64_t sampled_at_sync_ns,
    int64_t completed_at_sync_ns,
    int64_t lateness_ns,
    int32_t status_code,
    uint8_t final_status,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);
int r2kernel_codec_decode_control_response_v2(
    const uint8_t* wire,
    size_t wire_len,
    uint64_t* out_request_id,
    int64_t* out_responded_at_sync_ns,
    int64_t* out_accepted_at_sync_ns,
    int64_t* out_scheduled_for_sync_ns,
    int64_t* out_dispatched_at_sync_ns,
    int64_t* out_sampled_at_sync_ns,
    int64_t* out_completed_at_sync_ns,
    int64_t* out_lateness_ns,
    int32_t* out_status_code,
    uint8_t* out_final_status,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);

// Shared memory
#define R2KERNEL_CLOCK_SOURCE_LOCAL_UNSYNCED 0
#define R2KERNEL_CLOCK_SOURCE_LOCAL_SYSTEM 1
#define R2KERNEL_CLOCK_SOURCE_SOFTWARE_PTP 2
#define R2KERNEL_CLOCK_SOURCE_HARDWARE_PTP 3
#define R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL 4
#define R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL_HW 5

typedef struct R2KernelClockSnapshot {
    int64_t realtime_ns;
    int64_t monotonic_ns;
    int64_t logical_ns;
    int64_t offset_ns;
    int64_t rtt_ns;
    int64_t uncertainty_ns;
    uint8_t source;
    uint8_t stable;
    uint8_t accepted_for_one_way;
    uint8_t reserved;
} R2KernelClockSnapshot;

int64_t r2kernel_clock_realtime_ns(void);
int64_t r2kernel_clock_monotonic_ns(void);
int64_t r2kernel_clock_now_ns(void);
int r2kernel_clock_snapshot(R2KernelClockSnapshot* out_snapshot);
int r2kernel_clock_configure(
    int64_t offset_ns,
    int64_t rtt_ns,
    int64_t uncertainty_ns,
    uint8_t source,
    uint8_t stable,
    uint8_t accepted_for_one_way
);
int r2kernel_clock_reset(void);
int r2kernel_shm_create(const char* name, size_t slot_size, size_t slot_count, R2KernelShmHandle** out_handle);
int r2kernel_shm_open(const char* name, size_t slot_size, size_t slot_count, R2KernelShmHandle** out_handle);
int r2kernel_shm_close(R2KernelShmHandle* handle);
int r2kernel_shm_unlink(const char* name);

/* Named SHM operations synchronize one slot at a time on supported Linux
 * runtimes: fixed per-slot local mutex + blocking exclusive OFD byte range
 * [slot_index*slot_size, (slot_index+1)*slot_size). Geometry must fit off_t.
 * Unsupported kernels/platforms fail; no flock/process-lock fallback.
 * Header offsets and arbitrary slot geometry >=64 are unchanged. Stop and
 * upgrade every participant together; mixed protocols/direct unsynchronized
 * mmap access are unsupported. No C call retains ownership after return.
 * Do not close concurrently, reuse a handle after fork, or treat dup as an
 * independent owner. Valid caller buffers must not alias the SHM mapping.
 *
 * Slot access is publish/read_snapshot/reset_slot only; each is one complete
 * operation under the slot guard. */
typedef struct R2KernelShmSlotInfo {
    uint8_t seq;
    uint8_t reserved[3];
    uint32_t length;
    uint64_t generation;
    int64_t timestamp;
    int64_t prev_timestamp;
    int64_t published_timestamp;
} R2KernelShmSlotInfo;
/* Publication validates capacity before mutation and commits READY last.
 * Empty payloads may use data=NULL. Previous timestamp is captured in-slot. */
int r2kernel_shm_publish(const R2KernelShmHandle* handle, size_t slot_index,
    const uint8_t* data, size_t len, uint64_t generation, int64_t timestamp,
    int64_t published_timestamp);
/* NULL out_payload selects a metadata-only snapshot, including WRITING state.
 * A non-NULL payload requires READY. Non-NULL expected_generation rejects a
 * changed generation before copying. Not-ready/mismatch returns NotFound (-2).
 * BufferTooSmall (-4) reports required length in out_info and leaves payload
 * untouched, including corrupt stored length; callers must bound that length
 * by slot geometry before allocation. Other failures do not yield a valid
 * snapshot. Zero-length full reads may use a non-NULL dummy buffer with
 * out_cap=0. reserved bytes are always zero. Nonpositive stored publication
 * timestamp falls back to the observed timestamp from the same snapshot. */
int r2kernel_shm_read_snapshot(const R2KernelShmHandle* handle, size_t slot_index,
    const uint64_t* expected_generation, R2KernelShmSlotInfo* out_info,
    uint8_t* out_payload, size_t out_cap);
int r2kernel_shm_reset_slot(const R2KernelShmHandle* handle, size_t slot_index);

// Event client
/* Heap retention is bounded by positive integer environment settings read at
 * create: R2KERNEL_EVENT_CLIENT_QUEUE_MAX_MESSAGES (1024), and
 * R2KERNEL_EVENT_CLIENT_QUEUE_MAX_BYTES (67108864). Invalid values fail create.
 * Bytes count retained message structures and String/Vec capacities; deque
 * storage is additionally bounded by the message-count limit. Oldest events
 * are dropped for space; individually oversized events are rejected without
 * evicting others. skipped_generations includes both overwrite and heap drops.
 * Receive and close release retained messages and byte charges. */
int r2kernel_event_client_create(const char* shm_name, size_t slot_size, size_t slot_count, R2KernelEventClient** out_handle);
/* Consumes a valid handle on every return, including cleanup errors. The handle
 * is invalid afterward: discard it and never retry close on the same handle.
 * Thread safety: close must not run concurrently with any other call on the
 * same handle; the caller must ensure every other call on that handle has
 * returned before closing it. */
int r2kernel_event_client_close(R2KernelEventClient* handle);
int r2kernel_event_client_subscribe(R2KernelEventClient* handle, const char* topic, const char* publisher, size_t slot_index, const char* lock_name);
int r2kernel_event_client_subscribe_ex(R2KernelEventClient* handle, const char* topic, const char* publisher, size_t slot_index, size_t queue_depth, size_t entry_slot_count, const char* lock_name);
int r2kernel_event_client_subscribe_latest_ex(R2KernelEventClient* handle, const char* topic, const char* publisher, size_t slot_index, size_t queue_depth, size_t entry_slot_count, const char* lock_name);
/*
 * Receives the next queued Event. A null out_topic/out_publisher/out_payload
 * skips that field (its length is reported as 0). When a non-null buffer is
 * too small, the Event stays queued, every required length is reported in
 * out_topic_len/out_publisher_len/out_payload_len and BufferTooSmall (-4) is
 * returned; retry with buffers of at least those sizes to receive it. A caller
 * that retries without growing receives BufferTooSmall again.
 */
int r2kernel_event_client_recv(
    R2KernelEventClient* handle,
    int64_t timeout_ms,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_publisher,
    size_t out_publisher_cap,
    size_t* out_publisher_len,
    int64_t* out_ts,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);
int r2kernel_event_client_recv_ex(
    R2KernelEventClient* handle,
    int64_t timeout_ms,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_publisher,
    size_t out_publisher_cap,
    size_t* out_publisher_len,
    int64_t* out_observed_at_sync_ns,
    int64_t* out_published_at_sync_ns,
    int64_t* out_received_at_sync_ns,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);
int r2kernel_event_client_get_stats(
    R2KernelEventClient* handle,
    uint64_t* out_queued_messages,
    uint64_t* out_skipped_generations,
    uint64_t* out_stale_wakeups
);

// Event transport
/*
 * Output buffers (out_report, out_peer_id, topic/publisher/payload buffers):
 * out_len reports the required size on success and on a buffer-too-small
 * result. Unlike the schema/codec buffers above, apply_state, accept_peer and
 * the recv_event calls consume what they return, so a null or undersized
 * buffer for a non-empty value never consumes it: the call returns
 * buffer-too-small (-4) with every required length and keeps the item for the
 * next call (accept_peer: the accepted peer id; recv_event: the Event, one
 * kept Event per peer; apply_state: the apply already happened and its
 * connected/disconnected transitions are reported again, ahead of its own,
 * by the next apply_state call).
 * Thread safety: a handle is not thread-safe; serialize every call on it.
 * Signals: a send to a closed peer fails (SystemError -3) and never raises
 * SIGPIPE, so a host does not need to ignore SIGPIPE.
 */
#define R2KERNEL_EVENT_TRANSPORT_ALREADY_CONNECTED 1
int r2kernel_event_transport_create(
    const char* local_host_id,
    size_t queue_capacity,
    int64_t connect_timeout_ms,
    int64_t io_timeout_ms,
    R2KernelEventTransport** out_handle
);
int r2kernel_event_transport_close(R2KernelEventTransport* handle);
int r2kernel_event_transport_bind(
    R2KernelEventTransport* handle,
    const char* host,
    uint16_t port,
    uint16_t* out_port
);
/*
 * Applies the desired peer state (JSON) and writes a JSON report.
 * Per peer, should_connect=false means accept-only (this host never dials it).
 * A non-empty remote_subscriptions is rejected with InvalidArgument: a peer's
 * subscriptions come only from its own subscription frames received on the
 * native session.
 * Report: subscriptions_synced lists the peers that acknowledged this host's
 * latest subscription request with a matching digest on the current
 * connection. subscription_ack_pending lists peers whose acknowledgement of
 * the latest request has not arrived yet. A peer whose subscription requests
 * do not ask for acknowledgement is refused as a protocol error.
 */
int r2kernel_event_transport_apply_state_json(
    R2KernelEventTransport* handle,
    const uint8_t* json,
    size_t json_len,
    uint8_t* out_report,
    size_t out_report_cap,
    size_t* out_report_len
);
/*
 * Accepts one session, waiting at most io_timeout_ms for a connection and for
 * its handshake. Returns 0 with the peer id, NotFound (-2) when no connection
 * arrived, or R2KERNEL_EVENT_TRANSPORT_ALREADY_CONNECTED (1, not an error)
 * when the connection duplicated a live session of the same peer and was
 * closed by duplicate resolution; the peer stays connected on its existing
 * session and out_peer_id is not written. Both hosts resolve a simultaneous
 * dial to the same session: the one initiated by the lexically smaller host id.
 * A plaintext connection never replaces a live secure session of the same peer
 * id; it is refused with SystemError (-3).
 */
int r2kernel_event_transport_accept_peer(
    R2KernelEventTransport* handle,
    uint8_t* out_peer_id,
    size_t out_peer_id_cap,
    size_t* out_peer_id_len
);
int r2kernel_event_transport_enqueue_event(
    R2KernelEventTransport* handle,
    const char* topic,
    const char* publisher,
    uint16_t priority,
    const uint8_t* payload,
    size_t payload_len,
    int64_t timestamp_ns,
    uint16_t mode
);
int r2kernel_event_transport_enqueue_event_ex(
    R2KernelEventTransport* handle,
    const char* topic,
    const char* publisher,
    uint16_t priority,
    const uint8_t* payload,
    size_t payload_len,
    int64_t observed_at_sync_ns,
    int64_t published_at_sync_ns,
    uint16_t mode
);
/*
 * Sends at most limit queued Events. Each send ends within io_timeout_ms: a
 * peer that has not taken a whole Event by then is disconnected, its queued
 * Events are discarded and the call returns SystemError (-3). No send starts
 * once the call has run for io_timeout_ms, so one call takes at most about
 * twice io_timeout_ms; Events left queued go out on the next call.
 */
int r2kernel_event_transport_flush_n(
    R2KernelEventTransport* handle,
    size_t limit,
    size_t* out_flushed
);
int r2kernel_event_transport_recv_event(
    R2KernelEventTransport* handle,
    const char* peer_id,
    uint16_t* out_mode,
    uint16_t* out_priority,
    uint64_t* out_sequence,
    int64_t* out_timestamp_ns,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_publisher,
    size_t out_publisher_cap,
    size_t* out_publisher_len,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);
int r2kernel_event_transport_recv_event_ex(
    R2KernelEventTransport* handle,
    const char* peer_id,
    uint16_t* out_mode,
    uint16_t* out_priority,
    uint64_t* out_sequence,
    int64_t* out_observed_at_sync_ns,
    int64_t* out_published_at_sync_ns,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_publisher,
    size_t out_publisher_cap,
    size_t* out_publisher_len,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);
/*
 * recv_event_ex with an explicit wait instead of io_timeout_ms. timeout_ms 0
 * never blocks: it processes the frames the peer already delivered (applying
 * its subscription requests and acknowledgements) and returns the first Event,
 * or NotFound (-2) when none is available. One call applies at most 64 setup
 * frames; after that many without an Event it returns NotFound and the next
 * call continues. Each call that finds nothing costs one non-blocking socket
 * read. A send-only user calls it with 0 for every
 * connected peer to keep the forward set current. A positive timeout_ms waits
 * at most that long; a handled signal during the wait neither ends it nor
 * closes the session. A negative value returns InvalidArgument (-1).
 */
int r2kernel_event_transport_recv_event_timeout(
    R2KernelEventTransport* handle,
    const char* peer_id,
    int64_t timeout_ms,
    uint16_t* out_mode,
    uint16_t* out_priority,
    uint64_t* out_sequence,
    int64_t* out_observed_at_sync_ns,
    int64_t* out_published_at_sync_ns,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_publisher,
    size_t out_publisher_cap,
    size_t* out_publisher_len,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len
);

/*
 * Session establishment and apply connects without the handle. accept_peer
 * and apply_state_json wait on the network (accept wait, handshake, outbound
 * connects) while the caller serializes every call on the handle. These calls
 * split that work so the waits run without the handle and only a short step
 * takes it:
 *   accept: session_setup(handle) -> session_setup_accept(setup, timeout_ms)
 *           -> session_peer_id(session) -> transport_install_session(handle);
 *   apply:  apply_begin_json(handle) -> apply_plan_connect(plan)
 *           -> apply_finish(handle).
 * A setup, a session and a plan are independent of the handle and of each
 * other: they may be used from any thread while other calls run on the
 * handle, but each one's own calls must be serialized. A setup taken before
 * bind cannot accept.
 *
 * session_setup_accept waits at most timeout_ms (0 polls without waiting)
 * for a pending connection, then runs its whole handshake (every handshake
 * frame wait and the ServerHello write) within io_timeout_ms of the accept.
 * NotFound (-2): no connection, or no handshake in time.
 * transport_install_session always takes ownership of the session: 0
 * installed, R2KERNEL_EVENT_TRANSPORT_ALREADY_CONNECTED (1) when it was a
 * duplicate closed by duplicate resolution, or an error (refused plaintext
 * session: SystemError -3); for a desired peer it also sends this host's
 * subscription request, as accept_peer does.
 * apply_begin_json does the part of apply_state_json without network waits
 * and returns a plan; apply_plan_connect runs the plan's outbound connects;
 * apply_finish always takes ownership of the plan, installs the connected
 * sessions and writes the report exactly as apply_state_json does (including
 * the buffer-too-small rule). A handle has at most one open plan (begun and
 * neither finished nor closed): while one is open, apply_begin_json and
 * apply_state_json return InvalidArgument (-1), and apply_finish with a plan
 * of another handle returns InvalidArgument and closes that plan. A plan
 * closed without finishing leaves the apply incomplete: the peers its begin
 * already disconnected are listed in "disconnected" by the next apply's
 * report, and the next apply reconciles again.
 */
int r2kernel_event_transport_session_setup(
    R2KernelEventTransport* handle,
    R2KernelEventSessionSetup** out_setup
);
int r2kernel_event_session_setup_close(R2KernelEventSessionSetup* setup);
int r2kernel_event_session_setup_accept(
    R2KernelEventSessionSetup* setup,
    int64_t timeout_ms,
    R2KernelEventSession** out_session
);
int r2kernel_event_session_peer_id(
    const R2KernelEventSession* session,
    uint8_t* out_peer_id,
    size_t out_peer_id_cap,
    size_t* out_peer_id_len
);
int r2kernel_event_session_close(R2KernelEventSession* session);
int r2kernel_event_transport_install_session(
    R2KernelEventTransport* handle,
    R2KernelEventSession* session
);
int r2kernel_event_transport_apply_begin_json(
    R2KernelEventTransport* handle,
    const uint8_t* json,
    size_t json_len,
    R2KernelEventApplyPlan** out_plan
);
int r2kernel_event_apply_plan_connect(R2KernelEventApplyPlan* plan);
int r2kernel_event_transport_apply_finish(
    R2KernelEventTransport* handle,
    R2KernelEventApplyPlan* plan,
    uint8_t* out_report,
    size_t out_report_cap,
    size_t* out_report_len
);
int r2kernel_event_apply_plan_close(R2KernelEventApplyPlan* plan);

// Locks (POSIX semaphores for now)
int r2kernel_lock_create(const char* name, R2KernelLockHandle** out_handle);
int r2kernel_lock_open(const char* name, R2KernelLockHandle** out_handle);
/* Thread safety: close must not run concurrently with any other call on the
 * same handle; the caller must ensure every other call on that handle has
 * returned before closing it. The handle is invalid after close. */
int r2kernel_lock_close(R2KernelLockHandle* handle);
int r2kernel_lock_unlink(const char* name);
int r2kernel_lock_post(R2KernelLockHandle* handle);
int r2kernel_lock_trywait(R2KernelLockHandle* handle);
int r2kernel_lock_wait(R2KernelLockHandle* handle, int64_t timeout_ms);

#ifdef __cplusplus
}
#endif

#endif // R2KERNEL_CORE_H
