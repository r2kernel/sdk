// Offline Subsystem lifecycle regressions: reset admission and outcomes,
// serialized concurrent resets and terminations, and lifecycle calls made
// while a termination or hook is in flight.
#include "subsystem_offline_fixture.hpp"
using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace {
void reset_admission_and_outcomes(ManifestFiles& files) {
    // Both entry points must use the same admission and hook-error policy.
    for (const bool generic : {false, true}) {
        for (const auto initial : {DeviceState::faulted, DeviceState::error, DeviceState::degraded,
                                   DeviceState::paused, DeviceState::stopped}) {
            auto client = std::make_shared<RecordingClient>();
            Fixture subsystem(options("fixture.reset", files.write("fixture.reset", ""), client));
            subsystem.fail_initialize = initial == DeviceState::error;
            subsystem.initialize();
            if (initial == DeviceState::faulted) require(subsystem.fault(), "reset fixture fault failed");
            if (initial == DeviceState::degraded) require(subsystem.degrade(), "reset fixture degrade failed");
            if (initial == DeviceState::paused) require(subsystem.pause(), "reset fixture pause failed");
            if (initial == DeviceState::stopped) require(subsystem.stop(), "reset fixture stop failed");
            require(subsystem.state() == initial, "reset fixture entered the wrong source state");
            bool entered_resetting = false;
            subsystem.reset_hook = [&](Fixture& current) {
                entered_resetting = current.state() == DeviceState::resetting;
                return true;
            };
            const int starts = client->event_starts, stops = client->event_stops, control_stops = client->control_stops;
            if (generic) require(subsystem.request_lifecycle_transition("reset"), "allowed generic reset was refused");
            else subsystem.reset();
            require(entered_resetting && subsystem.reset_calls == 1 && subsystem.state() == DeviceState::ready,
                "allowed reset did not run once through Resetting to Ready");
            require(client->event_starts == starts && client->event_stops == stops && client->control_stops == control_stops,
                "reset unexpectedly changed transport ownership");
            if (initial == DeviceState::error) {
                subsystem.start();
                require(subsystem.state() == DeviceState::ready, "reset bypassed unsuccessful initialization");
            }
            subsystem.terminate();
        }
        for (const int outcome : {0, 1, 2}) {
            auto client = std::make_shared<RecordingClient>();
            Fixture subsystem(options("fixture.reset_failure", files.write("fixture.reset_failure", ""), client));
            require(subsystem.fault(), "failed reset fixture fault failed");
            subsystem.reset_hook = [outcome](Fixture& current) -> bool {
                require(current.state() == DeviceState::resetting, "failed reset hook did not enter Resetting");
                if (outcome == 1) throw std::runtime_error("reset hook exception");
                if (outcome == 2) throw 7;
                return false;
            };
            if (generic) require(!subsystem.request_lifecycle_transition("reset"), "failed generic reset reported success");
            else subsystem.reset();
            require(subsystem.reset_calls == 1 && subsystem.state() == DeviceState::faulted,
                "false or throwing reset must leave Faulted, not Error");
            require(client->event_stops == 0 && client->control_stops == 0, "failed reset tore down transport");
        }
    }
}

void reset_rejects_incompatible_and_nested_calls(ManifestFiles& files) {
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.reset_rejected", files.write("fixture.reset_rejected", ""), client));
    const auto rejected_without_effects = [&]() {
        const auto previous = subsystem.state();
        const int calls = subsystem.reset_calls, starts = client->event_starts;
        const int stops = client->event_stops, control_stops = client->control_stops;
        subsystem.reset();
        require(!subsystem.request_lifecycle_transition("reset"), "incompatible generic reset was admitted");
        require(subsystem.state() == previous && subsystem.reset_calls == calls,
            "rejected reset changed state or invoked its hook");
        require(client->event_starts == starts && client->event_stops == stops && client->control_stops == control_stops,
            "rejected reset changed transport ownership");
    };
    rejected_without_effects(); // Created.
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "rejected reset fixture did not initialize");
    rejected_without_effects();
    subsystem.start();
    require(subsystem.state() == DeviceState::operating, "rejected reset fixture did not start");
    rejected_without_effects();
    require(subsystem.fault(), "nested reset fixture fault failed");
    bool nested_rejected = false;
    subsystem.reset_hook = [&](Fixture& current) {
        const int before = current.reset_calls;
        current.reset();
        nested_rejected = !current.request_lifecycle_transition("reset") && !current.resume();
        current.start();
        return nested_rejected && current.reset_calls == before && current.state() == DeviceState::resetting;
    };
    subsystem.reset();
    require(nested_rejected && subsystem.reset_calls == 1 && subsystem.state() == DeviceState::ready,
        "nested incompatible lifecycle request ran a hook or disrupted reset");
    subsystem.terminate();
}

