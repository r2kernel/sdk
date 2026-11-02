#include "r2kernel/core/client.hpp"

#include "control_source_topic.hpp"
#include "manager_peer_auth.hpp"
#include "callback_authority_scope.hpp"
#include "control_socket_env.hpp"
#include "control_requests.hpp"
#include "json_text.hpp"
#include "control_registration.hpp"
#include "control_shm_peer_reset.hpp"

#include "r2kernel_core.h"
#include "r2kernel_core_control.h"
#include <yaml-cpp/yaml.h>
#include <array>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cctype>
#include <condition_variable>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <mutex>
#include <optional>
#include <random>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <grp.h>
#include <pthread.h>
#include <sched.h>
#include <netinet/in.h>
#include <sys/mman.h>
#include <sys/poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/types.h>
#include <unistd.h>
#endif
// Impl uses this unit's anonymous-namespace types by design (one unit, below);
// GCC's -Wsubobject-linkage ODR heuristic cannot apply. No codegen effect.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wsubobject-linkage"
#endif

// One translation unit, split for size into private fragments that are
// included below in their original source order. A single unit keeps the
// codegen of per-call paths together (the SDK is built without LTO).
// Fragments are not standalone headers.
namespace r2kernel::core {

namespace {
thread_local std::optional<r2kernel::wrapper::ControlSourceAuthority> active_control_authority;
thread_local std::optional<r2kernel::wrapper::ProtocolHostAuthority> active_host_authority;
struct TransportAuthorityScope {
    std::optional<r2kernel::wrapper::ControlSourceAuthority> previous_control=std::move(active_control_authority);
    std::optional<r2kernel::wrapper::ProtocolHostAuthority> previous_host=std::move(active_host_authority);
    explicit TransportAuthorityScope(r2kernel::wrapper::ControlRequest& request, bool manager_verified) {
      try {
        active_control_authority.reset(); active_host_authority.reset();
        const auto& sender=request.sender;
        const std::string control_prefix="__r2kernel_authority_v1__:";
        const std::string host_prefix="__r2kernel_host_v1__:";
        const bool control=sender.rfind("__r2kernel_authority_",0)==0;
        const bool host=sender.rfind("__r2kernel_host_",0)==0;
        if(!control && !host) return;
        if(!manager_verified) throw std::runtime_error("Authenticated transport context requires a verified manager connection");
        const auto& prefix=control?control_prefix:host_prefix;
        if(sender.rfind(prefix,0)!=0 || sender.size()>prefix.size()+4096) throw std::runtime_error("Unsupported authenticated transport context");
        // The exact field set of the Python SDK, every value a nonempty string
        // except codec_source; then the route and kind checks.
        static const std::vector<const char*> control_fields={"peer","epoch","source","source_session","source_registration",
            "source_reference","target","target_session","target_registration","interface","binding_revision","topic"};
        static const std::vector<const char*> host_fields={"kind","peer","epoch","codec_source"};
        const auto& expected=control?control_fields:host_fields;
        const auto malformed=[] {return std::runtime_error("Malformed authenticated transport context");};
        YAML::Node metadata;
        try {metadata=YAML::Load(sender.substr(prefix.size()));} catch (const YAML::Exception&) {throw malformed();}
        if(!metadata.IsMap() || metadata.size()!=expected.size()) throw malformed();
        std::map<std::string,std::string> fields;
        for(const char* field : expected) {
            const auto value=metadata[field];
            if(!value || !value.IsScalar()) throw malformed();
            auto text=value.as<std::string>();
            if(text.empty() && std::string_view(field)!="codec_source") throw malformed();
            fields.emplace(field,std::move(text));
        }
        if(control) {
            if(fields.at("topic")!=request.topic) throw std::runtime_error("Authenticated Control route mismatch");
            active_control_authority=r2kernel::wrapper::ControlSourceAuthority{fields.at("peer"),fields.at("source"),fields.at("source_session"),fields.at("source_reference"),
                fields.at("target_session"),fields.at("interface"),fields.at("binding_revision"),fields.at("source_registration"),fields.at("target_registration"),fields.at("epoch"),fields.at("target"),fields.at("topic")};
            request.sender=active_control_authority->source;
        } else {
            if(fields.at("kind")!="protocol") throw std::runtime_error("Authenticated host authority kind mismatch");
            active_host_authority=r2kernel::wrapper::ProtocolHostAuthority{fields.at("kind"),fields.at("peer"),fields.at("epoch")};
            request.sender="host:"+active_host_authority->peer;
        }
      } catch (...) {
        active_control_authority=std::move(previous_control); active_host_authority=std::move(previous_host); throw;
      }
    }
    ~TransportAuthorityScope(){active_control_authority=std::move(previous_control);active_host_authority=std::move(previous_host);}
};

bool valid_resolved_control_port(long long port, std::string_view route_kind,
                                 bool production_local_control_available) {
    if (port < 0 || port > std::numeric_limits<std::uint16_t>::max()) return false;
    if (port > 0) return true;
    return route_kind == "local_control" && production_local_control_available;
}


void validate_prepared_control_peer(const YAML::Node& document, const std::string& target,
                                   const std::string& topic, const std::array<std::string, 4>& expected) {
    constexpr std::string_view procedure_prefix = "procedure.call_typed/";
    constexpr std::string_view operation_prefix = "operation.start_typed/";
    const std::string_view route(topic);
    const bool procedure = route.substr(0, procedure_prefix.size()) == procedure_prefix;
    const bool operation = route.substr(0, operation_prefix.size()) == operation_prefix;
    // Control sends null until the peer reports codec status (as Python, only
    // an explicit false refuses; sdk/testdata/wire/resolve-control-target-reply).
    const auto codec_context = document["codec_context_available"];
    if (codec_context && codec_context.IsScalar() && !codec_context.as<bool>())
        throw std::runtime_error("Prepared Control peer codec context is unavailable: " + target);
    const auto metadata = document["codec_metadata"];
    if (!metadata || !metadata.IsMap()) throw std::runtime_error("Prepared Control peer is missing codec identity: " + target);
    const auto valid_fingerprint = [](const std::string& fingerprint) {
        return fingerprint.size() == 64 && std::all_of(fingerprint.begin(), fingerprint.end(),
            [](unsigned char value) { return std::isxdigit(value) != 0; });
    };
    if (expected[0].empty() || !valid_fingerprint(expected[1]) ||
        (operation ? !expected[2].empty() || !expected[3].empty() : expected[2].empty() || !valid_fingerprint(expected[3])))
        throw std::runtime_error("Prepared Control peer endpoint contract mismatch: " + target + "/" + topic);
    // Operation start returns its protocol JSON reply, not a message codec.
    // Its declared request is still fingerprint-checked against this endpoint.
    const std::size_t fields = operation ? 2 : expected.size();
    for (std::size_t index = 0; index < fields; index += 2) {
        const auto codec = metadata[expected[index]];
        if (!codec || !codec.IsMap() || !codec["fingerprint"] ||
            codec["fingerprint"].as<std::string>() != expected[index + 1])
            throw std::runtime_error("Prepared Control peer schema fingerprint mismatch: " + target + "/" + topic + " ref=" + expected[index]);
    }
    const auto contracts = document[operation ? "operation_contracts" : procedure ? "procedure_contracts" : "control_contracts"];
    const auto key = operation ? route.substr(operation_prefix.size()) : procedure ? route.substr(procedure_prefix.size()) : route;
    bool matched = false;
    if (contracts && contracts.IsMap() && !key.empty()) {
        const auto endpoint = contracts[std::string(key)];
        matched = endpoint && endpoint.IsMap() && endpoint["request_type"] &&
            endpoint["request_type"].as<std::string>() == expected[0] &&
            (operation || (endpoint["response_type"] && endpoint["response_type"].as<std::string>() == expected[2]));
    }
    if (!matched) throw std::runtime_error("Prepared Control peer endpoint contract mismatch: " + target + "/" + topic);
}

#if !defined(_WIN32)
int wait_fd_readable(int fd, int timeout_ms) {
    pollfd item {};
    item.fd = fd;
    item.events = POLLIN;
    while (true) {
        const int rc = ::poll(&item, 1, timeout_ms);
        if (rc < 0 && errno == EINTR) {
            continue;
        }
        return rc;
    }
}
#endif

#include "client_protocol.hpp"

#include "client_control_exchange.hpp"

}  // namespace

#include "control_handler_publication.hpp"

struct R2KernelClient::Impl {
    struct PublishSlot {
        std::size_t slot_index = 0;
        std::size_t size = 0;
        std::size_t queue_depth = 1;
        std::size_t entry_slot_count = 1;
        std::uint64_t generation = 0;
        int priority = 5;
    };

