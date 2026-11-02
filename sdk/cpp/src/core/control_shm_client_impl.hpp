// Included within R2KernelClient::Impl, Unix only: the Control SHM v5 client,
// handler and preparation paths.
void configure_control_shm(const std::string& registration) {
    if (json_string_field(registration, "control_local_payload_policy") != std::optional<std::string>("forwarded_shm_v5") ||
        json_string_field(registration, "control_local_protocol") != std::optional<std::string>("binary_shm_v5"))
        throw std::runtime_error("Local Control requires forwarded_shm_v5 / binary_shm_v5; upgrade runtime and SDK together");
    const auto required_limit = [&registration](const char* name) -> std::size_t {
        const auto value = json_int_field(registration, name);
        if (!value || *value <= 0) throw std::runtime_error("Missing/invalid negotiated Control SHM limit: " + std::string(name));
        return static_cast<std::size_t>(*value);
    };
    ControlShmLimits limits;
    limits.min_region_bytes = required_limit("control_shm_min_region_bytes");
    limits.max_region_bytes = required_limit("control_shared_buffer_slot_size");
    limits.max_channels = required_limit("control_shm_max_channels");
    limits.max_owned_bytes = required_limit("control_shm_max_owned_bytes");
    limits.max_mapped_bytes = required_limit("control_shm_max_mapped_bytes");
    limits.rt_memory_budget_bytes = configured_control_shm_rt_memory_budget();
    limits.validate();
    (void)ControlShmProcessMemoryBudget::instance();
    std::lock_guard<std::mutex> guard(control_shm_channels_mutex);
    const auto& existing = control_shm_metrics.limits;
    if (control_shm_negotiated) {
        if (existing.min_region_bytes != limits.min_region_bytes || existing.max_region_bytes != limits.max_region_bytes ||
            existing.max_channels != limits.max_channels || existing.max_owned_bytes != limits.max_owned_bytes ||
            existing.max_mapped_bytes != limits.max_mapped_bytes || existing.rt_memory_budget_bytes != limits.rt_memory_budget_bytes)
            throw std::runtime_error("Control SHM limits changed; recreate the SDK client before reconnecting");
        return;
    }
    control_shm_metrics.limits = limits;
    control_shm_negotiated = true;
}

bool handshake_control_shm(const std::string& target, const std::string& host, int port,
                           const std::string& endpoint, int timeout_ms) {
    return handshake_control_shm_until(target, host, port, endpoint, timeout_ms,
        std::chrono::steady_clock::now() + std::chrono::milliseconds(std::max(timeout_ms, 1)));
}

bool handshake_control_shm_until(const std::string& target, const std::string& host, int port,
    const std::string& endpoint, int timeout_ms, ControlShmFraming::Deadline deadline) {
    try {
        ControlShmConnectionState handshake_channel(control_shm_metrics);
        auto socket = ControlShmFraming::connect_until(endpoint, deadline);
        std::string request;
        append_control_shm_header(request, kLocalControlKindHandshakeReq);
        append_string(request, target); append_string(request, host);
        append_u16(request, static_cast<std::uint16_t>(port));
        append_u32(request, static_cast<std::uint32_t>(std::max(timeout_ms, 1)));
        ControlShmFraming::send_until(socket.get(), request, -1, deadline, control_shm_metrics);
        auto response = ControlShmFraming::receive_until(socket.get(), deadline, control_shm_metrics);
        if (response.descriptor.get() >= 0 || !is_control_shm_frame(response.data, kLocalControlKindHandshakeResp))
            throw ControlShmProtocolError("Control v5 handshake response is incompatible");
        std::size_t position = 6;
        const auto ok = read_u8(response.data, position);
        const auto error = read_string_binary(response.data, position);
        if (!ok || !error || position != response.data.size() || *ok > 1 || (*ok == 1 && !error->empty()))
            throw ControlShmProtocolError("Malformed Control v5 handshake response");
        if (*ok == 0)
            throw std::runtime_error("Control v5 handshake failed: " + error.value_or("malformed response"));
        set_control_ready(target, true);
        return true;
    } catch (const std::exception& error) {
        control_shm_metrics.record_error(error);
        set_control_ready(target, false);
        std::cerr << "Control SHM handshake failed: " << error.what() << '\n';
        return false;
    }
}

