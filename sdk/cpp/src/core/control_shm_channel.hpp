// Version 5 requires remaining_ns on requests, transports all payloads in
// anonymous SHM and requires explicit response-consumption acknowledgements.
#include "control_shm_memory.hpp"

constexpr std::uint8_t kControlShmVersion = 5;
constexpr std::size_t kControlShmMetadataLimit = 64U * 1024U;

bool is_control_shm_frame(const std::string& data, std::uint8_t kind) {
    return data.size() >= 6 && std::memcmp(data.data(), kLocalControlMagic, 4) == 0 &&
        static_cast<std::uint8_t>(data[4]) == kControlShmVersion &&
        static_cast<std::uint8_t>(data[5]) == kind;
}

void append_control_shm_header(std::string& out, std::uint8_t kind) {
    append_header(out, kind);
    out[4] = static_cast<char>(kControlShmVersion);
}

struct ControlShmFrame {
    std::string data;
    ControlShmDescriptor descriptor;
};

using detail::ControlShmPeerClosed;
using detail::ControlShmSocketError;

class ControlShmFraming {
public:
    using Deadline = std::chrono::steady_clock::time_point;

    static void send(int socket, const std::string& data, int descriptor, int timeout_ms,
                     ControlShmMetrics& metrics) {
        send_until(socket, data, descriptor,
                   std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms), metrics);
    }

    static void send_until(int socket, const std::string& data, int descriptor, Deadline deadline,
                           ControlShmMetrics& metrics) {
        send_view_until(socket, data, descriptor, deadline, metrics);
    }
    static void send_view_until(int socket, std::string_view data, int descriptor, Deadline deadline,
                                ControlShmMetrics& metrics) {
        if (data.size() < 6 || data.size() > kControlShmMetadataLimit)
            throw ControlShmProtocolError("Control v5 metadata frame size out of bounds");
        const auto size = static_cast<std::uint32_t>(data.size());
        const char* prefix = reinterpret_cast<const char*>(&size);
        const auto frame_size = sizeof(size) + data.size();
        std::size_t sent = 0;
        while (sent < frame_size) {
            check_deadline(deadline);
            // Keep the length prefix and prepared metadata in their existing
            // storage. One byte cursor handles a short write in either vector.
            iovec vectors[2]{};
            std::size_t vector_count = 0;
            if (sent < sizeof(size)) {
                vectors[vector_count++] = {const_cast<char*>(prefix + sent), sizeof(size) - sent};
                vectors[vector_count++] = {const_cast<char*>(data.data()), data.size()};
            } else {
                const auto body_sent = sent - sizeof(size);
                vectors[vector_count++] = {const_cast<char*>(data.data() + body_sent), data.size() - body_sent};
            }
            alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int))]{};
            msghdr message{};
            message.msg_iov = vectors;
            message.msg_iovlen = vector_count;
            if (descriptor >= 0 && sent == 0) {
                message.msg_control = ancillary;
                message.msg_controllen = sizeof(ancillary);
                auto* header = CMSG_FIRSTHDR(&message);
                header->cmsg_level = SOL_SOCKET;
                header->cmsg_type = SCM_RIGHTS;
                header->cmsg_len = CMSG_LEN(sizeof(int));
                std::memcpy(CMSG_DATA(header), &descriptor, sizeof(descriptor));
            }
            const auto count = ::sendmsg(socket, &message, send_flags());
            if (count < 0 && errno == EINTR) continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                wait(socket, POLLOUT, deadline);
                continue;
            }
            if (count <= 0) throw ControlShmSocketError("Control v5 frame send failed", count < 0 ? errno : 0);
            if (sent == 0 && descriptor >= 0) ++metrics.fd_sent;
            sent += static_cast<std::size_t>(count);
            check_deadline(deadline);
        }
    }

    static ControlShmFrame receive(int socket, int timeout_ms, ControlShmMetrics& metrics) {
        return receive_until(socket, std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms), metrics);
    }

    static ControlShmFrame receive_until(int socket, Deadline deadline, ControlShmMetrics& metrics) {
        ControlShmFrame frame;
        std::uint32_t size = 0;
        receive_exact(socket, reinterpret_cast<char*>(&size), sizeof(size), deadline, frame, true, metrics);
        if (size < 6 || size > kControlShmMetadataLimit)
            throw ControlShmProtocolError("Control v5 metadata frame size out of bounds");
        frame.data.resize(size);
        receive_exact(socket, frame.data.data(), size, deadline, frame, false, metrics);
        return frame;
    }

    static ControlShmDescriptor receive_into_until(int socket, char* output, std::size_t capacity,
                                                    std::size_t& length, Deadline deadline,
                                                    ControlShmMetrics& metrics) {
        ControlShmFrame frame;
        std::uint32_t size = 0;
        receive_exact(socket, reinterpret_cast<char*>(&size), sizeof(size), deadline, frame, true, metrics);
        if (size < 6 || size > kControlShmMetadataLimit || size > capacity)
            throw ControlShmProtocolError("Control v5 metadata frame size out of bounds");
        receive_exact(socket, output, size, deadline, frame, false, metrics);
        length = size;
        return std::move(frame.descriptor);
    }

    static ControlShmDescriptor connect(const std::string& endpoint, int timeout_ms) {
        return connect_until(endpoint, std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms));
    }

    static ControlShmDescriptor connect_until(const std::string& endpoint, Deadline deadline) {
        ControlShmDescriptor socket(::socket(AF_UNIX, SOCK_STREAM, 0));
        if (socket.get() < 0) throw std::runtime_error("Control v5 socket allocation failed");
        if (::fcntl(socket.get(), F_SETFD, FD_CLOEXEC) != 0)
            throw std::runtime_error("Control v5 socket CLOEXEC failed");
#if defined(__APPLE__)
        int enabled = 1;
        if (::setsockopt(socket.get(), SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled)) != 0)
            throw std::runtime_error("Control v5 socket signal policy failed");
