#pragma once

#include <cstdint>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <functional>
#include <memory>
#include <map>
#include <mutex>
#include <shared_mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "r2kernel/export.hpp"
#include "r2kernel/wrapper/bound_control.hpp"
#include "r2kernel/wrapper/core_client.hpp"
#include "r2kernel/wrapper/device_state.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "r2kernel/wrapper/logger.hpp"
#include "r2kernel/wrapper/manifest.hpp"

namespace r2kernel::core {
class R2KernelClient;
}  // namespace r2kernel::core

namespace r2kernel::wrapper {

namespace detail {
class SubsystemConfig;
// A field of a published sample that must carry the frame of one of the
// provider's spatial ports (an API whose frames.policy is spatial_ports).
struct PublishedFrameCheck {
    std::string field;
    std::size_t field_index = 0;  // wire index of the field in the message
    std::string port;
    std::string frame;
    std::string contract;
};
}  // namespace detail

enum class ClockSource : std::uint8_t {
    LocalUnsynced = 0,
    LocalSystem = 1,
    SoftwarePtp = 2,
    HardwarePtp = 3,
    R2KernelLogical = 4,
    R2KernelLogicalHw = 5,
};

struct ClockSnapshot {
    std::int64_t realtime_ns = 0;
    std::int64_t monotonic_ns = 0;
    std::int64_t logical_ns = 0;
    std::int64_t offset_ns = 0;
    std::int64_t rtt_ns = -1;
    std::int64_t uncertainty_ns = -1;
    ClockSource source = ClockSource::LocalUnsynced;
    bool stable = false;
    bool accepted_for_one_way = false;
};

struct SubsystemOptions {
    std::string name;
    std::string log_level = "INFO";
    std::filesystem::path user_data_path = "./userdata";
    int heartbeat_port = 40000;
    std::filesystem::path manifest_path;
    // Static deployment bindings; empty: R2KERNEL_DEPLOYMENT_MANIFEST, else
    // r2kernel.deployment.yaml next to the subsystem manifest, in its parent
    // directory or in the working directory (first that exists).
    std::filesystem::path deployment_manifest_path;
    std::unordered_map<std::string, std::string> launch_bindings;
    Logger::Sink log_sink;
    std::shared_ptr<CoreClient> core_client;
    std::optional<std::vector<std::string>> schema_roots;
};

struct ControlTimingOptions {
    std::optional<std::int64_t> generated_at_sync_ns;
    std::optional<std::int64_t> execute_after_ns;
    std::optional<std::int64_t> execute_at_sync_ns;
    std::optional<std::int64_t> valid_for_ns;
    std::optional<std::int64_t> valid_until_sync_ns;
    int priority = 128;
    // Exactly "drop", "reject" or "execute_immediately"; any other value is
    // refused with std::invalid_argument before the request is sent.
    std::string late_policy = "execute_immediately";
    std::optional<std::int64_t> max_lateness_ns = 1'000'000;
    std::string correlation_id;
    std::string request_id;
};

struct ControlTransactOptions {
    float period_ms = 0.0F;
    float deadline_ms = 1000.0F;
    int timeout_ms = 1000;
    int priority = -1;
    std::string request_type;
    std::string response_type;
    std::optional<ControlTimingOptions> timing;
};

struct ControlEndpointOptions {
    std::string request_type;
    std::string response_type;
    std::string timing_support = "none";
    // Structured native handlers consume/produce payload bytes with prepared codecs.
    bool native_payload = false;
};

struct ControlEndpointRequest {
    std::string sender;
    std::string topic;
    std::string payload_json;
    ByteBuffer payload;
    std::string payload_type;
    std::string response_type;
    bool timed = false;
    ControlTimingMetadata timing;
    std::int64_t accepted_at_sync_ns = 0;
};

struct ControlEndpointResponse {
    bool ok = true;
    std::uint16_t transport_status = 0;
    ByteBuffer payload;
    std::string payload_json = "null";
    std::string payload_type;
    std::optional<ControlExecutionReport> execution_report;
    std::optional<int> status_code;
    std::string status_message;
};

using ControlEndpointHandler = std::function<ControlEndpointResponse(const ControlEndpointRequest&)>;

struct ProcedureOptions {
    std::string request_type;
    std::string response_type;
    std::string timing_support = "none";
    // The handler consumes ProcedureRequest::params_payload and returns
    // ProcedureResponse::result_payload, both encoded with the declared types,
    // with no JSON conversion on a typed call. Requires both types.
    bool native_payload = false;
};

struct ProcedureCallOptions {
    int timeout_ms = 1000;
    std::string request_type;
    std::string response_type;
    std::optional<ControlTimingOptions> timing;
};

struct OperationOptions {
    std::string request_type;
    std::string feedback_type;
    std::string result_type;
    std::string state_type = "string";
    std::string timing_support = "none";
};

// timeout_ms is sent to the provider as given (null when unset); the caller
// waits at most timeout_ms, or 1000 ms when unset, as in the Python SDK.
struct OperationStartOptions {
    std::optional<int> timeout_ms;
    std::string request_type;
    std::optional<ControlTimingOptions> timing;
};

// A timing request or response wire object split into its parts, as the
// Python SDK's unpack_timing_request/unpack_timing_response tuple
// (header, payload_ref, payload). Header and payload are JSON object text.
struct TimingEnvelope {
    std::string header_json;
    std::string payload_ref;
    std::string payload_json;
};

struct ValidationIssue {
    std::string level;
    std::string code;
    std::string message;
    std::string api_id;
};

struct R2KERNEL_CPP_API ContractValidationReport {
    std::vector<std::string> api_ids;
    std::vector<std::string> component_ids;
    std::vector<ValidationIssue> issues;

