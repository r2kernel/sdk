// Included once by subsystem.cpp (same translation unit). Event transport,
// lifecycle transitions, heartbeat and handler re-registration.

void Subsystem::start_event_transport() {
    if (ensure_core_client("start_event_transport")) {
        core_client_->start_event_transport();
    }
}

void Subsystem::stop_event_transport() {
    if (ensure_core_client("stop_event_transport")) {
        core_client_->stop_event_transport();
    }
}

void Subsystem::initialize() {
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    if (EventRegistrationActivity::active_for(lifecycle_state.get()))
        throw std::runtime_error("Cannot initialize during Event registration");
    std::unique_lock<std::recursive_mutex> lifecycle(lifecycle_state->transition, std::defer_lock);
    std::unique_lock<std::recursive_mutex> registration(event_registration_mutex_, std::defer_lock);
    std::lock(lifecycle, registration);
    std::optional<EventRegistrationActivity> registration_activity;
    registration_activity.emplace(lifecycle_state);
    if (event_teardown_in_progress_.load()) throw std::runtime_error("Cannot initialize during Event transport teardown");
    LifecycleTransitionActivity initialization_activity(*lifecycle_state);
    std::unique_lock<std::shared_mutex> codec_initialization(codec_lifecycle_mutex_, std::try_to_lock);
    if (!codec_initialization.owns_lock() || active_codec_callbacks_.load() != 0)
        throw std::runtime_error("Cannot reinitialize codecs during an in-flight Control transaction or Event callback");
    if (state_ == DeviceState::operating || control_dispatch_running_) {
        throw std::runtime_error("Stop active Control dispatch before reinitializing schema snapshots");
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        clear_subsystem_initialization_success(this);
    }
    managed_codec_initialization_attempted_.store(true);
    std::atomic_store(&prepared_state_, std::shared_ptr<const PreparedState>{});
    start_heartbeat();
    if (core_client_) {
        core_client_->start_event_transport();
    }
    set_state(DeviceState::registering);
    run_phase(
        "initialize",
        [this, &registration, &registration_activity, &lifecycle_state]() {
            auto prepared = std::make_shared<PreparedState>();
            prepared->context = schema_roots_ ? std::make_shared<SchemaContext>(*schema_roots_)
                                              : std::make_shared<SchemaContext>();
            prepared->inventory = std::make_shared<contract_loader::Inventory>(prepared->context);
            contract_loader::InventoryScope inventory_scope(prepared->inventory);
            // Worker registrations may stage while the user hook is running.
            // Reacquire before validating and keep it through the state commit,
            // so no successful staging operation can miss this initialization.
            registration_activity.reset();
            registration.unlock();
            bool initialized;
            try {
                initialized = on_initialize();
            } catch (...) {
                registration.lock();
                registration_activity.emplace(lifecycle_state);
                throw;
            }
            registration.lock();
            registration_activity.emplace(lifecycle_state);
            if (!initialized) return false;
            if (!contract_policy_off()) {
                const auto report = validate_declarations();
                if (!report.ok()) {
                    const auto message = report.format_errors();
                    if (contract_policy_strict()) {
                        logger_.error(kLifecycleTag, message);
                        return false;
                    }
                    logger_.warning(kLifecycleTag, message + " [contracts.policy=warn]");
                }
            }
            // Type metadata of every registered endpoint, checked before the
            // codecs those types name are prepared.
            const auto runtime_types = runtime_contract_type_report(*prepared->context, contract_policy_);
            if (!runtime_types.ok()) {
                logger_.error(kLifecycleTag, runtime_types.format_errors());
                return false;
            }
            if (!runtime_types.issues.empty()) {
                std::string messages;
                for (const auto& issue : runtime_types.issues) messages += (messages.empty() ? "" : "; ") + issue.message;
                logger_.warning(kLifecycleTag, "Runtime contract type validation warnings: " + messages);
            } else if (contract_policy_strict()) {
                logger_.info(kLifecycleTag, "Runtime contract type validation passed [policy=strict]");
            }
            prepare_schema_codecs(prepared);
            if (core_client_) core_client_->clear_control_codec_routes();
            std::atomic_store(&prepared_state_, std::shared_ptr<const PreparedState>(std::move(prepared)));
            // Publish complete identity before resource allocation/subscription.
            // The local status exchange waits for the backend response.
            try {
                send_heartbeat_once(core_client_ ? StatusUpdate::codec_acknowledgement : StatusUpdate::heartbeat);
                for (const auto& spec : staged_event_publish_)
                    if (!core_client_->register_event_publish(spec))
                        throw std::runtime_error("Prepared Event publisher registration rejected: " + spec.topic);
                for (const auto& spec : staged_event_subscribe_)
                    if (!core_client_->register_event_subscribe(spec))
                        throw std::runtime_error("Prepared Event subscriber registration rejected: " + spec.topic);
                staged_event_publish_.clear();
                staged_event_subscribe_.clear();
            } catch (...) {
                std::atomic_store(&prepared_state_, std::shared_ptr<const PreparedState>{});
                throw;
            }
            return true;
        },
        DeviceState::ready,
        DeviceState::error
    );
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (state_ == DeviceState::ready) {
            mark_subsystem_initialization_success(this);
        }
    }
}