    struct SubscriptionSlot {
        std::size_t slot_index = 0;
        std::size_t queue_depth = 1;
        std::size_t entry_slot_count = 1;
        std::string lock_name;
    };

    explicit Impl(ClientOptions in_options)
        : options(std::move(in_options)) {
        if (options.subsystem_name.empty()) {
            throw std::runtime_error("Subsystem name is required");
        }
        if (options.session_id.empty()) {
            if (const auto session = env_string_value("R2KERNEL_SUBSYSTEM_SESSION_ID")) {
                options.session_id = *session;
            }
        }
        if (options.session_id.empty()) {
            options.session_id = make_session_id(options.subsystem_name);
        }
        if (options.socket_timeout_ms <= 0) {
            options.socket_timeout_ms = 200;
        }
        notify_endpoint = make_notify_endpoint();
        notify_socket = create_unix_listener(notify_endpoint);
        register_subsystem();
        open_runtime_handles();
    }

    ~Impl() {
        stop_transport();
        stop_control_server();
#if !defined(_WIN32)
        close_event_inline();
        if (env_int_value("R2KERNEL_CONTROL_SHM_METRICS", 0) == 1)
            std::cerr << control_shm_metrics.json() << '\n';
#endif
        inform_subsystem_down();
        clear_publish_locks();
        control_ready.clear();
        if (event_client != nullptr) {
            r2kernel_event_client_close(event_client);
            event_client = nullptr;
        }
        if (shm_handle != nullptr) {
            r2kernel_shm_close(shm_handle);
            shm_handle = nullptr;
        }
        close_socket(notify_socket);
        notify_socket = kInvalidSocket;
        if (!notify_endpoint.empty()) {
            (void)::unlink(notify_endpoint.c_str());
            notify_endpoint.clear();
        }
    }

#include "client_impl_events.hpp"

#include "client_impl_local_sockets.hpp"

#include "client_impl_control_server.hpp"

#if !defined(_WIN32)
#include "control_shm_client_impl.hpp"