#endif
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        if (endpoint.empty() || endpoint.size() >= sizeof(address.sun_path))
            throw std::runtime_error("Invalid Control v5 socket endpoint");
        std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1);
        const int original_flags = ::fcntl(socket.get(), F_GETFL);
        if (original_flags < 0 || ::fcntl(socket.get(), F_SETFL, original_flags | O_NONBLOCK) != 0)
            throw std::runtime_error("Control v5 nonblocking connect setup failed");
        check_deadline(deadline);
        if (::connect(socket.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
            if (errno != EINPROGRESS) throw std::runtime_error("Control v5 connect failed");
            wait(socket.get(), POLLOUT, deadline);
            int result = 0;
            socklen_t length = sizeof(result);
            if (::getsockopt(socket.get(), SOL_SOCKET, SO_ERROR, &result, &length) != 0 || result != 0)
                throw std::runtime_error("Control v5 connect failed");
        }
        check_deadline(deadline);
        detail::verify_control_server_connection(socket.get(), deadline);
        check_deadline(deadline);
        // Keep nonblocking I/O so a large/partial metadata frame cannot defeat
        // the single exchange deadline after poll reports readiness.
        return socket;
    }
private:
    static int send_flags() {
#if defined(MSG_NOSIGNAL)
        return MSG_NOSIGNAL | MSG_DONTWAIT;
#else
        return MSG_DONTWAIT;
#endif
    }
    static void check_deadline(Deadline deadline) {
        if (std::chrono::steady_clock::now() >= deadline)
            throw ControlShmDeadlineError("Control v5 I/O deadline exceeded");
    }
    static void wait(int socket, short events, Deadline deadline) {
        for (;;) {
            check_deadline(deadline);
            const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (remaining <= 0) { check_deadline(deadline); continue; }
            pollfd descriptor{socket, events, 0};
#if defined(__linux__)
            timespec timeout{static_cast<time_t>(remaining / 1000000000),
                             static_cast<long>(remaining % 1000000000)};
            const int result = ::ppoll(&descriptor, 1, &timeout, nullptr);
#else
            const int result = ::poll(&descriptor, 1, static_cast<int>(std::min<std::int64_t>(
                (remaining + 999999) / 1000000, std::numeric_limits<int>::max())));
#endif
            if (result < 0 && errno == EINTR) continue;
            if (result == 0) {
                // Re-check the absolute clock after an early timeout.
                check_deadline(deadline);
                continue;
            }
            if (result < 0 || (descriptor.revents & POLLNVAL))
                throw std::runtime_error("Control v5 socket closed or I/O deadline exceeded");
            // A pending socket error (POLLERR) is reported, with its errno, by
            // the send or receive that follows (ControlShmSocketError).
            if (descriptor.revents & (events | POLLHUP | POLLERR)) return;
        }
    }
    static void receive_exact(int socket, char* output, std::size_t size, Deadline deadline,
                              ControlShmFrame& frame, bool prefix, ControlShmMetrics& metrics) {
        std::size_t received = 0;
        while (received < size) {
            check_deadline(deadline);
            iovec vector{output + received, size - received};
            // Room for surplus descriptors so every delivered fd can be closed
            // before rejecting a malformed ancillary message.
            alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int) * 16)]{};
            msghdr message{};
            message.msg_iov = &vector; message.msg_iovlen = 1;
            message.msg_control = ancillary; message.msg_controllen = sizeof(ancillary);
            std::array<ControlShmDescriptor, 16> descriptors;
            std::size_t descriptor_count = 0;
            // Stack RAII owns every installed descriptor without heap allocation.
            int flags = MSG_DONTWAIT;
