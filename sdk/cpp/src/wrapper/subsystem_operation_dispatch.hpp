// Included once by subsystem.cpp (same translation unit). Server-side
// Operation start, cancel and query request handling.

namespace {
bool is_json_object(std::string_view text) {
    const auto trimmed = operation_event_wire::detail::trim(text);
    return !trimmed.empty() && trimmed.front() == '{';
}

// The result event payload and its declared type, as the Python publisher
// (_operation_result_event_payload): a result object holding a
// "timing_response" object with a "payload" object publishes that payload,
// typed by the timing response's payload_ref (else result_type).
std::pair<std::string, std::string> operation_result_event_payload(const std::string& result_json,
                                                                   const std::string& result_type) {
    if (result_json.find("\"timing_response\"") == std::string::npos || !is_json_object(result_json))
        return {result_json, result_type};
    const auto timing_response = json_raw_field(result_json, "timing_response");
    if (!timing_response || !is_json_object(*timing_response)) return {result_json, result_type};
    const auto inner = json_raw_field(*timing_response, "payload");
    if (!inner || !is_json_object(*inner)) return {result_json, result_type};
    const auto payload_ref = trim_copy(json_string_field_local(*timing_response, "payload_ref").value_or(""));
    return {*inner, payload_ref.empty() ? result_type : payload_ref};
}

// The JSON an Operation query reports for a result or state payload: a
// payload published as encoded bytes is decoded with its declared type now
// (null, logged, when that fails); otherwise the stored JSON.
std::string queried_payload_json(const Subsystem& subsystem, const std::optional<ByteBuffer>& typed,
                                 const std::string& payload_type, const std::string& json) {
    if (!typed) return json;
    try {
        return subsystem.decode_message_json(payload_type, *typed);
    } catch (const std::exception& exc) {
        subsystem.logger().error(kOperationTag, "Operation query could not decode a " + payload_type + " payload: " + exc.what());
        return "null";
    }
}
}  // namespace

// The SDAE bytes of one of this provider's Operation events (see
// operation_event_wire::encode_payload): a JSON payload whose payload_type
// names a message is encoded with this Subsystem's (prepared) codec, as the
// Python publisher does; an already encoded payload is written as given.
EventPayload Subsystem::operation_event_bytes(const OperationRecord& operation,
                                              const char* kind,
                                              const std::string& payload_type,
                                              const OperationPayload& payload,
                                              std::int64_t ts,
                                              const std::string& state,
                                              bool cancel_requested) const {
    if (payload.is_typed) {
        return operation_event_wire::encode_typed_payload(kind, operation.operation_id, operation.operation, name_, ts,
                                                          payload_type, payload.typed, payload.typed_size, state,
                                                          cancel_requested);
    }
    return operation_event_wire::encode_payload(
        kind, operation.operation_id, operation.operation, name_, ts, payload_type, std::string(payload.json),
        [this](const std::string& message_ref, const std::string& json) { return encode_message_json(message_ref, json); },
        state, cancel_requested);
}

// Publishes a state event as the Python _publish_operation_state: a finished
// Operation publishes only its own terminal state again, and after a cancel
// request only cancel_requested or a terminal state is published. The state
// payload is typed by state_type.
void Subsystem::publish_operation_state_event(OperationRecord& operation,
                                              const std::string& state,
                                              const OperationPayload& payload) {
    if (payload.is_typed && !operation_event_wire::is_message_type(operation.state_type))
        throw std::invalid_argument("an encoded Operation state payload needs a message state_type, not '"
                                    + operation.state_type + "'");
    std::int64_t ts;
    bool cancel_requested;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        if (is_terminal_operation_state(operation.state) && operation.state != state) return;
        if (operation.cancel_requested && !is_terminal_operation_state(state) && state != "cancel_requested") return;
        operation.state = state;
        operation.state_payload_json = payload.record_json();
        operation.state_payload_typed = payload.record_typed();
        operation.updated_ts = now_ns();
        ts = operation.updated_ts;
        cancel_requested = operation.cancel_requested;
    }
    const std::string topic = operation_topic(name_, operation.operation, operation.operation_id, "state");
    publish_event(topic, operation_event_bytes(operation, "state", operation.state_type, payload, ts, state, cancel_requested), ts);
}

