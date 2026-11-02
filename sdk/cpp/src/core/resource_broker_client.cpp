// Brokered resource descriptors (r2kernel-resourced). Cold setup-time code in
// its own translation unit; the wire contract and every check follow the
// Python SDK's r2kernel/core/resource.py.
#include "r2kernel/core/resource_broker_client.hpp"

#include "r2kernel/core/client.hpp"
#include "json_text.hpp"
#include "json_value.hpp"

#include <chrono>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#if defined(__linux__)
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
    ResourceRequest(const std::string& socket_path, std::string line)
        : socket_path_(socket_path), line_(std::move(line)), deadline_(Clock::now() + kResourceRequestTimeout) {}

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
            if (error != nullptr && error->truthy())
                throw std::runtime_error(error->is_string() ? error->text() : error->dump());
            throw std::runtime_error("R2Kernel resource request failed");
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

std::pair<std::string, int> request_fd(const std::string& socket_path, const std::string& request_json) {
#if defined(__linux__)
    ResourceRequest request(socket_path, request_json + "\n");
    auto [metadata, fd] = request.run();
    return {metadata.dump(), fd};
#else
    (void)socket_path;
    (void)request_json;
    throw std::runtime_error("R2Kernel resource broker requests require Linux Unix descriptor passing");
#endif
}

BrokeredSocketResult socket_result(std::pair<std::string, int> brokered) {
    BrokeredSocketResult result;
    result.socket = brokered.second;
    result.metadata_json = std::move(brokered.first);
    return result;
}

void close_descriptor(int& fd) noexcept {
#if defined(__linux__)
    if (fd >= 0) ::close(fd);
#endif
    fd = -1;
}

}  // namespace

void BrokeredSocketResult::close() noexcept { close_descriptor(socket); }
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
    return socket_result(request_fd(socket_path_,
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
