// Exercise production SDK framing/regions using actual OS mappings and fds.
#include <array>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <string_view>
#include <sys/socket.h>
#include <thread>
#include <vector>

// Limit actual sendmsg writes to exercise boundaries that AF_UNIX does not
// reliably produce for a four-byte prefix. Successful writes and SCM_RIGHTS
// delivery still use the real kernel; only explicit retry cases inject errno.
struct ControlFrameSendProbe {
    std::string_view body;
    int descriptor = -1;
    std::vector<int> steps; // Negative errno, positive byte cap, or zero for no cap.
    std::size_t attempts = 0;
    std::size_t sent = 0;
    std::size_t positive_writes = 0;
    std::size_t partial_writes = 0;
    std::size_t kernel_backpressure = 0;
    std::atomic<bool>* backpressure_observed = nullptr;
    std::chrono::milliseconds positive_delay{0};
};

thread_local ControlFrameSendProbe* control_frame_send_probe = nullptr;

ssize_t control_frame_test_sendmsg(int socket, const msghdr* message, int flags) {
    auto* probe = control_frame_send_probe;
    if (!probe) return ::sendmsg(socket, message, flags);
    assert(flags & MSG_DONTWAIT);
#if defined(MSG_NOSIGNAL)
    assert(flags & MSG_NOSIGNAL);
#endif
    const auto prefix_size = sizeof(std::uint32_t);
    const auto frame_size = prefix_size + probe->body.size();
    assert(probe->sent < frame_size);
    if (probe->sent < prefix_size) {
        assert(message->msg_iovlen == 2);
        assert(message->msg_iov[0].iov_len == prefix_size - probe->sent);
        const auto body_size = static_cast<std::uint32_t>(probe->body.size());
        assert(std::memcmp(message->msg_iov[0].iov_base,
                           reinterpret_cast<const char*>(&body_size) + probe->sent,
                           prefix_size - probe->sent) == 0);
        assert(message->msg_iov[1].iov_base == probe->body.data());
        assert(message->msg_iov[1].iov_len == probe->body.size());
    } else {
        assert(message->msg_iovlen == 1);
        assert(message->msg_iov[0].iov_base == probe->body.data() + probe->sent - prefix_size);
        assert(message->msg_iov[0].iov_len == frame_size - probe->sent);
    }
    if (probe->descriptor >= 0 && probe->sent == 0) {
        auto* header = CMSG_FIRSTHDR(const_cast<msghdr*>(message));
        assert(header && header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_RIGHTS);
        assert(header->cmsg_len == CMSG_LEN(sizeof(int)));
        int descriptor = -1;
        std::memcpy(&descriptor, CMSG_DATA(header), sizeof(descriptor));
        assert(descriptor == probe->descriptor);
        assert(CMSG_NXTHDR(const_cast<msghdr*>(message), header) == nullptr);
    } else {
        assert(message->msg_control == nullptr && message->msg_controllen == 0);
    }
    const int step = probe->attempts < probe->steps.size() ? probe->steps[probe->attempts] : 0;
    ++probe->attempts;
    if (step < 0) {
        errno = -step;
        return -1;
    }
    msghdr limited_message = *message;
    std::array<iovec, 2> limited_vectors{};
    if (step > 0) {
        std::size_t remaining = static_cast<std::size_t>(step);
        limited_message.msg_iovlen = 0;
        for (std::size_t index = 0; index < message->msg_iovlen && remaining; ++index) {
            limited_vectors[index] = message->msg_iov[index];
            if (limited_vectors[index].iov_len > remaining) limited_vectors[index].iov_len = remaining;
            remaining -= limited_vectors[index].iov_len;
            ++limited_message.msg_iovlen;
        }
        limited_message.msg_iov = limited_vectors.data();
    }
    const auto count = ::sendmsg(socket, &limited_message, flags);
    if (count > 0) {
        ++probe->positive_writes;
        if (static_cast<std::size_t>(count) < frame_size - probe->sent) ++probe->partial_writes;
        probe->sent += static_cast<std::size_t>(count);
        if (probe->positive_delay.count()) std::this_thread::sleep_for(probe->positive_delay);
    } else if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        ++probe->kernel_backpressure;
        if (probe->backpressure_observed) *probe->backpressure_observed = true;
    }
    return count;
}

