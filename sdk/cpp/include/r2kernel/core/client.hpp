#pragma once

#include <cstdint>
#include <functional>
#include <stdexcept>
#include <chrono>
#include <memory>
#include <optional>
#include <string>

#include "r2kernel/core/resource_broker_client.hpp"
#include "r2kernel/export.hpp"
#include "r2kernel/wrapper/core_client.hpp"

namespace r2kernel::core {

struct ClientOptions {
    std::string subsystem_name;
    std::string control_host = "127.0.0.1";
    int control_udp_port = 40000;
    int socket_timeout_ms = 200;
    std::string session_id;
};

// Control serves a different Event shared-memory pool than the one this
// process mapped (Control recreated it): the Event publications and
// subscriptions of this process cannot be restored until it restarts.
class R2KERNEL_CPP_API EventPoolReplacedError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class R2KERNEL_CPP_API R2KernelClient final : public r2kernel::wrapper::CoreClient {
public:
    explicit R2KernelClient(ClientOptions options);
    ~R2KernelClient() override;

    // Identity and clock.
    std::string session_id() const override;
    std::int64_t now_ns() const override;
    bool refresh_clock_status() override;

    // Lifecycle.
    void set_lifecycle_transition_handler(r2kernel::wrapper::LifecycleTransitionHandler handler) override;

    // Events.
    void start_event_transport() override;
    void stop_event_transport() override;
    bool register_event_publish(const r2kernel::wrapper::EventPublishSpec& spec) override;
    bool register_event_subscribe(const r2kernel::wrapper::EventSubscribeSpec& spec) override;
    bool publish_event(const std::string& topic,
                       const r2kernel::wrapper::EventPayload& payload,
                       std::int64_t timestamp_ns,
                       int priority = 5) override;

    // Control server.
    int bind_control(int port = 0, const std::string& host = "0.0.0.0") override;
    bool start_control_server(const r2kernel::wrapper::ControlHandler& handler) override;
    void stop_control_server() override;
    int get_control_port() const override;

    // Control client.
    std::optional<r2kernel::wrapper::ControlRoute> resolve_control_target(const std::string& target) override;
    bool connect_control(const std::string& name,
                         const std::string& host,
                         int port) override;
    bool handshake_control(const std::string& target, int timeout_ms = 1000) override;
    bool send_control(const std::string& target,
                      const std::string& topic,
                      const r2kernel::wrapper::ByteBuffer& payload,
                      std::int64_t timestamp_ns,
                      float period_ms,
                      float deadline_ms,
                      int priority) override;
    std::optional<r2kernel::wrapper::ControlResponse> recv_control(const std::string& target,
                                                                   int timeout_ms = 0) override;
    std::optional<r2kernel::wrapper::ControlResponse> transact_control(
        const std::string& target, const std::string& topic,
        const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
        float period_ms, float deadline_ms, int timeout_ms, int priority) override;
    std::optional<r2kernel::wrapper::ControlResponse> transact_control_result(
        const std::string& target, const std::string& topic,
        const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
        float period_ms, float deadline_ms, int timeout_ms, int priority) override;
    void wait_control_reclaimed(const std::string& target, const std::string& topic, int timeout_ms) override;
    // Wrapper entry preserves the original invocation budget through conversion.
    std::optional<r2kernel::wrapper::ControlResponse> transact_control_started(
        const std::string& target, const std::string& topic,
        const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
        float period_ms, float deadline_ms, int timeout_ms, int priority,
        bool result_ready, std::chrono::steady_clock::time_point started);
    bool ensure_control_route(const std::string& target, std::chrono::steady_clock::time_point deadline);

    // Preparation and provider worker profile.
    bool prepare_control(const std::string& target, const std::string& topic,
        std::size_t request_capacity, std::size_t response_capacity,
        int timeout_ms = 1000, bool result_ready = false) override;
    void prepare_control_route(const std::string& target, const std::string& topic,
        const std::string& request_ref, const std::string& request_fingerprint,
        const std::string& response_ref, const std::string& response_fingerprint) override;
    void prepare_control_route_until(const std::string& target, const std::string& topic,
        const std::string& request_ref, const std::string& request_fingerprint,
        const std::string& response_ref, const std::string& response_fingerprint,
        std::chrono::steady_clock::time_point deadline);
    void clear_control_codec_routes() override;
    void configure_control_worker(const std::string& policy, int priority, const std::vector<int>& cpus) override;
    std::vector<r2kernel::wrapper::ControlWorkerStatus> control_worker_status() const override;

    // Status publication.
    void publish_prepared_status(const std::string& status_json) override;

    // Self-binding of this client's registered subsystem requirement
    // (control.self_binding.mode: local). bind_requirement returns the bound
    // binding row as JSON; unbind_requirement returns whether a self-bound row
    // was removed. deadline is absolute; without it each control-socket
    // operation is bounded by 1 s, as in the Python SDK. Backend refusals throw
    // wrapper::ControlRequestError; the deadline and transport failures throw
    // std::runtime_error, a blank requirement or target std::invalid_argument.
    // No retry.
    std::string bind_requirement(const std::string& requirement, const std::string& target,
        std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt);
    bool unbind_requirement(const std::string& requirement,
        std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt);

    // Control registration upkeep for the subsystem heartbeat (cold control
    // plane). keep_registration runs one heartbeat: send_status returns the
    // failure reason or nullopt. When Control does not accept the status, it
    // runs register_again (registration, full status, endpoints) with bounded
    // backoff and logs every failure through log. Returns whether the
    // registration is still lost after this heartbeat.
    using RegistrationStep = std::function<std::optional<std::string>()>;
    using RegistrationLog = std::function<void(bool error, const std::string& message)>;
    bool keep_registration(const RegistrationStep& send_status, const RegistrationStep& register_again,
        const RegistrationLog& log);
    // Whether the last keep_registration left the registration lost (not yet
    // restored). Read it under the same serialization as keep_registration.
    bool registration_lost() const;
    // One registration attempt with this client's session, PID, lease and
    // notify endpoint. Throws on refusal or transport failure. Returns the
    // names of registration settings Control now offers differently that this
    // running client keeps (empty when none).
    std::string reregister_subsystem();
    // Requests every accepted Event publication and subscription again on the
    // new registration. Throws EventPoolReplacedError or on the first refusal.
    std::size_t restore_event_registrations();

    // Brokered resource descriptors of a managed package: each call creates a
    // ResourceBrokerClient from the environment (R2KERNEL_RESOURCE_LEASE_ID,
    // R2KERNEL_RESOURCE_SERVICE_SOCKET) and makes one broker request.
    BrokeredSocketResult open_udp_socket(const UdpSocketOptions& options) const;
    // Outbound TCP connection through the host's app uplink (AF_UNIX relay).
    BrokeredSocketResult open_uplink_tcp(const UplinkTcpOptions& options) const;
    BrokeredSocketResult open_tcp_socket(const TcpSocketOptions& options) const;
    BrokeredSocketResult open_frontend_tcp_listener(const FrontendTcpListenerOptions& options = {}) const;
    BrokeredSocketResult open_can_socket(const CanSocketOptions& options) const;
    BrokeredFdResult open_device(const std::string& path, const DeviceOpenOptions& options = {}) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace r2kernel::core
