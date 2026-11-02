// Included within R2KernelClient::Impl. Runtime registration, shared-memory
// handles, notification/receive loops and shutdown notification.

    void register_subsystem() {
        std::string request = make_json_request(
            "\"cmd\":\"" + std::string(kRegisterSubsystem) + "\","
            "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
            "\"session_id\":\"" + json_escape(options.session_id) + "\","
            "\"pid\":" + std::to_string(current_pid()) + ","
            "\"lease_id\":\"" + json_escape(env_string_value("R2KERNEL_RESOURCE_LEASE_ID").value_or("")) + "\","
            "\"notify_endpoint\":\"" + json_escape(notify_endpoint) + "\""
        );
        std::string last_error;
        for (int attempt = 1; attempt <= startup_retry_count; ++attempt) {
            try {
                const std::string response = exchange_control_json(
                    request,
                    options.control_host,
                    options.control_udp_port,
                    startup_request_timeout_ms
                );
                apply_startup_barrier_config(response);

                const auto result = json_string_field(response, kResult);
                const auto error = json_string_field(response, "error");
                const auto pool_name = json_string_field(response, kPoolName);
                const auto pool_slot_size = json_int_field(response, kPoolSlotSize);
                const auto pool_slot_count = json_int_field(response, kPoolSlotCount);
                if (result && *result == kOk && pool_name && pool_slot_size && pool_slot_count) {
                    apply_clock_status_from_registration(response);
                    shm_name = *pool_name;
                    slot_size = static_cast<std::size_t>(*pool_slot_size);
                    slot_count = static_cast<std::size_t>(*pool_slot_count);
                    if (const auto control_endpoint = json_string_field(response, kLocalControlEndpoint)) {
                        std::lock_guard<std::mutex> route_guard(mutex);
                        if (local_control_endpoint != *control_endpoint) {
                            local_control_endpoint = *control_endpoint;
#if !defined(_WIN32)
                            control_shm_endpoint_generation.fetch_add(1, std::memory_order_release);
#endif
                        }
                    }
#if !defined(_WIN32)
                    configure_manager_peer_auth(response);
                    configure_control_shm(response);
#endif
                    if (const auto endpoint = json_string_field(response, kEventInlineEndpoint)) {
                        event_inline_endpoint = detail::registered_event_inline_endpoint(*endpoint);
                    }
                    if (const auto threshold = json_int_field(response, kEventInlinePayloadThreshold)) {
                        event_inline_payload_threshold = static_cast<std::size_t>(std::max<long long>(*threshold, 0));
                    }
                    return;
                }
                if (error && !error->empty()) {
                    last_error = *error;
                } else {
                    last_error = "control backend returned an unexpected registration payload";
                }
            } catch (const std::exception& ex) {
                last_error = ex.what();
            } catch (...) {
                last_error = "unknown registration error";
            }
            if (attempt < startup_retry_count) {
                std::this_thread::sleep_for(std::chrono::milliseconds(startup_retry_delay_ms));
            }
        }
        if (!last_error.empty()) {
            throw std::runtime_error(
                "Failed to register subsystem `" + options.subsystem_name + "`: " + last_error
            );
        }
        throw std::runtime_error(
            "Failed to register subsystem `" + options.subsystem_name + "` with control backend"
        );
    }

    void open_runtime_handles() {
        event_pool_identity = detail::shm_identity(shm_name);
        if (r2kernel_shm_open(shm_name.c_str(), slot_size, slot_count, &shm_handle) != 0 || shm_handle == nullptr) {
            throw std::runtime_error("Failed to open shared-memory pool");
        }
        if (r2kernel_event_client_create(shm_name.c_str(), slot_size, slot_count, &event_client) != 0 || event_client == nullptr) {
            throw std::runtime_error("Failed to create Rust event client");
        }
    }

    void notify_loop() {
        while (!stop_requested.load()) {
            const int ready = wait_fd_readable(notify_socket, 200);
            if (ready <= 0) {
                continue;
            }
            const int client_fd = ::accept(notify_socket, nullptr, nullptr);
            if (client_fd < 0) {
                continue;
            }
            try { verify_incoming_manager(client_fd); } catch(const std::exception& error) {
                std::cerr << "Managed notify peer rejected: " << error.what() << '\n'; ::close(client_fd); continue;
            }
            std::string message;
            char buf[4096];
            while (true) {
                const auto received = ::recv(client_fd, buf, sizeof(buf), 0);
                if (received < 0 && errno == EINTR) {
                    continue;
                }
                if (received <= 0) {
                    break;
                }
                message.append(buf, static_cast<std::size_t>(received));
            }
            ::close(client_fd);
            if (message.empty()) {
                continue;
            }
            const auto command = json_string_field(message, kCommand);
            if (!command) {
                continue;
            }
            if (*command == kNotifyPublisher) {
                const auto topic = json_string_field(message, kTopic);
                const auto publisher = json_string_field(message, kPublisher);
                const auto slot_index = json_int_field(message, kPublishSlotIndex);
                const auto lock_name = json_string_field(message, kLockName);
                if (topic && publisher && slot_index && lock_name) {
                    const auto queue_depth = json_int_field(message, kQueueDepth);
                    const auto entry_slot_count = json_int_field(message, kEntrySlotCount);
                    add_subscription(
                        *topic,
                        *publisher,
                        static_cast<std::size_t>(*slot_index),
                        static_cast<std::size_t>(std::max<long long>(1, queue_depth.value_or(1))),
                        static_cast<std::size_t>(std::max<long long>(1, entry_slot_count.value_or(1))),
                        *lock_name,
                        json_bool_field(message, "replay_existing").value_or(false)
                    );
                }
                continue;
            }
            if (*command == kNotifySubscriber) {
                const auto topic = json_string_field(message, kTopic);
                const auto subscriber = json_string_field(message, kSubsystem);
                const auto lock_name = json_string_field(message, kLockName);
                if (topic && subscriber && lock_name) {
                    std::lock_guard<std::mutex> guard(mutex);
                    add_publish_lock_unlocked(*topic, *subscriber, *lock_name);
                }
                continue;
            }
            if (*command == kLifecycleTransition) {
                r2kernel::wrapper::LifecycleTransitionHandler handler;
                {
                    std::lock_guard<std::mutex> guard(mutex);
                    handler = lifecycle_transition_handler;
                }
                if (handler) {
                    r2kernel::wrapper::LifecycleTransitionRequest request;
                    request.transition = json_string_field(message, "transition").value_or("");
                    request.request_id = json_string_field(message, "request_id").value_or("");
                    request.reason = json_string_field(message, "reason").value_or("");
                    request.previous_lifecycle = json_string_field(message, "previous_lifecycle").value_or("");
                    handler(request);
                }
            }
        }
    }

    void recv_loop() {
        std::vector<std::uint8_t> topic_buf(detail::kEventReceiveNameCapacity, 0);
        std::vector<std::uint8_t> publisher_buf(detail::kEventReceiveNameCapacity, 0);
        std::vector<std::uint8_t> payload_buf(std::max<std::size_t>(max_subscribe_payload_size, std::max<std::size_t>(slot_size, 1024)), 0);
        detail::EventRetryNameBuffers oversized_names;
        while (!stop_requested.load()) {
            {
                std::lock_guard<std::mutex> guard(mutex);
                const auto needed = std::max<std::size_t>(max_subscribe_payload_size, std::max<std::size_t>(slot_size, 1024));
                if (payload_buf.size() < needed) {
                    payload_buf.resize(needed, 0);
                }
            }
            std::size_t topic_len = 0;
            std::size_t publisher_len = 0;
            std::size_t payload_len = 0;
            std::int64_t timestamp = 0;
            std::int64_t published_at_sync_ns = 0;
            std::int64_t received_at_sync_ns = 0;
            const int rc = r2kernel_event_client_recv_ex(
                event_client,
                options.socket_timeout_ms,
                topic_buf.data(),
                topic_buf.size(),
                &topic_len,
                publisher_buf.data(),
                publisher_buf.size(),
                &publisher_len,
                &timestamp,
                &published_at_sync_ns,
                &received_at_sync_ns,
                payload_buf.data(),
                payload_buf.size(),
                &payload_len
            );
            if (rc != 0) {
                if (R2KERNEL_SDK_UNLIKELY(rc == detail::kCoreBufferTooSmall)) {
                    // Cold, out of this translation unit: grows payload_buf in place
                    // and retries once with Core's reported lengths.
                    detail::receive_oversized_event(event_client, mutex, subscribe_specs, payload_buf,
                        oversized_names, topic_len, publisher_len, payload_len);
                }
                continue;
            }
            const std::string topic(reinterpret_cast<const char*>(topic_buf.data()), topic_len);
            r2kernel::wrapper::EventCallback callback;
            r2kernel::wrapper::EventMetadataCallback metadata_callback;
            {
                std::lock_guard<std::mutex> guard(mutex);
                const auto it = subscribe_specs.find(topic);
                if (it == subscribe_specs.end()) {
                    continue;
                }
                callback = it->second.callback;
                metadata_callback = it->second.metadata_callback;
            }
            if (!callback && !metadata_callback) {
                continue;
            }
            r2kernel::wrapper::EventPayload payload(payload_buf.begin(), payload_buf.begin() + static_cast<std::ptrdiff_t>(payload_len));
            if (metadata_callback) {
                metadata_callback(payload, r2kernel::wrapper::EventTimingMetadata {
                    timestamp,
                    published_at_sync_ns,
                    received_at_sync_ns,
                });
            }
            if (callback) {
                callback(payload, timestamp);
            }
        }
    }

    bool add_subscription(const std::string& topic,
                          const std::string& publisher,
                          std::size_t slot_index,
                          std::size_t queue_depth,
                          std::size_t entry_slot_count,
                          const std::string& lock_name,
                          bool replay_existing = false) {
        std::lock_guard<std::mutex> guard(mutex);
        if (subscribe_specs.find(topic) == subscribe_specs.end()) {
            return false;
        }
        const auto key = std::make_pair(topic, publisher);
        // Backend notifications are the cold refresh boundary. The core checks
        // the actual named semaphore identity and geometry, preserving the
        // receive cursor only when the existing resource is still identical.
        // Only a backend-confirmed subscription that preceded this publisher
        // may consume its bounded initial buffer. Late joins keep latest-only.
        const auto subscribe = replay_existing
            ? r2kernel_event_client_subscribe_ex
            : r2kernel_event_client_subscribe_latest_ex;
        const int rc = subscribe(
            event_client,
            topic.c_str(),
            publisher.c_str(),
            slot_index,
            std::max<std::size_t>(queue_depth, 1),
            std::max<std::size_t>(entry_slot_count, 1),
            lock_name.c_str()
        );
        if (rc == 0) {
            subscriptions.insert_or_assign(key, SubscriptionSlot {
                slot_index,
                std::max<std::size_t>(queue_depth, 1),
                std::max<std::size_t>(entry_slot_count, 1),
                lock_name,
            });
            return true;
        }
        subscriptions.erase(key);
        std::cerr << "Native Event subscription failed topic=" << topic
                  << " publisher=" << publisher << " rc=" << rc << '\n';
        return false;
    }

    void add_publish_lock_unlocked(const std::string& topic,
                                   const std::string& subscriber,
                                   const std::string& lock_name) {
        auto& locks = publish_locks[topic];
        const auto existing = locks.find(subscriber);
        if (existing != locks.end()) {
            if (existing->second != nullptr) {
                r2kernel_lock_close(existing->second);
            }
            locks.erase(existing);
        }

        R2KernelLockHandle* handle = nullptr;
        const int rc = r2kernel_lock_open(lock_name.c_str(), &handle);
        if (rc == 0 && handle != nullptr) {
            locks.emplace(subscriber, handle);
            if (subscriber == "stamped_stream_runtime" || subscriber == "__ws_event_bridge__") {
                const auto slot_it = publish_slots.find(topic);
                if (slot_it != publish_slots.end() && shm_handle != nullptr) {
                    R2KernelShmSlotInfo slot {};
                    if (r2kernel_shm_read_snapshot(shm_handle, slot_it->second.slot_index, nullptr, &slot, nullptr, 0) == 0 &&
                        slot.timestamp > 0) {
                        (void)r2kernel_lock_post(handle);
                    }
                }
            }
            startup_barrier_cv.notify_all();
        }
    }

    void clear_publish_locks() {
        std::lock_guard<std::mutex> guard(mutex);
        for (auto& [topic, locks] : publish_locks) {
            (void)topic;
            for (auto& [subscriber, handle] : locks) {
                (void)subscriber;
                if (handle != nullptr) {
                    r2kernel_lock_close(handle);
                }
            }
        }
        publish_locks.clear();
    }

    void inform_subsystem_down() {
        down_reported.store(true);
        try {
            std::string request = make_json_request(
                "\"cmd\":\"" + std::string(kInformSubsystemDown) + "\","
                "\"subsystem\":\"" + json_escape(options.subsystem_name) + "\","
                "\"session_id\":\"" + json_escape(options.session_id) + "\","
                "\"pid\":" + std::to_string(current_pid()) + ","
                "\"lease_id\":\"" + json_escape(env_string_value("R2KERNEL_RESOURCE_LEASE_ID").value_or("")) + "\","
                "\"notify_endpoint\":\"" + json_escape(notify_endpoint) + "\""
            );
            send_control_json_best_effort(request, options.control_host, options.control_udp_port);
        } catch (...) {
        }
    }
