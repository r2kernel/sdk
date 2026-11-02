// Requirement self-binding of a Subsystem. Cold control-plane
// code kept in its own translation unit so it does not change the codegen of
// subsystem.cpp's per-call paths; it forwards to the native core client.
#include "r2kernel/wrapper/subsystem.hpp"
#include "r2kernel/core/client.hpp"

#include <chrono>
#include <stdexcept>
#include <string>

namespace r2kernel::wrapper {

namespace {
r2kernel::core::R2KernelClient& self_binding_client(const std::shared_ptr<CoreClient>& client, const char* caller) {
    if (!client)
        throw std::logic_error(std::string(caller) + " requires a registered subsystem: no core client is bound yet "
            "(call initialize() first)");
    auto* native = dynamic_cast<r2kernel::core::R2KernelClient*>(client.get());
    if (!native)
        throw std::logic_error(std::string(caller) + " requires the native r2kernel::core::R2KernelClient core client");
    return *native;
}
}  // namespace

std::string Subsystem::bind_requirement(const std::string& requirement, const std::string& target,
                                        std::optional<std::chrono::steady_clock::time_point> deadline) {
    return self_binding_client(core_client_, "bind_requirement").bind_requirement(requirement, target, deadline);
}

bool Subsystem::unbind_requirement(const std::string& requirement,
                                   std::optional<std::chrono::steady_clock::time_point> deadline) {
    return self_binding_client(core_client_, "unbind_requirement").unbind_requirement(requirement, deadline);
}

}  // namespace r2kernel::wrapper
