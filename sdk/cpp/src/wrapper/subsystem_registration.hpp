// Included once by subsystem.cpp (same translation unit). Clock and frame
// transforms, Event registration/publication and Control, Procedure and
// Operation endpoint registration.

std::int64_t Subsystem::now_ns() const {
    if (timestamp_getter_) {
        return timestamp_getter_();
    }
    if (core_client_) {
        return core_client_->now_ns();
    }
    return default_now_ns();
}

void Subsystem::set_frame_transform(FrameTransform transform) {
    if (transform.parent_frame.empty() || transform.child_frame.empty()
        || transform.parent_frame == transform.child_frame) {
        throw std::invalid_argument("frame transform parent and child must be distinct non-empty names");
    }
    double norm_squared = 0.0;
    for (const double value : transform.translation) {
        if (!std::isfinite(value)) {
            throw std::invalid_argument("frame transform translation must contain finite numbers");
        }
    }
    for (const double value : transform.rotation_xyzw) {
        if (!std::isfinite(value)) {
            throw std::invalid_argument("frame transform rotation must contain finite numbers");
        }
        norm_squared += value * value;
    }
    const double norm = std::sqrt(norm_squared);
    if (norm < 1e-9) {
        throw std::invalid_argument("frame transform rotation quaternion must be non-zero");
    }
    for (double& value : transform.rotation_xyzw) {
        value /= norm;
    }
    if (!transform.is_static && transform.stale_after_ms == 0) {
        throw std::invalid_argument("dynamic frame transform stale_after_ms must be positive");
    }
    if (transform.observed_at_sync_ns <= 0) {
        transform.observed_at_sync_ns = now_ns();
    }
    const FrameTransform reported = transform;
    {
        std::lock_guard<std::mutex> lock(spatial_mutex_);
        spatial_transforms_[transform.child_frame] = std::move(transform);
    }
    std::ostringstream payload;
    payload << "{\"cmd\":\"update_frame_transform\","
            << "\"subsystem\":\"" << json_escape(name_) << "\",";
    const std::string session_id = core_client_ ? core_client_->session_id() : std::string {};
    if (!session_id.empty()) {
        payload << "\"session_id\":\"" << json_escape(session_id) << "\",";
    }
    payload << "\"child_frame\":\"" << json_escape(reported.child_frame) << "\","
            << "\"transform\":" << frame_transform_json(reported) << "}";
    if (const auto failure = send_control_json_best_effort(payload.str()))
        logger_.warning(kHeartbeatTag, "frame transform update was not delivered: " + *failure);
}

bool Subsystem::clear_frame_transform(const std::string& child_frame) {
    bool removed = false;
    {
        std::lock_guard<std::mutex> lock(spatial_mutex_);
        removed = spatial_transforms_.erase(child_frame) > 0;
    }
    if (removed) {
        std::ostringstream payload;
        payload << "{\"cmd\":\"update_frame_transform\","
                << "\"subsystem\":\"" << json_escape(name_) << "\",";
        const std::string session_id = core_client_ ? core_client_->session_id() : std::string {};
        if (!session_id.empty()) {
            payload << "\"session_id\":\"" << json_escape(session_id) << "\",";
        }
        payload << "\"child_frame\":\"" << json_escape(child_frame) << "\","
                << "\"remove\":true}";
        if (const auto failure = send_control_json_best_effort(payload.str()))
            logger_.warning(kHeartbeatTag, "frame transform removal was not delivered: " + *failure);
    }
    return removed;
}

std::vector<FrameTransform> Subsystem::frame_transforms() const {
    std::lock_guard<std::mutex> lock(spatial_mutex_);
    std::vector<FrameTransform> result;
    result.reserve(spatial_transforms_.size());
    for (const auto& [_, transform] : spatial_transforms_) {
        result.push_back(transform);
    }
    return result;
}

std::string Subsystem::spatial_status_payload() const {
    return "{\"transforms\":" + frame_transforms_json(frame_transforms()) + "}";
}

void Subsystem::set_timestamp_getter(TimestampGetter getter) {
    timestamp_getter_ = std::move(getter);
}

