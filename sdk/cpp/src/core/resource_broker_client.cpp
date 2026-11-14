// Brokered resource descriptors (r2kernel-resourced). Cold setup-time code in
// its own translation unit; the wire contract and every check follow the
// Python SDK's r2kernel/core/resource.py.
#include "r2kernel/core/resource_broker_client.hpp"

#include "r2kernel/core/client.hpp"
#include "json_text.hpp"
#include "json_value.hpp"

#include <chrono>
#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#if defined(__linux__)
#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

namespace r2kernel::core {
namespace {

using r2kernel::sdk_detail::JsonValue;
using r2kernel::sdk_detail::json_escape;

constexpr std::size_t kMaxResourceResponseBytes = 1024 * 1024;
constexpr std::size_t kReceiveChunkBytes = 8192;
constexpr int kAncillaryDescriptorSlots = 64;
constexpr auto kResourceRequestTimeout = std::chrono::seconds(5);
// open_uplink_tcp: the broker may spend the whole connect timeout resolving and
// connecting, so its reply is awaited for that timeout plus this margin.
constexpr auto kUplinkReplyMargin = std::chrono::seconds(2);
// Protocol bounds of an uplink request: the timeout is at most the largest
// max_connect_timeout_ms a host accepts, and the host is at most 253 bytes.
constexpr auto kUplinkMaxTimeout = std::chrono::milliseconds(120000);
constexpr std::size_t kUplinkMaxHostBytes = 253;

std::string env_or_empty(const char* name) {
    const char* raw = std::getenv(name);
    return raw == nullptr ? std::string {} : std::string(raw);
}

std::string quoted(const std::string& value) { return "\"" + json_escape(value) + "\""; }
const char* json_bool(bool value) { return value ? "true" : "false"; }

#if defined(__linux__)
using Clock = std::chrono::steady_clock;

class ResourceRequest {
public:
    // budget is the request's absolute deadline from now (connect, send, every receive).
    ResourceRequest(const std::string& socket_path, std::string line, std::chrono::milliseconds budget)
        : socket_path_(socket_path), line_(std::move(line)), deadline_(Clock::now() + budget) {}

    ~ResourceRequest() {
        for (const int fd : fds_) ::close(fd);
        if (socket_ >= 0) ::close(socket_);
    }
    ResourceRequest(const ResourceRequest&) = delete;
    ResourceRequest& operator=(const ResourceRequest&) = delete;

    // One submission; returns the broker's result object and the descriptor.
    std::pair<JsonValue, int> run() {
        connect_broker();
        send_line();
        std::string response;
        for (;;) {
            wait_for(POLLIN);
            std::vector<char> data(std::min(kReceiveChunkBytes, kMaxResourceResponseBytes - response.size() + 1));
            const ssize_t count = receive(data);
            remaining();
            if (count == 0) throw std::runtime_error("R2Kernel resource response ended before a complete line");
            response.append(data.data(), static_cast<std::size_t>(count));
            if (response.size() > kMaxResourceResponseBytes)
                throw std::runtime_error("R2Kernel resource response exceeds 1 MiB");
            const std::string_view chunk(data.data(), static_cast<std::size_t>(count));
            const auto newline = chunk.find('\n');
            if (newline == std::string_view::npos) continue;
            if (newline != chunk.size() - 1)
                throw std::runtime_error("R2Kernel resource connection permits exactly one response");
            response.pop_back();
            return finish(response);
        }
    }

private:
    std::chrono::nanoseconds remaining() const {
        const auto left = deadline_ - Clock::now();
        if (left <= Clock::duration::zero()) throw std::runtime_error("R2Kernel resource request deadline exceeded");
        return std::chrono::duration_cast<std::chrono::nanoseconds>(left);
    }

    void wait_for(short events) {
        for (;;) {
            const auto left = remaining().count();
            timespec timeout {static_cast<time_t>(left / 1000000000), static_cast<long>(left % 1000000000)};
            pollfd descriptor {socket_, events, 0};
            const int result = ::ppoll(&descriptor, 1, &timeout, nullptr);
            if (result < 0 && errno == EINTR) continue;
            if (result < 0) throw std::runtime_error(std::string("R2Kernel resource socket poll failed: ") + std::strerror(errno));
            if (result > 0) return;
        }
    }

