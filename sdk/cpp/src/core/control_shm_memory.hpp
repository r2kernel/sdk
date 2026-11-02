// Production local Control: anonymous, connection-scoped shared memory.
// Included in client.cpp after the binary descriptor helpers (Unix only).
#if defined(__linux__)
#include <linux/memfd.h>
#include <sys/syscall.h>
#endif

class ControlShmAccountedError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class ControlShmProtocolError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// An exchange's absolute deadline expired. Distinct from protocol, capacity and
// socket failures so a prepared lane can be re-prepared after a late exchange.
class ControlShmDeadlineError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

std::size_t configured_control_shm_rt_memory_budget() {
    const char* configured = std::getenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES");
    if (!configured) return 64U * 1024U * 1024U;
    if (*configured == '\0') throw std::runtime_error("Invalid R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES");
    for (const char* digit = configured; *digit; ++digit)
        if (*digit < '0' || *digit > '9')
            throw std::runtime_error("Invalid R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES");
    char* end = nullptr;
    errno = 0;
    const auto value = std::strtoull(configured, &end, 10);
    if (errno != 0 || *end != '\0' || value == 0 || value > std::numeric_limits<std::size_t>::max())
        throw std::runtime_error("Invalid R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES");
    return static_cast<std::size_t>(value);
}

std::size_t control_shm_page_rounded_bytes(std::size_t size) {
    static const std::size_t page_size = [] {
        const long observed = ::sysconf(_SC_PAGESIZE);
        if (observed <= 0) throw std::runtime_error("Invalid Control memory accounting page size");
        return static_cast<std::size_t>(observed);
    }();
    if (size > std::numeric_limits<std::size_t>::max() - (page_size - 1))
        throw ControlShmAccountedError("Control prepared memory page rounding overflow");
    return ((size + page_size - 1) / page_size) * page_size;
}

struct ControlShmLimits {
    std::size_t min_region_bytes = 4096;
    std::size_t max_region_bytes = 16U * 1024U * 1024U;
    std::size_t max_channels = 128;
    std::size_t max_owned_bytes = 64U * 1024U * 1024U;
    std::size_t rt_memory_budget_bytes = 64U * 1024U * 1024U;
    std::size_t max_mapped_bytes = 64U * 1024U * 1024U;

    void validate() const {
        if (min_region_bytes < 4096 || max_region_bytes < min_region_bytes ||
            max_region_bytes > 16U * 1024U * 1024U || max_channels == 0 || rt_memory_budget_bytes == 0 ||
            max_owned_bytes < max_region_bytes || max_mapped_bytes < max_region_bytes)
            throw std::runtime_error("Invalid local Control SHM resource limits");
    }
};

// Process admission is configured once, at negotiation or first memory use. Client
// negotiation retains its own limit; changing the environment cannot resize an
// active process account. This mutex is used only for admission/release/stats.
class ControlShmProcessMemoryBudget {
public:
    static ControlShmProcessMemoryBudget& instance() {
        // Keep the accountant alive through destruction of static SDK clients.
        static auto* budget = [] {
            auto* created = new ControlShmProcessMemoryBudget;
            published_instance().store(created, std::memory_order_release);
            return created;
        }();
        return *budget;
    }
    static std::optional<std::array<std::size_t, 3>> configured_snapshot() {
        const auto* configured = published_instance().load(std::memory_order_acquire);
        if (!configured) return std::nullopt;
        return configured->snapshot();
    }
    void reserve(std::size_t size) {
        std::lock_guard<std::mutex> guard(mutex_);
        if (size > limit_ || bytes_ > limit_ - size)
            throw ControlShmAccountedError("Process Control prepared/RT memory budget exceeded");
        bytes_ += size;
        peak_ = std::max(peak_, bytes_);
    }
    void release(std::size_t size) {
        std::lock_guard<std::mutex> guard(mutex_);
        bytes_ -= size;
    }
    std::array<std::size_t, 3> snapshot() const {
        std::lock_guard<std::mutex> guard(mutex_);
        return {limit_, bytes_, peak_};
    }
private:
    ControlShmProcessMemoryBudget() : limit_(configured_control_shm_rt_memory_budget()) {}
    static std::atomic<ControlShmProcessMemoryBudget*>& published_instance() {
        static std::atomic<ControlShmProcessMemoryBudget*> published{nullptr};
        return published;
    }
    mutable std::mutex mutex_;
    std::size_t limit_ = 64U * 1024U * 1024U;
    std::size_t bytes_ = 0, peak_ = 0;
};

