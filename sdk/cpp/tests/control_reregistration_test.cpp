// A running C++ subsystem registers again after Control restarts.
//
// Regression: after a Control restart the
// heartbeat only sent best-effort status updates that Control refused (the
// registration was gone), so the subsystem never became visible again. Uses a
// restartable fake Control over a real Unix socket; registrations vanish on
// restart like the backend's in-memory state.
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "../src/core/control_registration_keeper.hpp"
#include "fake_control_socket.hpp"
#include "r2kernel_core.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

using namespace r2kernel::wrapper;
using r2kernel::core::detail::ControlRegistrationKeeper;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

std::string field(const std::string& json, const std::string& key) {
    const auto at = json.find("\"" + key + "\":");
    if (at == std::string::npos) return {};
    auto begin = at + key.size() + 3;
    if (json[begin] == '"') {
        const auto end = json.find('"', begin + 1);
        return json.substr(begin + 1, end - begin - 1);
    }
    const auto end = json.find_first_of(",}", begin);
    return json.substr(begin, end - begin);
}

std::string command(const std::string& request) { return field(request, "cmd"); }

// Registration reply fields after the Event pool (closes the JSON object).
const std::string kControlSettings =
    "\"manager_peer_auth_required\":false,"
    "\"control_local_endpoint\":\"/tmp/r2k-cpp-rereg-unused.sock\","
    "\"control_local_payload_policy\":\"forwarded_shm_v5\","
    "\"control_local_protocol\":\"binary_shm_v5\",\"control_shm_min_region_bytes\":4096,"
    "\"control_shared_buffer_slot_size\":16777216,\"control_shm_max_channels\":128,"
    "\"control_shm_max_owned_bytes\":67108864,\"control_shm_max_mapped_bytes\":67108864}";

class RestartableControl {
public:
    // The first registrations are refused with `refusals` (a Control reply reason each).
    explicit RestartableControl(std::string pool, std::vector<std::string> refusals = {})
        : path_(r2kernel_test::unique_temp_path("rereg").string() + ".sock"), pool_(std::move(pool)),
          refusals_(std::move(refusals)) {
        ::setenv("R2KERNEL_CONTROL_SOCKET", path_.c_str(), 1);
        start();
    }
    ~RestartableControl() { stop(); }

    void start() {
        fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, path_.c_str(), path_.size() + 1);
        require(fd_ >= 0 && ::bind(fd_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0
                && ::listen(fd_, 16) == 0, "fake Control listen");
        stop_ = false;
        thread_ = std::thread([this] { serve(); });
    }
    // Control stops: socket gone, every registration lost.
    void stop() {
        if (fd_ < 0) return;
        stop_ = true;
        ::shutdown(fd_, SHUT_RDWR);
        ::close(fd_);
        thread_.join();
        fd_ = -1;
        ::unlink(path_.c_str());
        std::lock_guard<std::mutex> lock(mutex_);
        registered_.clear();
        ++generation_;
    }
    std::vector<std::string> requests() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return requests_;
    }
    // A slow Control: holds every registration reply this long (0: none).
    // registration_started() reports a registration request held so.
    void delay_registration(std::chrono::milliseconds delay) {
        registration_started_ = false;
        registration_delay_ms_ = static_cast<int>(delay.count());
    }
    bool registration_started() const { return registration_started_.load(); }

