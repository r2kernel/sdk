// The native Event receive loop (client.cpp recv_loop) after BufferTooSmall:
// the kept Event is received once more with the lengths Core reports; the
// loop's own payload buffer grows in place, so a later Event of that payload
// size needs no retry; an oversized topic or publisher name is retried into
// name buffers the loop keeps (reused, never shrunk), while the loop's
// per-message 256-byte name buffers stay fixed.
// r2kernel_event_client_recv_ex is replaced by a scripted queue that follows
// the Core contract (a too-small buffer keeps the front Event and reports
// every required length).
#include "../src/core/client.cpp"
#include "fake_control_socket.hpp"

#include <deque>
#include <iostream>

namespace growth {
struct ScriptedEvent {
    std::string topic;
    std::string publisher;
    std::vector<std::uint8_t> payload;
};
struct Capacities {
    std::int64_t timeout_ms;
    std::size_t topic;
    std::size_t publisher;
    std::size_t payload;
    int rc;
};

std::mutex mutex;
std::deque<ScriptedEvent> queue;
std::vector<Capacities> calls;
std::atomic<bool> drained{false};

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}
}  // namespace growth

extern "C" int r2kernel_event_client_recv_ex(R2KernelEventClient*, int64_t timeout_ms, uint8_t* out_topic, size_t topic_cap,
    size_t* out_topic_len, uint8_t* out_publisher, size_t publisher_cap, size_t* out_publisher_len, int64_t* observed,
    int64_t* published, int64_t* received, uint8_t* out_payload, size_t payload_cap, size_t* out_payload_len) {
    using namespace growth;
    std::unique_lock<std::mutex> guard(mutex);
    if (queue.empty()) {
        drained = true;
        guard.unlock();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        return -2;
    }
    const auto& front = queue.front();
    *out_topic_len = front.topic.size();
    *out_publisher_len = front.publisher.size();
    *out_payload_len = front.payload.size();
    if (front.topic.size() > topic_cap || front.publisher.size() > publisher_cap || front.payload.size() > payload_cap) {
        calls.push_back({timeout_ms, topic_cap, publisher_cap, payload_cap, r2kernel::core::detail::kCoreBufferTooSmall});
        return r2kernel::core::detail::kCoreBufferTooSmall;
    }
    calls.push_back({timeout_ms, topic_cap, publisher_cap, payload_cap, 0});
    std::memcpy(out_topic, front.topic.data(), front.topic.size());
    std::memcpy(out_publisher, front.publisher.data(), front.publisher.size());
    std::memcpy(out_payload, front.payload.data(), front.payload.size());
    *observed = 1;
    *published = 2;
    *received = 3;
    queue.pop_front();
    return 0;
}