ControlResponse Subsystem::handle_operation_start_request(const ControlRequest& request) {
    OperationStartResponse operation_response;
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    OperationAdmission admission(lifecycle_state);
    operation_response.timestamp_ns = now_ns();
    const std::string text(request.payload.begin(), request.payload.end());
    const auto operation_name = json_string_field_local(text, "operation");
    operation_response.request_id = json_string_field_local(text, "request_id").value_or("");
    if (!admission) {
        operation_response.ok = false;
        operation_response.accepted = false;
        operation_response.error_code = "unavailable";
        operation_response.error_message = "operation server is not running";
    } else if (!operation_name.has_value() || operation_name->empty()) {
        operation_response.ok = false;
        operation_response.accepted = false;
        operation_response.error_code = "invalid_argument";
        operation_response.error_message = "missing operation name";
    } else {
        OperationHandler handler;
        OperationOptions contract;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto it = operation_handlers_.find(*operation_name);
            if (it != operation_handlers_.end()) {
                handler = it->second;
            }
            const auto contract_it = operation_contracts_.find(*operation_name);
            if (contract_it != operation_contracts_.end()) {
                contract = contract_it->second;
            } else {
                // No contract (every type empty): no declared type on the events.
                contract.state_type.clear();
            }
        }
        if (!handler) {
            operation_response.ok = false;
            operation_response.accepted = false;
            operation_response.error_code = "not_found";
            operation_response.error_message = "unknown operation";
        } else {
            const bool timed = json_bool_field_local(text, "timed").value_or(false);
            const std::string timed_late_policy =
                json_string_field_local(text, "timed_late_policy").value_or("execute_immediately");
            try {
                enforce_timing_support("operation", *operation_name, contract.timing_support, timed);
                if (timed) {
                    // A received policy outside drop/reject/execute_immediately is refused, not passed on.
                    (void)late_policy_code(timed_late_policy);
                }
            } catch (const std::exception& exc) {
                operation_response.ok = false;
                operation_response.accepted = false;
                operation_response.error_code = "invalid_argument";
                operation_response.error_message = exc.what();
                std::ostringstream payload;
                payload << "{"
                        << "\"request_id\":\"" << json_escape(operation_response.request_id) << "\","
                        << "\"ok\":false,"
                        << "\"accepted\":false,"
                        << "\"operation_id\":null,"
                        << "\"state\":null,"
                        << "\"error_code\":\"invalid_argument\","
                        << "\"error_message\":\"" << json_escape(operation_response.error_message) << "\""
                        << "}";
                ControlResponse response;
                response.timestamp_ns = operation_response.timestamp_ns;
                response.status = 0;
                const auto payload_text = payload.str();
                response.payload.assign(payload_text.begin(), payload_text.end());
                return response;
            }
            auto operation = std::make_shared<OperationRecord>();
            operation->host_authority = current_protocol_host_authority();
            operation->operation_id = std::to_string(now_ns());
            operation->operation = *operation_name;
            operation->params_json = json_raw_field(text, "params").value_or("null");
            operation->request_type = json_string_field_local(text, "request_type").value_or(contract.request_type);
            operation->feedback_type = contract.feedback_type;
            operation->result_type = contract.result_type;
            operation->state_type = contract.state_type;
            operation->timed = timed;
            operation->accepted_at_sync_ns = json_int_field_local(text, "accepted_at_sync_ns").value_or(0);
            if (operation->timed) {
                operation->timing.request_id_text = json_string_field_local(text, "timed_request_id").value_or(
                    json_string_field_local(text, "request_id").value_or("")
                );
                operation->timing.request_id = request_id_u64_from_text(operation->timing.request_id_text);
                if (operation->timing.request_id_text.empty()) {
                    operation->timing.request_id_text = request_id_text_from_u64(operation->timing.request_id);
                }
                operation->timing.sent_at_sync_ns = json_int_field_local(text, "timed_sent_at_sync_ns").value_or(0);
                operation->timing.execute_at_sync_ns = json_int_field_local(text, "timed_execute_at_sync_ns").value_or(0);
                if (const auto max_lateness = json_int_field_local(text, "timed_max_lateness_ns")) {
                    operation->timing.max_lateness_ns = *max_lateness;
                }
                operation->timing.priority = static_cast<int>(json_int_field_local(text, "timed_priority").value_or(128));
                operation->timing.late_policy = timed_late_policy;
            }
            operation->created_ts = now_ns();
            operation->updated_ts = operation->created_ts;
            {
                std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                operation_records_[operation->operation_id] = operation;
            }

            auto ensure_topics = [this, operation]() -> bool {
                const std::string feedback_topic = operation_topic(name_, operation->operation, operation->operation_id, "feedback");
                const std::string result_topic = operation_topic(name_, operation->operation, operation->operation_id, "result");
                const std::string state_topic = operation_topic(name_, operation->operation, operation->operation_id, "state");
                if (!register_event_publish(EventPublishSpec{feedback_topic, "bytes", 64U * 1024U, 5})) {
                    return false;
                }
                if (!register_event_publish(EventPublishSpec{result_topic, "bytes", 64U * 1024U, 5})) {
                    return false;
                }
                if (!register_event_publish(EventPublishSpec{state_topic, "bytes", 64U * 1024U, 5})) {
                    return false;
                }
                start_event_transport();
                std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                operation->topics_registered = true;
                return true;
            };
            auto publish_state = [this, operation](const std::string& state, const OperationPayload& payload) {
                publish_operation_state_event(*operation, state, payload);
            };
            auto publish_feedback = [this, operation](const OperationPayload& payload) {
                if (payload.is_typed && !operation_event_wire::is_message_type(operation->feedback_type))
                    throw std::invalid_argument("an encoded Operation feedback payload needs a message feedback_type, not '"
                                                + operation->feedback_type + "'");
                std::int64_t ts;
                {
                    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                    ++operation->feedback_count;
                    operation->updated_ts = now_ns();
                    ts = operation->updated_ts;
                }
                const std::string topic = operation_topic(name_, operation->operation, operation->operation_id, "feedback");
                publish_event(topic, operation_event_bytes(*operation, "feedback", operation->feedback_type, payload, ts), ts);
            };
            auto publish_result = [this, operation](const OperationPayload& payload) {
                const auto ts = now_ns();
                const std::string topic = operation_topic(name_, operation->operation, operation->operation_id, "result");
                if (payload.is_typed) {
                    publish_event(topic, operation_event_bytes(*operation, "result", operation->result_type, payload, ts), ts);
                    return;
                }
                const auto [event_payload, event_payload_type] =
                    operation_result_event_payload(std::string(payload.json), operation->result_type);
                publish_event(topic, operation_event_bytes(*operation, "result", event_payload_type,
                                                           OperationPayload::from_json(event_payload), ts), ts);
            };
            // As the Python complete_operation: the terminal state event has no
            // payload (state_type may differ from result_type); a result, when
            // there is one, follows in its own result event. Only the first
            // completion counts, and a terminal state the handler published
            // itself already ended the Operation: nothing is stored or published.
            auto complete = [this, operation, publish_state, publish_result, lifecycle_state](
                                const std::string& state, std::string error_code, std::string error_message,
                                const OperationPayload& result) {
                if (result.is_typed && !operation_event_wire::is_message_type(operation->result_type))
                    throw std::invalid_argument("an encoded Operation result needs a message result_type, not '"
                                                + operation->result_type + "'");
                bool already_terminal = false;
                {
                    std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                    if (operation->completed) return;
                    operation->completed = true;
                    already_terminal = is_terminal_operation_state(operation->state);
                    if (!already_terminal) {
                        if (state == "canceled") operation->cancel_requested = true;
                        if (state == "failed") {
                            operation->error_code = std::move(error_code);
                            operation->error_message = std::move(error_message);
                        }
                        operation->result_json = result.record_json();
                        operation->result_typed = result.record_typed();
                    }
                }
                lifecycle_state->operation_completed.notify_all();
                if (already_terminal) return;
                publish_state(state, OperationPayload{});
                if (!result.absent()) publish_result(result);
            };

            struct OperationContextImpl final : public OperationContext {
                std::string operation_id_value;
                std::function<bool()> is_cancel_requested_fn;
                std::function<void(const OperationPayload&)> publish_feedback_fn;
                std::function<void(const std::string&, const OperationPayload&)> publish_state_fn;
                std::function<void(const OperationPayload&)> publish_result_fn;
                std::function<void(const std::string&, std::string, std::string, const OperationPayload&)> complete_fn;

                const std::string& operation_id() const override { return operation_id_value; }
                bool is_cancel_requested() const override { return is_cancel_requested_fn(); }
                void publish_feedback(std::string payload_json) override {
                    publish_feedback_fn(OperationPayload::from_json(payload_json));
                }
                void publish_state(std::string state, std::string payload_json = "null") override {
                    publish_state_fn(state, OperationPayload::from_json(payload_json));
                }
                void publish_result(std::string payload_json) override {
                    publish_result_fn(OperationPayload::from_json(payload_json));
                }
                void succeed(std::string result_json = "null") override {
                    complete_fn("succeeded", {}, {}, OperationPayload::from_json(result_json));
                }
                void fail(std::string error_code, std::string error_message, std::string payload_json = "null") override {
                    complete_fn("failed", std::move(error_code), std::move(error_message), OperationPayload::from_json(payload_json));
                }
                void canceled(std::string payload_json = "null") override {
                    complete_fn("canceled", {}, {}, OperationPayload::from_json(payload_json));
                }
                void publish_feedback(const CodecByteView& typed_payload) override {
                    publish_feedback_fn(OperationPayload::from_typed(typed_payload));
                }
                void publish_state(std::string state, const CodecByteView& typed_payload) override {
                    publish_state_fn(state, OperationPayload::from_typed(typed_payload));
                }
                void publish_result(const CodecByteView& typed_payload) override {
                    publish_result_fn(OperationPayload::from_typed(typed_payload));
                }
                void succeed(const CodecByteView& typed_result) override {
                    complete_fn("succeeded", {}, {}, OperationPayload::from_typed(typed_result));
                }
                void fail(std::string error_code, std::string error_message, const CodecByteView& typed_payload) override {
                    complete_fn("failed", std::move(error_code), std::move(error_message), OperationPayload::from_typed(typed_payload));
                }
                void canceled(const CodecByteView& typed_payload) override {
                    complete_fn("canceled", {}, {}, OperationPayload::from_typed(typed_payload));
                }
            };

            if (!ensure_topics()) {
                operation_response.ok = false;
                operation_response.accepted = false;
                operation_response.error_code = "resource_exhausted";
                operation_response.error_message = "failed to allocate operation topics";
            } else {
                // As the Python provider: queued and running are record states
                // (start reply, query) and are not published as state events.
                std::list<OperationWorker> retired;
                OperationWorker* worker = nullptr;
                bool launched = false;
                std::list<OperationWorker>::iterator worker_position;
                {
                    std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
                    if (admission.can_launch()) {
                        lifecycle_state->operation_workers.emplace_back();
                        worker_position = std::prev(lifecycle_state->operation_workers.end());
                        worker = &*worker_position;
                        worker->operation = operation;
                    }
                }
                if (worker) {
                    try {
                        std::thread thread(
                    [this, operation, handler, publish_state, publish_feedback, publish_result, complete, lifecycle_state, worker]() mutable {
                        OperationWorkerActivity worker_activity(lifecycle_state.get());
                        struct Finish {
                            OperationWorker* worker;
                            ~Finish() { worker->finished.store(true, std::memory_order_release); }
                        } finish{worker};
                        OperationHandler owned_handler;
                        owned_handler.swap(handler);
                        r2kernel::core::detail::CallbackAuthorityScope authority(operation->host_authority);
                        OperationContextImpl context;
                        context.operation_id_value = operation->operation_id;
                        context.is_cancel_requested_fn = [this, operation]() {
                            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                            return operation->cancel_requested || operation->worker_stop_requested;
                        };
                        context.publish_feedback_fn = publish_feedback;
                        context.publish_state_fn = publish_state;
                        context.publish_result_fn = publish_result;
                        context.complete_fn = complete;

                        // As the Python _run_operation: an Operation canceled
                        // before its handler starts ends canceled without
                        // running it; otherwise it is running (not published).
                        bool canceled_before_start;
                        {
                            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                            canceled_before_start = operation->cancel_requested;
                            if (!canceled_before_start) {
                                operation->state = "running";
                                operation->updated_ts = now_ns();
                            }
                        }
                        if (canceled_before_start) {
                            context.canceled("null");
                            return;
                        }
                        OperationRequest operation_request;
                        operation_request.sender = "";
                        operation_request.target = name_;
                        operation_request.operation = operation->operation;
                        operation_request.operation_id = operation->operation_id;
                        operation_request.request_id = operation->operation_id;
                        operation_request.params_json = operation->params_json;
                        operation_request.timestamp_ns = operation->created_ts;
                        operation_request.request_type = operation->request_type;
                        operation_request.feedback_type = operation->feedback_type;
                        operation_request.result_type = operation->result_type;
                        operation_request.timed = operation->timed;
                        operation_request.timing = operation->timing;
                        operation_request.accepted_at_sync_ns = operation->accepted_at_sync_ns;

                        try {
                            std::optional<std::string> result_json = owned_handler(operation_request, context);
                            bool completed = false;
                            bool cancel_requested = false;
                            {
                                std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                                completed = operation->completed;
                                cancel_requested = operation->cancel_requested;
                            }
                            if (!completed) {
                                if (cancel_requested) {
                                    context.canceled(result_json.value_or("null"));
                                } else {
                                    context.succeed(result_json.value_or("null"));
                                }
                            }
                        } catch (const std::exception& exc) {
                            logger_.error(kOperationTag, exc.what());
                            context.fail("execution_failed", exc.what(), "null");
                        } catch (...) {
                            logger_.error(kOperationTag, "unknown exception");
                            context.fail("execution_failed", "unknown exception", "null");
                        }
                    }
                        );
                        std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
                        worker->thread = std::move(thread);
                        launched = true;
                    } catch (...) {
                        std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
                        lifecycle_state->operation_workers.erase(worker_position);
                        throw;
                    }
                }
                {
                    std::lock_guard<std::mutex> lock(lifecycle_state->operation_mutex);
                    for (auto it = lifecycle_state->operation_workers.begin();
                         it != lifecycle_state->operation_workers.end();) {
                        auto current = it++;
                        if (current->thread.joinable() && current->finished.load(std::memory_order_acquire))
                            retired.splice(retired.end(), lifecycle_state->operation_workers, current);
                    }
                }
                join_operation_workers(retired);
                if (launched) {
                    operation_response.ok = true;
                    operation_response.accepted = true;
                    operation_response.operation_id = operation->operation_id;
                    operation_response.state = "queued";
                } else {
                    {
                        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
                        const auto it = operation_records_.find(operation->operation_id);
                        if (it != operation_records_.end() && it->second == operation)
                            operation_records_.erase(it);
                    }
                    operation_response.ok = false;
                    operation_response.accepted = false;
                    operation_response.error_code = "unavailable";
                    operation_response.error_message = "operation server is not running";
                }
            }
        }
    }

    std::ostringstream payload;
    payload << "{"
            << "\"request_id\":\"" << json_escape(operation_response.request_id) << "\","
            << "\"ok\":" << (operation_response.ok ? "true" : "false") << ","
            << "\"accepted\":" << (operation_response.accepted ? "true" : "false") << ","
            << "\"operation_id\":";
    if (operation_response.operation_id.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.operation_id) << "\"";
    }
    payload << ",\"state\":";
    if (operation_response.state.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.state) << "\"";
    }
    payload << ",\"error_code\":";
    if (operation_response.error_code.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_code) << "\"";
    }
    payload << ",\"error_message\":";
    if (operation_response.error_message.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_message) << "\"";
    }
    payload << "}";

    ControlResponse response;
    response.timestamp_ns = operation_response.timestamp_ns == 0 ? now_ns() : operation_response.timestamp_ns;
    response.status = 0;
    const auto payload_text = payload.str();
    response.payload.assign(payload_text.begin(), payload_text.end());
    return response;
}