    bool handshake_control_managed(const std::string& target, int timeout_ms) {
        return handshake_control_managed_until(target, timeout_ms,
            std::chrono::steady_clock::now() + std::chrono::milliseconds(std::max(timeout_ms, 1)));
    }

    bool handshake_control_managed_until(const std::string& target, int timeout_ms,
        ControlShmFraming::Deadline deadline) {
        std::string control_endpoint;
        std::string host;
        int port = 0;
        {
            std::lock_guard<std::mutex> guard(mutex);
            const auto route_it = control_routes.find(target);
            if (local_control_endpoint.empty() || route_it == control_routes.end()) {
                return false;
            }
            control_endpoint = local_control_endpoint;
            host = route_it->second.first;
            port = route_it->second.second;
        }
        if (control_endpoint.empty()) {
            return false;
        }
        return handshake_control_shm_until(target, host, port, control_endpoint, timeout_ms, deadline);
    }

    std::optional<r2kernel::wrapper::ControlResponse> transact_control_managed(
        const std::string& target, const std::string& topic,
        const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
        float period_ms, float deadline_ms, int priority, int requested_timeout_ms,
        bool result_ready, std::chrono::steady_clock::time_point call_started) {
        const auto started = call_started;
        const int timeout_ms = requested_timeout_ms > 0 ? requested_timeout_ms : 1000;
        auto session = prepare_control_shm_session(target, topic);
        if (!session) return std::nullopt;
        return transact_control_shm_session(session, payload, timestamp_ns, period_ms,
            deadline_ms, priority, timeout_ms, result_ready, call_started, started);
    }
#endif