    bool ok() const;
    std::string format_errors() const;
};

struct FrameTransform {
    std::string parent_frame;
    std::string child_frame;
    std::array<double, 3> translation {0.0, 0.0, 0.0};
    std::array<double, 4> rotation_xyzw {0.0, 0.0, 0.0, 1.0};
    std::int64_t observed_at_sync_ns = 0;
    bool is_static = false;
    std::uint32_t stale_after_ms = 3000;
};

class R2KERNEL_CPP_API Subsystem {
public:
    using TimestampGetter = std::function<std::int64_t()>;

    explicit Subsystem(SubsystemOptions options);
    virtual ~Subsystem();

    const std::string& name() const noexcept { return name_; }
    const std::filesystem::path& user_data_path() const noexcept { return user_data_path_; }
    int heartbeat_port() const noexcept { return heartbeat_port_; }
    DeviceState state() const noexcept { return state_; }
    const std::vector<std::string>& declared_apis() const noexcept { return declared_apis_; }
    const std::vector<std::string>& declared_components() const noexcept { return declared_components_; }
    const std::filesystem::path& manifest_path() const noexcept { return manifest_path_; }
    // The loaded subsystem manifest (empty name when none was found, which
    // only a non-strict contract policy allows) and the deployment manifest
    // found at construction, if any.
    const SubsystemManifest& subsystem_manifest() const noexcept { return subsystem_manifest_; }
    const std::optional<DeploymentManifest>& deployment_manifest() const noexcept { return deployment_manifest_; }
    // Endpoints of endpoint_kind ("publishes", "subscribes", "controls",
    // "procedures" or "operations") that this subsystem's manifest and spec
    // contracts allow it to register, sorted; empty for another kind.
    std::vector<std::string> declared_runtime_endpoints(const std::string& endpoint_kind) const;

    // Bindings of this subsystem's required interfaces (Python SDK
    // launch_bindings, runtime_bindings, effective_bindings, resolve_binding,
    // resolve_runtime_binding, binding_target). Control is asked once per call
    // (1 s, or until deadline).
    //
    // launch_bindings: SubsystemOptions::launch_bindings and R2KERNEL_BINDINGS.
    // runtime_bindings: Control's current rows for declared requirements
    // (empty when Control is unavailable). effective_bindings: launch bindings
    // overridden by runtime bindings.
    std::map<std::string, BindingReference> launch_bindings() const;
    std::map<std::string, BindingReference> runtime_bindings() const;
    std::map<std::string, BindingReference> effective_bindings() const;
    // The runtime binding when Control has one in state bound (an explicit
    // runtime unbind or a non-callable state throws std::runtime_error);
    // otherwise the launch binding, else the deployment manifest binding.
    // With a deadline only a ready runtime binding is accepted: a missing
    // one throws std::runtime_error and an expired deadline throws
    // std::runtime_error("Control binding resolution deadline exceeded").
    // An undeclared requirement throws std::runtime_error, a deployment
    // resolution failure ManifestError.
    BoundInterface resolve_binding(const std::string& requirement,
                                   std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt) const;
    // Only the actual runtime binding, never launch or deployment data:
    // std::nullopt while it is not bound or its source registration is
    // pending; transport, protocol and binding errors throw std::runtime_error.
    std::optional<BoundInterface> resolve_runtime_binding(
        const std::string& requirement,
        std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt) const;
    // resolve_binding(requirement, deadline).target().
    std::string binding_target(const std::string& requirement,
                               std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt) const;

    // Calls on the target of a required interface binding (Python SDK
    // transact_bound_control, call_bound_procedure, start_bound_operation).
    // One invocation budget (the call's timeout, for Control also its
    // deadline_ms) starts at entry and covers binding_target(binding,
    // deadline) and the call: a binding that is not ready, an unavailable
    // Control or a spent budget throws std::runtime_error and sends nothing.
    // Otherwise each behaves as transact_control, call_procedure and
    // start_operation on that target.
    std::optional<ControlResponse> transact_bound_control(const std::string& binding, const std::string& topic,
                                                          const ByteBuffer& payload, float period_ms,
                                                          float deadline_ms, int timeout_ms = 1000,
                                                          int priority = -1);
    std::optional<ControlResponse> transact_bound_control(const std::string& binding, const std::string& topic,
                                                          const ByteBuffer& payload,
                                                          const ControlTransactOptions& options);
    std::optional<ControlResponse> transact_bound_control(const std::string& binding, const std::string& topic,
                                                          const std::string& payload_json,
                                                          const ControlTransactOptions& options);
    std::optional<ProcedureResponse> call_bound_procedure(const std::string& binding, const std::string& procedure,
                                                          const std::string& params_json = "null",
                                                          int timeout_ms = 1000);
    std::optional<ProcedureResponse> call_bound_procedure(const std::string& binding, const std::string& procedure,
                                                          const std::string& params_json,
                                                          const ProcedureCallOptions& options);
    std::optional<OperationStartResponse> start_bound_operation(const std::string& binding,
                                                                const std::string& operation,
                                                                const std::string& params_json = "null",
                                                                std::optional<int> timeout_ms = std::nullopt);
    std::optional<OperationStartResponse> start_bound_operation(const std::string& binding,
                                                                const std::string& operation,
                                                                const std::string& params_json,
                                                                const OperationStartOptions& options);

    Logger& logger() noexcept { return logger_; }
    const Logger& logger() const noexcept { return logger_; }