bool prepare_control_shm(const std::string& target, const std::string& topic,
    std::size_t request_capacity, std::size_t response_capacity, int timeout_ms,
    bool result_ready, ControlShmFraming::Deadline deadline) {
    if (timeout_ms <= 0 || request_capacity == 0 || response_capacity == 0 ||
        request_capacity > control_shm_metrics.limits.max_region_bytes - kPayloadOffset ||
        response_capacity > control_shm_metrics.limits.max_region_bytes - kPayloadOffset)
        throw std::invalid_argument("Control preparation requires positive bounded payload capacities and timeout");
    auto channel = select_control_shm_session(target, topic, true);
    if (!channel) throw std::runtime_error("Control preparation requires a bound local route");
    channel->wait_reclaimed(deadline);
    std::unique_lock<std::mutex> operation(channel->operation_mutex, std::try_to_lock);
    if (!operation.owns_lock()) throw std::runtime_error("Concurrent Control preparation on the same lane");
    channel->require_reclaimed();
    if (channel->socket_descriptor() >= 0 || channel->preparation_attempted)
        throw std::runtime_error("Control preparation requires a fresh lane; rebind before preparing again");
    channel->preparation_attempted = true;
    try {
        const auto& session = channel->session;
        if (!channel->session_valid.load(std::memory_order_acquire) ||
            session.endpoint_generation != control_shm_endpoint_generation.load(std::memory_order_acquire) ||
            session.codec_generation != control_shm_codec_generation.load(std::memory_order_acquire))
            throw std::runtime_error("Control preparation route changed");
        channel->codec_identity = prepare_control_codec_identity(target, topic, deadline);
        channel->prepare_memory();
        channel->state.outgoing.reserve_payload(request_capacity);
        if (result_ready) channel->ensure_reclaimer(deadline);
        channel->connect_until(session.endpoint, deadline);
        ControlShmMetadataWriter request(channel->prepared.buffer(0));
        request.header(kLocalControlKindPrepareReq);
        request.text(target); request.text(session.host);
        request.scalar(static_cast<std::uint16_t>(session.port)); request.text(session.managed_topic);
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0) throw std::runtime_error("Control preparation deadline exceeded");
        request.scalar(static_cast<std::uint32_t>(remaining));
        request.scalar(static_cast<std::uint32_t>(request_capacity));
        request.scalar(static_cast<std::uint32_t>(response_capacity));
        request.lease(channel->state.outgoing.token(), channel->state.outgoing.capacity(), 0);
        auto frame = channel->exchange_prepared(session.endpoint, request.view(),
            channel->state.outgoing.descriptor(), deadline);
        decode_control_shm_preparation_response(frame, channel->response_lease);
        channel->state.incoming.bind_prepared(channel->response_lease, std::move(frame.descriptor), response_capacity);
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Control preparation deadline exceeded during response mapping");
        if (!control_shm_accepting_calls.load(std::memory_order_acquire) ||
            session.endpoint_generation != control_shm_endpoint_generation.load(std::memory_order_acquire) ||
            session.codec_generation != control_shm_codec_generation.load(std::memory_order_acquire))
            throw std::runtime_error("Control preparation was cancelled or its route changed before readiness");
        channel->request_capacity = request_capacity;
        channel->response_capacity = response_capacity;
        channel->preparation_timeout_ms = timeout_ms;
        channel->prepared_result_ready = result_ready;
        channel->provider_ready = true;
        set_control_ready(target, true);
        return true;
    } catch (const std::exception& error) {
        control_shm_metrics.record_error(error);
        channel->invalidate();
        invalidate_control_codec_route(target, topic);
        set_control_ready(target, false);
        throw;
    }
}

std::shared_ptr<ControlShmClientChannel> prepare_control_shm_session(
    const std::string& target, const std::string& topic) {
    return select_control_shm_session(target, topic, false);
}

