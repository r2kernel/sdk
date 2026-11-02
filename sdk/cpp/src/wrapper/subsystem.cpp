#include "r2kernel/wrapper/subsystem.hpp"
#include "r2kernel/core/client.hpp"

#include "contract_loader.hpp"
#include "logged_problem_capture.hpp"
#include "best_effort_notification.hpp"
#include "control_dispatch_publication.hpp"
#include "../core/callback_authority_scope.hpp"
#include "../core/control_source_topic.hpp"
#include "../core/control_socket_env.hpp"
#include "../core/json_text.hpp"
#include "operation_event_wire.hpp"
#include "timing_support.hpp"
#include "subsystem_config.hpp"
#include "r2kernel_core.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cmath>
#include <cctype>
#include <exception>
#include <cstdlib>
#include <cerrno>
#include <cstring>
#include <iomanip>
#include <iterator>
#include <limits>
#include <list>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <set>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <pwd.h>
#include <unistd.h>
#endif

// One translation unit, split for size into private fragments that are
// included below in their original source order. A single unit keeps the
// codegen of per-call paths together (the SDK is built without LTO).
// Fragments are not standalone headers.
namespace r2kernel::wrapper {

namespace {

#include "subsystem_helpers.hpp"

#include "subsystem_manifest.hpp"

#include "subsystem_spatial.hpp"

#include "subsystem_platform.hpp"

#include "subsystem_deployment.hpp"

#include "subsystem_wire.hpp"

// The public loaders read contract specs from the environment's roots; the
// Subsystem constructor uses the parse_* functions under its own inventory.
template <class Load>
auto with_default_contract_inventory(Load&& load) {
    contract_loader::InventoryScope scope(
        std::make_shared<contract_loader::Inventory>(std::make_shared<SchemaContext>()));
    return load();
}

}  // namespace

SubsystemManifest load_subsystem_manifest(const std::filesystem::path& path,
                                          const std::optional<std::filesystem::path>& package_root) {
    return with_default_contract_inventory([&] { return load_package_subsystem_manifest(path, package_root); });
}

RobotPlatformManifest load_robot_platform_manifest(const std::filesystem::path& path) {
    return with_default_contract_inventory([&] { return parse_robot_platform_manifest(path); });
}

ApplicationManifest load_application_manifest(const std::filesystem::path& path) {
    return with_default_contract_inventory([&] { return parse_application_manifest(path); });
}

DeploymentManifest load_deployment_manifest(const std::filesystem::path& path) {
    return with_default_contract_inventory([&] { return parse_deployment_manifest(path); });
}

std::vector<BoundInterface> validate_required_bindings_for_subsystem(const SubsystemManifest& subsystem,
                                                                     const DeploymentManifest& deployment) {
    return required_bindings_for_subsystem(subsystem, deployment);
}

#include "subsystem_setup.hpp"

#include "subsystem_registration.hpp"

namespace {
using ControlClock = std::chrono::steady_clock;
ControlClock::time_point control_call_deadline(ControlClock::time_point started, int timeout_ms, float deadline_ms) {
    if (timeout_ms <= 0) throw std::invalid_argument("Control timeout must be positive");
    const double budget = deadline_ms > 0.0F && std::isfinite(deadline_ms)
        ? std::min(static_cast<double>(timeout_ms), static_cast<double>(deadline_ms))
        : static_cast<double>(timeout_ms);
    return started + std::chrono::duration_cast<ControlClock::duration>(
        std::chrono::duration<double, std::milli>(budget));
}
void require_control_deadline(ControlClock::time_point deadline) {
    if (ControlClock::now() >= deadline) throw std::runtime_error("Control invocation deadline exceeded");
}
// The native client source-wraps topics itself on the wire to Control; any
// other CoreClient receives the wrapped topic Control routes on.
std::string core_transport_topic(const CoreClient& core, const std::string& source, const std::string& topic) {
    if (dynamic_cast<const r2kernel::core::R2KernelClient*>(&core)) return topic;
    return r2kernel::core::detail::encode_source_topic(source, topic);
}
std::optional<ControlResponse> transact_core_started(CoreClient& core, const std::string& source,
    const std::string& target, const std::string& topic, const ByteBuffer& payload,
    std::int64_t timestamp, float period_ms, float deadline_ms, int timeout_ms, int priority,
    bool result_ready, ControlClock::time_point started) {
    const auto deadline = control_call_deadline(started, timeout_ms, deadline_ms);
    require_control_deadline(deadline);
    std::optional<ControlResponse> response;
    if (auto* native = dynamic_cast<r2kernel::core::R2KernelClient*>(&core)) {
        response = native->transact_control_started(target, topic, payload, timestamp,
            period_ms, deadline_ms, timeout_ms, priority, result_ready, started);
    } else {
        // Other CoreClient implementations have no budgeted entry point. Use
        // their own transport and report overruns after it returns.
        response = result_ready
            ? core.transact_control_result(target, core_transport_topic(core, source, topic), payload, timestamp, period_ms, deadline_ms,
                timeout_ms, priority)
            : core.transact_control(target, core_transport_topic(core, source, topic), payload, timestamp, period_ms, deadline_ms,
                timeout_ms, priority);
    }
    require_control_deadline(deadline);
    return response;
}
bool ensure_protocol_route(CoreClient& core, const std::string& target, ControlClock::time_point deadline) {
    require_control_deadline(deadline);
    if (auto* native = dynamic_cast<r2kernel::core::R2KernelClient*>(&core))
        return native->ensure_control_route(target, deadline);
    const auto route = core.resolve_control_target(target);
    if (!route || route->host.empty() || !resolved_control_route_port_is_valid(&core, route->port)) return false;
    require_control_deadline(deadline);
    if (!core.connect_control(target, route->host, route->port)) return false;
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - ControlClock::now()).count();
    if (remaining <= 0) throw std::runtime_error("Control route exhausted invocation budget");
    const bool ready = core.handshake_control(target, static_cast<int>(remaining));
    require_control_deadline(deadline);
    return ready;
}
}

