#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "r2kernel/export.hpp"

namespace r2kernel::wrapper {

using ByteBuffer = std::vector<std::uint8_t>;
// Encoded message bytes (prepared_codec.hpp).
struct CodecByteView;
using EventPayload = ByteBuffer;
using EventCallback = std::function<void(const EventPayload&, std::int64_t timestamp_ns)>;

struct EventTimingMetadata {
    std::int64_t observed_at_sync_ns = 0;
    std::int64_t published_at_sync_ns = 0;
    std::int64_t received_at_sync_ns = 0;
};

using EventMetadataCallback = std::function<void(const EventPayload&, const EventTimingMetadata&)>;

struct EventPublishSpec {
    std::string topic;
    std::string type;
    std::size_t size = 0;
    int priority = 5;
    std::size_t queue_depth = 0;
};

struct EventSubscribeSpec {
    std::string topic;
    std::string type;
    std::size_t size = 0;
    EventCallback callback;
    EventMetadataCallback metadata_callback;
};

struct ControlSourceAuthority {
    std::string peer;
    std::string source;
    std::string source_session;
    std::string source_reference;
    std::string target_session;
    std::string interface;
    std::string binding_revision;
    std::string source_registration;
    std::string target_registration;
    std::string epoch;
    std::string target;
    std::string topic;
};

// Fields of the Python dict, in its order; kind is always "protocol".
struct ProtocolHostAuthority {
    std::string kind;
    std::string peer;
    std::string epoch;
};
// Request-local verified transport authority, kept out of the by-value request
// and callback types; no SDK-declared name is authenticated.
R2KERNEL_CPP_API std::optional<ControlSourceAuthority> current_control_source_authority();
R2KERNEL_CPP_API std::optional<ProtocolHostAuthority> current_protocol_host_authority();

struct ControlRequest {
    std::string sender;
    std::string topic;
    ByteBuffer payload;
    std::int64_t timestamp_ns = 0;
    float period_ms = 0.0F;
    float deadline_ms = 0.0F;
};

struct ControlResponse {
    std::int64_t timestamp_ns = 0;
    std::uint16_t status = 0;
    ByteBuffer payload;
};

using ControlHandler = std::function<ControlResponse(ControlRequest)>;

// One provider data-worker readback, as Python control_worker_status():
// policy is the OS scheduling policy value (SCHED_OTHER/FIFO/RR), phase is
// "profile_applied", "profile_failed" or "profile_observed" (no profile was
// configured), and error is empty when ready.
struct ControlWorkerStatus {
    std::int64_t tid = 0;
    int policy = -1;
    int priority = -1;
    std::vector<int> cpus;
    bool ready = false;
    std::optional<std::string> error;
    std::string phase;
};

// Control's answer to resolve_control_target, as the Python SDK returns it.
// port is the wire control_port (0 on a local_control route); the
// *_json members carry the provider's advertised maps as JSON objects.
struct ControlRoute {
    std::string subsystem;
    std::string host;
    int port = 0;
    std::string route_kind;
    bool relay_enabled = false;
    std::optional<std::string> peer_id;
    std::string session_id;
    std::optional<std::string> registration_revision;
    std::optional<bool> codec_context_available;
    std::vector<std::string> controls;
    std::vector<std::string> procedures;
    std::vector<std::string> operations;
    std::string control_contracts_json = "{}";
    std::string procedure_contracts_json = "{}";
    std::string operation_contracts_json = "{}";
    std::string codec_metadata_json = "{}";
    std::string contract_codecs_json = "{}";
    // The complete reply object, for fields not modelled above.
    std::string json;
};

struct ControlTimingMetadata {
    std::uint64_t request_id = 0;
    std::string request_id_text;
    std::int64_t generated_at_sync_ns = 0;
    std::int64_t sent_at_sync_ns = 0;
    std::int64_t execute_at_sync_ns = 0;
    std::optional<std::int64_t> valid_until_sync_ns;
    std::optional<std::int64_t> max_lateness_ns;
    int priority = 128;
    std::string late_policy = "execute_immediately";
};

struct ControlExecutionReport {
    std::int64_t accepted_at_sync_ns = 0;
    std::int64_t scheduled_for_sync_ns = 0;
    std::int64_t dispatched_at_sync_ns = 0;
    std::optional<std::int64_t> sampled_at_sync_ns;
    std::int64_t completed_at_sync_ns = 0;
    std::int64_t lateness_ns = 0;
    std::string final_status = "executed";
};

struct ControlResponseMetadata {
    std::uint64_t request_id = 0;
    std::string request_id_text;
    std::int64_t responded_at_sync_ns = 0;
    std::int64_t accepted_at_sync_ns = 0;
    std::int64_t scheduled_for_sync_ns = 0;
    std::int64_t dispatched_at_sync_ns = 0;
    std::optional<std::int64_t> sampled_at_sync_ns;
    std::int64_t completed_at_sync_ns = 0;
    std::int64_t lateness_ns = 0;
    int status_code = 0;
    std::string final_status = "executed";
};

struct DecodedControlResponse {
    ControlResponseMetadata timing;
    ByteBuffer payload;
    std::string payload_json;
};

// With ProcedureOptions::native_payload the params arrive encoded with
// request_type in params_payload and params_json is empty; otherwise
// params_payload is empty.
struct ProcedureRequest {
    std::string sender;
    std::string target;
    std::string procedure;
    std::string request_id;
    std::string params_json = "null";
    ByteBuffer params_payload;
    std::int64_t timestamp_ns = 0;
    int timeout_ms = 1000;
    std::string request_type;
    std::string response_type;
    bool timed = false;
    ControlTimingMetadata timing;
    std::int64_t accepted_at_sync_ns = 0;
};

// A handler may return its result encoded with response_type in
// result_payload instead of result_json (required with
// ProcedureOptions::native_payload); a non-empty result_payload is sent as
// given. The bytes overload of Subsystem::call_procedure returns the encoded
// result there and leaves result_json "null".
struct ProcedureResponse {
    bool ok = false;
    std::string request_id;
    std::string result_json = "null";
    ByteBuffer result_payload;
    std::string error_code;
    std::string error_message;
    std::int64_t timestamp_ns = 0;
    std::optional<ControlExecutionReport> execution_report;
    std::optional<int> status_code;
    std::string status_message;
};

using ProcedureHandler = std::function<ProcedureResponse(ProcedureRequest)>;

struct OperationRequest {
    std::string sender;
    std::string target;
    std::string operation;
    std::string operation_id;
    std::string request_id;
    std::string params_json = "null";
    std::int64_t timestamp_ns = 0;
    int timeout_ms = 1000;
    std::string request_type;
    std::string feedback_type;
    std::string result_type;
    bool timed = false;
    ControlTimingMetadata timing;
    std::int64_t accepted_at_sync_ns = 0;
};

struct OperationStartResponse {
    bool ok = false;
    bool accepted = false;
    std::string request_id;
    std::string operation_id;
    std::string state;
    std::string error_code;
    std::string error_message;
    std::int64_t timestamp_ns = 0;
};

struct OperationQueryResponse {
    bool ok = false;
    std::string request_id;
    std::string operation_id;
    std::string operation;
    std::string state;
    bool cancel_requested = false;
    long long feedback_count = 0;
    std::string result_json = "null";
    std::string state_payload_json = "null";
    std::string error_code;
    std::string error_message;
    std::int64_t created_ts = 0;
    std::int64_t updated_ts = 0;
    std::int64_t timestamp_ns = 0;
};

// How an Operation event payload travelled: absent, JSON text, encoded with
// the message type named in payload_type, or bytes as given. The Python SDK
// exposes the same distinction through the payload's Python type (None, the
// JSON value, the decoded message, bytes).
enum class OperationEventPayloadEncoding { none, json, typed, raw };

// payload_json is the payload as JSON text: the JSON as sent, the codec's JSON
// for a typed payload, a JSON string of lowercase hex for raw bytes (as the
// codec writes bytes fields), or "null" without a payload. payload_encoding
// tells a raw-bytes payload from a JSON string payload; payload_bytes holds the
// raw bytes (empty for the other encodings). payload_type is the type the
// provider declared for this event kind, empty when it declared none.
// A subscription with OperationSubscribeOptions::payload_json false instead
// keeps typed and raw payloads as bytes in payload_bytes (a typed payload as
// encoded with payload_type) and leaves payload_json empty for them.
struct OperationEventMessage {
    std::string operation_id;
    std::string operation;
    std::string target;
    std::string kind;
    std::string state;
    std::string payload_json = "null";
    std::string payload_type;
    OperationEventPayloadEncoding payload_encoding = OperationEventPayloadEncoding::none;
    ByteBuffer payload_bytes;
    bool cancel_requested = false;
    std::int64_t timestamp_ns = 0;
};

using OperationEventCallback = std::function<void(const OperationEventMessage&)>;

// Operation event subscription. size is the Event buffer size. payload_json
// true decodes a typed payload into OperationEventMessage::payload_json with
// this subscriber's prepared codec (and writes raw bytes as hex JSON);
// false skips that JSON conversion: typed and raw payloads arrive only in
// payload_bytes, to be read with PreparedCodec::decode_nodes into a reused
// CodecWorkspace. JSON payloads arrive as JSON text either way.
struct OperationSubscribeOptions {
    std::size_t size = 64U * 1024U;
    bool payload_json = true;
};

struct LifecycleTransitionRequest {
    std::string transition;
    std::string request_id;
    std::string reason;
    std::string previous_lifecycle;
};

using LifecycleTransitionHandler = std::function<void(const LifecycleTransitionRequest&)>;

class R2KERNEL_CPP_API OperationContext {
public:
    virtual ~OperationContext() = default;