bool Subsystem::request_lifecycle_transition(const std::string& transition,
                                             const std::string& reason) {
    const std::string requested = normalize_lifecycle_transition(transition);
    if (requested == "start") {
        return start_impl();
    }
    if (requested == "reset") {
        return run_lifecycle_transition(
            "reset",
            [this]() { return on_reset(); },
            DeviceState::resetting,
            DeviceState::ready,
            DeviceState::faulted,
            {DeviceState::faulted, DeviceState::error, DeviceState::degraded, DeviceState::paused, DeviceState::stopped}
        );
    }
    if (requested == "pause") {
        return run_lifecycle_transition(
            "pause",
            [this]() { return on_pause(); },
            DeviceState::pausing,
            DeviceState::paused,
            DeviceState::faulted,
            {DeviceState::ready, DeviceState::operating, DeviceState::degraded},
            true
        );
    }
    if (requested == "resume") {
        return run_lifecycle_transition(
            "resume",
            [this]() { return on_resume(); },
            DeviceState::resuming,
            DeviceState::operating,
            DeviceState::faulted,
            {DeviceState::paused},
            true
        );
    }
    if (requested == "stop") {
        return run_lifecycle_transition(
            "stop",
            [this]() { return on_stop(); },
            DeviceState::stopping,
            DeviceState::stopped,
            DeviceState::faulted,
            {
                DeviceState::booting,
                DeviceState::registering,
                DeviceState::waiting_binding,
                DeviceState::waiting_target,
                DeviceState::ready,
                DeviceState::operating,
                DeviceState::paused,
                DeviceState::degraded,
                DeviceState::faulted,
                DeviceState::error,
            },
            true
        );
    }
    if (requested == "fault") {
        return run_lifecycle_transition(
            "fault",
            [this, reason]() { return on_fault(reason); },
            DeviceState::faulted,
            DeviceState::faulted,
            DeviceState::faulted,
            {
                DeviceState::created,
                DeviceState::booting,
                DeviceState::registering,
                DeviceState::disconnected,
                DeviceState::waiting_binding,
                DeviceState::waiting_target,
                DeviceState::ready,
                DeviceState::operating,
                DeviceState::pausing,
                DeviceState::paused,
                DeviceState::resuming,
                DeviceState::resetting,
                DeviceState::degraded,
                DeviceState::faulted,
                DeviceState::error,
            }
        );
    }
    if (requested == "degrade") {
        return run_lifecycle_transition(
            "degrade",
            [this, reason]() { return on_degraded(reason); },
            DeviceState::degraded,
            DeviceState::degraded,
            DeviceState::faulted,
            {DeviceState::ready, DeviceState::operating, DeviceState::paused},
            true
        );
    }
    logger_.warning(kLifecycleTag, "Unsupported lifecycle transition requested: " + transition);
    return false;
}

bool Subsystem::pause() {
    return request_lifecycle_transition("pause");
}

bool Subsystem::resume() {
    return request_lifecycle_transition("resume");
}

bool Subsystem::stop() {
    return request_lifecycle_transition("stop");
}