void concurrent_resets_are_serialized(ManifestFiles& files) {
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.reset_serial", files.write("fixture.reset_serial", ""), client));
    require(subsystem.fault(), "serialized reset fixture fault failed");
    std::mutex mutex;
    std::condition_variable changed;
    bool entered = false, release = false;
    subsystem.reset_hook = [&](Fixture&) {
        std::unique_lock<std::mutex> lock(mutex);
        entered = true;
        changed.notify_all();
        changed.wait(lock, [&]() { return release; });
        return true;
    };
    std::thread first([&]() { subsystem.reset(); });
    {
        std::unique_lock<std::mutex> lock(mutex);
        changed.wait(lock, [&]() { return entered; });
    }
    std::atomic<bool> second_started{false}, second_done{false}, second_ok{true};
    std::thread second([&]() {
        second_started = true;
        second_ok = subsystem.request_lifecycle_transition("reset");
        second_done = true;
    });
    while (!second_started) std::this_thread::yield();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    const bool finished_while_hook_active = second_done;
    const int calls_while_hook_active = subsystem.reset_calls;
    {
        std::lock_guard<std::mutex> lock(mutex);
        release = true;
        changed.notify_all();
    }
    first.join();
    second.join();
    require(!finished_while_hook_active && calls_while_hook_active == 1 && !second_ok
        && subsystem.reset_calls == 1 && subsystem.state() == DeviceState::ready,
        "concurrent reset did not serialize and recheck admission after the active reset");
    require(client->event_stops == 0 && client->control_stops == 0, "concurrent reset tore down transport");
}

void terminate_waits_for_lifecycle_hook(ManifestFiles& files) {
    for (const bool starting : {false, true}) {
        const auto name = starting ? "fixture.start_terminate" : "fixture.init_terminate";
        const auto manifest = files.write(name,
            "provides:\n  local:\n    contract: fixture.events.v1\n    visibility: internal\n"
            "    endpoints:\n      publishes: [worker]\n");
        auto client = std::make_shared<RecordingClient>();
        std::mutex mutex;
        std::condition_variable changed;
        bool entered = false, release = false, hook_completed = false;
        bool terminate_started = false, terminate_done = false;
        bool nested_termination_preserved_transport = false;
        std::atomic<int> terminate_calls{0};
        const auto hook = [&](Fixture& current) {
            current.terminate();
            nested_termination_preserved_transport = client->event_stops == 0
                && client->control_stops == 0 && terminate_calls == 0;
            std::unique_lock<std::mutex> lock(mutex);
            entered = true;
            changed.notify_all();
            hook_completed = changed.wait_for(lock, std::chrono::seconds(10), [&]() { return release; });
            lock.unlock();
            return hook_completed && current.register_event_publish({"worker", "bytes", 32});
        };
        Fixture subsystem(options(name, manifest, client), starting ? std::function<bool(Fixture&)>{} : hook);
        subsystem.terminate_hook = [&](Fixture& current) { ++terminate_calls; current.terminate(); return true; };
        if (starting) {
            subsystem.initialize();
            require(subsystem.state() == DeviceState::ready, "start termination fixture failed to initialize");
            subsystem.start_hook = hook;
        }
        std::thread transition([&]() { if (starting) subsystem.start(); else subsystem.initialize(); });
        bool hook_entered;
        {
            std::unique_lock<std::mutex> lock(mutex);
            hook_entered = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return entered; });
        }
        std::thread terminate([&]() {
            {
                std::lock_guard<std::mutex> lock(mutex);
                terminate_started = true;
                changed.notify_all();
            }
            subsystem.terminate();
            {
                std::lock_guard<std::mutex> lock(mutex);
                terminate_done = true;
                changed.notify_all();
            }
        });
        bool attempted, returned_early;
        {
            std::unique_lock<std::mutex> lock(mutex);
            attempted = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return terminate_started; });
            returned_early = changed.wait_for(lock, std::chrono::milliseconds(250), [&]() { return terminate_done; });
            release = true;
            changed.notify_all();
        }
        transition.join();
        terminate.join();
        require(hook_entered && attempted && !returned_early && hook_completed
            && nested_termination_preserved_transport && client->has_row("worker", true)
            && terminate_calls == 1 && client->event_stops == 1 && client->control_stops == 0,
            "terminate did not wait for the lifecycle hook and run exactly once afterward");
    }
}

