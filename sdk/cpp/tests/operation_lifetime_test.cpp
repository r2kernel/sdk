// Offline behavioral tests of Operation worker ownership and termination.
#include "../src/wrapper/operation_event_wire.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "fake_control_socket.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

using namespace r2kernel::wrapper;
using namespace std::chrono_literals;
namespace fs = std::filesystem;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

// A process watchdog bounds even a broken self-join or stuck teardown. Normal
// failure paths release both gates and join the terminator before reporting.
class Watchdog {
public:
    Watchdog() : thread_([this] {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!cv_.wait_for(lock, 20s, [this] { return done_; })) {
            std::cerr << "operation lifetime test timed out\n";
            std::_Exit(1);
        }
    }) {}
    ~Watchdog() {
        { std::lock_guard<std::mutex> lock(mutex_); done_ = true; }
        cv_.notify_all();
        thread_.join();
    }
private:
    std::mutex mutex_;
    std::condition_variable cv_;
    bool done_ = false;
    std::thread thread_;
};

struct Gates {
    std::mutex mutex;
    std::condition_variable cv;
    bool entered = false;
    bool release_handler = false;
    bool saw_cancel = false;
    bool handler_returning = false;
    bool result_entered = false;
    bool release_result = false;
    bool result_returned = false;
    bool self_returned = false;
    bool terminated = false;
    bool hook_called = false;
    bool block_result = false;
    bool throw_handler = false;
    bool self_terminate = false;
    bool explicit_terminal = false;
    bool terminal_published = false;
    std::string log;

    template<class Predicate> bool wait(Predicate predicate) {
        std::unique_lock<std::mutex> lock(mutex);
        return cv.wait_for(lock, 2s, predicate);
    }
};

class Client final : public CoreClient {
public:
    explicit Client(Gates& gates) : gates_(gates) {}
    std::int64_t now_ns() const override { return ++clock_; }
    bool refresh_clock_status() override { return true; }
    void start_event_transport() override {}
    void stop_event_transport() override {}
    void publish_prepared_status(const std::string&) override {}
    bool register_event_publish(const EventPublishSpec&) override {
        std::function<void()> callback;
        {
            std::lock_guard<std::mutex> lock(gates_.mutex);
            callback = std::move(on_register_);
            on_register_ = {};
        }
        if (callback) callback();
        return true;
    }
    bool register_event_subscribe(const EventSubscribeSpec&) override { return true; }
    bool publish_event(const std::string& topic, const EventPayload& payload, std::int64_t, int) override {
        std::function<void()> callback;
        {
            std::lock_guard<std::mutex> lock(gates_.mutex);
            callback = std::move(on_publish_);
            on_publish_ = {};
        }
        if (callback) callback();
        // The completion publication: the terminal state event (published
        // first, as in the Python SDK) or a result event. A failed Operation
        // without a result publishes only the terminal state.
        const auto ends_with = [&topic](const char* suffix) {
            const std::string text(suffix);
            return topic.size() >= text.size() && topic.compare(topic.size() - text.size(), text.size(), text) == 0;
        };
        bool completion = ends_with("/result");
        if (ends_with("/state") && operation_event_wire::is_wire(payload)) {
            const auto state = operation_event_wire::decode(payload).state;
            completion = state == "succeeded" || state == "failed" || state == "canceled";
        }
        if (completion) {
            std::unique_lock<std::mutex> lock(gates_.mutex);
            if (gates_.block_result) {
                gates_.result_entered = true;
                gates_.cv.notify_all();
                gates_.cv.wait(lock, [this] { return gates_.release_result; });
                gates_.result_returned = true;
                gates_.cv.notify_all();
            }
        }
        return true;
    }
    int bind_control(int port, const std::string&) override { return port; }
    bool start_control_server(const ControlHandler& handler) override { handler_ = handler; return true; }
    void stop_control_server() override {}
    int get_control_port() const override { return 41001; }
    std::optional<ControlRoute> resolve_control_target(const std::string&) override {
        ControlRoute route; route.host = "127.0.0.1"; route.port = 41001; return route;
    }
    bool connect_control(const std::string&, const std::string&, int) override { return true; }
    bool handshake_control(const std::string&, int) override { return true; }
    bool send_control(const std::string&, const std::string&, const ByteBuffer&, std::int64_t, float, float, int) override { return true; }
    std::optional<ControlResponse> recv_control(const std::string&, int) override { return ControlResponse{}; }
    void prepare_control_route(const std::string&, const std::string&, const std::string&, const std::string&, const std::string&, const std::string&) override {}
    std::optional<ControlResponse> transact_control(const std::string&, const std::string&, const ByteBuffer&, std::int64_t, float, float, int, int) override { return ControlResponse{}; }
    ControlHandler handler_;
    std::function<void()> on_register_;
    std::function<void()> on_publish_;
private:
    Gates& gates_;
    mutable std::atomic<std::int64_t> clock_{0};
};

