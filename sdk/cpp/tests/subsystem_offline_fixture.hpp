#pragma once

// Shared offline fixture for the Subsystem regression tests: a recording
// CoreClient that acknowledges prepared status, a hookable Subsystem, and
// temporary manifests. No backend is involved; native negotiation is covered by
// the manual raw_control_runtime_fixture.

#include "r2kernel/wrapper/subsystem.hpp"
#include "wrapped_topic.hpp"
#include <yaml-cpp/yaml.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace r2kernel_test {
using namespace r2kernel::wrapper;

inline void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }

class RecordingClient final : public CoreClient {
public:
    std::int64_t now_ns() const override { return 1; }
    bool refresh_clock_status() override { return true; }
    void start_event_transport() override { ++event_starts; }
    void stop_event_transport() override { ++event_stops; if (stop_callback) stop_callback(); }
    void publish_prepared_status(const std::string& body) override {
        std::unique_lock<std::mutex> lock(mutex);
        if (block_next_ack) {
            block_next_ack = false;
            ack_waiting = true;
            changed.notify_all();
            changed.wait(lock, [&]() { return release_ack; });
        }
        if (reject_next_ack) { reject_next_ack = false; throw std::runtime_error("fixture rejects ACK"); }
        acknowledged = YAML::Load(body)["param"];
        ++ack_count;
    }
    bool has_row(const std::string& topic, bool publisher) const {
        std::lock_guard<std::mutex> lock(mutex);
        return has_row_unlocked(topic, publisher);
    }
    YAML::Node status() const { std::lock_guard<std::mutex> lock(mutex); return YAML::Clone(acknowledged); }
    bool register_event_publish(const EventPublishSpec& spec) override { return acquire(spec.topic, true); }
    bool register_event_subscribe(const EventSubscribeSpec& spec) override { return acquire(spec.topic, false); }
    bool publish_event(const std::string&, const EventPayload&, std::int64_t, int priority) override { last_publish_priority = priority; return true; }
    int bind_control(int port, const std::string&) override { return port; }
    bool start_control_server(const ControlHandler& handler) override { dispatch = handler; return true; }
    void stop_control_server() override { ++control_stops; dispatch = {}; }
    int get_control_port() const override { return 41001; }
    std::optional<ControlRoute> resolve_control_target(const std::string&) override {
        ControlRoute route;
        if (resolved_test_port) { route.host = "127.0.0.1"; route.port = *resolved_test_port; }
        route.controls = route_controls; route.procedures = route_procedures; route.operations = route_operations;
        return route;
    }
    bool connect_control(const std::string&, const std::string&, int) override { ++connect_calls; return true; }
    bool handshake_control(const std::string&, int) override { return true; }
    bool send_control(const std::string&, const std::string&, const ByteBuffer&, std::int64_t, float, float, int) override { return true; }
    std::optional<ControlResponse> recv_control(const std::string&, int) override { return next_recv.value_or(ControlResponse{}); }
    bool prepare_control(const std::string&, const std::string& wrapped, std::size_t, std::size_t, int timeout,
        bool) override {
        last_topic = unwrap_source_topic(wrapped).topic; last_timeout = timeout;
        return true;
    }
    void prepare_control_route(const std::string&, const std::string& topic, const std::string& request,
        const std::string& request_fingerprint, const std::string& response, const std::string& response_fingerprint) override {
        constexpr std::string_view operation_prefix = "operation.start_typed/";
        const bool operation = std::string_view(topic).substr(0, operation_prefix.size()) == operation_prefix && topic.size() > operation_prefix.size();
        require(!request.empty() && request_fingerprint.size() == 64 &&
            (operation ? response.empty() && response_fingerprint.empty() : !response.empty() && response_fingerprint.size() == 64),
            "result caller skipped prepared identity");
        last_prepared_request = request; last_prepared_response = response;
        ++prepared_routes;
        if (reject_route) throw std::runtime_error("fixture peer rejects codec identity");
    }
    std::optional<ControlResponse> transact_control(const std::string&, const std::string& wrapped,
        const ByteBuffer& payload, std::int64_t timestamp, float, float, int, int) override {
        const auto routed = unwrap_source_topic(wrapped);
        const auto& topic = routed.topic;
        last_source = routed.source;
        ++normal_calls;
        last_payload.assign(payload.begin(), payload.end());
        if (topic == "operation.start_typed/work") {
            const std::string receipt = "{\"ok\":true,\"accepted\":true,\"operation_id\":\"offline-operation\",\"state\":\"queued\"}";
            return ControlResponse{timestamp, 0, ByteBuffer(receipt.begin(), receipt.end())};
        }
        return ControlResponse{timestamp, 0, payload};
    }
    std::optional<ControlResponse> transact_control_result(const std::string& target,
        const std::string& wrapped, const ByteBuffer& payload, std::int64_t timestamp,
        float, float, int timeout, int priority) override {
        last_target = target; last_topic = unwrap_source_topic(wrapped).topic; last_timeout = timeout; last_priority = priority;
        ++result_calls;
        return ControlResponse{timestamp, 0, payload};
    }
    void wait_control_reclaimed(const std::string& target, const std::string& wrapped, int timeout) override {
        require(target == last_target && unwrap_source_topic(wrapped).topic == last_topic, "reclamation facade changed lane identity");
        last_timeout = timeout; ++reclaim_calls;
    }
    ControlHandler dispatch;
    std::atomic<int> event_starts{0}, event_stops{0}, control_stops{0};
    int prepared_routes = 0, result_calls = 0, reclaim_calls = 0;
    int normal_calls = 0;
    std::string last_prepared_request, last_prepared_response;
    std::optional<int> resolved_test_port;
    int connect_calls = 0;
    int last_timeout = 0, last_priority = 0, last_publish_priority = 0;
    std::string last_target, last_topic, last_payload, last_source;
    std::optional<ControlResponse> next_recv;
    std::vector<std::string> route_controls, route_procedures, route_operations;
    bool reject_route = false;
    bool wait_for_ack() {
        std::unique_lock<std::mutex> lock(mutex);
        return changed.wait_for(lock, std::chrono::seconds(3), [&]() { return ack_waiting; });
    }
    void unblock_ack() { std::lock_guard<std::mutex> lock(mutex); release_ack = true; changed.notify_all(); }
    void block_ack() {
        std::lock_guard<std::mutex> lock(mutex);
        block_next_ack = true; ack_waiting = false; release_ack = false;
    }
    bool reject_next_ack = false;
    std::function<void()> stop_callback;
    std::function<void()> registration_callback;
    bool reject_next_resource = false;
    bool throw_next_resource = false;
    bool block_next_ack = false;
    std::atomic<int> resources{0};
    std::atomic<int> ack_count{0};
private:
    bool acquire(const std::string& topic, bool publisher) {
        {
            std::lock_guard<std::mutex> lock(mutex);
            require(has_row_unlocked(topic, publisher), "resource acquisition preceded acknowledged endpoint identity");
            ++resources;
            if (throw_next_resource) { throw_next_resource = false; throw std::runtime_error("fixture resource exception"); }
            if (reject_next_resource) { reject_next_resource = false; return false; }
        }
        if (registration_callback) registration_callback();
        return true;
    }
    bool has_row_unlocked(const std::string& topic, bool publisher) const {
        for (const auto& row : acknowledged[publisher ? "publishing" : "subscribing"])
            if (row[0].as<std::string>() == topic) return true;
        return false;
    }
    mutable std::mutex mutex;
    std::condition_variable changed;
    YAML::Node acknowledged;
    bool ack_waiting = false, release_ack = false;
};
class Fixture final : public Subsystem {
public:
    Fixture(SubsystemOptions options, std::function<bool(Fixture&)> hook = {}) : Subsystem(std::move(options)), hook_(std::move(hook)) {}
    bool fail_initialize = false;
    std::atomic<int> initialize_calls{0};
    std::atomic<int> reset_calls{0};
    std::function<bool(Fixture&)> start_hook;
    std::function<bool(Fixture&)> reset_hook;
    std::function<bool(Fixture&)> terminate_hook;
private:
    bool on_initialize() override { ++initialize_calls; return !fail_initialize && (!hook_ || hook_(*this)); }
    bool on_start() override { return !start_hook || start_hook(*this); }
    bool on_terminate() override { return !terminate_hook || terminate_hook(*this); }
    bool on_reset() override { ++reset_calls; return !reset_hook || reset_hook(*this); }
    bool on_pause() override { return true; }
    bool on_stop() override { return true; }
    std::function<bool(Fixture&)> hook_;
};
struct ManifestFiles {
    std::filesystem::path root = std::filesystem::temp_directory_path() /
        ("r2kernel-subsystem-test-" + std::to_string(::getpid()) + "-" +
         std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    ManifestFiles() { std::filesystem::create_directories(root); }
    ~ManifestFiles() { std::error_code error; std::filesystem::remove_all(root, error); }
    std::filesystem::path write(const std::string& name, const std::string& declarations, const std::string& role = "component") {
        const auto path = root / (name + ".yaml");
        std::ofstream file(path);
        file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << name << "\n  role: " << role << '\n' << declarations;
        require(bool(file), "manifest fixture write failed");
        return path;
    }
};
inline SubsystemOptions options(const std::string& name, const std::filesystem::path& manifest, std::shared_ptr<RecordingClient> client) {
    SubsystemOptions result; result.name = name; result.manifest_path = manifest; result.core_client = std::move(client);
    result.schema_roots = std::vector<std::string>{};
    result.log_sink = [](std::string_view, std::string_view, std::string_view, std::string_view) {};
    return result;
}

// Runs each check against one temporary manifest directory with strict
// declarations and an absent Control socket (heartbeats stay disconnected).
inline int run_offline_suite(const char* label, std::initializer_list<void (*)(ManifestFiles&)> checks) {
    try {
        ::setenv("R2KERNEL_CONTRACT_POLICY", "strict", 1);
        ::setenv("R2KERNEL_CONTROL_SOCKET", "/tmp/r2kernel-offline-test-no-control.sock", 1);
        ManifestFiles files;
        for (const auto check : checks) check(files);
        std::cout << label << " offline checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}

}  // namespace r2kernel_test
