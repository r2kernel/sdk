// Offline Event registration regressions: acknowledged ordering, ACK/resource
// failure rollback, and registrations racing initialization or hooks.
#include "subsystem_offline_fixture.hpp"
using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace {
void event_ordering_rollback_and_lifecycle(ManifestFiles& files) {
    const auto manifest = files.write("fixture.event", "provides:\n  digital:\n    contract: api.io.digital.timed_output.v1\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n    endpoints:\n      publishes: [staged, after, rejected, ack_failure, duplicate, other, throwing, structured, unprepared, reset_race, start_race]\n      subscribes: [staged_sub, after_sub, rejected_sub]\n");
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.event", manifest, client), [](Fixture& current) {
        ControlEndpointOptions typed; typed.request_type = "msg.io.digital.timed_output.v1/TimedOutputControlRequest";
        typed.response_type = "msg.io.digital.timed_output.v1/TimedOutputControlResponse"; typed.timing_support = "required";
        return current.register_control_endpoint("digital_output_control", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, typed)
            && current.register_event_publish({"staged", "bytes", 32})
            && current.register_event_subscribe({"staged_sub", "bytes", 32, {}, {}});
    });
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready && client->resources == 2, "staged registrations failed identity ordering");
    require(subsystem.register_event_publish({"after", "bytes", 32}), "post-Ready publisher registration failed");
    require(subsystem.register_event_subscribe({"after_sub", "bytes", 32, {}, {}}), "post-Ready subscriber registration failed");
    client->reject_next_resource = true;
    require(!subsystem.register_event_publish({"rejected", "bytes", 32}), "resource rejection reported success");
    require(!client->has_row("rejected", true), "rejected publisher identity remained advertised");
    require(subsystem.register_event_publish({"rejected", "bytes", 32}), "rejected publisher could not be retried");
    client->reject_next_resource = true;
    require(!subsystem.register_event_subscribe({"rejected_sub", "bytes", 32, {}, {}}), "subscriber rejection reported success");
    require(!client->has_row("rejected_sub", false), "rejected subscriber identity remained advertised");
    require(subsystem.register_event_subscribe({"rejected_sub", "bytes", 32, {}, {}}), "rejected subscriber could not be retried");
    client->throw_next_resource = true;
    require(!subsystem.register_event_publish({"throwing", "bytes", 32}) && !client->has_row("throwing", true), "exception did not roll back publisher identity");
    client->reject_next_ack = true;
    const int resources_before = client->resources;
    require(!subsystem.register_event_publish({"ack_failure", "bytes", 32}), "ACK rejection reported success");
    require(client->resources == resources_before && !client->has_row("ack_failure", true), "ACK failure acquired resource or retained tentative identity");
    require(!subsystem.register_event_publish({"after", "float32", 32}), "conflicting duplicate type accepted");
    require(!subsystem.register_event_subscribe({"operation/remote.provider/work/feedback", "bytes", 32, {}, {}}), "generic Event registration bypassed manifest restrictions");
    const auto callback = [](const OperationEventMessage&) {};
    require(subsystem.subscribe_operation_feedback("remote.provider", "work", "", callback)
        && subsystem.subscribe_operation_result("remote.provider", "work", "", callback)
        && subsystem.subscribe_operation_state("remote.provider", "work", "", callback), "Operation helper could not register canonical remote transport topics");
    require(subsystem.validate_declarations().api_ids.size() == 1 && subsystem.validate_declarations().ok(), "Operation helper metadata failed declaration validation");
    client->block_ack();
    std::atomic<bool> first_ok{false}, duplicate_ok{false}, duplicate_done{false};
    std::thread first([&]() { first_ok = subsystem.register_event_publish({"duplicate", "bytes", 32}); });
    const bool duplicate_ack = client->wait_for_ack();
    std::thread duplicate([&]() { duplicate_ok = subsystem.register_event_publish({"duplicate", "bytes", 32}); duplicate_done = true; });
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    const bool finished_before_ack = duplicate_done;
    client->unblock_ack();
    first.join(); duplicate.join();
    require(duplicate_ack && !finished_before_ack && first_ok && duplicate_ok, "concurrent duplicate observed uncommitted registration");
    require(client->resources == resources_before + 4, "concurrent duplicate acquired more than one resource");
    require(subsystem.register_event_publish({"structured", "msg.io.digital.timed_output.v1/TimedOutputControlResponse", 32}), "prepared structured Event registration failed");
    const int before_unprepared = client->resources;
    bool unprepared_rejected = false;
    try { (void)subsystem.register_event_publish({"unprepared", "msg.fixture.absent.v1/Value", 32}); }
    catch (const std::exception&) { unprepared_rejected = true; }
    require(unprepared_rejected && client->resources == before_unprepared && !client->has_row("unprepared", true), "unprepared structured Event reached identity/resource negotiation");
    // A registration on another thread must not reject an otherwise valid
    // lifecycle request. It may complete before or after the ACK is released.
    client->block_ack();
    std::atomic<bool> other_ok{false};
    std::thread other([&]() { other_ok = subsystem.register_event_publish({"other", "bytes", 32}); });
    const bool fault_ack = client->wait_for_ack();
    std::mutex admission_mutex;
    std::condition_variable admission_changed;
    bool admission_started = false, admission_done = false, fault_accepted = false, request_threw = false;
    std::thread lifecycle([&]() {
        {
            std::lock_guard<std::mutex> lock(admission_mutex);
            admission_started = true;
            admission_changed.notify_all();
        }
        try { fault_accepted = subsystem.request_lifecycle_transition("fault"); }
        catch (...) { request_threw = true; }
        std::lock_guard<std::mutex> lock(admission_mutex);
        admission_done = true;
        admission_changed.notify_all();
    });
    bool fault_started;
    {
        std::unique_lock<std::mutex> lock(admission_mutex);
        fault_started = admission_changed.wait_for(lock, std::chrono::seconds(3), [&]() { return admission_started; });
        admission_changed.wait_for(lock, std::chrono::milliseconds(500), [&]() { return admission_done; });
    }
    client->unblock_ack();
    other.join(); lifecycle.join();
    require(fault_ack && fault_started && !request_threw && fault_accepted && other_ok
        && subsystem.state() == DeviceState::faulted && client->has_row("other", true),
        "concurrent fault was spuriously rejected during Event registration");
    client->block_ack();
    std::atomic<bool> reset_registration_ok{false};
    std::thread registration_during_reset([&]() { reset_registration_ok = subsystem.register_event_publish({"reset_race", "bytes", 32}); });
    const bool reset_ack = client->wait_for_ack();
    bool reset_started = false, reset_done = false;
    std::thread reset([&]() {
        {
            std::lock_guard<std::mutex> lock(admission_mutex);
            reset_started = true;
            admission_changed.notify_all();
        }
        subsystem.reset();
        std::lock_guard<std::mutex> lock(admission_mutex);
        reset_done = true;
        admission_changed.notify_all();
    });
    bool reset_attempted;
    {
        std::unique_lock<std::mutex> lock(admission_mutex);
        reset_attempted = admission_changed.wait_for(lock, std::chrono::seconds(3), [&]() { return reset_started; });
        admission_changed.wait_for(lock, std::chrono::milliseconds(500), [&]() { return reset_done; });
    }
    client->unblock_ack();
    registration_during_reset.join(); reset.join();
    require(reset_ack && reset_attempted && reset_registration_ok && subsystem.reset_calls == 1
        && subsystem.state() == DeviceState::ready && client->has_row("reset_race", true),
        "concurrent reset was spuriously rejected during Event registration");
    client->block_ack();
    std::atomic<bool> start_registration_ok{false};
    std::thread registration_during_start([&]() { start_registration_ok = subsystem.register_event_publish({"start_race", "bytes", 32}); });
    const bool start_ack = client->wait_for_ack();
    bool start_started = false, start_done = false;
    std::thread start([&]() {
        {
            std::lock_guard<std::mutex> lock(admission_mutex);
            start_started = true;
            admission_changed.notify_all();
        }
        subsystem.start();
        std::lock_guard<std::mutex> lock(admission_mutex);
        start_done = true;
        admission_changed.notify_all();
    });
    bool start_attempted;
    {
        std::unique_lock<std::mutex> lock(admission_mutex);
        start_attempted = admission_changed.wait_for(lock, std::chrono::seconds(3), [&]() { return start_started; });
        admission_changed.wait_for(lock, std::chrono::milliseconds(500), [&]() { return start_done; });
    }
    client->unblock_ack();
    registration_during_start.join(); start.join();
    require(start_ack && start_attempted && start_registration_ok
        && subsystem.state() == DeviceState::operating && client->has_row("start_race", true),
        "concurrent start was spuriously rejected during Event registration");
    // Retained resources keep their identity across reset/reinitialize, while an
    // initialization failure must not make an existing topic appear usable.
    require(subsystem.fault(), "repeated Event reset fixture did not enter an allowed reset state");
    subsystem.reset();
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready && client->has_row("after", true), "reset/reinitialize lost retained Event identity");
    subsystem.fail_initialize = true;
    subsystem.initialize();
    require(subsystem.state() == DeviceState::error && !subsystem.register_event_publish({"after", "bytes", 32}), "failed initialization accepted a stale duplicate");
    subsystem.fail_initialize = false;
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "failed initialization could not recover");
    subsystem.terminate();
}