class ControlShmDurationMetrics {
public:
    void record(std::chrono::steady_clock::duration duration) {
        const auto elapsed = static_cast<std::uint64_t>(std::max<std::int64_t>(0,
            std::chrono::duration_cast<std::chrono::nanoseconds>(duration).count()));
        count.fetch_add(1, std::memory_order_relaxed);
        total_ns.fetch_add(elapsed, std::memory_order_relaxed);
        auto previous = max_ns.load(std::memory_order_relaxed);
        while (elapsed > previous && !max_ns.compare_exchange_weak(previous, elapsed, std::memory_order_relaxed)) {}
    }
    std::atomic<std::uint64_t> count{0}, total_ns{0}, max_ns{0};
};

class ControlShmDurationScope {
public:
    ControlShmDurationScope(ControlShmDurationMetrics& metrics, bool enabled) : metrics_(metrics), enabled_(enabled) {
        if (enabled_) started_ = std::chrono::steady_clock::now();
    }
    ControlShmDurationScope(ControlShmDurationMetrics& metrics, bool enabled,
        std::chrono::steady_clock::time_point started, bool record_on_exit = true)
        : metrics_(metrics), enabled_(enabled), record_on_exit_(record_on_exit), started_(started) {}
    ~ControlShmDurationScope() { if (record_on_exit_) finish(); }
    void finish() {
        if (!enabled_) return;
        finish(std::chrono::steady_clock::now());
    }
    void finish(std::chrono::steady_clock::time_point completed) {
        if (!enabled_) return;
        metrics_.record(completed - started_);
        enabled_ = false;
    }
private:
    ControlShmDurationMetrics& metrics_;
    bool enabled_;
    bool record_on_exit_ = true;
    std::chrono::steady_clock::time_point started_{};
};

class ControlShmMetrics {
public:
    ControlShmLimits limits;
    std::atomic<std::uint64_t> channels_opened{0}, channels_closed{0};
    std::atomic<std::uint64_t> regions_created{0}, region_reuses{0}, region_resizes{0};
    std::atomic<std::uint64_t> mapping_opens{0}, mapping_reuses{0}, mapping_closes{0};
    std::atomic<std::uint64_t> payload_reads{0}, payload_writes{0};
    std::atomic<std::uint64_t> payload_bytes_read{0}, payload_bytes_written{0};
    std::atomic<std::uint64_t> fd_sent{0}, fd_received{0};
    std::atomic<std::uint64_t> protocol_errors{0}, capacity_errors{0}, io_errors{0};
    std::atomic<std::uint64_t> prepared_session_hits{0}, prepared_session_misses{0};
    std::atomic<std::uint64_t> prepared_lane_recoveries{0}, prepared_lane_recovery_failures{0};
    const bool timing_enabled = env_int_value("R2KERNEL_CONTROL_SHM_TIMING_METRICS", 0) == 1;
    ControlShmDurationMetrics response_ready, release_receive, reclaim_barrier;