    void set_core_client(std::shared_ptr<CoreClient> core_client);
    std::shared_ptr<CoreClient> core_client() const noexcept { return core_client_; }
    ContractValidationReport validate_declarations() const;
    // Requires resolvable type metadata on every registered Control endpoint,
    // Procedure and Operation, including internal ones, as the Python SDK's
    // r2kernel.contract.validate_runtime_contract_types does. An empty policy
    // selects R2KERNEL_CONTRACT_POLICY: strict reports errors, warn reports
    // warnings and off skips the check. initialize() applies it before Ready.
    ContractValidationReport validate_runtime_contract_types(const std::string& policy = {}) const;

    void set_frame_transform(FrameTransform transform);
    bool clear_frame_transform(const std::string& child_frame);
    std::vector<FrameTransform> frame_transforms() const;
    // Composed runtime frame graph lookup (Control frame_lookup, one request
    // within 1 s): Control's reply as JSON text, for example the composed
    // transform or {"res":"error",...}; "null" for an empty reply. Frames are
    // qualified "<subsystem>/<frame>" names, or unqualified names unique in the
    // graph. A blank frame throws std::invalid_argument; transport failures
    // and the deadline throw std::runtime_error.
    std::string lookup_frame_transform(const std::string& source_frame, const std::string& target_frame) const;
    // {"transforms":[...]} of the locally owned edges, as JSON text (the
    // heartbeat's spatial_runtime).
    std::string spatial_status_payload() const;

    // Binds this subsystem's own declared requirement to a local provider
    // ("<provider>.<interface>") when control.self_binding.mode is local.
    // Returns the bound binding row as JSON (origin "self"). deadline is
    // absolute; without it each control-socket operation is bounded by 1 s, as
    // in the Python SDK. A backend refusal throws ControlRequestError carrying
    // the backend reason code verbatim; the deadline and transport failures
    // throw std::runtime_error. Self-bindings end with the registration and are
    // not re-requested automatically: call it again from on_start after
    // re-registration. A blank requirement or target throws
    // std::invalid_argument; before initialize() (no core client) or with a
    // non-native core client it throws std::logic_error.
    std::string bind_requirement(const std::string& requirement, const std::string& target,
                                 std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt);
    // Removes this subsystem's self-binding of requirement. Returns false when
    // no self-bound row existed (operator bindings are never touched).
    bool unbind_requirement(const std::string& requirement,
                            std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt);

    // Configuration document <user_data_path>/config (YAML; loaded at
    // construction and created when missing), as the Python SDK's config_*
    // methods. Values are JSON text. A string path is dotted or
    // slash-delimited ("a.b", "a/b"); a vector names literal keys. A missing or
    // null value returns default_json. config_set replaces intermediate values
    // that are not objects, throws std::invalid_argument for value_json that is
    // not JSON, and writes the file when save is true.
    std::string config_data() const;
    std::string config_get(const std::string& path, const std::string& default_json = "null") const;
    std::string config_get(const std::vector<std::string>& keys, const std::string& default_json = "null") const;
    void config_set(const std::string& value_json, const std::string& path, bool save = false);
    void config_set(const std::string& value_json, const std::vector<std::string>& keys, bool save = false);
    std::string config_reload();
    void config_save();

    std::int64_t now_ns() const;
    void set_timestamp_getter(TimestampGetter getter);
    ClockSnapshot clock_snapshot() const;
    static bool configure_clock(std::int64_t offset_ns,
                                std::int64_t rtt_ns = -1,
                                std::int64_t uncertainty_ns = -1,
                                ClockSource source = ClockSource::R2KernelLogical,
                                bool stable = false,
                                bool accepted_for_one_way = false);
    static void reset_clock();

    bool register_event_publish(const EventPublishSpec& spec);
    bool register_event_subscribe(const EventSubscribeSpec& spec);
    // Publishes with the registered publication's priority. For an Event of a
    // provided API whose frames.policy is spatial_ports, a sample whose mapped
    // frame field differs from the provider's spatial port frame is refused:
    // the error is logged as frame_mismatch, nothing is sent, and the call
    // returns false.
    bool publish_event(const std::string& topic,
                       const EventPayload& payload,
                       std::int64_t timestamp_ns = -1);

    // Returns the bound (or requested) port; throws when this role cannot use
    // native Control or no core client is bound.
    int bind_control(int port = 0, const std::string& host = "0.0.0.0") {
        return bind_control_impl(port, host);
    }
    bool register_control_endpoint(const std::string& topic,
                                   const ControlEndpointHandler& handler,
                                   ControlEndpointOptions options = {}) {
        return register_control_endpoint_impl(topic, handler, options);
    }
    bool register_raw_control_endpoint(const std::string& topic, const ControlHandler& handler);
    std::vector<std::string> get_control_info() const { return control_endpoint_names_impl(); }
    std::vector<std::pair<std::string, ControlEndpointOptions>> get_control_contract_info() const {
        return control_contract_info_impl();
    }
    bool start_control_server() { return start_control_server_impl(); }
    bool start_control_server(const ControlHandler& handler) { return start_control_server_impl(handler); }
    void stop_control_server() { stop_control_server_impl(); }
    int get_control_port() const { return control_port_impl(); }

