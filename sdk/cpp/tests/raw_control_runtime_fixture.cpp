// Run against an actual local r2kernel-control with production Control/Event
// transports and strict policy. See raw_control_runtime_fixtures/README.md for
// the four modes.
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <limits>
#include <sched.h>
#include <sstream>
#include <vector>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>
using namespace r2kernel::wrapper;
namespace {
void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
int ephemeral_port() {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    require(fd >= 0, "fixture port socket failed");
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t length = sizeof(address);
    const bool bound = ::bind(fd, reinterpret_cast<sockaddr*>(&address), length) == 0
        && ::getsockname(fd, reinterpret_cast<sockaddr*>(&address), &length) == 0;
    ::close(fd);
    require(bound, "fixture ephemeral port failed");
    return ntohs(address.sin_port);
}
SubsystemOptions options(const std::string& name, const std::filesystem::path& manifest, bool servo_caller = false) {
    SubsystemOptions result; result.name = name; result.manifest_path = manifest;
    if (servo_caller) result.launch_bindings["servo"] = "fixture.raw_gateway.servo";
    result.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions{name});
    return result;
}
class Fixture final : public Subsystem {
public:
    Fixture(SubsystemOptions value, std::string mode) : Subsystem(std::move(value)), mode_(std::move(mode)) {}
private:
    bool on_initialize() override {
        if (mode_ == "gateway") {
            return bind_control(ephemeral_port()) && start_control_server([this](ControlRequest request) {
                // A preparation packet must never run this application effect.
                return ControlResponse{++control_calls_, 0, std::move(request.payload)};
            });
        }
        if (mode_ == "operation-provider") {
            OperationOptions types;
            types.request_type = "bytes"; types.feedback_type = "bytes"; types.result_type = "bytes";
            return register_operation("work", [](OperationRequest, OperationContext& context) -> std::optional<std::string> {
                context.publish_feedback("{\"progress\":1}");
                context.publish_state("running", "{\"phase\":\"work\"}");
                context.succeed("{\"done\":true}");
                return std::nullopt;
            }, types);
        }
        return true;
    }
    bool on_start() override {
        return mode_ != "operation-provider" || (bind_control(ephemeral_port()) && start_operation_server());
    }
    bool on_stop() override { return mode_ == "control-caller"; }
    bool on_terminate() override {
        if (mode_ == "gateway") stop_control_server();
        if (mode_ == "operation-provider") stop_operation_server();
        return true;
    }
    bool on_reset() override { return true; }
    std::string mode_;
    std::atomic<std::int64_t> control_calls_{0};
};
using BenchmarkClock = std::chrono::steady_clock;
std::int64_t elapsed_ns(BenchmarkClock::time_point start, BenchmarkClock::time_point end) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
}
std::string distribution_json(const std::vector<std::int64_t>& samples) {
    require(!samples.empty(), "Control benchmark has no timing samples");
    auto ordered = samples;
    std::sort(ordered.begin(), ordered.end());
    const auto percentile = [&](double fraction) {
        const auto index = static_cast<std::size_t>(std::ceil(fraction * ordered.size())) - 1;
        return ordered[index];
    };
    std::ostringstream output;
    output << "{\"p50\":" << percentile(0.50) << ",\"p95\":" << percentile(0.95)
        << ",\"p99\":" << percentile(0.99) << ",\"max\":" << ordered.back() << '}';
    return output.str();
}
struct ControlBenchmarkResult {
    std::vector<std::int64_t> response_ns, wait_ns, completion_ns;
    std::int64_t total_ns = 0;
    std::string json(bool response_ready) const {
        std::ostringstream output;
        output << "{\"completed_count\":" << response_ns.size() << ",\"error_count\":0"
            << ",\"total_wall_ns\":" << total_ns
            << ",\"full_completion_rate_hz\":" << (static_cast<double>(response_ns.size()) * 1e9 / total_ns)
            << ",\"response_wall_ns\":" << distribution_json(response_ns)
            << ",\"full_completion_wall_ns\":" << distribution_json(completion_ns);
        if (response_ready) output << ",\"reclaimed_wait_wall_ns\":" << distribution_json(wait_ns);
        output << '}';
        return output.str();
    }
};
void check_control_benchmark(Fixture& caller) {
    const char* configured_count = std::getenv("R2KERNEL_CONTROL_BENCH_COUNT");
    if (!configured_count) return;
    const std::string count_text(configured_count);
    require(!count_text.empty() && std::all_of(count_text.begin(), count_text.end(), [](unsigned char character) {
        return character >= '0' && character <= '9';
    }), "R2KERNEL_CONTROL_BENCH_COUNT must be a positive integer");
    const auto count = static_cast<std::size_t>(std::stoull(count_text));
    require(count > 0, "R2KERNEL_CONTROL_BENCH_COUNT must be positive");
    ByteBuffer sent(64);
    for (std::size_t index = 0; index < sent.size(); ++index) sent[index] = static_cast<std::uint8_t>(index * 37U);
    const auto transact = [&](bool response_ready) {
        return response_ready
            ? caller.transact_control_result("fixture.raw_gateway", "servo_control", sent, 1.0F, 1000.0F, 1000, -1)
            : caller.transact_control("fixture.raw_gateway", "servo_control", sent, 1.0F, 1000.0F, 1000, -1);
    };
    const auto verify = [&](const std::optional<ControlResponse>& response) {
        require(response && response->status == 0 && response->payload == sent,
            "Control benchmark failed: missing/error response or 64-byte echo integrity mismatch");
    };
    // Warm both completion APIs and the same lane's persistent reclaimer.
    for (bool response_ready : {false, true}) {
        for (int warmup = 0; warmup < 32; ++warmup) {
            verify(transact(response_ready));
            if (response_ready) caller.wait_control_reclaimed("fixture.raw_gateway", "servo_control", 1000);
        }
    }
    const auto measure = [&](bool response_ready) {
        ControlBenchmarkResult result;
        result.response_ns.reserve(count); result.completion_ns.reserve(count);
        if (response_ready) result.wait_ns.reserve(count);
        const auto total_start = BenchmarkClock::now();
        for (std::size_t packet = 0; packet < count; ++packet) {
            const auto start = BenchmarkClock::now();
            const auto response = transact(response_ready);
            const auto returned = BenchmarkClock::now();
            verify(response);
            auto completed = returned;
            if (response_ready) {
                const auto wait_start = BenchmarkClock::now();
                caller.wait_control_reclaimed("fixture.raw_gateway", "servo_control", 1000);
                completed = BenchmarkClock::now();
                result.wait_ns.push_back(elapsed_ns(wait_start, completed));
            }
            result.response_ns.push_back(elapsed_ns(start, returned));
            result.completion_ns.push_back(elapsed_ns(start, completed));
        }
        result.total_ns = elapsed_ns(total_start, BenchmarkClock::now());
        require(result.response_ns.size() == count && result.completion_ns.size() == count
            && (!response_ready || result.wait_ns.size() == count) && result.total_ns > 0,
            "Control benchmark sample count or duration is invalid");
        return result;
    };
    const auto synchronous = measure(false);
    const auto response_ready = measure(true);
    sched_param caller_parameters{};
    const int caller_policy = ::sched_getscheduler(0);
    require(caller_policy >= 0 && ::sched_getparam(0, &caller_parameters) == 0,
        "Control benchmark could not read caller scheduling policy");
    const char* policy_name = caller_policy == SCHED_OTHER ? "Other"
        : caller_policy == SCHED_FIFO ? "FIFO" : caller_policy == SCHED_RR ? "RR" : "Unknown";
    std::cout << "{\"event\":\"control_completion_benchmark\",\"transport\":\"forwarded_shm_v5\","
        << "\"scope\":\"x86 local echo throughput; no GPIO or 1ms deadline guarantee\","
        << "\"payload_bytes\":64,\"pacing\":\"none\",\"timeout_ms\":1000,"
        << "\"warmup_per_api\":32,\"count_per_api\":" << count
        << ",\"caller_policy\":\"" << policy_name << "\",\"caller_priority\":" << caller_parameters.sched_priority
        << ",\"synchronous\":" << synchronous.json(false)
        << ",\"response_ready\":" << response_ready.json(true) << '}' << std::endl;
}
void check_control(Fixture& caller) {
    const auto route = caller.resolve_control_target("fixture.raw_gateway");
    require(route && route->port >= 0, "Control provider resolution failed");
    require(caller.connect_control("fixture.raw_gateway", route->host, route->port), "Control connect failed");
    require(caller.prepare_control("fixture.raw_gateway", "servo_control", 64, 64, 3000, true),
        "Control preparation did not confirm provider-ready mappings");
    bool reprepare_rejected = false;
    try { caller.prepare_control("fixture.raw_gateway", "servo_control", 64, 64, 3000, true); }
    catch (const std::exception&) { reprepare_rejected = true; }
    require(reprepare_rejected, "Control silently reprepared an existing lane");
    const ByteBuffer sent{0, 1, 2, 127, 128, 255};
    const auto response = caller.transact_control("fixture.raw_gateway", "servo_control", sent, 1.0F, 50.0F, 3000, -1);
    require(response && response->status == 0 && response->payload == sent, "raw public Control changed bytes or did not return");
    require(response->timestamp_ns == 1, "Control preparation invoked the application callback");
    bool oversized_rejected = false;
    try { caller.transact_control("fixture.raw_gateway", "servo_control", ByteBuffer(65, 1), 1.0F, 50.0F, 3000, -1); }
    catch (const std::exception&) { oversized_rejected = true; }
    require(oversized_rejected, "Prepared Control accepted bytes beyond its exact declared capacity");
    std::cout << "raw Control servo_control byte-for-byte echo passed: " << sent.size() << " bytes\n";
    const auto result = caller.transact_control_result("fixture.raw_gateway", "servo_control", sent, 1.0F, 1000.0F, 3000, -1);
    require(result && result->status == 0 && result->payload == sent, "public response-ready Control changed bytes or did not return");
    require(result->timestamp_ns == 2, "Rejected over-capacity Control request invoked the callback");
    caller.wait_control_reclaimed("fixture.raw_gateway", "servo_control", 3000);
    // Reuse the same lane through the original completion API after reclamation.
    const auto reused = caller.transact_control("fixture.raw_gateway", "servo_control", ByteBuffer{42}, 1.0F, 1000.0F, 3000, -1);
    require(reused && reused->payload == ByteBuffer{42} && result->payload == sent, "Control lane reuse changed an owned response");
    std::cout << "response-ready Control, explicit reclamation and synchronous lane reuse passed\n";
    require(caller.connect_control("fixture.raw_gateway", route->host, route->port), "Control capacity rebind failed");
    require(caller.prepare_control("fixture.raw_gateway", "servo_control", 65, 65, 3000, true),
        "Control rebind did not prepare its new explicit capacities");
    const ByteBuffer larger(65, 7);
    const auto rebound = caller.transact_control("fixture.raw_gateway", "servo_control", larger, 1.0F, 1000.0F, 3000, -1);
    require(rebound && rebound->payload == larger && rebound->timestamp_ns == 4,
        "Rebound Control preparation changed callback count or did not admit new capacity");
    require(result->payload == sent, "Control rebind reclaimed an application-owned response");
    check_control_benchmark(caller);
    // A supported caller lifecycle must not turn an invalidated prepared lane
    // into an implicitly cold-admitted channel on the next ordinary exchange.
    require(caller.connect_control("fixture.raw_gateway", route->host, route->port), "Control invalidation setup rebind failed");
    require(caller.prepare_control("fixture.raw_gateway", "servo_control", 8, 8, 3000, false),
        "Control invalidation setup preparation failed");
    const ByteBuffer eight(8, 42);
    const auto baseline = caller.transact_control("fixture.raw_gateway", "servo_control", eight, 1.0F, 1000.0F, 3000, -1);
    require(baseline && baseline->payload == eight, "Control invalidation baseline failed");
    require(caller.stop(), "Control caller did not stop before codec reinitialization");
    caller.initialize(); require(caller.state() == DeviceState::ready, "Control caller codec reinitialization failed");
    caller.start(); require(caller.state() == DeviceState::operating, "Control caller restart failed");
    const auto must_reject = [](const auto& exchange) {
        bool rejected = false;
        try { exchange(); } catch (const std::exception&) { rejected = true; }
        require(rejected, "Invalidated prepared Control silently admitted an ordinary exchange");
    };
    must_reject([&] { caller.transact_control("fixture.raw_gateway", "servo_control", eight, 1.0F, 1000.0F, 3000, -1); });
    must_reject([&] { caller.transact_control("fixture.raw_gateway", "servo_control", ByteBuffer(9, 1), 1.0F, 1000.0F, 3000, -1); });
    must_reject([&] { caller.transact_control_result("fixture.raw_gateway", "servo_control", eight, 1.0F, 1000.0F, 3000, -1); });
    require(caller.prepare_control("fixture.raw_gateway", "servo_control", 8, 8, 3000, false),
        "Explicit idle preparation did not recover the invalidated Control lane");
    const auto recovered = caller.transact_control("fixture.raw_gateway", "servo_control", eight, 1.0F, 1000.0F, 3000, -1);
    require(recovered && recovered->payload == eight && recovered->timestamp_ns == baseline->timestamp_ns + 1,
        "Invalidation/repreparation invoked a callback or failed recovery");
    must_reject([&] { caller.transact_control_result("fixture.raw_gateway", "servo_control", eight, 1.0F, 1000.0F, 3000, -1); });
    require(caller.connect_control("fixture.raw_gateway", route->host, route->port), "Control explicit recovery rebind failed");
    require(caller.prepare_control("fixture.raw_gateway", "servo_control", 9, 9, 3000, true),
        "Control explicit recovery did not admit new capacities/completion mode");
    const auto final = caller.transact_control_result("fixture.raw_gateway", "servo_control", ByteBuffer(9, 7), 1.0F, 1000.0F, 3000, -1);
    require(final && final->payload == ByteBuffer(9, 7) && final->timestamp_ns == recovered->timestamp_ns + 1,
        "Rejected completion ran a callback or explicit rebind failed");
    caller.wait_control_reclaimed("fixture.raw_gateway", "servo_control", 3000);
    std::cout << "prepared Control codec-generation tombstone and explicit lifecycle recovery passed\n";
}
void check_operation(Fixture& caller) {
    struct Events {
        std::mutex mutex; std::condition_variable changed;
        bool feedback = false, result = false, state = false;
        std::string operation_id;
    };
    const auto events = std::make_shared<Events>();
    const auto callback = [events](const OperationEventMessage& message) {
        std::lock_guard<std::mutex> lock(events->mutex);
        if (message.kind == "feedback") events->feedback = message.payload_json.find("progress") != std::string::npos;
        if (message.kind == "result") events->result = message.payload_json.find("done") != std::string::npos;
        if (message.kind == "state") events->state = true;
        if (!message.operation_id.empty()) events->operation_id = message.operation_id;
        events->changed.notify_all();
    };
    // Empty operation id subscribes to the canonical operation topic before the
    // provider runs; no timing assumption hides lost feedback/result events.
    require(caller.subscribe_operation_feedback("fixture.operation_provider", "work", "", callback), "feedback subscription failed");
    require(caller.subscribe_operation_result("fixture.operation_provider", "work", "", callback), "result subscription failed");
    require(caller.subscribe_operation_state("fixture.operation_provider", "work", "", callback), "state subscription failed");
    const auto response = caller.start_operation("fixture.operation_provider", "work", "{}", 5000);
    require(response && response->ok && response->accepted && !response->operation_id.empty(), "Operation start was not accepted");
    {
        std::unique_lock<std::mutex> lock(events->mutex);
        require(events->changed.wait_for(lock, std::chrono::seconds(10), [&]() { return events->feedback && events->result && events->state; }), "Operation feedback/result/state were not all delivered");
        require(events->operation_id == response->operation_id, "Operation Event id differs from accepted operation");
    }
    const auto query = caller.query_operation("fixture.operation_provider", response->operation_id, 3000);
    require(query && query->ok && query->state == "succeeded" && query->feedback_count == 1, "Operation terminal query did not match delivered Events");
    std::cout << "Operation start, feedback/result/state and succeeded query passed: " << response->operation_id << '\n';
}
}
int main(int argc, char** argv) {
    try {
        require(argc >= 3, "usage: raw_control_runtime_fixture gateway|control-caller|operation-provider|operation-caller MANIFEST_DIR [provider_lifetime_seconds]");
        ::setenv("R2KERNEL_CONTRACT_POLICY", "strict", 1);
        const std::string mode = argv[1];
        const bool provider = mode == "gateway" || mode == "operation-provider";
        require(provider || mode == "control-caller" || mode == "operation-caller", "unknown fixture mode");
        const std::string name = mode == "gateway" ? "fixture.raw_gateway" : mode == "control-caller" ? "fixture.raw_caller"
            : mode == "operation-provider" ? "fixture.operation_provider" : "fixture.operation_caller";
        Fixture fixture(options(name, std::filesystem::path(argv[2]) / (mode + ".yaml"), mode == "control-caller"), mode);
        fixture.initialize(); require(fixture.state() == DeviceState::ready, "fixture initialize did not reach Ready");
        fixture.start(); require(fixture.state() == DeviceState::operating, "fixture start did not reach Operating");
        std::cout << name << " Operating" << std::endl;
        if (provider) std::this_thread::sleep_for(std::chrono::seconds(argc > 3 ? std::stoi(argv[3]) : 40));
        else if (mode == "control-caller") check_control(fixture);
        else check_operation(fixture);
        fixture.terminate();
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