void initialize_can_wait_for_event_registration(ManifestFiles& files) {
    for (const bool fail_hook : {false, true}) {
        const auto name = fail_hook ? "fixture.init_worker_failure" : "fixture.init_worker";
        const auto manifest = files.write(name,
            "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
            "    endpoints:\n      publishes: [worker_pub, after_pub]\n      subscribes: [worker_sub, after_sub]\n");
        auto client = std::make_shared<RecordingClient>();
        std::thread worker;
        std::mutex mutex;
        std::condition_variable changed;
        bool done = false, published = false, subscribed = false;
        Fixture subsystem(options(name, manifest, client), [&](Fixture& current) {
            worker = std::thread([&]() {
                bool pub = false, sub = false;
                try {
                    pub = current.register_event_publish({"worker_pub", "bytes", 32});
                    sub = current.register_event_subscribe({"worker_sub", "bytes", 32, {}, {}});
                } catch (...) {}
                std::lock_guard<std::mutex> lock(mutex);
                published = pub; subscribed = sub; done = true;
                changed.notify_one();
            });
            std::unique_lock<std::mutex> lock(mutex);
            const bool completed = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return done; });
            return completed && published && subscribed && !fail_hook;
        });
        subsystem.initialize();
        worker.join();
        require(done && published && subscribed, "initialize hook could not wait for both worker Event registrations");
        if (fail_hook) {
            require(subsystem.state() == DeviceState::error && client->resources == 0,
                "failed initialize hook committed staged Event resources");
            subsystem.reset();
            require(subsystem.state() == DeviceState::ready, "failed initialize fixture could not reset");
            subsystem.start();
            require(subsystem.state() == DeviceState::ready,
                "reset after failed initialization bypassed the start guard");
        } else {
            require(subsystem.state() == DeviceState::ready && client->resources == 2
                && client->has_row("worker_pub", true) && client->has_row("worker_sub", false),
                "worker-staged Event identities were not acknowledged and committed");
            require(subsystem.register_event_publish({"after_pub", "bytes", 32})
                && subsystem.register_event_subscribe({"after_sub", "bytes", 32, {}, {}})
                && client->has_row("after_pub", true) && client->has_row("after_sub", false),
                "post-initialization Event registration failed");
        }
        subsystem.terminate();
    }
}

