// Included once, inside subsystem.cpp's anonymous namespace (same translation
// unit). Log tags, control-socket notification and the text/JSON helpers
// used across the Subsystem implementation.

constexpr char kLifecycleTag[] = "Lifecycle";
constexpr char kEventTag[] = "EventMessage";
constexpr char kHeartbeatTag[] = "Heartbeat";
constexpr char kOperationTag[] = "Operation";
constexpr char kCodecTag[] = "Codec";
constexpr char kTypedProcedureTopicPrefix[] = "procedure.call_typed/";
constexpr char kTypedOperationStartTopicPrefix[] = "operation.start_typed/";
constexpr char kTypedOperationCancelTopic[] = "operation.cancel_typed";
constexpr char kTypedOperationQueryTopic[] = "operation.query_typed";

// Native resolution validates zero only for an explicitly negotiated local SHM
// endpoint. Other CoreClient implementations retain their positive-port contract.
bool resolved_control_route_port_is_valid(const CoreClient* client, int port) {
    return port > 0 || (port == 0 && dynamic_cast<const r2kernel::core::R2KernelClient*>(client));
}

struct SubsystemInitializationRegistry {
    std::mutex mutex;
    std::unordered_set<const Subsystem*> successfully_initialized;
};

SubsystemInitializationRegistry& subsystem_initialization_registry() {
    static SubsystemInitializationRegistry registry;
    return registry;
}
// Lifecycle serialization must not share the Event registration lock: hooks
// can wait for workers registering Events. The per-Subsystem lifecycle state
// lives in this registry, keyed by instance, rather than in the public class.
struct OperationWorker {
    std::thread thread;
    std::shared_ptr<void> operation;
    std::atomic<bool> finished{false};
};

struct SubsystemLifecycleState {
    std::recursive_mutex transition;
    unsigned transitions = 0; // Access only while transition is held.
    std::mutex flight_mutex;
    std::condition_variable flight_completed;
    // Protected by flight_mutex; unequal values identify the active flight.
    std::uint64_t flight_started = 0;
    std::uint64_t flight_finished = 0;
    // Admission reservations cover topic setup as well as worker creation.
    std::mutex operation_mutex;
    std::condition_variable operation_admissions_finished;
    bool operation_admission_open = false;
    bool operation_draining = false;
    std::uint64_t operation_generation = 0;
    std::size_t operation_admissions = 0;
    std::list<OperationWorker> operation_workers;
    // Notified when an Operation completes; waited on with the Subsystem's
    // Operation record mutex (wait_for_operation_quiescence).
    std::condition_variable operation_completed;
};

// As the Python _OPERATION_SHUTDOWN_GRACE_S: how long stopping the Operation
// server (or terminate) lets running Operations finish before it requests
// their cancellation, and how long it then waits again (0.25 s).
constexpr auto kOperationShutdownGrace = std::chrono::milliseconds(1000);
constexpr auto kOperationCancelGrace = std::chrono::milliseconds(250);

bool is_terminal_operation_state(std::string_view state) {
    return state == "succeeded" || state == "failed" || state == "canceled" || state == "rejected";
}

// As the Python SDK (_OPERATION_TERMINAL_STATES): a cancel request changes only
// an Operation that has not finished. It has finished when its handler
// completed it, or published a terminal state itself and has not returned yet.
// completed alone tracks the worker's lifetime (quiescence, joins).
template <typename Operation>
bool operation_accepts_cancel(const Operation& operation) {
    return !operation.completed && !is_terminal_operation_state(operation.state);
}

// Python _wait_for_operation_quiescence: waits, at most timeout, until every
// Operation record has completed. records_mutex guards the records.
template <typename Records>
bool wait_for_operation_quiescence(SubsystemLifecycleState& state, std::mutex& records_mutex,
                                   const Records& records, std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(records_mutex);
    return state.operation_completed.wait_for(lock, timeout, [&records] {
        for (const auto& entry : records)
            if (entry.second && !entry.second->completed) return false;
        return true;
    });
}

// The cancellation step of the Python _stop_operation_server: every unfinished
// Operation is asked to cancel; returns those not asked before, which the
// caller publishes as cancel_requested state events. Caller holds the records'
// mutex.
template <typename Records>
std::vector<typename Records::mapped_type> request_operation_cancellation(Records& records, std::int64_t now_ns) {
    std::vector<typename Records::mapped_type> newly_canceled;
    for (auto& entry : records) {
        auto& operation = entry.second;
        if (!operation || !operation_accepts_cancel(*operation)) continue;
        if (!operation->cancel_requested) newly_canceled.push_back(operation);
        operation->cancel_requested = true;
        operation->updated_ts = now_ns;
    }
    return newly_canceled;
}