std::shared_ptr<ControlShmClientChannel> select_control_shm_session(
    const std::string& target, const std::string& topic, bool explicit_preparation) {
    if (!control_shm_accepting_calls.load(std::memory_order_acquire))
        throw std::runtime_error("Control SHM client is stopped");
    auto& cache = control_shm_thread_sessions();
    if (auto channel = cache.find(control_shm_client_identity, target, topic,
            control_shm_endpoint_generation.load(std::memory_order_acquire),
            control_shm_codec_generation.load(std::memory_order_acquire))) {
        ++control_shm_metrics.prepared_session_hits;
        return channel;
    }
    ++control_shm_metrics.prepared_session_misses;
    std::shared_ptr<ControlShmClientChannel> channel, retired;
    {
        std::lock_guard<std::mutex> guard(mutex);
        const auto route = control_routes.find(target);
        if (route == control_routes.end() || local_control_endpoint.empty()) return {};
        std::lock_guard<std::mutex> registry(control_shm_channels_mutex);
        if (!control_shm_accepting_calls.load(std::memory_order_acquire))
            throw std::runtime_error("Control SHM client is stopped");
        const auto managed_topic = r2kernel::core::detail::encode_source_topic(options.subsystem_name, topic);
        const auto key = std::make_pair(target, managed_topic);
        auto found = control_shm_channels.find(key);
        const auto endpoint_generation = control_shm_endpoint_generation.load(std::memory_order_acquire);
        const auto codec_generation = control_shm_codec_generation.load(std::memory_order_acquire);
        if (found != control_shm_channels.end() &&
            (!found->second->session_valid.load(std::memory_order_acquire) ||
             found->second->session.endpoint_generation != endpoint_generation ||
             found->second->session.codec_generation != codec_generation)) {
            // Keep prepared state as a tombstone until an explicit idle
            // preparation/rebind. An ordinary exchange must not cold-admit a
            // replacement lane or discard its capacity/completion contract.
            std::unique_lock<std::mutex> operation(found->second->operation_mutex, std::try_to_lock);
            if (!operation.owns_lock() || found->second->has_pending_release())
                throw std::runtime_error("Cannot refresh a Control session while its lane is busy or pending reclamation");
            found->second->require_reclaimed(); // Preserve sticky receiver errors.
            found->second->invalidate();
            if (found->second->preparation_attempted && !explicit_preparation)
                throw std::runtime_error("Prepared Control session was invalidated; explicitly reprepare or rebind the idle lane");
            retired = found->second;
            control_shm_channels.erase(found);
            found = control_shm_channels.end();
        }
        if (found == control_shm_channels.end()) {
            channel = std::make_shared<ControlShmClientChannel>(control_shm_metrics);
            channel->session = {options.subsystem_name, target, topic, managed_topic,
                route->second.first, local_control_endpoint,
                route->second.second, endpoint_generation, codec_generation, {}};
            channel->session.route_ready = control_shm_route_readiness.at(target);
            control_shm_channels.emplace(key, channel);
        } else channel = found->second;
    }
    if (retired) retired->stop();
    cache.remember(control_shm_client_identity, channel);
    return channel;
}

