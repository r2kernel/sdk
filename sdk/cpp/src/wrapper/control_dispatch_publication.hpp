#pragma once

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace r2kernel::wrapper::detail {

// Management publishes complete registrations. Readers pin normalized metadata
// and clone only the callable outside management locks for request-local mutable
// captures. Management keeps original registrations until readers finish.
// This is shared_ptr publication, not a claim of lock-free atomic operations.
template <typename Registration, typename RawHandler>
class ControlDispatchPublication final {
public:
    struct Snapshot {
        std::unordered_map<std::string, Registration> endpoints;
        RawHandler raw_handler;
        std::unordered_map<std::string, RawHandler> raw_endpoints;
        bool raw_endpoints_enabled = false;
    };

    ControlDispatchPublication() : published_(std::make_shared<const Snapshot>()) {}

    std::shared_ptr<const Snapshot> acquire() const {
        return std::atomic_load(&published_);
    }

    void register_endpoint(const std::string& topic, Registration registration) {
        update([&](Snapshot& next) { next.endpoints[topic] = std::move(registration); });
    }

    void register_raw_endpoint(const std::string& topic, RawHandler handler) {
        update([&](Snapshot& next) { next.raw_endpoints[topic] = std::move(handler); });
    }

    void enable_raw_endpoints(bool enabled) {
        update([&](Snapshot& next) { next.raw_endpoints_enabled = enabled; });
    }

    void set_raw_handler(RawHandler handler) {
        update([&](Snapshot& next) { next.raw_handler = std::move(handler); });
    }

    // The opaque diagnostics owner may itself be replaced at codec ACK.
    // Pin that owner before replacement so readers cannot release it last.
    void retire_owner(std::shared_ptr<const void> owner) {
        std::lock_guard<std::mutex> lock(management_mutex_);
        retired_owners_.push_back(std::move(owner));
    }

    // Called only by registration, heartbeat, or stop management paths. Release
    // outside the management mutex: a user destructor may register an endpoint.
    void collect_retired() {
        std::vector<std::shared_ptr<const Snapshot>> completed;
        std::vector<std::shared_ptr<const void>> completed_owners;
        {
            std::lock_guard<std::mutex> lock(management_mutex_);
            for (auto it = retired_.begin(); it != retired_.end();) {
                if (it->use_count() == 1) {
                    completed.push_back(std::move(*it));
                    it = retired_.erase(it);
                } else {
                    ++it;
                }
            }
            for (auto it = retired_owners_.begin(); it != retired_owners_.end();) {
                if (it->use_count() == 1) {
                    completed_owners.push_back(std::move(*it));
                    it = retired_owners_.erase(it);
                } else {
                    ++it;
                }
            }
        }
    }

private:
    template <typename Mutation>
    void update(Mutation mutation) {
        {
            std::lock_guard<std::mutex> lock(management_mutex_);
            auto previous = acquire();
            auto next = std::make_shared<Snapshot>(*previous);
            mutation(*next);
            // Pin before publication, including the interval in which the
            // final reader releases the previous published snapshot.
            retired_.push_back(std::move(previous));
            std::atomic_store(&published_, std::shared_ptr<const Snapshot>(std::move(next)));
        }
    }

    mutable std::mutex management_mutex_;
    std::shared_ptr<const Snapshot> published_;
    std::vector<std::shared_ptr<const Snapshot>> retired_;
    std::vector<std::shared_ptr<const void>> retired_owners_;
};

} // namespace r2kernel::wrapper::detail