int main() {
    using namespace growth;
    try {
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
        const std::string pool = "/r2k-cpp-test-growth-" + std::to_string(::getpid());
        R2KernelShmHandle* shm = nullptr;
        require(r2kernel_shm_create(pool.c_str(), 4096, 4, &shm) == 0 && shm != nullptr, "shared-memory pool");
        r2kernel_test::FakeControl control;
        control.respond([&](const std::string& request) -> std::string {
            if (request.find("\"cmd\":\"register_subsystem\"") == std::string::npos) return "{\"res\":\"ok\",\"ok\":true}";
            return "{\"res\":\"ok\",\"ok\":true,\"topic_buffer_pool_name\":\"" + pool + "\","
                   "\"topic_buffer_pool_slot_size\":4096,\"topic_buffer_pool_slot_count\":4,"
                   "\"manager_peer_auth_required\":false,"
                   "\"control_local_endpoint\":\"/tmp/r2k-cpp-test-unused.sock\","
                   "\"control_local_payload_policy\":\"forwarded_shm_v5\","
                   "\"control_local_protocol\":\"binary_shm_v5\",\"control_shm_min_region_bytes\":4096,"
                   "\"control_shared_buffer_slot_size\":16777216,\"control_shm_max_channels\":128,"
                   "\"control_shm_max_owned_bytes\":67108864,\"control_shm_max_mapped_bytes\":67108864}";
        });
        // The loop starts with 256-byte names and a max(slot size, 1024) = 4096-byte payload buffer.
        const std::string big_topic = "growth/big";
        const std::string long_topic(300, 't');
        const std::string long_topic_2(280, 'u');
        const std::string short_topic = "growth/small";
        const std::vector<ScriptedEvent> script = {
            {big_topic, "publisher", std::vector<std::uint8_t>(5000, 0x11)},     // payload too large: grows payload_buf
            {big_topic, "publisher", std::vector<std::uint8_t>(5000, 0x22)},     // fits the grown loop buffer
            {long_topic, "publisher", std::vector<std::uint8_t>(64, 0x33)},      // topic too long: retry name buffer 300
            {long_topic_2, "publisher", std::vector<std::uint8_t>(64, 0x44)},    // reuses the 300-byte retry buffer
            {short_topic, "publisher", std::vector<std::uint8_t>(64, 0x55)},
        };
        {
            std::lock_guard<std::mutex> guard(mutex);
            queue.assign(script.begin(), script.end());
        }
        std::vector<std::pair<std::string, std::vector<std::uint8_t>>> delivered;
        std::mutex delivered_mutex;
        {
            r2kernel::core::ClientOptions options;
            options.subsystem_name = "growth_sub";
            r2kernel::core::R2KernelClient client(options);
            for (const auto& topic : {big_topic, long_topic, long_topic_2, short_topic}) {
                r2kernel::wrapper::EventSubscribeSpec spec;
                spec.topic = topic;
                spec.size = 64;
                spec.callback = [&delivered, &delivered_mutex, topic](const r2kernel::wrapper::EventPayload& data,
                                                                      std::int64_t) {
                    std::lock_guard<std::mutex> guard(delivered_mutex);
                    delivered.emplace_back(topic, data);
                };
                require(client.register_event_subscribe(spec), "subscription registration");
            }
            client.start_event_transport();
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (!drained && std::chrono::steady_clock::now() < deadline)
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            client.stop_event_transport();
        }
        r2kernel_shm_close(shm);
        r2kernel_shm_unlink(pool.c_str());

        require(drained, "the scripted Events were not all received");
        require(delivered.size() == script.size(), "every Event must be delivered once, got " + std::to_string(delivered.size()));
        for (std::size_t index = 0; index < script.size(); ++index)
            require(delivered[index].first == script[index].topic && delivered[index].second == script[index].payload,
                "Event " + std::to_string(index) + " was not delivered intact and in order");
        // Per Event: the loop's receive (timeout > 0) and, after BufferTooSmall, one retry (timeout 0).
        const auto expect = [](const Capacities& call, std::int64_t timeout, std::size_t topic, std::size_t payload, int rc,
                               const std::string& what) {
            require((timeout == 0) == (call.timeout_ms == 0) && call.topic == topic && call.publisher == 256
                    && call.payload == payload && call.rc == rc,
                what + ": timeout=" + std::to_string(call.timeout_ms) + " topic=" + std::to_string(call.topic)
                    + " payload=" + std::to_string(call.payload) + " rc=" + std::to_string(call.rc));
        };
        constexpr int kTooSmall = r2kernel::core::detail::kCoreBufferTooSmall;
        require(calls.size() == 8, "expected 8 receive calls (3 retries), got " + std::to_string(calls.size()));
        expect(calls[0], 1, 256, 4096, kTooSmall, "first oversized payload on the default buffers");
        expect(calls[1], 0, 256, 5000, 0, "retry with the reported payload length");
        expect(calls[2], 1, 256, 5000, 0, "the loop keeps its grown payload buffer (no retry)");
        expect(calls[3], 1, 256, 5000, kTooSmall, "oversized topic on the fixed loop name buffer");
        expect(calls[4], 0, 300, 5000, 0, "retry with the reported topic length");
        expect(calls[5], 1, 256, 5000, kTooSmall, "second oversized topic");
        expect(calls[6], 0, 300, 5000, 0, "the retry name buffer is reused, never shrunk");
        expect(calls[7], 1, 256, 5000, 0, "small Event after growth");
        std::cout << "event receive buffer growth checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
