#pragma once

// Keeps a running subsystem registered with Control across a Control restart.
// Cold control plane (heartbeat thread); never on a per-cycle path.
//
// Every heartbeat sends the subsystem status and reads Control's answer. When
// Control does not accept it (unreachable, restarted, registration gone), the
// subsystem registers again through the normal registration request with its
// unchanged session, PID, resource lease and notify endpoint, publishes its
// full status and requests its Event publications, Event subscriptions and
// Control handler again. Control verifies the lease with the resource broker.
//
// Attempts back off: the first runs in the heartbeat that found the failure;
// after a failed attempt the next one is due kRegistrationRetryInitial later,
// doubling up to kRegistrationRetryMax, and runs in the first heartbeat after
// that time (a heartbeat follows kHeartbeatPeriod after the previous one
// finished; kRegistrationStartBound = due time plus that period). Every failed
// attempt is logged; the subsystem keeps trying while it runs.

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <functional>
#include <optional>
#include <string>
#include <utility>

namespace r2kernel::core::detail {

constexpr std::chrono::milliseconds kHeartbeatPeriod {1000};
constexpr std::chrono::milliseconds kRegistrationRetryInitial {1000};
constexpr std::chrono::milliseconds kRegistrationRetryMax {4000};
constexpr std::chrono::milliseconds kRegistrationStartBound = kRegistrationRetryMax + kHeartbeatPeriod;

class ControlRegistrationKeeper {
public:
    using Clock = std::function<std::chrono::steady_clock::time_point()>;
    // nullopt on success, otherwise the failure reason.
    using Step = std::function<std::optional<std::string>()>;
    using Log = std::function<void(bool error, const std::string& message)>;

    explicit ControlRegistrationKeeper(Clock clock = [] { return std::chrono::steady_clock::now(); })
        : clock_(std::move(clock)) {}

    bool registration_lost() const noexcept { return lost_since_.has_value(); }

    void heartbeat(const Step& send_status, const Step& register_again, const Log& log) {
        const auto now = clock_();
        if (!lost_since_) {
            const auto failure = send_status();
            if (!failure) return;
            lost_since_ = now;
            attempts_ = 0;
            retry_ = kRegistrationRetryInitial;
            log(true, "Heartbeat not accepted (" + *failure + "); registering with Control again");
        } else if (now < next_attempt_) {
            return;
        }
        ++attempts_;
        const auto failure = register_again();
        const auto finished = clock_();
        const auto elapsed = seconds_text(finished - *lost_since_);
        if (!failure) {
            log(false, "Registered with Control again after " + elapsed + "s (" + std::to_string(attempts_)
                + " attempt(s))");
            lost_since_.reset();
            return;
        }
        const auto delay = retry_;
        next_attempt_ = finished + delay;
        retry_ = std::min<std::chrono::milliseconds>(retry_ * 2, kRegistrationRetryMax);
        log(true, "Registration attempt " + std::to_string(attempts_) + " failed " + elapsed
            + "s after the registration was lost: " + *failure + "; next attempt in "
            + std::to_string(std::chrono::duration_cast<std::chrono::seconds>(delay).count()) + "s");
    }

private:
    static std::string seconds_text(std::chrono::steady_clock::duration duration) {
        char text[32];
        std::snprintf(text, sizeof(text), "%.1f", std::chrono::duration<double>(duration).count());
        return text;
    }

    Clock clock_;
    std::optional<std::chrono::steady_clock::time_point> lost_since_;
    unsigned attempts_ = 0;
    std::chrono::milliseconds retry_ = kRegistrationRetryInitial;
    std::chrono::steady_clock::time_point next_attempt_ {};
};

}  // namespace r2kernel::core::detail