ClockSnapshot Subsystem::clock_snapshot() const {
    R2KernelClockSnapshot raw {};
    if (r2kernel_clock_snapshot(&raw) != 0) {
        const auto now = default_now_ns();
        return ClockSnapshot {
            now,
            0,
            now,
            0,
            -1,
            -1,
            ClockSource::LocalUnsynced,
            false,
            false
        };
    }
    return ClockSnapshot {
        raw.realtime_ns,
        raw.monotonic_ns,
        raw.logical_ns,
        raw.offset_ns,
        raw.rtt_ns,
        raw.uncertainty_ns,
        static_cast<ClockSource>(raw.source),
        raw.stable != 0,
        raw.accepted_for_one_way != 0
    };
}

bool Subsystem::configure_clock(std::int64_t offset_ns,
                                std::int64_t rtt_ns,
                                std::int64_t uncertainty_ns,
                                ClockSource source,
                                bool stable,
                                bool accepted_for_one_way) {
    return r2kernel_clock_configure(
        offset_ns,
        rtt_ns,
        uncertainty_ns,
        static_cast<std::uint8_t>(source),
        stable ? 1 : 0,
        accepted_for_one_way ? 1 : 0
    ) == 0;
}

void Subsystem::reset_clock() {
    (void)r2kernel_clock_reset();
}

bool Subsystem::register_event_publish(const EventPublishSpec& spec) {
    if (event_teardown_in_progress_.load()) return false;
    std::lock_guard<std::recursive_mutex> registration(event_registration_mutex_);
    if (event_teardown_in_progress_.load()) return false;
    EventRegistrationActivity registration_activity(subsystem_lifecycle_lock(this));
    if (manifest_role_ == "gateway") {
        logger_.error(kLifecycleTag, "gateway subsystems may register only Control endpoints");
        return false;
    }
    if (!ensure_core_client("register_event_publish")) {
        return false;
    }
    if (allowed_event_publish_topics_.find(spec.topic) == allowed_event_publish_topics_.end()
        && !is_operation_transport_topic_for(spec.topic, name_)) {
        if (reject_contract_violation(
                "event publish topic is not declared by subsystem manifest or R2Kernel spec contract: " + spec.topic
                    + ". Put request_id, operation_id, object_id, or task_id in the payload, not in the endpoint name."
            )) {
            return false;
        }
    }
    bool stage_registration = false;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        stage_registration = state_ == DeviceState::registering || state_ == DeviceState::booting;
        if (!stage_registration && (!std::atomic_load(&prepared_state_) || state_ == DeviceState::error)) return false;
        const auto existing = std::find_if(
            event_publish_info_.begin(),
            event_publish_info_.end(),
            [&](const auto& info) { return info.topic == spec.topic; }
        );
        if (existing != event_publish_info_.end()) {
            if (existing->type == spec.type) {
                return true;
            }
            logger_.error(kLifecycleTag, "event publish topic already registered with a different type: " + spec.topic);
            return false;
        }
    }
    if (stage_registration) {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        event_publish_info_.push_back(EventTopicInfo{spec.topic, spec.type});
        staged_event_publish_.push_back(spec);
        return true;
    }
    if (std::atomic_load(&prepared_state_) && spec.type.find('/') != std::string::npos) (void)prepared_codec(spec.type);
    return register_event_identity(EventTopicInfo{spec.topic, spec.type}, true,
        [this, &spec]() { return core_client_->register_event_publish(spec); });
}

bool Subsystem::register_event_subscribe(const EventSubscribeSpec& spec) {
    return register_event_subscribe_impl(spec, {});
}