    void reserve_channel() {
        std::lock_guard<std::mutex> guard(mutex_);
        if (active_channels_ >= limits.max_channels) {
            ++capacity_errors;
            throw ControlShmAccountedError("Local Control SHM channel limit exceeded");
        }
        ++active_channels_;
        peak_channels_ = std::max(peak_channels_, active_channels_);
        ++channels_opened;
    }
    void release_channel() {
        std::lock_guard<std::mutex> guard(mutex_);
        if (active_channels_) --active_channels_;
        ++channels_closed;
    }
    void reserve_bytes(std::size_t size, bool owned) {
        std::lock_guard<std::mutex> guard(mutex_);
        auto& current = owned ? owned_bytes_ : mapped_bytes_;
        auto& peak = owned ? peak_owned_bytes_ : peak_mapped_bytes_;
        const auto limit = owned ? limits.max_owned_bytes : limits.max_mapped_bytes;
        if (size > limit || current > limit - size) {
            ++capacity_errors;
            throw ControlShmAccountedError("Local Control SHM byte budget exceeded");
        }
        current += size;
        peak = std::max(peak, current);
    }
    void release_bytes(std::size_t size, bool owned) {
        std::lock_guard<std::mutex> guard(mutex_);
        auto& current = owned ? owned_bytes_ : mapped_bytes_;
        current -= size;
    }
    void reserve_prepared_bytes(std::size_t size) {
        std::lock_guard<std::mutex> guard(mutex_);
        std::size_t actual_bytes = 0;
        try { actual_bytes = control_shm_page_rounded_bytes(size); }
        catch (const ControlShmAccountedError&) { ++capacity_errors; throw; }
        if (actual_bytes > limits.rt_memory_budget_bytes || prepared_bytes_ > limits.rt_memory_budget_bytes - actual_bytes) {
            ++capacity_errors;
            throw ControlShmAccountedError("Control prepared/RT memory budget exceeded");
        }
        try { ControlShmProcessMemoryBudget::instance().reserve(actual_bytes); }
        catch (const ControlShmAccountedError&) { ++capacity_errors; throw; }
        prepared_bytes_ += actual_bytes;
        peak_prepared_bytes_ = std::max(peak_prepared_bytes_, prepared_bytes_);
    }
    void release_prepared_bytes(std::size_t size) {
        std::lock_guard<std::mutex> guard(mutex_);
        const auto actual_bytes = control_shm_page_rounded_bytes(size);
        prepared_bytes_ -= actual_bytes;
        ControlShmProcessMemoryBudget::instance().release(actual_bytes);
    }
    void record_error(const std::exception& error) {
        if (dynamic_cast<const ControlShmAccountedError*>(&error) != nullptr) return;
        if (dynamic_cast<const ControlShmProtocolError*>(&error) != nullptr) ++protocol_errors;
        else ++io_errors;
    }
    std::string json() const {
        std::lock_guard<std::mutex> guard(mutex_);
        std::string out = "{\"event\":\"control_shm_summary\",\"runtime\":\"cpp_sdk\","
                          "\"mode\":\"forwarded_shm_v5\",\"policy\":\"forwarded_shm_v5\"";
        const auto field = [&out](const char* name, std::uint64_t value) {
            out += ",\"" + std::string(name) + "\":" + std::to_string(value);
        };
        // Diagnostics do not configure an unused process or throw due to a
        // later invalid environment change during client destruction.
        const auto process_memory = ControlShmProcessMemoryBudget::configured_snapshot();
        field("process_memory_configured", process_memory.has_value());
        if (process_memory) {
            field("process_rt_memory_budget_bytes", (*process_memory)[0]);
            field("process_prepared_bytes", (*process_memory)[1]);
            field("process_peak_prepared_bytes", (*process_memory)[2]);
        }
        field("prepared_bytes", prepared_bytes_); field("peak_prepared_bytes", peak_prepared_bytes_);
        field("rt_memory_budget_bytes", limits.rt_memory_budget_bytes);
        field("active_channels", active_channels_); field("peak_channels", peak_channels_);
        field("owned_bytes", owned_bytes_); field("peak_owned_bytes", peak_owned_bytes_);
        field("mapped_bytes", mapped_bytes_); field("peak_mapped_bytes", peak_mapped_bytes_);
#define R2K_CONTROL_COUNTER(name) field(#name, name.load())
        R2K_CONTROL_COUNTER(channels_opened); R2K_CONTROL_COUNTER(channels_closed);
        R2K_CONTROL_COUNTER(regions_created); R2K_CONTROL_COUNTER(region_reuses); R2K_CONTROL_COUNTER(region_resizes);
        R2K_CONTROL_COUNTER(mapping_opens); R2K_CONTROL_COUNTER(mapping_reuses); R2K_CONTROL_COUNTER(mapping_closes);
        R2K_CONTROL_COUNTER(payload_reads); R2K_CONTROL_COUNTER(payload_writes);
        R2K_CONTROL_COUNTER(payload_bytes_read); R2K_CONTROL_COUNTER(payload_bytes_written);
        R2K_CONTROL_COUNTER(fd_sent); R2K_CONTROL_COUNTER(fd_received);
        R2K_CONTROL_COUNTER(protocol_errors); R2K_CONTROL_COUNTER(capacity_errors); R2K_CONTROL_COUNTER(io_errors);
        R2K_CONTROL_COUNTER(prepared_session_hits); R2K_CONTROL_COUNTER(prepared_session_misses);
        R2K_CONTROL_COUNTER(prepared_lane_recoveries); R2K_CONTROL_COUNTER(prepared_lane_recovery_failures);
#undef R2K_CONTROL_COUNTER
        field("timing_enabled", timing_enabled);
        out += ",\"response_ready_scope\":\"r2kernel_client_call_start_to_owned_validated_response\""
               ",\"release_receive_scope\":\"owned_validated_response_to_validated_released_including_receiver_scheduling\""
               ",\"reclaim_barrier_scope\":\"next_call_wait_for_previous_release\"";
        const auto duration = [&field, &out, enabled = timing_enabled](const char* name, const ControlShmDurationMetrics& metrics) {
            const auto prefix = std::string(name);
            const auto count = metrics.count.load(std::memory_order_relaxed);
            field((prefix + "_count").c_str(), count);
            field((prefix + "_total_ns").c_str(), metrics.total_ns.load(std::memory_order_relaxed));
            if (enabled && count == 0) out += ",\"" + prefix + "_max_ns\":null";
            else field((prefix + "_max_ns").c_str(), metrics.max_ns.load(std::memory_order_relaxed));
        };
        duration("response_ready", response_ready);
        duration("release_receive", release_receive);
        duration("reclaim_barrier", reclaim_barrier);
        field("max_channels", limits.max_channels);
        field("max_owned_bytes", limits.max_owned_bytes); field("max_mapped_bytes", limits.max_mapped_bytes);
        out += "}";
        return out;
    }
private:
    std::size_t prepared_bytes_ = 0, peak_prepared_bytes_ = 0;
    mutable std::mutex mutex_;
    std::size_t active_channels_ = 0, peak_channels_ = 0;
    std::size_t owned_bytes_ = 0, peak_owned_bytes_ = 0;
    std::size_t mapped_bytes_ = 0, peak_mapped_bytes_ = 0;
};