    void connect_broker() {
        remaining();
        sockaddr_un address {};
        if (socket_path_.empty() || socket_path_.size() >= sizeof(address.sun_path))
            throw std::runtime_error("R2Kernel resource socket path is empty or too long: " + socket_path_);
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, socket_path_.data(), socket_path_.size());
        socket_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
        if (socket_ < 0) throw std::runtime_error(std::string("R2Kernel resource socket: ") + std::strerror(errno));
        if (::connect(socket_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0) return;
        if (errno != EINPROGRESS && errno != EAGAIN)
            throw std::runtime_error("Cannot connect to the R2Kernel resource broker at " + socket_path_ + ": "
                                     + std::strerror(errno));
        wait_for(POLLOUT);
        int error = 0;
        socklen_t length = sizeof(error);
        if (::getsockopt(socket_, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0)
            throw std::runtime_error("Cannot connect to the R2Kernel resource broker at " + socket_path_ + ": "
                                     + std::strerror(error != 0 ? error : errno));
    }

    void send_line() {
        std::size_t sent = 0;
        while (sent < line_.size()) {
            remaining();
            const ssize_t count = ::send(socket_, line_.data() + sent, line_.size() - sent, MSG_NOSIGNAL);
            if (count < 0 && errno == EINTR) continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                wait_for(POLLOUT);
                continue;
            }
            if (count <= 0)
                throw std::runtime_error(std::string("R2Kernel resource request send failed: ") + std::strerror(errno));
            sent += static_cast<std::size_t>(count);
        }
    }

    // Adopts every delivered right before checking the frame, the deadline,
    // the truncation flags or the response status.
    ssize_t receive(std::vector<char>& data) {
        alignas(cmsghdr) char control[CMSG_SPACE(kAncillaryDescriptorSlots * sizeof(int))];
        iovec vector {data.data(), data.size()};
        msghdr message {};
        message.msg_iov = &vector;
        message.msg_iovlen = 1;
        message.msg_control = control;
        message.msg_controllen = sizeof(control);
        ssize_t count = -1;
        do {
            count = ::recvmsg(socket_, &message, MSG_CMSG_CLOEXEC);
        } while (count < 0 && errno == EINTR);
        if (count < 0)
            throw std::runtime_error(std::string("R2Kernel resource response receive failed: ") + std::strerror(errno));
        bool invalid_ancillary = false;
        for (cmsghdr* header = CMSG_FIRSTHDR(&message); header != nullptr; header = CMSG_NXTHDR(&message, header)) {
            if (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS) {
                invalid_ancillary = true;
                continue;
            }
            const std::size_t length = header->cmsg_len - CMSG_LEN(0);
            const std::size_t usable = length - length % sizeof(int);
            for (std::size_t offset = 0; offset < usable; offset += sizeof(int)) {
                int fd = -1;
                std::memcpy(&fd, CMSG_DATA(header) + offset, sizeof(int));
                fds_.push_back(fd);
            }
            invalid_ancillary = invalid_ancillary || length == 0 || usable != length;
        }
        if (invalid_ancillary || fds_.size() > 1 || (message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)) != 0)
            throw std::runtime_error("Invalid R2Kernel resource response ancillary descriptors");
        for (const int fd : fds_) (void)::fcntl(fd, F_SETFD, FD_CLOEXEC);
        return count;
    }

    std::pair<JsonValue, int> finish(const std::string& text) {
        const JsonValue response = r2kernel::sdk_detail::parse_json(text);
        const JsonValue* ok = response.find("ok");
        if (!response.is_object() || ok == nullptr || !ok->is_bool())
            throw std::runtime_error("Malformed R2Kernel resource response");
        if (!ok->as_bool()) {
            const JsonValue* error = response.find("error");
            std::string code = response.string_member("error_code").value_or(std::string {});
            if (error != nullptr && error->truthy())
                throw ResourceBrokerError(std::move(code), error->is_string() ? error->text() : error->dump());
            throw ResourceBrokerError(std::move(code), "R2Kernel resource request failed");
        }
        const JsonValue* metadata = response.find("result");
        if (metadata == nullptr || !metadata->is_object())
            throw std::runtime_error("Malformed R2Kernel resource response result");
        if (fds_.size() != 1) throw std::runtime_error("R2Kernel resource response did not include a file descriptor");
        remaining();
        const int fd = fds_.back();
        fds_.pop_back();
        return {*metadata, fd};
    }

