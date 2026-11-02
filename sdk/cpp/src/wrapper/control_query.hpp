#pragma once

// Subsystem-owned Control socket queries (binding status and resolution,
// frame lookup): one authenticated JSON exchange per call, as the Python
// SDK's exchange_control_request(..., retries=1). Internal (hidden).

#include "../core/control_requests.hpp"
#include "../core/json_value.hpp"

#include <chrono>
#include <optional>
#include <string>

namespace r2kernel::wrapper::detail {

// Default budget of one query (Python timeout_s=1.0).
inline std::chrono::steady_clock::time_point control_query_deadline() {
    return std::chrono::steady_clock::now() + std::chrono::seconds(1);
}

// The reply as a JSON document, or std::nullopt for an empty reply (Python
// None). Transport failures, the deadline and a reply that is not JSON throw
// std::runtime_error.
inline std::optional<r2kernel::sdk_detail::JsonValue> control_query(const std::string& request,
    std::chrono::steady_clock::time_point deadline = control_query_deadline()) {
    const std::string reply = r2kernel::core::detail::exchange_control_request(request, deadline);
    if (reply.find_first_not_of(" \t\r\n") == std::string::npos) return std::nullopt;
    return r2kernel::sdk_detail::parse_json(reply);
}

inline std::string trimmed(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\n\r\f\v");
    if (begin == std::string::npos) return {};
    const auto end = value.find_last_not_of(" \t\n\r\f\v");
    return value.substr(begin, end - begin + 1);
}

// Python str(member or "").strip() of a reply member (absent, null or
// false-y: "").
inline std::string reply_text(const r2kernel::sdk_detail::JsonValue* value) {
    if (value == nullptr || !value->truthy()) return {};
    return trimmed(value->is_string() ? value->text() : value->dump());
}

// Python repr() of a reply member, for error text.
inline std::string reply_repr(const r2kernel::sdk_detail::JsonValue* value) {
    if (value == nullptr || value->is_null()) return "None";
    if (value->is_bool()) return value->as_bool() ? "True" : "False";
    if (value->is_string()) return "'" + value->text() + "'";
    return value->dump();
}

}  // namespace r2kernel::wrapper::detail
