// Included once, inside subsystem.cpp's anonymous namespace (same translation
// unit). Per-call timing/status codes, request ids, codec JSON and the
// Control request/response wire format. Keeping them in subsystem.cpp's
// unit preserves same-unit inlining into the call and dispatch paths.

bool is_operation_transport_topic_for(const std::string& topic, const std::string& subsystem_name) {
    const std::string prefix = "operation/" + subsystem_name + "/";
    if (!starts_with(topic, prefix)) {
        return false;
    }
    const std::string rest = topic.substr(prefix.size());
    const auto first_separator = rest.find('/');
    if (first_separator == std::string::npos || first_separator != rest.rfind('/')) {
        return false;
    }
    const auto suffix_pos = topic.rfind('/');
    if (suffix_pos == std::string::npos || suffix_pos <= prefix.size()) {
        return false;
    }
    const std::string suffix = topic.substr(suffix_pos + 1);
    return suffix == "feedback" || suffix == "result" || suffix == "state";
}

void push_issue(ContractValidationReport& report,
                std::string code,
                std::string message,
                std::string api_id = {}) {
    report.issues.push_back(ValidationIssue{"error", std::move(code), std::move(message), std::move(api_id)});
}

void enforce_timing_support(
    const std::string& kind,
    const std::string& name,
    const std::string& timing_support,
    bool timed
) {
    const std::string normalized = normalize_timing_support(timing_support);
    if (normalized == "required" && !timed) {
        throw std::runtime_error(kind + " '" + name + "' requires timed request");
    }
    if (normalized == "none" && timed) {
        throw std::runtime_error(kind + " '" + name + "' does not accept timed request");
    }
}

// A late policy is exactly drop, reject or execute_immediately (wire codes
// 0-2). Any other name or received code is refused: it never degrades to
// execute_immediately, the most permissive policy.
std::uint8_t late_policy_code(const std::string& late_policy) {
    if (late_policy == "drop") {
        return 0;
    }
    if (late_policy == "reject") {
        return 1;
    }
    if (late_policy == "execute_immediately") {
        return 2;
    }
    throw std::invalid_argument("late_policy must be one of drop, reject, execute_immediately; found '"
        + late_policy + "'");
}

std::string late_policy_name(std::uint8_t code) {
    switch (code) {
    case 0:
        return "drop";
    case 1:
        return "reject";
    case 2:
        return "execute_immediately";
    default:
        throw std::invalid_argument("unknown late_policy wire code " + std::to_string(code));
    }
}

std::uint8_t final_status_code(const std::string& final_status) {
    if (final_status == "executed") {
        return 0;
    }
    if (final_status == "executed_late") {
        return 1;
    }
    if (final_status == "dropped") {
        return 2;
    }
    if (final_status == "rejected") {
        return 3;
    }
    if (final_status == "failed") {
        return 4;
    }
    if (final_status == "canceled") {
        return 5;
    }
    return 4;
}

std::string final_status_name(std::uint8_t code) {
    switch (code) {
    case 0:
        return "executed";
    case 1:
        return "executed_late";
    case 2:
        return "dropped";
    case 3:
        return "rejected";
    case 5:
        return "canceled";
    default:
        return "failed";
    }
}

std::uint64_t request_id_u64_from_text(const std::string& text) {
    const std::string trimmed = trim_copy(text);
    if (trimmed.empty()) {
        static std::atomic<std::uint64_t> counter {1};
        const auto tick = static_cast<std::uint64_t>(
            std::chrono::steady_clock::now().time_since_epoch().count()
        );
        return tick ^ counter.fetch_add(1, std::memory_order_relaxed);
    }
    try {
        if (trimmed.rfind("0x", 0) == 0 || trimmed.rfind("0X", 0) == 0) {
            return static_cast<std::uint64_t>(std::stoull(trimmed, nullptr, 16));
        }
        const bool decimal = std::all_of(trimmed.begin(), trimmed.end(), [](unsigned char ch) {
            return std::isdigit(ch) != 0;
        });
        return static_cast<std::uint64_t>(std::stoull(trimmed, nullptr, decimal ? 10 : 16));
    } catch (...) {
        return static_cast<std::uint64_t>(std::hash<std::string>{}(trimmed));
    }
}