bool Subsystem::fault(const std::string& reason) {
    return request_lifecycle_transition("fault", reason);
}

bool Subsystem::degrade(const std::string& reason) {
    return request_lifecycle_transition("degrade", reason);
}

void Subsystem::start() {
    (void)start_impl();
}

bool Subsystem::start_impl() {
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    if (EventRegistrationActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Lifecycle transition start rejected during Event registration");
        return false;
    }
    std::lock_guard<std::recursive_mutex> transition(lifecycle_state->transition);
    LifecycleTransitionActivity transition_activity(*lifecycle_state);
    DeviceState current_state;
    bool initialization_succeeded = false;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        current_state = state_;
        initialization_succeeded = subsystem_initialization_succeeded(this);
    }
    if (!initialization_succeeded
        || (current_state != DeviceState::ready && current_state != DeviceState::stopped)) {
        logger_.warning(
            kLifecycleTag,
            std::string("Lifecycle transition start rejected from ") + device_state_name(current_state)
                + " (successful initialize and Ready or Stopped state required)"
        );
        return false;
    }
    bool has_events;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        has_events = !event_publish_info_.empty() || !event_subscribe_info_.empty();
    }
    if (core_client_ && has_events) core_client_->start_event_transport();
    run_phase("start", [this]() { return on_start(); }, DeviceState::operating, DeviceState::error);
    return state() == DeviceState::operating;
}

void Subsystem::terminate() {
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    if (EventRegistrationActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Lifecycle termination rejected during Event registration");
        return;
    }
    if (OperationWorkerActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Lifecycle termination rejected from Operation worker");
        return;
    }
    if (OperationAdmission::active_for(lifecycle_state.get())
        || OperationServerStart::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Lifecycle termination rejected during Operation admission");
        return;
    }
    if (TerminationActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Recursive lifecycle termination rejected");
        return;
    }
    // A lifecycle hook owns transition: it must never follow a flight whose
    // owner is waiting for that same lock.
    if (LifecycleTransitionActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, "Lifecycle termination rejected during lifecycle transition");
        return;
    }
    {
        std::unique_lock<std::mutex> lock(lifecycle_state->flight_mutex);
        if (lifecycle_state->flight_started != lifecycle_state->flight_finished) {
            // Compare with the completed generation observed on arrival, not
            // the current active flight: a newer flight may start first.
            const auto completed = lifecycle_state->flight_finished;
            lifecycle_state->flight_completed.wait(lock, [&] {
                return lifecycle_state->flight_finished != completed;
            });
            return;
        }
        ++lifecycle_state->flight_started;
    }
    TerminationFlightOwner flight_owner(*lifecycle_state);
    // Claim the flight before waiting for another thread's transition. Release
    // transition before waiting for registration or joining callbacks.
    std::unique_lock<std::recursive_mutex> transition(lifecycle_state->transition);
    if (lifecycle_state->transitions != 0) {
        logger_.warning(kLifecycleTag, "Lifecycle termination rejected during lifecycle transition");
        return;
    }
    EventTeardownActivity teardown(event_teardown_in_progress_);
    TerminationActivity termination_activity(lifecycle_state.get());
    OperationDrain operation_drain(*lifecycle_state, operation_server_enabled_);
    transition.unlock();
    // Running Operations end as the Python stop_operation_server ends them:
    // a grace period to finish, then a cancel request for each unfinished one,
    // published as a cancel_requested state event while the Event transport is
    // still up. Their workers are then joined before on_terminate().
    (void)wait_for_operation_quiescence(*lifecycle_state, heartbeat_state_mutex_, operation_records_,
                                        kOperationShutdownGrace);
    std::vector<std::shared_ptr<OperationRecord>> canceled;
    {
        std::lock_guard<std::mutex> workers(lifecycle_state->operation_mutex);
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        canceled = request_operation_cancellation(operation_records_, now_ns());
        // A handler can report a result and then continue running. It still
        // needs cancellation even though its operation is already terminal;
        // the finished Operation itself keeps its state, as in Python.
        for (auto& worker : lifecycle_state->operation_workers) {
            if (!worker.finished.load(std::memory_order_acquire)) {
                auto* operation = static_cast<OperationRecord*>(worker.operation.get());
                operation->worker_stop_requested = true;
            }
        }
    }
    for (const auto& operation : canceled) {
        try {
            publish_operation_state_event(*operation, "cancel_requested", OperationPayload{});
        } catch (const std::exception& exc) {
            logger_.error(kOperationTag, std::string("cancel_requested publication failed: ") + exc.what());
        }
    }
    drain_operation_workers(*lifecycle_state);
    {
        // Wait for an in-flight registration to finish; then release the lock
        // before joining callback threads, which may themselves register Events.
        std::lock_guard<std::recursive_mutex> registration(event_registration_mutex_);
    }
    run_phase("terminate", [this]() { return on_terminate(); }, DeviceState::ready, DeviceState::error);
    if (core_client_) {
        core_client_->stop_event_transport();
    }
    stop_control_dispatch_server();
    stop_heartbeat();
}