struct SubsystemLifecycleRegistry {
    std::mutex mutex;
    std::unordered_map<const Subsystem*, std::shared_ptr<SubsystemLifecycleState>> locks;
};

SubsystemLifecycleRegistry& subsystem_lifecycle_registry() {
    static SubsystemLifecycleRegistry registry;
    return registry;
}

std::shared_ptr<SubsystemLifecycleState> subsystem_lifecycle_lock(const Subsystem* subsystem) {
    auto& registry = subsystem_lifecycle_registry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    auto& entry = registry.locks[subsystem];
    if (!entry) entry = std::make_shared<SubsystemLifecycleState>();
    return entry;
}

// Reservations cover topic setup as well as registration of the worker.
class OperationAdmission {
public:
    explicit OperationAdmission(const std::shared_ptr<SubsystemLifecycleState>& state) : state_(state) {
        std::lock_guard<std::mutex> lock(state_->operation_mutex);
        if (state_->operation_admission_open && !state_->operation_draining) {
            admitted_ = true;
            generation_ = state_->operation_generation;
            ++state_->operation_admissions;
            previous_ = current_;
            current_ = this;
        }
    }
    ~OperationAdmission() {
        if (!admitted_) return;
        current_ = previous_;
        {
            std::lock_guard<std::mutex> lock(state_->operation_mutex);
            --state_->operation_admissions;
        }
        state_->operation_admissions_finished.notify_all();
    }
    OperationAdmission(const OperationAdmission&) = delete;
    OperationAdmission& operator=(const OperationAdmission&) = delete;
    explicit operator bool() const { return admitted_; }
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* admission = current_; admission; admission = admission->previous_)
            if (admission->state_.get() == state) return true;
        return false;
    }
    // Caller holds operation_mutex.
    bool can_launch() const {
        return admitted_ && state_->operation_admission_open && !state_->operation_draining
            && generation_ == state_->operation_generation;
    }
private:
    std::shared_ptr<SubsystemLifecycleState> state_;
    std::uint64_t generation_ = 0;
    bool admitted_ = false;
    OperationAdmission* previous_ = nullptr;
    inline static thread_local OperationAdmission* current_ = nullptr;
};

// A server start is also fenced: teardown must not stop transports while an
// earlier start is still arranging them, and must reject reentrant teardown.
class OperationServerStart {
public:
    OperationServerStart(const std::shared_ptr<SubsystemLifecycleState>& state, bool& enabled)
        : state_(state) {
        std::lock_guard<std::mutex> flight(state_->flight_mutex);
        std::lock_guard<std::mutex> lock(state_->operation_mutex);
        if (!state_->operation_draining && state_->flight_started == state_->flight_finished) {
            enabled = true;
            state_->operation_admission_open = true;
            ++state_->operation_admissions;
            admitted_ = true;
            previous_ = current_;
            current_ = this;
        }
    }
    ~OperationServerStart() {
        if (!admitted_) return;
        current_ = previous_;
        {
            std::lock_guard<std::mutex> lock(state_->operation_mutex);
            --state_->operation_admissions;
        }
        state_->operation_admissions_finished.notify_all();
    }
    explicit operator bool() const { return admitted_; }
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* start = current_; start; start = start->previous_)
            if (start->state_.get() == state) return true;
        return false;
    }
private:
    std::shared_ptr<SubsystemLifecycleState> state_;
    bool admitted_ = false;
    OperationServerStart* previous_ = nullptr;
    inline static thread_local OperationServerStart* current_ = nullptr;
};

class OperationWorkerActivity {
public:
    explicit OperationWorkerActivity(const SubsystemLifecycleState* state)
        : state_(state), previous_(current_) { current_ = this; }
    ~OperationWorkerActivity() { current_ = previous_; }
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* activity = current_; activity; activity = activity->previous_)
            if (activity->state_ == state) return true;
        return false;
    }
private:
    const SubsystemLifecycleState* state_;
    OperationWorkerActivity* previous_;
    inline static thread_local OperationWorkerActivity* current_ = nullptr;
};

// Never hold a subsystem or registry lock while joining.
void join_operation_workers(std::list<OperationWorker>& workers) {
    for (auto& worker : workers) if (worker.thread.joinable()) worker.thread.join();
}

