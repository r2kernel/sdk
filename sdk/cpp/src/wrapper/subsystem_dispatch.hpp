// Included once by subsystem.cpp (same translation unit). Server-side
// Control dispatch and Procedure/typed request handling.

std::string Subsystem::operation_topic(const std::string& target,
                                    const std::string& operation,
                                    const std::string& operation_id,
                                    const std::string& kind) const {
    (void)operation_id;
    return "operation/" + target + "/" + operation + "/" + kind;
}

bool Subsystem::ensure_control_dispatch_server(const char* caller) {
    if (!ensure_core_client(caller)) {
        return false;
    }
    if (control_dispatch_running_) {
        return true;
    }
    // Handlers are served over the local endpoint and Control assigns the
    // route port (port 0 asks for it), so no TCP port has to be found first.
    if (!control_bind_requested_ && core_client_->get_control_port() <= 0) {
        try {
            (void)core_client_->bind_control(0, "0.0.0.0");
        } catch (const std::exception& error) {
            logger_.error(kLifecycleTag, std::string(caller) + ": Control bind failed: " + error.what());
            return false;
        }
    }
    const bool ok = core_client_->start_control_server([this](ControlRequest request) {
        return dispatch_control_request(std::move(request));
    });
    if (ok) {
        control_dispatch_running_ = true;
    }
    return ok;
}

void Subsystem::stop_control_dispatch_server() {
    if (!control_dispatch_running_) {
        return;
    }
    if (core_client_) {
        core_client_->stop_control_server();
    }
    control_dispatch_running_ = false;
}

ControlResponse Subsystem::dispatch_control_request(ControlRequest request) {
    CodecCallbackActivity activity(codec_lifecycle_mutex_, active_codec_callbacks_);
    if (!activity) throw std::runtime_error("Control dispatch unavailable during codec initialization");

    const auto named_publication = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire();
    const auto named_raw = named_publication->raw_endpoints.find(request.topic);
    if (named_publication->raw_endpoints_enabled && named_raw != named_publication->raw_endpoints.end()) {
        const auto handler = named_raw->second;
        return handler(std::move(request));
    }
    if (request.topic == "control_hello") {
        ControlResponse response;
        response.timestamp_ns = now_ns();
        response.status = 0;
        response.payload = std::move(request.payload);
        return response;
    }
    if (starts_with(request.topic, kTypedProcedureTopicPrefix)) {
        return handle_typed_procedure_request(
            request,
            request.topic.substr(std::char_traits<char>::length(kTypedProcedureTopicPrefix))
        );
    }
    if (request.topic == "procedure.call") {
        return handle_procedure_request(request);
    }
    if (starts_with(request.topic, kTypedOperationStartTopicPrefix)) {
        return handle_typed_operation_start_request(
            request,
            request.topic.substr(std::char_traits<char>::length(kTypedOperationStartTopicPrefix))
        );
    }
    if (request.topic == kTypedOperationCancelTopic) {
        return handle_typed_operation_status_request(request, true);
    }
    if (request.topic == kTypedOperationQueryTopic) {
        return handle_typed_operation_status_request(request, false);
    }
    if (request.topic == "operation.start") {
        return handle_operation_start_request(request);
    }
    if (request.topic == "operation.cancel") {
        return handle_operation_cancel_request(request);
    }
    if (request.topic == "operation.query") {
        return handle_operation_query_request(request);
    }
    const auto publication = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch;
    const auto dispatch = publication->acquire();
    const auto endpoint = dispatch->endpoints.find(request.topic);
    if (endpoint != dispatch->endpoints.end()) {
        return handle_registered_control_request(request, endpoint->second);
    }
    if (dispatch->raw_handler) {
        // Preserve request-local mutable captures. The immutable registration
        // is pinned while its callable is cloned without a management lock.
        const auto raw_handler = dispatch->raw_handler;
        return raw_handler(std::move(request));
    }
    ControlResponse response;
    response.timestamp_ns = now_ns();
    response.status = 1;
    return response;
}

