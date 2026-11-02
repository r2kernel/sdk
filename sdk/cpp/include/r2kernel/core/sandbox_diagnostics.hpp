#pragma once

// Explains socket() failures caused by a brokered package sandbox.
// The launcher sets R2KERNEL_SANDBOX_NETWORK=brokered (and
// R2KERNEL_SANDBOX_CAN=brokered when CAN is brokered) for packages whose
// seccomp filter denies direct AF_INET/AF_INET6/AF_PACKET (or AF_CAN) sockets
// with EPERM. The filter is inherited by every child process of the package.

#include <optional>
#include <string>

#include "r2kernel/export.hpp"

namespace r2kernel {

constexpr char kSandboxNetworkEnv[] = "R2KERNEL_SANDBOX_NETWORK";
constexpr char kSandboxCanEnv[] = "R2KERNEL_SANDBOX_CAN";

// For socket(family, ...) that failed with error_number: the explanation when
// the failure is the brokered sandbox denial (EPERM, a sandboxed family and the
// matching variable set to "brokered"), otherwise std::nullopt.
R2KERNEL_CPP_API std::optional<std::string> sandbox_socket_denial(int family, int error_number);

}  // namespace r2kernel