    bool register_procedure(const std::string& name, const ProcedureHandler& handler) {
        return register_procedure_impl(name, handler);
    }
    bool register_procedure(const std::string& name,
                            const ProcedureHandler& handler,
                            ProcedureOptions options) {
        return register_procedure_impl(name, handler, options);
    }
    bool register_typed_procedure(const std::string& name,
                                  const ProcedureHandler& handler,
                                  const std::string& request_type,
                                  const std::string& response_type,
                                  const std::string& timing_support = "none") {
        return register_typed_procedure_impl(name, handler, request_type, response_type, timing_support);
    }
    bool start_procedure_server() { return start_procedure_server_impl(); }
    void stop_procedure_server() { stop_procedure_server_impl(); }
    std::vector<std::string> get_procedure_info() const { return procedure_names_impl(); }
    std::vector<std::pair<std::string, ProcedureOptions>> get_procedure_contract_info() const {
        return procedure_contract_info_impl();
    }
    std::optional<ProcedureResponse> call_procedure(const std::string& target,
                                                    const std::string& procedure,
                                                    const std::string& params_json = "null",
                                                    int timeout_ms = 1000) {
        return call_procedure_impl(target, procedure, params_json, timeout_ms);
    }
    std::optional<ProcedureResponse> call_procedure(const std::string& target,
                                                    const std::string& procedure,
                                                    const std::string& params_json,
                                                    const ProcedureCallOptions& options) {
        return call_procedure_impl(target, procedure, params_json, options);
    }
    // JSON-free typed call: params_payload is encoded with options.request_type
    // and the result arrives encoded with options.response_type in
    // ProcedureResponse::result_payload (result_json stays "null"). Both types
    // are required.
    std::optional<ProcedureResponse> call_procedure(const std::string& target,
                                                    const std::string& procedure,
                                                    const ByteBuffer& params_payload,
                                                    const ProcedureCallOptions& options) {
        return call_procedure_impl(target, procedure, params_payload, options);
    }
    std::optional<ProcedureResponse> call_typed_procedure(const std::string& target,
                                                          const std::string& procedure,
                                                          const std::string& request_type,
                                                          const std::string& response_type,
                                                          const std::string& params_json = "{}",
                                                          int timeout_ms = 1000) {
        return call_typed_procedure_impl(target, procedure, request_type, response_type, params_json, timeout_ms);
    }

    bool register_operation(const std::string& name, const OperationHandler& handler) {
        return register_operation_impl(name, handler);
    }
    bool register_operation(const std::string& name,
                            const OperationHandler& handler,
                            OperationOptions options) {
        return register_operation_impl(name, handler, options);
    }
    bool start_operation_server() { return start_operation_server_impl(); }
    void stop_operation_server() { stop_operation_server_impl(); }
    std::vector<std::string> get_operation_info() const { return operation_names_impl(); }
    std::vector<std::pair<std::string, OperationOptions>> get_operation_contract_info() const {
        return operation_contract_info_impl();
    }
    std::optional<OperationStartResponse> start_operation(const std::string& target,
                                                          const std::string& operation,
                                                          const std::string& params_json = "null",
                                                          std::optional<int> timeout_ms = std::nullopt) {
        return start_operation_impl(target, operation, params_json, timeout_ms);
    }
    std::optional<OperationStartResponse> start_operation(const std::string& target,
                                                          const std::string& operation,
                                                          const std::string& params_json,
                                                          const OperationStartOptions& options) {
        return start_operation_impl(target, operation, params_json, options);
    }
    std::optional<OperationQueryResponse> cancel_operation(const std::string& target,
                                                           const std::string& operation_id,
                                                           int timeout_ms = 1000) {
        return cancel_operation_impl(target, operation_id, timeout_ms);
    }
    std::optional<OperationQueryResponse> query_operation(const std::string& target,
                                                          const std::string& operation_id,
                                                          int timeout_ms = 1000) {
        return query_operation_impl(target, operation_id, timeout_ms);
    }
    bool subscribe_operation_feedback(const std::string& target,
                                      const std::string& operation,
                                      const std::string& operation_id,
                                      const OperationEventCallback& callback,
                                      std::size_t size = 64U * 1024U) {
        return subscribe_operation_event_impl(target, operation, operation_id, "feedback", callback, {size, true});
    }
    bool subscribe_operation_result(const std::string& target,
                                    const std::string& operation,
                                    const std::string& operation_id,
                                    const OperationEventCallback& callback,
                                    std::size_t size = 64U * 1024U) {
        return subscribe_operation_event_impl(target, operation, operation_id, "result", callback, {size, true});
    }
    bool subscribe_operation_state(const std::string& target,
                                   const std::string& operation,
                                   const std::string& operation_id,
                                   const OperationEventCallback& callback,
                                   std::size_t size = 64U * 1024U) {
        return subscribe_operation_event_impl(target, operation, operation_id, "state", callback, {size, true});
    }
    // With options.payload_json false, typed payloads are delivered as their
    // encoded bytes without JSON conversion (see OperationSubscribeOptions).
    bool subscribe_operation_feedback(const std::string& target, const std::string& operation,
                                      const std::string& operation_id, const OperationEventCallback& callback,
                                      const OperationSubscribeOptions& options) {
        return subscribe_operation_event_impl(target, operation, operation_id, "feedback", callback, options);
    }
    bool subscribe_operation_result(const std::string& target, const std::string& operation,
                                    const std::string& operation_id, const OperationEventCallback& callback,
                                    const OperationSubscribeOptions& options) {
        return subscribe_operation_event_impl(target, operation, operation_id, "result", callback, options);
    }
    bool subscribe_operation_state(const std::string& target, const std::string& operation,
                                   const std::string& operation_id, const OperationEventCallback& callback,
                                   const OperationSubscribeOptions& options) {
        return subscribe_operation_event_impl(target, operation, operation_id, "state", callback, options);
    }