bool Subsystem::register_event_subscribe_impl(const EventSubscribeSpec& spec, const std::string& operation_target) {
    if (event_teardown_in_progress_.load()) return false;
    std::lock_guard<std::recursive_mutex> registration(event_registration_mutex_);
    if (event_teardown_in_progress_.load()) return false;
    EventRegistrationActivity registration_activity(subsystem_lifecycle_lock(this));
    if (manifest_role_ == "gateway") {
        logger_.error(kLifecycleTag, "gateway subsystems may register only Control endpoints");
        return false;
    }
    if (!ensure_core_client("register_event_subscribe")) {
        return false;
    }
    if (allowed_event_subscribe_topics_.find(spec.topic) == allowed_event_subscribe_topics_.end()
        && !is_operation_transport_topic_for(spec.topic, name_)
        && (operation_target.empty() || !is_operation_transport_topic_for(spec.topic, operation_target))) {
        if (reject_contract_violation(
                "event subscribe topic is not declared by subsystem manifest or R2Kernel spec contract: " + spec.topic
                    + ". Put request_id, operation_id, object_id, or task_id in the payload, not in the endpoint name."
            )) {
            return false;
        }
    }
    bool stage_registration = false;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        stage_registration = state_ == DeviceState::registering || state_ == DeviceState::booting;
        if (!stage_registration && (!std::atomic_load(&prepared_state_) || state_ == DeviceState::error)) return false;
        const auto existing = std::find_if(
            event_subscribe_info_.begin(),
            event_subscribe_info_.end(),
            [&](const auto& info) { return info.topic == spec.topic; }
        );
        if (existing != event_subscribe_info_.end()) {
            if (existing->type == spec.type) {
                return true;
            }
            logger_.error(kLifecycleTag, "event subscribe topic already registered with a different type: " + spec.topic);
            return false;
        }
    }
    if (!operation_target.empty() && is_operation_transport_topic_for(spec.topic, operation_target)) {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        operation_subscribe_topics_.insert(spec.topic);
    }
    EventSubscribeSpec guarded = spec;
    if (spec.callback) guarded.callback = [this, callback = spec.callback](const EventPayload& payload, std::int64_t timestamp) {
        CodecCallbackActivity activity(codec_lifecycle_mutex_, active_codec_callbacks_);
        if (!activity || !std::atomic_load(&prepared_state_)) {
            logger_.error(kEventTag, "Event callback unavailable before codec preparation or during reinitialization");
            return;
        }
        callback(payload, timestamp);
    };
    if (spec.metadata_callback) guarded.metadata_callback = [this, callback = spec.metadata_callback](const EventPayload& payload, const EventTimingMetadata& timing) {
        CodecCallbackActivity activity(codec_lifecycle_mutex_, active_codec_callbacks_);
        if (!activity || !std::atomic_load(&prepared_state_)) {
            logger_.error(kEventTag, "Event metadata callback unavailable before codec preparation or during reinitialization");
            return;
        }
        callback(payload, timing);
    };
    if (stage_registration) {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        event_subscribe_info_.push_back(EventTopicInfo{spec.topic, spec.type});
        staged_event_subscribe_.push_back(std::move(guarded));
        return true;
    }
    if (std::atomic_load(&prepared_state_) && spec.type.find('/') != std::string::npos) (void)prepared_codec(spec.type);
    return register_event_identity(EventTopicInfo{spec.topic, spec.type}, false,
        [this, &guarded]() { return core_client_->register_event_subscribe(guarded); });
}

// The caller holds event_registration_mutex_: duplicate calls cannot observe
// an advertised row until its resource registration has completed or rolled back.
bool Subsystem::register_event_identity(const EventTopicInfo& info, bool publisher,
                                        const std::function<bool()>& acquire_resource) {
    auto& rows = publisher ? event_publish_info_ : event_subscribe_info_;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (!std::atomic_load(&prepared_state_) || state_ == DeviceState::error) {
            logger_.error(kEventTag, "Event registration requires successful codec initialization");
            return false;
        }
        rows.push_back(info);
    }
    std::string failure;
    try {
        send_heartbeat_once(StatusUpdate::codec_acknowledgement);
        if (acquire_resource()) return true;
        failure = "Event resource registration rejected: " + info.topic;
    } catch (const std::exception& error) {
        failure = error.what();
    } catch (...) {
        failure = "Unknown Event registration failure: " + info.topic;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        rows.erase(std::remove_if(rows.begin(), rows.end(), [&](const auto& row) { return row.topic == info.topic; }), rows.end());
        if (!publisher) operation_subscribe_topics_.erase(info.topic);
    }
    // Withdraw tentative identity synchronously; failures remain visible and
    // the periodic heartbeat will carry the already-restored local snapshot.
    try { send_heartbeat_once(StatusUpdate::codec_acknowledgement); }
    catch (const std::exception& error) { failure += "; identity withdrawal failed: " + std::string(error.what()); }
    catch (...) { failure += "; identity withdrawal failed with unknown error"; }
    logger_.error(kEventTag, failure);
    return false;
}

