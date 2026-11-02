// Included within R2KernelClient::Impl. Clock status, Event startup barrier,
// transport threads and Event registration/publication.

    std::int64_t now_ns() const {
        return r2kernel_clock_now_ns();
    }

    bool refresh_clock_status() {
        const std::string request = make_json_request("\"cmd\":\"" + std::string(kClockStatus) + "\"");
        try {
            const std::string response = exchange_control_json(
                request,
                options.control_host,
                options.control_udp_port,
                std::max(options.socket_timeout_ms, 1000)
            );
            if (json_string_field(response, kResult).value_or("") != kOk) {
                return false;
            }
            apply_clock_status_from_registration(response);
            return true;
        } catch (...) {
            return false;
        }
    }

    void apply_startup_barrier_config(const std::string& response) {
        if (startup_barrier_env_override) {
            return;
        }
        const auto barrier = json_int_field(response, kEventStartupBarrierMs);
        if (!barrier) {
            return;
        }
        startup_barrier_ms = std::max<long long>(0, *barrier);
    }

    bool wait_for_publish_subscribers(const std::string& topic) {
        if (startup_barrier_ms <= 0) {
            return false;
        }
        std::unique_lock<std::mutex> guard(mutex);
        if (startup_barrier_done.find(topic) != startup_barrier_done.end()) {
            return false;
        }
        auto has_publish_locks = [this, &topic]() {
            const auto locks_it = publish_locks.find(topic);
            return locks_it != publish_locks.end() && !locks_it->second.empty();
        };
        if (has_publish_locks()) {
            startup_barrier_done.insert(topic);
            return false;
        }

        const auto wait_started = std::chrono::steady_clock::now();
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(startup_barrier_ms);
        (void)startup_barrier_cv.wait_until(guard, deadline, has_publish_locks);
        startup_barrier_done.insert(topic);
        return std::chrono::steady_clock::now() > wait_started;
    }

    void start_transport() {
        const bool already_running = transport_running.exchange(true);
        if (already_running) {
            return;
        }
#if !defined(_WIN32)
        int event_listener = -1;
        try {
            std::lock_guard<std::mutex> guard(event_inline_mutex);
            cleanup_event_inline_local_endpoint_locked();
            if (!prepare_event_inline_local_endpoint_locked()) {
                throw std::runtime_error("Event listener endpoint is unavailable");
            }
            event_listener = create_unix_listener(event_inline_local_endpoint);
        } catch (...) {
            transport_running.store(false);
            std::lock_guard<std::mutex> guard(event_inline_mutex);
            cleanup_event_inline_local_endpoint_locked();
            throw;
        }
#endif
        stop_requested.store(false);
        try {
            notify_thread = std::thread([this]() { notify_loop(); });
            recv_thread = std::thread([this]() { recv_loop(); });
#if !defined(_WIN32)
            event_inline_local_thread = std::thread([this, event_listener]() { event_inline_local_loop(event_listener); });
#endif
        } catch (...) {
            stop_requested.store(true);
            transport_running.store(false);
            if (notify_thread.joinable()) notify_thread.join();
            if (recv_thread.joinable()) recv_thread.join();
#if !defined(_WIN32)
            ::close(event_listener);
            std::lock_guard<std::mutex> guard(event_inline_mutex);
            cleanup_event_inline_local_endpoint_locked();
#endif
            throw;
        }
    }

    void stop_transport() {
        const bool was_running = transport_running.exchange(false);
        stop_requested.store(true);
        if (notify_thread.joinable()) {
            notify_thread.join();
        }
        if (recv_thread.joinable()) {
            recv_thread.join();
        }
#if !defined(_WIN32)
        if (event_inline_local_thread.joinable()) {
            event_inline_local_thread.join();
        }
#endif
        if (!was_running) {
            return;
        }
    }

    bool register_publish(const r2kernel::wrapper::EventPublishSpec& spec, int attempts) {
        const std::size_t requested_queue_depth = event_queue_depth_for_size(spec.size, spec.queue_depth);
        std::string request = make_json_request(
            "\"cmd\":\"" + std::string(kRequestPublishSlots) + "\","
            "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
            "\"session_id\":\"" + json_escape(options.session_id) + "\","
            "\"topic\":\"" + json_escape(spec.topic) + "\","
            "\"size\":" + std::to_string(spec.size) + ","
            "\"mode\":\"event\","
            "\"priority\":" + std::to_string(spec.priority) + ","
            "\"queue_depth\":" + std::to_string(requested_queue_depth)
        );
        for (int attempt = 1; attempt <= attempts; ++attempt) {
            try {
                const std::string response = exchange_control_json(
                    request,
                    options.control_host,
                    options.control_udp_port,
                    startup_request_timeout_ms
                );
                apply_startup_barrier_config(response);
                const auto result = json_string_field(response, kResult);
                const auto slot_index = json_int_field(response, kPublishSlotIndex);
                const auto queue_depth = json_int_field(response, kQueueDepth);
                const auto entry_slot_count = json_int_field(response, kEntrySlotCount);
                if (result && *result == kOk && slot_index) {
                    std::lock_guard<std::mutex> guard(mutex);
                    publish_specs[spec.topic] = spec;
                    PublishSlot& current = publish_slots[spec.topic];
                    const PublishSlot allocated {
                        static_cast<std::size_t>(*slot_index),
                        spec.size,
                        static_cast<std::size_t>(std::max<long long>(1, queue_depth.value_or(static_cast<long long>(requested_queue_depth)))),
                        static_cast<std::size_t>(std::max<long long>(1, entry_slot_count.value_or(static_cast<long long>(entry_slot_count_for_size(slot_size, spec.size))))),
                        0,
                        spec.priority,
                    };
                    // Control kept this publication's slots (re-registration while it
                    // stayed up): continue the generation its subscribers track, or
                    // they skip the new Events as older than the ones they hold. A
                    // changed allocation starts a new sequence at 0 (a new topic's
                    // default entry has generation 0 either way).
                    const bool kept = current.slot_index == allocated.slot_index
                        && current.queue_depth == allocated.queue_depth
                        && current.entry_slot_count == allocated.entry_slot_count;
                    const std::uint64_t generation = kept ? current.generation : 0;
                    current = allocated;
                    current.generation = generation;
                    for (const auto& info : json_subscriber_locks(response)) {
                        if (info.internal && info.subscriber == "__ws_event_bridge__") {
                            continue;
                        }
                        add_publish_lock_unlocked(spec.topic, info.subscriber, info.lock_name);
                    }
                    return true;
                }
            } catch (...) {
            }
            if (attempt < attempts) {
                std::this_thread::sleep_for(std::chrono::milliseconds(startup_retry_delay_ms));
            }
        }
        return false;
    }

    // restoring: a request repeated on a new registration keeps its spec when refused.
    bool register_subscribe(const r2kernel::wrapper::EventSubscribeSpec& spec, int attempts, bool restoring) {
        {
            std::lock_guard<std::mutex> guard(mutex);
            subscribe_specs[spec.topic] = spec;
            max_subscribe_payload_size = std::max(max_subscribe_payload_size, spec.size);
        }

        const std::string inline_endpoint = event_inline_local_endpoint_for_registration();
        std::string request = make_json_request(
            "\"cmd\":\"" + std::string(kRegisterSubscription) + "\","
            "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
            "\"session_id\":\"" + json_escape(options.session_id) + "\","
            "\"topic\":\"" + json_escape(spec.topic) + "\","
            "\"size\":" + std::to_string(spec.size) + ","
            "\"mode\":\"event\","
            "\"event_inline_endpoint\":\"" + json_escape(inline_endpoint) + "\""
        );

        for (int attempt = 1; attempt <= attempts; ++attempt) {
            try {
                const std::string response = exchange_control_json(
                    request,
                    options.control_host,
                    options.control_udp_port,
                    startup_request_timeout_ms
                );
                apply_startup_barrier_config(response);
                const auto result = json_string_field(response, kResult);
                if (result && *result == kOk) {
                    for (const auto& info : json_publishers(response)) {
                        if (!add_subscription(
                            spec.topic,
                            info.publisher,
                            info.slot_index,
                            info.queue_depth,
                            info.entry_slot_count,
                            info.lock_name
                        )) {
                            throw std::runtime_error("native Event subscription failed for " + spec.topic);
                        }
                    }
                    return true;
                }
            } catch (...) {
            }
            if (attempt < attempts) {
                std::this_thread::sleep_for(std::chrono::milliseconds(startup_retry_delay_ms));
            }
        }
        if (restoring) return false;
        std::lock_guard<std::mutex> guard(mutex);
        subscribe_specs.erase(spec.topic);
        return false;
    }

    bool publish(const std::string& topic,
                 const r2kernel::wrapper::EventPayload& payload,
                 std::int64_t timestamp_ns,
                 int priority) {
        PublishSlot slot;
        {
            std::lock_guard<std::mutex> guard(mutex);
            const auto it = publish_slots.find(topic);
            if (it == publish_slots.end()) {
                return false;
            }
            if (it->second.size > 0 && payload.size() > it->second.size) {
                return false;
            }
            slot = it->second;
        }
        if (priority < 0) priority = slot.priority;

        if (slot.generation == 0) {
            (void)wait_for_publish_subscribers(topic);
        }
        const std::int64_t observed_at_sync_ns = timestamp_ns > 0 ? timestamp_ns : now_ns();
        const std::int64_t published_at_sync_ns = now_ns();

        const std::size_t target_slot = slot.slot_index
            + (slot.generation % std::max<std::size_t>(slot.queue_depth, 1))
            * std::max<std::size_t>(slot.entry_slot_count, 1);
        const int publish_rc = r2kernel_shm_publish(
            shm_handle, target_slot, payload.empty() ? nullptr : payload.data(), payload.size(),
            slot.generation, observed_at_sync_ns, published_at_sync_ns);
        if (publish_rc != 0) {
            return false;
        }

        bool stamped_stream_runtime_inline_delivered = false;
#if !defined(_WIN32)
        if (should_publish_event_inline(topic, payload)) {
            stamped_stream_runtime_inline_delivered = publish_event_inline(
                topic,
                payload,
                observed_at_sync_ns,
                published_at_sync_ns,
                priority
            );
        }
#endif

        {
            std::lock_guard<std::mutex> guard(mutex);
            auto& current = publish_slots[topic];
            current.generation += 1;
            for (auto& [subscriber, handle] : publish_locks[topic]) {
                if (stamped_stream_runtime_inline_delivered && subscriber == "stamped_stream_runtime") {
                    continue;
                }
                if (handle != nullptr) {
                    (void)r2kernel_lock_post(handle);
                }
            }
        }

        return true;
    }

    bool control_is_managed() const {
        return true;
    }