ControlResponse Subsystem::handle_registered_control_request(
    const ControlRequest& request,
    const ControlEndpointRegistration& registration
) {
    ControlResponse response;
    response.timestamp_ns = now_ns();
    response.status = 1;

    try {
        // Only the callable needs a request-local clone; normalized metadata
        // remains in the pinned immutable registration. Never invoke the
        // original target: mutable captures must start afresh per request.
        const auto handler = registration.handler;
        const auto& request_type = registration.options.request_type;
        const auto& response_type = registration.options.response_type;
        const auto& timing_support = registration.options.timing_support;

        ControlEndpointRequest endpoint_request;
        endpoint_request.sender = request.sender;
        endpoint_request.topic = request.topic;
        endpoint_request.response_type = response_type;
        endpoint_request.accepted_at_sync_ns = response.timestamp_ns;

        if (is_control_wire(request.payload)) {
            auto decoded = decode_control_request_wire(request.payload);
            endpoint_request.timed = true;
            endpoint_request.timing = decoded.timing;
            endpoint_request.payload = std::move(decoded.payload);
            endpoint_request.payload_type = request_type;
            if (!registration.options.native_payload) {
                if (!request_type.empty()) endpoint_request.payload_json = decode_message_json(request_type, endpoint_request.payload);
                else endpoint_request.payload_json.assign(endpoint_request.payload.begin(), endpoint_request.payload.end());
            }
        } else {
            endpoint_request.payload = request.payload;
            endpoint_request.payload_type = request_type;
            if (!registration.options.native_payload) {
                if (!request_type.empty()) endpoint_request.payload_json = decode_message_json(request_type, endpoint_request.payload);
                else endpoint_request.payload_json.assign(request.payload.begin(), request.payload.end());
            }
        }

        if (timing_support == "required" && !endpoint_request.timed) {
            throw std::runtime_error("Control endpoint requires timed request");
        }
        if (timing_support == "none" && endpoint_request.timed) {
            throw std::runtime_error("Control endpoint does not accept timed request");
        }

        ControlEndpointResponse endpoint_response = handler(endpoint_request);
        response.timestamp_ns = now_ns();
        response.status = endpoint_response.transport_status;

        const std::string response_payload_type = trim_copy(
            endpoint_response.payload_type.empty() ? response_type : endpoint_response.payload_type
        );
        if (endpoint_request.timed) {
            if (!endpoint_response.execution_report.has_value()) {
                throw std::runtime_error("timed Control response requires execution_report");
            }
            const auto& report = *endpoint_response.execution_report;
            ByteBuffer payload_bytes;
            if (registration.options.native_payload || !endpoint_response.payload.empty()) {
                payload_bytes = std::move(endpoint_response.payload);
            } else if (!response_payload_type.empty()) {
                payload_bytes = encode_message_json(
                    response_payload_type,
                    endpoint_response.payload_json.empty() ? "{}" : endpoint_response.payload_json
                );
            } else {
                const auto& payload_json = endpoint_response.payload_json;
                payload_bytes.assign(payload_json.begin(), payload_json.end());
            }

            ControlResponseMetadata meta;
            meta.request_id = endpoint_request.timing.request_id;
            meta.request_id_text = endpoint_request.timing.request_id_text;
            meta.responded_at_sync_ns = response.timestamp_ns;
            meta.accepted_at_sync_ns = report.accepted_at_sync_ns;
            meta.scheduled_for_sync_ns = report.scheduled_for_sync_ns;
            meta.dispatched_at_sync_ns = report.dispatched_at_sync_ns;
            meta.completed_at_sync_ns = report.completed_at_sync_ns;
            meta.lateness_ns = report.lateness_ns;
            const bool execution_ok = report.final_status == "executed" || report.final_status == "executed_late";
            meta.status_code = endpoint_response.status_code.value_or(endpoint_response.ok && execution_ok ? 0 : 1);
            meta.final_status = endpoint_response.status_message.empty()
                ? report.final_status
                : endpoint_response.status_message;
            response.payload = encode_control_response_wire(meta, payload_bytes);
        } else if (registration.options.native_payload || !endpoint_response.payload.empty()) {
            response.payload = std::move(endpoint_response.payload);
        } else if (!response_payload_type.empty()) {
            response.payload = encode_message_json(
                response_payload_type,
                endpoint_response.payload_json.empty() ? "{}" : endpoint_response.payload_json
            );
        } else {
            const auto& payload_json = endpoint_response.payload_json;
            response.payload.assign(payload_json.begin(), payload_json.end());
        }
        return response;
    } catch (const std::exception& exc) {
        const std::string message = exc.what();
        response.timestamp_ns = now_ns();
        response.status = 1;
        response.payload.assign(message.begin(), message.end());
        return response;
    }
}

