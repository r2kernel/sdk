#pragma once

// Brokered file descriptors issued by r2kernel-resourced to a managed package
// (same contract as the Python SDK's r2kernel.core.ResourceBrokerClient). Each
// request sends one JSON line over the broker's Unix socket and receives one
// JSON line carrying exactly one descriptor (SCM_RIGHTS). A request is never
// replayed: a failed receive cannot establish whether the broker already
// performed it. Linux only; elsewhere every request throws std::runtime_error.

#include <optional>
#include <string>

#include "r2kernel/core/inherited_fds.hpp"
#include "r2kernel/export.hpp"

namespace r2kernel::core {

// Environment of a managed package (set by r2kernel launch run / deploy up).
constexpr char kResourceLeaseIdEnv[] = "R2KERNEL_RESOURCE_LEASE_ID";
constexpr char kResourceServiceSocketEnv[] = "R2KERNEL_RESOURCE_SERVICE_SOCKET";
constexpr char kDefaultResourceSocket[] = "/run/r2kernel/resourced.sock";

// A brokered socket. The descriptor is close-on-exec; ownership passes to the
// caller (close() or ::close). metadata_json is the broker's result object as
// compact JSON text.
struct R2KERNEL_CPP_API BrokeredSocketResult {
    int socket = -1;
    std::string metadata_json;
    // Closes socket when it is open and sets it to -1. Frontend Unix listeners
    // are shut down first so the broker can stop their TCP relay.
    void close() noexcept;
};

// A brokered device descriptor; same ownership rules as BrokeredSocketResult.
struct R2KERNEL_CPP_API BrokeredFdResult {
    int fd = -1;
    std::string metadata_json;
    void close() noexcept;
};

// Python keyword arguments of ResourceBrokerClient.open_udp_socket
// (interface_name is Python's interface).
struct UdpSocketOptions {
    std::string interface_name;
    std::string bind_host = "0.0.0.0";
    int bind_port = 0;
};

struct TcpSocketOptions {
    std::string interface_name;
    std::string bind_host = "0.0.0.0";
    int bind_port = 0;
    // Sent as JSON null when unset.
    std::optional<std::string> connect_host;
    std::optional<int> connect_port;
    bool listen = false;
    int backlog = 128;
};

struct FrontendTcpListenerOptions {
    // "api" or "ws" (validated by the broker).
    std::string kind = "api";
    // Unset: sent as "" and 0, the package frontend grant decides.
    std::optional<std::string> bind_host;
    std::optional<int> bind_port;
    int backlog = 128;
};

struct CanSocketOptions {
    std::string interface_name;
    bool receive_own_messages = false;
    bool can_fd_frames = false;
    bool nonblocking = true;
};

struct DeviceOpenOptions {
    bool read = true;
    bool write = true;
    bool nonblocking = true;
};

class R2KERNEL_CPP_API ResourceBrokerClient {
public:
    // An empty lease_id reads R2KERNEL_RESOURCE_LEASE_ID; an empty socket_path
    // reads R2KERNEL_RESOURCE_SERVICE_SOCKET, else kDefaultResourceSocket.
    // Throws std::runtime_error when no lease is available.
    explicit ResourceBrokerClient(std::string lease_id = {}, std::string socket_path = {});

    const std::string& lease_id() const noexcept { return lease_id_; }
    const std::string& socket_path() const noexcept { return socket_path_; }

    // Each request has an absolute 5 s budget (connect, send, every receive).
    // Broker refusals, malformed or oversized (> 1 MiB) responses, invalid or
    // truncated ancillary data and a missing descriptor throw
    // std::runtime_error; every descriptor received with a rejected response
    // is closed.
    BrokeredSocketResult open_udp_socket(const UdpSocketOptions& options) const;
    // Connected, revocable AF_UNIX stream to one root-approved endpoint.
    // Validates the broker execution_id and never exposes an Internet FD.
    BrokeredSocketResult open_approved_tcp_stream(const std::string& grant_id) const;
    BrokeredSocketResult open_tcp_socket(const TcpSocketOptions& options) const;
    // Returns an AF_UNIX stream listener for a broker-owned loopback TCP relay.
    // metadata_json keeps bind_host/bind_port of that TCP endpoint. Validate the
    // Unix descriptor and transport metadata before use; close() stops the relay.
    BrokeredSocketResult open_frontend_tcp_listener(const FrontendTcpListenerOptions& options = {}) const;
    BrokeredSocketResult open_can_socket(const CanSocketOptions& options) const;
    BrokeredFdResult open_device(const std::string& path, const DeviceOpenOptions& options = {}) const;

    // Validates a socket inherited from the package leader
    // (R2KERNEL_INHERITED_FDS); no lease or broker request is involved. Same as
    // r2kernel::inherited_socket.
    static int inherited_fd(const std::string& kind, const InheritedSocketExpectation& expect);

private:
    std::string lease_id_;
    std::string socket_path_;
};

}  // namespace r2kernel::core
