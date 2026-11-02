// Actual SDK + sockets/mappings, with poll counting and injected lock failure.
#include <atomic>
#include <cerrno>
#include <chrono>
#include <sys/poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <pthread.h>
#include <sched.h>
#include <thread>
static std::atomic<unsigned> poll_calls{0};
static std::atomic<unsigned> interrupted_polls{0};
static std::atomic<unsigned> early_poll_timeouts{0};
static std::atomic<bool> fail_lock{false};
static std::atomic<unsigned> lock_calls{0};
static std::atomic<unsigned> unlock_calls{0};
static std::atomic<bool> pretend_lock_success{false};
static int observed_munlock(const void* address, std::size_t size) {
    ++unlock_calls;
    return ::munlock(address, size);
}
static std::atomic<int> injected_policy{-1};
static int observed_getschedparam(pthread_t thread, int* policy, sched_param* parameters) {
    const int result = ::pthread_getschedparam(thread, policy, parameters);
    if (result == 0 && injected_policy >= 0) { *policy = injected_policy; parameters->sched_priority = 1; }
    return result;
}
static std::atomic<bool> delay_positive_send{false};
static std::atomic<unsigned> delay_receive_call{0};
static ssize_t observed_sendmsg(int socket, const msghdr* message, int flags) {
    const auto count = ::sendmsg(socket, message, flags);
    if (count > 0 && delay_positive_send.exchange(false))
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
    return count;
}
static ssize_t observed_recvmsg(int socket, msghdr* message, int flags) {
    const auto count = ::recvmsg(socket, message, flags);
    if (count > 0 && delay_receive_call != 0 && --delay_receive_call == 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
    return count;
}
static int observed_poll(pollfd* descriptors, nfds_t count, int timeout) {
    ++poll_calls;
    if (early_poll_timeouts != 0) { --early_poll_timeouts; return 0; }
    if (interrupted_polls.load() != 0) {
        --interrupted_polls;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        errno = EINTR;
        return -1;
    }
    return ::poll(descriptors, count, timeout);
}
#if defined(__linux__)
static int observed_ppoll(pollfd* descriptors, nfds_t count, const timespec* timeout, const sigset_t* mask) {
    ++poll_calls;
    if (early_poll_timeouts != 0) { --early_poll_timeouts; return 0; }
    if (interrupted_polls.load() != 0) {
        --interrupted_polls;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        errno = EINTR;
        return -1;
    }
    return ::ppoll(descriptors, count, timeout, mask);
}
#endif
static int observed_mlock(const void* address, std::size_t size) {
    ++lock_calls;
    if (fail_lock) { errno = EPERM; return -1; }
    if (pretend_lock_success) return 0;
    return ::mlock(address, size);
}
#define pthread_getschedparam observed_getschedparam
#define sendmsg observed_sendmsg
#define recvmsg observed_recvmsg
#define poll observed_poll
#if defined(__linux__)
#define ppoll observed_ppoll
#endif
#define mlock observed_mlock
#define munlock observed_munlock
#include "../src/core/client.cpp"
#undef pthread_getschedparam
#undef sendmsg
#undef recvmsg
#undef munlock
#undef mlock
#undef poll
#if defined(__linux__)
#undef ppoll
#endif
#include "control_shm_peer_helpers.hpp"
#include <cassert>
#include <dirent.h>
#include <future>
using namespace r2kernel::core;
using Deadline = ControlShmFraming::Deadline;
static Deadline budget(int milliseconds = 100) {
    return std::chrono::steady_clock::now() + std::chrono::milliseconds(milliseconds);
}
template<class Function> static void rejects(Function function) {
    bool rejected = false;
    try { function(); } catch (const std::exception&) { rejected = true; }
    assert(rejected);
}
static std::size_t descriptors() {
#if defined(__linux__)
    DIR* directory = ::opendir("/proc/self/fd"); assert(directory);
    std::size_t count = 0;
    while (::readdir(directory)) ++count;
    ::closedir(directory); return count;
#else
    return 0;
#endif
}
static void queued_io_and_expired_deadline() {
    ControlShmMetrics metrics;
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    std::string message; append_control_shm_header(message, kLocalControlKindResponseReleased);
    append_u64_little_endian(message, 1);
    poll_calls = 0;
    ControlShmFraming::send_until(sender.get(), message, -1, budget(), metrics);
    assert(poll_calls == 0);
    auto frame = ControlShmFraming::receive_until(receiver.get(), budget(), metrics);
    assert(frame.data == message && poll_calls == 0);
    ControlShmFraming::send_until(sender.get(), message, -1, budget(), metrics);
    const auto expired = std::chrono::steady_clock::now() - std::chrono::milliseconds(1);
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), expired, metrics); });
    // Expired calls do not consume prequeued bytes or publish a prefix.
    assert(ControlShmFraming::receive_until(receiver.get(), budget(), metrics).data == message);
    rejects([&] { ControlShmFraming::send_until(sender.get(), message, -1, expired, metrics); });
    char byte;
    assert(::recv(receiver.get(), &byte, 1, MSG_DONTWAIT) == -1 && errno == EAGAIN);
    interrupted_polls = 100;
    const auto start = std::chrono::steady_clock::now();
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), budget(8), metrics); });
    assert(std::chrono::steady_clock::now() - start < std::chrono::milliseconds(80));
    assert(interrupted_polls > 0);
    interrupted_polls = 0;
    poll_calls = 0;
    early_poll_timeouts = 5;
    const auto timeout_start = std::chrono::steady_clock::now();
    const auto absolute_timeout = timeout_start + std::chrono::milliseconds(15);
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), absolute_timeout, metrics); });
    assert(std::chrono::steady_clock::now() >= absolute_timeout);
    assert(early_poll_timeouts == 0 && poll_calls >= 6);
}
static void late_positive_io_rejected_and_descriptor_owned() {
    ControlShmMetrics metrics;
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    ControlShmRegion owner(metrics, true); owner.write({1});
    std::string message; append_control_shm_header(message, kLocalControlKindResponseReleased);
    append_u64_little_endian(message, 1);
    delay_positive_send = true;
    rejects([&] { ControlShmFraming::send_until(sender.get(), message, owner.descriptor(), budget(5), metrics); });
    assert(!delay_positive_send);
    // The kernel accepted a complete frame; the final positive send still must
    // reject deadline expiry after the syscall returns to the caller.
    const auto before = descriptors();
    delay_receive_call = 2; // Delay the terminal body receive after its FD prefix.
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), budget(5), metrics); });
    assert(metrics.fd_received == 1 && descriptors() == before);
    ControlShmFraming::send_until(sender.get(), message, owner.descriptor(), budget(), metrics);
    delay_receive_call = 1; // Delay first positive prefix carrying the installed FD.
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), budget(5), metrics); });
    assert(metrics.fd_received == 2 && descriptors() == before);
}
static void partial_prefix_descriptor_cleanup() {
    ControlShmMetrics metrics;
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    ControlShmRegion owner(metrics, true); owner.write({1});
    const std::uint32_t size = 14;
    iovec vector{const_cast<std::uint32_t*>(&size), 1};
    alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int))]{};
    msghdr message{}; message.msg_iov = &vector; message.msg_iovlen = 1;
    message.msg_control = ancillary; message.msg_controllen = sizeof(ancillary);
    auto* header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS; header->cmsg_len = CMSG_LEN(sizeof(int));
    const int descriptor = owner.descriptor(); std::memcpy(CMSG_DATA(header), &descriptor, sizeof(descriptor));
    assert(::sendmsg(sender.get(), &message, 0) == 1);
    const auto before = descriptors();
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), budget(10), metrics); });
    assert(metrics.fd_received == 1 && descriptors() == before);
}
static void backpressure_deadline_and_one_descriptor() {
    ControlShmMetrics metrics;
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    int small_buffer = 1024;
    assert(::setsockopt(sender.get(), SOL_SOCKET, SO_SNDBUF, &small_buffer, sizeof(small_buffer)) == 0);
    ControlShmRegion owner(metrics, true); owner.write({2});
    std::string large(ControlShmPreparedMemory::capacity, 'x');
    poll_calls = 0;
    const auto start = std::chrono::steady_clock::now();
    rejects([&] { ControlShmFraming::send_until(sender.get(), large, owner.descriptor(), budget(15), metrics); });
    assert(poll_calls > 0 && metrics.fd_sent == 1);
    assert(std::chrono::steady_clock::now() - start < std::chrono::milliseconds(100));
    const auto before = descriptors();
    rejects([&] { ControlShmFraming::receive_until(receiver.get(), budget(10), metrics); });
    assert(metrics.fd_received == 1 && descriptors() == before);
}
static void prepared_budget_and_lock_failure() {
    ControlShmMetrics metrics;
    constexpr auto lane_bytes = ControlShmPreparedMemory::capacity * ControlShmPreparedMemory::buffer_count;
    metrics.limits.rt_memory_budget_bytes = lane_bytes;
    {
        ControlShmPreparedMemory first(metrics);
        assert(reinterpret_cast<std::uintptr_t>(first.buffer(0)) % ::sysconf(_SC_PAGESIZE) == 0);
        rejects([&] { ControlShmPreparedMemory second(metrics); });
        fail_lock = true;
        rejects([&] { first.prepare(true); });
        fail_lock = false;
        // An ordinary caller has no required lock even after explicit failure.
        first.prepare(false);
        assert(metrics.json().find("\"prepared_bytes\":" + std::to_string(lane_bytes)) != std::string::npos);
    }
    assert(metrics.json().find("\"prepared_bytes\":0") != std::string::npos);
    { ControlShmPreparedMemory after_release(metrics); }
    {
        ControlShmRegion owner(metrics, true);
        owner.prepare_memory(true);
        fail_lock = true;
        rejects([&] { owner.write({3}); });
        fail_lock = false;
        assert(owner.capacity() == 0);
        assert(metrics.json().find("\"prepared_bytes\":0") != std::string::npos);
        assert(metrics.json().find("\"owned_bytes\":0") != std::string::npos);
    }
    {
        ControlShmRegion owner(metrics, true), borrower(metrics, false);
        owner.write({4});
        borrower.prepare_memory(true);
        fail_lock = true;
        rejects([&] { borrower.read(owner.lease(1), ControlShmDescriptor(::dup(owner.descriptor()))); });
        fail_lock = false;
        assert(borrower.capacity() == 0);
        assert(metrics.json().find("\"prepared_bytes\":0") != std::string::npos);
        assert(metrics.json().find("\"mapped_bytes\":0") != std::string::npos);
    }
    // Failure during lane construction releases the channel admission too.
    metrics.limits.rt_memory_budget_bytes = lane_bytes - 1;
    rejects([&] { ControlShmClientChannel lane(metrics); });
    assert(metrics.json().find("\"active_channels\":0") != std::string::npos);
}
static void prepared_metadata_reuse() {
    ControlShmMetrics metrics;
    ControlShmPreparedMemory prepared(metrics);
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    auto* original = prepared.buffer(1);
    for (std::uint64_t ticket = 1; ticket <= 128; ++ticket) {
        ControlShmMetadataWriter writer(prepared.buffer(0));
        writer.header(kLocalControlKindResponseReleased); writer.ticket(ticket);
        ControlShmFraming::send_view_until(sender.get(), writer.view(), -1, budget(), metrics);
        std::size_t length = 0;
        auto descriptor = ControlShmFraming::receive_into_until(receiver.get(), prepared.buffer(1),
            ControlShmPreparedMemory::capacity, length, budget(), metrics);
        assert(descriptor.get() < 0 && prepared.buffer(1) == original);
        ControlShmMetadataReader reader({original, length});
        assert(reader.kind(kLocalControlKindResponseReleased) && reader.ticket() == ticket);
        reader.end();
    }
    ControlShmMetadataWriter full(prepared.buffer(0));
    full.bytes(std::string(ControlShmPreparedMemory::capacity, 'x'));
    rejects([&] { full.scalar(std::uint8_t{0}); });
}
static void process_budget_across_clients() {
    ControlShmMetrics first, second;
    auto& process = ControlShmProcessMemoryBudget::instance();
    const auto initial = process.snapshot();
    assert(initial[1] == 0);
    first.limits.rt_memory_budget_bytes = initial[0];
    second.limits.rt_memory_budget_bytes = initial[0];
    const auto first_bytes = control_shm_page_rounded_bytes(1);
    const auto usable_cap = (initial[0] / first_bytes) * first_bytes;
    assert(usable_cap >= first_bytes);
    const auto second_bytes = usable_cap;
    first.reserve_prepared_bytes(first_bytes);
    rejects([&] { second.reserve_prepared_bytes(second_bytes); });
    assert(process.snapshot()[1] == first_bytes);
    first.release_prepared_bytes(first_bytes);
    second.reserve_prepared_bytes(usable_cap);
    assert(process.snapshot()[1] == usable_cap);
    second.release_prepared_bytes(usable_cap);
    assert(process.snapshot()[1] == 0);
    // The process admission limit is immutable after initialization.
    const char* old_environment = std::getenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES");
    const std::optional<std::string> saved = old_environment ? std::optional<std::string>(old_environment) : std::nullopt;
    assert(::setenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", "1", 1) == 0);
    assert(process.snapshot()[0] == initial[0]);
    second.reserve_prepared_bytes(usable_cap);
    second.release_prepared_bytes(usable_cap);
    if (saved) assert(::setenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", saved->c_str(), 1) == 0);
    else assert(::unsetenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES") == 0);
}
static void scheduling_promotion_requires_existing_mapping_lock() {
    ControlShmMetrics metrics;
    ControlShmClientChannel lane(metrics);
    lane.state.outgoing.write({5});
    lane.state.incoming.read(lane.state.outgoing.lease(1),
        ControlShmDescriptor(::dup(lane.state.outgoing.descriptor())));
    lock_calls = 0;
    unlock_calls = 0;
    injected_policy = SCHED_OTHER;
    lane.prepare_memory();
    assert(lock_calls == 0); // Injected initial caller is SCHED_OTHER.
    injected_policy = SCHED_RR;
    fail_lock = true;
    rejects([&] { lane.prepare_memory(); });
    assert(lock_calls == 1); // Metadata lock failure aborts promotion explicitly.
    // Check already-created payload mappings also request strict lock after an
    // OTHER -> RR transition; injected failure avoids dependence on host limits.
    rejects([&] { lane.state.outgoing.prepare_memory(true); });
    rejects([&] { lane.state.incoming.prepare_memory(true); });
    assert(lock_calls == 3 && unlock_calls == 3);
    assert(metrics.json().find("\"prepared_bytes\":262144") != std::string::npos);
    fail_lock = false;
    injected_policy = -1;
}
static void unaligned_shm_actual_page_budget_and_release() {
#if defined(__linux__)
    ControlShmMetrics metrics;
    const auto page = control_shm_page_rounded_bytes(1);
    const auto logical_size = page + 1;
    const auto actual_size = page * 2;
    ControlShmDescriptor descriptor(static_cast<int>(::syscall(SYS_memfd_create,
        "unaligned-control-budget-test", MFD_CLOEXEC | MFD_ALLOW_SEALING)));
    assert(descriptor.get() >= 0);
    assert(::ftruncate(descriptor.get(), static_cast<off_t>(logical_size)) == 0);
    assert(::fcntl(descriptor.get(), F_ADD_SEALS, F_SEAL_GROW | F_SEAL_SHRINK) == 0);
    const std::uint32_t length = 1;
    const std::uint8_t payload = 0x55;
    assert(::pwrite(descriptor.get(), &length, sizeof(length), 8) == sizeof(length));
    assert(::pwrite(descriptor.get(), &payload, 1, kPayloadOffset) == 1);
    const LocalPayloadLease lease{"unaligned-budget-test", 0, logical_size, 1, 1};
    metrics.limits.rt_memory_budget_bytes = logical_size;
    {
        ControlShmRegion reader(metrics, false);
        reader.prepare_memory(true);
        rejects([&] { reader.read(lease, ControlShmDescriptor(::dup(descriptor.get()))); });
        assert(reader.capacity() == 0);
        assert(metrics.json().find("\"prepared_bytes\":0") != std::string::npos);
        assert(metrics.json().find("\"mapped_bytes\":0") != std::string::npos);
        metrics.limits.rt_memory_budget_bytes = actual_size;
        pretend_lock_success = true; // Accounting test is independent of host CAP_IPC_LOCK.
        assert(reader.read(lease, ControlShmDescriptor(::dup(descriptor.get()))) == std::vector<std::uint8_t>{payload});
        const auto summary = metrics.json();
        assert(summary.find("\"prepared_bytes\":" + std::to_string(actual_size)) != std::string::npos);
        assert(summary.find("\"mapped_bytes\":" + std::to_string(logical_size)) != std::string::npos);
        assert(ControlShmProcessMemoryBudget::instance().snapshot()[1] == actual_size);
        reader.reset();
        pretend_lock_success = false;
        assert(metrics.json().find("\"prepared_bytes\":0") != std::string::npos);
        assert(metrics.json().find("\"mapped_bytes\":0") != std::string::npos);
        assert(ControlShmProcessMemoryBudget::instance().snapshot()[1] == 0);
    }
    rejects([&] { metrics.reserve_prepared_bytes(std::numeric_limits<std::size_t>::max()); });
    assert(ControlShmProcessMemoryBudget::instance().snapshot()[1] == 0);
#endif
}
static void prepared_session_identity_and_nonowning_cache() {
    ControlShmMetrics metrics;
    ControlShmSessionCache cache;
    const auto first_client = next_control_shm_client_identity();
    const auto second_client = next_control_shm_client_identity();
    assert(first_client != second_client);
    std::atomic<std::uint64_t> exhausted{std::numeric_limits<std::uint64_t>::max() - 1};
    assert(claim_control_shm_client_identity(exhausted) == std::numeric_limits<std::uint64_t>::max() - 1);
    rejects([&] { claim_control_shm_client_identity(exhausted); });
    rejects([&] { claim_control_shm_client_identity(exhausted); });
    assert(exhausted == std::numeric_limits<std::uint64_t>::max());
    auto first = std::make_shared<ControlShmClientChannel>(metrics);
    first->session = {"gateway-a", "interface", "motion", "gateway-a/motion", "host-a", "endpoint-a", 1, 2, 3};
    auto second = std::make_shared<ControlShmClientChannel>(metrics);
    second->session = {"gateway-b", "interface", "motion", "gateway-b/motion", "host-b", "endpoint-b", 2, 2, 3};
    auto nested = std::make_shared<ControlShmClientChannel>(metrics);
    nested->session = {"gateway-a", "interface-b", "motion", "gateway-a/motion", "host-c", "endpoint-a", 3, 2, 3};
    cache.remember(first_client, first);
    cache.remember(second_client, second);
    cache.remember(first_client, nested);
    assert(cache.find(first_client, "interface", "motion", 2, 3) == first);
    assert(cache.find(second_client, "interface", "motion", 2, 3) == second);
    assert(cache.find(first_client, "interface-b", "motion", 2, 3) == nested);
    assert(first->prepared.buffer(0) != nested->prepared.buffer(0));
    assert(!cache.find(first_client, "interface", "other-topic", 2, 3));
    assert(!cache.find(first_client, "interface", "motion", 4, 3));
    assert(!cache.find(first_client, "interface", "motion", 2, 4));
    assert(!cache.find(next_control_shm_client_identity(), "interface", "motion", 2, 3));
    first->invalidate();
    assert(!cache.find(first_client, "interface", "motion", 2, 3));
    assert(cache.find(second_client, "interface", "motion", 2, 3) == second);
    const std::weak_ptr<ControlShmClientChannel> nonowning = second;
    second.reset();
    assert(nonowning.expired());
    assert(!cache.find(second_client, "interface", "motion", 2, 3));
}

static void handler_publications_keep_active_generation_and_management_destruction() {
    ControlHandlerPublication publication;
    std::atomic<bool> destroyed{false};
    std::thread::id destructor_thread;
    struct DestructionProbe {
        std::atomic<bool>& destroyed;
        std::thread::id& thread;
        ~DestructionProbe() { thread = std::this_thread::get_id(); destroyed = true; }
    };
    auto lifetime = std::shared_ptr<DestructionProbe>(new DestructionProbe{destroyed, destructor_thread});
    r2kernel::wrapper::ControlHandler original = [lifetime](r2kernel::wrapper::ControlRequest) {
        return r2kernel::wrapper::ControlResponse{11, 0, {1}};
    };
    publication.publish(original);
    original = {};
    lifetime.reset();
    {
        auto old = publication.acquire();
        publication.publish([](r2kernel::wrapper::ControlRequest) {
            return r2kernel::wrapper::ControlResponse{22, 0, {2}};
        });
        auto current = publication.acquire();
        assert(old({}).timestamp_ns == 11 && current({}).timestamp_ns == 22);
        assert(!destroyed);
    }
    // Even the last packet reader only decrements a pin. Next management
    // publication retires the inactive old closure on its own thread.
    assert(!destroyed);
    publication.publish([](r2kernel::wrapper::ControlRequest) {
        return r2kernel::wrapper::ControlResponse{33, 0, {3}};
    });
    assert(destroyed && destructor_thread == std::this_thread::get_id());
    publication.clear();
    assert(!publication.acquire());
    // A callback may publish another handler; its old generation remains pinned.
    publication.publish([&publication](r2kernel::wrapper::ControlRequest) {
        publication.publish([](r2kernel::wrapper::ControlRequest) {
            return r2kernel::wrapper::ControlResponse{44, 0, {4}};
        });
        return r2kernel::wrapper::ControlResponse{33, 0, {3}};
    });
    { auto old = publication.acquire(); assert(old({}).timestamp_ns == 33); }
    { auto current = publication.acquire(); assert(current({}).timestamp_ns == 44); }
    publication.clear();
}

static void handler_publication_reclaims_only_inactive_generations() {
    ControlHandlerPublication publication;
    std::vector<std::unique_ptr<ControlHandlerPublication::Reader>> readers;
    // Publication count is not a new admission limit. All these active
    // generations remain callable until the readers drain.
    for (std::size_t index = 0; index < 160; ++index) {
        publication.publish([index](r2kernel::wrapper::ControlRequest) {
            return r2kernel::wrapper::ControlResponse{static_cast<std::int64_t>(index), 0, {}};
        });
        readers.emplace_back(new ControlHandlerPublication::Reader(publication.acquire()));
    }
    publication.publish({});
    publication.collect();
    rejects([&] { publication.clear(); });
    assert((*readers.front())({}).timestamp_ns == 0);
    assert((*readers.back())({}).timestamp_ns == 159);
    readers.clear();
    publication.collect();
    publication.clear();
}

static void handler_callable_clones_preserve_mutable_captures_and_inflight_update() {
    ControlHandlerPublication publication;
    publication.publish([counter = 0](r2kernel::wrapper::ControlRequest) mutable {
        return r2kernel::wrapper::ControlResponse{++counter, 0, {}};
    });
    {
        auto handler = publication.acquire();
        for (int request = 0; request < 20; ++request) assert(handler({}).timestamp_ns == 1);
    }
    std::vector<std::thread> lanes;
    for (int lane = 0; lane < 8; ++lane) lanes.emplace_back([&publication] {
        for (int request = 0; request < 20; ++request) {
            auto handler = publication.acquire();
            assert(handler({}).timestamp_ns == 1);
        }
    });
    for (auto& lane : lanes) lane.join();
    std::promise<void> entered, resume;
    auto started = entered.get_future();
    auto release = resume.get_future().share();
    publication.publish([&entered, release](r2kernel::wrapper::ControlRequest) {
        entered.set_value(); release.wait();
        return r2kernel::wrapper::ControlResponse{11, 0, {}};
    });
    auto old_result = std::async(std::launch::async, [&publication] {
        auto handler = publication.acquire(); return handler({});
    });
    assert(started.wait_for(std::chrono::seconds(2)) == std::future_status::ready);
    publication.publish([](r2kernel::wrapper::ControlRequest) {
        return r2kernel::wrapper::ControlResponse{22, 0, {}};
    });
    { auto newest = publication.acquire(); assert(newest({}).timestamp_ns == 22); }
    resume.set_value();
    assert(old_result.get().timestamp_ns == 11);
    publication.collect();
    publication.clear();
}

static void nested_packet_publication_defers_original_destruction() {
    ControlHandlerPublication publication;
    std::atomic<bool> destroyed{false};
    std::thread::id destructor_thread;
    struct Probe {
        std::atomic<bool>& destroyed;
        std::thread::id& thread;
        ~Probe() { thread = std::this_thread::get_id(); destroyed = true; }
    };
    auto lifetime = std::shared_ptr<Probe>(new Probe{destroyed, destructor_thread});
    r2kernel::wrapper::ControlHandler first = [lifetime](r2kernel::wrapper::ControlRequest) {
        return r2kernel::wrapper::ControlResponse{};
    };
    publication.publish(first);
    first = {}; lifetime.reset();
    {
        auto held = publication.acquire();
        publication.publish([&publication](r2kernel::wrapper::ControlRequest) {
            publication.publish([](r2kernel::wrapper::ControlRequest) {
                return r2kernel::wrapper::ControlResponse{};
            });
            publication.collect(); // Packet callbacks cannot reclaim originals.
            return r2kernel::wrapper::ControlResponse{};
        });
    } // The first original is now an inactive retired publication.
    std::thread packet([&publication] { auto callback = publication.acquire(); callback({}); });
    packet.join();
    assert(!destroyed);
    publication.collect();
    assert(destroyed && destructor_thread == std::this_thread::get_id());
    publication.clear();
}

static void bounded_duration_metrics_and_disabled_scope() {
    ControlShmDurationMetrics metrics;
    { ControlShmDurationScope disabled(metrics, false); }
    assert(metrics.count == 0 && metrics.total_ns == 0 && metrics.max_ns == 0);
    metrics.record(std::chrono::nanoseconds(5));
    metrics.record(std::chrono::nanoseconds(3));
    metrics.record(std::chrono::nanoseconds(8));
    assert(metrics.count == 3 && metrics.total_ns == 16 && metrics.max_ns == 8);
    { ControlShmDurationScope enabled(metrics, true); enabled.finish(); enabled.finish(); }
    assert(metrics.count == 4);
}

static void declared_preparation_capacities_pin_both_mappings() {
    ControlShmMetrics metrics;
    ControlShmRegion request(metrics, true), provider_request(metrics, false);
    ControlShmRegion response(metrics, true), caller_response(metrics, false);
    request.reserve_payload(3);
    response.reserve_payload(5);
    assert(request.capacity() - kPayloadOffset > 3); // Physical page rounding is not permission to grow.
    provider_request.bind_prepared(request.lease(0), ControlShmDescriptor(::dup(request.descriptor())), 3);
    caller_response.bind_prepared(response.lease(0), ControlShmDescriptor(::dup(response.descriptor())), 5);
    const auto regions = metrics.regions_created.load();
    const auto mappings = metrics.mapping_opens.load();
    const auto request_token = request.token(), response_token = response.token();
    for (int call = 0; call < 8; ++call) {
        assert(!request.write({1, 2, 3}));
        assert(provider_request.read(request.lease(3), ControlShmDescriptor{}) == std::vector<std::uint8_t>({1, 2, 3}));
        assert(!response.write({4, 5, 6, 7, 8}));
        const auto owned = caller_response.read(response.lease(5), ControlShmDescriptor{});
        assert(!response.write({9}));
        assert(owned == std::vector<std::uint8_t>({4, 5, 6, 7, 8}));
    }
    rejects([&] { request.write({1, 2, 3, 4}); });
    rejects([&] { response.write({1, 2, 3, 4, 5, 6}); });
    rejects([&] { provider_request.read(request.lease(4), ControlShmDescriptor{}); });
    rejects([&] { caller_response.read(response.lease(6), ControlShmDescriptor{}); });
    rejects([&] { provider_request.read(request.lease(3), ControlShmDescriptor(::dup(request.descriptor()))); });
    rejects([&] { request.reserve_payload(4); });
    rejects([&] { caller_response.bind_prepared(response.lease(0), ControlShmDescriptor(::dup(response.descriptor())), 5); });
    assert(metrics.regions_created == regions && metrics.mapping_opens == mappings);
    assert(metrics.region_resizes == 0 && request.token() == request_token && response.token() == response_token);
    // Rejected writes did not replace the lane or mutate its prior payload.
    assert(provider_request.read(request.lease(3), ControlShmDescriptor{}) == std::vector<std::uint8_t>({1, 2, 3}));
    assert(caller_response.read(response.lease(1), ControlShmDescriptor{}) == std::vector<std::uint8_t>({9}));
}

static void failed_preparation_preserves_manager_cause_over_socket() {
    ControlShmMetrics metrics;
    int pair[2]; assert(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    ControlShmDescriptor sender(pair[0]), receiver(pair[1]);
    ControlShmRegion owner(metrics, true); owner.write({1});
    const std::string cause = "provider-ready preparation requires a registered local Control handler";
    for (const auto& fault : {"valid", "lease", "descriptor", "trailing", "missing_lease", "version4"}) {
        std::string message;
        append_control_shm_header(message, kLocalControlKindPrepareResp);
        append_u8(message, 0); append_string(message, cause);
        if (std::string_view(fault) != "missing_lease")
            append_u8(message, std::string_view(fault) == "lease" ? 1 : 0);
        if (std::string_view(fault) == "trailing") message.push_back('\0');
        if (std::string_view(fault) == "version4") message[4] = 4;
        const auto before = descriptors();
        ControlShmFraming::send_until(sender.get(), message,
            std::string_view(fault) == "descriptor" ? owner.descriptor() : -1, budget(), metrics);
        {
            auto received = ControlShmFraming::receive_until(receiver.get(), budget(), metrics);
            ControlShmPreparedFrame frame{received.data, std::move(received.descriptor)};
            LocalPayloadLease lease;
            bool rejected = false;
            try { decode_control_shm_preparation_response(frame, lease); }
            catch (const ControlShmProtocolError&) {
                rejected = true;
                assert(std::string_view(fault) != "valid");
            }
            catch (const std::runtime_error& error) {
                rejected = true;
                assert(std::string_view(fault) == "valid");
                assert(std::string(error.what()) == "Control preparation failed: " + cause);
            }
            assert(rejected && lease.region_name.empty());
        }
        assert(descriptors() == before);
    }
    assert(metrics.mapping_opens == 0 && metrics.payload_reads == 0);
}

int main() {
    queued_io_and_expired_deadline();
    failed_preparation_preserves_manager_cause_over_socket();
    late_positive_io_rejected_and_descriptor_owned();
    partial_prefix_descriptor_cleanup();
    backpressure_deadline_and_one_descriptor();
    prepared_budget_and_lock_failure();
    prepared_metadata_reuse();
    declared_preparation_capacities_pin_both_mappings();
    process_budget_across_clients();
    scheduling_promotion_requires_existing_mapping_lock();
    unaligned_shm_actual_page_budget_and_release();
    prepared_session_identity_and_nonowning_cache();
    handler_publications_keep_active_generation_and_management_destruction();
    handler_publication_reclaims_only_inactive_generations();
    handler_callable_clones_preserve_mutable_captures_and_inflight_update();
    nested_packet_publication_defers_original_destruction();
    bounded_duration_metrics_and_disabled_scope();
}
