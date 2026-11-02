// Peer closure of a Control v5 lane (see the header).
#include "control_shm_peer_reset.hpp"

namespace r2kernel::core::detail {

bool control_shm_peer_closed(const std::exception& error, int socket) noexcept {
    (void)socket;
    if (dynamic_cast<const ControlShmPeerClosed*>(&error) != nullptr) return true;
    const auto* socket_error = dynamic_cast<const ControlShmSocketError*>(&error);
    return socket_error != nullptr && socket_error->peer_reset();
}

}  // namespace r2kernel::core::detail