    bool connect_control(const std::string& name,
                      const std::string& host,
                      int port) {
#if !defined(_WIN32)
        std::vector<std::shared_ptr<ControlShmClientChannel>> retired;
#endif
        {
            std::lock_guard<std::mutex> guard(mutex);
#if !defined(_WIN32)
            std::lock_guard<std::mutex> registry_guard(control_shm_channels_mutex);
            if (control_shm_stop_in_progress)
                throw std::runtime_error("Cannot reconnect a Control target during client shutdown");
            std::vector<std::unique_lock<std::mutex>> lane_locks;
            for (const auto& entry : control_shm_channels) {
                if (entry.first.first != name) continue;
                lane_locks.emplace_back(entry.second->operation_mutex, std::try_to_lock);
                if (!lane_locks.back().owns_lock() || entry.second->has_pending_release())
                    throw std::runtime_error("Cannot reconnect a Control target while its lane is busy or pending reclamation");
                entry.second->require_reclaimed(); // Rebind must not erase sticky release failures.
                retired.push_back(entry.second);
            }
            for (const auto& channel : retired) channel->invalidate();
            for (auto entry = control_shm_channels.begin(); entry != control_shm_channels.end();) {
                if (entry->first.first == name) entry = control_shm_channels.erase(entry);
                else ++entry;
            }
            control_shm_accepting_calls = true;
#endif
            prepared_codec_routes.erase(name);
            prepared_codec_checked_at.erase(name);
            control_routes[name] = {host, port};
            control_ready[name] = false;
#if !defined(_WIN32)
            control_shm_route_readiness[name] = std::make_shared<std::atomic<bool>>(false);
#endif
            control_pending_responses.erase(name);
        }
#if !defined(_WIN32)
        // No registry/client mutex is held while stopping or joining owners.
        for (const auto& channel : retired) channel->stop();
#endif
        return true;
    }

    bool handshake_control(const std::string& target, int timeout_ms) {
#if defined(_WIN32)
        (void)target;
        (void)timeout_ms;
        return false;
#else
        return handshake_control_managed(target, timeout_ms);
#endif
    }

    bool send_control(const std::string& target,
                   const std::string& topic,
                   const r2kernel::wrapper::ByteBuffer& payload,
                   std::int64_t timestamp_ns,
                   float period_ms,
                   float deadline_ms,
                   int priority) {
#if defined(_WIN32)
        return false;
#else
        const auto call_started = std::chrono::steady_clock::now();
        const double budget_ms = deadline_ms > 0.0F && std::isfinite(deadline_ms)
            ? static_cast<double>(deadline_ms) : 1000.0;
        const int timeout_ms = static_cast<int>(std::min(
            std::ceil(budget_ms), static_cast<double>(std::numeric_limits<int>::max())));
        const auto deadline = call_started + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::duration<double, std::milli>(std::min(budget_ms,
                static_cast<double>(std::numeric_limits<int>::max()))));
        // Reject a prepared tombstone before even the optional handshake can
        // admit a new channel. The actual transaction rechecks this identity.
        if (!prepare_control_shm_session(target, topic)) return false;
        if (!is_control_ready(target)) {
            if (!handshake_control_managed_until(target, timeout_ms, deadline)) return false;
        }
        if (std::chrono::steady_clock::now() >= deadline) return false;
        return send_control_raw_until(target, topic, payload, timestamp_ns,
            period_ms, deadline_ms, priority, timeout_ms, call_started);
#endif
    }

    std::optional<r2kernel::wrapper::ControlResponse> recv_control(const std::string& target,
                                                         int timeout_ms) {
        auto response = recv_control_raw(target, timeout_ms);
        if (response.has_value()) {
            set_control_ready(target, true);
        }
        return response;
    }

#include "client_impl_runtime_registration.hpp"