class ControlShmDescriptor {
public:
    explicit ControlShmDescriptor(int descriptor = -1) : descriptor_(descriptor) {}
    ~ControlShmDescriptor() { reset(); }
    ControlShmDescriptor(const ControlShmDescriptor&) = delete;
    ControlShmDescriptor& operator=(const ControlShmDescriptor&) = delete;
    ControlShmDescriptor(ControlShmDescriptor&& other) noexcept : descriptor_(other.release()) {}
    ControlShmDescriptor& operator=(ControlShmDescriptor&& other) noexcept {
        if (this != &other) reset(other.release());
        return *this;
    }
    int get() const { return descriptor_; }
    int release() { const int result = descriptor_; descriptor_ = -1; return result; }
    void reset(int descriptor = -1) {
        if (descriptor_ >= 0) ::close(descriptor_);
        descriptor_ = descriptor;
    }
private:
    int descriptor_;
};

std::string new_control_shm_token() {
    static std::atomic<std::uint64_t> counter{0};
    static const auto epoch = std::chrono::high_resolution_clock::now().time_since_epoch().count();
    return "r2kctl-" + std::to_string(::getpid()) + "-" + std::to_string(epoch) + "-" + std::to_string(++counter);
}

class ControlShmRegion {
public:
    explicit ControlShmRegion(ControlShmMetrics& metrics, bool owned)
        : metrics_(metrics), owned_(owned) { token_.resize(128, '\0'); token_.clear(); }
    ~ControlShmRegion() { reset(); }
    ControlShmRegion(const ControlShmRegion&) = delete;
    ControlShmRegion& operator=(const ControlShmRegion&) = delete;

