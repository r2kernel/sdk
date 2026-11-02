// Included within R2KernelClient::Impl. Managed handler authority, Control
// worker scheduling, local handler server, target resolution and prepared
// codec route identity.
#if !defined(_WIN32)
    void configure_manager_peer_auth(const std::string& response) {
        const auto document=YAML::Load(response);
        const auto required=document["manager_peer_auth_required"];
        if(!required || !required.IsScalar()) throw std::runtime_error("Runtime did not negotiate managed handler peer authority");
        std::lock_guard<std::mutex> guard(manager_peer_auth_mutex);
        manager_peer_auth_required=detail::managed_peer_authentication_required();
        if(manager_peer_auth_required && !required.as<bool>()) throw std::runtime_error("Production SDK cannot accept disabled manager authentication");
        manager_peer_auth_socket=document["resource_service_socket"] && !document["resource_service_socket"].IsNull() ? document["resource_service_socket"].as<std::string>() : "";
        if(manager_peer_auth_required && manager_peer_auth_socket.empty()) throw std::runtime_error("Managed handler root broker socket is unavailable");
        manager_peer_auth_configured=true;
    }
    bool verify_incoming_manager(int descriptor,
        std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1)) {
        std::string broker; bool required;
        {std::lock_guard<std::mutex> guard(manager_peer_auth_mutex);
         if(!manager_peer_auth_configured) throw std::runtime_error("Managed handler peer authority is not ready");
         required=manager_peer_auth_required; broker=manager_peer_auth_socket;}
        if(required) detail::verify_manager_peer_connection(descriptor,broker,deadline);
        return required;
    }

    void configure_control_worker(const std::string& policy_name, int priority, const std::vector<int>& cpus) {
#if defined(__linux__)
        std::string normalized;
        for (const char ch : policy_name) normalized.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(ch))));
        if (normalized.rfind("SCHED_", 0) == 0) normalized.erase(0, 6);
        const int policy = normalized == "OTHER" ? SCHED_OTHER : normalized == "FIFO" ? SCHED_FIFO
            : normalized == "RR" ? SCHED_RR : -1;
        if (policy < 0) throw std::invalid_argument("Control worker policy must be OTHER, FIFO or RR");
        if (priority < ::sched_get_priority_min(policy) || priority > ::sched_get_priority_max(policy))
            throw std::invalid_argument("Control worker priority is outside the policy range");
        if (cpus.empty()) throw std::invalid_argument("Control worker cpus must be a nonempty list of CPU indices");
        cpu_set_t affinity; CPU_ZERO(&affinity);
        for (const int cpu : cpus) {
            if (cpu < 0 || cpu >= CPU_SETSIZE) throw std::invalid_argument("Control worker CPU exceeds affinity capacity");
            CPU_SET(cpu, &affinity);
        }
        std::lock_guard<std::mutex> guard(control_worker_mutex);
        if (control_worker_started) throw std::runtime_error("Control worker profile cannot change after a worker starts");
        control_worker_policy = policy; control_worker_priority = priority;
        control_worker_affinity = affinity;
        control_worker_configured = true;
#else
        (void)policy_name; (void)priority; (void)cpus;
        throw std::runtime_error("Control worker scheduling profile requires Linux");