#define sendmsg control_frame_test_sendmsg
#include "../src/core/client.cpp"
#undef sendmsg
#include "control_shm_peer_helpers.hpp"
#include <dirent.h>
#include <signal.h>
#include <sys/wait.h>

using namespace r2kernel::core;
r2kernel::wrapper::ControlResponse decode_control_shm_response(ControlShmFrame frame,
                                                                     ControlShmRegion& incoming,
                                                                     std::uint64_t* response_ticket_out = nullptr,
                                                                     LocalPayloadLease* response_lease_out = nullptr) {
    if (is_control_shm_frame(frame.data, kLocalControlKindError)) {
        std::size_t error_position = 6;
        const auto error = read_string_binary(frame.data, error_position);
        if (!error || error_position != frame.data.size() || frame.descriptor.get() >= 0)
            throw ControlShmProtocolError("Malformed Control v5 transport error");
        throw std::runtime_error("Control v5 runtime error: " + *error);
    }
    if (!is_control_shm_frame(frame.data, kLocalControlKindTransactResp))
        throw ControlShmProtocolError("Expected Control v5 response; runtime/SDK upgrade may be required");
    std::size_t position = 6;
    const auto ok = read_u8(frame.data, position);
    const auto status = read_u16(frame.data, position);
    const auto timestamp = read_i64_binary(frame.data, position);
    const auto error = read_string_binary(frame.data, position);
    if (!ok || !status || !timestamp || !error) throw ControlShmProtocolError("Truncated Control v5 response");
    if (*ok == 0) {
        const auto lease_present = read_u8(frame.data, position);
        const auto inline_length = read_u32(frame.data, position);
        if (!lease_present || *lease_present != 0 || !inline_length || *inline_length != 0 ||
            position != frame.data.size() || frame.descriptor.get() >= 0)
            throw ControlShmProtocolError("Malformed failed Control v5 response");
        throw std::runtime_error("Control v5 runtime error: " + *error);
    }
    if (*ok != 1 || !error->empty()) throw ControlShmProtocolError("Malformed successful Control v5 response");
    const auto response_lease = read_control_shm_lease(frame.data, position, false);
    const auto ticket = read_u64_little_endian(frame.data, position);
    if (!ticket || *ticket == 0 || position != frame.data.size())
        throw ControlShmProtocolError("Malformed successful Control v5 response ticket or trailing data");
    if (response_ticket_out != nullptr) *response_ticket_out = *ticket;
    if (response_lease_out != nullptr) *response_lease_out = response_lease;
    auto payload = incoming.read(response_lease, std::move(frame.descriptor));
    return r2kernel::wrapper::ControlResponse{*timestamp, *status, std::move(payload)};
}
void acknowledge_control_shm_response(int socket, std::uint64_t response_ticket,
    std::uint64_t expected_ticket, const LocalPayloadLease& response_lease,
    ControlShmFraming::Deadline deadline, ControlShmMetrics& metrics) {
    consume_control_shm_response(socket, response_ticket, expected_ticket, response_lease, deadline, metrics);
    receive_control_shm_release(socket, response_ticket, deadline, metrics);
}

template<class Function> void must_reject(Function function) {
    bool rejected = false;
    try { function(); } catch (const std::exception&) { rejected = true; }
    assert(rejected);
}

std::size_t descriptor_count() {
#if defined(__linux__)
    DIR* directory = ::opendir("/proc/self/fd");
    assert(directory);
    std::size_t count = 0;
    while (::readdir(directory)) ++count;
    ::closedir(directory);
    return count;
#else
    return 0;
#endif
}