bool Subsystem::publish_event(const std::string& topic,
                              const EventPayload& payload,
                              std::int64_t timestamp_ns) {
    if (const auto checks = published_frame_checks_.find(topic); checks != published_frame_checks_.end()) {
        if (const auto refusal = published_frame_mismatch(topic, payload, checks->second)) {
            logger_.error(kEventTag, *refusal);
            return false;
        }
    }
    if (!ensure_core_client("publish_event")) {
        return false;
    }
    if (timestamp_ns < 0) {
        timestamp_ns = now_ns();
    }
    return core_client_->publish_event(topic, payload, timestamp_ns, -1);
}

int Subsystem::bind_control_impl(int port, const std::string& host) {
    if (reject_native_control_for_role("binding")) {
        throw std::runtime_error("subsystem role '" + manifest_role_ + "' cannot bind a native Control server");
    }
    if (!ensure_core_client("bind_control")) {
        throw std::runtime_error("bind_control requires a core client");
    }
    const int bound = core_client_->bind_control(port, host);
    control_bind_requested_ = true;
    return bound;
}

bool Subsystem::register_raw_control_endpoint(const std::string& topic, const ControlHandler& handler) {
    if (reject_native_control_for_role("raw endpoint registration")) return false;
    const auto name = trim_copy(topic);
    if (name.empty() || !handler) return false;
    if (starts_with(name, "procedure.") || starts_with(name, "operation.")) {
        logger_.error(kLifecycleTag, "raw Control endpoint conflicts with an SDK Protocol topic");
        return false;
    }
    try {
        const auto& provided = subsystem_manifest_.provides;
        bool declared_private = false;
        for (const auto& [_, declaration] : provided) {
            const auto endpoints = declaration.endpoints.find("controls");
            const bool explicitly_declared = endpoints != declaration.endpoints.end()
                && std::find(endpoints->second.begin(), endpoints->second.end(), name) != endpoints->second.end();
            const auto require_raw_labels = [&] {
                const auto request = declaration.labels.find("request_type");
                const auto response = declaration.labels.find("response_type");
                if ((request != declaration.labels.end() || response != declaration.labels.end())
                    && (request == declaration.labels.end() || response == declaration.labels.end()
                        || request->second != "bytes" || response->second != "bytes"))
                    throw std::runtime_error("Raw Control contradicts declared request/response type labels");
            };
            if (explicitly_declared) require_raw_labels();
            if (declaration.visibility == "internal" && explicitly_declared) declared_private = true;
            std::vector<std::string> apis;
            if (starts_with(declaration.contract, "api.")) apis.push_back(declaration.contract);
            else if (starts_with(declaration.contract, "component.")) {
                const auto component = contract_loader::find_component_contract(declaration.contract);
                if (!component) throw std::runtime_error("Raw Control component contract is unavailable");
                apis = component->conformance_apis;
            }
            for (const auto& api_id : apis) {
                const auto api = contract_loader::find_api_contract(api_id);
                if (!api) throw std::runtime_error("Raw Control API contract is unavailable");
                for (const auto& endpoint : api->controls) {
                    if (endpoint.topic != name) continue;
                    require_raw_labels();
                    if (endpoint.request != "bytes" || endpoint.response != "bytes")
                        throw std::runtime_error("Raw Control cannot replace a typed contract endpoint");
                    if (declaration.visibility == "internal") declared_private = true;
                }
            }
        }
        if (!declared_private) throw std::runtime_error("Raw Control is not a declared private raw endpoint");
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            if (control_endpoint_handlers_.count(name) || procedure_handlers_.count(name) || operation_handlers_.count(name))
                throw std::runtime_error("Raw Control endpoint conflicts with an existing endpoint");
            std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->register_raw_endpoint(name, handler);
        }
        send_heartbeat_once();
        return true;
    } catch (const std::exception& error) {
        logger_.error(kLifecycleTag, error.what());
        return false;
    }
}

