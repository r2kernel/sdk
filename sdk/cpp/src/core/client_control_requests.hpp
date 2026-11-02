// Included at the end of client.cpp (same translation unit). Cold entry points
// of the subsystem-owned control requests implemented in control_requests.cpp.
#include "control_requests.hpp"

#if defined(__GNUC__)
#define R2KERNEL_SDK_COLD __attribute__((cold))
#else
#define R2KERNEL_SDK_COLD
#endif

namespace r2kernel::core {

R2KERNEL_SDK_COLD std::string detail::exchange_control_request(const std::string& request,
    std::optional<std::chrono::steady_clock::time_point> deadline) {
    // Without a deadline each socket operation is bounded by 1 s (one attempt),
    // as the Python SDK's self-binding requests.
    if (!deadline) return exchange_control_json(request, "", 0, 1000);
    if (std::chrono::steady_clock::now() >= *deadline)
        throw std::runtime_error("Control request deadline exceeded");
    return exchange_control_json(request, "", 0, std::numeric_limits<int>::max(), *deadline);
}

R2KERNEL_SDK_COLD std::string R2KernelClient::bind_requirement(const std::string& requirement,
    const std::string& target, std::optional<std::chrono::steady_clock::time_point> deadline) {
    return detail::request_self_binding_set(impl_->options, requirement, target, deadline);
}

R2KERNEL_SDK_COLD bool R2KernelClient::unbind_requirement(const std::string& requirement,
    std::optional<std::chrono::steady_clock::time_point> deadline) {
    return detail::request_self_binding_unset(impl_->options, requirement, deadline);
}

}  // namespace r2kernel::core

#undef R2KERNEL_SDK_COLD