ControlResponse Subsystem::handle_procedure_request(const ControlRequest& request) {
    ProcedureResponse procedure_response;
    procedure_response.timestamp_ns = now_ns();
    const std::string text(request.payload.begin(), request.payload.end());
    const auto procedure = json_string_field_local(text, "procedure");
    procedure_response.request_id = json_string_field_local(text, "request_id").value_or("");
    if (!procedure.has_value() || procedure->empty()) {
        procedure_response.ok = false;
        procedure_response.error_code = "invalid_argument";
        procedure_response.error_message = "missing procedure name";
    } else {
        ProcedureHandler handler;
        ProcedureOptions contract;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto it = procedure_handlers_.find(*procedure);
            if (it != procedure_handlers_.end()) {
                handler = it->second;
            }
            const auto contract_it = procedure_contracts_.find(*procedure);
            if (contract_it != procedure_contracts_.end()) {
                contract = contract_it->second;
            }
        }
        if (!handler) {
            procedure_response.ok = false;
            procedure_response.error_code = "not_found";
            procedure_response.error_message = "unknown procedure";
        } else if (normalize_timing_support(contract.timing_support) == "required") {
            procedure_response.ok = false;
            procedure_response.error_code = "invalid_argument";
            procedure_response.error_message = "procedure requires timed request";
        } else {
            ProcedureRequest procedure_request;
            procedure_request.sender = request.sender;
            procedure_request.target = json_string_field_local(text, "target").value_or(name_);
            procedure_request.procedure = *procedure;
            procedure_request.request_id = procedure_response.request_id;
            procedure_request.params_json = json_raw_field(text, "params").value_or("null");
            procedure_request.timestamp_ns = request.timestamp_ns;
            procedure_request.timeout_ms = static_cast<int>(json_int_field_local(text, "timeout_ms").value_or(1000));
            procedure_request.request_type = contract.request_type;
            procedure_request.response_type = contract.response_type;
            try {
                // A native handler reads and writes encoded payloads; this JSON
                // request is converted for it.
                if (contract.native_payload) {
                    const auto params = trim_copy(procedure_request.params_json);
                    procedure_request.params_payload =
                        encode_message_json(contract.request_type, params == "null" ? "{}" : params);
                    procedure_request.params_json.clear();
                }
                procedure_response = handler(procedure_request);
                if (procedure_response.request_id.empty()) {
                    procedure_response.request_id = procedure_request.request_id;
                }
                if (procedure_response.timestamp_ns == 0) {
                    procedure_response.timestamp_ns = now_ns();
                }
                if ((contract.native_payload || !procedure_response.result_payload.empty())
                    && procedure_response.ok && !contract.response_type.empty()) {
                    procedure_response.result_json =
                        decode_message_json(contract.response_type, procedure_response.result_payload);
                }
                if (procedure_response.result_json.empty()) {
                    procedure_response.result_json = "null";
                }
            } catch (const std::exception& exc) {
                procedure_response.ok = false;
                procedure_response.request_id = procedure_request.request_id;
                procedure_response.result_json = "null";
                procedure_response.error_code = "execution_failed";
                procedure_response.error_message = exc.what();
                procedure_response.timestamp_ns = now_ns();
            } catch (...) {
                procedure_response.ok = false;
                procedure_response.request_id = procedure_request.request_id;
                procedure_response.result_json = "null";
                procedure_response.error_code = "execution_failed";
                procedure_response.error_message = "unknown exception";
                procedure_response.timestamp_ns = now_ns();
            }
        }
    }

    std::ostringstream payload;
    payload << "{"
            << "\"request_id\":\"" << json_escape(procedure_response.request_id) << "\","
            << "\"ok\":" << (procedure_response.ok ? "true" : "false") << ","
            << "\"result\":" << (procedure_response.result_json.empty() ? "null" : procedure_response.result_json) << ","
            << "\"error_code\":";
    if (procedure_response.error_code.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(procedure_response.error_code) << "\"";
    }
    payload << ",\"error_message\":";
    if (procedure_response.error_message.empty()) {
        payload << "null";
    } else {
        payload << "\"" << json_escape(procedure_response.error_message) << "\"";
    }
    payload << "}";

    ControlResponse response;
    response.timestamp_ns = procedure_response.timestamp_ns == 0 ? now_ns() : procedure_response.timestamp_ns;
    response.status = 0;
    const auto payload_text = payload.str();
    response.payload.assign(payload_text.begin(), payload_text.end());
    return response;
}