#endif
    }

    std::vector<r2kernel::wrapper::ControlWorkerStatus> control_worker_status() const {
        std::lock_guard<std::mutex> guard(control_worker_mutex);
        return control_worker_diagnostics;
    }

    void prepare_control_worker() {
#if defined(__linux__)
        std::lock_guard<std::mutex> guard(control_worker_mutex);
        control_worker_started = true;
        r2kernel::wrapper::ControlWorkerStatus status;
        status.tid = ::syscall(SYS_gettid);
        const auto thread = ::pthread_self();
        if (control_worker_configured) {
            sched_param parameters{}; parameters.sched_priority = control_worker_priority;
            if (::pthread_setaffinity_np(thread, sizeof(cpu_set_t), &control_worker_affinity) != 0 ||
                ::pthread_setschedparam(thread, control_worker_policy, &parameters) != 0)
                status.error = "Control worker scheduling/affinity setup failed";
        }
        sched_param observed{};
        cpu_set_t affinity; CPU_ZERO(&affinity);
        if (::pthread_getschedparam(thread, &status.policy, &observed) != 0 ||
            ::pthread_getaffinity_np(thread, sizeof(affinity), &affinity) != 0) {
            status.error = "Control worker scheduling/affinity readback failed";
        } else {
            status.priority = observed.sched_priority;
            for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
                if (CPU_ISSET(cpu, &affinity)) status.cpus.push_back(cpu);
            if (control_worker_configured && (status.policy != control_worker_policy ||
                status.priority != control_worker_priority || !CPU_EQUAL(&affinity, &control_worker_affinity)))
                status.error = "Control worker scheduling/affinity readback mismatch";
        }
        status.ready = !status.error.has_value();
        status.phase = !control_worker_configured ? "profile_observed" : status.ready ? "profile_applied" : "profile_failed";
        control_worker_diagnostics.push_back(status);
        if (!status.ready) throw std::runtime_error(*status.error);
#endif
    }

    void local_control_connection_loop(int client_fd) {
        try {
            prepare_control_worker();
        } catch (const std::exception& error) {
            std::cerr << "Managed handler preparation rejected: " << error.what() << '\n';
            ::close(client_fd); return; // Manager observes failure; never reports provider readiness.
        }
        control_shm_handler_connection(client_fd, false);
    }

    void control_local_loop(int listen_fd) {
        using ConnectionWorker = std::pair<std::thread, std::shared_ptr<std::atomic<bool>>>;
        std::vector<ConnectionWorker> workers;
        while (!control_stop_requested.load()) {
            control_handler_publication.collect();
            for (auto it = workers.begin(); it != workers.end();) {
                if (it->second->load()) {
                    if (it->first.joinable()) {
                        it->first.join();
                    }
                    it = workers.erase(it);
                } else {
                    ++it;
                }
            }
            const int rc = wait_fd_readable(listen_fd, 200);
            if (rc <= 0) {
                continue;
            }
            const int client_fd = ::accept(listen_fd, nullptr, nullptr);
            if (client_fd < 0) {
                continue;
            }
            auto done = std::make_shared<std::atomic<bool>>(false);
            workers.emplace_back(
                std::thread([this, client_fd, done]() {
                    local_control_connection_loop(client_fd);
                    done->store(true);
                }),
                done
            );
        }

        ::close(listen_fd);
        for (auto& worker : workers) {
            if (worker.first.joinable()) {
                worker.first.join();
            }
        }
        cleanup_control_local_endpoint();
    }
