// Cold Control registration upkeep of R2KernelClient: the registration
// request, the Event pool identity and the bounded re-registration keeper. A
// separate translation unit so this code stays out of client.cpp's per-call
// codegen.
#include "control_registration.hpp"
#include "control_registration_keeper.hpp"
#include "json_text.hpp"

#include <cstdlib>

#if !defined(_WIN32)
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace r2kernel::core::detail {

std::string registration_request(const ClientOptions& options, const std::string& notify_endpoint) {
    using r2kernel::sdk_detail::json_escape;
    const char* lease = std::getenv("R2KERNEL_RESOURCE_LEASE_ID");
#if defined(_WIN32)
    const int pid = 0;
#else
    const int pid = static_cast<int>(::getpid());
#endif
    return "{\"cmd\":\"register_subsystem\","
           "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
           "\"session_id\":\"" + json_escape(options.session_id) + "\","
           "\"pid\":" + std::to_string(pid) + ","
           "\"lease_id\":\"" + json_escape(lease != nullptr ? lease : "") + "\","
           "\"notify_endpoint\":\"" + json_escape(notify_endpoint) + "\"}";
}

std::optional<std::pair<std::uint64_t, std::uint64_t>> shm_identity(const std::string& name) {
#if defined(_WIN32)
    (void)name;
    return std::nullopt;
#else
    struct stat status {};
    const std::string path = "/dev/shm/" + (name.rfind('/', 0) == 0 ? name.substr(1) : name);
    if (::stat(path.c_str(), &status) != 0) return std::nullopt;
    return std::make_pair(static_cast<std::uint64_t>(status.st_dev), static_cast<std::uint64_t>(status.st_ino));
#endif
}

bool keep_registration(std::shared_ptr<ControlRegistrationKeeper>& keeper,
    const R2KernelClient::RegistrationStep& send_status, const R2KernelClient::RegistrationStep& register_again,
    const R2KernelClient::RegistrationLog& log) {
    if (!keeper) keeper = std::make_shared<ControlRegistrationKeeper>();
    keeper->heartbeat(send_status, register_again, log);
    return keeper->registration_lost();
}

bool registration_lost(const ControlRegistrationKeeper* keeper) noexcept {
    return keeper != nullptr && keeper->registration_lost();
}

}  // namespace r2kernel::core::detail
