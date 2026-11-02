#pragma once

// The source bound to a Gateway's provided Control interface, as Control's
// runtime binding status reports it (Python SDK
// GatewaySubsystem.bound_control_authority dictionary).

#include <optional>
#include <string>

namespace r2kernel::wrapper {

struct BoundControlAuthority {
    // Source subsystem and its requirement reference "<source>.<requirement>".
    std::string source;
    std::string source_reference;
    // Federation peer of the source; absent for a local source. Values Control
    // reports as null are absent; a non-string value keeps its JSON text.
    std::optional<std::string> peer;
    std::optional<std::string> source_session;
    std::optional<std::string> target_session;
    // The Gateway's provided interface (Python key "interface").
    std::string interface_name;
    std::optional<std::string> source_registration;
    std::optional<std::string> target_registration;
    std::optional<std::string> binding_revision;
};

}  // namespace r2kernel::wrapper