class Endpoint final : public Subsystem {
public:
    Endpoint(SubsystemOptions options, Gates& gates) : Subsystem(std::move(options)), gates_(gates) {}
private:
    bool on_initialize() override {
        OperationOptions options;
        options.request_type = options.feedback_type = options.result_type = "bytes";
        return register_operation("move", [this](OperationRequest, OperationContext& context) -> std::optional<std::string> {
            {
                std::unique_lock<std::mutex> lock(gates_.mutex);
                gates_.entered = true;
                gates_.cv.notify_all();
                if (!gates_.self_terminate) {
                    if (gates_.explicit_terminal) {
                        lock.unlock();
                        context.succeed("{}");
                        lock.lock();
                        gates_.terminal_published = true;
                        gates_.cv.notify_all();
                    }
                    while (!gates_.release_handler) {
                        lock.unlock();
                        const bool canceled = context.is_cancel_requested();
                        lock.lock();
                        if (canceled) { gates_.saw_cancel = true; gates_.cv.notify_all(); }
                        gates_.cv.wait_for(lock, 10ms, [this] { return gates_.release_handler; });
                    }
                    gates_.handler_returning = true;
                    gates_.cv.notify_all();
                    if (gates_.throw_handler) throw std::runtime_error("fixture operation failure");
                    return std::string("{}");
                }
            }
            terminate();
            { std::lock_guard<std::mutex> lock(gates_.mutex); gates_.self_returned = true; }
            gates_.cv.notify_all();
            return std::string("{}");
        }, options);
    }
    bool on_start() override { return true; }
    bool on_terminate() override {
        std::lock_guard<std::mutex> lock(gates_.mutex);
        gates_.hook_called = true;
        gates_.cv.notify_all();
        return true;
    }
    bool on_reset() override { return true; }
    Gates& gates_;
};

struct Fixture {
    Gates gates;
    std::shared_ptr<Client> client = std::make_shared<Client>(gates);
    fs::path directory = r2kernel_test::unique_temp_path("operation_lifetime");
    SubsystemOptions options() {
        fs::create_directories(directory);
        auto manifest = directory / "fixture.yaml";
        std::ofstream file(manifest);
        file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: fixture.operation_lifetime\n  role: component\n"
                "provides:\n  local:\n    contract: fixture.operation_lifetime.private.v1\n    visibility: internal\n"
                "    endpoints:\n      operations: [move]\n";
        require(bool(file), "manifest fixture write failed");
        SubsystemOptions result;
        result.name = "fixture.operation_lifetime";
        result.manifest_path = manifest;
        result.schema_roots = std::vector<std::string>{};
        result.core_client = client;
        result.log_sink = [this](std::string_view, std::string_view, std::string_view, std::string_view message) {
            std::lock_guard<std::mutex> lock(gates.mutex);
            gates.log += std::string(message) + "\n";
        };
        return result;
    }
    ~Fixture() { std::error_code ignored; fs::remove_all(directory, ignored); }
};

std::string start(const ControlHandler& dispatch, const char* id) {
    ControlRequest request;
    request.sender = "peer";
    request.topic = "operation.start";
    const std::string body = std::string("{\"operation\":\"move\",\"request_id\":\"") + id + "\",\"params\":{}}";
    request.payload.assign(body.begin(), body.end());
    const auto reply = dispatch(std::move(request));
    return std::string(reply.payload.begin(), reply.payload.end());
}
bool accepted(const std::string& response) { return response.find("\"accepted\":true") != std::string::npos; }