std::string request_id_text_from_u64(std::uint64_t value) {
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
}

bool is_control_wire(const ByteBuffer& payload) {
    return payload.size() >= 8
        && payload[0] == static_cast<std::uint8_t>('S')
        && payload[1] == static_cast<std::uint8_t>('D')
        && payload[2] == static_cast<std::uint8_t>('T')
        && payload[3] == static_cast<std::uint8_t>('R');
}

// Reusable per-thread output for the unprepared (generic) codec ABI. Core
// performs one conversion per call and still re-reads the spec roots each call.
class GenericCodecOutput final {
public:
    GenericCodecOutput() {
        if (r2kernel_codec_output_create(&output_) != 0)
            throw std::runtime_error("generic codec output create failed");
    }
    ~GenericCodecOutput() { r2kernel_codec_output_free(output_); }
    GenericCodecOutput(const GenericCodecOutput&) = delete;
    GenericCodecOutput& operator=(const GenericCodecOutput&) = delete;
    R2KernelCodecOutput* get() const noexcept { return output_; }
private:
    R2KernelCodecOutput* output_ = nullptr;
};

constexpr char kUnpreparedCodecWarning[] =
    "unprepared codec path re-reads spec roots per call; initialize() or use SchemaContext/PreparedCodec for repeated use";

void warn_unprepared_codec_once(const Logger& logger) {
    static std::once_flag warned;
    std::call_once(warned, [&logger] { logger.warning(kCodecTag, kUnpreparedCodecWarning); });
}

R2KernelCodecOutput* generic_codec_output() {
    thread_local GenericCodecOutput output;
    return output.get();
}

ByteBuffer codec_encode_message_json(const Logger& logger, const std::string& message_ref, std::string_view json) {
    warn_unprepared_codec_once(logger);
    auto* output = generic_codec_output();
    const int rc = r2kernel_codec_encode_message_json_into(
        message_ref.c_str(), reinterpret_cast<const std::uint8_t*>(json.data()), json.size(), output);
    if (rc != 0) {
        throw std::runtime_error("message encode failed rc=" + std::to_string(rc) + " ref=" + message_ref);
    }
    std::size_t size = 0;
    const auto* bytes = r2kernel_codec_output_bytes(output, &size);
    return size == 0 ? ByteBuffer{} : ByteBuffer(bytes, bytes + size);
}

std::string codec_decode_message_json(const Logger& logger, const std::string& message_ref, const ByteBuffer& payload) {
    warn_unprepared_codec_once(logger);
    auto* output = generic_codec_output();
    const int rc = r2kernel_codec_decode_message_json_into(message_ref.c_str(), payload.data(), payload.size(), output);
    if (rc != 0) {
        throw std::runtime_error("message decode failed rc=" + std::to_string(rc) + " ref=" + message_ref);
    }
    std::size_t size = 0;
    const auto* bytes = r2kernel_codec_output_bytes(output, &size);
    return std::string(reinterpret_cast<const char*>(bytes), size);
}

ByteBuffer encode_control_request_wire(const ControlTimingMetadata& meta, const ByteBuffer& payload) {
    std::size_t out_len = 0;
    int rc = r2kernel_codec_encode_control_request_v2(
        meta.request_id,
        meta.generated_at_sync_ns,
        meta.sent_at_sync_ns,
        meta.execute_at_sync_ns,
        meta.valid_until_sync_ns.value_or(0),
        meta.max_lateness_ns.value_or(-1),
        static_cast<std::uint16_t>(std::clamp(meta.priority, 0, 65535)),
        late_policy_code(meta.late_policy),
        payload.data(),
        payload.size(),
        nullptr,
        0,
        &out_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control request encode failed rc=" + std::to_string(rc));
    }
    ByteBuffer out(out_len);
    rc = r2kernel_codec_encode_control_request_v2(
        meta.request_id,
        meta.generated_at_sync_ns,
        meta.sent_at_sync_ns,
        meta.execute_at_sync_ns,
        meta.valid_until_sync_ns.value_or(0),
        meta.max_lateness_ns.value_or(-1),
        static_cast<std::uint16_t>(std::clamp(meta.priority, 0, 65535)),
        late_policy_code(meta.late_policy),
        payload.data(),
        payload.size(),
        out.data(),
        out.size(),
        &out_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control request encode failed rc=" + std::to_string(rc));
    }
    out.resize(out_len);
    return out;
}