ControlResponse Subsystem::handle_typed_procedure_request(const ControlRequest& request,
                                                     const std::string& procedure) {
    ControlResponse response;
    response.timestamp_ns = now_ns();
    response.status = 1;
    try {
        if (!procedure_server_enabled_) {
            throw std::runtime_error("procedure server is not running");
        }
        ProcedureHandler handler;
        ProcedureOptions contract;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto it = procedure_handlers_.find(procedure);
            if (it != procedure_handlers_.end()) {
                handler = it->second;
            }
            const auto contract_it = procedure_contracts_.find(procedure);
            if (contract_it != procedure_contracts_.end()) {
                contract = contract_it->second;
            }
        }
        if (!handler) {
            throw std::runtime_error("unknown typed procedure: " + procedure);
        }
        contract.request_type = trim_copy(std::move(contract.request_type));
        contract.response_type = trim_copy(std::move(contract.response_type));
        if (contract.request_type.empty() || contract.response_type.empty()) {
            throw std::runtime_error("typed procedure contract metadata missing: " + procedure);
        }

        ByteBuffer request_payload = request.payload;
        bool timed = false;
        ControlTimingMetadata timing;
        if (is_control_wire(request_payload)) {
            auto decoded = decode_control_request_wire(request_payload);
            timed = true;
            timing = decoded.timing;
            request_payload = std::move(decoded.payload);
        }
        enforce_timing_support("procedure", procedure, contract.timing_support, timed);

        ProcedureRequest procedure_request;
        procedure_request.sender = request.sender;
        procedure_request.target = name_;
        procedure_request.procedure = procedure;
        procedure_request.request_id = timed ? timing.request_id_text : std::to_string(now_ns());
        if (contract.native_payload) {
            procedure_request.params_json.clear();
            procedure_request.params_payload = std::move(request_payload);
        } else {
            procedure_request.params_json = decode_message_json(contract.request_type, request_payload);
        }
        procedure_request.timestamp_ns = request.timestamp_ns;
        procedure_request.timeout_ms = static_cast<int>(std::max<float>(request.deadline_ms, 1.0F));
        procedure_request.request_type = contract.request_type;
        procedure_request.response_type = contract.response_type;
        procedure_request.timed = timed;
        procedure_request.timing = timing;
        procedure_request.accepted_at_sync_ns = response.timestamp_ns;

        ProcedureResponse procedure_response = handler(procedure_request);
        if (procedure_response.request_id.empty()) {
            procedure_response.request_id = procedure_request.request_id;
        }
        if (procedure_response.result_json.empty()) {
            procedure_response.result_json = "null";
        }
        if (procedure_response.timestamp_ns == 0) {
            procedure_response.timestamp_ns = now_ns();
        }

        // Untimed typed replies have no application-status envelope. Preserve
        // failure through the existing nonzero transport status + diagnostic
        // payload contract instead of encoding a success-shaped typed result.
        if (!timed && (!procedure_response.ok || procedure_response.status_code.value_or(0) != 0)) {
            std::string diagnostic = procedure_response.error_code;
            if (!procedure_response.error_message.empty()) {
                if (!diagnostic.empty()) diagnostic += ": ";
                diagnostic += procedure_response.error_message;
            }
            if (diagnostic.empty()) diagnostic = "typed procedure handler failed";
            response.timestamp_ns = procedure_response.timestamp_ns;
            response.status = 1;
            response.payload.assign(diagnostic.begin(), diagnostic.end());
            return response;
        }

        // An encoded result (required from a native handler) is sent as given.
        const ByteBuffer response_payload = contract.native_payload || !procedure_response.result_payload.empty()
            ? std::move(procedure_response.result_payload)
            : encode_message_json(contract.response_type, procedure_response.result_json);
        response.timestamp_ns = procedure_response.timestamp_ns;
        response.status = 0;
        if (timed) {
            const auto report = procedure_response.execution_report.value_or(
                make_default_execution_report(
                    timing,
                    procedure_request.accepted_at_sync_ns,
                    procedure_response.timestamp_ns,
                    procedure_response.ok
                )
            );
            ControlResponseMetadata meta;
            meta.request_id = timing.request_id;
            meta.request_id_text = timing.request_id_text;
            meta.responded_at_sync_ns = procedure_response.timestamp_ns;
            meta.accepted_at_sync_ns = report.accepted_at_sync_ns;
            meta.scheduled_for_sync_ns = report.scheduled_for_sync_ns;
            meta.dispatched_at_sync_ns = report.dispatched_at_sync_ns;
            meta.completed_at_sync_ns = report.completed_at_sync_ns;
            meta.lateness_ns = report.lateness_ns;
            meta.status_code = procedure_response.status_code.value_or(procedure_response.ok ? 0 : 1);
            meta.final_status = procedure_response.status_message.empty()
                ? report.final_status
                : procedure_response.status_message;
            response.payload = encode_control_response_wire(meta, response_payload);
        } else {
            response.payload = response_payload;
        }
        return response;
    } catch (const std::exception& exc) {
        const std::string message = exc.what();
        response.timestamp_ns = now_ns();
        response.status = 1;
        response.payload.assign(message.begin(), message.end());
        return response;
    } catch (...) {
        const std::string message = "unknown exception";
        response.timestamp_ns = now_ns();
        response.status = 1;
        response.payload.assign(message.begin(), message.end());
        return response;
    }
}

