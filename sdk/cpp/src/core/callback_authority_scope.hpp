#pragma once
#include "r2kernel/wrapper/core_client.hpp"

namespace r2kernel::core::detail {
// Private propagation across an Operation executor thread. Public request
// structures retain their existing layout and authority never comes from data.
class CallbackAuthorityScope {
public:
    explicit CallbackAuthorityScope(std::optional<r2kernel::wrapper::ProtocolHostAuthority> host);
    ~CallbackAuthorityScope();
    CallbackAuthorityScope(const CallbackAuthorityScope&) = delete;
    CallbackAuthorityScope& operator=(const CallbackAuthorityScope&) = delete;
private:
    std::optional<r2kernel::wrapper::ControlSourceAuthority> previous_control_;
    std::optional<r2kernel::wrapper::ProtocolHostAuthority> previous_host_;
};
}