    ClientOptions options;
    SocketHandle notify_socket = kInvalidSocket;
    std::string notify_endpoint;
    R2KernelShmHandle* shm_handle = nullptr;
    R2KernelEventClient* event_client = nullptr;
    std::string shm_name;
    std::size_t slot_size = 0;
    std::size_t slot_count = 0;
    std::mutex mutex;
    std::condition_variable startup_barrier_cv;
    std::unordered_map<std::string, r2kernel::wrapper::EventPublishSpec> publish_specs;
    std::unordered_map<std::string, PublishSlot> publish_slots;
    std::unordered_map<std::string, std::unordered_map<std::string, R2KernelLockHandle*>> publish_locks;
    std::unordered_map<std::string, r2kernel::wrapper::EventSubscribeSpec> subscribe_specs;
    r2kernel::wrapper::LifecycleTransitionHandler lifecycle_transition_handler;
    std::size_t max_subscribe_payload_size = 0;
    std::map<std::pair<std::string, std::string>, SubscriptionSlot> subscriptions;
    std::thread notify_thread;
    std::thread recv_thread;
    std::atomic<bool> stop_requested {false};
    std::atomic<bool> transport_running {false};
    std::mutex manager_peer_auth_mutex;
    bool manager_peer_auth_configured=false;
    bool manager_peer_auth_required=true;
    std::string manager_peer_auth_socket;
    std::string event_inline_endpoint;
    std::size_t event_inline_payload_threshold = 4 * 1024;
    // Set once a peer verification refused this registration's Event inline
    // endpoint: inline delivery stays off until the subsystem registers again.
    std::atomic<bool> event_inline_refused{false};
#if !defined(_WIN32)
    std::mutex event_inline_mutex;
    int event_inline_fd = -1;
    std::string event_inline_connected_endpoint;
    std::string event_inline_local_endpoint;
    std::thread event_inline_local_thread;
#endif
    int control_listen_port = 0;
    std::string local_control_endpoint;
    std::string control_requested_host = "0.0.0.0";
    int control_requested_port = 0;
    std::string control_local_endpoint;
#if !defined(_WIN32)
    ControlShmMetrics control_shm_metrics;
    bool control_shm_negotiated = false; // Protected by control_shm_channels_mutex.
    std::atomic<bool> control_shm_accepting_calls{true};
    const std::uint64_t control_shm_client_identity = next_control_shm_client_identity();
    std::atomic<std::uint64_t> control_shm_endpoint_generation{1};
    std::atomic<std::uint64_t> control_shm_codec_generation{1};
    bool control_shm_stop_in_progress = false; // Protected by control_shm_channels_mutex.
    std::mutex control_shm_channels_mutex;
    std::map<std::pair<std::string, std::string>, std::shared_ptr<ControlShmClientChannel>> control_shm_channels;
    std::unordered_map<std::string, std::shared_ptr<std::atomic<bool>>> control_shm_route_readiness;
#endif
    std::unordered_map<std::string, std::pair<std::string, int>> control_routes;
    std::unordered_map<std::string, r2kernel::wrapper::ControlResponse> control_pending_responses;
    std::unordered_set<std::string> control_inflight_targets;
    std::map<std::string, std::map<std::string, std::array<std::string, 4>>> prepared_codec_routes;
    std::map<std::string, std::map<std::string, std::chrono::steady_clock::time_point>> prepared_codec_checked_at;
    std::unordered_map<std::string, bool> control_ready;
    ControlHandlerPublication control_handler_publication;
    std::thread control_thread;
    std::atomic<bool> control_stop_requested {false};
    mutable std::mutex control_worker_mutex;
    bool control_worker_configured = false, control_worker_started = false;
    int control_worker_policy = 0, control_worker_priority = 0;
    std::vector<r2kernel::wrapper::ControlWorkerStatus> control_worker_diagnostics;
#if defined(__linux__)
    cpu_set_t control_worker_affinity{};
#endif
    int startup_retry_count = std::max(1, env_int_value("R2KERNEL_STARTUP_RETRY_COUNT", 20));
    int startup_retry_delay_ms = std::max(1, env_int_value("R2KERNEL_STARTUP_RETRY_DELAY_MS", 100));
    // Per-request timeout of the registration requests, as in the Python SDK (at least 100 ms).
    int startup_request_timeout_ms = std::max(100, env_int_value("R2KERNEL_STARTUP_REQUEST_TIMEOUT_MS", 2000));
    bool startup_barrier_env_override = std::getenv("R2KERNEL_EVENT_STARTUP_BARRIER_MS") != nullptr;
    long long startup_barrier_ms = std::max(0, env_int_value("R2KERNEL_EVENT_STARTUP_BARRIER_MS", 0));
    std::unordered_set<std::string> startup_barrier_done;
    // Control registration upkeep (heartbeat thread, except down_reported);
    // the cold methods are defined in client_control_registration.hpp.
    std::optional<std::pair<std::uint64_t, std::uint64_t>> event_pool_identity;
    bool event_pool_replaced = false;
    std::atomic<bool> down_reported {false};
    std::shared_ptr<detail::ControlRegistrationKeeper> registration_keeper;
    std::string reregister_subsystem();
    std::size_t restore_event_registrations();
};

