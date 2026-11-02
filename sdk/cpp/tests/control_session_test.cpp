// Public SDK calls against a bounded local protocol counterparty. These tests
// exercise real sockets, SHM and reclamation, not the installed broker's authority.
#include <atomic>
#include <sys/mman.h>
static std::atomic<unsigned> mapping_calls{0};
static void* observed_mmap(void* address, std::size_t length, int protection,
                           int flags, int descriptor, off_t offset) {
    ++mapping_calls;
    return ::mmap(address, length, protection, flags, descriptor, offset);
}
// Holds the caller's receive until the counterparty has answered
// and closed the lane, so the client classifies the failure with the lane
// already closed, deterministically.
#include <sys/socket.h>
#include <chrono>
#include <thread>
static std::atomic<bool> hold_caller_receive{false}, counterparty_closed{false};
static std::thread::id held_caller;
static ssize_t observed_recvmsg(int socket, msghdr* message, int flags) {
    if (hold_caller_receive && std::this_thread::get_id() == held_caller) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!counterparty_closed && std::chrono::steady_clock::now() < deadline) std::this_thread::yield();
    }
    return ::recvmsg(socket, message, flags);
}
#define mmap observed_mmap
#define recvmsg observed_recvmsg
#include "../src/core/client.cpp"
#undef recvmsg
#undef mmap
#include "control_shm_peer_helpers.hpp"
#include <cassert>
#include <filesystem>
using namespace r2kernel::core;
using r2kernel::wrapper::ByteBuffer;
namespace {
using Clock = std::chrono::steady_clock;
using Deadline = ControlShmFraming::Deadline;
Deadline budget() { return Clock::now() + std::chrono::seconds(5); }
template<class Function> void rejects(Function&& function) {
    bool rejected = false;
    try { function(); } catch (const std::exception&) { rejected = true; }
    assert(rejected);
}
class Environment {
public:
    Environment(const char* name, const std::string& value) : name_(name) {
        if (const char* previous = std::getenv(name)) previous_ = previous;
        assert(::setenv(name, value.c_str(), 1) == 0);
    }
    ~Environment() {
        assert((previous_ ? ::setenv(name_, previous_->c_str(), 1) : ::unsetenv(name_)) == 0);
    }
private:
    const char* name_;
    std::optional<std::string> previous_;
};
class SessionPeer {
public:
    SessionPeer() {
        char directory[] = "/tmp/r2k-session-XXXXXX";
        const char* created = ::mkdtemp(directory); assert(created);
        root_ = created;
        endpoint = root_ + "/exchange.sock";
        control_endpoint = root_ + "/control.sock";
        pool_ = "r2k-session-" + std::filesystem::path(root_).filename().string();
        assert(r2kernel_shm_create(pool_.c_str(), 4096, 1, &pool_handle_) == 0 && pool_handle_);
        control_listener_.reset(create_unix_listener(control_endpoint));
        listener_.reset(create_unix_listener(endpoint));
        assert(control_listener_.get() >= 0 && listener_.get() >= 0);
        control_owner_ = std::thread([this] { accept_loop(control_listener_.get(), false); });
        owner_ = std::thread([this] { accept_loop(listener_.get(), true); });
    }
    ~SessionPeer() {
        finish();
        assert(r2kernel_shm_close(pool_handle_) == 0);
        assert(r2kernel_shm_unlink(pool_.c_str()) == 0);
        std::error_code error;
        std::filesystem::remove_all(root_, error); assert(!error);
    }
    // Call only after the SDK client is destroyed and all lanes have closed.
    void finish() {
        stopping_ = true;
        allow_release = true;
        if (owner_.joinable()) owner_.join();
        if (control_owner_.joinable()) control_owner_.join();
        for (auto& worker : workers_) if (worker.joinable()) worker.join();
        if (failure_) std::rethrow_exception(failure_);
    }
    std::string endpoint, control_endpoint;
    std::atomic<unsigned> connections{0}, preparations{0}, callbacks{0}, consumed{0}, json_requests{0};
    std::atomic<int> last_priority{-1};
    std::atomic<int> handshake_delay_ms{0}, handler_delay_ms{0};
    std::atomic<bool> allow_release{true}, corrupt_release{false};
    std::atomic<std::uint64_t> remaining_ns{0};
    // Close the lane once: kDropUnread when the next frame arrived, leaving it
    // unread (the client's receive fails with ECONNRESET), kDropIdle at once
    // (the client's next send fails with EPIPE).
    static constexpr int kDropUnread = 1, kDropIdle = 2;
    std::atomic<int> drop_lane{0};
    std::atomic<bool> lane_dropped{false};
    // Answer the next transaction with a failure (ok=0), then close the lane,
    // as Control does for a transaction failure it reports.
    std::atomic<bool> fail_next_transaction{false};
private:
    void accept_loop(int listener, bool binary) {
        try {
            while (!stopping_) {
                pollfd descriptor{listener, POLLIN, 0};
                const int ready = ::poll(&descriptor, 1, 10);
                if (ready < 0 && errno == EINTR) continue;
                assert(ready >= 0);
                if (ready == 0) continue;
                const int socket = ::accept(listener, nullptr, nullptr); assert(socket >= 0);
                if (binary) {
                    ++connections;
                    // Only this accept owner mutates the worker list.
                    workers_.emplace_back([this, socket] {
                        try { binary_connection(socket); }
                        catch (const ControlShmPeerClosed&) {} // SDK closes invalidated/idle lanes.
                        catch (...) { record_failure(); }
                    });
                } else {
                    json_connection(socket);
                }
            }
        } catch (...) { record_failure(); }
    }
    void record_failure() {
        std::lock_guard<std::mutex> guard(failure_mutex_);
        if (!failure_) failure_ = std::current_exception();
    }
    void json_connection(int descriptor) {
        ControlShmDescriptor socket(descriptor);
        std::string request; char bytes[4096];
        for (;;) {
            const auto count = ::recv(socket.get(), bytes, sizeof(bytes), 0);
            if (count < 0 && errno == EINTR) continue;
            assert(count >= 0);
            if (count == 0) break;
            request.append(bytes, static_cast<std::size_t>(count));
        }
        ++json_requests;
        // Registration supplies the genuine C ABI pool. The raw endpoint needs
        // no schema identity, but prepare still performs its cold manager lookup.
        const std::string response = "{\"res\":\"ok\",\"ok\":true,"
            "\"topic_buffer_pool_name\":\"" + pool_ + "\","
            "\"topic_buffer_pool_slot_size\":4096,\"topic_buffer_pool_slot_count\":1,"
            "\"manager_peer_auth_required\":false,"
            "\"control_local_endpoint\":\"" + endpoint + "\","
            "\"control_local_payload_policy\":\"forwarded_shm_v5\","
            "\"control_local_protocol\":\"binary_shm_v5\","
            "\"control_shm_min_region_bytes\":4096,"
            "\"control_shared_buffer_slot_size\":16777216,"
            "\"control_shm_max_channels\":128,"
            "\"control_shm_max_owned_bytes\":67108864,"
            "\"control_shm_max_mapped_bytes\":67108864}";
        std::size_t sent = 0;
        while (sent != response.size()) {
            const auto count = ::send(socket.get(), response.data() + sent, response.size() - sent, 0);
            if (count < 0 && errno == EINTR) continue;
            assert(count > 0); sent += static_cast<std::size_t>(count);
        }
    }
    static bool closed(int socket) {
        char byte;
        const auto count = ::recv(socket, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
        if (count < 0) assert(errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR);
        return count == 0;
    }
    void binary_connection(int descriptor) {
        ControlShmDescriptor socket(descriptor);
        ControlShmConnectionState lane(metrics_);
        std::uint64_t ticket = 1;
        bool prepared = false;
        for (;;) {
            // Wait for the next frame (bounded like its receive), noticing a drop request meanwhile.
            const auto idle_deadline = budget();
            pollfd readable{socket.get(), POLLIN, 0};
            while (drop_lane != kDropIdle && Clock::now() < idle_deadline && ::poll(&readable, 1, 5) <= 0) {}
            if (drop_lane.exchange(0) != 0) {
                lane_dropped = true; // A frame that arrived stays unread: the client's receive gets ECONNRESET.
                return;
            }
            auto frame = ControlShmFraming::receive_until(socket.get(), budget(), metrics_);
            ControlShmMetadataReader reader(frame.data);
            if (reader.kind(kLocalControlKindHandshakeReq)) {
                reader.text(); reader.text(); reader.scalar<std::uint16_t>(); reader.scalar<std::uint32_t>(); reader.end();
                assert(frame.descriptor.get() < 0);
                std::this_thread::sleep_for(std::chrono::milliseconds(handshake_delay_ms.load()));
                if (closed(socket.get())) return;
                std::string response; append_control_shm_header(response, kLocalControlKindHandshakeResp);
                append_u8(response, 1); append_string(response, "");
                ControlShmFraming::send_until(socket.get(), response, -1, budget(), metrics_);
                return;
            }
            const bool preparation = reader.kind(kLocalControlKindPrepareReq);
            assert(preparation || reader.kind(kLocalControlKindTransactReq));
            reader.text(); reader.text(); reader.scalar<std::uint16_t>(); reader.text();
            if (preparation) {
                reader.scalar<std::uint32_t>(); // Remaining preparation timeout.
                const auto request_capacity = reader.scalar<std::uint32_t>();
                const auto response_capacity = reader.scalar<std::uint32_t>();
                LocalPayloadLease lease; reader.lease(lease); reader.end();
                lane.incoming.bind_prepared(lease, std::move(frame.descriptor), request_capacity);
                lane.outgoing.reserve_payload(response_capacity);
                std::string response; append_control_shm_header(response, kLocalControlKindPrepareResp);
                append_u8(response, 1); append_string(response, "");
                append_payload_lease_binary(response, lane.outgoing.lease(0));
                // Count before replying: the client may observe the reply first.
                prepared = true; ++preparations;
                ControlShmFraming::send_until(socket.get(), response, lane.outgoing.descriptor(), budget(), metrics_);
                continue;
            }
            reader.scalar<std::int64_t>(); reader.scalar<float>(); reader.scalar<float>();
            reader.scalar<std::uint32_t>(); last_priority = reader.scalar<std::uint8_t>();
            remaining_ns = reader.ticket();
            LocalPayloadLease lease; reader.lease(lease); reader.end();
            auto payload = lane.incoming.read(lease, std::move(frame.descriptor));
            if (fail_next_transaction.exchange(false)) {
                std::string failed; append_control_shm_header(failed, kLocalControlKindTransactResp);
                append_u8(failed, 0); append_u16(failed, 0); append_i64(failed, 0);
                append_string(failed, "Control-reported transaction failure for the test");
                append_u8(failed, 0); append_u32(failed, 0);
                ControlShmFraming::send_until(socket.get(), failed, -1, budget(), metrics_);
                socket.reset();
                counterparty_closed = true;
                return;
            }
            const auto callback = ++callbacks;
            std::this_thread::sleep_for(std::chrono::milliseconds(handler_delay_ms.load()));
            if (closed(socket.get())) return; // Caller deadline expired; never replay its effect.
            const bool changed = lane.outgoing.write(payload);
            std::string response; append_control_shm_header(response, kLocalControlKindTransactResp);
            append_u8(response, 1); append_u16(response, 0); append_i64(response, callback); append_string(response, "");
            append_payload_lease_binary(response, lane.outgoing.lease(payload.size()));
            append_u64_little_endian(response, ticket);
            ControlShmFraming::send_until(socket.get(), response, (!prepared && changed) ? lane.outgoing.descriptor() : -1,
                budget(), metrics_);
            auto acknowledgement = ControlShmFraming::receive_until(socket.get(), budget(), metrics_);
            ControlShmMetadataReader consumed_reader(acknowledgement.data);
            assert(consumed_reader.kind(kLocalControlKindResponseConsumed));
            assert(consumed_reader.ticket() == ticket && acknowledgement.descriptor.get() < 0);
            consumed_reader.lease(lease); consumed_reader.end(); ++consumed;
            while (!allow_release && !stopping_) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            std::string released; append_control_shm_header(released, kLocalControlKindResponseReleased);
            append_u64_little_endian(released, corrupt_release ? ticket + 1 : ticket);
            ControlShmFraming::send_until(socket.get(), released, -1, budget(), metrics_);
            ++ticket;
        }
    }
    std::string root_, pool_;
    R2KernelShmHandle* pool_handle_ = nullptr;
    ControlShmDescriptor listener_, control_listener_;
    ControlShmMetrics metrics_;
    std::atomic<bool> stopping_{false};
    std::thread owner_, control_owner_;
    std::vector<std::thread> workers_;
    std::mutex failure_mutex_;
    std::exception_ptr failure_;
};
void wait_consumed(SessionPeer& peer) {
    const auto deadline = budget();
    while (peer.consumed == 0 && Clock::now() < deadline) std::this_thread::yield();
    assert(peer.consumed > 0);
}
// bind_control returns the requested port and configure_control_worker takes
// the Python policy names, validating them before any worker starts.
void bind_and_worker_profile_follow_python() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.worker"});
        assert(client.bind_control() == 0 && client.bind_control(41000, "127.0.0.1") == 41000);
        rejects([&] { client.bind_control(-1); });
        rejects([&] { client.configure_control_worker("IDLE", 0, {0}); });
        rejects([&] { client.configure_control_worker("FIFO", 0, {0}); });
        rejects([&] { client.configure_control_worker("sched_other", 0, {}); });
        client.configure_control_worker("sched_other", 0, {0});
        client.configure_control_worker("Other", 0, {0});
        assert(client.control_worker_status().empty());
    }
    peer.finish();
}
// The default transport priority follows the caller's topic, not the
// source-wrapped topic Control routes on.
void default_priority_follows_the_original_topic() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.priority"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "operation.start", 8, 8, 1000, false));
        const ByteBuffer eight(8, 42);
        assert(client.transact_control("provider", "operation.start", eight, 0, 0, 500, 500, -1));
        assert(peer.last_priority == 192);
        assert(client.transact_control("provider", "operation.start", eight, 0, 0, 500, 500, 7));
        assert(peer.last_priority == 7);
    }
    peer.finish();
}

