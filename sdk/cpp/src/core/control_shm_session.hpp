// Internal identity and a bounded, non-owning per-thread lookup. The registry
// remains the owner; cached entries cannot prolong mappings past client stop.
std::uint64_t claim_control_shm_client_identity(std::atomic<std::uint64_t>& next) {
    auto identity = next.load(std::memory_order_relaxed);
    while (identity != 0 && identity != std::numeric_limits<std::uint64_t>::max()) {
        if (next.compare_exchange_weak(identity, identity + 1, std::memory_order_relaxed)) return identity;
    }
    // Saturation is permanent. Never wrap to an identity held by another TLS.
    throw std::runtime_error("Control client identity exhausted");
}

std::uint64_t next_control_shm_client_identity() {
    static std::atomic<std::uint64_t> next{1};
    return claim_control_shm_client_identity(next);
}

class ControlShmSessionCache {
public:
    // A miss only performs the existing cold registry lookup. The admission
    // limit still belongs to the client, not this acceleration structure.
    // Matches the default negotiated channel count; evicting a cached entry
    // never evicts/admission-limits the live registry lane.
    static constexpr std::size_t capacity = 128;
    std::shared_ptr<ControlShmClientChannel> find(std::uint64_t client,
        const std::string& target, const std::string& topic,
        std::uint64_t endpoint_generation, std::uint64_t codec_generation) {
        for (const auto& entry : entries_) {
            if (entry.client != client) continue;
            auto channel = entry.channel.lock();
            if (!channel || !channel->session_valid.load(std::memory_order_acquire)) continue;
            const auto& session = channel->session;
            if (session.target == target && session.topic == topic &&
                session.endpoint_generation == endpoint_generation &&
                session.codec_generation == codec_generation) return channel;
        }
        return {};
    }
    void remember(std::uint64_t client, const std::shared_ptr<ControlShmClientChannel>& channel) {
        for (auto& entry : entries_) {
            if (entry.channel.expired()) {
                entry = {client, channel}; return;
            }
            if (entry.client == client && entry.channel.lock() == channel) return;
        }
        entries_[replacement_] = {client, channel};
        replacement_ = (replacement_ + 1) % capacity;
    }
    std::optional<bool> target_ready(std::uint64_t client, const std::string& target,
        std::uint64_t endpoint_generation, std::uint64_t codec_generation) {
        for (const auto& entry : entries_) {
            if (entry.client != client) continue;
            auto channel = entry.channel.lock();
            if (!channel || !channel->session_valid.load(std::memory_order_acquire)) continue;
            const auto& session = channel->session;
            if (session.target == target && session.endpoint_generation == endpoint_generation &&
                session.codec_generation == codec_generation && session.route_ready)
                return session.route_ready->load(std::memory_order_acquire);
        }
        return {};
    }
private:
    struct Entry {
        std::uint64_t client = 0;
        std::weak_ptr<ControlShmClientChannel> channel;
    };
    std::array<Entry, capacity> entries_{};
    std::size_t replacement_ = 0;
};

ControlShmSessionCache& control_shm_thread_sessions() {
    thread_local ControlShmSessionCache cache;
    return cache;
}