    void reset() {
        if (mapping_ != MAP_FAILED) {
            if (locked_) {
                ::munlock(mapping_, capacity_);
                metrics_.release_prepared_bytes(capacity_);
                locked_ = false;
            }
            ::munmap(mapping_, capacity_);
            mapping_ = MAP_FAILED;
            metrics_.release_bytes(capacity_, owned_);
            if (!owned_) ++metrics_.mapping_closes;
        }
        descriptor_.reset();
        capacity_ = 0;
        token_.clear();
        bounded_ = false;
        payload_capacity_ = 0;
    }
    // Caller selects strict locking from its scheduling policy before exchange.
    void prepare_memory(bool required_lock) {
        if (required_lock && !required_lock_) {
            if (mapping_ != MAP_FAILED && !locked_) lock_mapping(mapping_, capacity_);
            required_lock_ = true;
        }
    }
    void reserve_payload(std::size_t payload_capacity) {
        if (!owned_ || capacity_ != 0 || payload_capacity == 0 ||
            payload_capacity > metrics_.limits.max_region_bytes - kPayloadOffset)
            throw ControlShmAccountedError("Invalid fresh Control preparation payload capacity");
        const auto needed = std::max(metrics_.limits.min_region_bytes, payload_capacity + kPayloadOffset);
        const auto rounded = control_shm_page_rounded_bytes(needed);
        create(std::min(rounded, metrics_.limits.max_region_bytes));
        ++metrics_.regions_created;
        payload_capacity_ = payload_capacity;
        bounded_ = true;
    }
    void bind_prepared(const LocalPayloadLease& lease, ControlShmDescriptor incoming,
                       std::size_t payload_capacity = 0) {
        if (capacity_ != 0 || lease.length != 0)
            throw ControlShmProtocolError("Control preparation requires a fresh empty mapping");
        read(lease, std::move(incoming));
        payload_capacity_ = payload_capacity ? payload_capacity : capacity_ - kPayloadOffset;
        if (payload_capacity_ > capacity_ - kPayloadOffset)
            throw ControlShmProtocolError("Prepared Control region is smaller than its declared capacity");
        bounded_ = true;
    }
    bool write(const std::vector<std::uint8_t>& payload) {
        if (!owned_) throw std::logic_error("Cannot write a borrowed Control region");
        if (bounded_ && payload.size() > payload_capacity_) {
            ++metrics_.capacity_errors;
            throw ControlShmAccountedError("Prepared Control payload capacity exceeded; bind a new lane");
        }
        const auto& limits = metrics_.limits;
        if (payload.size() > limits.max_region_bytes - kPayloadOffset) {
            ++metrics_.capacity_errors;
            throw ControlShmAccountedError("Control payload exceeds negotiated region capacity");
        }
        bool changed = false;
        if (capacity_ < payload.size() + kPayloadOffset) {
            const auto previous = capacity_;
            const auto page = static_cast<std::size_t>(::sysconf(_SC_PAGESIZE));
            if (page == 0 || page > limits.max_region_bytes)
                throw std::runtime_error("Invalid OS page size for Control SHM");
            const auto needed = std::max(limits.min_region_bytes, payload.size() + kPayloadOffset);
            auto next = std::max(needed, previous * 2);
            next = std::min(limits.max_region_bytes, ((next + page - 1) / page) * page);
            create(next);
            ++metrics_.regions_created;
            if (previous) ++metrics_.region_resizes;
            changed = true;
        } else ++metrics_.region_reuses;
        auto* bytes = static_cast<std::uint8_t*>(mapping_);
        __atomic_store_n(bytes, static_cast<std::uint8_t>(1), __ATOMIC_RELEASE);
        if (!payload.empty()) std::memcpy(bytes + kPayloadOffset, payload.data(), payload.size());
        const auto length = static_cast<std::uint32_t>(payload.size());
        std::memcpy(bytes + 8, &length, sizeof(length));
        __atomic_store_n(bytes, static_cast<std::uint8_t>(0), __ATOMIC_RELEASE);
        ++metrics_.payload_writes;
        metrics_.payload_bytes_written += payload.size();
        return changed;
    }
    LocalPayloadLease lease(std::size_t length) const {
        return LocalPayloadLease{token_, 0, capacity_, 1, length};
    }
    const std::string& token() const { return token_; }
    int descriptor() const { return descriptor_.get(); }
    std::size_t capacity() const { return capacity_; }