bool Subsystem::register_control_endpoint_impl(const std::string& topic,
                                       const ControlEndpointHandler& handler,
                                       ControlEndpointOptions options) {
    if (reject_native_control_for_role("endpoint registration")) {
        return false;
    }
    const std::string topic_name = trim_copy(topic);
    if (topic_name.empty()) {
        logger_.error(kLifecycleTag, "Control topic is required");
        return false;
    }
    if (!handler) {
        logger_.error(kLifecycleTag, "Control endpoint handler is required");
        return false;
    }
    if (allowed_control_topics_.find(topic_name) == allowed_control_topics_.end()) {
        if (reject_contract_violation(
                "control endpoint is not declared by subsystem manifest or R2Kernel spec contract: " + topic_name
            )) {
            return false;
        }
    }
    options.request_type = trim_copy(std::move(options.request_type));
    options.response_type = trim_copy(std::move(options.response_type));
    try {
        options.timing_support = normalize_timing_support(std::move(options.timing_support));
    } catch (const std::exception& exc) {
        logger_.error(kLifecycleTag, exc.what());
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        const auto publication = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch;
        if (publication->acquire()->raw_endpoints.count(topic_name)) {
            logger_.error(kLifecycleTag, "typed Control endpoint conflicts with a raw endpoint");
            return false;
        }
        const auto previous = control_endpoint_handlers_.find(topic_name);
        auto registration = ControlEndpointRegistration{handler, std::move(options)};
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->register_endpoint(topic_name, registration);
        if (previous == control_endpoint_handlers_.end())
            control_endpoint_handlers_[topic_name] = std::move(registration);
        else
            previous->second = std::move(registration);
    }
    send_heartbeat_once();
    return true;
}

std::vector<std::string> Subsystem::control_endpoint_names_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::string> names;
    names.reserve(control_endpoint_handlers_.size());
    for (const auto& [name, _registration] : control_endpoint_handlers_) {
        names.push_back(name);
    }
    const auto raw = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire();
    for (const auto& [name, _] : raw->raw_endpoints) names.push_back(name);
    std::sort(names.begin(), names.end());
    return names;
}

std::vector<std::pair<std::string, ControlEndpointOptions>> Subsystem::control_contract_info_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::pair<std::string, ControlEndpointOptions>> contracts;
    contracts.reserve(control_endpoint_handlers_.size());
    for (const auto& [name, registration] : control_endpoint_handlers_) {
        if (!registration.options.request_type.empty()
            || !registration.options.response_type.empty()
            || registration.options.timing_support != "none") {
            contracts.emplace_back(name, registration.options);
        }
    }
    const auto raw = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire();
    for (const auto& [name, _] : raw->raw_endpoints) {
        ControlEndpointOptions options; options.request_type = "bytes"; options.response_type = "bytes";
        contracts.emplace_back(name, std::move(options));
    }
    std::sort(contracts.begin(), contracts.end(), [](const auto& lhs, const auto& rhs) {
        return lhs.first < rhs.first;
    });
    return contracts;
}

bool Subsystem::start_control_server_impl() {
    if (reject_native_control_for_role("server start")) {
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        control_server_enabled_ = true;
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->enable_raw_endpoints(true);
    }
    return ensure_control_dispatch_server("start_control_server");
}

bool Subsystem::start_control_server_impl(const ControlHandler& handler) {
    if (reject_native_control_for_role("server start")) {
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->set_raw_handler(handler);
        control_server_enabled_ = true;
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->enable_raw_endpoints(true);
        control_handler_ = handler;
    }
    return ensure_control_dispatch_server("start_control_server");
}