void exercise_response_ack_peer(const std::string& fault) {
    ControlShmMetrics metrics;
    int sockets[2];
    assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    ControlShmDescriptor caller(sockets[0]), peer(sockets[1]);
    const LocalPayloadLease lease{"response-region", 0, 4096, 1, 0};
    std::atomic<bool> peer_failed{false};
    std::thread peer_thread([&] {
        try {
            auto acknowledgement = ControlShmFraming::receive(peer.get(), 1000, metrics);
            assert(acknowledgement.descriptor.get() < 0);
            assert(is_control_shm_frame(acknowledgement.data, kLocalControlKindResponseConsumed));
            std::size_t position = 6;
            const auto ticket = read_u64_little_endian(acknowledgement.data, position);
            const auto echoed_lease = read_payload_lease_binary(acknowledgement.data, position);
            assert(ticket && *ticket == 1);
            assert(echoed_lease && echoed_lease->region_name == lease.region_name);
            assert(echoed_lease->slot_index == lease.slot_index);
            assert(echoed_lease->slot_size == lease.slot_size);
            assert(echoed_lease->slot_count == lease.slot_count);
            assert(echoed_lease->length == lease.length);
            assert(position == acknowledgement.data.size());
            if (fault == "missing") {
                assert(::shutdown(peer.get(), SHUT_RDWR) == 0);
                return;
            }
            std::string released;
            append_control_shm_header(released, kLocalControlKindResponseReleased);
            append_u64_little_endian(released, fault == "wrong_ticket" ? 2 : 1);
            if (fault == "version4") released[4] = 4;
            if (fault == "trailing") released.push_back('\0');
            if (fault == "partial_deadline") {
                const auto frame_size = static_cast<std::uint32_t>(released.size());
                assert(::send(peer.get(), &frame_size, sizeof(frame_size), 0) == sizeof(frame_size));
                assert(::send(peer.get(), released.data(), 7, 0) == 7);
                std::this_thread::sleep_for(std::chrono::milliseconds(120));
                (void)::send(peer.get(), released.data() + 7, released.size() - 7, 0);
                return;
            }
            if (fault == "descriptor") {
                ControlShmRegion owner(metrics, true);
                owner.write({});
                ControlShmFraming::send(peer.get(), released, owner.descriptor(), 500, metrics);
            } else {
                ControlShmFraming::send(peer.get(), released, -1, 500, metrics);
            }
        } catch (...) {
            peer_failed = true;
        }
    });
    bool rejected = false;
    const auto wait_budget = fault == "partial_deadline" ? std::chrono::milliseconds(40)
                                                           : std::chrono::milliseconds(500);
    try {
        acknowledge_control_shm_response(
            caller.get(), 1, 1, lease,
            std::chrono::steady_clock::now() + wait_budget, metrics);
        rejected = false;
    } catch (const std::exception&) {
        rejected = true;
    }
    peer_thread.join();
    assert(!peer_failed);
    assert(rejected == (fault != "valid"));
}

class ScopedControlFrameSendProbe {
public:
    explicit ScopedControlFrameSendProbe(ControlFrameSendProbe& probe) {
        assert(control_frame_send_probe == nullptr);
        control_frame_send_probe = &probe;
    }
    ~ScopedControlFrameSendProbe() { control_frame_send_probe = nullptr; }
};