#if defined(MSG_CMSG_CLOEXEC)
            flags |= MSG_CMSG_CLOEXEC;
#endif
            const auto count = ::recvmsg(socket, &message, flags);
            if (count < 0 && errno == EINTR) continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                wait(socket, POLLIN, deadline);
                continue;
            }
            if (count < 0) throw ControlShmSocketError("Control v5 frame receive syscall failed", errno);
            if (count == 0 && prefix && received == 0) throw ControlShmPeerClosed();
            if (count == 0) throw ControlShmProtocolError("Control v5 frame ended before completion");
            bool invalid = (message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)) != 0;
            for (auto* header = CMSG_FIRSTHDR(&message); header; header = CMSG_NXTHDR(&message, header)) {
                if (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS || header->cmsg_len < CMSG_LEN(0)) {
                    invalid = true;
                    continue;
                }
                const auto bytes = header->cmsg_len - CMSG_LEN(0);
                if (bytes % sizeof(int)) invalid = true;
                for (std::size_t offset = 0; offset + sizeof(int) <= bytes; offset += sizeof(int)) {
                    int descriptor = -1;
                    std::memcpy(&descriptor, CMSG_DATA(header) + offset, sizeof(descriptor));
                    if (descriptor_count < descriptors.size()) descriptors[descriptor_count++].reset(descriptor);
                    else { ::close(descriptor); invalid = true; }
                    ++metrics.fd_received;
                    if (::fcntl(descriptor, F_SETFD, FD_CLOEXEC) != 0) invalid = true;
                }
            }
            if (descriptor_count != 0) {
                if (!prefix || received != 0 || frame.descriptor.get() >= 0 || descriptor_count != 1) invalid = true;
                if (!invalid) frame.descriptor = std::move(descriptors.front());
            }
            if (invalid) throw ControlShmProtocolError("Unexpected/truncated Control v5 ancillary descriptors");
            received += static_cast<std::size_t>(count);
            // Own all received rights before rejecting a late successful read.
            check_deadline(deadline);
        }
    }
};

#include "control_shm_prepared.hpp"

class ControlShmConnectionState {
public:
    explicit ControlShmConnectionState(ControlShmMetrics& metrics)
        : outgoing(metrics, true), incoming(metrics, false), metrics_(metrics) { metrics_.reserve_channel(); }
    ~ControlShmConnectionState() { outgoing.reset(); incoming.reset(); metrics_.release_channel(); }
    ControlShmRegion outgoing;
    ControlShmRegion incoming;
private:
    ControlShmMetrics& metrics_;
};


struct ControlShmSessionIdentity {
    std::string source, target, topic, managed_topic, host, endpoint;
    int port = 0;
    std::uint64_t endpoint_generation = 0;
    std::uint64_t codec_generation = 0;
    std::shared_ptr<std::atomic<bool>> route_ready;
};

class ControlShmClientChannel {
public:
    explicit ControlShmClientChannel(ControlShmMetrics& metrics)
        : state(metrics), prepared(metrics), metrics_(metrics) { response_lease.region_name.resize(128, '\0'); response_lease.region_name.clear(); }
    ~ControlShmClientChannel() { stop(); }
    ControlShmConnectionState state;
    ControlShmPreparedMemory prepared;
    LocalPayloadLease response_lease;
    std::mutex operation_mutex;
    bool invalidated = false; // Protected by operation_mutex.
    // Identity is set once before registry publication, then remains immutable.
    ControlShmSessionIdentity session;
    std::atomic<bool> session_valid{true};
    std::uint64_t next_response_ticket = 1;
    bool provider_ready = false;
    bool preparation_attempted = false;
    bool prepared_result_ready = false;
    std::size_t request_capacity = 0, response_capacity = 0;
    int preparation_timeout_ms = 0; // Bound of an automatic re-preparation of this lane.
    std::optional<std::array<std::string, 4>> codec_identity;
    void mark_response_ready(std::chrono::steady_clock::time_point ready) {
        response_ready_at_ = ready;
    }