ControlResponse Subsystem::handle_operation_cancel_request(const ControlRequest& request) {
    OperationQueryResponse operation_response;
    operation_response.timestamp_ns = now_ns();
    const std::string text(request.payload.begin(), request.payload.end());
    operation_response.request_id = json_string_field_local(text, "request_id").value_or("");
    const auto operation_id = json_string_field_local(text, "operation_id");
    if (!operation_id.has_value() || operation_id->empty()) {
        operation_response.ok = false;
        operation_response.error_code = "invalid_argument";
        operation_response.error_message = "missing operation_id";
    } else {
        std::shared_ptr<OperationRecord> operation;
        std::pair<std::optional<ByteBuffer>, std::optional<ByteBuffer>> typed_payloads;
        bool cancel_published = false;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto it = operation_records_.find(*operation_id);
            if (it != operation_records_.end()) {
                operation = it->second;
                // As the Python SDK: a finished Operation keeps its state and
                // publishes no cancel_requested event. A terminal state the
                // handler published itself finishes it before its handler returns.
                if (operation_accepts_cancel(*operation)) {
                    operation->cancel_requested = true;
                    operation->updated_ts = now_ns();
                    cancel_published = true;
                }
            }
        }
        if (!operation) {
            operation_response.ok = false;
            operation_response.error_code = "not_found";
            operation_response.error_message = "unknown operation";
        } else {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            operation_response.ok = true;
            operation_response.operation_id = operation->operation_id;
            operation_response.operation = operation->operation;
            operation_response.state = operation->state;
            operation_response.cancel_requested = operation->cancel_requested;
            operation_response.feedback_count = operation->feedback_count;
            operation_response.result_json = operation->result_json;
            operation_response.state_payload_json = operation->state_payload_json;
            typed_payloads = {operation->result_typed, operation->state_payload_typed};
            operation_response.created_ts = operation->created_ts;
            operation_response.updated_ts = operation->updated_ts;
            operation_response.error_code = operation->error_code;
            operation_response.error_message = operation->error_message;
        }
        if (cancel_published) publish_operation_state_event(*operation, "cancel_requested", OperationPayload{});
        // Encoded payloads are decoded outside the record lock (the codec
        // checks the lifecycle state under it).
        if (operation) {
            operation_response.result_json = queried_payload_json(*this, typed_payloads.first, operation->result_type,
                                                                  operation_response.result_json);
            operation_response.state_payload_json = queried_payload_json(
                *this, typed_payloads.second, operation->state_type, operation_response.state_payload_json);
        }
    }

    std::ostringstream payload;
    payload << "{"
            << "\"request_id\":\"" << json_escape(operation_response.request_id) << "\","
            << "\"ok\":" << (operation_response.ok ? "true" : "false") << ","
            << "\"operation_id\":";
    if (operation_response.operation_id.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.operation_id) << "\"";
    }
    payload << ",\"operation\":";
    if (operation_response.operation.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.operation) << "\"";
    }
    payload << ",\"state\":";
    if (operation_response.state.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.state) << "\"";
    }
    payload << ",\"cancel_requested\":" << (operation_response.cancel_requested ? "true" : "false")
            << ",\"feedback_count\":" << operation_response.feedback_count
            << ",\"result\":" << json_null_safe(operation_response.result_json)
            << ",\"state_payload\":" << json_null_safe(operation_response.state_payload_json)
            << ",\"created_ts\":" << operation_response.created_ts
            << ",\"updated_ts\":" << operation_response.updated_ts
            << ",\"error_code\":";
    if (operation_response.error_code.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_code) << "\"";
    }
    payload << ",\"error_message\":";
    if (operation_response.error_message.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_message) << "\"";
    }
    payload << "}";

    ControlResponse response;
    response.timestamp_ns = operation_response.timestamp_ns == 0 ? now_ns() : operation_response.timestamp_ns;
    response.status = 0;
    const auto payload_text = payload.str();
    response.payload.assign(payload_text.begin(), payload_text.end());
    return response;
}