#endif

    int bind_control(int port, const std::string& host) {
        if (port < 0 || port > 65535) throw std::invalid_argument("Control bind port must be 0..65535");
        control_requested_host = host.empty() ? "0.0.0.0" : host;
        control_requested_port = port;
        return control_listen_port > 0 ? control_listen_port : control_requested_port;
    }

    bool start_control_server(const r2kernel::wrapper::ControlHandler& handler) {
        control_handler_publication.publish(handler);
#if defined(_WIN32)
        (void)handler;
        return false;
#else
        if (control_thread.joinable()) {
            return register_control_handler_managed();
        }
        if (!prepare_local_control_endpoint()) {
            return false;
        }
        int listen_fd = -1;
        try {
            // Bind and publish the configured IPC group before registration
            // makes this handler available to another OS principal.
            listen_fd = create_unix_listener(control_local_endpoint, apply_control_handler_socket_policy);
        } catch (...) {
            cleanup_control_local_endpoint();
            throw;
        }
        if (!register_control_handler_managed()) {
            ::close(listen_fd);
            cleanup_control_local_endpoint();
            return false;
        }
        control_stop_requested.store(false);
        try {
            control_thread = std::thread([this, listen_fd]() { control_local_loop(listen_fd); });
        } catch (...) {
            control_stop_requested.store(true);
            ::close(listen_fd);
            cleanup_control_local_endpoint();
            control_listen_port = 0;
            throw;
        }
        return true;
#endif
    }

    void stop_control_server() {
        control_stop_requested.store(true);
        if (control_thread.joinable()) {
            control_thread.join();
        }
#if !defined(_WIN32)
        // Drain owners outside the registry lock; destruction joins their
        // persistent release receivers and interrupts pending socket waits.
        decltype(control_shm_channels) stopped_channels;
        {
            std::lock_guard<std::mutex> channel_guard(control_shm_channels_mutex);
            control_shm_accepting_calls = false;
            control_shm_stop_in_progress = true;
            stopped_channels.swap(control_shm_channels);
        }
        for (const auto& entry : stopped_channels) entry.second->stop();
#endif
        control_handler_publication.clear();
        std::lock_guard<std::mutex> guard(mutex);
        control_requested_port = 0;
        control_requested_host = "0.0.0.0";
        control_listen_port = 0;
        control_pending_responses.clear();
        control_inflight_targets.clear();
        control_routes.clear();
#if !defined(_WIN32)
        control_shm_route_readiness.clear();
        std::lock_guard<std::mutex> channel_guard(control_shm_channels_mutex);
        control_shm_stop_in_progress = false;
#endif
    }

    int get_control_port() const {
        return control_listen_port;
    }

    std::optional<r2kernel::wrapper::ControlRoute> resolve_control_target(const std::string& target,
        std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
        const std::string request = make_json_request(
            "\"cmd\":\"" + std::string(kResolveControlTarget) + "\","
            "\"subsystem\":\"" + json_escape(target) + "\""
        );
        const std::string response = exchange_control_json(
            request,
            options.control_host,
            options.control_udp_port,
            std::max(options.socket_timeout_ms, 2000), deadline
        );
        auto route = detail::parse_control_route(response);
        if (!route) return std::nullopt;
        bool production_local_control_available = false;
#if !defined(_WIN32)
        if (route->port == 0) {
            std::lock_guard<std::mutex> route_guard(mutex);
            std::lock_guard<std::mutex> registry_guard(control_shm_channels_mutex);
            production_local_control_available = control_shm_negotiated &&
                !local_control_endpoint.empty();
        }
#endif
        if (!valid_resolved_control_port(route->port, route->route_kind, production_local_control_available))
            return std::nullopt;
        return route;
    }

    void verify_control_codec_peer(const std::string& target, const std::string& topic,
                                   const std::array<std::string, 4>& expected,
                                   std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
        const auto response = exchange_control_json(make_json_request(
            "\"cmd\":\"" + std::string(kResolveControlTarget) + "\",\"subsystem\":\"" + json_escape(target) + "\""),
            options.control_host, options.control_udp_port, std::max(options.socket_timeout_ms, 2000), deadline);
        auto document = YAML::Load(response);
        if (!document["codec_metadata"] && document["data"]) document = document["data"];
        validate_prepared_control_peer(document, target, topic, expected);
    }

    void revalidate_control_codec_route(const std::string& target, const std::string& topic,
        std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
        std::optional<std::array<std::string, 4>> expected;
        {
            std::lock_guard<std::mutex> guard(mutex);
            const auto peer = prepared_codec_routes.find(target);
            if (peer != prepared_codec_routes.end()) {
                const auto route = peer->second.find(topic);
                if (route != peer->second.end()) expected = route->second;
            }
        }
        // Unprepared raw routes preserve compatibility. Prepared routes must
        // verify fresh peer identity before a new local SHM channel sends bytes.
        if (expected) verify_control_codec_peer(target, topic, *expected, deadline);
    }

    std::optional<std::array<std::string, 4>> prepare_control_codec_identity(
        const std::string& target, const std::string& topic, std::chrono::steady_clock::time_point deadline) {
        const auto response = exchange_control_json(make_json_request(
            "\"cmd\":\"" + std::string(kResolveControlTarget) + "\",\"subsystem\":\"" + json_escape(target) + "\""),
            options.control_host, options.control_udp_port, std::max(options.socket_timeout_ms, 2000), deadline);
        auto document = YAML::Load(response);
        if (!document["codec_metadata"] && document["data"]) document = document["data"];
        constexpr std::string_view procedure_prefix = "procedure.call_typed/";
        constexpr std::string_view operation_prefix = "operation.start_typed/";
        const std::string_view route(topic);
        const bool procedure = route.substr(0, procedure_prefix.size()) == procedure_prefix;
        const bool operation = route.substr(0, operation_prefix.size()) == operation_prefix;
        const auto contracts = document[operation ? "operation_contracts" : procedure ? "procedure_contracts" : "control_contracts"];
        const auto key = operation ? route.substr(operation_prefix.size()) : procedure ? route.substr(procedure_prefix.size()) : route;
        const auto endpoint = contracts && contracts.IsMap() ? contracts[std::string(key)] : YAML::Node{};
        if (!endpoint || !endpoint.IsMap() || !endpoint["request_type"]) {
            if (procedure || operation) throw std::runtime_error("Prepared Protocol endpoint contract is unavailable");
            return {};
        }
        const auto request_ref = endpoint["request_type"].as<std::string>();
        if (request_ref.empty() || request_ref == "bytes") return {};
        std::array<std::string, 4> identity;
        identity[0] = request_ref;
        const auto metadata = document["codec_metadata"];
        if (!metadata || !metadata.IsMap() || !metadata[request_ref] || !metadata[request_ref]["fingerprint"])
            throw std::runtime_error("Prepared Control request codec identity is unavailable");
        identity[1] = metadata[request_ref]["fingerprint"].as<std::string>();
        if (!operation) {
            if (!endpoint["response_type"]) throw std::runtime_error("Prepared Control response contract is unavailable");
            identity[2] = endpoint["response_type"].as<std::string>();
            if (!metadata[identity[2]] || !metadata[identity[2]]["fingerprint"])
                throw std::runtime_error("Prepared Control response codec identity is unavailable");
            identity[3] = metadata[identity[2]]["fingerprint"].as<std::string>();
        }
        validate_prepared_control_peer(document, target, topic, identity);
        {
            std::lock_guard<std::mutex> guard(mutex);
            const auto peer = prepared_codec_routes.find(target);
            if (peer != prepared_codec_routes.end()) {
                const auto existing = peer->second.find(topic);
                if (existing != peer->second.end() && existing->second != identity)
                    throw std::runtime_error("Prepared Control route codec identity changed before preparation");
            }
        }
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Control preparation codec verification exceeded deadline");
        return identity;
    }

    void invalidate_control_codec_route(const std::string& target, const std::string& topic) {
        std::lock_guard<std::mutex> guard(mutex);
        const auto peer = prepared_codec_routes.find(target);
        if (peer != prepared_codec_routes.end()) peer->second.erase(topic);
        const auto checked=prepared_codec_checked_at.find(target);
        if(checked!=prepared_codec_checked_at.end()) checked->second.erase(topic);
    }

    bool is_control_ready(const std::string& target) {
#if !defined(_WIN32)
        if (!control_shm_accepting_calls.load(std::memory_order_acquire)) return false;
        const auto ready = control_shm_thread_sessions().target_ready(control_shm_client_identity, target,
            control_shm_endpoint_generation.load(std::memory_order_acquire),
            control_shm_codec_generation.load(std::memory_order_acquire));
        if (ready) return *ready;
#endif
        std::lock_guard<std::mutex> guard(mutex);
        const auto it = control_ready.find(target);
        return it != control_ready.end() && it->second;
    }

    void set_control_ready(const std::string& target, bool ready) {
        std::lock_guard<std::mutex> guard(mutex);
        control_ready[target] = ready;
#if !defined(_WIN32)
        const auto publication = control_shm_route_readiness.find(target);
        if (publication != control_shm_route_readiness.end()) publication->second->store(ready, std::memory_order_release);
#endif
    }

    bool send_control_raw(const std::string& target,
                       const std::string& topic,
                       const r2kernel::wrapper::ByteBuffer& payload,
                       std::int64_t timestamp_ns,
                       float period_ms,
                       float deadline_ms,
                       int priority) {
        return send_control_raw_until(target, topic, payload, timestamp_ns,
            period_ms, deadline_ms, priority, 1000, std::chrono::steady_clock::now());
    }

    bool send_control_raw_until(const std::string& target,
        const std::string& topic, const r2kernel::wrapper::ByteBuffer& payload,
        std::int64_t timestamp_ns, float period_ms, float deadline_ms, int priority,
        int timeout_ms, std::chrono::steady_clock::time_point call_started) {
#if defined(_WIN32)
        (void)target;
        (void)topic;
        (void)payload;
        (void)timestamp_ns;
        (void)period_ms;
        (void)deadline_ms;
        return false;
#else
        if (!is_control_ready(target)) {
            return false;
        }
        {
            std::lock_guard<std::mutex> guard(mutex);
            if (!control_inflight_targets.insert(target).second)
                throw std::runtime_error("Control target already has an in-flight or unread response: " + target);
        }
        std::optional<r2kernel::wrapper::ControlResponse> response;
        try {
            response = transact_control_managed(target, topic, payload, timestamp_ns,
                period_ms, deadline_ms, priority, timeout_ms, false, call_started);
        } catch (...) {
            std::lock_guard<std::mutex> guard(mutex);
            control_inflight_targets.erase(target);
            throw;
        }
        if (!response.has_value()) {
            {
                std::lock_guard<std::mutex> guard(mutex);
                control_inflight_targets.erase(target);
            }
            set_control_ready(target, false);
            return false;
        }
        std::lock_guard<std::mutex> guard(mutex);
        control_pending_responses[target] = std::move(*response);
        control_ready[target] = true;
        return true;
#endif
    }

    std::optional<r2kernel::wrapper::ControlResponse> recv_control_raw(const std::string& target,
                                                             int timeout_ms) {
        (void)timeout_ms;
        std::lock_guard<std::mutex> guard(mutex);
        const auto it = control_pending_responses.find(target);
        if (it == control_pending_responses.end()) {
            return std::nullopt;
        }
        auto response = it->second;
        control_pending_responses.erase(it);
        control_inflight_targets.erase(target);
        return response;
    }