    virtual const std::string& operation_id() const = 0;
    virtual bool is_cancel_requested() const = 0;
    virtual void publish_feedback(std::string payload_json) = 0;
    virtual void publish_state(std::string state, std::string payload_json = "null") = 0;
    virtual void publish_result(std::string payload_json) = 0;
    virtual void succeed(std::string result_json = "null") = 0;
    virtual void fail(
        std::string error_code,
        std::string error_message,
        std::string payload_json = "null"
    ) = 0;
    virtual void canceled(std::string payload_json = "null") = 0;

    // JSON-free typed payloads: bytes already encoded with the message type the
    // Operation declares for that event (feedback_type, state_type,
    // result_type), for example PreparedCodec::encode_nodes output. They are
    // published as given, with no JSON conversion and no re-validation, so the
    // event equals the one the JSON overload writes for the same value. A
    // declared type that is not a message reference throws
    // std::invalid_argument. The view is read before the call returns; an
    // Operation query decodes a stored typed result or state payload to JSON
    // only when asked.
    virtual void publish_feedback(const CodecByteView& typed_payload) = 0;
    virtual void publish_state(std::string state, const CodecByteView& typed_payload) = 0;
    virtual void publish_result(const CodecByteView& typed_payload) = 0;
    virtual void succeed(const CodecByteView& typed_result) = 0;
    virtual void fail(std::string error_code, std::string error_message, const CodecByteView& typed_payload) = 0;
    virtual void canceled(const CodecByteView& typed_payload) = 0;
};

using OperationHandler = std::function<std::optional<std::string>(OperationRequest, OperationContext&)>;

// Refusal reported by the control backend for a subsystem-owned request such
// as binding_self_set. reason() is the backend's stable reason code, or empty
// for an authentication or malformed-request error; what() is
// "<reason>: <error>" (only "<error>" when reason() is empty).
class R2KERNEL_CPP_API ControlRequestError : public std::runtime_error {
public:
    ControlRequestError(std::string reason, const std::string& error);
    ~ControlRequestError() override;
    const std::string& reason() const noexcept { return reason_; }

private:
    std::string reason_;
};

// Byte-level runtime client used by Subsystem. Members are grouped as
// identity/clock, lifecycle, Events, Control server, Control client,
// preparation/worker and status publication.
class R2KERNEL_CPP_API CoreClient {
public:
    virtual ~CoreClient() = default;