std::optional<ProcedureResponse> Subsystem::call_procedure_impl(const std::string& target,
                                                       const std::string& procedure,
                                                       const std::string& params_json,
                                                       int timeout_ms,
                                                       ControlClock::time_point started) {
    const auto invocation_deadline = control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms));
    if (manifest_role_ == "gateway") {
        return make_procedure_response(false, "", "null", "role_forbidden", "Gateway interfaces are Control-only", now_ns());
    }
    if (!ensure_core_client("call_procedure")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return make_procedure_response(false, "", "null", "unavailable", "failed to bind Control route", now_ns());
    const std::string request_id = std::to_string(now_ns());
    const std::string safe_params = params_json.empty() ? "null" : params_json;
    const std::string request =
        "{"
        "\"kind\":\"procedure_request\","
        "\"target\":\"" + json_escape(target) + "\","
        "\"procedure\":\"" + json_escape(procedure) + "\","
        "\"request_id\":\"" + json_escape(request_id) + "\","
        "\"timeout_ms\":" + std::to_string(std::max(timeout_ms, 1)) + ","
        "\"params\":" + safe_params +
        "}";
    const ByteBuffer payload(request.begin(), request.end());
    auto response = transact_control_impl(target, "procedure.call", payload,
        0.0F, static_cast<float>(timeout_ms), timeout_ms, -1, false, started);
    if (!response.has_value()) {
        return make_procedure_response(false, request_id, "null", "timeout", "procedure transport timeout", now_ns());
    }
    const std::string response_text(response->payload.begin(), response->payload.end());
    ProcedureResponse out;
    out.request_id = json_string_field_local(response_text, "request_id").value_or(request_id);
    out.ok = json_bool_field_local(response_text, "ok").value_or(false);
    out.result_json = json_raw_field(response_text, "result").value_or("null");
    out.error_code = json_string_field_local(response_text, "error_code").value_or("");
    out.error_message = json_string_field_local(response_text, "error_message").value_or("");
    out.timestamp_ns = response->timestamp_ns;
    require_control_deadline(invocation_deadline);
    return out;
}