    bool connect_control(const std::string& name,
                         const std::string& host,
                         int port) {
        return connect_control_impl(name, host, port);
    }
    std::optional<ControlRoute> resolve_control_target(const std::string& target) {
        return resolve_control_target_impl(target);
    }
    bool handshake_control(const std::string& target, int timeout_ms = 1000) {
        return handshake_control_impl(target, timeout_ms);
    }
    bool prepare_control(const std::string& target, const std::string& topic,
                         std::size_t request_capacity, std::size_t response_capacity,
                         int timeout_ms = 1000, bool result_ready = false);
    // Prepares the Procedure (typed: "procedure.call_typed/<procedure>",
    // otherwise "procedure.call") or Operation start (typed:
    // "operation.start_typed/<operation>", otherwise "operation.start") lane
    // of target after checking that target advertises the name, within one
    // timeout_ms budget, as the Python SDK.
    bool prepare_procedure(const std::string& target, const std::string& procedure,
                           std::size_t request_capacity, std::size_t response_capacity,
                           int timeout_ms = 1000, bool result_ready = false, bool typed = true);
    bool prepare_operation(const std::string& target, const std::string& operation,
                           std::size_t request_capacity, std::size_t response_capacity,
                           int timeout_ms = 1000, bool result_ready = false, bool typed = true);
    // policy is "OTHER", "FIFO" or "RR" (case-insensitive, optional "SCHED_" prefix).
    void configure_control_worker(const std::string& policy, int priority, const std::vector<int>& cpus);
    std::vector<ControlWorkerStatus> control_worker_status() const;
    bool send_control(const std::string& target,
                      const std::string& topic,
                      const ByteBuffer& payload,
                      std::int64_t timestamp_ns = -1,
                      float period_ms = 0.0F,
                      float deadline_ms = 1000.0F,
                      int priority = -1) {
        if (reject_native_control_for_role("send")) {
            return false;
        }
        return send_control_impl(target, topic, payload, timestamp_ns, period_ms, deadline_ms, priority);
    }
    // type is the response payload type: empty or "bytes" keeps the payload
    // bytes; a message reference replaces them with the decoded JSON text
    // (Python returns the decoded value). A payload that does not decode
    // throws std::runtime_error.
    std::optional<ControlResponse> recv_control(const std::string& target,
                                                const std::string& type,
                                                int timeout_ms = 0) {
        if (reject_native_control_for_role("receive")) {
            return std::nullopt;
        }
        return recv_control_impl(target, type, timeout_ms);
    }
    std::optional<ControlResponse> transact_control(const std::string& target,
                                                 const std::string& topic,
                                                 const ByteBuffer& payload,
                                                 float period_ms,
                                                 float deadline_ms,
                                                 int timeout_ms = 1000,
                                                 int priority = -1) {
        if (reject_native_control_for_role("transact")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload, period_ms, deadline_ms, timeout_ms, priority, false);
    }
    std::optional<ControlResponse> transact_control(const std::string& target,
                                                 const std::string& topic,
                                                 const ByteBuffer& payload,
                                                 const ControlTransactOptions& options) {
        if (reject_native_control_for_role("transact")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload, options, false);
    }
    std::optional<ControlResponse> transact_control(const std::string& target,
                                                 const std::string& topic,
                                                 const std::string& payload_json,
                                                 const ControlTransactOptions& options) {
        if (reject_native_control_for_role("transact")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload_json, options, false);
    }
    std::optional<ControlResponse> transact_control_result(const std::string& target,
                                                 const std::string& topic,
                                                 const ByteBuffer& payload,
                                                 float period_ms,
                                                 float deadline_ms,
                                                 int timeout_ms = 1000,
                                                 int priority = -1) {
        if (reject_native_control_for_role("transact result")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload, period_ms, deadline_ms, timeout_ms, priority, true);
    }
    std::optional<ControlResponse> transact_control_result(const std::string& target,
                                                 const std::string& topic,
                                                 const ByteBuffer& payload,
                                                 const ControlTransactOptions& options) {
        if (reject_native_control_for_role("transact result")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload, options, true);
    }
    std::optional<ControlResponse> transact_control_result(const std::string& target,
                                                 const std::string& topic,
                                                 const std::string& payload_json,
                                                 const ControlTransactOptions& options) {
        if (reject_native_control_for_role("transact result")) {
            return std::nullopt;
        }
        return transact_control_impl(target, topic, payload_json, options, true);
    }
    void wait_control_reclaimed(const std::string& target, const std::string& topic, int timeout_ms = 1000);
    PreparedCodec prepared_codec(const std::string& message_ref) const;
    std::string codec_metadata_json() const;
    std::string contract_codecs_json() const;
    ByteBuffer encode_message_json(const std::string& message_ref,
                                   const std::string& payload_json) const;
    std::string decode_message_json(const std::string& message_ref,
                                    const ByteBuffer& payload) const;
    DecodedControlResponse decode_control_response(const ControlResponse& response,
                                                           const std::string& response_type) const {
        return decode_control_response_impl(response, response_type);
    }

    // Timing header helpers of the Python SDK. Headers, execution reports,
    // payloads and wire objects are JSON object text with the Python dict
    // shapes; malformed input throws std::invalid_argument.
    // build_timing_request_header takes the Python keyword arguments from
    // timing (source_subsystem defaults to this subsystem); valid_for_ns is
    // measured from generated_at_sync_ns, or from now when unset.
    std::string build_timing_request_header(const std::string& target_subsystem,
                                            const ControlTimingOptions& timing = {},
                                            const std::string& source_subsystem = {}) const;
    // Returns the header with execution.execute_at_sync_ns resolved and the
    // accepted time used (now when unset).
    std::pair<std::string, std::int64_t> normalize_timing_request_header(
        const std::string& header_json, std::optional<std::int64_t> accepted_at_sync_ns = std::nullopt) const;
    std::string build_timing_response_header(const std::string& request_header_json,
                                             const std::string& execution_report_json,
                                             int status_code,
                                             const std::string& status_message,
                                             std::optional<std::int64_t> responded_at_sync_ns = std::nullopt) const;
    std::string pack_timing_request(const std::string& request_header_json, const std::string& payload_ref,
                                    const std::string& payload_json) const;
    TimingEnvelope unpack_timing_request(const std::string& wire_json) const;
    std::string pack_timing_response(const std::string& response_header_json, const std::string& payload_ref,
                                     const std::string& payload_json) const;
    TimingEnvelope unpack_timing_response(const std::string& wire_json) const;

