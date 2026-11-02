// Included within R2KernelClient::Impl, Unix only. Local handler endpoint
// registration, framed socket I/O and inline Event delivery.
#if !defined(_WIN32)
    static std::string make_local_control_endpoint_name(const std::string& subsystem_name) {
        return "/tmp/r2kernel-control-handler-" + subsystem_name + "-" + std::to_string(static_cast<long long>(::getpid())) + ".sock";
    }

    bool prepare_local_control_endpoint() {
        if (!control_local_endpoint.empty()) {
            return true;
        }
        control_local_endpoint = make_local_control_endpoint_name(options.subsystem_name);
        std::remove(control_local_endpoint.c_str());
        return true;
    }

    void cleanup_control_local_endpoint() {
        if (!control_local_endpoint.empty()) {
            std::remove(control_local_endpoint.c_str());
            control_local_endpoint.clear();
        }
    }

    bool register_control_handler_managed() {
        // A requested port of 0 lets Control assign the route port, as the Python SDK does.
        if (control_requested_port < 0) {
            return false;
        }
        if (!prepare_local_control_endpoint()) {
            return false;
        }
        std::string request = make_json_request(
            "\"cmd\":\"" + std::string(kRegisterControlHandler) + "\","
            "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
            "\"session_id\":\"" + json_escape(options.session_id) + "\","
            "\"endpoint\":\"" + json_escape(control_local_endpoint) + "\","
            "\"" + std::string(kLocalControlProtocol) + "\":\"" +
                "binary_shm_v5" + "\","
            "\"bind_host\":\"" + json_escape(control_requested_host.empty() ? std::string("0.0.0.0") : control_requested_host) + "\","
            "\"port\":" + std::to_string(control_requested_port)
        );
        for (int attempt = 1; attempt <= startup_retry_count; ++attempt) {
            try {
                const std::string response = exchange_control_json(
                    request,
                    options.control_host,
                    options.control_udp_port,
                    std::max(options.socket_timeout_ms, 2000)
                );
                const auto result = json_string_field(response, kResult);
                const auto port = json_int_field(response, kControlPortKey);
                if (result && *result == kOk && port) {
                    configure_manager_peer_auth(response);
                    control_listen_port = static_cast<int>(*port);
                    return true;
                }
            } catch (...) {
            }
            if (attempt < startup_retry_count) {
                std::this_thread::sleep_for(std::chrono::milliseconds(startup_retry_delay_ms));
            }
        }
        return false;
    }

    std::string read_all_fd(int fd) {
        std::string data;
        char buffer[8192];
        for (;;) {
            const auto received = ::recv(fd, buffer, sizeof(buffer), 0);
            if (received <= 0) {
                break;
            }
            data.append(buffer, static_cast<std::size_t>(received));
        }
        return data;
    }

    bool write_all_fd(int fd, const std::string& data) {
        const char* ptr = data.data();
        std::size_t remaining = data.size();
        while (remaining > 0) {
            const auto sent = ::send(fd, ptr, remaining, 0);
            if (sent <= 0) {
                return false;
            }
            ptr += sent;
            remaining -= static_cast<std::size_t>(sent);
        }
        return true;
    }

    bool write_all_fd_bytes(int fd, const void* data, std::size_t size) {
        const auto* ptr = static_cast<const char*>(data);
        std::size_t remaining = size;
        while (remaining > 0) {
            const auto sent = ::send(fd, ptr, remaining, 0);
            if (sent <= 0) {
                return false;
            }
            ptr += sent;
            remaining -= static_cast<std::size_t>(sent);
        }
        return true;
    }

    bool read_exact_fd(int fd, void* data, std::size_t size) {
        auto* ptr = static_cast<char*>(data);
        std::size_t remaining = size;
        while (remaining > 0) {
            const auto received = ::recv(fd, ptr, remaining, 0);
            if (received <= 0) {
                return false;
            }
            ptr += received;
            remaining -= static_cast<std::size_t>(received);
        }
        return true;
    }

    std::optional<std::string> read_framed_fd(int fd) {
        std::uint32_t frame_len = 0;
        if (!read_exact_fd(fd, &frame_len, sizeof(frame_len))) {
            return std::nullopt;
        }
        if (frame_len > kControlMessageBufferCap) {
            return std::nullopt;
        }
        std::string payload(frame_len, '\0');
        if (frame_len > 0 && !read_exact_fd(fd, payload.data(), payload.size())) {
            return std::nullopt;
        }
        return payload;
    }

    bool write_framed_fd(int fd, const std::string& payload) {
        const auto frame_len = static_cast<std::uint32_t>(payload.size());
        return write_all_fd_bytes(fd, &frame_len, sizeof(frame_len)) &&
               write_all_fd(fd, payload);
    }

    bool connect_event_inline_locked(const std::string& endpoint, int timeout_ms) {
        if (event_inline_fd >= 0 && event_inline_connected_endpoint == endpoint) {
            return true;
        }
        close_event_inline_locked();
        const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) {
            return false;
        }
        timeval tv {};
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        sockaddr_un addr {};
        if (endpoint.size() >= sizeof(addr.sun_path)) {
            reject_unix_endpoint(fd, "Event inline", endpoint);
        }
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", endpoint.c_str());
        if (::connect(fd, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) != 0) {
            ::close(fd);
            return false;
        }
        try { detail::verify_control_server_connection(fd); } catch (...) { ::close(fd); throw; }
        event_inline_fd = fd;
        event_inline_connected_endpoint = endpoint;
        return true;
    }

    void close_event_inline_locked() {
        if (event_inline_fd >= 0) {
            ::close(event_inline_fd);
            event_inline_fd = -1;
        }
        event_inline_connected_endpoint.clear();
    }

    void close_event_inline() {
        std::lock_guard<std::mutex> guard(event_inline_mutex);
        close_event_inline_locked();
    }

    bool should_publish_event_inline(const std::string& topic, const r2kernel::wrapper::EventPayload& payload) {
        if (event_inline_endpoint.empty() || event_inline_payload_threshold == 0 ||
            payload.size() > event_inline_payload_threshold ||
            event_inline_refused.load(std::memory_order_relaxed)) {
            return false;
        }
        std::lock_guard<std::mutex> guard(mutex);
        const auto locks_it = publish_locks.find(topic);
        if (locks_it == publish_locks.end()) {
            return false;
        }
        return locks_it->second.size() == 1 && locks_it->second.find("stamped_stream_runtime") != locks_it->second.end();
    }

    bool publish_event_inline(
        const std::string& topic,
        const r2kernel::wrapper::EventPayload& payload,
        std::int64_t timestamp_ns,
        std::int64_t published_at_sync_ns,
        int priority
    ) {
        std::string endpoint;
        {
            std::lock_guard<std::mutex> guard(mutex);
            endpoint = event_inline_endpoint;
        }
        if (endpoint.empty()) {
            return false;
        }
        std::string request;
        append_event_inline_header(request, kEventInlineKindPublishReq);
        append_string(request, topic);
        append_string(request, options.subsystem_name);
        append_i64(request, timestamp_ns);
        append_i64(request, published_at_sync_ns);
        append_u16(request, static_cast<std::uint16_t>(std::clamp(priority, 0, 65535)));
        append_bytes(request, payload.empty() ? nullptr : payload.data(), payload.size());

        // The slot is already SEQ_READY: a refused peer verification never
        // leaves publish() by exception. It returns false, so publish() posts
        // the stamped_stream_runtime lock as without inline, and it disables
        // inline delivery for this registration (reported once), so later
        // publishes do not repeat the peer proof.
        std::string refusal;
        {
            std::lock_guard<std::mutex> guard(event_inline_mutex);
            if (event_inline_refused.load(std::memory_order_relaxed)) {
                return false;
            }
            try {
                for (int attempt = 0; attempt < 2; ++attempt) {
                    if (!connect_event_inline_locked(endpoint, std::max(options.socket_timeout_ms, 100))) {
                        close_event_inline_locked();
                        continue;
                    }
                    if (!write_framed_fd(event_inline_fd, request)) {
                        close_event_inline_locked();
                        continue;
                    }
                    const auto response = read_framed_fd(event_inline_fd);
                    if (response.has_value() && parse_event_inline_publish_response(*response)) {
                        return true;
                    }
                    close_event_inline_locked();
                }
                return false;
            } catch (const std::exception& error) {
                close_event_inline_locked();
                refusal = error.what();
            }
        }
        if (!event_inline_refused.exchange(true)) {
            detail::report_event_inline_refusal(endpoint, topic, refusal.c_str());
        }
        return false;
    }

    bool parse_event_inline_publish_response(const std::string& response) const {
        return response.size() >= 7 &&
               std::memcmp(response.data(), kEventInlineMagic, sizeof(kEventInlineMagic)) == 0 &&
               static_cast<std::uint8_t>(response[4]) == kEventInlineVersion &&
               static_cast<std::uint8_t>(response[5]) == kEventInlineKindPublishResp &&
               static_cast<std::uint8_t>(response[6]) == 1;
    }

    static std::string make_event_inline_local_endpoint_name(const std::string& subsystem_name) {
        return "/tmp/r2kernel-event-handler-" + subsystem_name + "-" + std::to_string(static_cast<long long>(::getpid())) + ".sock";
    }

    bool prepare_event_inline_local_endpoint_locked() {
        if (!event_inline_local_endpoint.empty()) {
            return true;
        }
        event_inline_local_endpoint = make_event_inline_local_endpoint_name(options.subsystem_name);
        sockaddr_un addr {};
        if (event_inline_local_endpoint.size() >= sizeof(addr.sun_path)) {
            event_inline_local_endpoint.clear();
            return false;
        }
        std::remove(event_inline_local_endpoint.c_str());
        return true;
    }

    void cleanup_event_inline_local_endpoint_locked() {
        if (!event_inline_local_endpoint.empty()) {
            std::remove(event_inline_local_endpoint.c_str());
            event_inline_local_endpoint.clear();
        }
    }

    std::string event_inline_local_endpoint_for_registration() {
        std::lock_guard<std::mutex> guard(event_inline_mutex);
        return prepare_event_inline_local_endpoint_locked() ? event_inline_local_endpoint : "";
    }

    static std::string event_inline_deliver_response(bool ok) {
        std::string out;
        append_event_inline_header(out, kEventInlineKindDeliverResp);
        append_u8(out, ok ? 1 : 0);
        return out;
    }

    bool handle_event_inline_deliver_request(const std::string& request) {
        if (request.size() < 6 ||
            std::memcmp(request.data(), kEventInlineMagic, sizeof(kEventInlineMagic)) != 0 ||
            static_cast<std::uint8_t>(request[4]) != kEventInlineVersion ||
            static_cast<std::uint8_t>(request[5]) != kEventInlineKindDeliverReq) {
            return false;
        }
        std::size_t pos = 6;
        const auto topic = read_string_binary(request, pos);
        const auto timestamp_ns = read_i64_binary(request, pos);
        const auto published_at_sync_ns = read_i64_binary(request, pos);
        const auto payload = read_bytes_binary(request, pos);
        if (!topic || !timestamp_ns || !published_at_sync_ns || !payload || pos != request.size()) {
            return false;
        }

        r2kernel::wrapper::EventCallback callback;
        r2kernel::wrapper::EventMetadataCallback metadata_callback;
        {
            std::lock_guard<std::mutex> guard(mutex);
            const auto it = subscribe_specs.find(*topic);
            if (it == subscribe_specs.end()) {
                return false;
            }
            callback = it->second.callback;
            metadata_callback = it->second.metadata_callback;
        }
        if (!callback && !metadata_callback) {
            return false;
        }
        if (metadata_callback) {
            metadata_callback(*payload, r2kernel::wrapper::EventTimingMetadata {
                *timestamp_ns,
                *published_at_sync_ns,
                now_ns(),
            });
        }
        if (callback) {
            callback(*payload, *timestamp_ns);
        }
        return true;
    }

    void event_inline_local_loop(int listen_fd) {
        std::string endpoint;
        {
            std::lock_guard<std::mutex> guard(event_inline_mutex);
            endpoint = event_inline_local_endpoint;
        }

        while (!stop_requested.load()) {
            const int rc = wait_fd_readable(listen_fd, 200);
            if (rc <= 0) {
                continue;
            }
            const int client_fd = ::accept(listen_fd, nullptr, nullptr);
            if (client_fd < 0) {
                continue;
            }

            try { verify_incoming_manager(client_fd); } catch(const std::exception& error) {
                std::cerr << "Managed Event peer rejected: " << error.what() << '\n'; ::close(client_fd); continue;
            }
            timeval client_tv {};
            client_tv.tv_sec = 0;
            client_tv.tv_usec = 200 * 1000;
            (void)setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &client_tv, sizeof(client_tv));
            (void)setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO, &client_tv, sizeof(client_tv));

            while (!stop_requested.load()) {
                const int ready = wait_fd_readable(client_fd, 200);
                if (ready == 0) {
                    continue;
                }
                if (ready < 0) {
                    break;
                }
                const auto request = read_framed_fd(client_fd);
                if (!request.has_value()) {
                    break;
                }
                const bool ok = handle_event_inline_deliver_request(*request);
                if (!write_framed_fd(client_fd, event_inline_deliver_response(ok))) {
                    break;
                }
            }
            ::close(client_fd);
        }
        ::close(listen_fd);
        std::lock_guard<std::mutex> guard(event_inline_mutex);
        cleanup_event_inline_local_endpoint_locked();
    }
#endif