std::optional<r2kernel::wrapper::ControlResponse> transact_control_shm_session(
    const std::shared_ptr<ControlShmClientChannel>& channel,
    const r2kernel::wrapper::ByteBuffer& payload, std::int64_t timestamp_ns,
    float period_ms, float deadline_ms, int priority, int timeout_ms, bool result_ready,
    std::chrono::steady_clock::time_point call_started,
    std::chrono::steady_clock::time_point started) {
    ControlShmDurationScope response_timing(control_shm_metrics.response_ready, control_shm_metrics.timing_enabled, call_started, false);
    const auto budget_ms = deadline_ms > 0.0F && std::isfinite(deadline_ms)
        ? std::min(static_cast<double>(timeout_ms), static_cast<double>(deadline_ms))
        : static_cast<double>(timeout_ms);
    const auto deadline = started + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
        std::chrono::duration<double, std::milli>(budget_ms));
    const auto& session = channel->session;
    const auto& target = session.target;
    const auto& topic = session.managed_topic;
    const auto& codec_topic = session.topic;
    const auto& host = session.host;
    const auto& endpoint = session.endpoint;
    const int port = session.port;
    // Wait before taking operation_mutex; its owner is the release receiver.
    // Sticky background errors deliberately escape this foreground catch.
    channel->wait_reclaimed(deadline);
    std::unique_lock<std::mutex> operation(channel->operation_mutex, std::try_to_lock);
    if (!operation.owns_lock()) throw std::runtime_error("Concurrent Control call on the same channel is not allowed");
    channel->require_reclaimed();
    if (channel->provider_ready && payload.size() > channel->request_capacity)
        throw ControlShmAccountedError("Prepared Control request capacity exceeded; rebind to change capacities");
    if (channel->provider_ready && result_ready && !channel->prepared_result_ready)
        throw std::runtime_error("Prepared Control result-ready completion was not declared; rebind the lane");
    bool request_region_changed = false, published = false;
    try {
        if (channel->invalidated) throw std::runtime_error("Control SHM channel was invalidated");
        if (!control_shm_accepting_calls.load(std::memory_order_acquire) ||
            session.endpoint_generation != control_shm_endpoint_generation.load(std::memory_order_acquire) ||
            session.codec_generation != control_shm_codec_generation.load(std::memory_order_acquire))
            throw std::runtime_error("Control session changed before its transaction acquired the lane");
        if (channel->socket_descriptor() < 0) revalidate_control_codec_route(target, codec_topic, deadline);
        if (!channel->provider_ready) {
            channel->prepare_memory();
            if (result_ready) channel->ensure_reclaimer(deadline);
        }
        const bool changed = request_region_changed = channel->state.outgoing.write(payload);
        channel->connect_until(endpoint, deadline);
        ControlShmMetadataWriter request(channel->prepared.buffer(0));
        request.header(kLocalControlKindTransactReq);
        request.text(target); request.text(host);
        request.scalar(static_cast<std::uint16_t>(port)); request.text(topic);
        request.scalar(timestamp_ns); request.scalar(period_ms); request.scalar(deadline_ms);
        const auto remaining_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (remaining_ns <= 0) throw ControlShmDeadlineError("Control exchange deadline exceeded before publication");
        request.scalar(static_cast<std::uint32_t>(timeout_ms));
        request.scalar(static_cast<std::uint8_t>(default_control_priority(codec_topic, priority)));
        request.ticket(static_cast<std::uint64_t>(remaining_ns));
        request.lease(channel->state.outgoing.token(), channel->state.outgoing.capacity(), payload.size());
        published = true; // From here a frame prefix or descriptor may reach Control.
        auto frame = channel->exchange_prepared(endpoint, request.view(),
            changed ? channel->state.outgoing.descriptor() : -1, deadline);
        std::uint64_t response_ticket = 0;
        auto response = decode_prepared_control_shm_response(std::move(frame), channel->state.incoming,
            response_ticket, channel->response_lease);
        if (control_shm_metrics.timing_enabled) {
            const auto ready = std::chrono::steady_clock::now();
            channel->mark_response_ready(ready);
            response_timing.finish(ready);
        }
        channel->consume_response(response_ticket, channel->response_lease, deadline);
        if (result_ready) {
            channel->reclaim_response(response_ticket, deadline);
        } else {
            channel->receive_release(response_ticket, deadline);
            ++channel->next_response_ticket;
        }
        if (session.route_ready) session.route_ready->store(true, std::memory_order_release);
        return response;
    } catch (const std::exception& error) {
        control_shm_metrics.record_error(error);
        const bool late = dynamic_cast<const ControlShmDeadlineError*>(&error) != nullptr;
        if (late && !published && !request_region_changed && channel->provider_ready &&
            channel->socket_descriptor() >= 0) {
            // Nothing reached Control: the prepared lane stays synchronized and
            // only this exchange fails, without replay.
            std::cerr << "Control SHM exchange failed before publication (lane kept): " << error.what() << '\n';
            throw;
        }
        // A late or peer-closed exchange on a prepared lane is re-prepared after
        // Control closes the failed connection; other failures stay tombstoned.
        // Peer-closed is what the socket primitives reported: end of stream
        // before a frame, or a send, receive or wait that found the lane reset
        // (EPIPE, ECONNRESET; a wait that saw the pending error leaves it to that
        // send or receive). A failure Control reported in a response stays
        // tombstoned even though Control closes the lane after it.
        const bool recover = channel->provider_ready && (late || detail::control_shm_peer_closed(error, channel->socket_descriptor()));
        auto retired = recover ? channel->retire_socket() : ControlShmDescriptor{};
        channel->invalidate();
        invalidate_control_codec_route(target, codec_topic);
        if (!channel->preparation_attempted) {
            std::lock_guard<std::mutex> guard(control_shm_channels_mutex);
            const auto found = control_shm_channels.find(std::make_pair(target, topic));
            if (found != control_shm_channels.end() && found->second == channel) control_shm_channels.erase(found);
        }
        set_control_ready(target, false);
        std::cerr << "Control SHM exchange failed (no replay): " << error.what() << '\n';
        if (recover) {
            operation.unlock(); // Re-preparation retires this tombstone under its own lock.
            recover_prepared_control_shm_lane(*channel, std::move(retired));
        }
        if (result_ready || channel->preparation_attempted) throw;
        return std::nullopt;
    }
}

