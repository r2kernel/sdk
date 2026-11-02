// Subsystem queries (Python SDK wrapper/subsystem.py and
// _subsystem_communication.py): the composed frame graph and the runtime
// bindings of the required interfaces, answered by Control, and the declared
// runtime endpoints. Cold control-plane code in its own translation unit.
#include "r2kernel/wrapper/subsystem.hpp"

#include "control_query.hpp"
#include "../core/json_text.hpp"

#include <algorithm>
#include <stdexcept>
#include <string>

namespace r2kernel::wrapper {
namespace {

using detail::reply_repr;
using detail::reply_text;
using detail::trimmed;
using r2kernel::sdk_detail::JsonValue;
using r2kernel::sdk_detail::json_escape;
using Clock = std::chrono::steady_clock;

constexpr char kBindingTag[] = "Lifecycle";

std::string binding_resolve_request(const std::string& subsystem, const std::string& requirement) {
    return "{\"cmd\":\"binding_resolve\",\"subsystem\":\"" + json_escape(subsystem) + "\",\"requirement\":\""
        + json_escape(requirement) + "\"}";
}

[[noreturn]] void binding_deadline_exceeded() {
    throw std::runtime_error("Control binding resolution deadline exceeded");
}

// A runtime binding row in state bound with both resolved fields.
BoundInterface bound_interface_from_runtime_binding(const std::string& subsystem, const SubsystemManifest& manifest,
                                                    const std::string& requirement, const JsonValue& binding) {
    const std::string source = subsystem + "." + requirement;
    const std::string state = reply_text(binding.find("state"));
    if (state != "bound")
        throw std::runtime_error("Runtime binding is not callable: " + source + " state=" + (state.empty() ? "unknown" : state));
    const std::string target_subsystem = reply_text(binding.find("resolved_target_subsystem"));
    const std::string provider_interface = reply_text(binding.find("resolved_provider_interface"));
    if (target_subsystem.empty() || provider_interface.empty())
        throw std::runtime_error("Runtime binding is not callable: " + source + " has no resolved provider target");
    return BoundInterface {requirement, target_subsystem, provider_interface,
                           manifest.requirements.at(requirement).contract};
}

// resolve_binding without a deadline (Python _resolve_runtime_binding): an
// unavailable Control or a reply without a binding is std::nullopt, so the
// launch and deployment bindings apply; an explicit unbind still throws.
std::optional<BoundInterface> available_runtime_binding(const std::string& subsystem, const SubsystemManifest& manifest,
                                                        const Logger& logger, const std::string& requirement) {
    if (manifest.requirements.count(requirement) == 0)
        throw std::runtime_error("Subsystem " + subsystem + " does not declare required interface: " + requirement);
    std::optional<JsonValue> response;
    try {
        response = detail::control_query(binding_resolve_request(subsystem, requirement));
    } catch (const std::exception& error) {
        logger.debug(kBindingTag, std::string("Runtime binding resolve unavailable: ") + error.what());
        return std::nullopt;
    }
    if (!response || !response->is_object()) return std::nullopt;
    const JsonValue* runtime = response->find("runtime");
    if (response->member_equals("res", "unbound") && runtime != nullptr && runtime->truthy())
        throw std::runtime_error("Runtime binding is explicitly unbound: " + subsystem + "." + requirement);
    const JsonValue* ok = response->find("ok");
    if (ok == nullptr || !ok->truthy()) return std::nullopt;
    const JsonValue* binding = response->find("binding");
    if (binding == nullptr || !binding->is_object()) return std::nullopt;
    return bound_interface_from_runtime_binding(subsystem, manifest, requirement, *binding);
}

}  // namespace

std::string Subsystem::lookup_frame_transform(const std::string& source_frame, const std::string& target_frame) const {
    const std::string source = trimmed(source_frame);
    const std::string target = trimmed(target_frame);
    if (source.empty() || target.empty()) throw std::invalid_argument("source_frame and target_frame are required");
    const auto reply = detail::control_query("{\"cmd\":\"frame_lookup\",\"source\":\"" + json_escape(source)
                                            + "\",\"target\":\"" + json_escape(target) + "\"}");
    return reply ? reply->dump() : "null";
}

std::vector<std::string> Subsystem::declared_runtime_endpoints(const std::string& endpoint_kind) const {
    const std::unordered_set<std::string>* endpoints = nullptr;
    if (endpoint_kind == "publishes") endpoints = &allowed_event_publish_topics_;
    else if (endpoint_kind == "subscribes") endpoints = &allowed_event_subscribe_topics_;
    else if (endpoint_kind == "controls") endpoints = &allowed_control_topics_;
    else if (endpoint_kind == "procedures") endpoints = &allowed_procedure_names_;
    else if (endpoint_kind == "operations") endpoints = &allowed_operation_names_;
    if (endpoints == nullptr) return {};
    std::vector<std::string> names(endpoints->begin(), endpoints->end());
    std::sort(names.begin(), names.end());
    return names;
}

std::map<std::string, BindingReference> Subsystem::launch_bindings() const {
    std::map<std::string, BindingReference> bindings;
    for (const auto& [requirement, target] : launch_bindings_)
        bindings[requirement] = BindingReference::parse(target, "R2KERNEL_BINDINGS." + requirement);
    return bindings;
}

std::map<std::string, BindingReference> Subsystem::runtime_bindings() const {
    std::optional<JsonValue> response;
    try {
        response = detail::control_query("{\"cmd\":\"binding_status\",\"subsystem\":\"" + json_escape(name_) + "\"}");
    } catch (const std::exception& error) {
        logger_.debug(kBindingTag, std::string("Runtime binding status unavailable: ") + error.what());
        return {};
    }
    std::map<std::string, BindingReference> bindings;
    if (!response || !response->is_object()) return bindings;
    const JsonValue* items = response->find("bindings");
    if (items == nullptr || !items->is_array()) return bindings;
    for (const auto& item : items->items()) {
        if (!item.is_object() || !item.member_equals("source_subsystem", name_)) continue;
        const std::string requirement = reply_text(item.find("requirement"));
        const std::string target = reply_text(item.find("target"));
        if (requirement.empty() || target.empty()) continue;
        if (subsystem_manifest_.requirements.count(requirement) == 0) continue;
        try {
            bindings[requirement] = BindingReference::parse(target, "runtime binding " + name_ + "." + requirement);
        } catch (const ManifestError& error) {
            logger_.warning(kBindingTag, std::string("Ignoring invalid runtime binding: ") + error.what());
        }
    }
    return bindings;
}

std::map<std::string, BindingReference> Subsystem::effective_bindings() const {
    auto bindings = launch_bindings();
    for (auto& [requirement, target] : runtime_bindings()) bindings[requirement] = std::move(target);
    return bindings;
}

BoundInterface Subsystem::resolve_binding(const std::string& requirement,
                                          std::optional<std::chrono::steady_clock::time_point> deadline) const {
    std::optional<BoundInterface> runtime;
    if (!deadline) {
        runtime = available_runtime_binding(name_, subsystem_manifest_, logger_, requirement);
    } else {
        runtime = resolve_runtime_binding(requirement, deadline);
        if (!runtime) throw std::runtime_error("Runtime binding is not ready: " + name_ + "." + requirement);
    }
    if (runtime) return *runtime;
    if (const auto direct = launch_bindings_.find(requirement); direct != launch_bindings_.end()) {
        const auto required = subsystem_manifest_.requirements.find(requirement);
        if (required == subsystem_manifest_.requirements.end())
            throw std::runtime_error("Subsystem " + name_ + " does not declare required interface: " + requirement);
        const auto target = BindingReference::parse(direct->second, "R2KERNEL_BINDINGS." + requirement);
        if (deployment_manifest_) return deployment_manifest_->resolve(name_, requirement, target);
        return BoundInterface {requirement, target.subsystem, target.interface_name, required->second.contract};
    }
    if (!deployment_manifest_) throw std::runtime_error("Required binding is missing: " + name_ + "." + requirement);
    return deployment_manifest_->resolve(name_, requirement);
}

std::string Subsystem::binding_target(const std::string& requirement,
                                      std::optional<std::chrono::steady_clock::time_point> deadline) const {
    return resolve_binding(requirement, deadline).target();
}

std::optional<BoundInterface> Subsystem::resolve_runtime_binding(
    const std::string& requirement, std::optional<std::chrono::steady_clock::time_point> deadline) const {
    if (deadline && Clock::now() >= *deadline) binding_deadline_exceeded();
    const auto required = subsystem_manifest_.requirements.find(requirement);
    if (required == subsystem_manifest_.requirements.end())
        throw std::runtime_error("Subsystem " + name_ + " does not declare required interface: " + requirement);
    const std::string source = name_ + "." + requirement;
    std::optional<JsonValue> reply;
    try {
        reply = detail::control_query(binding_resolve_request(name_, requirement),
                                      deadline ? *deadline : detail::control_query_deadline());
    } catch (const std::exception& error) {
        if (deadline && Clock::now() >= *deadline) binding_deadline_exceeded();
        throw std::runtime_error("Runtime binding resolve failed for " + source + ": " + error.what());
    }
    if (deadline && Clock::now() >= *deadline) binding_deadline_exceeded();
    if (!reply || !reply->is_object())
        throw std::runtime_error("Runtime binding resolve returned a malformed response for " + source);
    const JsonValue& response = *reply;
    const auto check_response_identity = [&]() {
        if (!response.member_equals("subsystem", name_))
            throw std::runtime_error("Runtime binding response subsystem mismatch for " + source);
        if (!response.member_equals("requirement", requirement))
            throw std::runtime_error("Runtime binding response requirement mismatch for " + source);
        if (!response.member_equals("source", source))
            throw std::runtime_error("Runtime binding response source mismatch for " + source);
    };
    const auto check_binding_identity = [&](const JsonValue& binding) {
        if (!binding.member_is("runtime", true))
            throw std::runtime_error("Runtime binding response is not marked runtime for " + source);
        if (!binding.member_equals("source_subsystem", name_))
            throw std::runtime_error("Runtime binding source subsystem mismatch for " + source);
        if (!binding.member_equals("requirement", requirement))
            throw std::runtime_error("Runtime binding requirement mismatch for " + source);
        if (!binding.member_equals("source", source))
            throw std::runtime_error("Runtime binding source mismatch for " + source);
    };
    const bool refused = response.member_is("ok", false);
    if (refused && response.member_equals("res", "unbound") && response.member_is("runtime", true)) {
        check_response_identity();
        throw std::runtime_error("Runtime binding is explicitly unbound: " + source);
    }
    if (refused && response.member_equals("res", "missing")) {
        const JsonValue* reason = response.find("reason");
        check_response_identity();
        const JsonValue* binding = response.find("binding");
        if (reason != nullptr && reason->is_string() && reason->text() == "not_bound") {
            if (binding != nullptr && !binding->is_null())
                throw std::runtime_error("Runtime binding resolve returned a malformed not_bound response for " + source);
            return std::nullopt;
        }
        if (reason != nullptr && reason->is_string() && reason->text() == "source_registration_pending") {
            if (binding == nullptr || !binding->is_object())
                throw std::runtime_error("Runtime binding resolve returned a malformed pending binding for " + source);
            check_binding_identity(*binding);
            const std::string state = reply_text(binding->find("state"));
            // A restarted source asks before its first status; a binding pinned to
            // its previous registration then reports registration_changed.
            if (state != "source_missing" && state != "source_role_missing" && state != "registration_changed") {
                throw std::runtime_error("Runtime binding pending response has unexpected state for " + source + ": "
                                         + (state.empty() ? "unknown" : state));
            }
            return std::nullopt;
        }
        std::string detail = reply_text(reason);
        if (detail.empty()) detail = reply_text(response.find("error"));
        if (detail.empty()) detail = "unknown missing-binding reason";
        throw std::runtime_error("Runtime binding resolve failed for " + source + ": " + detail);
    }
    if (response.member_is("ok", true) && response.member_equals("res", "ok")) {
        const JsonValue* binding = response.find("binding");
        if (binding == nullptr || !binding->is_object())
            throw std::runtime_error("Runtime binding resolve returned a malformed binding for " + source);
        check_binding_identity(*binding);
        const std::string& contract = required->second.contract;
        for (const char* field : {"required_contract", "contract"}) {
            if (!binding->member_equals(field, contract)) {
                throw std::runtime_error("Runtime binding contract mismatch for " + source + ": expected " + contract
                                         + ", got " + reply_repr(binding->find(field)));
            }
        }
        if (!binding->member_equals("provided_contract", contract)) {
            throw std::runtime_error("Runtime binding provider contract mismatch for " + source + ": expected "
                                     + contract + ", got " + reply_repr(binding->find("provided_contract")));
        }
        for (const char* field : {"resolved_target_subsystem", "resolved_provider_interface"}) {
            const auto value = binding->string_member(field);
            if (!value || trimmed(*value).empty()) {
                throw std::runtime_error("Runtime binding is not callable: " + source
                                         + " has no resolved provider target");
            }
        }
        return bound_interface_from_runtime_binding(name_, subsystem_manifest_, requirement, *binding);
    }
    std::string detail = reply_text(response.find("reason"));
    if (detail.empty()) detail = reply_text(response.find("error"));
    if (detail.empty()) detail = reply_text(response.find("res"));
    if (detail.empty()) detail = "unknown response";
    throw std::runtime_error("Runtime binding resolve returned an invalid response for " + source + ": " + detail);
}

}  // namespace r2kernel::wrapper