void Subsystem::stop_control_server_impl() {
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->set_raw_handler(nullptr);
        control_handler_ = nullptr;
        control_server_enabled_ = false;
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->enable_raw_endpoints(false);
    }
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    bool operation_enabled;
    {
        std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
        operation_enabled = operation_server_enabled_;
    }
    if (!procedure_server_enabled_ && !operation_enabled) {
        stop_control_dispatch_server();
    }
    if (!CodecCallbackActivity::on_callback_thread())
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->collect_retired();
}

int Subsystem::control_port_impl() const {
    if (!core_client_) {
        return 0;
    }
    return core_client_->get_control_port();
}

bool Subsystem::register_procedure_impl(const std::string& name, const ProcedureHandler& handler) {
    return register_procedure_impl(name, handler, ProcedureOptions{});
}

bool Subsystem::register_procedure_impl(const std::string& name,
                                 const ProcedureHandler& handler,
                                 ProcedureOptions options) {
    if (manifest_role_ == "gateway") {
        logger_.error(kLifecycleTag, "gateway subsystems may register only Control endpoints");
        return false;
    }
    if (name.empty()) {
        logger_.error(kLifecycleTag, "procedure name is required");
        return false;
    }
    if (allowed_procedure_names_.find(name) == allowed_procedure_names_.end()) {
        if (reject_contract_violation(
                "procedure is not declared by subsystem manifest or R2Kernel spec contract: " + name
            )) {
            return false;
        }
    }
    options.request_type = trim_copy(std::move(options.request_type));
    options.response_type = trim_copy(std::move(options.response_type));
    if (options.native_payload && (options.request_type.empty() || options.response_type.empty())) {
        logger_.error(kLifecycleTag, "procedure native_payload requires request_type and response_type: " + name);
        return false;
    }
    try {
        options.timing_support = normalize_timing_support(std::move(options.timing_support));
    } catch (const std::exception& exc) {
        logger_.error(kLifecycleTag, exc.what());
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire()->raw_endpoints.count(name)) {
            logger_.error(kLifecycleTag, "Procedure endpoint conflicts with a raw Control endpoint"); return false;
        }
        procedure_handlers_[name] = handler;
        if (!options.request_type.empty() || !options.response_type.empty() || options.timing_support != "none") {
            procedure_contracts_[name] = std::move(options);
        } else {
            procedure_contracts_.erase(name);
        }
    }
    send_heartbeat_once();
    return true;
}

bool Subsystem::register_typed_procedure_impl(const std::string& name,
                                       const ProcedureHandler& handler,
                                       const std::string& request_type,
                                       const std::string& response_type,
                                       const std::string& timing_support) {
    if (trim_copy(request_type).empty() || trim_copy(response_type).empty()) {
        logger_.error(kLifecycleTag, "typed procedure requires request_type and response_type");
        return false;
    }
    ProcedureOptions options;
    options.request_type = request_type;
    options.response_type = response_type;
    options.timing_support = timing_support;
    return register_procedure_impl(name, handler, std::move(options));
}

bool Subsystem::register_operation_impl(const std::string& name, const OperationHandler& handler) {
    return register_operation_impl(name, handler, OperationOptions{});
}

bool Subsystem::register_operation_impl(const std::string& name,
                                const OperationHandler& handler,
                                OperationOptions options) {
    if (manifest_role_ == "gateway") {
        logger_.error(kLifecycleTag, "gateway subsystems may register only Control endpoints");
        return false;
    }
    if (name.empty()) {
        logger_.error(kLifecycleTag, "operation name is required");
        return false;
    }
    if (allowed_operation_names_.find(name) == allowed_operation_names_.end()) {
        if (reject_contract_violation(
                "operation is not declared by subsystem manifest or R2Kernel spec contract: " + name
            )) {
            return false;
        }
    }
    options.request_type = trim_copy(std::move(options.request_type));
    options.feedback_type = trim_copy(std::move(options.feedback_type));
    options.result_type = trim_copy(std::move(options.result_type));
    options.state_type = trim_copy(std::move(options.state_type));
    try {
        options.timing_support = normalize_timing_support(std::move(options.timing_support));
    } catch (const std::exception& exc) {
        logger_.error(kLifecycleTag, exc.what());
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire()->raw_endpoints.count(name)) {
            logger_.error(kLifecycleTag, "Operation endpoint conflicts with a raw Control endpoint"); return false;
        }
        operation_handlers_[name] = handler;
        if (!options.request_type.empty()
            || !options.feedback_type.empty()
            || !options.result_type.empty()
            || !options.state_type.empty()
            || options.timing_support != "none") {
            operation_contracts_[name] = std::move(options);
        } else {
            operation_contracts_.erase(name);
        }
    }
    send_heartbeat_once();
    return true;
}

