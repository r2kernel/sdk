// Cold, subsystem-owned control-socket requests: requirement self-binding
// (interface binding_self_set / binding_self_unset). A separate translation
// unit so this setup-time code stays out of client.cpp's per-call codegen; the
// socket exchange itself is client.cpp's (detail::exchange_control_request).
#include "control_requests.hpp"

#include "json_text.hpp"
#include "yaml_json.hpp"
#include "r2kernel/core/client.hpp"

#include <yaml-cpp/yaml.h>

#include <sys/socket.h>
#include <sys/un.h>

#include <algorithm>
#include <cstdint>
#include <cctype>
#include <iostream>
#include <optional>
#include <vector>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace r2kernel::wrapper {
ControlRequestError::ControlRequestError(std::string reason, const std::string& error)
    : std::runtime_error(reason.empty() ? error : reason + ": " + error), reason_(std::move(reason)) {}
ControlRequestError::~ControlRequestError() = default;
}  // namespace r2kernel::wrapper

namespace r2kernel::core::detail {
namespace {

using r2kernel::sdk_detail::json_escape;
using r2kernel::sdk_detail::yaml_json_text;

std::string json_object(std::string_view body) {
    return "{" + std::string(body) + "}";
}


// Exact text of one member of the reply's top-level JSON object. Nested
// objects are skipped as a whole, so a same-named nested key never matches.
std::optional<std::string> json_top_level_member(const std::string& text, std::string_view key) {
    std::size_t pos = text.find('{');
    if (pos == std::string::npos) return std::nullopt;
    int depth = 0;
    bool in_string = false, escaped = false;
    std::size_t key_start = std::string::npos;
    std::string current_key;
    bool expect_key = true;
    for (; pos < text.size(); ++pos) {
        const char ch = text[pos];
        if (in_string) {
            if (escaped) escaped = false;
            else if (ch == '\\') escaped = true;
            else if (ch == '"') {
                in_string = false;
                if (depth == 1 && expect_key) current_key = text.substr(key_start, pos - key_start);
            }
            continue;
        }
        if (ch == '"') {
            in_string = true;
            key_start = pos + 1;
        } else if (ch == '{' || ch == '[') {
            ++depth;
        } else if (ch == '}' || ch == ']') {
            --depth;
        } else if (depth == 1 && ch == ':' && expect_key) {
            expect_key = false;
            if (current_key == key) {
                std::size_t start = pos + 1;
                while (start < text.size() && std::isspace(static_cast<unsigned char>(text[start]))) ++start;
                std::size_t end = start;
                int value_depth = 0;
                bool value_string = false, value_escaped = false;
                for (; end < text.size(); ++end) {
                    const char value = text[end];
                    if (value_string) {
                        if (value_escaped) value_escaped = false;
                        else if (value == '\\') value_escaped = true;
                        else if (value == '"') value_string = false;
                        continue;
                    }
                    if (value == '"') value_string = true;
                    else if (value == '{' || value == '[') ++value_depth;
                    else if (value == '}' || value == ']') {
                        if (value_depth == 0) break;
                        --value_depth;
                    } else if (value == ',' && value_depth == 0) break;
                }
                while (end > start && std::isspace(static_cast<unsigned char>(text[end - 1]))) --end;
                if (end == start) return std::nullopt;
                return text.substr(start, end - start);
            }
        } else if (depth == 1 && ch == ',') {
            expect_key = true;
        }
    }
    return std::nullopt;
}

// Maps the reply envelope (interface binding_self_set/unset) to the result
// document or an explicit error: {"status":"error"} → ControlRequestError with
// an empty reason; {"ok":false} → ControlRequestError(reason, error).
YAML::Node checked_self_binding_reply(const char* command, const std::string& reply) {
    YAML::Node document;
    try {
        document = YAML::Load(reply);
    } catch (const YAML::Exception&) {
        throw std::runtime_error(std::string(command) + " reply is not valid JSON: " + reply);
    }
    if (!document.IsMap()) throw std::runtime_error(std::string(command) + " reply is not a JSON object: " + reply);
    const auto text_field = [&](const char* name) -> std::optional<std::string> {
        const auto node = document[name];
        if (!node || !node.IsScalar()) return std::nullopt;
        return node.as<std::string>();
    };
    if (text_field("status") == std::optional<std::string>("error")) {
        const auto error = text_field("error");
        throw r2kernel::wrapper::ControlRequestError({},
            error && !error->empty() ? *error : std::string(command) + " returned an error without text");
    }
    const auto ok = document["ok"];
    if (!ok || !ok.IsScalar()) throw std::runtime_error(std::string(command) + " reply lacks ok: " + reply);
    if (!ok.as<bool>()) {
        const auto reason = text_field("reason");
        const auto error = text_field("error");
        if (!reason || !error)
            throw std::runtime_error(std::string(command) + " refusal lacks reason or error: " + reply);
        throw r2kernel::wrapper::ControlRequestError(*reason, *error);
    }
    if (text_field("res") != std::optional<std::string>("ok"))
        throw std::runtime_error(std::string(command) + " returned an unexpected response: " + reply);
    return document;
}

// Same argument rule as the Python SDK: a non-empty, non-blank string.
void require_self_binding_argument(const char* command, const char* name, const std::string& value) {
    if (value.find_first_not_of(" \t\n\r\f\v") == std::string::npos)
        throw std::invalid_argument(std::string(command) + ": " + name + " must be a non-empty string");
}

constexpr char kBindingSelfSet[] = "binding_self_set";
constexpr char kBindingSelfUnset[] = "binding_self_unset";

}  // namespace

std::string request_self_binding_set(const ClientOptions& options, const std::string& requirement,
    const std::string& target, std::optional<std::chrono::steady_clock::time_point> deadline) {
    require_self_binding_argument(kBindingSelfSet, "requirement", requirement);
    require_self_binding_argument(kBindingSelfSet, "target", target);
    if (options.session_id.empty())
        throw std::runtime_error("bind_requirement requires a registered subsystem session");
    const std::string request = json_object(
        "\"cmd\":\"" + std::string(kBindingSelfSet) + "\","
        "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
        "\"session_id\":\"" + json_escape(options.session_id) + "\","
        "\"requirement\":\"" + json_escape(requirement) + "\","
        "\"target\":\"" + json_escape(target) + "\"");
    const auto reply = exchange_control_request(request, deadline);
    (void)checked_self_binding_reply(kBindingSelfSet, reply);
    auto binding = json_top_level_member(reply, "binding");
    if (!binding || binding->front() != '{')
        throw std::runtime_error(std::string(kBindingSelfSet) + " success reply lacks the binding row: " + reply);
    return *binding;
}

std::optional<EventReceiveSizes> grown_event_receive_sizes(EventReceiveSizes capacity, EventReceiveSizes reported) {
    if (reported.topic <= capacity.topic && reported.publisher <= capacity.publisher
        && reported.payload <= capacity.payload) {
        std::cerr << "Event receive buffer too small, but Core reported no larger length (topic=" << reported.topic
                  << ", publisher=" << reported.publisher << ", payload=" << reported.payload
                  << "); the Event was not delivered\n";
        return std::nullopt;
    }
    return EventReceiveSizes{std::max(capacity.topic, reported.topic), std::max(capacity.publisher, reported.publisher),
                             std::max(capacity.payload, reported.payload)};
}

void receive_oversized_event(R2KernelEventClient* client, std::mutex& mutex,
    std::unordered_map<std::string, r2kernel::wrapper::EventSubscribeSpec>& subscriptions,
    std::vector<std::uint8_t>& payload_buffer, EventRetryNameBuffers& names,
    std::size_t topic_len, std::size_t publisher_len, std::size_t payload_len) {
    const auto sizes = grown_event_receive_sizes(
        {kEventReceiveNameCapacity, kEventReceiveNameCapacity, payload_buffer.size()},
        {topic_len, publisher_len, payload_len});
    if (!sizes) return;
    if (payload_buffer.size() < sizes->payload) payload_buffer.resize(sizes->payload, 0);
    if (names.topic.size() < sizes->topic) names.topic.resize(sizes->topic, 0);
    if (names.publisher.size() < sizes->publisher) names.publisher.resize(sizes->publisher, 0);
    std::int64_t timestamp = 0, published_at_sync_ns = 0, received_at_sync_ns = 0;
    const int rc = r2kernel_event_client_recv_ex(client, 0, names.topic.data(), names.topic.size(), &topic_len,
        names.publisher.data(), names.publisher.size(), &publisher_len, &timestamp, &published_at_sync_ns,
        &received_at_sync_ns, payload_buffer.data(), payload_buffer.size(), &payload_len);
    if (rc != 0) {
        std::cerr << "Event receive retry with the reported buffer sizes failed rc=" << rc
                  << "; the Event was not delivered\n";
        return;
    }
    const std::string topic(reinterpret_cast<const char*>(names.topic.data()), topic_len);
    std::unique_lock<std::mutex> guard(mutex);
    const auto it = subscriptions.find(topic);
    if (it == subscriptions.end()) return;
    const r2kernel::wrapper::EventCallback callback = it->second.callback;
    const r2kernel::wrapper::EventMetadataCallback metadata_callback = it->second.metadata_callback;
    guard.unlock();
    const r2kernel::wrapper::EventPayload payload(payload_buffer.begin(),
        payload_buffer.begin() + static_cast<std::ptrdiff_t>(payload_len));
    if (metadata_callback) {
        metadata_callback(payload, r2kernel::wrapper::EventTimingMetadata {timestamp, published_at_sync_ns, received_at_sync_ns});
    }
    if (callback) callback(payload, timestamp);
}

void report_event_inline_refusal(const std::string& endpoint, const std::string& topic, const char* reason) {
    std::cerr << "Event inline delivery disabled for endpoint " << endpoint
              << " until the subsystem registers again: publish on topic " << topic << " failed: " << reason
              << "; Events are delivered through the lock notification instead\n";
}

std::string event_inline_endpoint_refusal(const std::string& endpoint) {
    if (endpoint.size() < sizeof(sockaddr_un::sun_path)) return {};
    return "Event inline delivery disabled: the registered endpoint exceeds the Unix socket path limit ("
        + std::to_string(sizeof(sockaddr_un::sun_path) - 1) + " bytes): " + endpoint;
}

std::string registered_event_inline_endpoint(const std::string& endpoint) {
    const auto refusal = event_inline_endpoint_refusal(endpoint);
    if (refusal.empty()) return endpoint;
    std::cerr << refusal << '\n';
    return {};
}

// Control's resolve_control_target reply as a ControlRoute, or nullopt
// when the reply is not ok or names no host/port.
std::optional<r2kernel::wrapper::ControlRoute> parse_control_route(const std::string& response) {
    const YAML::Node document = YAML::Load(response);
    if (!document.IsMap() || !document["res"] || document["res"].Scalar() != "ok") return std::nullopt;
    const auto host = document["host"];
    const auto port = document["control_port"];
    if (!host || !host.IsScalar() || !port || !port.IsScalar()) return std::nullopt;
    const auto text = [&document](const char* key) {
        const auto value = document[key];
        return value && value.IsScalar() ? value.Scalar() : std::string{};
    };
    const auto names = [&document](const char* key) {
        std::vector<std::string> out;
        const auto value = document[key];
        if (value && value.IsSequence())
            for (const auto& item : value) out.push_back(item.Scalar());
        return out;
    };
    const auto json = [&document](const char* key) {
        const auto value = document[key];
        return value && value.IsMap() ? yaml_json_text(value) : std::string("{}");
    };
    r2kernel::wrapper::ControlRoute route;
    route.subsystem = text("subsystem");
    route.host = host.Scalar();
    route.port = port.as<int>();
    route.route_kind = text("route_kind");
    route.relay_enabled = document["control_relay_enabled"] && document["control_relay_enabled"].as<bool>(false);
    if (document["peer_id"] && document["peer_id"].IsScalar()) route.peer_id = document["peer_id"].Scalar();
    route.session_id = text("session_id");
    if (document["registration_revision"] && document["registration_revision"].IsScalar())
        route.registration_revision = document["registration_revision"].Scalar();
    if (document["codec_context_available"] && document["codec_context_available"].IsScalar())
        route.codec_context_available = document["codec_context_available"].as<bool>();
    route.controls = names("controls");
    route.procedures = names("procedures");
    route.operations = names("operations");
    route.control_contracts_json = json("control_contracts");
    route.procedure_contracts_json = json("procedure_contracts");
    route.operation_contracts_json = json("operation_contracts");
    route.codec_metadata_json = json("codec_metadata");
    route.contract_codecs_json = json("contract_codecs");
    route.json = yaml_json_text(document);
    return route;
}

bool request_self_binding_unset(const ClientOptions& options, const std::string& requirement,
    std::optional<std::chrono::steady_clock::time_point> deadline) {
    require_self_binding_argument(kBindingSelfUnset, "requirement", requirement);
    if (options.session_id.empty())
        throw std::runtime_error("unbind_requirement requires a registered subsystem session");
    const std::string request = json_object(
        "\"cmd\":\"" + std::string(kBindingSelfUnset) + "\","
        "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
        "\"session_id\":\"" + json_escape(options.session_id) + "\","
        "\"requirement\":\"" + json_escape(requirement) + "\"");
    const auto reply = exchange_control_request(request, deadline);
    const auto document = checked_self_binding_reply(kBindingSelfUnset, reply);
    const auto removed = document["removed"];
    if (!removed || !removed.IsScalar())
        throw std::runtime_error(std::string(kBindingSelfUnset) + " success reply lacks removed: " + reply);
    return removed.as<bool>();
}

}  // namespace r2kernel::core::detail