    void start_event_transport();
    void stop_event_transport();

    void initialize();
    void start();
    bool request_lifecycle_transition(const std::string& transition,
                                      const std::string& reason = std::string {});
    bool pause();
    bool resume();
    bool stop();
    bool fault(const std::string& reason = std::string {});
    bool degrade(const std::string& reason = std::string {});
    void terminate();
    void reset();

protected:
    virtual bool on_initialize() = 0;
    virtual bool on_start() = 0;
    virtual bool on_terminate() = 0;
    virtual bool on_reset() = 0;
    virtual bool on_pause() { return false; }
    virtual bool on_resume() { return false; }
    virtual bool on_stop() { return false; }
    virtual bool on_fault(const std::string& reason) {
        (void)reason;
        return true;
    }
    virtual bool on_degraded(const std::string& reason) {
        (void)reason;
        return true;
    }

    virtual void on_state_changed(DeviceState) {}

    void set_state(DeviceState state);

private:
    struct EventTopicInfo {
        std::string topic;
        std::string type;
    };
    struct ControlEndpointRegistration {
        ControlEndpointHandler handler;
        ControlEndpointOptions options;
    };
    struct OperationRecord;
    struct PreparedState;
    struct CodecDiagnostics;
    bool register_event_subscribe_impl(const EventSubscribeSpec& spec, const std::string& operation_target);
    bool register_event_identity(const EventTopicInfo& info, bool publisher,
                                 const std::function<bool()>& acquire_resource);
    void prepare_schema_codecs(const std::shared_ptr<PreparedState>& state);
    void require_available_managed_codec(const std::shared_ptr<const PreparedState>& state) const;

    void run_phase(const char* label,
                   const std::function<bool()>& hook,
                   DeviceState success_state,
                   DeviceState failure_state);
    bool start_impl();
    bool run_lifecycle_transition(const char* label,
                                  const std::function<bool()>& hook,
                                  DeviceState entering_state,
                                  DeviceState success_state,
                                  DeviceState failure_state,
                                  const std::unordered_set<DeviceState>& allowed_states,
                                  bool restore_on_false = false);
    void install_lifecycle_transition_handler();
    bool ensure_core_client(const char* caller) const;
    int bind_control_impl(int port = 0, const std::string& host = "0.0.0.0");
    bool register_control_endpoint_impl(const std::string& topic,
                                const ControlEndpointHandler& handler,
                                ControlEndpointOptions options = {});
    std::vector<std::string> control_endpoint_names_impl() const;
    std::vector<std::pair<std::string, ControlEndpointOptions>> control_contract_info_impl() const;
    bool start_control_server_impl();
    bool start_control_server_impl(const ControlHandler& handler);
    void stop_control_server_impl();
    int control_port_impl() const;

    bool register_procedure_impl(const std::string& name, const ProcedureHandler& handler);
    bool register_procedure_impl(const std::string& name,
                          const ProcedureHandler& handler,
                          ProcedureOptions options);
    bool register_typed_procedure_impl(const std::string& name,
                                const ProcedureHandler& handler,
                                const std::string& request_type,
                                const std::string& response_type,
                                const std::string& timing_support = "none");
    bool start_procedure_server_impl();
    void stop_procedure_server_impl();
    std::vector<std::string> procedure_names_impl() const;
    std::vector<std::pair<std::string, ProcedureOptions>> procedure_contract_info_impl() const;
    // started: when the caller's invocation budget began (a bound call has
    // already spent part of it on binding resolution).
    std::optional<ProcedureResponse> call_procedure_impl(const std::string& target,
                                                const std::string& procedure,
                                                const std::string& params_json = "null",
                                                int timeout_ms = 1000,
                                                std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now());
    std::optional<ProcedureResponse> call_procedure_impl(const std::string& target,
                                                const std::string& procedure,
                                                const std::string& params_json,
                                                const ProcedureCallOptions& options,
                                                std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now());
    std::optional<ProcedureResponse> call_procedure_impl(const std::string& target, const std::string& procedure,
        const ByteBuffer& params_payload, const ProcedureCallOptions& options);
    std::optional<ProcedureResponse> call_typed_procedure_impl(const std::string& target,
                                                      const std::string& procedure,
                                                      const std::string& request_type,
                                                      const std::string& response_type,
                                                      const std::string& params_json = "{}",
                                                      int timeout_ms = 1000);

    bool register_operation_impl(const std::string& name, const OperationHandler& handler);
    bool register_operation_impl(const std::string& name,
                         const OperationHandler& handler,
                         OperationOptions options);
    bool start_operation_server_impl();
    void stop_operation_server_impl();
    std::vector<std::string> operation_names_impl() const;
    std::vector<std::pair<std::string, OperationOptions>> operation_contract_info_impl() const;
    std::optional<OperationStartResponse> start_operation_impl(const std::string& target,
                                                    const std::string& operation,
                                                    const std::string& params_json,
                                                    std::optional<int> timeout_ms,
                                                    std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now());
    std::optional<OperationStartResponse> start_operation_impl(const std::string& target,
                                                    const std::string& operation,
                                                    const std::string& params_json,
                                                    const OperationStartOptions& options,
                                                    std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now());
    std::optional<OperationQueryResponse> cancel_operation_impl(const std::string& target,
                                                     const std::string& operation_id,
                                                     int timeout_ms = 1000);
    std::optional<OperationQueryResponse> query_operation_impl(const std::string& target,
                                                    const std::string& operation_id,
                                                    int timeout_ms = 1000);
    bool subscribe_operation_event_impl(const std::string& target, const std::string& operation,
                                        const std::string& operation_id, const char* kind,
                                        const OperationEventCallback& callback,
                                        const OperationSubscribeOptions& options);

