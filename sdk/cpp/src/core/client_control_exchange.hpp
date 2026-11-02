// Included in client.cpp's anonymous namespace after client_protocol.hpp.
// Control JSON socket exchange and binary handler frame parsing/building;
// part of client.cpp's translation unit (no separate compilation).

// Control's Unix listener refuses a connection over its connection or request-byte
// limits with this reply (status "error", "retryable": true) and closes it unread.
bool is_retryable_admission_refusal(const std::string& response) {
    return json_string_field(response, "status").value_or("") == "error" &&
           json_bool_field(response, "retryable").value_or(false);
}

#if !defined(_WIN32)
std::optional<std::string> exchange_local_control_json(
    const std::string& endpoint, const std::string& request, int timeout_ms,
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
    deadline = std::min(deadline, std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms));
    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return std::nullopt;
    struct Socket { int value; ~Socket() { ::close(value); } } socket{fd};
    const int flags = ::fcntl(fd, F_GETFL);
    if (flags < 0 || ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0 ||
        ::fcntl(fd, F_SETFD, FD_CLOEXEC) != 0)
        throw std::runtime_error("Control JSON socket setup failed");
    const auto check_deadline = [&] {
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Control JSON exchange deadline exceeded");
    };
    const auto wait = [&](short events) {
        for (;;) {
            check_deadline();
            const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (remaining <= 0) { check_deadline(); continue; }
            pollfd descriptor{fd, events, 0};
#if defined(__linux__)
            timespec timeout{static_cast<time_t>(remaining / 1000000000),
                             static_cast<long>(remaining % 1000000000)};
            const int result = ::ppoll(&descriptor, 1, &timeout, nullptr);
#else
            const int result = ::poll(&descriptor, 1, static_cast<int>(
                std::min<std::int64_t>((remaining + 999999) / 1000000, std::numeric_limits<int>::max())));
#endif
            if (result < 0 && errno == EINTR) continue;
            check_deadline();
            if (result == 0) continue;
            if (result < 0 || (descriptor.revents & POLLNVAL))
                throw std::runtime_error("Control JSON socket wait failed");
            // While sending, an error (the peer closed) is reported by the next send;
            // while reading, bytes the peer wrote before resetting are read first.
            if ((descriptor.revents & POLLERR) && !(events & POLLOUT) && !(descriptor.revents & events))
                throw std::runtime_error("Control JSON socket wait failed");
            if (descriptor.revents & (events | POLLHUP | POLLERR)) return;
        }
    };
    sockaddr_un address{}; address.sun_family = AF_UNIX;
    if (endpoint.empty() || endpoint.size() >= sizeof(address.sun_path))
        throw std::runtime_error("Invalid Control JSON endpoint");
    std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1);
    check_deadline();
    if (::connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        if (errno != EINPROGRESS) return std::nullopt;
        wait(POLLOUT);
        int error = 0; socklen_t size = sizeof(error);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) != 0 || error != 0) return std::nullopt;
    }
    detail::verify_control_server_connection(fd, deadline);
    std::size_t sent = 0;
    // Control closes a connection it refuses (over its admission limits) without
    // reading it, so the send can fail while its refusal is already buffered.
    bool peer_closed = false;
    while (sent < request.size()) {
        check_deadline();
#if defined(MSG_NOSIGNAL)
        const int send_flags = MSG_NOSIGNAL | MSG_DONTWAIT;
#else
        const int send_flags = MSG_DONTWAIT;
#endif
        const auto count = ::send(fd, request.data() + sent, request.size() - sent, send_flags);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { wait(POLLOUT); continue; }
        if (count < 0 && (errno == EPIPE || errno == ECONNRESET)) { peer_closed = true; break; }
        if (count <= 0) return std::nullopt;
        sent += static_cast<std::size_t>(count);
        check_deadline();
    }
    if (!peer_closed) ::shutdown(fd, SHUT_WR);
    std::string response; char buffer[8192];
    for (;;) {
        check_deadline();
        const auto count = ::recv(fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { wait(POLLIN); continue; }
        // A peer that closed with request bytes unread resets the connection after
        // the reply it wrote has been read.
        if (count < 0 && errno == ECONNRESET && !response.empty()) break;
        if (count < 0) throw std::runtime_error("Control JSON socket read failed");
        check_deadline();
        if (count == 0) break;
        response.append(buffer, static_cast<std::size_t>(count));
    }
    if (response.empty()) return std::nullopt;
    if (peer_closed && !is_retryable_admission_refusal(response)) return std::nullopt;
    return response;
}

#include "control_shm_channel.hpp"
#endif