bool Subsystem::start_procedure_server_impl() {
    procedure_server_enabled_ = true;
    const bool ok = ensure_control_dispatch_server("start_procedure_server");
    if (ok) {
        send_heartbeat_once();
    }
    return ok;
}

bool Subsystem::start_operation_server_impl() {
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    OperationServerStart start(lifecycle_state, operation_server_enabled_);
    if (!start) {
        logger_.warning(kOperationTag, "Operation server start rejected during termination");
        return false;
    }
    const bool ok = ensure_control_dispatch_server("start_operation_server");
    if (ok) {
        send_heartbeat_once();
    }
    return ok;
}

void Subsystem::stop_procedure_server_impl() {
    procedure_server_enabled_ = false;
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    bool operation_enabled;
    {
        std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
        operation_enabled = operation_server_enabled_;
    }
    if (!control_server_enabled_ && !operation_enabled) {
        stop_control_dispatch_server();
    }
}

void Subsystem::stop_operation_server_impl() {
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    {
        std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
        operation_server_enabled_ = false;
        lifecycle_state->operation_admission_open = false;
        ++lifecycle_state->operation_generation;
    }
    // As the Python stop_operation_server: running Operations get a grace
    // period to finish; the rest are asked to cancel, each new request
    // published as a cancel_requested state event, and get a shorter one.
    // Their workers are joined by terminate().
    (void)wait_for_operation_quiescence(*lifecycle_state, heartbeat_state_mutex_, operation_records_,
                                        kOperationShutdownGrace);
    std::vector<std::shared_ptr<OperationRecord>> canceled;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        canceled = request_operation_cancellation(operation_records_, now_ns());
    }
    for (const auto& operation : canceled)
        publish_operation_state_event(*operation, "cancel_requested", OperationPayload{});
    (void)wait_for_operation_quiescence(*lifecycle_state, heartbeat_state_mutex_, operation_records_,
                                        kOperationCancelGrace);
    if (!control_server_enabled_ && !procedure_server_enabled_) {
        stop_control_dispatch_server();
    }
}

std::vector<std::string> Subsystem::procedure_names_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::string> names;
    names.reserve(procedure_handlers_.size());
    for (const auto& [name, _handler] : procedure_handlers_) {
        names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    return names;
}

std::vector<std::string> Subsystem::operation_names_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::string> names;
    names.reserve(operation_handlers_.size());
    for (const auto& [name, _handler] : operation_handlers_) {
        names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    return names;
}

std::vector<std::pair<std::string, ProcedureOptions>> Subsystem::procedure_contract_info_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::pair<std::string, ProcedureOptions>> contracts;
    contracts.reserve(procedure_contracts_.size());
    for (const auto& [name, options] : procedure_contracts_) {
        contracts.emplace_back(name, options);
    }
    std::sort(contracts.begin(), contracts.end(), [](const auto& lhs, const auto& rhs) {
        return lhs.first < rhs.first;
    });
    return contracts;
}

std::vector<std::pair<std::string, OperationOptions>> Subsystem::operation_contract_info_impl() const {
    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
    std::vector<std::pair<std::string, OperationOptions>> contracts;
    contracts.reserve(operation_contracts_.size());
    for (const auto& [name, options] : operation_contracts_) {
        contracts.emplace_back(name, options);
    }
    std::sort(contracts.begin(), contracts.end(), [](const auto& lhs, const auto& rhs) {
        return lhs.first < rhs.first;
    });
    return contracts;
}