private:
    std::string reply(const std::string& request) {
        const auto cmd = command(request);
        if (cmd == "register_subsystem" && !refusals_.empty()) {
            const auto reason = refusals_.front();
            refusals_.erase(refusals_.begin());
            return "{\"status\":\"error\",\"error\":\"subsystem registration refused (" + reason
                + ")\",\"reason\":\"" + reason + "\"}";
        }
        if (cmd == "register_subsystem") {
            registered_.insert(field(request, "subsystem"));
            return "{\"res\":\"ok\",\"ok\":true,\"topic_buffer_pool_name\":\"" + pool_ + "\","
                   "\"topic_buffer_pool_slot_size\":4096,\"topic_buffer_pool_slot_count\":8," + kControlSettings;
        }
        const bool known = registered_.count(field(request, "subsystem")) > 0;
        if (cmd == "update_subsystem_status")
            return known ? "{\"ok\":true,\"res\":\"ok\"}"
                         : "{\"status\":\"error\",\"error\":\"status requires an existing authenticated registration\"}";
        if (cmd == "request_publish_slots" && known)
            return "{\"res\":\"ok\",\"publish_slot_index\":" + std::to_string(1 + generation_)
                + ",\"queue_depth\":1,\"entry_slot_count\":1,\"subscribers\":[]}";
        if (cmd == "register_subscription" && known) return "{\"res\":\"ok\",\"publishers\":[]}";
        if (cmd == "clock_status" || cmd == "inform_subsystem_down") return "{\"res\":\"ok\"}";
        return "{\"status\":\"error\",\"error\":\"operation requires an existing authenticated registration\"}";
    }

    void serve() {
        while (!stop_) {
            const int client = ::accept(fd_, nullptr, nullptr);
            if (client < 0) return;
            std::string request;
            char buffer[4096];
            for (;;) {
                const auto count = ::recv(client, buffer, sizeof(buffer), 0);
                if (count <= 0) break;
                request.append(buffer, static_cast<std::size_t>(count));
            }
            std::string body;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                requests_.push_back(request);
                body = reply(request);
            }
            if (const int delay = registration_delay_ms_.load(); delay > 0 && command(request) == "register_subsystem") {
                registration_started_ = true;
                std::this_thread::sleep_for(std::chrono::milliseconds(delay));
            }
            (void)::send(client, body.data(), body.size(), MSG_NOSIGNAL);
            ::close(client);
        }
    }

    std::string path_;
    std::string pool_;
    std::vector<std::string> refusals_;
    int fd_ = -1;
    std::atomic<bool> stop_ {false};
    std::thread thread_;
    mutable std::mutex mutex_;
    std::vector<std::string> requests_;
    std::set<std::string> registered_;
    int generation_ = 0;
    std::atomic<int> registration_delay_ms_ {0};
    std::atomic<bool> registration_started_ {false};
};

struct Logs {
    std::mutex mutex;
    std::vector<std::pair<std::string, std::string>> records;
    std::size_t count(const std::string& level, const std::string& needle) {
        std::lock_guard<std::mutex> lock(mutex);
        std::size_t total = 0;
        for (const auto& [record_level, message] : records)
            if (record_level == level && message.find(needle) != std::string::npos) ++total;
        return total;
    }
};

class Plain final : public Subsystem {
public:
    using Subsystem::Subsystem;
private:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

fs::path write_manifest(const fs::path& directory, const std::string& name) {
    const auto path = directory / "subsystem.yaml";
    std::ofstream file(path);
    file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << name << "\n  role: component\n";
    return path;
}

// A Plain subsystem initialized against a RestartableControl, with its Event
// pool, manifest directory and captured logs; everything is removed on exit.
struct ControlledSubsystem {
    explicit ControlledSubsystem(const std::string& tag, std::vector<std::string> refusals = {},
                                 int startup_retries = 1)
        : directory(r2kernel_test::unique_temp_path(tag + "-dir")),
          pool("/r2k-cpp-" + tag + "-pool-" + std::to_string(::getpid())) {
        fs::create_directories(directory);
        require(r2kernel_shm_create(pool.c_str(), 4096, 8, &shm) == 0 && shm != nullptr, "shared-memory pool");
        ::setenv("R2KERNEL_RESOURCE_LEASE_ID", "lease-cpp-1", 1);
        ::setenv("R2KERNEL_STARTUP_RETRY_COUNT", std::to_string(startup_retries).c_str(), 1);
        control = std::make_unique<RestartableControl>(pool, std::move(refusals));
        r2kernel::core::ClientOptions client_options;
        client_options.subsystem_name = "qa.cpp." + tag;
        client = std::make_shared<r2kernel::core::R2KernelClient>(client_options);
        SubsystemOptions options;
        options.name = client_options.subsystem_name;
        options.manifest_path = write_manifest(directory, options.name);
        options.schema_roots = std::vector<std::string> {};
        options.core_client = client;
        options.log_sink = [this](std::string_view level, std::string_view, std::string_view, std::string_view message) {
            std::lock_guard<std::mutex> lock(logs.mutex);
            logs.records.emplace_back(std::string(level), std::string(message));
        };
        subsystem = std::make_unique<Plain>(std::move(options));
        subsystem->initialize();
        require(subsystem->state() == DeviceState::ready, "initialize");
    }
    ~ControlledSubsystem() {
        subsystem.reset();
        client.reset();
        control.reset();
        r2kernel_shm_close(shm);
        r2kernel_shm_unlink(pool.c_str());
        std::error_code ignored;
        fs::remove_all(directory, ignored);
    }
    std::vector<std::string> requests_since(std::size_t mark) const {
        const auto all = control->requests();
        return {all.begin() + static_cast<std::ptrdiff_t>(std::min(mark, all.size())), all.end()};
    }
    bool status_sent_since(std::size_t mark, const std::string& lifecycle) const {
        for (const auto& request : requests_since(mark))
            if (command(request) == "update_subsystem_status" && field(request, "lifecycle") == lifecycle) return true;
        return false;
    }