struct SubscriberLockInfo {
    std::string subscriber;
    std::string lock_name;
    bool internal = false;
};

struct PublisherSubscriptionInfo {
    std::string publisher;
    std::size_t slot_index;
    std::size_t queue_depth = 1;
    std::size_t entry_slot_count = 1;
    std::string lock_name;
};

std::vector<SubscriberLockInfo> json_subscriber_locks(const std::string& text) {
    std::vector<SubscriberLockInfo> result;
    const std::string pattern = "\"" + std::string(kSubscribers) + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return result;
    }
    const std::size_t array_begin = text.find('[', key_pos + pattern.size());
    const std::size_t array_end = text.find(']', array_begin == std::string::npos ? key_pos : array_begin);
    if (array_begin == std::string::npos || array_end == std::string::npos || array_end <= array_begin) {
        return result;
    }
    std::size_t pos = array_begin + 1;
    while (pos < array_end) {
        const std::size_t obj_begin = text.find('{', pos);
        if (obj_begin == std::string::npos || obj_begin >= array_end) {
            break;
        }
        const std::size_t obj_end = text.find('}', obj_begin);
        if (obj_end == std::string::npos || obj_end > array_end) {
            break;
        }
        const std::string object = text.substr(obj_begin, obj_end - obj_begin + 1);
        const auto subscriber = json_string_field(object, "subscriber");
        const auto lock_name = json_string_field(object, kLockName);
        const bool internal = json_bool_field(object, kInternal).value_or(false);
        if (subscriber && lock_name) {
            result.push_back({*subscriber, *lock_name, internal});
        }
        pos = obj_end + 1;
    }
    return result;
}

std::vector<PublisherSubscriptionInfo> json_publishers(const std::string& text) {
    std::vector<PublisherSubscriptionInfo> result;
    const std::string pattern = "\"" + std::string(kPublishers) + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return result;
    }
    const std::size_t array_begin = text.find('[', key_pos + pattern.size());
    const std::size_t array_end = text.find(']', array_begin == std::string::npos ? key_pos : array_begin);
    if (array_begin == std::string::npos || array_end == std::string::npos || array_end <= array_begin) {
        return result;
    }
    std::size_t pos = array_begin + 1;
    while (pos < array_end) {
        const std::size_t obj_begin = text.find('{', pos);
        if (obj_begin == std::string::npos || obj_begin >= array_end) {
            break;
        }
        const std::size_t obj_end = text.find('}', obj_begin);
        if (obj_end == std::string::npos || obj_end > array_end) {
            break;
        }
        const std::string object = text.substr(obj_begin, obj_end - obj_begin + 1);
        const auto publisher = json_string_field(object, kPublisher);
        const auto slot_index = json_int_field(object, kPublishSlotIndex);
        const auto queue_depth = json_int_field(object, kQueueDepth);
        const auto entry_slot_count = json_int_field(object, kEntrySlotCount);
        const auto lock_name = json_string_field(object, kLockName);
        if (publisher && slot_index && lock_name) {
            result.push_back({
                *publisher,
                static_cast<std::size_t>(*slot_index),
                static_cast<std::size_t>(std::max<long long>(1, queue_depth.value_or(1))),
                static_cast<std::size_t>(std::max<long long>(1, entry_slot_count.value_or(1))),
                *lock_name,
            });
        }
        pos = obj_end + 1;
    }
    return result;
}

std::string exchange_control_json(const std::string& request, const std::string& host, int port, int timeout_ms,
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
    (void)host;
    (void)port;
    // An admission refusal is retried after a short jittered pause within the request's
    // time budget (many local subsystems starting together); the last one is returned.
    const auto budget = std::min(deadline, std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms));
    thread_local std::minstd_rand jitter(static_cast<std::minstd_rand::result_type>(
        std::chrono::steady_clock::now().time_since_epoch().count()));
    for (;;) {
        auto response = exchange_local_control_json(control_socket_path(), request, timeout_ms, deadline);
        if (!response) {
            throw std::runtime_error(
                "Failed to exchange request with R2Kernel control socket at " + control_socket_path()
            );
        }
        const auto pause = std::chrono::milliseconds(std::uniform_int_distribution<int>(10, 50)(jitter));
        if (!is_retryable_admission_refusal(*response) || std::chrono::steady_clock::now() + pause >= budget)
            return *response;
        std::this_thread::sleep_for(pause);
    }
}

void send_control_json_best_effort(const std::string& request, const std::string& host, int port) {
    try {
        (void)exchange_control_json(request, host, port, sdk_environment::kBestEffortControlTimeoutMs);
    } catch (...) {
    }
}