std::optional<ProcedureResponse> Subsystem::call_procedure_impl(const std::string& target,
                                                       const std::string& procedure,
                                                       const std::string& params_json,
                                                       const ProcedureCallOptions& options,
                                                       ControlClock::time_point started) {
    const auto invocation_deadline = control_call_deadline(started, options.timeout_ms, static_cast<float>(options.timeout_ms));
    if (manifest_role_ == "gateway") {
        return make_procedure_response(false, "", "null", "role_forbidden", "Gateway interfaces are Control-only", now_ns());
    }
    const std::string request_type = trim_copy(options.request_type);
    const std::string response_type = trim_copy(options.response_type);
    if (request_type.empty() && response_type.empty() && !options.timing.has_value()) {
        return call_procedure_impl(target, procedure, params_json, options.timeout_ms, started);
    }
    if (request_type.empty() || response_type.empty()) {
        return make_procedure_response(
            false,
            "",
            "null",
            "invalid_argument",
            "typed procedure call requires request_type and response_type",
            now_ns()
        );
    }
    if (!ensure_core_client("call_procedure")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return make_procedure_response(false, "", "null", "unavailable", "failed to bind Control route", now_ns());

    ControlTransactOptions control_options;
    control_options.period_ms = 0.0F;
    control_options.deadline_ms = static_cast<float>(std::max(options.timeout_ms, 1));
    control_options.timeout_ms = std::max(options.timeout_ms, 1);
    control_options.request_type = request_type;
    control_options.response_type = response_type;
    control_options.timing = options.timing;
    if (options.timing.has_value()) {
        control_options.priority = options.timing->priority;
    }

    auto response = transact_control_impl(
        target,
        std::string(kTypedProcedureTopicPrefix) + procedure,
        params_json.empty() ? "{}" : params_json,
        control_options, false, started
    );
    if (!response.has_value()) {
        return make_procedure_response(false, "", "null", "timeout", "typed procedure transport timeout", now_ns());
    }
    if (response->status != 0) {
        const std::string message(response->payload.begin(), response->payload.end());
        return make_procedure_response(
            false,
            "",
            "null",
            "procedure_transport_failed",
            message.empty() ? "typed procedure transport failed" : message,
            response->timestamp_ns
        );
    }

    ProcedureResponse out;
    out.timestamp_ns = response->timestamp_ns;
    if (options.timing.has_value()) {
        const auto decoded = decode_control_response_impl(*response, response_type);
        out.ok = decoded.timing.status_code == 0;
        out.result_json = decoded.payload_json.empty() ? "null" : decoded.payload_json;
        out.request_id = decoded.timing.request_id_text;
        out.status_code = decoded.timing.status_code;
        out.status_message = decoded.timing.final_status;
        if (!out.ok) {
            out.error_code = "procedure_response_error";
            out.error_message = "timed procedure response status=" + decoded.timing.final_status;
        }
    } else {
        out.ok = true;
        out.result_json = decode_message_json(response_type, response->payload);
    }
    require_control_deadline(invocation_deadline);
    return out;
}

// The typed call above with encoded params and result: no JSON conversion.
std::optional<ProcedureResponse> Subsystem::call_procedure_impl(const std::string& target,
                                                                const std::string& procedure,
                                                                const ByteBuffer& params_payload,
                                                                const ProcedureCallOptions& options) {
    const auto started = ControlClock::now();
    const auto invocation_deadline = control_call_deadline(started, options.timeout_ms, static_cast<float>(options.timeout_ms));
    if (manifest_role_ == "gateway") {
        return make_procedure_response(false, "", "null", "role_forbidden", "Gateway interfaces are Control-only", now_ns());
    }
    ControlTransactOptions control_options;
    control_options.request_type = trim_copy(options.request_type);
    control_options.response_type = trim_copy(options.response_type);
    if (control_options.request_type.empty() || control_options.response_type.empty()) {
        return make_procedure_response(false, "", "null", "invalid_argument",
                                       "an encoded procedure call requires request_type and response_type", now_ns());
    }
    if (!ensure_core_client("call_procedure")) return std::nullopt;
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return make_procedure_response(false, "", "null", "unavailable", "failed to bind Control route", now_ns());
    control_options.deadline_ms = static_cast<float>(std::max(options.timeout_ms, 1));
    control_options.timeout_ms = std::max(options.timeout_ms, 1);
    control_options.timing = options.timing;
    if (options.timing.has_value()) control_options.priority = options.timing->priority;
    auto response = transact_control_impl(target, std::string(kTypedProcedureTopicPrefix) + procedure, params_payload,
                                          control_options, false, started);
    if (!response.has_value())
        return make_procedure_response(false, "", "null", "timeout", "typed procedure transport timeout", now_ns());
    if (response->status != 0) {
        const std::string message(response->payload.begin(), response->payload.end());
        return make_procedure_response(false, "", "null", "procedure_transport_failed",
                                       message.empty() ? "typed procedure transport failed" : message,
                                       response->timestamp_ns);
    }
    ProcedureResponse out;
    out.timestamp_ns = response->timestamp_ns;
    if (options.timing.has_value()) {
        auto decoded = decode_control_response_impl(*response, "");
        out.ok = decoded.timing.status_code == 0;
        out.result_payload = std::move(decoded.payload);
        out.request_id = decoded.timing.request_id_text;
        out.status_code = decoded.timing.status_code;
        out.status_message = decoded.timing.final_status;
        if (!out.ok) {
            out.error_code = "procedure_response_error";
            out.error_message = "timed procedure response status=" + decoded.timing.final_status;
        }
    } else {
        out.ok = true;
        out.result_payload = std::move(response->payload);
    }
    require_control_deadline(invocation_deadline);
    return out;
}

std::optional<ProcedureResponse> Subsystem::call_typed_procedure_impl(const std::string& target,
                                                             const std::string& procedure,
                                                             const std::string& request_type,
                                                             const std::string& response_type,
                                                             const std::string& params_json,
                                                             int timeout_ms) {
    ProcedureCallOptions options;
    options.timeout_ms = timeout_ms;
    options.request_type = request_type;
    options.response_type = response_type;
    return call_procedure_impl(target, procedure, params_json.empty() ? "{}" : params_json, options);
}

std::optional<OperationStartResponse> Subsystem::start_operation_impl(const std::string& target,
                                                           const std::string& operation,
                                                           const std::string& params_json,
                                                           std::optional<int> requested_timeout_ms,
                                                           ControlClock::time_point started) {
    // The provider receives the caller's value (null when unset); the caller
    // waits 1000 ms when unset, as in the Python SDK.
    const int timeout_ms = requested_timeout_ms.value_or(1000);
    const auto invocation_deadline = control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms));
    if (manifest_role_ == "gateway") {
        return OperationStartResponse {false, false, "", "", "", "role_forbidden", "Gateway interfaces are Control-only", now_ns()};
    }
    if (!ensure_core_client("start_operation")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return OperationStartResponse {false, false, "", "", "", "unavailable", "failed to bind Control route", now_ns()};

    const std::string request_id = std::to_string(now_ns());
    const std::string safe_params = params_json.empty() ? "null" : params_json;
    const std::string request =
        "{"
        "\"kind\":\"operation_start_request\","
        "\"target\":\"" + json_escape(target) + "\","
        "\"operation\":\"" + json_escape(operation) + "\","
        "\"request_id\":\"" + json_escape(request_id) + "\","
        "\"timeout_ms\":" + (requested_timeout_ms ? std::to_string(*requested_timeout_ms) : std::string("null")) + ","
        "\"params\":" + safe_params +
        "}";
    const ByteBuffer payload(request.begin(), request.end());
    auto response = transact_control_impl(target, "operation.start", payload,
        0.0F, static_cast<float>(timeout_ms), timeout_ms, -1, false, started);
    if (!response.has_value()) {
        return OperationStartResponse {false, false, request_id, "", "", "timeout", "operation transport timeout", now_ns()};
    }
    const std::string response_text(response->payload.begin(), response->payload.end());
    OperationStartResponse out;
    out.request_id = json_string_field_local(response_text, "request_id").value_or(request_id);
    out.ok = json_bool_field_local(response_text, "ok").value_or(false);
    out.accepted = json_bool_field_local(response_text, "accepted").value_or(false);
    out.operation_id = json_string_field_local(response_text, "operation_id").value_or("");
    out.state = json_string_field_local(response_text, "state").value_or("");
    out.error_code = json_string_field_local(response_text, "error_code").value_or("");
    out.error_message = json_string_field_local(response_text, "error_message").value_or("");
    out.timestamp_ns = response->timestamp_ns;
    require_control_deadline(invocation_deadline);
    return out;
}