    fs::path directory;
    std::string pool;
    R2KernelShmHandle* shm = nullptr;
    std::unique_ptr<RestartableControl> control;
    std::shared_ptr<r2kernel::core::R2KernelClient> client;
    Logs logs;
    std::unique_ptr<Plain> subsystem;
};

template <typename Predicate>
bool wait_until(Predicate predicate, std::chrono::milliseconds timeout) {
    const auto deadline = Clock::now() + timeout;
    while (!predicate()) {
        if (Clock::now() >= deadline) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return true;
}

std::chrono::milliseconds elapsed_since(Clock::time_point started) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - started);
}

constexpr std::chrono::milliseconds kQuickLifecycleChange {500};

// fault() during a re-registration that Control answers slowly
// returns at once; its status is left to the next heartbeat (logged), which
// carries it. Old code: fault() waited for the whole re-registration.
void lifecycle_status_does_not_wait_for_a_registration_in_progress() {
    ControlledSubsystem fixture("lifecycle-wait");
    auto& control = *fixture.control;
    control.delay_registration(std::chrono::milliseconds(1500));
    control.stop();
    control.start();
    require(wait_until([&control] { return control.registration_started(); }, std::chrono::seconds(3)),
        "the heartbeat did not register again after the Control restart");
    const auto mark = control.requests().size();
    const auto started = Clock::now();
    require(fixture.subsystem->fault("qa fault"), "fault");
    const auto waited = elapsed_since(started);
    require(waited < kQuickLifecycleChange,
        "fault() waited " + std::to_string(waited.count()) + " ms for the re-registration");
    require(fixture.requests_since(mark).empty(), "the lifecycle status was sent during the re-registration");
    require(fixture.logs.count("INFO", "Lifecycle status Faulted is left to the next heartbeat") == 1,
        "the deferred lifecycle status is logged");
    control.delay_registration(std::chrono::milliseconds(0));
    require(wait_until([&] { return fixture.status_sent_since(mark, "Faulted"); }, std::chrono::seconds(4)),
        "the next heartbeat did not carry the Faulted status");

    // Registration lost with the next attempt not yet due: the status waits for it.
    const auto failed_attempts = fixture.logs.count("ERROR", "Registration attempt");
    control.stop();
    require(wait_until([&] { return fixture.logs.count("ERROR", "Registration attempt") > failed_attempts; },
        std::chrono::seconds(3)), "the heartbeat did not find the lost registration");
    const auto stopping = Clock::now();
    require(!fixture.subsystem->stop(), "on_stop declines");
    require(elapsed_since(stopping) < kQuickLifecycleChange, "stop() waited for the lost registration");
    require(fixture.logs.count("INFO", "the registration with Control is being restored") >= 1,
        "a status left to the restored registration is logged");
    const auto restored = control.requests().size();
    control.start();
    require(wait_until([&] { return fixture.status_sent_since(restored, "Faulted"); }, std::chrono::seconds(7)),
        "the restored registration did not carry the Faulted status");
}

