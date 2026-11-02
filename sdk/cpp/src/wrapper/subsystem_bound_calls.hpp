// Included once at the end of subsystem.cpp (same translation unit). Calls
// on a required interface binding (Python SDK transact_bound_control,
// call_bound_procedure, start_bound_operation): one invocation budget, started
// at entry, covers the strict runtime binding resolution and the call, so a
// binding that is not ready or a spent budget never dispatches.

std::optional<ControlResponse> Subsystem::transact_bound_control(const std::string& binding, const std::string& topic,
    const ByteBuffer& payload, float period_ms, float deadline_ms, int timeout_ms, int priority) {
    const auto started = ControlClock::now();
    const auto target = binding_target(binding, control_call_deadline(started, timeout_ms, deadline_ms));
    if (reject_native_control_for_role("transact")) return std::nullopt;
    return transact_control_impl(target, topic, payload, period_ms, deadline_ms, timeout_ms, priority, false, started);
}

std::optional<ControlResponse> Subsystem::transact_bound_control(const std::string& binding, const std::string& topic,
    const ByteBuffer& payload, const ControlTransactOptions& options) {
    const auto started = ControlClock::now();
    const auto target = binding_target(binding, control_call_deadline(started, options.timeout_ms, options.deadline_ms));
    if (reject_native_control_for_role("transact")) return std::nullopt;
    return transact_control_impl(target, topic, payload, options, false, started);
}

std::optional<ControlResponse> Subsystem::transact_bound_control(const std::string& binding, const std::string& topic,
    const std::string& payload_json, const ControlTransactOptions& options) {
    const auto started = ControlClock::now();
    const auto target = binding_target(binding, control_call_deadline(started, options.timeout_ms, options.deadline_ms));
    if (reject_native_control_for_role("transact")) return std::nullopt;
    return transact_control_impl(target, topic, payload_json, options, false, started);
}

std::optional<ProcedureResponse> Subsystem::call_bound_procedure(const std::string& binding, const std::string& procedure,
    const std::string& params_json, int timeout_ms) {
    const auto started = ControlClock::now();
    const auto target = binding_target(binding, control_call_deadline(started, timeout_ms, static_cast<float>(timeout_ms)));
    return call_procedure_impl(target, procedure, params_json, timeout_ms, started);
}

std::optional<ProcedureResponse> Subsystem::call_bound_procedure(const std::string& binding, const std::string& procedure,
    const std::string& params_json, const ProcedureCallOptions& options) {
    const auto started = ControlClock::now();
    const auto target = binding_target(binding,
        control_call_deadline(started, options.timeout_ms, static_cast<float>(options.timeout_ms)));
    return call_procedure_impl(target, procedure, params_json, options, started);
}

std::optional<OperationStartResponse> Subsystem::start_bound_operation(const std::string& binding,
    const std::string& operation, const std::string& params_json, std::optional<int> timeout_ms) {
    const auto started = ControlClock::now();
    const int budget_ms = timeout_ms.value_or(1000);
    const auto target = binding_target(binding, control_call_deadline(started, budget_ms, static_cast<float>(budget_ms)));
    return start_operation_impl(target, operation, params_json, timeout_ms, started);
}

std::optional<OperationStartResponse> Subsystem::start_bound_operation(const std::string& binding,
    const std::string& operation, const std::string& params_json, const OperationStartOptions& options) {
    const auto started = ControlClock::now();
    const int budget_ms = options.timeout_ms.value_or(1000);
    const auto target = binding_target(binding, control_call_deadline(started, budget_ms, static_cast<float>(budget_ms)));
    return start_operation_impl(target, operation, params_json, options, started);
}