std::optional<OperationStartResponse> Subsystem::start_operation_impl(const std::string& target,
                                                           const std::string& operation,
                                                           const std::string& params_json,
                                                           const OperationStartOptions& options,
                                                           ControlClock::time_point started) {
    const int timeout_ms = options.timeout_ms.value_or(1000);
    const auto invocation_deadline = control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms));
    if (manifest_role_ == "gateway") {
        return OperationStartResponse {false, false, "", "", "", "role_forbidden", "Gateway interfaces are Control-only", now_ns()};
    }
    const std::string request_type = trim_copy(options.request_type);
    if (request_type.empty() && !options.timing.has_value()) {
        return start_operation_impl(target, operation, params_json, options.timeout_ms, started);
    }
    if (request_type.empty()) {
        return OperationStartResponse {
            false,
            false,
            "",
            "",
            "",
            "invalid_argument",
            "timed operation start requires request_type",
            now_ns()
        };
    }
    if (!ensure_core_client("start_operation")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return OperationStartResponse {false, false, "", "", "", "unavailable", "failed to bind Control route", now_ns()};

    ControlTransactOptions control_options;
    control_options.period_ms = 0.0F;
    control_options.deadline_ms = static_cast<float>(std::max(timeout_ms, 1));
    control_options.timeout_ms = std::max(timeout_ms, 1);
    control_options.request_type = request_type;
    control_options.timing = options.timing;
    if (options.timing.has_value()) {
        control_options.priority = options.timing->priority;
    }

    auto response = transact_control_impl(
        target,
        std::string(kTypedOperationStartTopicPrefix) + operation,
        params_json.empty() ? "{}" : params_json,
        control_options, false, started
    );
    if (!response.has_value()) {
        return OperationStartResponse {false, false, "", "", "", "timeout", "operation transport timeout", now_ns()};
    }
    if (response->status != 0) {
        const std::string message(response->payload.begin(), response->payload.end());
        return OperationStartResponse {
            false,
            false,
            "",
            "",
            "",
            "operation_transport_failed",
            message.empty() ? "typed operation transport failed" : message,
            response->timestamp_ns
        };
    }
    const std::string response_text(response->payload.begin(), response->payload.end());
    OperationStartResponse out;
    out.request_id = json_string_field_local(response_text, "request_id").value_or("");
    out.ok = json_bool_field_local(response_text, "ok").value_or(false);
    out.accepted = json_bool_field_local(response_text, "accepted").value_or(false);
    out.operation_id = json_string_field_local(response_text, "operation_id").value_or("");
    out.state = json_string_field_local(response_text, "state").value_or("");
    out.error_code = json_string_field_local(response_text, "error_code").value_or("");
    out.error_message = json_string_field_local(response_text, "error_message").value_or("");
    out.timestamp_ns = response->timestamp_ns;
    require_control_deadline(invocation_deadline);
    return out;
}

std::optional<OperationQueryResponse> Subsystem::cancel_operation_impl(const std::string& target,
                                                            const std::string& operation_id,
                                                            int timeout_ms) {
    const auto started = ControlClock::now();
    const auto invocation_deadline = control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms));
    if (manifest_role_ == "gateway") {
        return OperationQueryResponse {false, "", operation_id, "", "", false, 0, "null", "null", "role_forbidden", "Gateway interfaces are Control-only", 0, 0, now_ns()};
    }
    if (!ensure_core_client("cancel_operation")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return OperationQueryResponse {false, "", operation_id, "", "", false, 0, "null", "null", "unavailable", "failed to bind Control route", 0, 0, now_ns()};

    const std::string request_id = std::to_string(now_ns());
    const ByteBuffer payload = encode_message_json("msg.core.common.primitive.v1/StringValue", string_value_json(operation_id));
    auto response = transact_control_impl(target, kTypedOperationCancelTopic, payload,
        0.0F, static_cast<float>(timeout_ms), timeout_ms, -1, false, started);
    if (!response.has_value()) {
        return OperationQueryResponse {false, request_id, operation_id, "", "", false, 0, "null", "null", "timeout", "operation cancel timeout", 0, 0, now_ns()};
    }
    const std::string response_text(response->payload.begin(), response->payload.end());
    OperationQueryResponse out;
    out.request_id = json_string_field_local(response_text, "request_id").value_or(request_id);
    out.ok = json_bool_field_local(response_text, "ok").value_or(false);
    out.operation_id = json_string_field_local(response_text, "operation_id").value_or(operation_id);
    out.operation = json_string_field_local(response_text, "operation").value_or("");
    out.state = json_string_field_local(response_text, "state").value_or("");
    out.cancel_requested = json_bool_field_local(response_text, "cancel_requested").value_or(false);
    out.result_json = json_raw_field(response_text, "result").value_or("null");
    out.state_payload_json = json_raw_field(response_text, "state_payload").value_or("null");
    out.feedback_count = json_int_field_local(response_text, "feedback_count").value_or(0);
    out.created_ts = json_int_field_local(response_text, "created_ts").value_or(0);
    out.updated_ts = json_int_field_local(response_text, "updated_ts").value_or(0);
    out.error_code = json_string_field_local(response_text, "error_code").value_or("");
    out.error_message = json_string_field_local(response_text, "error_message").value_or("");
    out.timestamp_ns = response->timestamp_ns;
    require_control_deadline(invocation_deadline);
    return out;
}

