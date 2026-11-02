#include "../src/core/callback_authority_scope.hpp"
#include <thread>

int main() {
    using r2kernel::wrapper::current_protocol_host_authority;
    using r2kernel::wrapper::current_control_source_authority;
    using r2kernel::wrapper::ProtocolHostAuthority;
    if (current_protocol_host_authority() || current_control_source_authority()) return 1;
    {
        r2kernel::core::detail::CallbackAuthorityScope outer(ProtocolHostAuthority{"protocol", "peer-a", "epoch-a"});
        if (current_protocol_host_authority()->peer != "peer-a") return 2;
        bool propagated = false;
        const auto captured = current_protocol_host_authority();
        std::thread operation([&propagated, captured] {
            if (current_protocol_host_authority()) return;
            {
                r2kernel::core::detail::CallbackAuthorityScope scope(captured);
                propagated = current_protocol_host_authority()->kind == "protocol" &&
                    current_protocol_host_authority()->peer == "peer-a" &&
                    current_protocol_host_authority()->epoch == "epoch-a" && !current_control_source_authority();
            }
            if (current_protocol_host_authority()) propagated = false;
        });
        operation.join();
        if (!propagated || current_protocol_host_authority()->peer != "peer-a") return 3;
    }
    return current_protocol_host_authority() ? 4 : 0;
}