    const std::string& socket_path_;
    const std::string line_;
    const Clock::time_point deadline_;
    int socket_ = -1;
    std::vector<int> fds_;
};
#endif

std::pair<std::string, int> request_fd(const std::string& socket_path, const std::string& request_json,
                                       std::chrono::milliseconds budget = kResourceRequestTimeout) {
#if defined(__linux__)
    ResourceRequest request(socket_path, request_json + "\n", budget);
    auto [metadata, fd] = request.run();
    return {metadata.dump(), fd};
#else
    (void)socket_path;
    (void)request_json;
    (void)budget;
    throw std::runtime_error("R2Kernel resource broker requests require Linux Unix descriptor passing");
#endif
}

BrokeredSocketResult socket_result(std::pair<std::string, int> brokered) {
    BrokeredSocketResult result;
    result.socket = brokered.second;
    result.metadata_json = std::move(brokered.first);
    return result;
}

BrokeredSocketResult frontend_socket_result(std::pair<std::string, int> brokered) {
    try {
        const auto metadata = r2kernel::sdk_detail::parse_json(brokered.first);
        if (!metadata.member_equals("domain", "unix") || !metadata.member_equals("transport", "unix_listener"))
            throw std::runtime_error("R2Kernel frontend response must describe a Unix listener relay");
        const auto* port = metadata.find("bind_port");
        int port_number = 0;
        if (port != nullptr && port->is_number() && !port->text().empty() && port->text().size() <= 5
                && port->text().find_first_not_of("0123456789") == std::string::npos) {
            for (char digit : port->text()) port_number = port_number * 10 + digit - '0';
        }
        if ((!metadata.member_equals("bind_host", "127.0.0.1") && !metadata.member_equals("bind_host", "::1"))
                || port_number < 1 || port_number > 65535)
            throw std::runtime_error("R2Kernel frontend response has an invalid loopback TCP endpoint");
#if defined(__linux__)
        const auto option = [&](int name, int expected) {
            int actual = 0;
            socklen_t length = sizeof(actual);
            return ::getsockopt(brokered.second, SOL_SOCKET, name, &actual, &length) == 0
                && length == sizeof(actual) && actual == expected;
        };
        if (!option(SO_DOMAIN, AF_UNIX) || !option(SO_TYPE, SOCK_STREAM) || !option(SO_ACCEPTCONN, 1))
            throw std::runtime_error("R2Kernel frontend descriptor must be a listening Unix stream socket");
#endif
        return socket_result(std::move(brokered));
    } catch (...) {
#if defined(__linux__)
        ::close(brokered.second);
#endif
        throw;
    }
}

void close_descriptor(int& fd) noexcept {
#if defined(__linux__)
    if (fd >= 0) ::close(fd);
#endif
    fd = -1;
}

#if defined(__linux__)
std::optional<in6_addr> ipv6_address(const std::string& text) {
    in6_addr address {};
    if (text.find('\0') != std::string::npos || ::inet_pton(AF_INET6, text.c_str(), &address) != 1)
        return std::nullopt;
    return address;
}
#endif

// The host as the broker normalizes it: lowercase, without one trailing dot.
std::string normalized_uplink_host(const std::string& host) {
    if (host.empty() || host.size() > kUplinkMaxHostBytes
            || std::any_of(host.begin(), host.end(), [](unsigned char byte) { return byte >= 0x80; }))
        throw std::invalid_argument("UplinkTcpOptions::host must be an ASCII DNS name or IP literal of 1 to 253 bytes");
    std::string name = host.back() == '.' ? host.substr(0, host.size() - 1) : host;
    for (char& byte : name) {
        if (byte >= 'A' && byte <= 'Z') byte = static_cast<char>(byte - 'A' + 'a');
    }
    if (name.find(':') != std::string::npos) {
        bool literal = name.find_first_not_of("0123456789abcdef:.") == std::string::npos;
#if defined(__linux__)
        literal = literal && ipv6_address(name).has_value();
#endif
        if (!literal)
            throw std::invalid_argument("UplinkTcpOptions::host " + host
                                        + " is not an IPv6 literal (write it without brackets or a zone)");
    } else if (name.empty() || name.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-.") != std::string::npos
               || name.front() == '.' || name.back() == '.' || name.find("..") != std::string::npos) {
        throw std::invalid_argument("UplinkTcpOptions::host " + host + " is not a DNS name or an IPv4 literal");
    }
    return name;
}

// The reply names the requested host: the same text, or the same address for
// an IPv6 literal written differently.
bool same_uplink_host(const std::string& reported, const std::string& requested) {
    if (reported == requested) return true;
#if defined(__linux__)
    const auto left = ipv6_address(reported);
    const auto right = ipv6_address(requested);
    return left && right && std::memcmp(left->s6_addr, right->s6_addr, sizeof(left->s6_addr)) == 0;
#else
    return false;
#endif
}

// Names the first failed check of an uplink reply; empty when it is valid.
std::string uplink_problem(int fd, const JsonValue& metadata, const std::string& lease_id, const std::string& host,
                           std::uint16_t port) {
    const auto connection_id = metadata.string_member("connection_id");
    const auto reported_host = metadata.string_member("host");
    const JsonValue* reported_port = metadata.find("port");
    if (!metadata.member_equals("kind", "uplink_tcp")) return "metadata kind is not uplink_tcp";
    if (!metadata.member_equals("lease_id", lease_id)) return "metadata lease_id is not this client's lease";
    if (!connection_id || connection_id->size() != 32
            || connection_id->find_first_not_of("0123456789abcdef") != std::string::npos)
        return "metadata connection_id is not 32 lowercase hexadecimal digits";
    if (reported_port == nullptr || !reported_port->is_number() || reported_port->text() != std::to_string(port))
        return "metadata port does not match the request";
    if (!reported_host || !same_uplink_host(*reported_host, host)) return "metadata host does not match the request";
#if defined(__linux__)
    const auto option = [&](int name, int expected) {
        int actual = 0;
        socklen_t length = sizeof(actual);
        return ::getsockopt(fd, SOL_SOCKET, name, &actual, &length) == 0 && length == sizeof(actual)
            && actual == expected;
    };
    if (!option(SO_DOMAIN, AF_UNIX) || !option(SO_TYPE, SOCK_STREAM) || !option(SO_ACCEPTCONN, 0))
        return "the descriptor is not a nonlistening AF_UNIX stream socket";
    sockaddr_storage peer {};
    socklen_t peer_length = sizeof(peer);
    if (::getpeername(fd, reinterpret_cast<sockaddr*>(&peer), &peer_length) != 0
            || peer_length < sizeof(peer.ss_family) || peer.ss_family != AF_UNIX)
        return "the descriptor is not connected";
#else
    (void)fd;
#endif
    return {};
}

}  // namespace