std::optional<OperationQueryResponse> Subsystem::query_operation_impl(const std::string& target,
                                                           const std::string& operation_id,
                                                           int timeout_ms) {
    const auto started = ControlClock::now();
    const auto invocation_deadline = control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms));
    if (manifest_role_ == "gateway") {
        return OperationQueryResponse {false, "", operation_id, "", "", false, 0, "null", "null", "role_forbidden", "Gateway interfaces are Control-only", 0, 0, now_ns()};
    }
    if (!ensure_core_client("query_operation")) {
        return std::nullopt;
    }
    if (!ensure_protocol_route(*core_client_, target, invocation_deadline))
        return OperationQueryResponse {false, "", operation_id, "", "", false, 0, "null", "null", "unavailable", "failed to bind Control route", 0, 0, now_ns()};

    const std::string request_id = std::to_string(now_ns());
    const ByteBuffer payload = encode_message_json("msg.core.common.primitive.v1/StringValue", string_value_json(operation_id));
    auto response = transact_control_impl(target, kTypedOperationQueryTopic, payload,
        0.0F, static_cast<float>(timeout_ms), timeout_ms, -1, false, started);
    if (!response.has_value()) {
        return OperationQueryResponse {false, request_id, operation_id, "", "", false, 0, "null", "null", "timeout", "operation query timeout", 0, 0, now_ns()};
    }
    const std::string response_text(response->payload.begin(), response->payload.end());
    OperationQueryResponse out;
    out.request_id = json_string_field_local(response_text, "request_id").value_or(request_id);
    out.ok = json_bool_field_local(response_text, "ok").value_or(false);
    out.operation_id = json_string_field_local(response_text, "operation_id").value_or(operation_id);
    out.operation = json_string_field_local(response_text, "operation").value_or("");
    out.state = json_string_field_local(response_text, "state").value_or("");
    out.cancel_requested = json_bool_field_local(response_text, "cancel_requested").value_or(false);
    out.result_json = json_raw_field(response_text, "result").value_or("null");
    out.state_payload_json = json_raw_field(response_text, "state_payload").value_or("null");
    out.feedback_count = json_int_field_local(response_text, "feedback_count").value_or(0);
    out.created_ts = json_int_field_local(response_text, "created_ts").value_or(0);
    out.updated_ts = json_int_field_local(response_text, "updated_ts").value_or(0);
    out.error_code = json_string_field_local(response_text, "error_code").value_or("");
    out.error_message = json_string_field_local(response_text, "error_message").value_or("");
    out.timestamp_ns = response->timestamp_ns;
    require_control_deadline(invocation_deadline);
    return out;
}

namespace {
// Operation events arrive in the SDAE wire (operation_event_wire.hpp) that the
// Python SDK, this SDK and the Rust bench client publish. Any other payload is
// read as the JSON text event that earlier C++ and Rust publishers sent.
// Without payload_json a typed or raw payload stays bytes (no codec call).
OperationEventMessage operation_event_message(const Subsystem& subsystem,
                                              const EventPayload& payload,
                                              std::int64_t timestamp_ns,
                                              const char* topic_kind,
                                              bool payload_json) {
    if (operation_event_wire::is_wire(payload)) {
        return operation_event_wire::to_message(
            operation_event_wire::decode(payload),
            [&subsystem](const std::string& payload_type, const ByteBuffer& bytes) {
                return subsystem.decode_message_json(payload_type, bytes);
            },
            payload_json);
    }
    const std::string text(payload.begin(), payload.end());
    OperationEventMessage message;
    message.operation_id = json_string_field_local(text, "operation_id").value_or("");
    message.operation = json_string_field_local(text, "operation").value_or("");
    message.target = json_string_field_local(text, "target").value_or("");
    message.kind = json_string_field_local(text, "kind").value_or(topic_kind);
    message.state = json_string_field_local(text, "state").value_or("");
    message.payload_json = json_raw_field(text, "payload").value_or("null");
    message.payload_type = json_string_field_local(text, "payload_type").value_or("");
    message.payload_encoding = operation_event_wire::is_null_payload(message.payload_json)
        ? OperationEventPayloadEncoding::none : OperationEventPayloadEncoding::json;
    message.cancel_requested = json_bool_field_local(text, "cancel_requested").value_or(false);
    message.timestamp_ns = json_int_field_local(text, "ts").value_or(timestamp_ns);
    return message;
}

// As the Python subscriber: an event that cannot be decoded (bad wire, a typed
// payload whose codec is not prepared) is logged and dropped; the operation_id
// filter applies to the decoded event.
EventCallback operation_event_subscriber(const Subsystem& subsystem,
                                         std::string topic,
                                         const char* topic_kind,
                                         std::string operation_id,
                                         OperationEventCallback callback,
                                         bool payload_json) {
    return [&subsystem, topic = std::move(topic), topic_kind, operation_id = std::move(operation_id),
            callback = std::move(callback), payload_json](const EventPayload& payload, std::int64_t timestamp_ns) {
        OperationEventMessage message;
        try {
            message = operation_event_message(subsystem, payload, timestamp_ns, topic_kind, payload_json);
        } catch (const std::exception& exc) {
            subsystem.logger().error(kOperationTag, "Failed to decode Operation event '" + topic + "': " + exc.what());
            return;
        }
        if (!operation_id.empty() && message.operation_id != operation_id) {
            return;
        }
        callback(message);
    };
}
}  // namespace

