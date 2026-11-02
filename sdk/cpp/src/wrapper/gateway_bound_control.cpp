// Sources bound to a Gateway's provided Control interfaces (Python SDK
// wrapper/subsystem.py GatewaySubsystem). Cold control-plane code in its own
// translation unit; every check fails closed.
#include "r2kernel/wrapper/subsystem.hpp"

#include "control_query.hpp"
#include "../core/json_text.hpp"

#include <stdexcept>
#include <string>
#include <utility>

namespace r2kernel::wrapper {
namespace {

using detail::reply_repr;
using detail::trimmed;
using r2kernel::sdk_detail::JsonValue;

struct ControlTarget {
    std::string interface_name;
    std::string target;
};

ControlTarget declared_control_target(const Subsystem& gateway, const std::string& provider_interface) {
    const std::string interface_name = trimmed(provider_interface);
    if (interface_name.empty()) throw std::runtime_error("Gateway Control provider interface must be a non-empty string");
    if (gateway.subsystem_manifest().provides.count(interface_name) == 0) {
        throw std::runtime_error("Gateway Control provider interface is not declared: " + gateway.name() + "."
                                 + interface_name);
    }
    return {interface_name, gateway.name() + "." + interface_name};
}

std::string binding_status_request(const std::string& subsystem) {
    return "{\"cmd\":\"binding_status\",\"subsystem\":\"" + r2kernel::sdk_detail::json_escape(subsystem) + "\"}";
}

bool nonblank_string(const JsonValue* value) {
    return value != nullptr && value->is_string() && !trimmed(value->text()).empty();
}

std::optional<std::string> optional_text(const JsonValue* value) {
    if (value == nullptr || value->is_null()) return std::nullopt;
    return value->is_string() ? value->text() : value->dump();
}

}  // namespace

std::map<std::string, std::string> GatewaySubsystem::bound_control_sessions(const std::string& provider_interface) const {
    const auto [interface_name, target] = declared_control_target(*this, provider_interface);
    std::optional<JsonValue> reply;
    try {
        reply = detail::control_query(binding_status_request(name()));
    } catch (const std::exception& error) {
        throw std::runtime_error("Runtime binding status is unavailable for gateway Control target " + target + ": "
                                 + error.what());
    }
    if (!reply || !reply->is_object() || !reply->member_is("ok", true) || !reply->member_equals("res", "ok"))
        throw std::runtime_error("Runtime binding status is unsuccessful or malformed for gateway Control target " + target);
    const JsonValue* bindings = reply->find("bindings");
    if (bindings == nullptr || !bindings->is_array()) {
        throw std::runtime_error("Runtime binding status has no structured bindings list for gateway Control target "
                                 + target);
    }
    std::map<std::string, std::string> sources;
    const std::string where = "Runtime binding for gateway Control interface " + target;
    for (std::size_t index = 0; index < bindings->items().size(); ++index) {
        const JsonValue& binding = bindings->items()[index];
        if (!binding.is_object())
            throw std::runtime_error("Runtime binding status contains a malformed binding at index " + std::to_string(index));
        const auto binding_target = binding.string_member("target");
        if (!binding_target) {
            throw std::runtime_error("Runtime binding status contains a binding without a valid target at index "
                                     + std::to_string(index));
        }
        if (trimmed(*binding_target).empty()) {
            if (binding.member_equals("state", "unbound") && binding.member_is("explicit_unbound", true)) continue;
            throw std::runtime_error("Runtime binding status contains a binding without a valid target at index "
                                     + std::to_string(index));
        }
        const bool metadata_matches = binding.member_equals("target_subsystem", name())
            && binding.member_equals("provider_interface", interface_name);
        if (*binding_target != target && !metadata_matches) continue;
        if (*binding_target != target) throw std::runtime_error(where + " has inconsistent target metadata");
        const JsonValue* target_subsystem = binding.find("target_subsystem");
        if (target_subsystem != nullptr && !target_subsystem->is_null() && !binding.member_equals("target_subsystem", name()))
            throw std::runtime_error(where + " has inconsistent target subsystem");
        if (!binding.member_equals("provider_interface", interface_name))
            throw std::runtime_error(where + " has inconsistent provider interface");
        if (!binding.member_equals("state", "bound"))
            throw std::runtime_error(where + " is not bound (state=" + reply_repr(binding.find("state")) + ")");
        if (!nonblank_string(binding.find("source_subsystem")))
            throw std::runtime_error(where + " has no valid source subsystem");
        if (!nonblank_string(binding.find("source")))
            throw std::runtime_error(where + " has no valid source reference");
        BindingReference source;
        try {
            source = BindingReference::parse(*binding.string_member("source"), "runtime binding source for " + target);
        } catch (const ManifestError&) {
            throw std::runtime_error(where + " has an invalid source reference");
        }
        const std::string source_subsystem = trimmed(*binding.string_member("source_subsystem"));
        if (source.subsystem != source_subsystem) throw std::runtime_error(where + " has inconsistent source metadata");
        const JsonValue* requirement = binding.find("requirement");
        if (requirement != nullptr && !requirement->is_null() && !binding.member_equals("requirement", source.interface_name))
            throw std::runtime_error(where + " has inconsistent source requirement");
        if (!sources.emplace(source.format(), source_subsystem).second) {
            throw std::runtime_error("Ambiguous runtime bindings for gateway Control interface " + target
                                     + ": duplicate source reference " + source.format());
        }
    }
    return sources;
}

BoundControlAuthority GatewaySubsystem::bound_control_authority(const std::string& provider_interface) const {
    const auto [interface_name, target] = declared_control_target(*this, provider_interface);
    const auto reply = detail::control_query(binding_status_request(name()));
    if (!reply || !reply->is_object() || !reply->member_is("ok", true) || !reply->member_equals("res", "ok"))
        throw std::runtime_error("Runtime binding authority is unavailable for " + target);
    const JsonValue* bindings = reply->find("bindings");
    if (bindings == nullptr || !bindings->is_array())
        throw std::runtime_error("Runtime binding authority has no structured binding list for " + target);
    const JsonValue* owner = nullptr;
    std::size_t owners = 0;
    for (const auto& binding : bindings->items()) {
        if (binding.is_object() && binding.member_equals("target", target)) {
            owner = &binding;
            ++owners;
        }
    }
    if (owners != 1 || !owner->member_equals("state", "bound"))
        throw std::runtime_error("Gateway Control authority is missing or ambiguous for " + target);
    const auto source = owner->string_member("source_subsystem");
    const auto reference = owner->string_member("source");
    if (!source || !reference || BindingReference::parse(*reference, "binding authority source").subsystem != *source)
        throw std::runtime_error("Gateway Control authority has inconsistent source metadata for " + target);
    BoundControlAuthority authority;
    authority.source = *source;
    authority.source_reference = *reference;
    authority.peer = optional_text(owner->find("source_peer_id"));
    authority.source_session = optional_text(owner->find("source_session_id"));
    authority.target_session = optional_text(owner->find("target_session_id"));
    authority.interface_name = interface_name;
    authority.source_registration = optional_text(owner->find("source_registration_revision"));
    authority.target_registration = optional_text(owner->find("target_registration_revision"));
    authority.binding_revision = optional_text(owner->find("binding_revision"));
    return authority;
}

BoundControlAuthority GatewaySubsystem::validate_control_authority(
    const std::string& provider_interface, const std::string& source_subsystem,
    const std::optional<ControlSourceAuthority>& source_authority) const {
    BoundControlAuthority authority = bound_control_authority(provider_interface);
    if (source_subsystem != authority.source) {
        throw std::invalid_argument(provider_interface + " source is bound to " + authority.source
                                    + "; received Control request from " + source_subsystem);
    }
    if (authority.peer) {
        const auto same = [](const std::optional<std::string>& expected, const std::string& actual) {
            return expected && *expected == actual;
        };
        const bool matches = source_authority && source_authority->source == authority.source
            && source_authority->source_reference == authority.source_reference
            && same(authority.peer, source_authority->peer)
            && same(authority.source_session, source_authority->source_session)
            && same(authority.target_session, source_authority->target_session)
            && source_authority->interface == authority.interface_name
            && same(authority.source_registration, source_authority->source_registration)
            && same(authority.target_registration, source_authority->target_registration)
            && same(authority.binding_revision, source_authority->binding_revision);
        if (!matches)
            throw std::runtime_error("Authenticated Control registration or binding epoch does not match the bound owner");
    }
    return authority;
}

BoundControlAuthority GatewaySubsystem::validate_control_authority(const std::string& provider_interface,
                                                                   const ControlEndpointRequest& request) const {
    return validate_control_authority(provider_interface, request.sender, current_control_source_authority());
}

std::string GatewaySubsystem::bound_control_owner(const std::string& provider_interface) const {
    const std::string target = declared_control_target(*this, provider_interface).target;
    const auto sources = bound_control_sessions(provider_interface);
    if (sources.empty()) throw std::runtime_error("No active runtime binding targets gateway Control interface " + target);
    if (sources.size() != 1) {
        throw std::runtime_error("Ambiguous runtime bindings target gateway Control interface " + target + ": found "
                                 + std::to_string(sources.size()) + " source references");
    }
    return sources.begin()->second;
}

std::string GatewaySubsystem::bound_control_session(const std::string& provider_interface,
                                                    const std::string& source_subsystem) const {
    const std::string target = declared_control_target(*this, provider_interface).target;
    const std::string source = trimmed(source_subsystem);
    if (source.empty()) throw std::runtime_error("Gateway Control source subsystem must be a non-empty string");
    std::vector<std::string> references;
    for (const auto& [reference, bound_subsystem] : bound_control_sessions(provider_interface)) {
        if (bound_subsystem == source) references.push_back(reference);
    }
    if (references.empty()) {
        throw std::runtime_error("No runtime binding from source subsystem " + source
                                 + " targets gateway Control interface " + target);
    }
    if (references.size() != 1) {
        throw std::runtime_error("Ambiguous runtime bindings from source subsystem " + source
                                 + " target gateway Control interface " + target + ": found "
                                 + std::to_string(references.size()) + " source references");
    }
    return references.front();
}

}  // namespace r2kernel::wrapper