void exercise_frame_send_boundaries() {
    // Zero is the unconstrained one-call steady path. Other caps split the
    // prefix after byte 1/2/3, exactly after byte 4, and inside the body.
    for (const int first_write : {0, 1, 2, 3, 4, 5}) {
        for (const bool attach_descriptor : {false, true}) {
            ControlShmMetrics metrics;
            int sockets[2];
            assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
            ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
            ControlShmRegion owner(metrics, true), reader(metrics, false);
            const std::vector<std::uint8_t> payload(64, 0x59);
            owner.write(payload);
            std::string body;
            append_control_shm_header(body, kLocalControlKindHandshakeResp);
            body.append("metadata-boundary-check");
            ControlFrameSendProbe probe;
            probe.body = body;
            probe.descriptor = attach_descriptor ? owner.descriptor() : -1;
            if (first_write) {
                // Retry before and after the first real positive write. Rights
                // must survive the former and must never recur after the latter.
                probe.steps = {-EINTR, -EAGAIN, first_write, -EINTR, -EAGAIN, 1, 1, 1, 2, 3};
            }
            {
                ScopedControlFrameSendProbe scoped_probe(probe);
                ControlShmFraming::send_view_until(sender.get(), body, probe.descriptor,
                    std::chrono::steady_clock::now() + std::chrono::seconds(1), metrics);
            }
            assert(probe.sent == sizeof(std::uint32_t) + body.size());
            if (first_write) assert(probe.partial_writes > 0);
            else assert(probe.attempts == 1 && probe.positive_writes == 1);
            assert(metrics.fd_sent == (attach_descriptor ? 1 : 0));
            // Queue a second frame before reading to catch cursor overrun and
            // preserve the receiver's exact-prefix/exact-body stream contract.
            std::string next_body;
            append_control_shm_header(next_body, kLocalControlKindHandshakeResp);
            ControlShmFraming::send(sender.get(), next_body, -1, 1000, metrics);
            std::array<char, 128> received{};
            std::size_t received_size = 0;
            auto descriptor = ControlShmFraming::receive_into_until(receiver.get(), received.data(),
                received.size(), received_size, std::chrono::steady_clock::now() + std::chrono::seconds(1), metrics);
            assert(std::string_view(received.data(), received_size) == std::string_view(body));
            assert((descriptor.get() >= 0) == attach_descriptor);
            if (attach_descriptor) {
                assert(::fcntl(descriptor.get(), F_GETFD) & FD_CLOEXEC);
                assert(reader.read(owner.lease(payload.size()), std::move(descriptor)) == payload);
            }
            const auto next_frame = ControlShmFraming::receive(receiver.get(), 1000, metrics);
            assert(next_frame.data == next_body && next_frame.descriptor.get() < 0);
            assert(metrics.fd_received == (attach_descriptor ? 1 : 0));
        }
    }
}

void exercise_frame_send_backpressure() {
    for (const bool attach_descriptor : {false, true}) {
        ControlShmMetrics metrics;
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
        const int send_buffer_size = 4096;
        assert(::setsockopt(sender.get(), SOL_SOCKET, SO_SNDBUF,
                           &send_buffer_size, sizeof(send_buffer_size)) == 0);
        ControlShmRegion owner(metrics, true), reader(metrics, false);
        const std::vector<std::uint8_t> payload(64, 0x6c);
        owner.write(payload);
        std::string body(kControlShmMetadataLimit, 'b');
        std::atomic<bool> backpressure_observed{false}, writer_finished{false}, receiver_failed{false};
        ControlShmFrame frame;
        // The reader starts draining only after the real kernel has rejected a
        // write with EAGAIN, guaranteeing that poll/retry is exercised.
        std::thread receiver_thread([&] {
            while (!backpressure_observed && !writer_finished) std::this_thread::yield();
            try { frame = ControlShmFraming::receive(receiver.get(), 2000, metrics); }
            catch (...) { receiver_failed = true; }
        });
        ControlFrameSendProbe probe;
        probe.body = body;
        probe.descriptor = attach_descriptor ? owner.descriptor() : -1;
        probe.backpressure_observed = &backpressure_observed;
        bool writer_failed = false;
        try {
            ScopedControlFrameSendProbe scoped_probe(probe);
            ControlShmFraming::send(sender.get(), body, probe.descriptor, 2000, metrics);
        } catch (...) { writer_failed = true; }
        writer_finished = true;
        receiver_thread.join();
        assert(!writer_failed && !receiver_failed);
        assert(probe.kernel_backpressure > 0 && probe.partial_writes > 0);
        assert(probe.sent == sizeof(std::uint32_t) + body.size());
        assert(frame.data == body && (frame.descriptor.get() >= 0) == attach_descriptor);
        if (attach_descriptor) assert(reader.read(owner.lease(payload.size()), std::move(frame.descriptor)) == payload);
        assert(metrics.fd_sent == (attach_descriptor ? 1 : 0));
        assert(metrics.fd_received == (attach_descriptor ? 1 : 0));
    }
}