bool Subsystem::subscribe_operation_event_impl(const std::string& target,
                                               const std::string& operation,
                                               const std::string& operation_id,
                                               const char* kind,
                                               const OperationEventCallback& callback,
                                               const OperationSubscribeOptions& options) {
    if (manifest_role_ == "gateway") {
        logger_.error(kLifecycleTag, std::string("Gateway interfaces are Control-only; operation ") + kind
                                         + " subscription is forbidden");
        return false;
    }
    const std::string topic = operation_topic(target, operation, operation_id, kind);
    const bool ok = register_event_subscribe_impl(EventSubscribeSpec {
        topic,
        "bytes",
        options.size,
        operation_event_subscriber(*this, topic, kind, operation_id, callback, options.payload_json),
        {}
    }, target);
    if (ok) {
        start_event_transport();
    }
    return ok;
}

bool Subsystem::connect_control_impl(const std::string& name, const std::string& host, int port) {
    if (reject_native_control_for_role("connection")) {
        return false;
    }
    if (!ensure_core_client("connect_control")) {
        return false;
    }
    return core_client_->connect_control(name, host, port);
}

std::optional<ControlRoute> Subsystem::resolve_control_target_impl(const std::string& target) {
    if (reject_native_control_for_role("target resolution")) {
        return std::nullopt;
    }
    if (!ensure_core_client("resolve_control_target")) {
        return std::nullopt;
    }
    return core_client_->resolve_control_target(target);
}

bool Subsystem::handshake_control_impl(const std::string& target, int timeout_ms) {
    if (reject_native_control_for_role("handshake")) {
        return false;
    }
    if (!ensure_core_client("handshake_control")) {
        return false;
    }
    return core_client_->handshake_control(target, timeout_ms);
}

bool Subsystem::prepare_control(const std::string& target, const std::string& topic,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms, bool result_ready) {
    std::shared_lock<std::shared_mutex> preparation_lifetime(codec_lifecycle_mutex_, std::try_to_lock);
    if (!preparation_lifetime.owns_lock())
        throw std::runtime_error("Control preparation is unavailable during codec initialization");
    const bool protocol_topic = topic == "procedure.call" || topic == "operation.start" ||
        topic == "operation.cancel" || topic == "operation.query" ||
        topic == kTypedOperationCancelTopic || topic == kTypedOperationQueryTopic ||
        starts_with(topic, kTypedProcedureTopicPrefix) || starts_with(topic, kTypedOperationStartTopicPrefix);
    if (protocol_topic && manifest_role_ == "gateway")
        throw std::runtime_error("Gateway interfaces are Control-only");
    if (!protocol_topic && reject_native_control_for_role("prepare"))
        throw std::runtime_error("Subsystem role cannot prepare native Control");
    if (!ensure_core_client("prepare_control"))
        throw std::runtime_error("Control preparation requires a core client");
    return core_client_->prepare_control(target, core_transport_topic(*core_client_, name_, topic), request_capacity,
        response_capacity, timeout_ms, result_ready);
}

bool Subsystem::prepare_procedure(const std::string& target, const std::string& procedure,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms, bool result_ready, bool typed) {
    return prepare_protocol_lane(target, procedure, false, request_capacity, response_capacity, timeout_ms,
        result_ready, typed);
}

bool Subsystem::prepare_operation(const std::string& target, const std::string& operation,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms, bool result_ready, bool typed) {
    return prepare_protocol_lane(target, operation, true, request_capacity, response_capacity, timeout_ms,
        result_ready, typed);
}

bool Subsystem::prepare_protocol_lane(const std::string& target, const std::string& name, bool operation,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms, bool result_ready, bool typed) {
    if (timeout_ms <= 0) throw std::invalid_argument("Control preparation timeout must be positive");
    const auto deadline = ControlClock::now() + std::chrono::milliseconds(timeout_ms);
    if (manifest_role_ == "gateway") throw std::runtime_error("Gateway interfaces are Control-only");
    if (!ensure_core_client(operation ? "prepare_operation" : "prepare_procedure"))
        throw std::runtime_error("Control preparation requires a core client");
    const auto route = core_client_->resolve_control_target(target);
    if (!route) throw std::runtime_error("failed to resolve target subsystem: " + target);
    const auto advertised = [&name](const std::vector<std::string>& names) {
        return std::find(names.begin(), names.end(), name) != names.end();
    };
    if (operation) {
        if (!route->operations.empty() && !advertised(route->operations))
            throw std::runtime_error("operation is not advertised by target: " + name);
    } else {
        if (advertised(route->controls))
            throw std::runtime_error("'" + name + "' is advertised as a Control endpoint by target '" + target
                + "'; use the Control plane");
        if (!route->procedures.empty() && !advertised(route->procedures))
            throw std::runtime_error("procedure is not advertised by target: " + name);
    }
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - ControlClock::now()).count();
    if (remaining <= 0) throw std::runtime_error("Control preparation deadline exceeded");
    const std::string topic = operation
        ? (typed ? std::string(kTypedOperationStartTopicPrefix) + name : std::string("operation.start"))
        : (typed ? std::string(kTypedProcedureTopicPrefix) + name : std::string("procedure.call"));
    return prepare_control(target, topic, request_capacity, response_capacity, static_cast<int>(remaining),
        result_ready);
}

void Subsystem::configure_control_worker(const std::string& policy, int priority, const std::vector<int>& cpus) {
    if (!ensure_core_client("configure_control_worker"))
        throw std::runtime_error("Control worker configuration requires a core client");
    core_client_->configure_control_worker(policy, priority, cpus);
}

