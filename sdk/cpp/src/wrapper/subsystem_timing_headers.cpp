// Timing header helpers of a Subsystem, matching the Python SDK's
// build/normalize/pack/unpack timing functions (r2kernel.core.timing). Values
// are JSON object text shaped like the Python dicts. Cold code in its own
// translation unit.
#include "r2kernel/wrapper/subsystem.hpp"

#include "../core/json_text.hpp"
#include "../core/yaml_json.hpp"

#include <yaml-cpp/yaml.h>

#include <cstdint>
#include <random>
#include <stdexcept>
#include <string>

namespace r2kernel::wrapper {

namespace {

using r2kernel::sdk_detail::json_escape;
using r2kernel::sdk_detail::yaml_json_text;

YAML::Node json_value(const std::string& text, const char* what) {
    try {
        return YAML::Load(text);
    } catch (const YAML::Exception& error) {
        throw std::invalid_argument(std::string(what) + " is not valid JSON: " + error.what());
    }
}

YAML::Node json_object(const std::string& text, const char* what) {
    auto node = json_value(text, what);
    if (!node.IsMap()) throw std::invalid_argument(std::string(what) + " must be a JSON object");
    return node;
}

std::string trimmed(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n\f\v");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n\f\v") - first + 1);
}

// "<prefix>-" followed by 8 random hex digits, as Python's uuid4().hex[:8].
std::string random_id(const char* prefix) {
    thread_local std::mt19937 generator(std::random_device {}());
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out = std::string(prefix) + "-";
    for (int i = 0; i < 8; ++i) out.push_back(kHex[generator() & 0x0f]);
    return out;
}

std::string late_policy_name(const std::string& late_policy) {
    if (late_policy == "drop" || late_policy == "reject" || late_policy == "execute_immediately") return late_policy;
    throw std::invalid_argument("late_policy must be one of drop, reject, execute_immediately; found '"
        + late_policy + "'");
}

std::string pack_timing_wire(const char* header_key, const std::string& header_json, const std::string& payload_ref,
    const std::string& payload_json) {
    const auto header = json_object(header_json, header_key);
    const auto payload = json_object(payload_json, "timing payload");
    return std::string("{\"") + header_key + "\":" + yaml_json_text(header) + ",\"payload_ref\":\""
        + json_escape(payload_ref) + "\",\"payload\":" + yaml_json_text(payload) + "}";
}

TimingEnvelope unpack_timing_wire(const char* kind, const char* header_key, const std::string& wire_json) {
    const auto wire = json_value(wire_json, (std::string("timing ") + kind + " wire payload").c_str());
    if (!wire.IsMap()) throw std::invalid_argument(std::string("timing ") + kind + " wire payload must be an object");
    const auto header = wire[header_key];
    const auto payload_ref = wire["payload_ref"];
    const auto payload = wire["payload"];
    if (!header || !header.IsMap())
        throw std::invalid_argument(std::string("timing ") + kind + " is missing " + header_key);
    const std::string ref = payload_ref && payload_ref.IsScalar() ? trimmed(payload_ref.Scalar()) : std::string{};
    if (ref.empty()) throw std::invalid_argument(std::string("timing ") + kind + " is missing payload_ref");
    if (!payload || !payload.IsMap())
        throw std::invalid_argument(std::string("timing ") + kind + " is missing payload object");
    return TimingEnvelope {yaml_json_text(header), ref, yaml_json_text(payload)};
}

}  // namespace