void Subsystem::reset() {
    (void)request_lifecycle_transition("reset");
}

void Subsystem::set_state(DeviceState state) {
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (state_ == state) {
            return;
        }
        state_ = state;
    }
    on_state_changed(state_);
    send_lifecycle_status();
}

void Subsystem::run_phase(const char* label,
                          const std::function<bool()>& hook,
                          DeviceState success_state,
                          DeviceState failure_state) {
    try {
        detail::LoggedProblemCapture capture;
        if (hook()) {
            logger_.info(kLifecycleTag, std::string(label) + " succeeded");
            set_state(success_state);
            return;
        }
        logger_.error(kLifecycleTag, capture.false_hook_message(label));
        set_state(failure_state);
    } catch (const std::exception& exc) {
        logger_.error(kLifecycleTag, exc.what());
        set_state(failure_state);
    } catch (...) {
        logger_.error(kLifecycleTag, "unknown exception");
        set_state(failure_state);
    }
}

bool Subsystem::run_lifecycle_transition(const char* label,
                                         const std::function<bool()>& hook,
                                         DeviceState entering_state,
                                         DeviceState success_state,
                                         DeviceState failure_state,
                                         const std::unordered_set<DeviceState>& allowed_states,
                                         bool restore_on_false) {
    // Serialize admission through completion separately from Event
    // registration, while allowing recursive hook requests to be admitted
    // according to the current entering state.
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    if (EventRegistrationActivity::active_for(lifecycle_state.get())) {
        logger_.warning(kLifecycleTag, std::string("Lifecycle transition ") + label + " rejected during Event registration");
        return false;
    }
    std::lock_guard<std::recursive_mutex> transition(lifecycle_state->transition);
    LifecycleTransitionActivity transition_activity(*lifecycle_state);
    const DeviceState previous = state();
    if (allowed_states.find(previous) == allowed_states.end()) {
        logger_.warning(
            kLifecycleTag,
            std::string("Lifecycle transition ") + label + " rejected from " + device_state_name(previous)
        );
        return false;
    }
    set_state(entering_state);
    try {
        detail::LoggedProblemCapture capture;
        if (hook()) {
            logger_.info(kLifecycleTag, std::string("Lifecycle transition ") + label + " succeeded");
            set_state(success_state);
            return true;
        }
        logger_.warning(kLifecycleTag,
            capture.with_last_problem(std::string("Lifecycle transition ") + label + " was not applied"));
        set_state(restore_on_false ? previous : failure_state);
        return false;
    } catch (const std::exception& exc) {
        logger_.error(kLifecycleTag, exc.what());
        set_state(failure_state);
        return false;
    } catch (...) {
        logger_.error(kLifecycleTag, "unknown exception");
        set_state(failure_state);
        return false;
    }
}

bool Subsystem::ensure_core_client(const char* caller) const {
    if (core_client_) {
        return true;
    }
    logger_.error(kEventTag, std::string("no core client bound for ") + caller);
    return false;
}

std::int64_t Subsystem::default_now_ns() {
    return r2kernel_clock_now_ns();
}