// Cold: bounded re-preparation of a prepared lane whose exchange failed late.
// The failed exchange is never replayed and its error is still raised. First
// wait until Control has finished that transaction and closed the connection,
// so the fresh lane cannot race the old one for the provider channel; then
// prepare with the lane's original capacities, completion mode and timeout.
// A failure leaves the explicit tombstone of the failed lane in place.
#if defined(__GNUC__) || defined(__clang__)
[[gnu::cold]] [[gnu::noinline]]
#endif
void recover_prepared_control_shm_lane(const ControlShmClientChannel& failed, ControlShmDescriptor retired) {
    const auto deadline = std::chrono::steady_clock::now() +
        std::chrono::milliseconds(failed.preparation_timeout_ms);
    try {
        if (failed.preparation_timeout_ms <= 0)
            throw std::runtime_error("Control lane has no preparation timeout to bound its recovery");
        await_control_shm_peer_close(retired.get(), deadline);
        retired.reset();
        prepare_control_shm(failed.session.target, failed.session.topic, failed.request_capacity,
            failed.response_capacity, failed.preparation_timeout_ms, failed.prepared_result_ready, deadline);
        ++control_shm_metrics.prepared_lane_recoveries;
        std::cerr << "Control SHM prepared lane re-prepared after the failed exchange\n";
    } catch (const std::exception& error) {
        ++control_shm_metrics.prepared_lane_recovery_failures;
        std::cerr << "Control SHM prepared lane re-preparation failed: " << error.what() << '\n';
    }
}

// Cold: discard late frames (their descriptors are closed with the data) until
// Control closes the retired connection, or fail at the recovery deadline.
static void await_control_shm_peer_close(int socket, ControlShmFraming::Deadline deadline) {
    if (socket < 0) return;
    char discarded[256];
    for (;;) {
        const auto count = ::recv(socket, discarded, sizeof(discarded), MSG_DONTWAIT);
        if (count == 0) return;
        if (count > 0) continue;
        if (errno == EINTR) continue;
        if (errno == ECONNRESET) return;
        if (errno != EAGAIN && errno != EWOULDBLOCK)
            throw std::runtime_error("Control v5 failed-lane drain failed");
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0)
            throw ControlShmDeadlineError("Control did not close the failed lane before the recovery deadline");
        pollfd descriptor{socket, POLLIN, 0};
        const int ready = ::poll(&descriptor, 1, static_cast<int>(std::min<std::int64_t>(remaining, 1000)));
        if (ready < 0 && errno != EINTR) throw std::runtime_error("Control v5 failed-lane wait failed");
    }
}