void exercise_frame_send_deadline() {
    // A successful final write after the deadline still fails; the fd metric
    // reflects actual transfer, and the complete late frame exists at the peer.
    ControlShmMetrics metrics;
    int sockets[2];
    assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
    ControlShmRegion owner(metrics, true);
    owner.write({});
    std::string body;
    append_control_shm_header(body, kLocalControlKindHandshakeResp);
    ControlFrameSendProbe probe;
    probe.body = body;
    probe.descriptor = owner.descriptor();
    probe.positive_delay = std::chrono::milliseconds(250);
    {
        ScopedControlFrameSendProbe scoped_probe(probe);
        must_reject([&] { ControlShmFraming::send_view_until(sender.get(), body, probe.descriptor,
            std::chrono::steady_clock::now() + std::chrono::milliseconds(200), metrics); });
    }
    assert(probe.attempts == 1 && probe.sent == sizeof(std::uint32_t) + body.size());
    assert(metrics.fd_sent == 1);
    auto frame = ControlShmFraming::receive(receiver.get(), 1000, metrics);
    assert(frame.data == body && frame.descriptor.get() >= 0 && metrics.fd_received == 1);
}

int main() {
    const auto descriptors_before = descriptor_count();
    ControlShmMetrics metrics;
    metrics.limits.validate();
    exercise_frame_send_boundaries();
    exercise_frame_send_backpressure();
    exercise_frame_send_deadline();
    assert(descriptor_count() == descriptors_before);
    {
        ControlShmRegion owner(metrics, true), reader(metrics, false);
        std::string first_token;
        for (const auto size : {0U, 64U, 1024U, 4096U, 65536U, 1048576U, 64U}) {
            std::vector<std::uint8_t> payload(size, 0x6a);
            const bool changed = owner.write(payload);
            auto lease = owner.lease(size);
            if (first_token.empty()) { first_token = lease.region_name; assert(owner.capacity() == 4096); }
            auto actual = reader.read(lease, ControlShmDescriptor(changed ? ::dup(owner.descriptor()) : -1));
            assert(actual == payload);
            assert(owner.capacity() <= metrics.limits.max_region_bytes);
            assert(reader.capacity() == owner.capacity());
        }
        assert(owner.capacity() >= 1048576 + kPayloadOffset);
        assert(owner.capacity() < 2 * 1048576);
        auto lease = owner.lease(64);
        must_reject([&] { reader.read(lease, ControlShmDescriptor(::dup(owner.descriptor()))); });
        auto wrong = lease;
        wrong.length = 63;
        must_reject([&] { reader.read(wrong, ControlShmDescriptor{}); });
        wrong = lease;
        ++wrong.slot_size;
        must_reject([&] { reader.read(wrong, ControlShmDescriptor{}); });
        wrong = lease;
        wrong.region_name += "new";
        must_reject([&] { reader.read(wrong, ControlShmDescriptor{}); });
        ControlShmRegion new_reader(metrics, false);
        wrong = lease;
        --wrong.slot_size;
        must_reject([&] { new_reader.read(wrong, ControlShmDescriptor(::dup(owner.descriptor()))); });
        wrong = lease;
        wrong.slot_count = 2;
        must_reject([&] { new_reader.read(wrong, ControlShmDescriptor(::dup(owner.descriptor()))); });
#if defined(__linux__)
        assert(::ftruncate(owner.descriptor(), 1) == -1 && errno == EPERM);
        assert(::ftruncate(owner.descriptor(), owner.capacity() + 1) == -1 && errno == EPERM);
#endif
        void* writable = ::mmap(nullptr, owner.capacity(), PROT_READ | PROT_WRITE, MAP_SHARED, owner.descriptor(), 0);
        assert(writable != MAP_FAILED);
        static_cast<std::uint8_t*>(writable)[0] = 2;
        must_reject([&] { reader.read(lease, ControlShmDescriptor{}); });
        static_cast<std::uint8_t*>(writable)[0] = 0;
        assert(::munmap(writable, owner.capacity()) == 0);
        // The peer reference remains valid after owner termination/close, and
        // the kernel releases backing storage after the last peer closes it.
        owner.reset();
        assert(reader.read(lease, ControlShmDescriptor{}) == std::vector<std::uint8_t>(64, 0x6a));
    }
    assert(descriptor_count() == descriptors_before);
    assert(metrics.json().find("\"owned_bytes\":0") != std::string::npos);
    assert(metrics.json().find("\"mapped_bytes\":0") != std::string::npos);
    {
        ControlShmMetrics bounded;
        bounded.limits.max_channels = 2;
        bounded.limits.max_owned_bytes = 8192;
        bounded.limits.max_region_bytes = 8192;
        bounded.limits.max_mapped_bytes = 8192;
        bounded.limits.validate();
        ControlShmConnectionState first(bounded), second(bounded);
        must_reject([&] { ControlShmConnectionState third(bounded); });
        assert(first.outgoing.write(std::vector<std::uint8_t>(64)));
        assert(second.outgoing.write(std::vector<std::uint8_t>(64)));
        must_reject([&] { first.outgoing.write(std::vector<std::uint8_t>(4096)); });
        assert(first.outgoing.capacity() == 4096); // failed growth preserved old mapping
        must_reject([&] { first.outgoing.write(std::vector<std::uint8_t>(8192)); });
        first.incoming.read(first.outgoing.lease(64), ControlShmDescriptor(::dup(first.outgoing.descriptor())));
        second.incoming.read(second.outgoing.lease(64), ControlShmDescriptor(::dup(second.outgoing.descriptor())));
        ControlShmRegion extra_borrower(bounded, false);
        must_reject([&] {
            extra_borrower.read(second.outgoing.lease(64), ControlShmDescriptor(::dup(second.outgoing.descriptor())));
        });
        const auto capacity_errors = bounded.capacity_errors.load();
        try { first.outgoing.write(std::vector<std::uint8_t>(8192)); }
        catch (const std::exception& error) { bounded.record_error(error); }
        assert(bounded.capacity_errors == capacity_errors + 1);
        assert(bounded.io_errors == 0);
        assert(bounded.protocol_errors == 0);
    }
    assert(descriptor_count() == descriptors_before);
    {
        ControlShmMetrics classified;
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
        const std::uint32_t invalid_frame_size = 5;
        assert(::send(sender.get(), &invalid_frame_size, sizeof(invalid_frame_size), 0) == sizeof(invalid_frame_size));
        try {
            ControlShmFraming::receive(receiver.get(), 100, classified);
            assert(false && "malformed frame must be rejected");
        } catch (const ControlShmProtocolError& error) {
            classified.record_error(error);
        }
        assert(classified.protocol_errors == 1);
        assert(classified.io_errors == 0);

        const auto missing_endpoint = std::string("/tmp/r2kernel-control-shm-missing-") + std::to_string(::getpid()) + ".sock";
        try {
            (void)ControlShmFraming::connect(missing_endpoint, 100);
            assert(false && "missing socket endpoint must fail");
        } catch (const ControlShmProtocolError&) {
            assert(false && "connect syscall failure is not a protocol error");
        } catch (const std::exception& error) {
            classified.record_error(error);
        }
        assert(classified.protocol_errors == 1);
        assert(classified.io_errors == 1);
    }
    {
        ControlShmClientChannel channel(metrics);
        channel.state.outgoing.write(std::vector<std::uint8_t>(64));
        channel.invalidate();
        assert(channel.invalidated);
        assert(channel.state.outgoing.capacity() == 0);
        assert(channel.state.incoming.capacity() == 0);
    }
    assert(descriptor_count() == descriptors_before);
    for (const auto& fault : {"valid", "wrong_ticket", "trailing", "descriptor", "missing", "partial_deadline", "version4"})
        exercise_response_ack_peer(fault);
    assert(descriptor_count() == descriptors_before);
    {
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
        ControlShmRegion owner(metrics, true), reader(metrics, false);
        std::vector<std::uint8_t> payload(100, 9);
        owner.write(payload);
        std::string body;
        append_control_shm_header(body, kLocalControlKindTransactResp);
        append_u8(body, 1); append_u16(body, 0); append_i64(body, 55); append_string(body, "");
        append_payload_lease_binary(body, owner.lease(payload.size()));
        append_u64_little_endian(body, 1);
        assert(static_cast<std::uint8_t>(body[4]) == 5);
        assert(static_cast<std::uint8_t>(body[5]) == 4); // Response kind is unchanged.
        auto version4_body = body;
        version4_body[4] = 4;
        const auto mapping_opens_before = metrics.mapping_opens.load();
        const auto payload_reads_before = metrics.payload_reads.load();
        std::uint64_t rejected_ticket = 0;
        LocalPayloadLease rejected_lease;
        ControlShmFraming::send(sender.get(), version4_body, owner.descriptor(), 100, metrics);
        auto version4_frame = ControlShmFraming::receive(receiver.get(), 100, metrics);
        must_reject([&] {
            decode_control_shm_response(std::move(version4_frame), reader, &rejected_ticket, &rejected_lease);
        });
        assert(rejected_ticket == 0 && rejected_lease.region_name.empty());
        ControlShmFraming::send(sender.get(), version4_body, owner.descriptor(), 100, metrics);
        version4_frame = ControlShmFraming::receive(receiver.get(), 100, metrics);
        ControlShmPreparedFrame prepared_version4_frame{version4_frame.data, std::move(version4_frame.descriptor)};
        must_reject([&] {
            decode_prepared_control_shm_response(std::move(prepared_version4_frame), reader, rejected_ticket, rejected_lease);
        });
        assert(rejected_ticket == 0 && rejected_lease.region_name.empty());
        assert(reader.capacity() == 0);
        assert(metrics.mapping_opens == mapping_opens_before);
        assert(metrics.payload_reads == payload_reads_before);
        ControlShmFraming::send(sender.get(), body, owner.descriptor(), 100, metrics);
        auto frame = ControlShmFraming::receive(receiver.get(), 100, metrics);
        auto response = decode_control_shm_response(std::move(frame), reader);
        assert(response.payload == payload && response.timestamp_ns == 55 && response.status == 0);
        for (int repeat = 0; repeat < 20; ++repeat) {
            ControlShmFraming::send(sender.get(), body, -1, 100, metrics);
            frame = ControlShmFraming::receive(receiver.get(), 100, metrics);
            assert(decode_control_shm_response(std::move(frame), reader).payload == payload);
        }
        ControlShmFraming::send(sender.get(), body + "trailing", -1, 100, metrics);
        frame = ControlShmFraming::receive(receiver.get(), 100, metrics);
        must_reject([&] { decode_control_shm_response(std::move(frame), reader); });
        std::string inline_body;
        append_control_shm_header(inline_body, kLocalControlKindTransactResp);
        append_u8(inline_body, 1); append_u16(inline_body, 0); append_i64(inline_body, 55); append_string(inline_body, "");
        append_payload_lease_binary(inline_body, std::nullopt); append_bytes(inline_body, payload.data(), payload.size());
        ControlShmFrame inline_frame{inline_body, ControlShmDescriptor{}};
        must_reject([&] { decode_control_shm_response(std::move(inline_frame), reader); });
        must_reject([&] { ControlShmFraming::receive(receiver.get(), 5, metrics); });
        sender.reset();
        bool closed = false;
        try { ControlShmFraming::receive(receiver.get(), 100, metrics); }
        catch (const ControlShmPeerClosed&) { closed = true; }
        assert(closed);
    }
    assert(descriptor_count() == descriptors_before);
    // Malformed ancillary data closes all received duplicates, including extras.
    {
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
        ControlShmRegion owner(metrics, true);
        owner.write({});
        const auto count_before = descriptor_count();
        std::uint32_t frame_size = 6;
        iovec vector{&frame_size, sizeof(frame_size)};
        alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int) * 2)]{};
        msghdr message{};
        message.msg_iov = &vector; message.msg_iovlen = 1;
        message.msg_control = ancillary; message.msg_controllen = sizeof(ancillary);
        auto* header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(sizeof(int) * 2);
        int descriptors[2]{owner.descriptor(), owner.descriptor()};
        std::memcpy(CMSG_DATA(header), descriptors, sizeof(descriptors));
        assert(::sendmsg(sender.get(), &message, 0) == sizeof(frame_size));
        must_reject([&] { ControlShmFraming::receive(receiver.get(), 100, metrics); });
        assert(descriptor_count() == count_before);
    }
    assert(descriptor_count() == descriptors_before);
    // A descriptor on body bytes must not be discarded by a plain recv call.
    {
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        ControlShmDescriptor sender(sockets[0]), receiver(sockets[1]);
        ControlShmRegion owner(metrics, true);
        owner.write({});
        const auto count_before = descriptor_count();
        std::string body;
        append_control_shm_header(body, kLocalControlKindHandshakeResp);
        const std::uint32_t length = body.size();
        assert(::send(sender.get(), &length, sizeof(length), 0) == sizeof(length));
        iovec vector{body.data(), body.size()};
        alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int))]{};
        msghdr message{};
        message.msg_iov = &vector; message.msg_iovlen = 1;
        message.msg_control = ancillary; message.msg_controllen = sizeof(ancillary);
        auto* header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(sizeof(int));
        const int descriptor = owner.descriptor();
        std::memcpy(CMSG_DATA(header), &descriptor, sizeof(descriptor));
        assert(::sendmsg(sender.get(), &message, 0) == static_cast<ssize_t>(body.size()));
        must_reject([&] { ControlShmFraming::receive(receiver.get(), 100, metrics); });
        assert(descriptor_count() == count_before);
    }
    assert(descriptor_count() == descriptors_before);
    {
        std::atomic<bool> failed{false};
        std::vector<std::thread> channels;
        for (unsigned channel_id = 0; channel_id < 16; ++channel_id) {
            channels.emplace_back([channel_id, &metrics, &failed] {
                try {
                    int sockets[2];
                    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0)
                        throw std::runtime_error("socketpair failed");
                    ControlShmDescriptor caller(sockets[0]), provider(sockets[1]);
                    ControlShmConnectionState left(metrics), right(metrics);
                    for (unsigned sequence = 0; sequence < 100; ++sequence) {
                        std::vector<std::uint8_t> payload(1024, static_cast<std::uint8_t>(channel_id));
                        std::memcpy(payload.data(), &sequence, sizeof(sequence));
                        const bool changed = left.outgoing.write(payload);
                        std::string request;
                        append_control_shm_header(request, kLocalControlKindTransactResp);
                        append_u8(request, 1); append_u16(request, 0); append_i64(request, sequence);
                        append_string(request, ""); append_payload_lease_binary(request, left.outgoing.lease(payload.size()));
                        append_u64_little_endian(request, 1);
                        ControlShmFraming::send(caller.get(), request, changed ? left.outgoing.descriptor() : -1, 1000, metrics);
                        auto frame = ControlShmFraming::receive(provider.get(), 1000, metrics);
                        const auto decoded = decode_control_shm_response(std::move(frame), right.incoming);
                        if (decoded.payload != payload || decoded.timestamp_ns != sequence)
                            throw std::runtime_error("Cross-channel/sequence payload corruption");
                    }
                } catch (...) { failed = true; }
            });
        }
        for (auto& channel : channels) channel.join();
        assert(!failed);
        assert(metrics.json().find("\"active_channels\":0") != std::string::npos);
    }
    assert(descriptor_count() == descriptors_before);