void Subsystem::start_heartbeat() {
    if (heartbeat_thread_.joinable()) {
        return;
    }
    heartbeat_stop_.store(false);
    heartbeat_thread_ = std::thread([this]() { heartbeat_loop(); });
}

void Subsystem::stop_heartbeat() {
    heartbeat_stop_.store(true);
    heartbeat_wait_cv_.notify_all();
    if (heartbeat_thread_.joinable()) {
        heartbeat_thread_.join();
    }
}

void Subsystem::heartbeat_loop() {
    while (!heartbeat_stop_.load()) {
        try {
            send_heartbeat_once();
        } catch (const std::exception& exc) {
            logger_.warning(kHeartbeatTag, exc.what());
        }
        std::unique_lock<std::mutex> guard(heartbeat_wait_mutex_);
        heartbeat_wait_cv_.wait_for(guard, std::chrono::seconds(1), [this]() {
            return heartbeat_stop_.load();
        });
    }
}

void Subsystem::send_heartbeat_once(StatusUpdate update) {
    // User-captured destructors may reenter registration/heartbeat. Reclaim
    // before either heartbeat mutex is acquired. Registration/stop may be
    // invoked by a nested callback; leave their retirement for management.
    if (!CodecCallbackActivity::on_callback_thread())
        std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->collect_retired();
    // Hold through transmission so an old snapshot cannot overwrite an ACKed identity.
    std::unique_lock<std::mutex> publication(heartbeat_publication_mutex_, std::defer_lock);
    if (update != StatusUpdate::lifecycle_change) {
        publication.lock();
        static const auto clock_refresh_interval = upkeep_interval_from_env("R2KERNEL_CLOCK_STATUS_REFRESH_SEC");
        const auto now = std::chrono::steady_clock::now();
        if (core_client_ && now >= next_clock_status_refresh_) {
            next_clock_status_refresh_ = now + clock_refresh_interval;
            (void)core_client_->refresh_clock_status();
        }
    } else if (lifecycle_status_deferred(publication.try_lock(), device_state_name(state()))) {
        return;  // never waits behind a heartbeat that may be registering again
    }

    std::vector<EventTopicInfo> publish_info;
    std::vector<EventTopicInfo> subscribe_info;
    std::vector<std::string> procedure_info;
    std::vector<std::string> operation_info;
    std::vector<std::pair<std::string, ControlEndpointOptions>> control_info;
    std::map<std::string, ProcedureOptions> procedure_contracts;
    std::map<std::string, OperationOptions> operation_contracts;
    DeviceState state = DeviceState::booting;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        publish_info = event_publish_info_;
        subscribe_info = event_subscribe_info_;
        for (const auto& [topic, registration] : control_endpoint_handlers_)
            control_info.emplace_back(topic, registration.options);
        const auto raw_dispatch = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire();
        for (const auto& [topic, _] : raw_dispatch->raw_endpoints) {
            ControlEndpointOptions raw; raw.request_type = "bytes"; raw.response_type = "bytes";
            control_info.emplace_back(topic, std::move(raw));
        }
        const auto prepared = std::atomic_load(&prepared_state_);
        if (prepared && control_server_enabled_ && control_handler_) {
            for (const auto& topic : prepared->raw_control_topics) {
                if (control_endpoint_handlers_.count(topic) || raw_dispatch->raw_endpoints.count(topic)) continue;
                ControlEndpointOptions raw;
                raw.request_type = "bytes";
                raw.response_type = "bytes";
                control_info.emplace_back(topic, std::move(raw));
            }
        }
        for (const auto& [name, _handler] : procedure_handlers_) {
            procedure_info.push_back(name);
        }
        for (const auto& [name, _handler] : operation_handlers_) {
            operation_info.push_back(name);
        }
        for (const auto& [name, contract] : procedure_contracts_) procedure_contracts.emplace(name, contract);
        for (const auto& [name, contract] : operation_contracts_) operation_contracts.emplace(name, contract);
        state = state_;
    }
    std::sort(procedure_info.begin(), procedure_info.end());
    std::sort(operation_info.begin(), operation_info.end());

    std::ostringstream publishing;
    publishing << "[";
    for (std::size_t i = 0; i < publish_info.size(); ++i) {
        if (i > 0) {
            publishing << ",";
        }
        publishing << "[\"" << json_escape(publish_info[i].topic)
                   << "\",\"" << json_escape(publish_info[i].type) << "\"]";
    }
    publishing << "]";

    std::ostringstream subscribing;
    subscribing << "[";
    for (std::size_t i = 0; i < subscribe_info.size(); ++i) {
        if (i > 0) {
            subscribing << ",";
        }
        subscribing << "[\"" << json_escape(subscribe_info[i].topic)
                    << "\",\"" << json_escape(subscribe_info[i].type) << "\"]";
    }
    subscribing << "]";

    std::ostringstream procedures;
    procedures << "[";
    for (std::size_t i = 0; i < procedure_info.size(); ++i) {
        if (i > 0) {
            procedures << ",";
        }
        procedures << "\"" << json_escape(procedure_info[i]) << "\"";
    }
    procedures << "]";

    std::ostringstream operations;
    operations << "[";
    for (std::size_t i = 0; i < operation_info.size(); ++i) {
        if (i > 0) {
            operations << ",";
        }
        operations << "\"" << json_escape(operation_info[i]) << "\"";
    }
    operations << "]";

    std::ostringstream procedure_metadata, operation_metadata;
    procedure_metadata << '{'; operation_metadata << '{';
    bool first_procedure = true, first_operation = true;
    for (const auto& [name, contract] : procedure_contracts) {
        if (!first_procedure) procedure_metadata << ',';
        first_procedure = false;
        procedure_metadata << "\"" << json_escape(name) << "\":{\"request_type\":\"" << json_escape(contract.request_type)
            << "\",\"response_type\":\"" << json_escape(contract.response_type)
            << "\",\"timing_support\":\"" << json_escape(contract.timing_support) << "\"}";
    }
    for (const auto& [name, contract] : operation_contracts) {
        if (!first_operation) operation_metadata << ',';
        first_operation = false;
        operation_metadata << "\"" << json_escape(name) << "\":{\"request_type\":\"" << json_escape(contract.request_type)
            << "\",\"feedback_type\":\"" << json_escape(contract.feedback_type)
            << "\",\"result_type\":\"" << json_escape(contract.result_type)
            << "\",\"state_type\":\"" << json_escape(contract.state_type)
            << "\",\"timing_support\":\"" << json_escape(contract.timing_support) << "\"}";
    }
    procedure_metadata << '}'; operation_metadata << '}';

    std::ostringstream controls, control_contracts;
    controls << '['; control_contracts << '{';
    bool first_control = true;
    for (const auto& [topic, options] : control_info) {
        if (!first_control) { controls << ','; control_contracts << ','; }
        first_control = false;
        controls << "\"" << json_escape(topic) << "\"";
        control_contracts << "\"" << json_escape(topic) << "\":{\"request_type\":\"" << json_escape(options.request_type)
            << "\",\"response_type\":\"" << json_escape(options.response_type) << "\",\"timing_support\":\"" << json_escape(options.timing_support) << "\"}";
    }
    controls << ']'; control_contracts << '}';

    const int control_port_value = core_client_ ? core_client_->get_control_port() : 0;

    std::ostringstream payload;
    payload << "{"
            << "\"cmd\":\"update_subsystem_status\","
            << "\"param\":{"
            << "\"subsystem\":\"" << json_escape(name_) << "\",";
    const std::string session_id = core_client_ ? core_client_->session_id() : std::string {};
    if (!session_id.empty()) {
        payload << "\"session_id\":\"" << json_escape(session_id) << "\",";
    }
