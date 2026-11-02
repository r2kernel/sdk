#pragma once

// Fire-and-forget control-socket notification (heartbeat status, frame
// transforms) with one total deadline. Cold control-plane code: never on a
// per-cycle path. Included by wrapper/subsystem.cpp and its unit test;
// internal linkage, so no symbol is exported.

#include <cerrno>
#include <chrono>
#include <cstring>
#include <optional>
#include <string>

#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

namespace r2kernel::wrapper::detail {
namespace {

// Time left until deadline as a socket timeout, rounded up to whole
// microseconds. std::nullopt once the deadline has passed: a zero
// SO_SNDTIMEO/SO_RCVTIMEO would mean "block forever".
inline std::optional<timeval> best_effort_remaining(std::chrono::steady_clock::time_point deadline) {
    const auto left = std::chrono::ceil<std::chrono::microseconds>(deadline - std::chrono::steady_clock::now());
    if (left.count() <= 0) return std::nullopt;
    timeval value {};
    value.tv_sec = static_cast<time_t>(left.count() / 1000000);
    value.tv_usec = static_cast<suseconds_t>(left.count() % 1000000);
    return value;
}

// Sends body to the Unix stream socket at path, half-closes, and drains the
// reply until EOF. The whole exchange is bounded by timeout_ms: before each
// blocking call (connect, every send, every recv) the socket timeout is set to
// the time remaining until one deadline, so a backend that accepts late, reads
// slowly or never replies cannot hold the caller past timeout_ms (plus kernel
// timer granularity). Returns the failure reason; std::nullopt means the
// backend received the request and closed the connection.
#if defined(__GNUC__)
__attribute__((cold))
#endif
std::optional<std::string> send_json_best_effort(const std::string& path, const std::string& body, int timeout_ms) {
    using Clock = std::chrono::steady_clock;
    const auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
    sockaddr_un addr {};
    addr.sun_family = AF_UNIX;
    if (path.empty() || path.size() >= sizeof(addr.sun_path)) {
        return "control socket path is empty or exceeds the Unix socket path limit: " + path;
    }
    std::memcpy(addr.sun_path, path.c_str(), path.size() + 1);
    int socket_type = SOCK_STREAM;
#if defined(SOCK_CLOEXEC)
    socket_type |= SOCK_CLOEXEC;
#endif
    const int fd = ::socket(AF_UNIX, socket_type, 0);
    if (fd < 0) {
        return std::string("control socket creation failed: ") + std::strerror(errno);
    }
    struct Socket { int value; ~Socket() { ::close(value); } } socket {fd};
    const auto expired = [&path, timeout_ms]() {
        return "control socket exchange exceeded " + std::to_string(timeout_ms) + " ms: " + path;
    };
    const auto failure = [&path, timeout_ms](const char* step, int error) {
        if (error == EAGAIN || error == EWOULDBLOCK || error == EINPROGRESS || error == ETIMEDOUT) {
            return std::string("control socket ") + step + " timed out after " + std::to_string(timeout_ms)
                + " ms: " + path;
        }
        return std::string("control socket ") + step + " failed: " + std::strerror(error) + ": " + path;
    };
    // Arms one socket timeout with the remaining time. Returns the errno that
    // stops the exchange (ETIMEDOUT once the deadline has passed), else nullopt.
    const auto arm = [fd, deadline](int option) -> std::optional<int> {
        const auto remaining = best_effort_remaining(deadline);
        if (!remaining) return ETIMEDOUT;
        if (::setsockopt(fd, SOL_SOCKET, option, &*remaining, sizeof(*remaining)) != 0) return errno;
        return std::nullopt;
    };
    if (const auto error = arm(SO_SNDTIMEO)) {
        return *error == ETIMEDOUT ? expired() : failure("timeout setup", *error);
    }
    if (::connect(fd, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) != 0) {
        return failure("connect", errno);
    }
#if defined(MSG_NOSIGNAL)
    constexpr int send_flags = MSG_NOSIGNAL;
#else
    constexpr int send_flags = 0;
#endif
    const char* ptr = body.data();
    std::size_t remaining = body.size();
    while (remaining > 0) {
        if (const auto error = arm(SO_SNDTIMEO)) {
            return *error == ETIMEDOUT ? expired() : failure("timeout setup", *error);
        }
        const auto sent = ::send(fd, ptr, remaining, send_flags);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) {
            return failure("send", sent < 0 ? errno : EPIPE);
        }
        ptr += sent;
        remaining -= static_cast<std::size_t>(sent);
    }
    (void)::shutdown(fd, SHUT_WR);
    char discard[512];
    for (;;) {
        if (const auto error = arm(SO_RCVTIMEO)) {
            return *error == ETIMEDOUT ? expired() : failure("timeout setup", *error);
        }
        const auto received = ::recv(fd, discard, sizeof(discard), 0);
        if (received == 0) return std::nullopt;
        if (received < 0 && errno == EINTR) continue;
        if (received < 0) return failure("receive", errno);
    }
}

}  // namespace
}  // namespace r2kernel::wrapper::detail