// A lifecycle status that Control refuses is logged at error level; the
// lifecycle change does not register again inline, the next heartbeat does.
void refused_lifecycle_status_is_logged_and_left_to_the_heartbeat() {
    ControlledSubsystem fixture("lifecycle-refused");
    auto& control = *fixture.control;
    // Right after a heartbeat, so the next one is about a second away.
    const auto beat = control.requests().size();
    require(wait_until([&] { return fixture.status_sent_since(beat, "Ready"); }, std::chrono::seconds(3)),
        "no heartbeat");
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    control.stop();
    control.start();
    const auto mark = control.requests().size();
    const auto started = Clock::now();
    require(fixture.subsystem->degrade("qa degrade"), "degrade");
    require(elapsed_since(started) < kQuickLifecycleChange, "degrade() waited");
    const auto sent = fixture.requests_since(mark);
    std::vector<std::string> commands;
    for (const auto& request : sent) commands.push_back(command(request));
    require(commands == std::vector<std::string> {"update_subsystem_status"},
        "the lifecycle status registered again inline (" + std::to_string(commands.size()) + " requests)");
    require(field(sent.front(), "lifecycle") == "Degraded", "the lifecycle status carries Degraded");
    require(fixture.logs.count("ERROR", "Lifecycle status Degraded not accepted (Control refused the status: "
                "status requires an existing authenticated registration)") == 1,
        "the refused lifecycle status is logged at error level");
    require(fixture.logs.count("ERROR", "registering with Control again") == 0,
        "the lifecycle status started the re-registration");
    require(wait_until([&] {
        for (const auto& request : fixture.requests_since(mark)) if (command(request) == "register_subsystem") return true;
        return false;
    }, std::chrono::seconds(3)), "the next heartbeat did not register again");
    require(wait_until([&] { return fixture.logs.count("INFO", "Registered with Control again") == 1; },
        std::chrono::seconds(2)), "the re-registration is logged");
}

void keeper_backs_off_to_the_bound_and_logs_every_failure() {
    auto now = Clock::time_point {} + std::chrono::seconds(100);
    ControlRegistrationKeeper keeper([&now] { return now; });
    std::vector<Clock::time_point> attempts;
    std::vector<std::pair<bool, std::string>> logs;
    while (attempts.size() < 6) {
        keeper.heartbeat([] { return std::optional<std::string>("Control unreachable"); },
            [&]() -> std::optional<std::string> {
                attempts.push_back(now);
                if (attempts.size() < 6) return std::string("Control unreachable");
                return std::nullopt;
            },
            [&logs](bool error, const std::string& message) { logs.emplace_back(error, message); });
        now += r2kernel::core::detail::kHeartbeatPeriod;
    }
    const std::vector<long long> expected {1000, 2000, 4000, 4000, 4000};
    for (std::size_t index = 1; index < attempts.size(); ++index) {
        const auto gap = std::chrono::duration_cast<std::chrono::milliseconds>(attempts[index] - attempts[index - 1]);
        require(gap.count() == expected[index - 1], "backoff gap " + std::to_string(gap.count()));
        require(gap <= r2kernel::core::detail::kRegistrationStartBound, "gap exceeds the documented bound");
    }
    require(r2kernel::core::detail::kRegistrationStartBound == std::chrono::milliseconds(5000), "bound is 5 s");
    std::size_t failures = 0;
    for (const auto& [error, message] : logs) failures += error && message.rfind("Registration attempt", 0) == 0;
    require(failures == 5, "every failed attempt is logged");
    require(!keeper.registration_lost() && !logs.back().first
            && logs.back().second.find("Registered with Control again") == 0, "success is logged");
}