void wait_control_shm_reclaimed(const std::string& target, const std::string& topic, int timeout_ms) {
    if (timeout_ms <= 0) throw std::invalid_argument("Control reclamation timeout must be positive");
    if (!control_shm_accepting_calls.load(std::memory_order_acquire))
        throw std::runtime_error("Control SHM client is stopped");
    auto channel = control_shm_thread_sessions().find(control_shm_client_identity, target, topic,
        control_shm_endpoint_generation.load(std::memory_order_acquire),
        control_shm_codec_generation.load(std::memory_order_acquire));
    if (!channel) {
        std::lock_guard<std::mutex> guard(control_shm_channels_mutex);
        if (!control_shm_accepting_calls) throw std::runtime_error("Control SHM client is stopped");
        const auto found = control_shm_channels.find(std::make_pair(target,
            r2kernel::core::detail::encode_source_topic(options.subsystem_name, topic)));
        if (found != control_shm_channels.end()) channel = found->second;
    }
    if (channel) channel->wait_reclaimed(std::chrono::steady_clock::now() +
        std::chrono::milliseconds(timeout_ms));
}

void control_shm_handler_connection(int descriptor, bool manager_verified) {
    ControlShmDescriptor socket(descriptor);
    try {
        const auto flags = ::fcntl(descriptor, F_GETFL);
        if (flags < 0 || ::fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) != 0 ||
            ::fcntl(descriptor, F_SETFD, FD_CLOEXEC) != 0)
            throw std::runtime_error("Control v5 handler socket setup failed");
#if defined(__APPLE__)
        int enabled = 1;
        if (::setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled)) != 0)
            throw std::runtime_error("Control v5 handler signal policy failed");