ResourceBrokerError::ResourceBrokerError(std::string code, const std::string& message)
    : std::runtime_error(message), code_(std::move(code)) {}

ResourceBrokerError::~ResourceBrokerError() = default;

void BrokeredSocketResult::close() noexcept {
#if defined(__linux__)
    if (socket >= 0) {
        try {
            if (r2kernel::sdk_detail::parse_json(metadata_json).member_equals("transport", "unix_listener"))
                (void)::shutdown(socket, SHUT_RDWR);
        } catch (...) {
            // Public metadata text may have been changed by the caller; still release the FD.
        }
    }
#endif
    close_descriptor(socket);
}
void BrokeredFdResult::close() noexcept { close_descriptor(fd); }

ResourceBrokerClient::ResourceBrokerClient(std::string lease_id, std::string socket_path)
    : lease_id_(lease_id.empty() ? env_or_empty(kResourceLeaseIdEnv) : std::move(lease_id)),
      socket_path_(socket_path.empty() ? env_or_empty(kResourceServiceSocketEnv) : std::move(socket_path)) {
    if (socket_path_.empty()) socket_path_ = kDefaultResourceSocket;
    if (lease_id_.empty())
        throw std::runtime_error(
            "R2KERNEL_RESOURCE_LEASE_ID is not set; resource broker access requires r2kernel-launch");
}

BrokeredSocketResult ResourceBrokerClient::open_udp_socket(const UdpSocketOptions& options) const {
    return socket_result(request_fd(socket_path_,
        "{\"op\":\"open_udp_socket\",\"lease_id\":" + quoted(lease_id_) + ",\"interface\":"
            + quoted(options.interface_name) + ",\"bind_host\":" + quoted(options.bind_host)
            + ",\"bind_port\":" + std::to_string(options.bind_port) + "}"));
}

BrokeredSocketResult ResourceBrokerClient::open_uplink_tcp(const UplinkTcpOptions& options) const {
    const std::string host = normalized_uplink_host(options.host);
    if (options.port == 0) throw std::invalid_argument("UplinkTcpOptions::port must be in 1..65535");
    if (options.timeout.count() < 1 || options.timeout > kUplinkMaxTimeout)
        throw std::invalid_argument("UplinkTcpOptions::timeout must be between 1 ms and 120 s");
    auto result = socket_result(request_fd(socket_path_,
        "{\"op\":\"open_uplink_tcp\",\"lease_id\":" + quoted(lease_id_) + ",\"host\":" + quoted(host)
            + ",\"port\":" + std::to_string(options.port) + ",\"timeout_ms\":"
            + std::to_string(options.timeout.count()) + "}",
        options.timeout + kUplinkReplyMargin));
    try {
        const std::string problem = uplink_problem(result.socket, r2kernel::sdk_detail::parse_json(result.metadata_json),
                                                   lease_id_, host, options.port);
        if (!problem.empty()) throw std::runtime_error("Invalid app uplink response: " + problem);
        return result;
    } catch (...) {
        close_descriptor(result.socket);
        throw;
    }
}