    bool connect_control_impl(const std::string& name,
                      const std::string& host,
                      int port);
    std::optional<ControlRoute> resolve_control_target_impl(const std::string& target);
    bool prepare_protocol_lane(const std::string& target, const std::string& name, bool operation,
                               std::size_t request_capacity, std::size_t response_capacity,
                               int timeout_ms, bool result_ready, bool typed);
    bool handshake_control_impl(const std::string& target, int timeout_ms = 1000);
    bool send_control_impl(const std::string& target,
                   const std::string& topic,
                   const ByteBuffer& payload,
                   std::int64_t timestamp_ns = -1,
                   float period_ms = 0.0F,
                   float deadline_ms = 1000.0F,
                   int priority = -1);
    std::optional<ControlResponse> recv_control_impl(const std::string& target,
                                                     const std::string& type,
                                                     int timeout_ms);
    // Completion mode is explicit: false waits for the response, true also for
    // its result-ready completion.
    std::optional<ControlResponse> transact_control_impl(const std::string& target,
                                              const std::string& topic,
                                              const ByteBuffer& payload,
                                              float period_ms,
                                              float deadline_ms,
                                              int timeout_ms,
                                              int priority,
                                              bool result_ready);
    std::optional<ControlResponse> transact_control_impl(const std::string& target,
        const std::string& topic, const ByteBuffer& payload, float period_ms, float deadline_ms,
        int timeout_ms, int priority, bool result_ready, std::chrono::steady_clock::time_point started);
    std::optional<ControlResponse> transact_control_impl(const std::string& target,
                                              const std::string& topic,
                                              const ByteBuffer& payload,
                                              const ControlTransactOptions& options,
                                              bool result_ready,
                                              std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now());
    std::optional<ControlResponse> transact_control_impl(const std::string& target,
                                              const std::string& topic,
                                              const std::string& payload_json,
                                              const ControlTransactOptions& options,
                                              bool result_ready);
    std::optional<ControlResponse> transact_control_impl(const std::string& target,
        const std::string& topic, const std::string& payload_json,
        const ControlTransactOptions& options, bool result_ready,
        std::chrono::steady_clock::time_point started);
    // Caller owns the codec lifecycle shared lock through conversion and transport.
    std::optional<ControlResponse> transact_control_unlocked(const std::string& target,
        const std::string& topic, const ByteBuffer& payload,
        const ControlTransactOptions& options, bool result_ready,
        std::chrono::steady_clock::time_point started);
    DecodedControlResponse decode_control_response_impl(const ControlResponse& response,
                                                        const std::string& response_type) const;

    static std::int64_t default_now_ns();
    void start_heartbeat();
    void stop_heartbeat();
    void heartbeat_loop();
    // What a status update is for: the periodic heartbeat (keeps the Control
    // registration), initialize's codec acknowledgement, or the immediate
    // update after a lifecycle change, which never waits for a re-registration.
    enum class StatusUpdate { heartbeat, codec_acknowledgement, lifecycle_change };
    void send_heartbeat_once(StatusUpdate update = StatusUpdate::heartbeat);
    void maybe_recover_control_registration();
    bool keep_control_registration(const std::string& status);
    void send_lifecycle_status();
    bool lifecycle_status_deferred(bool publication_locked, const std::string& state);
    void publish_lifecycle_status(const std::string& status, const std::string& state);
    std::optional<std::string> register_with_control_again(r2kernel::core::R2KernelClient& native,
                                                           const std::string& status);
    std::string operation_topic(const std::string& target,
                             const std::string& operation,
                             const std::string& operation_id,
                             const std::string& kind) const;
    bool ensure_control_dispatch_server(const char* caller);
    void stop_control_dispatch_server();
    ControlResponse dispatch_control_request(ControlRequest request);
    ControlResponse handle_registered_control_request(const ControlRequest& request,
                                                const ControlEndpointRegistration& registration);
    ControlResponse handle_procedure_request(const ControlRequest& request);
    ControlResponse handle_typed_procedure_request(const ControlRequest& request,
                                              const std::string& procedure);
    ControlResponse handle_operation_start_request(const ControlRequest& request);
    ControlResponse handle_typed_operation_start_request(const ControlRequest& request,
                                                   const std::string& operation_name);
    ControlResponse handle_operation_cancel_request(const ControlRequest& request);
    ControlResponse handle_operation_query_request(const ControlRequest& request);
    struct OperationPayload;
    EventPayload operation_event_bytes(const OperationRecord& operation, const char* kind, const std::string& payload_type,
                                       const OperationPayload& payload, std::int64_t ts, const std::string& state = {},
                                       bool cancel_requested = false) const;
    void publish_operation_state_event(OperationRecord& operation, const std::string& state, const OperationPayload& payload);
    ControlResponse handle_typed_operation_status_request(const ControlRequest& request,
                                                       bool cancel);
    bool contract_policy_strict() const noexcept;
    bool contract_policy_off() const noexcept;
    ContractValidationReport runtime_contract_type_report(const SchemaContext& context,
                                                          const std::string& policy) const;
    bool reject_contract_violation(const std::string& message) const;
    bool reject_native_control_for_role(const std::string& operation);