R2KernelClient::R2KernelClient(ClientOptions options)
    : impl_(std::make_unique<Impl>(std::move(options))) {}

R2KernelClient::~R2KernelClient() = default;

std::string R2KernelClient::session_id() const {
    return impl_->options.session_id;
}

std::int64_t R2KernelClient::now_ns() const {
    return impl_->now_ns();
}

bool R2KernelClient::refresh_clock_status() {
    return impl_->refresh_clock_status();
}

void R2KernelClient::set_lifecycle_transition_handler(r2kernel::wrapper::LifecycleTransitionHandler handler) {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    impl_->lifecycle_transition_handler = std::move(handler);
}

void R2KernelClient::start_event_transport() {
    impl_->start_transport();
}

void R2KernelClient::stop_event_transport() {
    impl_->stop_transport();
}

bool R2KernelClient::register_event_publish(const r2kernel::wrapper::EventPublishSpec& spec) {
    return impl_->register_publish(spec, impl_->startup_retry_count);
}

bool R2KernelClient::register_event_subscribe(const r2kernel::wrapper::EventSubscribeSpec& spec) {
    return impl_->register_subscribe(spec, impl_->startup_retry_count, false);
}

bool R2KernelClient::publish_event(const std::string& topic,
                               const r2kernel::wrapper::EventPayload& payload,
                               std::int64_t timestamp_ns,
                               int priority) {
    return impl_->publish(topic, payload, timestamp_ns, priority);
}

int R2KernelClient::bind_control(int port, const std::string& host) {
    return impl_->bind_control(port, host);
}

bool R2KernelClient::start_control_server(const r2kernel::wrapper::ControlHandler& handler) {
    return impl_->start_control_server(handler);
}

void R2KernelClient::stop_control_server() {
    impl_->stop_control_server();
}

int R2KernelClient::get_control_port() const {
    return impl_->get_control_port();
}

std::optional<r2kernel::wrapper::ControlRoute> R2KernelClient::resolve_control_target(const std::string& target) {
    return impl_->resolve_control_target(target);
}

bool R2KernelClient::connect_control(const std::string& name,
                              const std::string& host,
                              int port) {
    return impl_->connect_control(name, host, port);
}

bool R2KernelClient::handshake_control(const std::string& target, int timeout_ms) {
    return impl_->handshake_control(target, timeout_ms);
}

bool R2KernelClient::send_control(const std::string& target,
                           const std::string& topic,
                           const r2kernel::wrapper::ByteBuffer& payload,
                           std::int64_t timestamp_ns,
                           float period_ms,
                           float deadline_ms,
                           int priority) {
    return impl_->send_control(target, topic, payload, timestamp_ns, period_ms, deadline_ms, priority);
}

std::optional<r2kernel::wrapper::ControlResponse> R2KernelClient::recv_control(const std::string& target,
                                                                 int timeout_ms) {
    return impl_->recv_control(target, timeout_ms);
}

std::optional<r2kernel::wrapper::ControlResponse> R2KernelClient::transact_control(
    const std::string& target, const std::string& topic,
    const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
    float period_ms, float deadline_ms, int timeout_ms, int priority) {
#if defined(_WIN32)
    throw std::runtime_error("Synchronous Control routes are unavailable on this platform");
#else
    const auto call_started = std::chrono::steady_clock::now();
    if (timeout_ms <= 0) throw std::invalid_argument("Control transaction timeout must be positive");
    // The response belongs to this call. It never enters the target-only send/recv
    // pending-response maps. SHM enforces one in-flight request per route.
    return impl_->transact_control_managed(target, topic, payload, timestamp_ns,
                                                  period_ms, deadline_ms, priority, timeout_ms, false, call_started);
#endif
}