void drain_and_publication(bool throws) {
    Fixture fixture;
    fixture.gates.block_result = true;
    fixture.gates.throw_handler = throws;
    Endpoint endpoint(fixture.options(), fixture.gates);
    endpoint.initialize();
    require(endpoint.state() == DeviceState::ready, "fixture did not initialize");
    require(endpoint.start_operation_server(), "Operation server did not start");
    auto dispatch = fixture.client->handler_;
    require(bool(dispatch), "no dispatch handler");
    const bool admitted = accepted(start(dispatch, "first"));
    const bool entered = fixture.gates.wait([&] { return fixture.gates.entered; });
    std::thread terminator([&] {
        endpoint.terminate();
        { std::lock_guard<std::mutex> lock(fixture.gates.mutex); fixture.gates.terminated = true; }
        fixture.gates.cv.notify_all();
    });
    const bool canceled = fixture.gates.wait([&] { return fixture.gates.saw_cancel || fixture.gates.terminated; });
    bool held_handler, hook_early;
    {
        std::unique_lock<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.cv.wait_for(lock, 100ms, [&] { return fixture.gates.terminated || fixture.gates.hook_called; });
        held_handler = !fixture.gates.terminated;
        hook_early = fixture.gates.hook_called;
    }
    const bool restarted_during_drain = endpoint.start_operation_server();
    const auto refused = start(dispatch, "during-drain");
    {
        std::lock_guard<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.release_handler = true;
    }
    fixture.gates.cv.notify_all();
    const bool reached_result = fixture.gates.wait([&] { return fixture.gates.result_entered || fixture.gates.terminated; });
    bool held_result, result_entered, saw_cancel;
    {
        std::unique_lock<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.cv.wait_for(lock, 100ms, [&] { return fixture.gates.terminated || fixture.gates.hook_called; });
        held_result = !fixture.gates.terminated && !fixture.gates.hook_called;
        result_entered = fixture.gates.result_entered;
        saw_cancel = fixture.gates.saw_cancel;
        fixture.gates.release_result = true;
    }
    fixture.gates.cv.notify_all();
    terminator.join();
    // A broken implementation may have returned while detached workers remain.
    // Exit without destroying their fixture on a failing implementation.
    std::lock_guard<std::mutex> lock(fixture.gates.mutex);
    const auto check = [](bool ok, const char* message) {
        if (!ok) { std::cerr << message << '\n'; std::_Exit(1); }
    };
    check(admitted && entered, "Operation was not admitted and entered");
    check(canceled && saw_cancel, "termination did not expose cooperative cancellation");
    check(held_handler && !hook_early, "termination or its hook ran before the handler finished");
    check(!restarted_during_drain, "Operation server restarted during termination drain");
    check(!accepted(refused), "new Operation admitted during drain");
    check(reached_result && result_entered && held_result, "termination did not wait for the completion publication");
    check(fixture.gates.result_returned && fixture.gates.hook_called, "termination did not finish after publication");
    check(!throws || fixture.gates.log.find("fixture operation failure") != std::string::npos,
        "handler exception was not reported");
}

void terminal_state_is_not_worker_completion() {
    Fixture fixture;
    fixture.gates.explicit_terminal = true;
    Endpoint endpoint(fixture.options(), fixture.gates);
    endpoint.initialize();
    require(endpoint.state() == DeviceState::ready && endpoint.start_operation_server(), "fixture did not start");
    const bool admitted = accepted(start(fixture.client->handler_, "terminal"));
    const bool published = fixture.gates.wait([&] { return fixture.gates.terminal_published; });
    std::thread terminator([&] {
        endpoint.terminate();
        { std::lock_guard<std::mutex> lock(fixture.gates.mutex); fixture.gates.terminated = true; }
        fixture.gates.cv.notify_all();
    });
    const bool canceled = fixture.gates.wait([&] { return fixture.gates.saw_cancel || fixture.gates.terminated; });
    bool held;
    {
        std::unique_lock<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.cv.wait_for(lock, 200ms, [&] { return fixture.gates.terminated || fixture.gates.hook_called; });
        held = !fixture.gates.terminated && !fixture.gates.hook_called;
        fixture.gates.release_handler = true;
    }
    fixture.gates.cv.notify_all();
    terminator.join();
    std::lock_guard<std::mutex> lock(fixture.gates.mutex);
    if (!admitted || !published || !held || !canceled || !fixture.gates.saw_cancel ||
        !fixture.gates.handler_returning || !fixture.gates.hook_called) {
        std::cerr << "terminal Operation worker was not canceled and drained\n";
        std::_Exit(1);
    }
}