BrokeredSocketResult ResourceBrokerClient::open_tcp_socket(const TcpSocketOptions& options) const {
    return socket_result(request_fd(socket_path_,
        "{\"op\":\"open_tcp_socket\",\"lease_id\":" + quoted(lease_id_) + ",\"interface\":"
            + quoted(options.interface_name) + ",\"bind_host\":" + quoted(options.bind_host)
            + ",\"bind_port\":" + std::to_string(options.bind_port) + ",\"connect_host\":"
            + (options.connect_host ? quoted(*options.connect_host) : "null") + ",\"connect_port\":"
            + (options.connect_port ? std::to_string(*options.connect_port) : "null") + ",\"listen\":"
            + json_bool(options.listen) + ",\"backlog\":" + std::to_string(options.backlog) + "}"));
}

BrokeredSocketResult ResourceBrokerClient::open_frontend_tcp_listener(const FrontendTcpListenerOptions& options) const {
    return frontend_socket_result(request_fd(socket_path_,
        "{\"op\":\"open_frontend_tcp_listener\",\"lease_id\":" + quoted(lease_id_) + ",\"kind\":"
            + quoted(options.kind) + ",\"bind_host\":" + quoted(options.bind_host.value_or(""))
            + ",\"bind_port\":" + std::to_string(options.bind_port.value_or(0)) + ",\"backlog\":"
            + std::to_string(options.backlog) + "}"));
}

BrokeredSocketResult ResourceBrokerClient::open_can_socket(const CanSocketOptions& options) const {
    return socket_result(request_fd(socket_path_,
        "{\"op\":\"open_can_socket\",\"lease_id\":" + quoted(lease_id_) + ",\"interface\":"
            + quoted(options.interface_name) + ",\"receive_own_messages\":"
            + json_bool(options.receive_own_messages) + ",\"can_fd_frames\":" + json_bool(options.can_fd_frames)
            + ",\"nonblocking\":" + json_bool(options.nonblocking) + "}"));
}

BrokeredFdResult ResourceBrokerClient::open_device(const std::string& path, const DeviceOpenOptions& options) const {
    auto brokered = request_fd(socket_path_,
        "{\"op\":\"open_device\",\"lease_id\":" + quoted(lease_id_) + ",\"path\":" + quoted(path) + ",\"read\":"
            + json_bool(options.read) + ",\"write\":" + json_bool(options.write) + ",\"nonblocking\":"
            + json_bool(options.nonblocking) + "}");
    BrokeredFdResult result;
    result.fd = brokered.second;
    result.metadata_json = std::move(brokered.first);
    return result;
}

int ResourceBrokerClient::inherited_fd(const std::string& kind, const InheritedSocketExpectation& expect) {
    return r2kernel::inherited_socket(kind, expect);
}

// R2KernelClient pass-throughs: each call creates a ResourceBrokerClient from
// the environment, as the Python SDK's R2KernelClient.open_* do.
BrokeredSocketResult R2KernelClient::open_udp_socket(const UdpSocketOptions& options) const {
    return ResourceBrokerClient().open_udp_socket(options);
}

BrokeredSocketResult R2KernelClient::open_uplink_tcp(const UplinkTcpOptions& options) const {
    return ResourceBrokerClient().open_uplink_tcp(options);
}

BrokeredSocketResult R2KernelClient::open_tcp_socket(const TcpSocketOptions& options) const {
    return ResourceBrokerClient().open_tcp_socket(options);
}

BrokeredSocketResult R2KernelClient::open_frontend_tcp_listener(const FrontendTcpListenerOptions& options) const {
    return ResourceBrokerClient().open_frontend_tcp_listener(options);
}

BrokeredSocketResult R2KernelClient::open_can_socket(const CanSocketOptions& options) const {
    return ResourceBrokerClient().open_can_socket(options);
}

BrokeredFdResult R2KernelClient::open_device(const std::string& path, const DeviceOpenOptions& options) const {
    return ResourceBrokerClient().open_device(path, options);
}

}  // namespace r2kernel::core