std::optional<r2kernel::wrapper::ControlResponse> R2KernelClient::transact_control_result(
    const std::string& target, const std::string& topic,
    const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
    float period_ms, float deadline_ms, int timeout_ms, int priority) {
#if defined(_WIN32)
    throw std::runtime_error("Response-ready Control is unavailable on this platform");
#else
    const auto call_started = std::chrono::steady_clock::now();
    if (timeout_ms <= 0) throw std::invalid_argument("Control transaction timeout must be positive");
    // Production TX validates its route on the same lane and deadline; HSH is
    // neither provider readiness nor a separate transaction budget.
    return impl_->transact_control_managed(target, topic, payload, timestamp_ns,
        period_ms, deadline_ms, priority, timeout_ms, true, call_started);
#endif
}

std::optional<r2kernel::wrapper::ControlResponse> R2KernelClient::transact_control_started(
    const std::string& target, const std::string& topic,
    const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
    float period_ms, float deadline_ms, int timeout_ms, int priority,
    bool result_ready, std::chrono::steady_clock::time_point started) {
#if defined(_WIN32)
    throw std::runtime_error("Budgeted Control transport is unavailable on this platform");
#else
    if (timeout_ms <= 0) throw std::invalid_argument("Control transaction timeout must be positive");
    return impl_->transact_control_managed(target, topic, payload, timestamp_ns,
        period_ms, deadline_ms, priority, timeout_ms, result_ready, started);
#endif
}

bool R2KernelClient::ensure_control_route(const std::string& target,
    std::chrono::steady_clock::time_point deadline) {
    {
        std::lock_guard<std::mutex> guard(impl_->mutex);
        if (impl_->control_routes.find(target) != impl_->control_routes.end()) return true;
    }
    const auto route = impl_->resolve_control_target(target, deadline);
    if (!route) return false;
    if (std::chrono::steady_clock::now() >= deadline)
        throw std::runtime_error("Control route resolution exceeded invocation deadline");
    return impl_->connect_control(target, route->host, route->port);
}

bool R2KernelClient::prepare_control(const std::string& target, const std::string& topic,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms, bool result_ready) {
#if defined(_WIN32)
    throw std::runtime_error("Provider-ready Control preparation is unavailable on this platform");
#else
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    return impl_->prepare_control_shm(target, topic, request_capacity, response_capacity, timeout_ms, result_ready, deadline);
#endif
}

void R2KernelClient::configure_control_worker(const std::string& policy, int priority, const std::vector<int>& cpus) {
#if defined(_WIN32)
    throw std::runtime_error("Control worker scheduling profile requires Linux");
#else
    impl_->configure_control_worker(policy, priority, cpus);
#endif
}

std::vector<r2kernel::wrapper::ControlWorkerStatus> R2KernelClient::control_worker_status() const {
#if defined(_WIN32)
    throw std::runtime_error("Control worker diagnostics require Linux");
#else
    return impl_->control_worker_status();
#endif
}

void R2KernelClient::wait_control_reclaimed(const std::string& target, const std::string& topic, int timeout_ms) {
#if defined(_WIN32)
    throw std::runtime_error("Control reclamation is unavailable on this platform");
#else
    impl_->wait_control_shm_reclaimed(target, topic, timeout_ms);
#endif
}

void R2KernelClient::publish_prepared_status(const std::string& status_json) {
    const auto response = exchange_control_json(status_json, impl_->options.control_host,
        impl_->options.control_udp_port, std::max(impl_->options.socket_timeout_ms, 2000));
    auto document = YAML::Load(response);
    if (!document["res"] && document["data"]) document = document["data"];
    if (!document["res"] || document["res"].as<std::string>() != "ok"
        || !document["ok"] || !document["ok"].as<bool>())
        throw std::runtime_error("Prepared schema status publication was rejected: " + response);
}

