// Actual Unix sockets, descriptors, mappings and the production lane owner.
#include "../src/core/client.cpp"
#include "control_shm_peer_helpers.hpp"
#include <cassert>
#include <future>
#include <dirent.h>
using namespace r2kernel::core;
using r2kernel::wrapper::ByteBuffer;
using r2kernel::wrapper::ControlResponse;
namespace {
using Deadline = ControlShmFraming::Deadline;
Deadline budget(int milliseconds = 1000) {
    return std::chrono::steady_clock::now() + std::chrono::milliseconds(milliseconds);
}
template<class Function> void rejects(Function&& function) {
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
class SocketPeer {
public:
    explicit SocketPeer(std::string fault, int calls = 1) : fault_(std::move(fault)), calls_(calls) {
        static std::atomic<unsigned> sequence{0};
        endpoint = "/tmp/r2k-result-" + std::to_string(::getpid()) + "-" + std::to_string(++sequence) + ".sock";
        listener_.reset(::socket(AF_UNIX, SOCK_STREAM, 0));
        assert(listener_.get() >= 0);
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1);
        assert(::bind(listener_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
        assert(::listen(listener_.get(), 1) == 0);
        owner_ = std::thread([this] {
            try { run(); } catch (...) { failure_ = std::current_exception(); }
        });
    }
    ~SocketPeer() {
        release();
        if (owner_.joinable()) owner_.join();
        ::unlink(endpoint.c_str());
    }
    void release() {
        std::lock_guard<std::mutex> guard(mutex_);
        release_ = true;
        changed_.notify_all();
    }
    void wait_consumed() {
        std::unique_lock<std::mutex> guard(mutex_);
        assert(changed_.wait_for(guard, std::chrono::seconds(2), [this] { return consumed_; }));
    }
    void finish() {
        if (owner_.joinable()) owner_.join();
        if (failure_) std::rethrow_exception(failure_);
    }
    std::string endpoint;
    ByteBuffer expected_payload{0, 7, 128, 255};
private:
    void run() {
        ControlShmDescriptor socket(::accept(listener_.get(), nullptr, nullptr));
        assert(socket.get() >= 0);
        assert(::fcntl(socket.get(), F_SETFL, O_NONBLOCK) == 0);
        ControlShmRegion incoming(metrics_, false), outgoing(metrics_, true);
        for (int call = 1; call <= calls_; ++call) {
            auto request = ControlShmFraming::receive(socket.get(), 2000, metrics_);
            assert(is_control_shm_frame(request.data, kLocalControlKindTransactReq));
            std::size_t position = 6;
            const auto request_lease = read_control_shm_lease(request.data, position);
            assert(incoming.read(request_lease, std::move(request.descriptor)) == expected_payload);
            const bool changed = outgoing.write(expected_payload);
            const auto lease = outgoing.lease(expected_payload.size());
            std::string response;
            append_control_shm_header(response, kLocalControlKindTransactResp);
            append_u8(response, 1); append_u16(response, 0); append_i64(response, 55); append_string(response, "");
            append_payload_lease_binary(response, lease);
            append_u64_little_endian(response, call);
            ControlShmFraming::send(socket.get(), response, changed ? outgoing.descriptor() : -1, 1000, metrics_);
            auto consumed = ControlShmFraming::receive(socket.get(), 1000, metrics_);
            assert(consumed.descriptor.get() < 0);
            assert(is_control_shm_frame(consumed.data, kLocalControlKindResponseConsumed));
            position = 6;
            const auto ticket = read_u64_little_endian(consumed.data, position);
            const auto consumed_lease = read_control_shm_lease(consumed.data, position);
            assert(ticket && *ticket == static_cast<std::uint64_t>(call));
            assert(consumed_lease.region_name == lease.region_name && consumed_lease.length == lease.length
                && consumed_lease.slot_size == lease.slot_size && consumed_lease.slot_count == lease.slot_count
                && consumed_lease.slot_index == lease.slot_index);
            {
                std::unique_lock<std::mutex> guard(mutex_);
                consumed_ = true;
                changed_.notify_all();
                if (fault_ == "delayed") changed_.wait(guard, [this] { return release_; });
            }
            if (fault_ == "missing" || fault_ == "shutdown") {
                try { (void)ControlShmFraming::receive(socket.get(), 2000, metrics_); assert(false); }
                catch (const ControlShmPeerClosed&) {}
                return;
            }
            // Overwrite peer memory after CONSUMED: the returned vector owns its bytes.
            outgoing.write(ByteBuffer(expected_payload.size(), 99));
            std::string released;
            append_control_shm_header(released, kLocalControlKindResponseReleased);
            append_u64_little_endian(released, fault_ == "wrong_ticket" ? call + 1 : call);
            if (fault_ == "trailing") released.push_back('\0');
            ControlShmFraming::send(socket.get(), released, fault_ == "descriptor" ? outgoing.descriptor() : -1, 1000, metrics_);
            if (fault_ == "duplicate") {
                ControlShmFraming::send(socket.get(), released, -1, 1000, metrics_);
                (void)ControlShmFraming::receive(socket.get(), 1000, metrics_);
                return;
            }
        }
    }
    ControlShmMetrics metrics_;
    ControlShmDescriptor listener_;
    std::string fault_;
    int calls_;
    std::thread owner_;
    std::mutex mutex_;
    std::condition_variable changed_;
    bool consumed_ = false, release_ = false;
    std::exception_ptr failure_;
};
ControlResponse result(ControlShmClientChannel& lane, ControlShmMetrics& metrics,
    SocketPeer& peer, Deadline deadline) {
    ControlShmDurationScope response_timing(metrics.response_ready, metrics.timing_enabled,
        std::chrono::steady_clock::now(), false);
    lane.wait_reclaimed(deadline);
    std::unique_lock<std::mutex> operation(lane.operation_mutex, std::try_to_lock);
    assert(operation.owns_lock());
    lane.require_reclaimed();
    lane.prepare_memory();
    lane.ensure_reclaimer(deadline);
    const bool changed = lane.state.outgoing.write(peer.expected_payload);
    ControlShmMetadataWriter request(lane.prepared.buffer(0));
    request.header(kLocalControlKindTransactReq);
    request.lease(lane.state.outgoing.token(), lane.state.outgoing.capacity(), peer.expected_payload.size());
    auto frame = lane.exchange_prepared(peer.endpoint, request.view(), changed ? lane.state.outgoing.descriptor() : -1, deadline);
    std::uint64_t ticket = 0;
    auto response = decode_prepared_control_shm_response(std::move(frame), lane.state.incoming, ticket, lane.response_lease);
    if (metrics.timing_enabled) {
        const auto ready = std::chrono::steady_clock::now();
        lane.mark_response_ready(ready);
        response_timing.finish(ready);
    }
    lane.consume_response(ticket, lane.response_lease, deadline);
    lane.reclaim_response(ticket, deadline);
    return response;
}
void delayed_and_independent_lanes() {
    ControlShmMetrics metrics;
    SocketPeer delayed("delayed"), immediate("valid", 16);
    ControlShmClientChannel slow(metrics), fast(metrics);
    const auto owned = result(slow, metrics, delayed, budget());
    delayed.wait_consumed();
    assert(owned.payload == delayed.expected_payload);
    rejects([&] { slow.wait_reclaimed(budget(20)); });
    // The barrier fails before a subsequent caller can mutate outgoing memory.
    const auto original_lease = slow.state.outgoing.lease(delayed.expected_payload.size());
    ControlShmRegion observer(metrics, false);
    assert(observer.read(original_lease, ControlShmDescriptor(::dup(slow.state.outgoing.descriptor()))) == delayed.expected_payload);
    for (int call = 0; call < 16; ++call) {
        const auto other = result(fast, metrics, immediate, budget());
        assert(other.payload == immediate.expected_payload);
        fast.wait_reclaimed(budget());
    }
    assert(observer.read(original_lease, ControlShmDescriptor{}) == delayed.expected_payload);
    delayed.release();
    slow.wait_reclaimed(budget());
    assert(owned.payload == delayed.expected_payload);
    assert(slow.next_response_ticket == 2 && fast.next_response_ticket == 17);
    delayed.finish(); immediate.finish();
}
void stale_barrier_observation_rejected() {
    ControlShmMetrics metrics;
    SocketPeer peer("delayed");
    ControlShmClientChannel lane(metrics);
    // Caller B observes no pending work, then is deliberately suspended before
    // acquiring the foreground mutex. Caller A publishes pending work meanwhile.
    std::promise<void> observed, resume;
    auto resume_future = resume.get_future();
    std::atomic<bool> rejected{false};
    std::thread caller_b([&] {
        lane.wait_reclaimed(budget());
        observed.set_value();
        resume_future.wait();
        std::unique_lock<std::mutex> operation(lane.operation_mutex, std::try_to_lock);
        if (!operation.owns_lock()) { rejected = true; return; }
        try { lane.require_reclaimed(); }
        catch (const std::exception&) { rejected = true; return; }
        lane.state.outgoing.write(ByteBuffer{99});
    });
    observed.get_future().wait();
    const auto owned = result(lane, metrics, peer, budget());
    peer.wait_consumed();
    // This check is deterministic even if the receiver won the operation mutex.
    rejects([&] { lane.require_reclaimed(); });
    resume.set_value();
    caller_b.join();
    assert(rejected);
    const auto lease = lane.state.outgoing.lease(peer.expected_payload.size());
    ControlShmRegion observer(metrics, false);
    assert(observer.read(lease, ControlShmDescriptor(::dup(lane.state.outgoing.descriptor()))) == peer.expected_payload);
    peer.release();
    lane.wait_reclaimed(budget());
    assert(owned.payload == peer.expected_payload);
    peer.finish();
}

void background_fault(const std::string& fault) {
    ControlShmMetrics metrics;
    SocketPeer peer(fault);
    ControlShmClientChannel lane(metrics);
    const auto owned = result(lane, metrics, peer, budget(fault == "missing" ? 200 : 1000));
    assert(owned.payload == peer.expected_payload);
    rejects([&] { lane.wait_reclaimed(budget()); });
    rejects([&] { lane.wait_reclaimed(budget()); }); // Sticky failure, no automatic replay.
    rejects([&] { (void)result(lane, metrics, peer, budget()); });
    assert(lane.invalidated && lane.socket_descriptor() < 0);
    assert(lane.state.outgoing.capacity() == 0 && lane.state.incoming.capacity() == 0);
    assert(owned.payload == peer.expected_payload);
    peer.finish();
}
void duplicate_release() {
    ControlShmMetrics metrics;
    SocketPeer peer("duplicate");
    ControlShmClientChannel lane(metrics);
    const auto owned = result(lane, metrics, peer, budget());
    lane.wait_reclaimed(budget());
    rejects([&] { (void)result(lane, metrics, peer, budget()); });
    { std::lock_guard<std::mutex> operation(lane.operation_mutex); lane.invalidate(); }
    assert(owned.payload == peer.expected_payload);
    peer.finish();
}
void shutdown_pending_owner() {
    ControlShmMetrics metrics;
    SocketPeer peer("shutdown");
    ControlShmClientChannel lane(metrics);
    const auto owned = result(lane, metrics, peer, budget(30000));
    peer.wait_consumed();
    const auto started = std::chrono::steady_clock::now();
    lane.stop();
    assert(std::chrono::steady_clock::now() - started < std::chrono::milliseconds(500));
    rejects([&] { lane.wait_reclaimed(budget()); });
    assert(owned.payload == peer.expected_payload);
    peer.finish();
}
void opt_in_metrics_record_one_ready_and_validated_release_per_ticket() {
    const char* previous = std::getenv("R2KERNEL_CONTROL_SHM_TIMING_METRICS");
    const std::optional<std::string> saved = previous ? std::optional<std::string>(previous) : std::nullopt;
    assert(::setenv("R2KERNEL_CONTROL_SHM_TIMING_METRICS", "1", 1) == 0);
    {
        ControlShmMetrics metrics;
        assert(metrics.timing_enabled);
        assert(metrics.json().find("\"response_ready_max_ns\":null") != std::string::npos);
        SocketPeer peer("valid", 2);
        ControlShmClientChannel lane(metrics);
        for (std::uint64_t ticket = 1; ticket <= 2; ++ticket) {
            const auto owned = result(lane, metrics, peer, budget());
            lane.wait_reclaimed(budget());
            assert(owned.payload == peer.expected_payload);
            assert(metrics.response_ready.count == ticket && metrics.release_receive.count == ticket);
            // An already reclaimed barrier does not count this ticket again.
            lane.wait_reclaimed(budget());
            assert(metrics.release_receive.count == ticket);
        }
        peer.finish();
    }
    {
        ControlShmMetrics metrics;
        SocketPeer peer("wrong_ticket");
        ControlShmClientChannel lane(metrics);
        (void)result(lane, metrics, peer, budget());
        rejects([&] { lane.wait_reclaimed(budget()); });
        rejects([&] { lane.wait_reclaimed(budget()); });
        assert(metrics.response_ready.count == 1 && metrics.release_receive.count == 0);
        peer.finish();
    }
    if (saved) assert(::setenv("R2KERNEL_CONTROL_SHM_TIMING_METRICS", saved->c_str(), 1) == 0);
    else assert(::unsetenv("R2KERNEL_CONTROL_SHM_TIMING_METRICS") == 0);
}
}
int main() {
    // These deliberate malformed peers have no managed broker; authority is
    // exercised separately against the genuine Required resource service.
    assert(::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1) == 0);
    const auto initial_descriptors = descriptor_count();
    delayed_and_independent_lanes();
    stale_barrier_observation_rejected();
    for (const auto& fault : {"missing", "wrong_ticket", "trailing", "descriptor"}) background_fault(fault);
    duplicate_release();
    shutdown_pending_owner();
    opt_in_metrics_record_one_ready_and_validated_release_per_ticket();
    {
        ControlShmMetrics metrics;
        const LocalPayloadLease lease{"unused", 0, 4096, 1, 0};
        rejects([&] { consume_control_shm_response(-1, 2, 1, lease, budget(), metrics); });
        rejects([&] { consume_control_shm_response(-1, UINT64_MAX, UINT64_MAX, lease, budget(), metrics); });
    }
    assert(descriptor_count() == initial_descriptors);
}