    void invalidate() {
        session_valid.store(false, std::memory_order_release);
        invalidated = true;
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        socket_.reset();
        state.outgoing.reset();
        state.incoming.reset();
    }
    int socket_descriptor() const { return socket_.get(); }
    // Cold failure path, operation_mutex held: stop sending on the failed lane but
    // keep its descriptor so the caller can wait for Control to finish and close it.
    ControlShmDescriptor retire_socket() {
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        if (socket_.get() >= 0) ::shutdown(socket_.get(), SHUT_WR);
        return std::move(socket_);
    }
    void connect_until(const std::string& endpoint, ControlShmFraming::Deadline deadline) {
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        if (stopping_) throw std::runtime_error("Control SHM channel is stopped");
        if (socket_.get() < 0) socket_ = ControlShmFraming::connect_until(endpoint, deadline);
    }
    ControlShmFrame exchange(const std::string& endpoint, const std::string& request,
                             int outgoing_descriptor, ControlShmFraming::Deadline deadline) {
        connect_until(endpoint, deadline);
        ControlShmFraming::send_until(socket_.get(), request, outgoing_descriptor, deadline, metrics_);
        return ControlShmFraming::receive_until(socket_.get(), deadline, metrics_);
    }
    void prepare_memory() {
        const bool required_lock = control_shm_caller_requires_lock();
        prepared.prepare(required_lock);
        state.outgoing.prepare_memory(required_lock);
        state.incoming.prepare_memory(required_lock);
    }
    ControlShmPreparedFrame exchange_prepared(const std::string& endpoint, std::string_view request,
                                             int descriptor, ControlShmFraming::Deadline deadline) {
        connect_until(endpoint, deadline);
        ControlShmFraming::send_view_until(socket_.get(), request, descriptor, deadline, metrics_);
        std::size_t length = 0;
        auto incoming = ControlShmFraming::receive_into_until(socket_.get(), prepared.buffer(1),
            ControlShmPreparedMemory::capacity, length, deadline, metrics_);
        return {{prepared.buffer(1), length}, std::move(incoming)};
    }
    void consume_response(std::uint64_t ticket, const LocalPayloadLease& lease,
                          ControlShmFraming::Deadline deadline) {
        if (ticket == 0 || ticket != next_response_ticket || ticket == std::numeric_limits<std::uint64_t>::max())
            throw ControlShmProtocolError("Invalid Control response ticket sequence");
        ControlShmMetadataWriter acknowledgement(prepared.buffer(2));
        acknowledgement.header(kLocalControlKindResponseConsumed);
        acknowledgement.ticket(ticket);
        acknowledgement.lease(lease.region_name, lease.slot_size, lease.length);
        ControlShmFraming::send_view_until(socket_.get(), acknowledgement.view(), -1, deadline, metrics_);
    }
    void receive_release(std::uint64_t ticket, ControlShmFraming::Deadline deadline) {
        // This ticket's owned-response boundary includes CONSUMED publication,
        // background receiver scheduling and the validated RELEASED wait.
        ControlShmDurationScope timing(metrics_.release_receive, metrics_.timing_enabled, response_ready_at_, false);
        std::size_t length = 0;
        auto descriptor = ControlShmFraming::receive_into_until(socket_.get(), prepared.buffer(3),
            ControlShmPreparedMemory::capacity, length, deadline, metrics_);
        ControlShmMetadataReader reader({prepared.buffer(3), length});
        if (descriptor.get() >= 0 || !reader.kind(kLocalControlKindResponseReleased) || reader.ticket() != ticket)
            throw ControlShmProtocolError("Malformed Control response-released frame");
        reader.end();
        timing.finish();
    }
    void wait_reclaimed(ControlShmFraming::Deadline deadline) {
        const auto started = metrics_.timing_enabled ? std::chrono::steady_clock::now() :
            std::chrono::steady_clock::time_point{};
        std::unique_lock<std::mutex> guard(reclaim_mutex_);
        // Record a barrier only when an earlier ticket is still retiring.
        ControlShmDurationScope timing(metrics_.reclaim_barrier, metrics_.timing_enabled && pending_, started);
        if (!reclaim_condition_.wait_until(guard, deadline, [this] { return !pending_ || stopping_; }))
            throw std::runtime_error("Control SHM reclamation barrier deadline exceeded");
        if (reclaim_failure_) std::rethrow_exception(reclaim_failure_);
        if (stopping_) throw std::runtime_error("Control SHM channel is stopped");
    }
    // The caller must own operation_mutex: re-check the barrier after taking
    // that lock, since another foreground may have published a release meanwhile.
    void require_reclaimed() {
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        if (reclaim_failure_) std::rethrow_exception(reclaim_failure_);
        if (stopping_) throw std::runtime_error("Control SHM channel is stopped");
        if (pending_) throw std::runtime_error("Control SHM lane is busy pending reclamation");
    }
    bool has_pending_release() {
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        return pending_;
    }
    void ensure_reclaimer(ControlShmFraming::Deadline deadline = ControlShmFraming::Deadline::max()) {
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Control SHM reclaimer preparation deadline exceeded");
        std::unique_lock<std::mutex> guard(reclaim_mutex_);
        if (stopping_) throw std::runtime_error("Control SHM channel is stopped");
        if (!reclaimer_.joinable()) reclaimer_ = std::thread([this] { reclaim_loop(); });
        if (!reclaim_condition_.wait_until(guard, deadline, [this] { return reclaimer_ready_ || stopping_; }))
            throw std::runtime_error("Control SHM reclaimer preparation deadline exceeded");
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Control SHM reclaimer preparation deadline exceeded");
        if (stopping_) throw std::runtime_error("Control SHM channel is stopped");
    }
    // Called while the foreground owns operation_mutex, after CONSUMED is sent.
    void reclaim_response(std::uint64_t ticket, ControlShmFraming::Deadline deadline) {
        std::lock_guard<std::mutex> guard(reclaim_mutex_);
        pending_ticket_ = ticket;
        pending_deadline_ = deadline;
        pending_ = true;
        reclaim_condition_.notify_all();
    }
    void stop() {
        std::lock_guard<std::mutex> shutdown_guard(stop_mutex_);
        {
            std::lock_guard<std::mutex> guard(reclaim_mutex_);
            stopping_ = true;
            if (socket_.get() >= 0) ::shutdown(socket_.get(), SHUT_RDWR);
            reclaim_condition_.notify_all();
        }
        if (reclaimer_.joinable()) reclaimer_.join();
        std::lock_guard<std::mutex> operation(operation_mutex);
        invalidate();
    }
private:
    void reclaim_loop() {
        {
            std::lock_guard<std::mutex> guard(reclaim_mutex_);
            reclaimer_ready_ = true;
            reclaim_condition_.notify_all();
        }
        for (;;) {
            std::uint64_t ticket;
            ControlShmFraming::Deadline deadline;
            {
                std::unique_lock<std::mutex> guard(reclaim_mutex_);
                reclaim_condition_.wait(guard, [this] { return pending_ || stopping_; });
                if (stopping_) return;
                ticket = pending_ticket_;
                deadline = pending_deadline_;
            }
            std::exception_ptr failure;
            {
                // Each owner acquires and releases its own mutex lock. The
                // foreground releases this lock when returning the owned result.
                std::lock_guard<std::mutex> operation(operation_mutex);
                try {
                    receive_release(ticket, deadline);
                    ++next_response_ticket;
                } catch (const std::exception& error) {
                    metrics_.record_error(error);
                    failure = std::current_exception();
                    invalidate();
                } catch (...) {
                    failure = std::current_exception();
                    invalidate();
                }
            }
            {
                std::lock_guard<std::mutex> guard(reclaim_mutex_);
                if (failure) reclaim_failure_ = failure;
                pending_ = false;
                reclaim_condition_.notify_all();
                if (stopping_ || failure) return;
            }
        }
    }
    ControlShmDescriptor socket_;
    ControlShmMetrics& metrics_;
    std::mutex stop_mutex_;
    std::mutex reclaim_mutex_;
    std::condition_variable reclaim_condition_;
    std::thread reclaimer_;
    bool reclaimer_ready_ = false;
    bool stopping_ = false;
    bool pending_ = false;
    std::uint64_t pending_ticket_ = 0;
    ControlShmFraming::Deadline pending_deadline_{};
    std::exception_ptr reclaim_failure_;
    std::chrono::steady_clock::time_point response_ready_at_{};
};

#include "control_shm_session.hpp"