#if !defined(_WIN32)
    payload << "\"pid\":" << static_cast<long long>(::getpid()) << ",";
#endif
    if (const char* lease_id = std::getenv("R2KERNEL_RESOURCE_LEASE_ID")) {
        if (*lease_id != '\0') {
            payload << "\"lease_id\":\"" << json_escape(lease_id) << "\",";
        }
    }
    // reported_lifecycle repeats the SDK's own state as the Python SDK sends
    // it; Control may display a binding-derived lifecycle instead.
    payload << "\"lifecycle\":\"" << device_state_name(state) << "\","
            << "\"reported_lifecycle\":\"" << device_state_name(state) << "\","
            << "\"publishing\":" << publishing.str() << ","
            << "\"subscribing\":" << subscribing.str() << ","
            << "\"control_port\":";
    if (control_port_value > 0) {
        payload << control_port_value;
    } else {
        payload << "null";
    }
    payload << ","
            << "\"procedures\":" << procedures.str() << ",\"operations\":" << operations.str()
            << ",\"procedure_contracts\":" << procedure_metadata.str()
            << ",\"operation_contracts\":" << operation_metadata.str();
    payload << ",\"controls\":" << controls.str() << ",\"control_contracts\":" << control_contracts.str();
    const auto codec_state = std::atomic_load(&prepared_state_);
    const bool codec_context_available = codec_state && state != DeviceState::error;
    const auto acknowledged = std::atomic_load(&acknowledged_codec_diagnostics_);
    // Retained identities diagnose resources through initialization and cleanup;
    // they never expose executable plans or claim an available codec context.
    const std::string flat_metadata = codec_context_available
        ? codec_state->codec_metadata_json
        : (acknowledged ? acknowledged->codec_metadata_json : "{}");
    const std::string contract_metadata = codec_context_available
        ? codec_state->contract_codecs_json
        : (acknowledged ? acknowledged->contract_codecs_json : "{}");
    payload << ",\"codec_metadata\":" << flat_metadata;
    payload << ",\"contract_codecs\":" << contract_metadata;
    payload << ",\"codec_context_available\":" << (codec_context_available ? "true" : "false");
    if (!manifest_status_json_.empty()) {
        payload << "," << manifest_status_json_;
    }
    payload << ",\"spatial_runtime\":" << spatial_status_payload();
    payload << "}"
            << "}";

    const std::string body = payload.str();
    if (update == StatusUpdate::codec_acknowledgement) {
        core_client_->publish_prepared_status(body);
        if (codec_context_available) {
            auto diagnostics = std::make_shared<CodecDiagnostics>(*acknowledged);
            diagnostics->codec_metadata_json = flat_metadata;
            diagnostics->contract_codecs_json = contract_metadata;
            diagnostics->dispatch->retire_owner(acknowledged);
            std::atomic_store(&acknowledged_codec_diagnostics_, std::shared_ptr<const CodecDiagnostics>(std::move(diagnostics)));
        }
    } else if (!dynamic_cast<r2kernel::core::R2KernelClient*>(core_client_.get())) {
        if (const auto failure = send_control_json_best_effort(body))
            logger_.warning(kHeartbeatTag, "heartbeat status was not delivered: " + *failure);
    } else if (update == StatusUpdate::lifecycle_change) {
        publish_lifecycle_status(body, device_state_name(state));
    } else if (keep_control_registration(body)) {
        return;  // registration lost: see subsystem_control_registration.cpp
    }
    if (update != StatusUpdate::lifecycle_change) maybe_recover_control_registration();
}

void Subsystem::maybe_recover_control_registration() {
    if (!core_client_ || !control_dispatch_running_) {
        return;
    }
    const auto now = std::chrono::steady_clock::now();
    if (now < next_control_registration_check_) {
        return;
    }
    static const auto interval = upkeep_interval_from_env("R2KERNEL_CONTROL_REGISTRATION_CHECK_SEC");
    next_control_registration_check_ = now + interval;

    try {
        const auto route = core_client_->resolve_control_target(name_);
        if (route && resolved_control_route_port_is_valid(core_client_.get(), route->port)) {
            return;
        }

        const bool ok = core_client_->start_control_server([this](ControlRequest request) {
            return dispatch_control_request(std::move(request));
        });
        if (ok) {
            logger_.info(kHeartbeatTag, "Control handler re-registered with control backend");
        } else {
            logger_.debug(kHeartbeatTag, "Control handler re-registration was not applied");
        }
    } catch (const std::exception& exc) {
        logger_.debug(
            kHeartbeatTag,
            std::string("Control handler re-registration skipped: ") + exc.what()
        );
    } catch (...) {
        logger_.debug(kHeartbeatTag, "Control handler re-registration skipped: unknown error");
    }
}