void drain_operation_workers(SubsystemLifecycleState& state) {
    std::list<OperationWorker> workers;
    {
        std::unique_lock<std::mutex> lock(state.operation_mutex);
        state.operation_admissions_finished.wait(lock, [&] { return state.operation_admissions == 0; });
        workers.splice(workers.end(), state.operation_workers);
    }
    join_operation_workers(workers);
}

class OperationDrain {
public:
    explicit OperationDrain(SubsystemLifecycleState& state, bool& enabled) : state_(state) {
        std::lock_guard<std::mutex> lock(state_.operation_mutex);
        enabled = false;
        state_.operation_draining = true;
        state_.operation_admission_open = false;
        ++state_.operation_generation;
    }
    ~OperationDrain() {
        std::lock_guard<std::mutex> lock(state_.operation_mutex);
        state_.operation_draining = false;
    }
    OperationDrain(const OperationDrain&) = delete;
    OperationDrain& operator=(const OperationDrain&) = delete;
private:
    SubsystemLifecycleState& state_;
};

class EventRegistrationActivity {
public:
    explicit EventRegistrationActivity(const std::shared_ptr<SubsystemLifecycleState>& state)
        : state_(state), previous_(current_) { current_ = this; }
    ~EventRegistrationActivity() { current_ = previous_; }
    EventRegistrationActivity(const EventRegistrationActivity&) = delete;
    EventRegistrationActivity& operator=(const EventRegistrationActivity&) = delete;
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* activity = current_; activity; activity = activity->previous_)
            if (activity->state_.get() == state) return true;
        return false;
    }
private:
    std::shared_ptr<SubsystemLifecycleState> state_;
    EventRegistrationActivity* previous_;
    inline static thread_local EventRegistrationActivity* current_ = nullptr;
};

// Distinguish a recursive terminate hook from an independent caller.
class TerminationActivity {
public:
    explicit TerminationActivity(const SubsystemLifecycleState* state)
        : state_(state), previous_(current_) { current_ = this; }
    ~TerminationActivity() { current_ = previous_; }
    TerminationActivity(const TerminationActivity&) = delete;
    TerminationActivity& operator=(const TerminationActivity&) = delete;
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* activity = current_; activity; activity = activity->previous_)
            if (activity->state_ == state) return true;
        return false;
    }
private:
    const SubsystemLifecycleState* state_;
    TerminationActivity* previous_;
    inline static thread_local TerminationActivity* current_ = nullptr;
};

class LifecycleTransitionActivity {
public:
    explicit LifecycleTransitionActivity(SubsystemLifecycleState& state)
        : state_(state), previous_(current_) {
        ++state_.transitions;
        current_ = this;
    }
    ~LifecycleTransitionActivity() {
        current_ = previous_;
        --state_.transitions;
    }
    LifecycleTransitionActivity(const LifecycleTransitionActivity&) = delete;
    LifecycleTransitionActivity& operator=(const LifecycleTransitionActivity&) = delete;
    static bool active_for(const SubsystemLifecycleState* state) {
        for (auto* activity = current_; activity; activity = activity->previous_)
            if (&activity->state_ == state) return true;
        return false;
    }
private:
    SubsystemLifecycleState& state_;
    LifecycleTransitionActivity* previous_;
    inline static thread_local LifecycleTransitionActivity* current_ = nullptr;
};

// The winner publishes completion on every exit, after all teardown guards.
class TerminationFlightOwner {
public:
    explicit TerminationFlightOwner(SubsystemLifecycleState& state) : state_(state) {}
    ~TerminationFlightOwner() {
        {
            std::lock_guard<std::mutex> lock(state_.flight_mutex);
            state_.flight_finished = state_.flight_started;
        }
        state_.flight_completed.notify_all();
    }
    TerminationFlightOwner(const TerminationFlightOwner&) = delete;
    TerminationFlightOwner& operator=(const TerminationFlightOwner&) = delete;
private:
    SubsystemLifecycleState& state_;
};

void erase_subsystem_lifecycle_lock(const Subsystem* subsystem) {
    auto& registry = subsystem_lifecycle_registry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    registry.locks.erase(subsystem);
}


void clear_subsystem_initialization_success(const Subsystem* subsystem) {
    auto& registry = subsystem_initialization_registry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    registry.successfully_initialized.erase(subsystem);
}

void mark_subsystem_initialization_success(const Subsystem* subsystem) {
    auto& registry = subsystem_initialization_registry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    registry.successfully_initialized.insert(subsystem);
}

