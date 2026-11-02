#pragma once

// Control registration upkeep of R2KernelClient (re-registration after Control
// lost the registration). Cold control plane, implemented in
// control_registration.cpp so it stays out of client.cpp's per-call codegen.

#include "r2kernel/core/client.hpp"

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>

namespace r2kernel::core::detail {

class ControlRegistrationKeeper;

// register_subsystem request: this client's subsystem, session, PID, resource
// lease (R2KERNEL_RESOURCE_LEASE_ID) and notify endpoint.
std::string registration_request(const ClientOptions& options,
    const std::string& notify_endpoint);

// Device and inode of a POSIX shared-memory object, nullopt where it has no
// /dev/shm path.
std::optional<std::pair<std::uint64_t, std::uint64_t>> shm_identity(
    const std::string& name);

// One heartbeat through the client's keeper (created on first use). Returns
// whether the registration is still lost.
bool keep_registration(std::shared_ptr<ControlRegistrationKeeper>& keeper,
    const R2KernelClient::RegistrationStep& send_status, const R2KernelClient::RegistrationStep& register_again,
    const R2KernelClient::RegistrationLog& log);

// Whether the keeper (nullptr before the first heartbeat) has a lost registration.
bool registration_lost(const ControlRegistrationKeeper* keeper) noexcept;

}  // namespace r2kernel::core::detail