void invalidation_keeps_prepared_contract() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.caller"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        const ByteBuffer eight(8, 42);
        const auto prepared_mappings = mapping_calls.load(), prepared_connections = peer.connections.load();
        const auto first = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(first && first->payload == eight && first->timestamp_ns == 1);
        assert(mapping_calls == prepared_mappings && peer.connections == prepared_connections);
        const auto mappings = mapping_calls.load();
        const auto connections = peer.connections.load(), lookups = peer.json_requests.load();
        client.clear_control_codec_routes();
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1); });
        rejects([&] { client.transact_control("provider", "echo", ByteBuffer(9, 1), 0, 0, 500, 500, -1); });
        rejects([&] { client.transact_control_result("provider", "echo", eight, 0, 0, 500, 500, -1); });
        rejects([&] { client.send_control("provider", "echo", eight, 0, 0, 500, -1); });
        assert(peer.callbacks == 1 && peer.connections == connections && peer.json_requests == lookups);
        assert(mapping_calls == mappings); // No replacement metadata, region or borrowed map admission.
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        auto recovered = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(recovered && recovered->timestamp_ns == 2 && recovered->payload == eight);
        rejects([&] { client.transact_control("provider", "echo", ByteBuffer(9, 1), 0, 0, 500, 500, -1); });
        rejects([&] { client.transact_control_result("provider", "echo", eight, 0, 0, 500, 500, -1); });
        assert(peer.callbacks == 2 && peer.preparations == 2);
        client.clear_control_codec_routes();
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 9, 9, 1000, true));
        auto rebound = client.transact_control_result("provider", "echo", ByteBuffer(9, 7), 0, 0, 500, 500, -1);
        assert(rebound && rebound->timestamp_ns == 3 && rebound->payload == ByteBuffer(9, 7));
        client.wait_control_reclaimed("provider", "echo", 500);
    }
    peer.finish();
}
void invalidation_preserves_pending_and_sticky_failures() {
    SessionPeer peer;
    peer.allow_release = false; peer.corrupt_release = true;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.pending"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, true));
        const auto response = client.transact_control_result("provider", "echo", {7}, 0, 0, 1000, 1000, -1);
        assert(response && response->payload == ByteBuffer{7});
        wait_consumed(peer);
        client.clear_control_codec_routes();
        const auto mappings = mapping_calls.load(), connections = peer.connections.load();
        rejects([&] { client.prepare_control("provider", "echo", 8, 8, 100, true); });
        rejects([&] { client.connect_control("provider", "127.0.0.1", 1); });
        rejects([&] { client.transact_control("provider", "echo", {7}, 0, 0, 100, 100, -1); });
        peer.allow_release = true;
        rejects([&] { client.wait_control_reclaimed("provider", "echo", 1000); });
        rejects([&] { client.prepare_control("provider", "echo", 8, 8, 100, true); });
        rejects([&] { client.connect_control("provider", "127.0.0.1", 1); });
        rejects([&] { client.transact_control("provider", "echo", {7}, 0, 0, 100, 100, -1); });
        assert(mapping_calls == mappings && peer.connections == connections && peer.callbacks == 1);
        assert(response->payload == ByteBuffer{7});
    }
    peer.finish();
}
void split_deadline(bool warm, int handshake_ms, int handler_ms, float deadline_ms, bool success) {
    SessionPeer peer;
    peer.handshake_delay_ms = handshake_ms; peer.handler_delay_ms = handler_ms;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.split"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        if (warm) assert(client.handshake_control("provider", 1000));
        const auto start = Clock::now();
        const bool sent = client.send_control("provider", "echo", {9}, 123, 0, deadline_ms, -1);
        const auto elapsed = Clock::now() - start;
        assert(sent == success);
        if (success) {
            // recv is a take of the already owned response, not a new I/O budget.
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            const auto response = client.recv_control("provider", 1);
            assert(response && response->payload == ByteBuffer{9} && response->timestamp_ns == 1);
            assert(!client.recv_control("provider", 1000));
        } else {
            assert(elapsed < std::chrono::milliseconds(static_cast<int>(deadline_ms) + 200));
            assert(!client.recv_control("provider", 1000));
            if (handler_ms != 0) {
                assert(peer.callbacks == 1 && peer.remaining_ns < static_cast<std::uint64_t>(deadline_ms * 1000000));
            } else assert(peer.callbacks == 0);
        }
    }
    peer.finish();
    assert(peer.callbacks == (handler_ms == 0 ? 0U : 1U)); // No replay on a deadline failure.
}
// r5v5: one late exchange must not leave the prepared lane dead for the rest of
// the run. The late call still fails (no replay); the lane is re-prepared within
// its preparation timeout and the next exchange succeeds.
void late_exchange_reprepares_lane() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.late"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        const ByteBuffer eight(8, 42);
        auto first = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(first && first->timestamp_ns == 1 && peer.preparations == 1);
        peer.handler_delay_ms = 40;
        const auto late_started = Clock::now();
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 10, 10, -1); });
        // Bounded: Control's close of the failed lane plus one preparation.
        assert(Clock::now() - late_started < std::chrono::milliseconds(1000));
        peer.handler_delay_ms = 0;
        // The re-preparation completes inside the failed call; the peer thread
        // still finishes the late handler and its consumed ACK on its own time.
        assert(peer.preparations == 2);
        const auto settled = budget();
        while ((peer.callbacks != 2 || peer.consumed != 1) && Clock::now() < settled) std::this_thread::yield();
        assert(peer.callbacks == 2 && peer.consumed == 1);
        auto recovered = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(recovered && recovered->timestamp_ns == 3 && recovered->payload == eight);
        assert(peer.preparations == 2 && peer.callbacks == 3); // No replay of the late request.
    }
    peer.finish();
}
// A send or receive that finds the lane closed by Control (EPIPE,
// ECONNRESET) re-prepares the prepared lane like a clean close. The failed call
// still fails and is not replayed; the next exchange succeeds.
void reset_lane_reprepares(int drop) {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.reset"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        const ByteBuffer eight(8, 11);
        auto first = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(first && first->timestamp_ns == 1 && peer.preparations == 1);
        peer.drop_lane = drop;
        if (drop == SessionPeer::kDropIdle) {
            const auto deadline = budget();
            while (!peer.lane_dropped && Clock::now() < deadline) std::this_thread::yield();
            assert(peer.lane_dropped);
        }
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1); });
        assert(peer.lane_dropped && peer.preparations == 2);
        auto recovered = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(recovered && recovered->timestamp_ns == 2 && recovered->payload == eight);
        assert(peer.callbacks == 2 && peer.preparations == 2); // The dropped request never ran.
    }
    peer.finish();
}
// A transaction failure that Control reports in its response (ok=0)
// and follows with closing the lane is not a reset lane: the call fails, the
// lane stays tombstoned and is not re-prepared automatically. An explicit
// preparation recovers it.
void reported_failure_keeps_tombstone() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.reported"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        const ByteBuffer eight(8, 3);
        auto first = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(first && first->timestamp_ns == 1 && peer.preparations == 1);
        const auto connections = peer.connections.load();
        peer.fail_next_transaction = true;
        held_caller = std::this_thread::get_id();
        hold_caller_receive = true;
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1); });
        hold_caller_receive = false;
        assert(counterparty_closed);
        assert(peer.preparations == 1 && peer.connections == connections); // Not re-prepared.
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1); });
        assert(peer.preparations == 1 && peer.connections == connections && peer.callbacks == 1);
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        auto recovered = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(recovered && recovered->timestamp_ns == 2 && recovered->payload == eight);
        assert(peer.preparations == 2);
    }
    peer.finish();
}
// r5v5: a deadline that expires before anything reaches Control fails only that
// exchange; the prepared lane keeps its connection and mappings.
void unpublished_deadline_keeps_lane() {
    SessionPeer peer;
    Environment control("R2KERNEL_CONTROL_SOCKET", peer.control_endpoint);
    {
        R2KernelClient client(ClientOptions{"session.unpublished"});
        assert(client.connect_control("provider", "127.0.0.1", 1));
        assert(client.prepare_control("provider", "echo", 8, 8, 1000, false));
        const ByteBuffer eight(8, 5);
        assert(client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1));
        const auto connections = peer.connections.load(), mappings = mapping_calls.load();
        rejects([&] { client.transact_control("provider", "echo", eight, 0, 0, 1e-6F, 500, -1); });
        auto next = client.transact_control("provider", "echo", eight, 0, 0, 500, 500, -1);
        assert(next && next->timestamp_ns == 2 && next->payload == eight);
        assert(peer.connections == connections && mapping_calls == mappings && peer.preparations == 1);
    }
    peer.finish();
}
} // namespace
int main() {
    Environment authority("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled");
    bind_and_worker_profile_follow_python();
    default_priority_follows_the_original_topic();
    invalidation_keeps_prepared_contract();
    invalidation_preserves_pending_and_sticky_failures();
    late_exchange_reprepares_lane();
    reset_lane_reprepares(SessionPeer::kDropUnread);
    reset_lane_reprepares(SessionPeer::kDropIdle);
    reported_failure_keeps_tombstone();
    unpublished_deadline_keeps_lane();
    split_deadline(true, 0, 1100, 1500, true);
    split_deadline(false, 100, 1100, 1500, true);
    split_deadline(true, 0, 120, 30, false);
    split_deadline(false, 150, 250, 300, false);
    split_deadline(false, 80, 0, 30, false);
    std::cout << "public prepared invalidation, explicit recovery, sticky reclamation and split deadlines passed\n";
}