void R2KernelClient::clear_control_codec_routes() {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    impl_->prepared_codec_routes.clear();
    impl_->prepared_codec_checked_at.clear();
#if !defined(_WIN32)
    impl_->control_shm_codec_generation.fetch_add(1, std::memory_order_release);
#endif
}
void R2KernelClient::prepare_control_route(const std::string& target, const std::string& topic,
    const std::string& request_ref, const std::string& request_fingerprint,
    const std::string& response_ref, const std::string& response_fingerprint) {
    prepare_control_route_until(target, topic, request_ref, request_fingerprint,
        response_ref, response_fingerprint, std::chrono::steady_clock::time_point::max());
}

void R2KernelClient::prepare_control_route_until(const std::string& target, const std::string& topic,
    const std::string& request_ref, const std::string& request_fingerprint,
    const std::string& response_ref, const std::string& response_fingerprint,
    std::chrono::steady_clock::time_point deadline) {
#if !defined(_WIN32)
    // PREPARE verified the live registrations/codecs on this exact lane. Do not
    // re-query or allocate codec registry entries on its first periodic TX.
    if (auto lane = control_shm_thread_sessions().find(impl_->control_shm_client_identity, target, topic,
        impl_->control_shm_endpoint_generation.load(std::memory_order_acquire),
        impl_->control_shm_codec_generation.load(std::memory_order_acquire))) {
        if (lane->provider_ready) {
            const auto& identity = lane->codec_identity;
            if (!identity || (*identity)[0] != request_ref || (*identity)[1] != request_fingerprint ||
                (*identity)[2] != response_ref || (*identity)[3] != response_fingerprint)
                throw std::runtime_error("Prepared Control transaction codec differs from its prepared lane");
            return;
        }
    }
#endif
    {
        std::lock_guard<std::mutex> guard(impl_->mutex);
        const auto peer = impl_->prepared_codec_routes.find(target);
        if (peer != impl_->prepared_codec_routes.end()) {
            const auto found = peer->second.find(topic);
            if (found != peer->second.end()) {
                const auto& cached = found->second;
                if (cached[0] != request_ref || cached[1] != request_fingerprint
                    || cached[2] != response_ref || cached[3] != response_fingerprint)
                    throw std::runtime_error("Prepared Control route schema changed; reconnect explicitly: " + target + "/" + topic);
                const auto checked_peer=impl_->prepared_codec_checked_at.find(target);
                if(checked_peer!=impl_->prepared_codec_checked_at.end()) {
                    const auto checked=checked_peer->second.find(topic);
                    if(checked!=checked_peer->second.end() && std::chrono::steady_clock::now()-checked->second<std::chrono::seconds(1)) return;
                }
            }
        }
    }
    // Cache preparation metadata for at most one second. The manager validates
    // live registration/session/binding authority for every packet; transport
    // failures invalidate this metadata without replaying the failed request.
    const std::array<std::string, 4> expected{request_ref, request_fingerprint, response_ref, response_fingerprint};
    impl_->verify_control_codec_peer(target, topic, expected, deadline);
    std::lock_guard<std::mutex> guard(impl_->mutex);
    const auto [found, inserted] = impl_->prepared_codec_routes[target].emplace(topic, expected);
    if (!inserted && found->second != expected) throw std::runtime_error("Concurrent prepared Control binding changed schema");
    impl_->prepared_codec_checked_at[target][topic]=std::chrono::steady_clock::now();
}

}  // namespace r2kernel::core

#include "client_control_requests.hpp"
#include "client_control_registration.hpp"

namespace r2kernel::wrapper {
std::optional<ControlSourceAuthority> current_control_source_authority(){return r2kernel::core::active_control_authority;}
std::optional<ProtocolHostAuthority> current_protocol_host_authority(){return r2kernel::core::active_host_authority;}
}

namespace r2kernel::core::detail {
CallbackAuthorityScope::CallbackAuthorityScope(std::optional<r2kernel::wrapper::ProtocolHostAuthority> host)
    : previous_control_(std::move(active_control_authority)), previous_host_(std::move(active_host_authority)) {
    active_control_authority.reset(); active_host_authority=std::move(host);
}
CallbackAuthorityScope::~CallbackAuthorityScope() {
    active_control_authority=std::move(previous_control_); active_host_authority=std::move(previous_host_);
}
}