#endif
        ControlShmConnectionState channel(control_shm_metrics);
        ControlShmPreparedMemory prepared(control_shm_metrics);
        const auto prepare_handler_memory = [&] {
            // External CPU placement may promote this already-running thread
            // from SCHED_OTHER to FIFO/RR. Locking is sticky after promotion.
            const bool required_lock = control_shm_caller_requires_lock();
            prepared.prepare(required_lock);
            channel.incoming.prepare_memory(required_lock);
            channel.outgoing.prepare_memory(required_lock);
        };
        prepare_handler_memory();
        LocalPayloadLease request_lease;
        request_lease.region_name.resize(128, '\0');
        request_lease.region_name.clear();
        bool provider_ready = false;
        bool manager_authenticated = false;
        std::string prepared_sender, prepared_topic;
        while (!control_stop_requested.load()) {
            char prefix_byte = 0;
            const auto queued = ::recv(descriptor, &prefix_byte, 1, MSG_PEEK | MSG_DONTWAIT);
            if (queued == 0) break;
            if (queued < 0 && errno == EINTR) continue;
            if (queued < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                if (wait_fd_readable(descriptor, 200) < 0) break;
                continue;
            }
            if (queued < 0) throw std::runtime_error("Control handler socket read probe failed");
            if (!provider_ready) prepare_handler_memory();
            std::size_t frame_length = 0;
            const auto frame_started = std::chrono::steady_clock::now();
            const auto supplied_deadline = [&](std::uint64_t remaining_ns) {
                const auto maximum = std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::time_point::max() - frame_started).count();
                if (remaining_ns == 0 || remaining_ns > static_cast<std::uint64_t>(maximum))
                    throw ControlShmProtocolError("Invalid Control handler remaining-time budget");
                return frame_started + std::chrono::nanoseconds(remaining_ns);
            };
            const auto authenticate = [&](ControlShmFraming::Deadline operation_deadline) {
                if (std::chrono::steady_clock::now() >= operation_deadline)
                    throw std::runtime_error("Control handler deadline exceeded before authority verification");
                if (!manager_authenticated) {
                    manager_verified = verify_incoming_manager(descriptor, operation_deadline);
                    manager_authenticated = true;
                }
                if (std::chrono::steady_clock::now() >= operation_deadline)
                    throw std::runtime_error("Control handler authority verification exceeded deadline");
            };
            auto incoming_descriptor = ControlShmFraming::receive_into_until(descriptor, prepared.buffer(1),
                ControlShmPreparedMemory::capacity, frame_length,
                frame_started + std::chrono::milliseconds(1000), control_shm_metrics);
            ControlShmMetadataReader reader({prepared.buffer(1), frame_length});
            if (reader.kind(kLocalControlKindHandlerPrepareReq)) {
                if (provider_ready || channel.incoming.capacity() != 0 || channel.outgoing.capacity() != 0)
                    throw ControlShmProtocolError("Control handler preparation requires a fresh lane");
                prepared_sender = reader.text(); prepared_topic = reader.text();
                const auto request_capacity = reader.scalar<std::uint32_t>();
                const auto response_capacity = reader.scalar<std::uint32_t>();
                const auto operation_deadline = supplied_deadline(reader.ticket());
                if (request_capacity == 0 || response_capacity == 0 ||
                    request_capacity > control_shm_metrics.limits.max_region_bytes - kPayloadOffset ||
                    response_capacity > control_shm_metrics.limits.max_region_bytes - kPayloadOffset)
                    throw ControlShmProtocolError("Invalid Control handler preparation capacities");
                reader.lease(request_lease); reader.end();
                authenticate(operation_deadline); // No borrowed mapping or callback before proof.
                channel.incoming.bind_prepared(request_lease, std::move(incoming_descriptor), request_capacity);
                channel.outgoing.reserve_payload(response_capacity);
                ControlShmMetadataWriter output(prepared.buffer(0));
                output.header(kLocalControlKindHandlerPrepareResp);
                output.lease(channel.outgoing.token(), channel.outgoing.capacity(), 0);
                ControlShmFraming::send_view_until(descriptor, output.view(), channel.outgoing.descriptor(),
                    operation_deadline, control_shm_metrics);
                provider_ready = true;
                continue;
            }
            if (!reader.kind(kLocalControlKindHandlerReq))
                throw ControlShmProtocolError("Expected Control v5 handler request");
            const auto sender = reader.text();
            const auto topic = reader.text();
            if (provider_ready && (sender != prepared_sender || topic != prepared_topic))
                throw ControlShmProtocolError("Prepared Control handler identity changed");
            const auto timestamp = reader.scalar<std::int64_t>();
            const auto period = reader.scalar<float>();
            const auto deadline = reader.scalar<float>();
            const auto operation_deadline = supplied_deadline(reader.ticket());
            reader.lease(request_lease); reader.end();
            authenticate(operation_deadline);
            r2kernel::wrapper::ControlRequest request{std::string(sender), std::string(topic),
                channel.incoming.read(request_lease, std::move(incoming_descriptor)), timestamp, period, deadline};
            if (std::chrono::steady_clock::now() >= operation_deadline)
                throw std::runtime_error("Control handler request mapping exceeded deadline");
            auto handler = control_handler_publication.acquire();
            r2kernel::wrapper::ControlResponse response;
            response.timestamp_ns = now_ns();
            response.status = 1;
            if (handler) {
                try { TransportAuthorityScope authority(request, manager_verified); response = handler(std::move(request)); }
                catch (const std::exception& error) { std::cerr << "Control handler failed: " << error.what() << '\n'; }
                catch (...) { std::cerr << "Control handler failed with a non-standard exception\n"; }
            }
            if (!provider_ready) prepare_handler_memory();
            if (std::chrono::steady_clock::now() >= operation_deadline)
                throw std::runtime_error("Control handler callback exceeded deadline");
            const bool changed = channel.outgoing.write(response.payload);
            ControlShmMetadataWriter output(prepared.buffer(0));
            output.header(kLocalControlKindHandlerResp);
            output.scalar(response.status); output.scalar(response.timestamp_ns);
            output.lease(channel.outgoing.token(), channel.outgoing.capacity(), response.payload.size());
            ControlShmFraming::send_view_until(descriptor, output.view(), changed ? channel.outgoing.descriptor() : -1,
                operation_deadline, control_shm_metrics);
        }
    } catch (const ControlShmPeerClosed&) {
        // Normal idle peer shutdown releases both directions via RAII.
    } catch (const std::exception& error) {
        control_shm_metrics.record_error(error);
        if (!control_stop_requested.load())
            std::cerr << "Control SHM handler connection closed: " << error.what() << '\n';
    }
}