    std::vector<std::uint8_t> read(const LocalPayloadLease& lease, ControlShmDescriptor incoming) {
        if (owned_) throw std::logic_error("Cannot import into an owned Control region");
        if (bounded_ && (lease.region_name != token_ || incoming.get() >= 0 ||
            lease.slot_size != capacity_ || lease.length > payload_capacity_))
            throw ControlShmProtocolError("Prepared Control mapping/capacity changed; bind a new lane");
        if (lease.region_name.empty() || lease.region_name.size() > 128 || lease.slot_index != 0 ||
            lease.slot_count != 1 || lease.slot_size < metrics_.limits.min_region_bytes ||
            lease.slot_size > metrics_.limits.max_region_bytes || lease.length > lease.slot_size - kPayloadOffset) {
            throw ControlShmProtocolError("Invalid Control SHM descriptor");
        }
        if (lease.region_name != token_) {
            if (incoming.get() < 0) throw ControlShmProtocolError("New Control region requires one descriptor");
            reset();
            struct stat status {};
            if (::fstat(incoming.get(), &status) != 0)
                throw std::runtime_error("Control SHM descriptor fstat failed");
            if (status.st_size < 0 || static_cast<std::size_t>(status.st_size) != lease.slot_size)
                throw ControlShmProtocolError("Control SHM descriptor size mismatch");
#if defined(__linux__)
            const auto seals = ::fcntl(incoming.get(), F_GET_SEALS);
            if (seals < 0) throw std::runtime_error("Control SHM descriptor seal query failed");
            if ((seals & (F_SEAL_GROW | F_SEAL_SHRINK)) != (F_SEAL_GROW | F_SEAL_SHRINK))
                throw ControlShmProtocolError("Control SHM requires immutable region dimensions");
#endif
            metrics_.reserve_bytes(lease.slot_size, false);
            void* address = ::mmap(nullptr, lease.slot_size, PROT_READ, MAP_SHARED, incoming.get(), 0);
            if (address == MAP_FAILED) {
                metrics_.release_bytes(lease.slot_size, false);
                throw std::runtime_error("Control SHM read-only mapping failed");
            }
            try {
                prefault_mapping(address, lease.slot_size, false);
                if (required_lock_) lock_mapping(address, lease.slot_size);
            } catch (...) {
                ::munmap(address, lease.slot_size);
                metrics_.release_bytes(lease.slot_size, false);
                throw;
            }
            mapping_ = address;
            capacity_ = lease.slot_size;
            token_ = lease.region_name;
            descriptor_ = std::move(incoming);
            ++metrics_.mapping_opens;
        } else {
            if (incoming.get() >= 0 || lease.slot_size != capacity_)
                throw ControlShmProtocolError("Control region token changed dimensions or repeated its descriptor");
            ++metrics_.mapping_reuses;
        }
        std::uint32_t length = 0;
        const auto* bytes = static_cast<const std::uint8_t*>(mapping_);
        if (__atomic_load_n(bytes, __ATOMIC_ACQUIRE) != 0)
            throw ControlShmProtocolError("Control SHM producer has not completed its payload");
        std::memcpy(&length, bytes + 8, sizeof(length));
        if (length != lease.length) throw ControlShmProtocolError("Control SHM header/descriptor length mismatch");
        std::vector<std::uint8_t> result(bytes + kPayloadOffset, bytes + kPayloadOffset + length);
        if (__atomic_load_n(bytes, __ATOMIC_ACQUIRE) != 0)
            throw ControlShmProtocolError("Control SHM payload changed during the read");
        ++metrics_.payload_reads;
        metrics_.payload_bytes_read += length;
        return result;
    }
private:
    static void prefault_mapping(void* address, std::size_t size, bool writable) {
        const long page_size = ::sysconf(_SC_PAGESIZE);
        if (page_size <= 0) throw std::runtime_error("Invalid Control memory page size");
        auto* bytes = static_cast<volatile unsigned char*>(address);
        for (std::size_t offset = 0; offset < size; offset += static_cast<std::size_t>(page_size)) {
            if (writable) bytes[offset] = 0;
            else { const auto byte = bytes[offset]; (void)byte; }
        }
    }
    void lock_mapping(void* address, std::size_t size) {
        metrics_.reserve_prepared_bytes(size);
        if (::mlock(address, size) != 0) {
            // mlock failure can leave part of this owned mapping locked.
            ::munlock(address, size);
            metrics_.release_prepared_bytes(size);
            throw std::runtime_error("Control SHM strict mlock failed");
        }
        locked_ = true;
    }
    void create(std::size_t size) {
        metrics_.reserve_bytes(size, true);
        struct Reservation {
            ControlShmMetrics& metrics;
            std::size_t size;
            bool transferred = false;
            ~Reservation() { if (!transferred) metrics.release_bytes(size, true); }
        } reservation{metrics_, size};
        auto token = new_control_shm_token();
        ControlShmDescriptor next;
#if defined(__linux__)
        next.reset(static_cast<int>(::syscall(SYS_memfd_create, token.c_str(), MFD_CLOEXEC | MFD_ALLOW_SEALING)));
#elif defined(__APPLE__)
        // A platform implementation, not a fallback when Linux allocation fails.
        // Darwin limits the rendezvous name to 31 bytes. The name disappears
        // immediately; the longer generation token is only wire metadata.
        std::array<unsigned char, 12> random_bytes{};
        ::arc4random_buf(random_bytes.data(), random_bytes.size());
        static constexpr char hexadecimal[] = "0123456789abcdef";
        std::string name = "/r2k-";
        for (const auto byte : random_bytes) {
            name += hexadecimal[byte >> 4];
            name += hexadecimal[byte & 15];
        }
        next.reset(::shm_open(name.c_str(), O_CREAT | O_EXCL | O_RDWR, 0600));
        if (next.get() >= 0 && ::shm_unlink(name.c_str()) != 0)
            throw std::runtime_error("Failed to unlink private Control SHM rendezvous name");
#else
        throw std::runtime_error("Anonymous Control SHM is unsupported on this platform");
#endif
        if (next.get() < 0) throw std::runtime_error("Anonymous Control SHM allocation failed");
        if (::fcntl(next.get(), F_SETFD, FD_CLOEXEC) != 0)
            throw std::runtime_error("Control SHM descriptor CLOEXEC failed");
        if (::ftruncate(next.get(), static_cast<off_t>(size)) != 0)
            throw std::runtime_error("Control SHM sizing failed");
#if defined(__linux__)
        const int allocation_error = ::posix_fallocate(next.get(), 0, static_cast<off_t>(size));
        if (allocation_error != 0) throw std::runtime_error("Control SHM backing storage reservation failed");
        if (::fcntl(next.get(), F_ADD_SEALS, F_SEAL_GROW | F_SEAL_SHRINK) != 0)
            throw std::runtime_error("Control SHM dimension sealing failed");
#endif
        void* address = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, next.get(), 0);
        if (address == MAP_FAILED) {
            throw std::runtime_error("Control SHM writable mapping failed");
        }
        try { prefault_mapping(address, size, true); }
        catch (...) { ::munmap(address, size); throw; }
        reset();
        try { if (required_lock_) lock_mapping(address, size); }
        catch (...) { ::munmap(address, size); throw; }
        descriptor_ = std::move(next);
        mapping_ = address;
        capacity_ = size;
        token_ = std::move(token);
        reservation.transferred = true;
    }
    ControlShmMetrics& metrics_;
    bool owned_;
    bool required_lock_ = false;
    bool bounded_ = false;
    std::size_t payload_capacity_ = 0;
    bool locked_ = false;
    ControlShmDescriptor descriptor_;
    void* mapping_ = MAP_FAILED;
    std::size_t capacity_ = 0;
    std::string token_;
};