void subsystem_registers_again_after_control_restart() {
    {
        ControlledSubsystem fixture("rereg");
        auto& control = *fixture.control;
        auto& client = fixture.client;
        auto& logs = fixture.logs;
        auto& subsystem = *fixture.subsystem;
        EventPublishSpec publish;
        publish.topic = "qa.cpp.pub";
        publish.size = 64;
        require(client->register_event_publish(publish), "publish registration");
        EventSubscribeSpec subscribe;
        subscribe.topic = "qa.cpp.sub";
        subscribe.size = 64;
        require(client->register_event_subscribe(subscribe), "subscription registration");
        const auto first = control.requests();
        std::string first_registration;
        for (const auto& request : first) if (command(request) == "register_subsystem") first_registration = request;

        control.stop();
        std::this_thread::sleep_for(std::chrono::milliseconds(2500));
        const auto mark = control.requests().size();
        const auto restarted = Clock::now();
        control.start();
        std::vector<std::string> after;
        while (Clock::now() - restarted < r2kernel::core::detail::kRegistrationStartBound + std::chrono::seconds(3)) {
            const auto all = control.requests();
            after.assign(all.begin() + static_cast<std::ptrdiff_t>(mark), all.end());
            bool subscribed = false;
            for (const auto& request : after) subscribed = subscribed || command(request) == "register_subscription";
            if (subscribed) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        const auto elapsed = Clock::now() - restarted;
        std::vector<std::string> commands;
        for (const auto& request : after) commands.push_back(command(request));
        const auto position = [&commands](const std::string& name, std::size_t from = 0) {
            for (std::size_t index = from; index < commands.size(); ++index) if (commands[index] == name) return index;
            return commands.size();
        };
        const auto registered = position("register_subsystem");
        require(registered < commands.size(), "no registration after the Control restart");
        const auto& registration = after[registered];
        for (const char* key : {"session_id", "pid", "lease_id", "notify_endpoint"})
            require(field(registration, key) == field(first_registration, key), std::string("changed ") + key);
        require(field(registration, "lease_id") == "lease-cpp-1", "lease");
        const auto status = position("update_subsystem_status", registered);
        const auto published = position("request_publish_slots", registered);
        const auto subscribed = position("register_subscription", registered);
        require(status < published && published < commands.size() && subscribed < commands.size(),
            "registration, status, then Event endpoints");
        require(field(after[status], "session_id") == field(first_registration, "session_id"), "status session");
        require(field(after[published], "topic") == "qa.cpp.pub", "publication restored");
        require(field(after[subscribed], "topic") == "qa.cpp.sub", "subscription restored");
        require(elapsed <= r2kernel::core::detail::kRegistrationStartBound + std::chrono::seconds(1),
            "re-registration exceeded the bound");
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        require(logs.count("ERROR", "registering with Control again") == 1, "loss is logged once");
        require(logs.count("ERROR", "Registration attempt") >= 1, "failed attempts are logged");
        require(logs.count("INFO", "Registered with Control again") == 1, "recovery is logged");

        // Restart between two heartbeats: only the refused status shows it.
        const auto before_quick = control.requests().size();
        control.stop();
        control.start();
        bool reregistered = false;
        for (int wait = 0; wait < 80 && !reregistered; ++wait) {
            const auto all = control.requests();
            for (auto index = before_quick; index < all.size(); ++index)
                reregistered = reregistered || command(all[index]) == "register_subsystem";
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        require(reregistered, "a restart between heartbeats was not detected");
        require(logs.count("ERROR", "status requires an existing authenticated registration") >= 1,
            "the refused status is logged");
        subsystem.terminate();
    }
}

// Re-registration while Control stays up: Control keeps the publication's
// slots and its subscribers' generation, so the publisher continues its
// generation. A reset to 0 left the reader on its higher stale generation and
// withheld the new Events.
void publisher_continues_its_generation_when_control_keeps_the_slots() {
    const std::string suffix = std::to_string(::getpid());
    const std::string pool = "/r2k-cpp-gen-pool-" + suffix;
    const std::string lock_name = "/r2k-cpp-gen-lock-" + suffix;
    constexpr std::size_t kSlotSize = 4096, kSlotCount = 16, kDepth = 4;
    R2KernelShmHandle* shm = nullptr;
    R2KernelLockHandle* lock = nullptr;
    R2KernelEventClient* reader = nullptr;
    require(r2kernel_shm_create(pool.c_str(), kSlotSize, kSlotCount, &shm) == 0, "shared-memory pool");
    require(r2kernel_lock_create(lock_name.c_str(), &lock) == 0, "subscriber lock");
    struct Cleanup {
        R2KernelShmHandle* shm; R2KernelLockHandle* lock; R2KernelEventClient** reader; std::string pool, lock_name;
        ~Cleanup() {
            if (*reader != nullptr) r2kernel_event_client_close(*reader);
            r2kernel_lock_close(lock);
            r2kernel_lock_unlink(lock_name.c_str());
            r2kernel_shm_close(shm);
            r2kernel_shm_unlink(pool.c_str());
        }
    } cleanup {shm, lock, &reader, pool, lock_name};
    std::atomic<int> slot_index {2};
    r2kernel_test::FakeControl control;
    control.respond([&](const std::string& request) -> std::string {
        const auto cmd = command(request);
        if (cmd == "register_subsystem")
            return "{\"res\":\"ok\",\"ok\":true,\"topic_buffer_pool_name\":\"" + pool + "\","
                   "\"topic_buffer_pool_slot_size\":4096,\"topic_buffer_pool_slot_count\":16," + kControlSettings;
        if (cmd == "request_publish_slots")
            return "{\"res\":\"ok\",\"publish_slot_index\":" + std::to_string(slot_index.load())
                + ",\"queue_depth\":4,\"entry_slot_count\":1,"
                  "\"subscribers\":[{\"subscriber\":\"qa.cpp.reader\",\"lock_name\":\"" + lock_name + "\"}]}";
        return "{\"res\":\"ok\",\"ok\":true}";
    });
    r2kernel::core::ClientOptions client_options;
    client_options.subsystem_name = "qa.cpp.generation";
    r2kernel::core::R2KernelClient client(client_options);
    EventPublishSpec publish;
    publish.topic = "qa.cpp.generation.topic";
    publish.size = 64;
    publish.queue_depth = kDepth;
    require(client.register_event_publish(publish), "publish registration");
    require(r2kernel_event_client_create(pool.c_str(), kSlotSize, kSlotCount, &reader) == 0, "reader");
    require(r2kernel_event_client_subscribe_ex(reader, publish.topic.c_str(), "qa.cpp.generation",
                static_cast<std::size_t>(slot_index.load()), kDepth, 1, lock_name.c_str()) == 0, "reader subscription");
    const auto receive = [&reader](std::int64_t timeout_ms) {
        std::vector<std::string> payloads;
        std::uint8_t topic[128], publisher[128], payload[128];
        std::size_t topic_len = 0, publisher_len = 0, payload_len = 0;
        std::int64_t timestamp = 0;
        while (r2kernel_event_client_recv(reader, timeout_ms, topic, sizeof(topic), &topic_len, publisher,
                   sizeof(publisher), &publisher_len, &timestamp, payload, sizeof(payload), &payload_len) == 0)
            payloads.emplace_back(reinterpret_cast<const char*>(payload), payload_len);
        return payloads;
    };
    const auto publish_one = [&](const std::string& text) {
        require(client.publish_event(publish.topic, EventPayload(text.begin(), text.end()), 0, 0), "publish " + text);
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    };
    for (int index = 0; index < 6; ++index) publish_one("old-" + std::to_string(index));
    require(receive(300).size() == 6, "the reader receives every Event before the re-registration");

    require(client.restore_event_registrations() == 1, "publication restored on the kept slots");
    publish_one("new-0");
    publish_one("new-1");
    const auto after = receive(500);
    require(after == std::vector<std::string>({"new-0", "new-1"}),
        "after a re-registration on kept slots the reader receives exactly the new Events, got "
            + std::to_string(after.size()));

    // A changed allocation starts the new slots at generation 0.
    slot_index = 8;
    require(client.restore_event_registrations() == 1, "publication restored on new slots");
    publish_one("moved-0");
    R2KernelShmSlotInfo slot {};
    slot.generation = 99;
    require(r2kernel_shm_read_snapshot(shm, 8, nullptr, &slot, nullptr, 0) == 0 && slot.generation == 0,
        "a changed allocation starts at generation 0");
}
}  // namespace

// The first registration retries Control's refusals, which
// include the two that ask for a retry, within R2KERNEL_STARTUP_RETRY_COUNT
// attempts (existing behavior, kept).
void first_registration_retries_a_refusal_that_asks_for_a_retry() {
    ControlledSubsystem fixture("first-retry", {"registration_gate_busy", "registration_cleanup_pending"}, 3);
    std::size_t registrations = 0;
    for (const auto& request : fixture.control->requests())
        if (command(request) == "register_subsystem") ++registrations;
    require(registrations == 3, "expected 3 registration attempts, got " + std::to_string(registrations));
}

int main() {
    ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
    const std::pair<const char*, void (*)()> checks[] {
        {"keeper_backs_off_to_the_bound_and_logs_every_failure", keeper_backs_off_to_the_bound_and_logs_every_failure},
        {"publisher_continues_its_generation_when_control_keeps_the_slots",
            publisher_continues_its_generation_when_control_keeps_the_slots},
        {"subsystem_registers_again_after_control_restart", subsystem_registers_again_after_control_restart},
        {"lifecycle_status_does_not_wait_for_a_registration_in_progress",
            lifecycle_status_does_not_wait_for_a_registration_in_progress},
        {"refused_lifecycle_status_is_logged_and_left_to_the_heartbeat",
            refused_lifecycle_status_is_logged_and_left_to_the_heartbeat},
        {"first_registration_retries_a_refusal_that_asks_for_a_retry",
            first_registration_retries_a_refusal_that_asks_for_a_retry},
    };
    int failed = 0;
    for (const auto& [name, check] : checks) {
        try {
            check();
            std::cout << "PASS " << name << '\n';
        } catch (const std::exception& error) {
            std::cerr << "FAIL " << name << ": " << error.what() << '\n';
            ++failed;
        }
    }
    if (failed != 0) return 1;
    std::cout << "control re-registration checks passed\n";
}