void registration_callback_rejects_lifecycle_during_hook(ManifestFiles& files) {
    for (const bool reset : {false, true}) {
        const auto name = reset ? "fixture.callback_reset" : "fixture.callback_start";
        const auto manifest = files.write(name,
            "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
            "    endpoints:\n      publishes: [worker]\n");
        auto client = std::make_shared<RecordingClient>();
        Fixture subsystem(options(name, manifest, client));
        subsystem.initialize();
        require(subsystem.state() == DeviceState::ready, "callback fixture failed to initialize");
        if (reset) require(subsystem.fault(), "callback reset fixture could not fault");

        std::thread worker;
        std::mutex mutex;
        std::condition_variable changed;
        bool done = false, registration_ok = false, callback_returned = false, callback_accepted = true;
        bool initialize_rejected = false, direct_calls_preserved_transport = false;
        client->registration_callback = [&]() {
            try {
                callback_accepted = subsystem.request_lifecycle_transition("fault");
                try { subsystem.initialize(); } catch (const std::runtime_error&) { initialize_rejected = true; }
                subsystem.start();
                subsystem.reset();
                subsystem.terminate();
                direct_calls_preserved_transport = client->event_stops == 0 && client->control_stops == 0;
                callback_returned = true;
            } catch (...) {}
        };
        const auto hook = [&](Fixture& current) {
            worker = std::thread([&]() {
                bool ok = false;
                try { ok = current.register_event_publish({"worker", "bytes", 32}); }
                catch (...) {}
                std::lock_guard<std::mutex> lock(mutex);
                registration_ok = ok; done = true;
                changed.notify_one();
            });
            std::unique_lock<std::mutex> lock(mutex);
            // Return on timeout before joining, breaking the lock inversion even
            // when a broken implementation blocks the callback on this hook.
            return changed.wait_for(lock, std::chrono::seconds(3), [&]() { return done; })
                && registration_ok && callback_returned && !callback_accepted
                && initialize_rejected && direct_calls_preserved_transport;
        };
        if (reset) subsystem.reset_hook = hook;
        else subsystem.start_hook = hook;
        if (reset) subsystem.reset(); else subsystem.start();
        worker.join();
        require(done && registration_ok && callback_returned && !callback_accepted
            && initialize_rejected && direct_calls_preserved_transport
            && client->has_row("worker", true)
            && subsystem.state() == (reset ? DeviceState::ready : DeviceState::operating),
            "registration callback blocked or lifecycle rejection disrupted valid registration");
        client->registration_callback = {};
        subsystem.terminate();
    }
}