ControlResponse Subsystem::handle_typed_operation_start_request(const ControlRequest& request,
                                                          const std::string& operation_name) {
    ControlResponse response;
    response.timestamp_ns = now_ns();
    response.status = 1;
    try {
        OperationOptions contract;
        {
            std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
            const auto contract_it = operation_contracts_.find(operation_name);
            if (contract_it != operation_contracts_.end()) {
                contract = contract_it->second;
            }
        }
        contract.request_type = trim_copy(std::move(contract.request_type));
        if (contract.request_type.empty()) {
            throw std::runtime_error("typed operation contract metadata missing: " + operation_name);
        }

        ByteBuffer request_payload = request.payload;
        bool timed = false;
        ControlTimingMetadata timing;
        if (is_control_wire(request_payload)) {
            auto decoded = decode_control_request_wire(request_payload);
            timed = true;
            timing = decoded.timing;
            request_payload = std::move(decoded.payload);
        }
        enforce_timing_support("operation", operation_name, contract.timing_support, timed);

        const std::string params_json = decode_message_json(contract.request_type, request_payload);
        const std::string request_id = timed ? timing.request_id_text : std::to_string(now_ns());

        std::ostringstream payload;
        payload << "{"
                << "\"kind\":\"operation_start_request\","
                << "\"target\":\"" << json_escape(name_) << "\","
                << "\"operation\":\"" << json_escape(operation_name) << "\","
                << "\"request_id\":\"" << json_escape(request_id) << "\","
                << "\"params\":" << (params_json.empty() ? "{}" : params_json) << ","
                << "\"request_type\":\"" << json_escape(contract.request_type) << "\","
                << "\"timed\":" << (timed ? "true" : "false") << ","
                << "\"accepted_at_sync_ns\":" << response.timestamp_ns;
        if (timed) {
            payload << ","
                    << "\"timed_request_id\":\"" << json_escape(timing.request_id_text) << "\","
                    << "\"timed_request_id_u64\":" << timing.request_id << ","
                    << "\"timed_sent_at_sync_ns\":" << timing.sent_at_sync_ns << ","
                    << "\"timed_execute_at_sync_ns\":" << timing.execute_at_sync_ns << ","
                    << "\"timed_priority\":" << timing.priority << ","
                    << "\"timed_late_policy\":\"" << json_escape(timing.late_policy) << "\"";
            if (timing.max_lateness_ns.has_value()) {
                payload << ",\"timed_max_lateness_ns\":" << *timing.max_lateness_ns;
            }
        }
        payload << "}";

        ControlRequest wrapped = request;
        wrapped.topic = "operation.start";
        const auto payload_text = payload.str();
        wrapped.payload.assign(payload_text.begin(), payload_text.end());
        return handle_operation_start_request(wrapped);
    } catch (const std::exception& exc) {
        const std::string message = exc.what();
        response.timestamp_ns = now_ns();
        response.status = 1;
        response.payload.assign(message.begin(), message.end());
        return response;
    }
}

ControlResponse Subsystem::handle_typed_operation_status_request(const ControlRequest& request,
                                                              bool cancel) {
    ControlResponse response;
    response.timestamp_ns = now_ns();
    response.status = 1;
    try {
        const std::string decoded = decode_message_json("msg.core.common.primitive.v1/StringValue", request.payload);
        const std::string operation_id = json_string_field_local(decoded, "value").value_or("");
        if (operation_id.empty()) {
            throw std::runtime_error("missing operation_id");
        }
        const std::string request_id = std::to_string(now_ns());
        const std::string payload =
            "{"
            "\"operation_id\":\"" + json_escape(operation_id) + "\","
            "\"request_id\":\"" + json_escape(request_id) + "\""
            "}";
        ControlRequest wrapped = request;
        wrapped.topic = cancel ? "operation.cancel" : "operation.query";
        wrapped.payload.assign(payload.begin(), payload.end());
        return cancel ? handle_operation_cancel_request(wrapped) : handle_operation_query_request(wrapped);
    } catch (const std::exception& exc) {
        const std::string message = exc.what();
        response.timestamp_ns = now_ns();
        response.status = 1;
        response.payload.assign(message.begin(), message.end());
        return response;
    }
}