bool subsystem_initialization_succeeded(const Subsystem* subsystem) {
    auto& registry = subsystem_initialization_registry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    return registry.successfully_initialized.find(subsystem) != registry.successfully_initialized.end();
}

std::string env_string_value(const char* name, const std::string& fallback = {}) {
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0') {
        return fallback;
    }
    return raw;
}

// Upkeep interval in seconds from name, as the Python SDK: default 5.0, at
// least 1.0. A value that is not a number keeps the default.
std::chrono::steady_clock::duration upkeep_interval_from_env(const char* name) {
    double seconds = 5.0;
    if (const char* raw = std::getenv(name); raw != nullptr && *raw != '\0') {
        char* end = nullptr;
        const double parsed = std::strtod(raw, &end);
        if (end != raw && *end == '\0' && std::isfinite(parsed)) seconds = parsed;
    }
    return std::chrono::duration_cast<std::chrono::steady_clock::duration>(
        std::chrono::duration<double>(std::max(1.0, seconds)));
}

int heartbeat_port_from_env(int configured_port) {
    return configured_port > 0 ? configured_port : 40000;
}

std::string control_socket_path() {
    const auto explicit_path = env_string_value(sdk_environment::kControlSocketEnv);
    if (!explicit_path.empty()) {
        return explicit_path;
    }
    return sdk_environment::kDefaultControlSocketPath;
}

// Fire-and-forget control-socket notification (heartbeat status, frame
// transforms). The whole exchange is bounded by
// sdk_environment::kBestEffortControlTimeoutMs: every blocking call gets only
// the time remaining until one deadline (best_effort_notification.hpp), so a
// stalled control backend cannot block the calling thread longer. Returns the
// failure reason; std::nullopt means the backend received the request.
#if defined(__GNUC__)
__attribute__((cold))
#endif
std::optional<std::string> send_control_json_best_effort(const std::string& body) {
    return detail::send_json_best_effort(control_socket_path(), body, sdk_environment::kBestEffortControlTimeoutMs);
}

using r2kernel::sdk_detail::json_escape;

std::string device_state_name(DeviceState state) {
    switch (state) {
    case DeviceState::created:
        return "Created";
    case DeviceState::booting:
        return "Booting";
    case DeviceState::registering:
        return "Registering";
    case DeviceState::disconnected:
        return "Disconnected";
    case DeviceState::waiting_binding:
        return "WaitingBinding";
    case DeviceState::waiting_target:
        return "WaitingTarget";
    case DeviceState::ready:
        return "Ready";
    case DeviceState::operating:
        return "Operating";
    case DeviceState::pausing:
        return "Pausing";
    case DeviceState::paused:
        return "Paused";
    case DeviceState::resuming:
        return "Resuming";
    case DeviceState::stopping:
        return "Stopping";
    case DeviceState::stopped:
        return "Stopped";
    case DeviceState::resetting:
        return "Resetting";
    case DeviceState::degraded:
        return "Degraded";
    case DeviceState::faulted:
        return "Faulted";
    case DeviceState::error:
        return "Error";
    }
    return "unknown";
}

std::string normalize_lifecycle_transition(std::string value) {
    const auto first = std::find_if_not(value.begin(), value.end(), [](unsigned char ch) {
        return std::isspace(ch) != 0;
    });
    const auto last = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char ch) {
        return std::isspace(ch) != 0;
    }).base();
    if (first >= last) {
        value.clear();
    } else {
        value = std::string(first, last);
    }
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    std::replace(value.begin(), value.end(), '-', '_');
    return value;
}

void json_skip_ws(const std::string& text, std::size_t& pos) {
    while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos])) != 0) {
        ++pos;
    }
}

std::optional<std::pair<std::size_t, std::size_t>> json_value_range(const std::string& text, std::size_t pos) {
    json_skip_ws(text, pos);
    if (pos >= text.size()) {
        return std::nullopt;
    }
    const std::size_t start = pos;
    const char first = text[pos];
    if (first == '"') {
        ++pos;
        bool escaped = false;
        while (pos < text.size()) {
            const char ch = text[pos++];
            if (escaped) {
                escaped = false;
                continue;
            }
            if (ch == '\\') {
                escaped = true;
                continue;
            }
            if (ch == '"') {
                return std::make_pair(start, pos);
            }
        }
        return std::nullopt;
    }
    if (first == '{' || first == '[') {
        const char open = first;
        const char close = first == '{' ? '}' : ']';
        int depth = 0;
        bool in_string = false;
        bool escaped = false;
        while (pos < text.size()) {
            const char ch = text[pos++];
            if (in_string) {
                if (escaped) {
                    escaped = false;
                } else if (ch == '\\') {
                    escaped = true;
                } else if (ch == '"') {
                    in_string = false;
                }
                continue;
            }
            if (ch == '"') {
                in_string = true;
                continue;
            }
            if (ch == open) {
                ++depth;
            } else if (ch == close) {
                --depth;
                if (depth == 0) {
                    return std::make_pair(start, pos);
                }
            }
        }
        return std::nullopt;
    }
    while (pos < text.size()) {
        const char ch = text[pos];
        if (ch == ',' || ch == '}' || ch == ']') {
            break;
        }
        ++pos;
    }
    return std::make_pair(start, pos);
}