    std::optional<std::vector<std::string>> schema_roots_;
    std::vector<std::string> codec_contract_ids_;
    mutable std::shared_mutex codec_lifecycle_mutex_;
    // Recursive because on_initialize may stage registrations on the same thread.
    mutable std::recursive_mutex event_registration_mutex_;
    std::atomic<bool> event_teardown_in_progress_{false};
    std::mutex heartbeat_publication_mutex_;
    std::atomic<std::size_t> active_codec_callbacks_{0};
    std::vector<EventPublishSpec> staged_event_publish_;
    std::vector<EventSubscribeSpec> staged_event_subscribe_;
    std::atomic<bool> managed_codec_initialization_attempted_{false};
    std::shared_ptr<const PreparedState> prepared_state_;
    std::shared_ptr<const CodecDiagnostics> acknowledged_codec_diagnostics_;
    std::string name_;
    std::string manifest_role_;
    std::filesystem::path user_data_path_;
    int heartbeat_port_ = 40000;
    DeviceState state_ = DeviceState::booting;
    Logger logger_;
    TimestampGetter timestamp_getter_;
    std::string contract_policy_ = "strict";
    std::vector<std::string> declared_apis_;
    std::vector<std::string> declared_components_;
    std::filesystem::path manifest_path_;
    std::unordered_map<std::string, std::string> launch_bindings_;
    std::string manifest_status_json_;
    mutable std::mutex spatial_mutex_;
    std::map<std::string, FrameTransform> spatial_transforms_;
    std::unordered_set<std::string> allowed_event_publish_topics_;
    // Published topic -> frame checks of the provided spatial_ports APIs.
    std::unordered_map<std::string, std::vector<detail::PublishedFrameCheck>> published_frame_checks_;
    std::unordered_set<std::string> allowed_event_subscribe_topics_;
    std::unordered_set<std::string> allowed_control_topics_;
    std::unordered_set<std::string> provided_control_topics_;
    std::unordered_set<std::string> operation_subscribe_topics_;
    std::unordered_set<std::string> allowed_procedure_names_;
    std::unordered_set<std::string> allowed_operation_names_;
    std::shared_ptr<CoreClient> core_client_;
    mutable std::mutex heartbeat_state_mutex_;
    std::vector<EventTopicInfo> event_publish_info_;
    std::vector<EventTopicInfo> event_subscribe_info_;
    std::unordered_map<std::string, ProcedureHandler> procedure_handlers_;
    std::unordered_map<std::string, ProcedureOptions> procedure_contracts_;
    std::unordered_map<std::string, OperationHandler> operation_handlers_;
    std::unordered_map<std::string, OperationOptions> operation_contracts_;
    std::unordered_map<std::string, ControlEndpointRegistration> control_endpoint_handlers_;
    std::unordered_map<std::string, std::shared_ptr<OperationRecord>> operation_records_;
    ControlHandler control_handler_;
    bool control_server_enabled_ = false;
    bool procedure_server_enabled_ = false;
    bool operation_server_enabled_ = false;
    bool control_dispatch_running_ = false;
    bool control_bind_requested_ = false;
    std::atomic<bool> heartbeat_stop_ {false};
    std::mutex heartbeat_wait_mutex_;
    std::condition_variable heartbeat_wait_cv_;
    std::thread heartbeat_thread_;
    std::chrono::steady_clock::time_point next_control_registration_check_ {};
    std::chrono::steady_clock::time_point next_clock_status_refresh_ {};
    std::shared_ptr<detail::SubsystemConfig> config_;
    SubsystemManifest subsystem_manifest_;
    std::optional<DeploymentManifest> deployment_manifest_;
};

class GatewaySubsystem : public Subsystem {
public:
    using Subsystem::Subsystem;
    static constexpr const char* role = "gateway";

    // Sources bound to a provided Control interface (Python SDK
    // GatewaySubsystem). Each call asks Control for the current binding status
    // (one request within 1 s). provider_interface must be declared under
    // provides; inconsistent, ambiguous or unavailable status throws
    // std::runtime_error (fail closed).
    //
    // source requirement reference -> source subsystem of every bound
    // source. bound means the binding topology and contracts are checked, not
    // that the source session or handler is live.
    R2KERNEL_CPP_API std::map<std::string, std::string> bound_control_sessions(
        const std::string& provider_interface) const;
    // The exact current owner registration and binding epoch of the one
    // bound source.
    R2KERNEL_CPP_API BoundControlAuthority bound_control_authority(const std::string& provider_interface) const;
    // bound_control_authority, checked against a received request: its source
    // subsystem must be the bound source (else std::invalid_argument) and, for
    // a federated source, the authenticated transport authority must match
    // every field (else std::runtime_error). The overload takes both from a
    // Control endpoint handler's request on the handler thread.
    R2KERNEL_CPP_API BoundControlAuthority validate_control_authority(
        const std::string& provider_interface, const std::string& source_subsystem,
        const std::optional<ControlSourceAuthority>& source_authority) const;
    R2KERNEL_CPP_API BoundControlAuthority validate_control_authority(const std::string& provider_interface,
                                                                      const ControlEndpointRequest& request) const;
    // The source subsystem when exactly one source is bound.
    R2KERNEL_CPP_API std::string bound_control_owner(const std::string& provider_interface) const;
    // The one source requirement reference of source_subsystem.
    R2KERNEL_CPP_API std::string bound_control_session(const std::string& provider_interface,
                                                       const std::string& source_subsystem) const;
};

class ComponentSubsystem : public Subsystem {
public:
    using Subsystem::Subsystem;
    static constexpr const char* role = "component";
};

class CompoundSubsystem : public Subsystem {
public:
    using Subsystem::Subsystem;
    static constexpr const char* role = "compound";
};

class AppSubsystem : public Subsystem {
public:
    using Subsystem::Subsystem;
    static constexpr const char* role = "app";
};

}  // namespace r2kernel::wrapper