// Followers remain attached through the hook and transport cleanup, including
// exceptional exits; a later call starts afresh.
void concurrent_termination_coalesces(ManifestFiles& files) {
    for (int outcome = 0; outcome != 4; ++outcome) {
        auto client = std::make_shared<RecordingClient>();
        Fixture subsystem(options("fixture.terminate_flight",
            files.write("fixture.terminate_flight", ""), client));
        std::mutex mutex;
        std::condition_variable changed;
        bool hook_entered = false, release_hook = false, stop_entered = false, release_stop = false;
        bool follower_started = false, follower_done = false, winner_done = false;
        bool winner_threw = false, follower_threw = false;
        std::atomic<int> calls{0};
        subsystem.terminate_hook = [&](Fixture& current) -> bool {
            ++calls;
            current.terminate();
            std::unique_lock<std::mutex> lock(mutex);
            hook_entered = true;
            changed.notify_all();
            if (!changed.wait_for(lock, std::chrono::seconds(10), [&] { return release_hook; })) return false;
            if (outcome == 2) throw std::runtime_error("terminate hook failure");
            return outcome != 1;
        };
        client->stop_callback = [&] {
            std::unique_lock<std::mutex> lock(mutex);
            stop_entered = true;
            changed.notify_all();
            changed.wait_for(lock, std::chrono::seconds(10), [&] { return release_stop; });
            if (outcome == 3) throw std::runtime_error("transport stop failure");
        };
        std::thread winner([&] {
            try { subsystem.terminate(); } catch (...) { winner_threw = true; }
            std::lock_guard<std::mutex> lock(mutex);
            winner_done = true;
            changed.notify_all();
        });
        bool entered;
        {
            std::unique_lock<std::mutex> lock(mutex);
            entered = changed.wait_for(lock, std::chrono::seconds(3), [&] { return hook_entered; });
        }
        std::thread follower([&] {
            {
                std::lock_guard<std::mutex> lock(mutex);
                follower_started = true;
                changed.notify_all();
            }
            try { subsystem.terminate(); } catch (...) { follower_threw = true; }
            std::lock_guard<std::mutex> lock(mutex);
            follower_done = true;
            changed.notify_all();
        });
        bool started, returned_in_hook, reached_stop, returned_in_stop;
        {
            std::unique_lock<std::mutex> lock(mutex);
            started = changed.wait_for(lock, std::chrono::seconds(3), [&] { return follower_started; });
            returned_in_hook = changed.wait_for(lock, std::chrono::milliseconds(250), [&] { return follower_done; });
            release_hook = true;
            changed.notify_all();
            reached_stop = changed.wait_for(lock, std::chrono::seconds(3), [&] { return stop_entered; });
            returned_in_stop = changed.wait_for(lock, std::chrono::milliseconds(250), [&] { return follower_done; });
            release_stop = true;
            changed.notify_all();
            if (!changed.wait_for(lock, std::chrono::seconds(3), [&] { return winner_done && follower_done; })) {
                std::cerr << "termination flight did not release its callers\n";
                std::abort();
            }
        }
        winner.join();
        follower.join();
        require(entered && started && !returned_in_hook && reached_stop && !returned_in_stop
            && calls == 1 && client->event_stops == 1 && !follower_threw && winner_threw == (outcome == 3),
            "overlapping termination did not coalesce through cleanup and failure");
        if (outcome == 1 || outcome == 2)
            require(subsystem.state() == DeviceState::error, "failed terminate hook did not retain its error semantics");
        client->stop_callback = {};
        subsystem.terminate_hook = [&](Fixture&) { ++calls; return true; };
        subsystem.terminate();
        require(calls == 2 && client->event_stops == 2, "later termination did not start a new flight");
    }
}