struct DecodedControlRequestWire {
    ControlTimingMetadata timing;
    ByteBuffer payload;
};

DecodedControlRequestWire decode_control_request_wire(const ByteBuffer& wire) {
    std::uint64_t request_id = 0;
    std::int64_t generated_at_sync_ns = 0;
    std::int64_t sent_at_sync_ns = 0;
    std::int64_t execute_at_sync_ns = 0;
    std::int64_t valid_until_sync_ns = 0;
    std::int64_t max_lateness_ns = -1;
    std::uint16_t priority = 0;
    std::uint8_t late_policy = 2;
    std::size_t payload_len = 0;
    int rc = r2kernel_codec_decode_control_request_v2(
        wire.data(),
        wire.size(),
        &request_id,
        &generated_at_sync_ns,
        &sent_at_sync_ns,
        &execute_at_sync_ns,
        &valid_until_sync_ns,
        &max_lateness_ns,
        &priority,
        &late_policy,
        nullptr,
        0,
        &payload_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control request decode failed rc=" + std::to_string(rc));
    }
    ByteBuffer payload(payload_len);
    rc = r2kernel_codec_decode_control_request_v2(
        wire.data(),
        wire.size(),
        &request_id,
        &generated_at_sync_ns,
        &sent_at_sync_ns,
        &execute_at_sync_ns,
        &valid_until_sync_ns,
        &max_lateness_ns,
        &priority,
        &late_policy,
        payload.data(),
        payload.size(),
        &payload_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control request decode failed rc=" + std::to_string(rc));
    }
    payload.resize(payload_len);
    ControlTimingMetadata timing;
    timing.request_id = request_id;
    timing.request_id_text = request_id_text_from_u64(request_id);
    timing.generated_at_sync_ns = generated_at_sync_ns;
    timing.sent_at_sync_ns = sent_at_sync_ns;
    timing.execute_at_sync_ns = execute_at_sync_ns;
    if (valid_until_sync_ns > 0) {
        timing.valid_until_sync_ns = valid_until_sync_ns;
    }
    if (max_lateness_ns >= 0) {
        timing.max_lateness_ns = max_lateness_ns;
    }
    timing.priority = static_cast<int>(priority);
    timing.late_policy = late_policy_name(late_policy);
    return DecodedControlRequestWire {timing, std::move(payload)};
}

ByteBuffer encode_control_response_wire(const ControlResponseMetadata& meta, const ByteBuffer& payload) {
    std::size_t out_len = 0;
    int rc = r2kernel_codec_encode_control_response_v2(
        meta.request_id,
        meta.responded_at_sync_ns,
        meta.accepted_at_sync_ns,
        meta.scheduled_for_sync_ns,
        meta.dispatched_at_sync_ns,
        meta.sampled_at_sync_ns.value_or(0),
        meta.completed_at_sync_ns,
        meta.lateness_ns,
        static_cast<std::int32_t>(meta.status_code),
        final_status_code(meta.final_status),
        payload.data(),
        payload.size(),
        nullptr,
        0,
        &out_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control response encode failed rc=" + std::to_string(rc));
    }
    ByteBuffer out(out_len);
    rc = r2kernel_codec_encode_control_response_v2(
        meta.request_id,
        meta.responded_at_sync_ns,
        meta.accepted_at_sync_ns,
        meta.scheduled_for_sync_ns,
        meta.dispatched_at_sync_ns,
        meta.sampled_at_sync_ns.value_or(0),
        meta.completed_at_sync_ns,
        meta.lateness_ns,
        static_cast<std::int32_t>(meta.status_code),
        final_status_code(meta.final_status),
        payload.data(),
        payload.size(),
        out.data(),
        out.size(),
        &out_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control response encode failed rc=" + std::to_string(rc));
    }
    out.resize(out_len);
    return out;
}