// Raw text of one member of the top-level JSON object `text`. Nested values
// are skipped whole, so a same-named key inside `params`, `payload` or
// `result` never matches (Control sends keys sorted, so `params` comes before
// `request_id`, `target` and `timeout_ms`; sdk/testdata/wire).
std::optional<std::string> json_raw_field(const std::string& text, std::string_view key) {
    std::size_t pos = 0;
    json_skip_ws(text, pos);
    if (pos >= text.size() || text[pos] != '{') {
        return std::nullopt;
    }
    ++pos;
    while (true) {
        json_skip_ws(text, pos);
        if (pos >= text.size() || text[pos] != '"') {
            return std::nullopt;
        }
        const auto name = json_value_range(text, pos);
        if (!name.has_value()) {
            return std::nullopt;
        }
        // Wire member names are plain identifiers: compare the raw text.
        const bool matched = std::string_view(text).substr(name->first + 1, name->second - name->first - 2) == key;
        pos = name->second;
        json_skip_ws(text, pos);
        if (pos >= text.size() || text[pos] != ':') {
            return std::nullopt;
        }
        const auto value = json_value_range(text, pos + 1);
        if (!value.has_value()) {
            return std::nullopt;
        }
        if (matched) {
            std::size_t end = value->second;
            while (end > value->first && std::isspace(static_cast<unsigned char>(text[end - 1])) != 0) {
                --end;
            }
            return text.substr(value->first, end - value->first);
        }
        pos = value->second;
        json_skip_ws(text, pos);
        if (pos >= text.size() || text[pos] != ',') {
            return std::nullopt;
        }
        ++pos;
    }
}

std::optional<std::string> json_string_field_local(const std::string& text, std::string_view key) {
    const auto raw = json_raw_field(text, key);
    if (!raw.has_value() || raw->size() < 2 || raw->front() != '"' || raw->back() != '"') {
        return std::nullopt;
    }
    std::string out;
    out.reserve(raw->size() - 2);
    bool escaped = false;
    for (std::size_t i = 1; i + 1 < raw->size(); ++i) {
        const char ch = (*raw)[i];
        if (escaped) {
            switch (ch) {
            case 'n':
                out.push_back('\n');
                break;
            case 'r':
                out.push_back('\r');
                break;
            case 't':
                out.push_back('\t');
                break;
            default:
                out.push_back(ch);
                break;
            }
            escaped = false;
            continue;
        }
        if (ch == '\\') {
            escaped = true;
            continue;
        }
        out.push_back(ch);
    }
    return out;
}

std::optional<long long> json_int_field_local(const std::string& text, std::string_view key) {
    const auto raw = json_raw_field(text, key);
    if (!raw.has_value()) {
        return std::nullopt;
    }
    try {
        return std::stoll(*raw);
    } catch (...) {
        return std::nullopt;
    }
}

std::optional<bool> json_bool_field_local(const std::string& text, std::string_view key) {
    const auto raw = json_raw_field(text, key);
    if (!raw.has_value()) {
        return std::nullopt;
    }
    if (*raw == "true") {
        return true;
    }
    if (*raw == "false") {
        return false;
    }
    return std::nullopt;
}

std::string json_null_safe(std::string value) {
    return value.empty() ? "null" : std::move(value);
}

bool starts_with(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.substr(0, prefix.size()) == prefix;
}

std::string trim_copy(std::string value) {
    auto begin = value.begin();
    while (begin != value.end() && std::isspace(static_cast<unsigned char>(*begin)) != 0) {
        ++begin;
    }
    auto end = value.end();
    while (end != begin && std::isspace(static_cast<unsigned char>(*(end - 1))) != 0) {
        --end;
    }
    return std::string(begin, end);
}

using detail::normalize_timing_support;
