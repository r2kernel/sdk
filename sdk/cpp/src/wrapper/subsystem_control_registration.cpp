// Subsystem heartbeat upkeep of the Control registration and the immediate
// lifecycle status (cold control plane). A separate translation unit so this
// code stays out of subsystem.cpp's per-call codegen.
//
// With the native client, every heartbeat status must be accepted by Control.
// When it is not (Control unreachable, restarted, registration gone), the
// subsystem registers again with its unchanged session, PID, lease and notify
// endpoint, publishes the same full status (manifest, provides, requires,
// endpoints), requests its Event publications and subscriptions and its
// Control handler again. Backoff and logging: core/control_registration_keeper.hpp.
//
// A lifecycle change (fault(), degrade(), a Control-driven transition, ...)
// sends one status and never waits for or starts a re-registration: while a
// heartbeat holds the publication lock, or the registration is lost, the
// status is left to the next heartbeat (logged at info level); a refused
// status is logged at error level and the next heartbeat registers again.
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <yaml-cpp/yaml.h>

#include <exception>
#include <optional>
#include <string>

namespace r2kernel::wrapper {
namespace {

constexpr char kHeartbeatTag[] = "Heartbeat";
constexpr char kRejectedPrefix[] = "rejected: ";

// nullopt when Control accepted the status; otherwise why it did not.
std::optional<std::string> status_failure(r2kernel::core::R2KernelClient& native, const std::string& status) {
    try {
        native.publish_prepared_status(status);
        return std::nullopt;
    } catch (const std::exception& error) {
        const std::string text = error.what();
        const auto rejected = text.find(kRejectedPrefix);
        if (rejected == std::string::npos) return "Control unreachable: " + text;
        try {
            const auto reply = YAML::Load(text.substr(rejected + sizeof(kRejectedPrefix) - 1));
            if (reply.IsMap() && reply["error"] && reply["error"].IsScalar())
                return "Control refused the status: " + reply["error"].as<std::string>();
        } catch (const std::exception&) {
        }
        return "Control returned no status acknowledgement: " + text;
    }
}

}  // namespace

// Native client only (send_heartbeat_once keeps the best-effort notification
// for other CoreClient implementations). Returns whether the registration is
// still lost after this heartbeat.
bool Subsystem::keep_control_registration(const std::string& status) {
    auto* native = static_cast<r2kernel::core::R2KernelClient*>(core_client_.get());
    return native->keep_registration(
        [native, &status]() { return status_failure(*native, status); },
        [this, native, &status]() { return register_with_control_again(*native, status); },
        [this](bool error, const std::string& message) {
            if (error) logger_.error(kHeartbeatTag, message);
            else logger_.info(kHeartbeatTag, message);
        });
}

// A failure of the immediate lifecycle status is logged; the state change stands.
void Subsystem::send_lifecycle_status() {
    try {
        send_heartbeat_once(StatusUpdate::lifecycle_change);
    } catch (const std::exception& error) {
        logger_.error(kHeartbeatTag, std::string("Immediate lifecycle status update failed: ") + error.what());
    }
}

// Whether the lifecycle status is left to the next heartbeat (logged): when a
// heartbeat holds the publication lock (it may be registering with Control
// again), or the native client's registration is lost and waits for the
// heartbeat that restores it. That heartbeat carries the new state.
bool Subsystem::lifecycle_status_deferred(bool publication_locked, const std::string& state) {
    const char* reason = "a heartbeat or a registration with Control is in progress";
    if (publication_locked) {
        const auto* native = dynamic_cast<const r2kernel::core::R2KernelClient*>(core_client_.get());
        if (native == nullptr || !native->registration_lost()) return false;
        reason = "the registration with Control is being restored";
    }
    logger_.info(kHeartbeatTag, "Lifecycle status " + state + " is left to the next heartbeat: " + reason);
    return true;
}

// The immediate status after a lifecycle change, sent once (native client). A
// refusal is logged; it never registers again inline, the next heartbeat does
// when Control lost the registration.
void Subsystem::publish_lifecycle_status(const std::string& status, const std::string& state) {
    auto* native = static_cast<r2kernel::core::R2KernelClient*>(core_client_.get());
    if (const auto failure = status_failure(*native, status)) {
        logger_.error(kHeartbeatTag, "Lifecycle status " + state + " not accepted (" + *failure
            + "); the next heartbeat registers again if Control lost the registration");
    }
}

std::optional<std::string> Subsystem::register_with_control_again(r2kernel::core::R2KernelClient& native,
                                                                   const std::string& status) {
    try {
        const auto changed = native.reregister_subsystem();
        if (!changed.empty()) {
            logger_.error(kHeartbeatTag, "Control now offers a different " + changed
                + "; this process keeps its current one until it restarts");
        }
        if (auto failure = status_failure(native, status)) return failure;
        try {
            (void)native.restore_event_registrations();
        } catch (const r2kernel::core::EventPoolReplacedError& error) {
            logger_.error(kHeartbeatTag, error.what());
        }
        if (control_dispatch_running_ && !native.start_control_server(
                [this](ControlRequest request) { return dispatch_control_request(std::move(request)); })) {
            return std::string("Control did not accept the Control handler registration");
        }
    } catch (const std::exception& error) {
        return std::string(error.what());
    }
    return std::nullopt;
}

}  // namespace r2kernel::wrapper
