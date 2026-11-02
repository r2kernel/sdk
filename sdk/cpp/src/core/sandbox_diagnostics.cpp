// Brokered sandbox socket-denial diagnostics. Cold; called only
// from an application's socket() error path.
#include "r2kernel/core/sandbox_diagnostics.hpp"

#include <cerrno>
#include <cstdlib>
#include <cstring>

#if defined(__linux__)
#include <sys/socket.h>
#if !defined(AF_CAN)
#define AF_CAN 29
#endif
#endif

namespace r2kernel {
namespace {
bool brokered(const char* variable) {
    const char* value = std::getenv(variable);
    return value != nullptr && std::strcmp(value, "brokered") == 0;
}
constexpr char kAlternatives[] =
    "; request a brokered socket from r2kernel-resourced or use a socket the package leader passed "
    "through R2KERNEL_INHERITED_FDS (r2kernel::inherited_socket)";
}  // namespace

std::optional<std::string> sandbox_socket_denial(int family, int error_number) {
#if defined(__linux__)
    if (error_number != EPERM) return std::nullopt;
    const char* name = family == AF_INET ? "AF_INET" : family == AF_INET6 ? "AF_INET6"
        : family == AF_PACKET ? "AF_PACKET" : family == AF_CAN ? "AF_CAN" : nullptr;
    if (name == nullptr) return std::nullopt;
    if (family == AF_CAN) {
        if (!brokered(kSandboxCanEnv)) return std::nullopt;
        return std::string("socket(AF_CAN) denied by the brokered CAN sandbox (R2KERNEL_SANDBOX_CAN=brokered)")
            + kAlternatives;
    }
    if (!brokered(kSandboxNetworkEnv)) return std::nullopt;
    return std::string("socket(") + name + ") denied by resources.sandbox.network=brokered" + kAlternatives;
#else
    (void)family;
    (void)error_number;
    return std::nullopt;
#endif
}

}  // namespace r2kernel