#if defined(__linux__)
    {
        int sockets[2];
        assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        const auto child = ::fork();
        assert(child >= 0);
        if (child == 0) {
            ::close(sockets[0]);
            ControlShmRegion owner(metrics, true);
            owner.write(std::vector<std::uint8_t>(1024, 42));
            std::string message;
            append_control_shm_header(message, kLocalControlKindTransactResp);
            append_u8(message, 1); append_u16(message, 0); append_i64(message, 0); append_string(message, "");
            append_payload_lease_binary(message, owner.lease(1024));
            append_u64_little_endian(message, 1);
            ControlShmFraming::send(sockets[1], message, owner.descriptor(), 1000, metrics);
            ::sleep(2); // bounded even if the parent test aborts
            ::_exit(3);
        }
        ::close(sockets[1]);
        ControlShmDescriptor socket(sockets[0]);
        ControlShmRegion reader(metrics, false);
        auto frame = ControlShmFraming::receive(socket.get(), 1000, metrics);
        assert(decode_control_shm_response(std::move(frame), reader).payload == std::vector<std::uint8_t>(1024, 42));
        assert(::kill(child, SIGKILL) == 0);
        int status = 0;
        assert(::waitpid(child, &status, 0) == child);
        assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
        bool closed = false;
        try { ControlShmFraming::receive(socket.get(), 1000, metrics); }
        catch (const ControlShmPeerClosed&) { closed = true; }
        assert(closed);
        reader.reset(); // last kernel reference released, no owner destructor needed
    }
    assert(descriptor_count() == descriptors_before);
#endif
    std::cout << "C++ production Control SHM tests passed\n";
}