ControlResponse Subsystem::handle_operation_query_request(const ControlRequest& request) {
    OperationQueryResponse operation_response;
    operation_response.timestamp_ns = now_ns();
    const std::string text(request.payload.begin(), request.payload.end());
    operation_response.request_id = json_string_field_local(text, "request_id").value_or("");
    const auto operation_id = json_string_field_local(text, "operation_id");
    if (!operation_id.has_value() || operation_id->empty()) {
        operation_response.ok = false;
        operation_response.error_code = "invalid_argument";
        operation_response.error_message = "missing operation_id";
    } else {
        std::shared_ptr<OperationRecord> operation;
        std::pair<std::optional<ByteBuffer>, std::optional<ByteBuffer>> typed_payloads;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto it = operation_records_.find(*operation_id);
            if (it != operation_records_.end()) {
                operation = it->second;
            }
        }
        if (!operation) {
            operation_response.ok = false;
            operation_response.error_code = "not_found";
            operation_response.error_message = "unknown operation";
        } else {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            operation_response.ok = true;
            operation_response.operation_id = operation->operation_id;
            operation_response.operation = operation->operation;
            operation_response.state = operation->state;
            operation_response.cancel_requested = operation->cancel_requested;
            operation_response.feedback_count = operation->feedback_count;
            operation_response.result_json = operation->result_json;
            operation_response.state_payload_json = operation->state_payload_json;
            typed_payloads = {operation->result_typed, operation->state_payload_typed};
            operation_response.created_ts = operation->created_ts;
            operation_response.updated_ts = operation->updated_ts;
            operation_response.error_code = operation->error_code;
            operation_response.error_message = operation->error_message;
        }
        if (operation) {
            operation_response.result_json = queried_payload_json(*this, typed_payloads.first, operation->result_type,
                                                                  operation_response.result_json);
            operation_response.state_payload_json = queried_payload_json(
                *this, typed_payloads.second, operation->state_type, operation_response.state_payload_json);
        }
    }

    std::ostringstream payload;
    payload << "{"
            << "\"request_id\":\"" << json_escape(operation_response.request_id) << "\","
            << "\"ok\":" << (operation_response.ok ? "true" : "false") << ","
            << "\"operation_id\":";
    if (operation_response.operation_id.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.operation_id) << "\"";
    }
    payload << ",\"operation\":";
    if (operation_response.operation.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.operation) << "\"";
    }
    payload << ",\"state\":";
    if (operation_response.state.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.state) << "\"";
    }
    payload << ",\"cancel_requested\":" << (operation_response.cancel_requested ? "true" : "false")
            << ",\"feedback_count\":" << operation_response.feedback_count
            << ",\"result\":" << json_null_safe(operation_response.result_json)
            << ",\"state_payload\":" << json_null_safe(operation_response.state_payload_json)
            << ",\"created_ts\":" << operation_response.created_ts
            << ",\"updated_ts\":" << operation_response.updated_ts
            << ",\"error_code\":";
    if (operation_response.error_code.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_code) << "\"";
    }
    payload << ",\"error_message\":";
    if (operation_response.error_message.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(operation_response.error_message) << "\"";
    }
    payload << "}";

    ControlResponse response;
    response.timestamp_ns = operation_response.timestamp_ns == 0 ? now_ns() : operation_response.timestamp_ns;
    response.status = 0;
    const auto payload_text = payload.str();
    response.payload.assign(payload_text.begin(), payload_text.end());
    return response;
}