std::string Subsystem::build_timing_request_header(const std::string& target_subsystem,
                                                   const ControlTimingOptions& timing,
                                                   const std::string& source_subsystem) const {
    const std::int64_t now = now_ns();
    if (timing.valid_for_ns && timing.valid_until_sync_ns)
        throw std::invalid_argument("valid_for_ns and valid_until_sync_ns are mutually exclusive");
    const std::string target = trimmed(target_subsystem);
    const std::string source = trimmed(source_subsystem.empty() ? name() : source_subsystem);
    if (target.empty()) throw std::invalid_argument("target_subsystem is required");
    if (source.empty()) throw std::invalid_argument("source_subsystem is required");
    const std::int64_t generated_at = timing.generated_at_sync_ns.value_or(now);
    std::optional<std::int64_t> valid_until = timing.valid_until_sync_ns;
    if (timing.valid_for_ns) valid_until = generated_at + *timing.valid_for_ns;

    std::string execution;
    if (timing.execute_at_sync_ns) {
        execution = "\"mode\":\"at_time\",\"execute_at_sync_ns\":" + std::to_string(*timing.execute_at_sync_ns);
    } else if (timing.execute_after_ns) {
        execution = "\"mode\":\"after_offset\",\"execute_after_ns\":" + std::to_string(*timing.execute_after_ns);
    } else {
        execution = "\"mode\":\"immediate\"";
    }
    if (timing.max_lateness_ns) execution += ",\"max_lateness_ns\":" + std::to_string(*timing.max_lateness_ns);
    if (valid_until) execution += ",\"valid_until_sync_ns\":" + std::to_string(*valid_until);
    execution += ",\"late_policy\":\"" + late_policy_name(timing.late_policy) + "\"";

    const std::string request_id = timing.request_id.empty() ? random_id("req") : timing.request_id;
    const std::string correlation_id = timing.correlation_id.empty() ? random_id("corr") : timing.correlation_id;
    return "{\"request_id\":\"" + json_escape(request_id) + "\",\"correlation_id\":\"" + json_escape(correlation_id)
        + "\",\"source_subsystem\":\"" + json_escape(source) + "\",\"target_subsystem\":\"" + json_escape(target)
        + "\",\"sent_at_sync_ns\":" + std::to_string(now) + ",\"generated_at_sync_ns\":" + std::to_string(generated_at)
        + ",\"priority\":" + std::to_string(timing.priority) + ",\"execution\":{" + execution + "}}";
}

std::pair<std::string, std::int64_t> Subsystem::normalize_timing_request_header(
    const std::string& header_json, std::optional<std::int64_t> accepted_at_sync_ns) const {
    const std::int64_t accepted = accepted_at_sync_ns.value_or(now_ns());
    auto header = json_object(header_json, "request_header");
    auto execution = header["execution"];
    if (!execution.IsMap()) return {yaml_json_text(header), accepted};
    const std::string mode = execution["mode"] && execution["mode"].IsScalar() && !execution["mode"].Scalar().empty()
        ? execution["mode"].Scalar() : "immediate";
    if (!execution["execute_at_sync_ns"]) {
        if (mode == "after_offset") {
            const auto after = execution["execute_after_ns"];
            execution["execute_at_sync_ns"] = accepted + (after ? after.as<std::int64_t>() : 0);
        } else if (mode == "immediate") {
            execution["execute_at_sync_ns"] = accepted;
        } else if (mode != "at_time") {
            throw std::invalid_argument("unsupported timing mode: " + mode);
        }
    }
    return {yaml_json_text(header), accepted};
}

std::string Subsystem::build_timing_response_header(const std::string& request_header_json,
                                                    const std::string& execution_report_json,
                                                    int status_code,
                                                    const std::string& status_message,
                                                    std::optional<std::int64_t> responded_at_sync_ns) const {
    const std::int64_t responded = responded_at_sync_ns.value_or(now_ns());
    const auto request_header = json_object(request_header_json, "request_header");
    const auto report = json_object(execution_report_json, "execution_report");
    if (!request_header["request_id"]) throw std::invalid_argument("request_header is missing request_id");
    return "{\"request_id\":" + yaml_json_text(request_header["request_id"])
        + ",\"correlation_id\":" + yaml_json_text(request_header["correlation_id"])
        + ",\"received_at_sync_ns\":" + yaml_json_text(report["accepted_at_sync_ns"])
        + ",\"responded_at_sync_ns\":" + std::to_string(responded)
        + ",\"status_code\":" + std::to_string(status_code)
        + ",\"status_message\":\"" + json_escape(status_message)
        + "\",\"execution_report\":" + yaml_json_text(report) + "}";
}

std::string Subsystem::pack_timing_request(const std::string& request_header_json, const std::string& payload_ref,
                                           const std::string& payload_json) const {
    return pack_timing_wire("request_header", request_header_json, payload_ref, payload_json);
}

TimingEnvelope Subsystem::unpack_timing_request(const std::string& wire_json) const {
    return unpack_timing_wire("request", "request_header", wire_json);
}

std::string Subsystem::pack_timing_response(const std::string& response_header_json, const std::string& payload_ref,
                                            const std::string& payload_json) const {
    return pack_timing_wire("response_header", response_header_json, payload_ref, payload_json);
}

TimingEnvelope Subsystem::unpack_timing_response(const std::string& wire_json) const {
    return unpack_timing_wire("response", "response_header", wire_json);
}

}  // namespace r2kernel::wrapper