std::vector<ControlWorkerStatus> Subsystem::control_worker_status() const {
    if (!core_client_) throw std::runtime_error("Control worker diagnostics require a core client");
    return core_client_->control_worker_status();
}

bool Subsystem::send_control_impl(const std::string& target,
                          const std::string& topic,
                          const ByteBuffer& payload,
                          std::int64_t timestamp_ns,
                          float period_ms,
                          float deadline_ms,
                          int priority) {
    if (!ensure_core_client("send_control")) {
        return false;
    }
    if (timestamp_ns < 0) {
        timestamp_ns = now_ns();
    }
    return core_client_->send_control(target, core_transport_topic(*core_client_, name_, topic), payload, timestamp_ns,
        period_ms, deadline_ms, priority);
}

std::optional<ControlResponse> Subsystem::recv_control_impl(const std::string& target, const std::string& type,
                                                         int timeout_ms) {
    if (!ensure_core_client("recv_control")) {
        return std::nullopt;
    }
    auto response = core_client_->recv_control(target, timeout_ms);
    const std::string payload_type = trim_copy(type);
    if (!response || response->payload.empty() || payload_type.empty() || payload_type == "bytes") return response;
    const std::string decoded = decode_message_json(payload_type, response->payload);
    response->payload.assign(decoded.begin(), decoded.end());
    return response;
}

std::optional<ControlResponse> Subsystem::transact_control_impl(const std::string& target,
                                                     const std::string& topic,
                                                     const ByteBuffer& payload,
                                                     float period_ms,
                                                     float deadline_ms,
                                                     int timeout_ms,
                                                     int priority,
                                                     bool result_ready) {
    return transact_control_impl(target, topic, payload, period_ms, deadline_ms, timeout_ms, priority,
        result_ready, ControlClock::now());
}

std::optional<ControlResponse> Subsystem::transact_control_impl(const std::string& target,
    const std::string& topic, const ByteBuffer& payload, float period_ms, float deadline_ms,
    int timeout_ms, int priority, bool result_ready, ControlClock::time_point started) {
    std::shared_lock<std::shared_mutex> transaction_lifetime(codec_lifecycle_mutex_, std::try_to_lock);
    if (!transaction_lifetime.owns_lock()) throw std::runtime_error("Control transaction is unavailable during codec initialization");
    if (!ensure_core_client("transact_control")) return std::nullopt;
    return transact_core_started(*core_client_, name_, target, topic, payload, now_ns(), period_ms,
        deadline_ms, timeout_ms, priority, result_ready, started);
}

std::optional<ControlResponse> Subsystem::transact_control_impl(const std::string& target,
                                                     const std::string& topic,
                                                     const ByteBuffer& payload,
                                                     const ControlTransactOptions& options,
                                                     bool result_ready,
                                                     ControlClock::time_point started) {
    std::shared_lock<std::shared_mutex> transaction_lifetime(codec_lifecycle_mutex_, std::try_to_lock);
    if (!transaction_lifetime.owns_lock()) throw std::runtime_error("Control transaction is unavailable during codec initialization");
    return transact_control_unlocked(target, topic, payload, options, result_ready, started);
}

std::optional<ControlResponse> Subsystem::transact_control_unlocked(const std::string& target,
    const std::string& topic, const ByteBuffer& payload, const ControlTransactOptions& options,
    bool result_ready, ControlClock::time_point started) {
    const auto invocation_deadline = control_call_deadline(started, options.timeout_ms, options.deadline_ms);
    const auto prepare_route = [&](const PreparedCodec& request, const std::string& response_ref,
                                   const std::string& response_fingerprint) {
        require_control_deadline(invocation_deadline);
        if (auto* native = dynamic_cast<r2kernel::core::R2KernelClient*>(core_client_.get()))
            native->prepare_control_route_until(target, topic, request.message_ref(), request.fingerprint(),
                response_ref, response_fingerprint, invocation_deadline);
        else core_client_->prepare_control_route(target, topic, request.message_ref(), request.fingerprint(),
                response_ref, response_fingerprint);
        require_control_deadline(invocation_deadline);
    };
    const auto state = std::atomic_load(&prepared_state_);
    if (!options.request_type.empty() || !options.response_type.empty()) require_available_managed_codec(state);
    if (state && (!options.request_type.empty() || !options.response_type.empty())) {
        // Resolve both immutable handles from this transaction's one prepared
        // generation. The lifecycle guard already prevents reinitialization.
        const auto codec_for = [&state](const std::string& reference) -> const PreparedCodec& {
            auto found = state->codecs.find(reference);
            if (found == state->codecs.end()) found = state->codecs.find(trim_copy(reference));
            if (found == state->codecs.end())
                throw std::runtime_error("Undeclared codec was not prepared before Ready: " + reference);
            return found->second;
        };
        const auto& request = codec_for(options.request_type);
        if (!ensure_core_client("prepare_control_route")) return std::nullopt;
        // Typed Operation start returns a JSON receipt; only its request has a
        // message codec. Other typed calls still require both prepared handles.
        const std::string_view operation_prefix(kTypedOperationStartTopicPrefix);
        const bool operation_request_only = options.response_type.empty() &&
            topic.size() > operation_prefix.size() &&
            std::string_view(topic).substr(0, operation_prefix.size()) == operation_prefix;
        if (operation_request_only) {
            prepare_route(request, "", "");
        } else {
            const auto& response = codec_for(options.response_type);
            prepare_route(request, response.message_ref(), response.fingerprint());
        }
    }
    ByteBuffer timed_wire_payload;
    const ByteBuffer* wire_payload = &payload;
    int transport_priority = options.priority;
    if (options.timing.has_value()) {
        const auto& timing = *options.timing;
        ControlTimingMetadata meta;
        meta.request_id = request_id_u64_from_text(timing.request_id);
        meta.request_id_text = request_id_text_from_u64(meta.request_id);
        meta.generated_at_sync_ns = timing.generated_at_sync_ns.value_or(now_ns());
        meta.sent_at_sync_ns = now_ns();
        meta.execute_at_sync_ns = timing.execute_at_sync_ns.value_or(
            meta.sent_at_sync_ns + timing.execute_after_ns.value_or(0)
        );
        meta.max_lateness_ns = timing.max_lateness_ns;
        if (timing.valid_until_sync_ns.has_value()) {
            meta.valid_until_sync_ns = timing.valid_until_sync_ns;
        } else if (timing.valid_for_ns.has_value()) {
            meta.valid_until_sync_ns = meta.generated_at_sync_ns + *timing.valid_for_ns;
        }
        meta.priority = timing.priority;
        meta.late_policy = timing.late_policy;
        timed_wire_payload = encode_control_request_wire(meta, payload);
        wire_payload = &timed_wire_payload;
        if (transport_priority < 0) {
            transport_priority = meta.priority;
        }
    }

    if (!ensure_core_client("transact_control")) return std::nullopt;
    return transact_core_started(*core_client_, name_, target, topic, *wire_payload, now_ns(),
        options.period_ms, options.deadline_ms, options.timeout_ms, transport_priority, result_ready, started);
}