    // Identity and clock.
    virtual std::string session_id() const { return ""; }
    virtual std::int64_t now_ns() const = 0;
    virtual bool refresh_clock_status() = 0;

    // Lifecycle.
    virtual void set_lifecycle_transition_handler(LifecycleTransitionHandler handler) {
        (void)handler;
    }

    // Events. A negative priority publishes with the registered publication's priority.
    virtual void start_event_transport() = 0;
    virtual void stop_event_transport() = 0;
    virtual bool register_event_publish(const EventPublishSpec& spec) = 0;
    virtual bool register_event_subscribe(const EventSubscribeSpec& spec) = 0;
    virtual bool publish_event(const std::string& topic,
                               const EventPayload& payload,
                               std::int64_t timestamp_ns,
                               int priority) = 0;

    // Control server. bind_control returns the bound (or requested) port and
    // throws on failure.
    virtual int bind_control(int port, const std::string& host) = 0;
    virtual bool start_control_server(const ControlHandler& handler) = 0;
    virtual void stop_control_server() = 0;
    virtual int get_control_port() const = 0;

    // Control client. resolve_control_target returns std::nullopt when Control
    // has no usable route to target. Subsystem passes an implementation other
    // than the native client the source-wrapped topic Control routes on
    // ("__r2kernel_source_v1__:<hex source>:<topic>") for send_control,
    // transact_control, transact_control_result, wait_control_reclaimed and
    // prepare_control; the native client wraps the topic itself.
    virtual std::optional<ControlRoute> resolve_control_target(const std::string& target) = 0;
    virtual bool connect_control(const std::string& name,
                                 const std::string& host,
                                 int port) = 0;
    virtual bool handshake_control(const std::string& target, int timeout_ms) = 0;
    virtual bool send_control(const std::string& target,
                              const std::string& topic,
                              const ByteBuffer& payload,
                              std::int64_t timestamp_ns,
                              float period_ms,
                              float deadline_ms,
                              int priority) = 0;
    virtual std::optional<ControlResponse> recv_control(const std::string& target,
                                                        int timeout_ms) = 0;
    // Synchronous route operation. The default composes target-only send/recv;
    // native clients override this.
    virtual std::optional<ControlResponse> transact_control(
        const std::string& target, const std::string& topic, const ByteBuffer& payload,
        std::int64_t timestamp_ns, float period_ms, float deadline_ms,
        int timeout_ms, int priority) {
        if (!send_control(target, topic, payload, timestamp_ns, period_ms, deadline_ms, priority))
            return std::nullopt;
        return recv_control(target, timeout_ms);
    }
    // Returns owned response bytes after CONSUMED. Reclamation errors are
    // surfaced by wait_control_reclaimed and the next call on this lane.
    virtual std::optional<ControlResponse> transact_control_result(
        const std::string& target, const std::string& topic, const ByteBuffer& payload,
        std::int64_t timestamp_ns, float period_ms, float deadline_ms,
        int timeout_ms, int priority) {
        (void)target; (void)topic; (void)payload; (void)timestamp_ns;
        (void)period_ms; (void)deadline_ms; (void)timeout_ms; (void)priority;
        throw std::runtime_error("CoreClient does not support response-ready Control completion");
    }
    virtual void wait_control_reclaimed(const std::string& target, const std::string& topic, int timeout_ms) {
        (void)target; (void)topic; (void)timeout_ms;
        throw std::runtime_error("CoreClient does not support Control reclamation completion");
    }