// The first call claims its flight before it waits on start's lifecycle lock.
// A nested call from that hook must not follow the flight and deadlock it.
void concurrent_termination_waits_for_transition(ManifestFiles& files) {
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.terminate_transition",
        files.write("fixture.terminate_transition", ""), client));
    subsystem.initialize();
    std::mutex mutex;
    std::condition_variable changed;
    bool hook_entered = false, request_nested = false, nested_done = false, release_hook = false;
    bool owner_started = false, follower_started = false, owner_done = false, follower_done = false;
    std::atomic<int> calls{0};
    subsystem.start_hook = [&](Fixture& current) {
        {
            std::unique_lock<std::mutex> lock(mutex);
            hook_entered = true;
            changed.notify_all();
            if (!changed.wait_for(lock, std::chrono::seconds(10), [&] { return request_nested; })) return false;
        }
        current.terminate();
        std::unique_lock<std::mutex> lock(mutex);
        nested_done = true;
        changed.notify_all();
        return changed.wait_for(lock, std::chrono::seconds(10), [&] { return release_hook; });
    };
    subsystem.terminate_hook = [&](Fixture&) { ++calls; return true; };
    std::thread transition([&] { subsystem.start(); });
    bool entered;
    {
        std::unique_lock<std::mutex> lock(mutex);
        entered = changed.wait_for(lock, std::chrono::seconds(3), [&] { return hook_entered; });
    }
    std::thread owner([&] {
        { std::lock_guard<std::mutex> lock(mutex); owner_started = true; changed.notify_all(); }
        subsystem.terminate();
        { std::lock_guard<std::mutex> lock(mutex); owner_done = true; changed.notify_all(); }
    });
    bool owner_attempted, owner_early;
    {
        std::unique_lock<std::mutex> lock(mutex);
        owner_attempted = changed.wait_for(lock, std::chrono::seconds(3), [&] { return owner_started; });
        owner_early = changed.wait_for(lock, std::chrono::milliseconds(250), [&] { return owner_done; });
    }
    std::thread follower([&] {
        { std::lock_guard<std::mutex> lock(mutex); follower_started = true; changed.notify_all(); }
        subsystem.terminate();
        { std::lock_guard<std::mutex> lock(mutex); follower_done = true; changed.notify_all(); }
    });
    bool follower_attempted, follower_early, nested_returned;
    {
        std::unique_lock<std::mutex> lock(mutex);
        follower_attempted = changed.wait_for(lock, std::chrono::seconds(3), [&] { return follower_started; });
        follower_early = changed.wait_for(lock, std::chrono::milliseconds(250), [&] { return follower_done; });
        request_nested = true;
        changed.notify_all();
        nested_returned = changed.wait_for(lock, std::chrono::seconds(3), [&] { return nested_done; });
        release_hook = true;
        changed.notify_all();
        if (!changed.wait_for(lock, std::chrono::seconds(3), [&] { return owner_done && follower_done; })) {
            std::cerr << "termination/lifecycle lock cycle\n";
            std::abort();
        }
    }
    transition.join();
    owner.join();
    follower.join();
    require(entered && owner_attempted && follower_attempted && !owner_early && !follower_early
        && nested_returned && calls == 1 && client->event_stops == 1,
        "termination calls did not share the flight waiting for lifecycle admission");
}

void initialize_is_rejected_during_termination(ManifestFiles& files) {
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.terminate_init",
        files.write("fixture.terminate_init", ""), client));
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "termination fixture did not initialize");
    std::mutex mutex;
    std::condition_variable changed;
    bool entered = false, release = false, completed = false, initialize_done = false, rejected = false;
    subsystem.terminate_hook = [&](Fixture&) {
        std::unique_lock<std::mutex> lock(mutex);
        entered = true;
        changed.notify_one();
        completed = changed.wait_for(lock, std::chrono::seconds(10), [&]() { return release; });
        return completed;
    };
    std::thread terminate([&]() { subsystem.terminate(); });
    bool hook_entered;
    {
        std::unique_lock<std::mutex> lock(mutex);
        hook_entered = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return entered; });
    }
    std::thread initialize([&]() {
        try { subsystem.initialize(); }
        catch (const std::runtime_error&) { rejected = true; }
        std::lock_guard<std::mutex> lock(mutex);
        initialize_done = true;
        changed.notify_all();
    });
    bool returned_during_teardown;
    {
        std::unique_lock<std::mutex> lock(mutex);
        returned_during_teardown = changed.wait_for(lock, std::chrono::seconds(3), [&]() { return initialize_done; });
        release = true;
        changed.notify_all();
    }
    initialize.join();
    terminate.join();
    require(hook_entered && completed && returned_during_teardown && rejected
        && subsystem.initialize_calls == 1 && client->event_stops == 1,
        "initialize was admitted or blocked during teardown");
}
}  // namespace
int main() {
    return run_offline_suite("Subsystem lifecycle", {
        reset_admission_and_outcomes,
        reset_rejects_incompatible_and_nested_calls,
        concurrent_resets_are_serialized,
        terminate_waits_for_lifecycle_hook,
        concurrent_termination_coalesces,
        concurrent_termination_waits_for_transition,
        initialize_is_rejected_during_termination});
}