void initialize_waits_for_other_thread_registration(ManifestFiles& files) {
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.init_registration",
        files.write("fixture.init_registration",
            "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
            "    endpoints:\n      publishes: [worker]\n"), client));
    subsystem.initialize();
    client->block_ack();
    std::atomic<bool> registered{false}, rejected{false};
    std::mutex mutex;
    std::condition_variable changed;
    bool started = false;
    std::thread registration([&]() { registered = subsystem.register_event_publish({"worker", "bytes", 32}); });
    const bool ack = client->wait_for_ack();
    std::thread initialize([&]() {
        { std::lock_guard<std::mutex> lock(mutex); started = true; changed.notify_one(); }
        try { subsystem.initialize(); } catch (...) { rejected = true; }
    });
    bool attempted;
    { std::unique_lock<std::mutex> lock(mutex); attempted = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return started; }); }
    client->unblock_ack();
    registration.join();
    initialize.join();
    require(ack && attempted && registered && !rejected && subsystem.initialize_calls == 2
        && subsystem.state() == DeviceState::ready && client->has_row("worker", true),
        "other thread's Event registration incorrectly rejected initialize");
    subsystem.terminate();
}

void registration_at_initialization_commit_is_not_lost(ManifestFiles& files) {
    const auto manifest = files.write("fixture.init_commit",
        "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
        "    endpoints:\n      publishes: [late]\n");
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.init_commit", manifest, client));
    client->block_ack();
    std::thread initialize([&]() { subsystem.initialize(); });
    const bool ack_reached = client->wait_for_ack();
    std::mutex mutex;
    std::condition_variable changed;
    bool started = false;
    std::atomic<bool> registered{false};
    std::thread late([&]() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            started = true;
            changed.notify_one();
        }
        registered = subsystem.register_event_publish({"late", "bytes", 32});
    });
    bool attempted;
    {
        std::unique_lock<std::mutex> lock(mutex);
        attempted = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return started; });
    }
    client->unblock_ack();
    initialize.join();
    late.join();
    require(ack_reached && attempted && subsystem.state() == DeviceState::ready && registered
        && client->has_row("late", true), "registration at initialization commit was lost");
    subsystem.terminate();
}

void hooks_can_wait_for_event_registration(ManifestFiles& files) {
    const auto manifest = files.write("fixture.hook_workers",
        "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
        "    endpoints:\n      publishes: [start_worker, reset_worker]\n");
    auto client = std::make_shared<RecordingClient>();
    std::thread worker;
    std::mutex mutex;
    std::condition_variable changed;
    bool done = false, registration_ok = false;
    const auto wait_for_worker = [&](Fixture& current, const char* topic) {
        {
            std::lock_guard<std::mutex> lock(mutex);
            done = false;
            registration_ok = false;
        }
        worker = std::thread([&, topic]() {
            bool ok = false;
            try { ok = current.register_event_publish({topic, "bytes", 32}); }
            catch (...) {}
            std::lock_guard<std::mutex> lock(mutex);
            registration_ok = ok;
            done = true;
            changed.notify_one();
        });
        std::unique_lock<std::mutex> lock(mutex);
        // On failure, return from the hook first so the worker can leave any
        // erroneously held registration lock; the caller always joins it.
        return changed.wait_for(lock, std::chrono::seconds(3), [&]() { return done; }) && registration_ok;
    };
    Fixture subsystem(options("fixture.hook_workers", manifest, client));
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "worker fixture failed to initialize");
    subsystem.start_hook = [&](Fixture& current) {
        current.terminate();
        return client->event_stops == 0 && client->control_stops == 0
            && wait_for_worker(current, "start_worker");
    };
    subsystem.start();
    worker.join();
    require(subsystem.state() == DeviceState::operating && client->has_row("start_worker", true),
        "start hook could not wait for a worker registering an Event");
    require(subsystem.fault(), "worker reset fixture could not fault");
    const int starts = client->event_starts, stops = client->event_stops, control_stops = client->control_stops;
    subsystem.reset_hook = [&](Fixture& current) { return wait_for_worker(current, "reset_worker"); };
    const bool reset_ok = subsystem.request_lifecycle_transition("reset");
    worker.join();
    require(reset_ok, "reset hook could not wait for Event registration");
    require(subsystem.state() == DeviceState::ready && client->has_row("reset_worker", true),
        "reset worker registration did not commit");
    require(client->event_starts == starts && client->event_stops == stops && client->control_stops == control_stops,
        "reset worker registration changed transport ownership");
    subsystem.terminate();
}
}  // namespace
int main() {
    return run_offline_suite("Event registration ordering/rollback", {
        event_ordering_rollback_and_lifecycle,
        initialize_can_wait_for_event_registration,
        registration_callback_rejects_lifecycle_during_hook,
        initialize_waits_for_other_thread_registration,
        registration_at_initialization_commit_is_not_lost,
        hooks_can_wait_for_event_registration});
}