    // Preparation and provider worker profile. Readiness includes the
    // authenticated provider's request mapping and bounded response allocation.
    virtual bool prepare_control(const std::string& target, const std::string& topic,
        std::size_t request_capacity, std::size_t response_capacity,
        int timeout_ms = 1000, bool result_ready = false) {
        (void)target; (void)topic; (void)request_capacity; (void)response_capacity;
        (void)timeout_ms; (void)result_ready;
        throw std::runtime_error("CoreClient does not support provider-ready Control preparation");
    }
    // Prepared callers require explicit peer schema verification. Other clients
    // must implement this rather than silently claiming native safety.
    virtual void prepare_control_route(const std::string& target, const std::string& topic,
        const std::string& request_ref, const std::string& request_fingerprint,
        const std::string& response_ref, const std::string& response_fingerprint) {
        (void)target; (void)topic; (void)request_ref; (void)request_fingerprint;
        (void)response_ref; (void)response_fingerprint;
        throw std::runtime_error("CoreClient does not support prepared Control peer identity verification");
    }
    virtual void clear_control_codec_routes() {}
    // policy is "OTHER", "FIFO" or "RR" (case-insensitive, optional "SCHED_" prefix).
    virtual void configure_control_worker(const std::string& policy, int priority, const std::vector<int>& cpus) {
        (void)policy; (void)priority; (void)cpus;
        throw std::runtime_error("CoreClient does not support Control worker configuration");
    }
    virtual std::vector<ControlWorkerStatus> control_worker_status() const {
        throw std::runtime_error("CoreClient does not support Control worker diagnostics");
    }

    // Status publication. Initialization requires a synchronous backend
    // acknowledgment of identity.
    virtual void publish_prepared_status(const std::string& status_json) {
        (void)status_json;
        throw std::runtime_error("CoreClient does not support acknowledged prepared status publication");
    }
};

}  // namespace r2kernel::wrapper