DecodedControlResponse decode_control_response_wire(const ByteBuffer& wire) {
    std::uint64_t request_id = 0;
    std::int64_t responded_at_sync_ns = 0;
    std::int64_t accepted_at_sync_ns = 0;
    std::int64_t scheduled_for_sync_ns = 0;
    std::int64_t dispatched_at_sync_ns = 0;
    std::int64_t sampled_at_sync_ns = 0;
    std::int64_t completed_at_sync_ns = 0;
    std::int64_t lateness_ns = 0;
    std::int32_t status_code = 0;
    std::uint8_t final_status = 4;
    std::size_t payload_len = 0;
    int rc = r2kernel_codec_decode_control_response_v2(
        wire.data(),
        wire.size(),
        &request_id,
        &responded_at_sync_ns,
        &accepted_at_sync_ns,
        &scheduled_for_sync_ns,
        &dispatched_at_sync_ns,
        &sampled_at_sync_ns,
        &completed_at_sync_ns,
        &lateness_ns,
        &status_code,
        &final_status,
        nullptr,
        0,
        &payload_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control response decode failed rc=" + std::to_string(rc));
    }
    ByteBuffer payload(payload_len);
    rc = r2kernel_codec_decode_control_response_v2(
        wire.data(),
        wire.size(),
        &request_id,
        &responded_at_sync_ns,
        &accepted_at_sync_ns,
        &scheduled_for_sync_ns,
        &dispatched_at_sync_ns,
        &sampled_at_sync_ns,
        &completed_at_sync_ns,
        &lateness_ns,
        &status_code,
        &final_status,
        payload.data(),
        payload.size(),
        &payload_len
    );
    if (rc != 0) {
        throw std::runtime_error("Control response decode failed rc=" + std::to_string(rc));
    }
    payload.resize(payload_len);

    DecodedControlResponse decoded;
    decoded.timing.request_id = request_id;
    decoded.timing.request_id_text = request_id_text_from_u64(request_id);
    decoded.timing.responded_at_sync_ns = responded_at_sync_ns;
    decoded.timing.accepted_at_sync_ns = accepted_at_sync_ns;
    decoded.timing.scheduled_for_sync_ns = scheduled_for_sync_ns;
    decoded.timing.dispatched_at_sync_ns = dispatched_at_sync_ns;
    if (sampled_at_sync_ns > 0) {
        decoded.timing.sampled_at_sync_ns = sampled_at_sync_ns;
    }
    decoded.timing.completed_at_sync_ns = completed_at_sync_ns;
    decoded.timing.lateness_ns = lateness_ns;
    decoded.timing.status_code = static_cast<int>(status_code);
    decoded.timing.final_status = final_status_name(final_status);
    decoded.payload = std::move(payload);
    return decoded;
}

ControlExecutionReport make_default_execution_report(
    const ControlTimingMetadata& timing,
    std::int64_t accepted_at_sync_ns,
    std::int64_t completed_at_sync_ns,
    bool ok
) {
    ControlExecutionReport report;
    report.accepted_at_sync_ns = accepted_at_sync_ns;
    report.scheduled_for_sync_ns = timing.execute_at_sync_ns > 0 ? timing.execute_at_sync_ns : accepted_at_sync_ns;
    report.dispatched_at_sync_ns = completed_at_sync_ns;
    report.completed_at_sync_ns = completed_at_sync_ns;
    report.lateness_ns = std::max<std::int64_t>(0, report.dispatched_at_sync_ns - report.scheduled_for_sync_ns);
    report.final_status = ok ? "executed" : "failed";
    return report;
}

std::string string_value_json(const std::string& value) {
    return std::string{"{\"value\":\""} + json_escape(value) + "\"}";
}

ProcedureResponse make_procedure_response(
    bool ok,
    std::string request_id,
    std::string result_json,
    std::string error_code,
    std::string error_message,
    std::int64_t timestamp_ns
) {
    ProcedureResponse response;
    response.ok = ok;
    response.request_id = std::move(request_id);
    response.result_json = std::move(result_json);
    response.error_code = std::move(error_code);
    response.error_message = std::move(error_message);
    response.timestamp_ns = timestamp_ns;
    return response;
}