std::optional<ControlResponse> Subsystem::transact_control_impl(const std::string& target,
                                                     const std::string& topic,
                                                     const std::string& payload_json,
                                                     const ControlTransactOptions& options,
                                                     bool result_ready) {
    return transact_control_impl(target, topic, payload_json, options, result_ready, ControlClock::now());
}

std::optional<ControlResponse> Subsystem::transact_control_impl(const std::string& target,
    const std::string& topic, const std::string& payload_json, const ControlTransactOptions& options,
    bool result_ready, ControlClock::time_point started) {
    std::shared_lock<std::shared_mutex> transaction_lifetime(codec_lifecycle_mutex_, std::try_to_lock);
    if (!transaction_lifetime.owns_lock()) throw std::runtime_error("Control transaction is unavailable during codec initialization");
    ByteBuffer payload;
    const std::string request_type = trim_copy(options.request_type);
    if (!request_type.empty()) {
        payload = encode_message_json(request_type, payload_json.empty() ? "{}" : payload_json);
    } else {
        payload.assign(payload_json.begin(), payload_json.end());
    }

    if (options.timing.has_value()) {
        if (request_type.empty()) {
            throw std::invalid_argument("timed Control requires request_type");
        }
    }

    return transact_control_unlocked(target, topic, payload, options, result_ready, started);
}

void Subsystem::wait_control_reclaimed(const std::string& target, const std::string& topic, int timeout_ms) {
    std::shared_lock<std::shared_mutex> transaction_lifetime(codec_lifecycle_mutex_, std::try_to_lock);
    if (!transaction_lifetime.owns_lock())
        throw std::runtime_error("Control reclamation is unavailable during codec initialization");
    if (!ensure_core_client("wait_control_reclaimed"))
        throw std::runtime_error("Control reclamation requires a core client");
    core_client_->wait_control_reclaimed(target, core_transport_topic(*core_client_, name_, topic), timeout_ms);
}

ByteBuffer Subsystem::encode_message_json(const std::string& message_ref,
                                          const std::string& payload_json) const {
    const auto state = std::atomic_load(&prepared_state_);
    require_available_managed_codec(state);
    if (!state) return codec_encode_message_json(logger_, trim_copy(message_ref), payload_json);
    thread_local CodecWorkspace workspace;
    auto found = state->codecs.find(message_ref);
    if (found == state->codecs.end()) found = state->codecs.find(trim_copy(message_ref));
    if (found == state->codecs.end()) throw std::runtime_error("Undeclared codec was not prepared before Ready: " + message_ref);
    return found->second.encode_json(payload_json, workspace).copy();
}

std::string Subsystem::decode_message_json(const std::string& message_ref,
                                           const ByteBuffer& payload) const {
    const auto state = std::atomic_load(&prepared_state_);
    require_available_managed_codec(state);
    if (!state) return codec_decode_message_json(logger_, trim_copy(message_ref), payload);
    thread_local CodecWorkspace workspace;
    auto found = state->codecs.find(message_ref);
    if (found == state->codecs.end()) found = state->codecs.find(trim_copy(message_ref));
    if (found == state->codecs.end()) throw std::runtime_error("Undeclared codec was not prepared before Ready: " + message_ref);
    const auto view = found->second.decode_json(payload, workspace);
    return std::string(view);
}

DecodedControlResponse Subsystem::decode_control_response_impl(const ControlResponse& response,
                                                               const std::string& response_type) const {
    const auto ref = trim_copy(response_type);
    if (!ref.empty()) require_available_managed_codec(std::atomic_load(&prepared_state_));
    auto decoded = decode_control_response_wire(response.payload);
    if (!ref.empty() && !decoded.payload.empty())
        decoded.payload_json = decode_message_json(response_type, decoded.payload);
    return decoded;
}

#include "subsystem_lifecycle.hpp"

#include "subsystem_dispatch.hpp"

#include "subsystem_operation_dispatch.hpp"

#include "subsystem_bound_calls.hpp"

}  // namespace r2kernel::wrapper