void partial_stop_and_restart() {
    Fixture fixture;
    Endpoint endpoint(fixture.options(), fixture.gates);
    endpoint.initialize();
    require(endpoint.state() == DeviceState::ready && endpoint.start_operation_server(), "fixture did not start");
    auto dispatch = fixture.client->handler_;
    const bool admitted = accepted(start(dispatch, "first"));
    const bool entered = fixture.gates.wait([&] { return fixture.gates.entered; });
    endpoint.stop_operation_server();
    const auto refused = start(dispatch, "stopped");
    const bool canceled = fixture.gates.wait([&] { return fixture.gates.saw_cancel; });
    const bool restarted = endpoint.start_operation_server();
    {
        std::lock_guard<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.release_handler = true;
    }
    fixture.gates.cv.notify_all();
    // The restarted server must admit work, and terminate must drain both workers.
    const bool readmitted = accepted(start(dispatch, "restarted"));
    endpoint.terminate();
    require(admitted && entered, "initial Operation was not admitted");
    require(!accepted(refused), "partial stop admitted an Operation");
    require(canceled, "partial stop did not request cancellation");
    require(restarted && readmitted, "explicit restart did not re-enable admission");
    {
        std::lock_guard<std::mutex> lock(fixture.gates.mutex);
        fixture.gates.entered = false;
        fixture.gates.hook_called = false;
    }
    const bool restarted_after_terminate = endpoint.start_operation_server();
    const bool admitted_after_terminate = restarted_after_terminate &&
        accepted(start(fixture.client->handler_, "after-terminate"));
    const bool entered_after_terminate = admitted_after_terminate &&
        fixture.gates.wait([&] { return fixture.gates.entered; });
    endpoint.terminate();
    require(restarted_after_terminate && admitted_after_terminate && entered_after_terminate,
        "explicit restart after termination did not admit an Operation");
}

void callback_terminate_refused() {
    Fixture fixture;
    fixture.gates.self_terminate = true;
    Endpoint endpoint(fixture.options(), fixture.gates);
    endpoint.initialize();
    require(endpoint.state() == DeviceState::ready && endpoint.start_operation_server(), "fixture did not start");
    const bool admitted = accepted(start(fixture.client->handler_, "self"));
    const bool returned = fixture.gates.wait([&] { return fixture.gates.self_returned; });
    bool hook_called;
    {
        std::lock_guard<std::mutex> lock(fixture.gates.mutex);
        hook_called = fixture.gates.hook_called;
    }
    endpoint.terminate();
    require(admitted && returned, "callback terminate did not return");
    require(!hook_called, "callback terminate started teardown");
}

// Topic registration happens synchronously inside operation.start, before its
// worker can be launched. As in the Python SDK no state event is published
// there (queued is not published), so the first publication comes from the
// worker. Teardown from either callback must refuse rather than wait for its
// own admission or worker to finish.
void admission_callback_terminate_refused(bool during_registration) {
    Fixture fixture;
    fixture.gates.release_handler = true;
    Endpoint endpoint(fixture.options(), fixture.gates);
    endpoint.initialize();
    require(endpoint.state() == DeviceState::ready && endpoint.start_operation_server(), "fixture did not start");
    bool callback_returned = false;
    bool hook_in_callback = false;
    bool callback_on_dispatch_thread = false;
    const auto dispatch_thread = std::this_thread::get_id();
    const auto callback = [&] {
        callback_on_dispatch_thread = std::this_thread::get_id() == dispatch_thread;
        endpoint.terminate();
        {
            std::lock_guard<std::mutex> lock(fixture.gates.mutex);
            hook_in_callback = fixture.gates.hook_called;
        }
        callback_returned = true;
    };
    {
        std::lock_guard<std::mutex> lock(fixture.gates.mutex);
        if (during_registration) fixture.client->on_register_ = callback;
        else fixture.client->on_publish_ = callback;
    }
    const bool admitted = accepted(start(fixture.client->handler_, "admission-callback"));
    endpoint.terminate();
    require(callback_returned && !hook_in_callback,
        "callback termination did not return without teardown");
    require(callback_on_dispatch_thread == during_registration,
        during_registration ? "topic registration left the dispatch thread"
                            : "an Operation state event was published during admission");
    require(admitted, "refused callback termination disrupted Operation admission");
    require(fixture.gates.hook_called, "subsequent external termination did not run its hook");
}
} // namespace

int main() {
    Watchdog watchdog;
    try {
        drain_and_publication(false);
        drain_and_publication(true);
        terminal_state_is_not_worker_completion();
        partial_stop_and_restart();
        callback_terminate_refused();
        admission_callback_terminate_refused(true);
        admission_callback_terminate_refused(false);
        std::cout << "Operation lifetime checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Operation lifetime test: " << error.what() << '\n';
        return 1;
    }
}
