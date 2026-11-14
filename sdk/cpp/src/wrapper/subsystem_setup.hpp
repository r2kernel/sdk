// Included once by subsystem.cpp (same translation unit). Callback activity
// guards, prepared codec state, Subsystem construction, contract policy,
// declaration validation and runtime contract type validation.

namespace {
// Stop new registration while teardown waits for completed resource acquisition.
class EventTeardownActivity final {
public:
    explicit EventTeardownActivity(std::atomic<bool>& in_progress) : in_progress_(in_progress) {
        if (in_progress_.exchange(true)) throw std::runtime_error("Event transport teardown is already in progress");
    }
    ~EventTeardownActivity() { in_progress_.store(false); }
    EventTeardownActivity(const EventTeardownActivity&) = delete;
    EventTeardownActivity& operator=(const EventTeardownActivity&) = delete;
private:
    std::atomic<bool>& in_progress_;
};

// Count callback lifetimes without holding a shared mutex while user code runs;
// callbacks may safely invoke another managed Control transaction.
class CodecCallbackActivity final {
public:
    CodecCallbackActivity(std::shared_mutex& lifecycle, std::atomic<std::size_t>& active) : active_(active) {
        std::shared_lock<std::shared_mutex> lock(lifecycle, std::try_to_lock);
        if (lock.owns_lock()) { active_.fetch_add(1); ++callback_depth_; acquired_ = true; }
    }
    ~CodecCallbackActivity() { if (acquired_) { --callback_depth_; active_.fetch_sub(1); } }
    explicit operator bool() const noexcept { return acquired_; }
    static bool on_callback_thread() noexcept { return callback_depth_ != 0; }
    CodecCallbackActivity(const CodecCallbackActivity&) = delete;
    CodecCallbackActivity& operator=(const CodecCallbackActivity&) = delete;
private:
    std::atomic<std::size_t>& active_;
    bool acquired_ = false;
    inline static thread_local std::size_t callback_depth_ = 0;
};

// Runtime contract type rule shared with the Python SDK
// (r2kernel.contract.validator): scalars, the _array/vector<>/array<,N>
// wrappers, and versioned references the schema context or an API contract's
// local messages resolve.
bool versioned_type_namespace(std::string_view type_namespace) {
    const auto dot = type_namespace.rfind('.');
    const auto suffix = dot == std::string_view::npos ? type_namespace : type_namespace.substr(dot + 1);
    return suffix.size() > 1 && suffix.front() == 'v'
        && std::all_of(suffix.begin() + 1, suffix.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; });
}

bool api_local_message_known(const std::string& api_id, const std::string& message_name) {
    if (!starts_with(api_id, "api.")) return false;
    try {
        const auto api = contract_loader::find_api_contract(api_id);
        return api && std::find(api->messages.begin(), api->messages.end(), message_name) != api->messages.end();
    } catch (const std::exception&) {
        return false;
    }
}

bool runtime_type_ref_known(const SchemaContext& context, const std::string& type_ref) {
    static const std::unordered_set<std::string> scalar_types = {
        "bool", "bytes", "float32", "float64", "int32", "int64", "str", "string", "uint8", "uint16", "uint32"};
    const auto normalized = trim_copy(type_ref);
    if (normalized.empty()) return false;
    if (scalar_types.count(normalized)) return true;
    constexpr std::string_view array_suffix = "_array";
    if (normalized.size() > array_suffix.size()
        && normalized.compare(normalized.size() - array_suffix.size(), array_suffix.size(), array_suffix) == 0)
        return runtime_type_ref_known(context, normalized.substr(0, normalized.size() - array_suffix.size()));
    if (normalized.back() == '>') {
        if (starts_with(normalized, "vector<")) return runtime_type_ref_known(context, normalized.substr(7, normalized.size() - 8));
        if (starts_with(normalized, "array<")) {
            const auto inner = normalized.substr(6, normalized.size() - 7);
            const auto separator = inner.rfind(',');
            return separator != std::string::npos && runtime_type_ref_known(context, inner.substr(0, separator));
        }
    }
    const auto slash = normalized.find('/');
    if (slash == std::string::npos) return false;
    const auto type_namespace = normalized.substr(0, slash);
    const auto message_name = normalized.substr(slash + 1);
    if (message_name.empty() || !versioned_type_namespace(type_namespace)) return false;
    if (api_local_message_known(type_namespace, message_name)) return true;
    try {
        (void)context.prepare(normalized);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}
}

struct Subsystem::PreparedState {
    std::shared_ptr<const SchemaContext> context;
    std::shared_ptr<const contract_loader::Inventory> inventory;
    std::unordered_map<std::string, PreparedCodec> codecs;
    std::string contract_codecs_json = "{}";
    std::string codec_metadata_json = "{}";
    std::set<std::string> raw_control_topics;
};

// The opaque diagnostics allocation also anchors dispatch management, so the
// public Subsystem holds one pointer for both. Codec identities retain no
// executable plan/context and change only after strict acknowledgment.
struct Subsystem::CodecDiagnostics {
    using DispatchPublication = detail::ControlDispatchPublication<ControlEndpointRegistration, ControlHandler>;
    std::string codec_metadata_json = "{}";
    std::string contract_codecs_json = "{}";
    std::shared_ptr<DispatchPublication> dispatch = std::make_shared<DispatchPublication>();
};

void Subsystem::require_available_managed_codec(const std::shared_ptr<const PreparedState>& state) const {
    DeviceState lifecycle;
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        lifecycle = state_;
    }
    if (lifecycle == DeviceState::error || (!state && managed_codec_initialization_attempted_.load()))
        throw std::runtime_error("Managed codec context is unavailable; initialize must succeed before codec operations");
}

PreparedCodec Subsystem::prepared_codec(const std::string& message_ref) const {
    const auto state = std::atomic_load(&prepared_state_);
    if (!state) throw std::runtime_error("Subsystem codecs are not prepared; initialize before codec access");
    auto found = state->codecs.find(message_ref);
    if (found == state->codecs.end()) found = state->codecs.find(trim_copy(message_ref));
    if (found == state->codecs.end()) throw std::runtime_error("Undeclared codec was not prepared before Ready: " + message_ref);
    return found->second;
}

void Subsystem::prepare_schema_codecs(const std::shared_ptr<PreparedState>& state) {
    contract_loader::InventoryScope scope(state->inventory);
    std::unordered_set<std::string> references;
    const auto add_type = [&references](const std::string& type) {
        const auto ref = trim_copy(type);
        if (ref.empty()) return;
        static const std::unordered_set<std::string> primitive_types = {
            "int32", "uint32", "int64", "float32", "float64", "bool", "string", "str", "bytes", "uint8", "uint16",
            "int32[]", "uint32[]", "int64[]", "float32[]", "float64[]", "bool[]"};
        if (primitive_types.count(ref)) return;
        if (ref.find('/') == std::string::npos) throw std::runtime_error("Structured message requires canonical reference: " + ref);
        references.insert(ref);
    };
    std::set<std::string> contracts(codec_contract_ids_.begin(), codec_contract_ids_.end());
    contracts.insert(declared_apis_.begin(), declared_apis_.end());
    contracts.insert(declared_components_.begin(), declared_components_.end());
    state->raw_control_topics.insert(provided_control_topics_.begin(), provided_control_topics_.end());
    std::map<std::string, std::set<std::string>> contract_references;
    for (const auto& id : contracts) {
        std::set<std::string> apis;
        if (starts_with(id, "api.")) apis.insert(id);
        else if (starts_with(id, "component.")) {
            const auto component = state->inventory->component(id);
            if (!component) throw std::runtime_error("Cannot prepare interface component contract: " + id);
            apis.insert(component->conformance_apis.begin(), component->conformance_apis.end());
        } else throw std::runtime_error("Unsupported prepared interface contract: " + id);
        const bool provided = std::find(declared_apis_.begin(), declared_apis_.end(), id) != declared_apis_.end()
            || std::find(declared_components_.begin(), declared_components_.end(), id) != declared_components_.end();
        auto& contract_refs = contract_references[id];
        const auto add_contract_type = [&](const std::string& type) {
            add_type(type);
            const auto ref = trim_copy(type);
            if (references.count(ref)) contract_refs.insert(ref);
        };
        for (const auto& api_id : apis) {
            const auto api = state->inventory->api(api_id);
            if (!api) throw std::runtime_error("Cannot prepare missing API contract: " + api_id);
            for (const auto& event : api->events) add_contract_type(event.type);
            for (const auto& endpoint : api->controls) {
                add_contract_type(endpoint.request); add_contract_type(endpoint.response);
                if (provided) state->raw_control_topics.insert(endpoint.topic);
            }
            for (const auto& procedure : api->procedures) { add_contract_type(procedure.request); add_contract_type(procedure.response); }
            for (const auto& operation : api->operations) { add_contract_type(operation.request); add_contract_type(operation.feedback); add_contract_type(operation.result); add_contract_type(operation.state); }
        }
    }
    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        for (const auto& event : event_publish_info_) add_type(event.type);
        for (const auto& event : event_subscribe_info_) add_type(event.type);
        for (const auto& [_, endpoint] : control_endpoint_handlers_) {
            add_type(endpoint.options.request_type); add_type(endpoint.options.response_type);
        }
        for (const auto& [_, procedure] : procedure_contracts_) { add_type(procedure.request_type); add_type(procedure.response_type); }
        for (const auto& [_, operation] : operation_contracts_) {
            add_type(operation.request_type); add_type(operation.feedback_type); add_type(operation.result_type); add_type(operation.state_type);
        }
    }
    // Operation query/cancel use this standard utility independently of their API request types.
    references.insert("msg.core.common.primitive.v1/StringValue");
    for (const auto& ref : references) state->codecs.emplace(ref, state->context->prepare(ref));
    std::string json = "{"; bool first_contract = true;
    for (const auto& [contract, refs] : contract_references) {
        if (!first_contract) json += ',';
        first_contract = false;
        json += "\"" + json_escape(contract) + "\":{";
        bool first_ref = true;
        for (const auto& ref : refs) {
            if (!first_ref) json += ',';
            first_ref = false;
            const auto& codec = state->codecs.at(ref);
            json += "\"" + json_escape(ref) + "\":{\"ref\":\"" + json_escape(ref)
                + "\",\"fingerprint\":\"" + codec.fingerprint() + "\",\"source\":\"" + codec.source() + "\"}";
        }
        json += '}';
    }
    state->contract_codecs_json = json + '}';
    std::map<std::string, PreparedCodec> sorted(state->codecs.begin(), state->codecs.end());
    std::string flat_json = "{"; bool first = true;
    for (const auto& [ref, codec] : sorted) {
        if (!first) flat_json += ',';
        first = false;
        flat_json += "\"" + json_escape(ref) + "\":{\"ref\":\"" + json_escape(ref)
            + "\",\"fingerprint\":\"" + codec.fingerprint() + "\",\"source\":\"" + codec.source() + "\"}";
    }
    state->codec_metadata_json = flat_json + '}';
}

std::string Subsystem::contract_codecs_json() const {
    const auto state = std::atomic_load(&prepared_state_);
    return state ? state->contract_codecs_json : "{}";
}

std::string Subsystem::codec_metadata_json() const {
    const auto state = std::atomic_load(&prepared_state_);
    if (!state) return "{}";
    return state->codec_metadata_json;
}

struct Subsystem::OperationRecord {
    std::optional<ProtocolHostAuthority> host_authority;
    std::string operation_id;
    std::string operation;
    std::string params_json = "null";
    std::string request_type;
    std::string feedback_type;
    std::string result_type;
    std::string state_type;
    std::string state = "queued";
    std::string result_json = "null";
    std::string state_payload_json = "null";
    // A result or state payload published as encoded bytes is kept as given
    // (result_type / state_type) and decoded to JSON only for a query.
    std::optional<ByteBuffer> result_typed;
    std::optional<ByteBuffer> state_payload_typed;
    std::string error_code;
    std::string error_message;
    std::int64_t created_ts = 0;
    std::int64_t updated_ts = 0;
    long long feedback_count = 0;
    bool cancel_requested = false;
    // Teardown asks a handler still running after its Operation finished to
    // stop (is_cancel_requested) without changing the finished Operation.
    bool worker_stop_requested = false;
    bool completed = false;
    bool topics_registered = false;
    bool timed = false;
    ControlTimingMetadata timing;
    std::int64_t accepted_at_sync_ns = 0;
};

// A payload handed to an Operation event publisher: JSON text (encoded with
// the declared type when that names a message; "null" is no payload), or bytes
// already encoded with the declared message type (the JSON-free path). Both
// are views, read before the publisher returns.
struct Subsystem::OperationPayload {
    std::string_view json = "null";
    const std::uint8_t* typed = nullptr;
    std::size_t typed_size = 0;
    bool is_typed = false;

    static OperationPayload from_json(std::string_view text) {
        OperationPayload payload;
        payload.json = text;
        return payload;
    }
    static OperationPayload from_typed(const CodecByteView& view) {
        OperationPayload payload;
        payload.typed = view.data;
        payload.typed_size = view.size;
        payload.is_typed = true;
        return payload;
    }
    bool absent() const { return !is_typed && operation_event_wire::is_null_payload(json); }
    // The payload as an Operation record keeps it for a query.
    std::string record_json() const { return is_typed ? "null" : json_null_safe(std::string(json)); }
    std::optional<ByteBuffer> record_typed() const {
        if (!is_typed) return std::nullopt;
        return ByteBuffer(typed, typed + typed_size);
    }
};

Subsystem::Subsystem(SubsystemOptions options)
    : schema_roots_(std::move(options.schema_roots)),
      name_(std::move(options.name)),
      user_data_path_(std::move(options.user_data_path)),
      heartbeat_port_(heartbeat_port_from_env(options.heartbeat_port)),
      logger_(name_, options.log_level, std::move(options.log_sink)),
      timestamp_getter_(),
      contract_policy_(contract_policy_from_env()),
      manifest_path_(std::move(options.manifest_path)),
      launch_bindings_(std::move(options.launch_bindings)),
      core_client_(std::move(options.core_client)) {
    // Initialize the function-local registry before this object's destructor
    // is registered, so static Subsystem instances unregister before it ends.
    (void)subsystem_initialization_registry();
    config_ = detail::load_subsystem_config(user_data_path_);
    // The launcher's package spec roots are searched before the installed dictionary.
    validate_package_spec_roots(caller_package_spec_roots());
    const auto construction_context = schema_roots_ ? std::make_shared<SchemaContext>(*schema_roots_)
                                                     : std::make_shared<SchemaContext>();
    contract_loader::InventoryScope construction_inventory(std::make_shared<contract_loader::Inventory>(construction_context));
    for (const auto& [requirement, target] : load_launch_bindings_env()) {
        launch_bindings_[requirement] = target;
    }
    // The manifest is required whatever the contract policy (Python
    // _load_required_subsystem_manifest).
    const auto manifest_location = first_existing_manifest_path(name_, user_data_path_, manifest_path_);
    if (!manifest_location) {
        throw std::runtime_error("Subsystem manifest is required for " + name_
                                 + ". Pass manifest_path, set R2KERNEL_SUBSYSTEM_MANIFEST, or place " + name_
                                 + ".subsystem.yaml or subsystem.yaml in the working directory or two directories "
                                   "above the user data path.");
    }
    const SubsystemManifest manifest = load_package_subsystem_manifest(*manifest_location, std::nullopt);
    if (manifest.name != name_) {
        throw std::runtime_error("Subsystem name " + python_repr(name_) + " does not match manifest subsystem.name "
                                 + python_repr(manifest.name) + ": " + manifest.path.string());
    }
    {
        manifest_path_ = manifest.path;
        manifest_role_ = manifest.role;
        // Non-spec identifiers are valid only for explicitly internal interfaces.
        // Public declarations still pass the unchanged manifest/spec validation.
        for (const auto* interfaces : {&manifest.provides, &manifest.requirements}) {
            for (const auto& [_, declaration] : *interfaces) {
                if (declaration.visibility == "public" || starts_with(declaration.contract, "api.")
                    || starts_with(declaration.contract, "component."))
                    append_unique(codec_contract_ids_, declaration.contract);
            }
        }
        // Exports have a distinct status type without visibility: their parser
        // already requires a known public api.* projected from a public component.
        for (const auto& [_, declaration] : manifest.exports)
            append_unique(codec_contract_ids_, declaration.contract);
        AllowedEndpointSets provided;
        for (const auto& [_, declaration] : manifest.provides)
            if (declaration.visibility == "internal")
                add_manifest_interface_endpoints(provided, declaration, false);
        provided_control_topics_ = std::move(provided.controls);
        const auto allowed = allowed_endpoint_sets(manifest);
        allowed_event_publish_topics_ = allowed.publishes;
        published_frame_checks_ = package_provided_frame_checks(manifest);
        allowed_event_subscribe_topics_ = allowed.subscribes;
        allowed_control_topics_ = allowed.controls;
        allowed_procedure_names_ = allowed.procedures;
        allowed_operation_names_ = allowed.operations;
        for (const auto& [_, declaration] : manifest.provides) {
            if (declaration.visibility != "public") {
                continue;
            }
            if (starts_with(declaration.contract, "api.")) {
                append_unique(declared_apis_, declaration.contract);
            } else if (starts_with(declaration.contract, "component.")) {
                append_unique(declared_components_, declaration.contract);
            }
        }
        for (const auto& [requirement, _] : launch_bindings_) {
            if (manifest.requirements.find(requirement) == manifest.requirements.end()) {
                throw std::runtime_error("R2KERNEL_BINDINGS references unknown required interface for " + name_ + ": " + requirement);
            }
        }
        subsystem_manifest_ = manifest;
        if (const auto deployment = deployment_manifest_location(options.deployment_manifest_path, manifest.path)) {
            deployment_manifest_ = parse_deployment_manifest(*deployment);
            (void)required_bindings_for_subsystem(subsystem_manifest_, *deployment_manifest_);
        } else if (const auto waiting = unbound_required_interfaces(subsystem_manifest_, launch_bindings_);
                   !waiting.empty()) {
            logger_.warning(kLifecycleTag, "Subsystem " + name_ + " is waiting for runtime binding(s): " + waiting);
            state_ = DeviceState::waiting_binding;
        }
        manifest_status_json_ = build_manifest_status_json(
            manifest, launch_bindings_, deployment_manifest_ ? &*deployment_manifest_ : nullptr);
    }
    std::atomic_store(&acknowledged_codec_diagnostics_,
        std::shared_ptr<const CodecDiagnostics>(std::make_shared<CodecDiagnostics>()));
    install_lifecycle_transition_handler();
}

Subsystem::~Subsystem() {
    // Derived members are already gone here. Users must terminate while the
    // derived object is still alive; this drain protects the remaining base.
    const auto lifecycle_state = subsystem_lifecycle_lock(this);
    OperationDrain operation_drain(*lifecycle_state, operation_server_enabled_);
    {
        std::lock_guard<std::mutex> workers(lifecycle_state->operation_mutex);
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        for (auto& [_, operation] : operation_records_) {
            if (operation && operation_accepts_cancel(*operation)) {
                operation->cancel_requested = true;
                operation->updated_ts = now_ns();
            }
        }
        for (auto& worker : lifecycle_state->operation_workers) {
            if (!worker.finished.load(std::memory_order_acquire)) {
                auto* operation = static_cast<OperationRecord*>(worker.operation.get());
                operation->worker_stop_requested = true;
            }
        }
    }
    drain_operation_workers(*lifecycle_state);
    clear_subsystem_initialization_success(this);
    if (core_client_) {
        core_client_->stop_event_transport();
    }
    stop_control_dispatch_server();
    stop_heartbeat();
    // Both dispatch and heartbeat are drained; break pins of previous ACKed
    // diagnostics, which share this object's opaque publication anchor.
    std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->collect_retired();
    erase_subsystem_lifecycle_lock(this);
}

bool Subsystem::contract_policy_strict() const noexcept {
    return contract_policy_ == "strict";
}

bool Subsystem::contract_policy_off() const noexcept {
    return contract_policy_ == "off";
}

bool Subsystem::reject_contract_violation(const std::string& message) const {
    if (contract_policy_off()) {
        return false;
    }
    if (contract_policy_ == "warn") {
        logger_.warning(kLifecycleTag, message + " [contracts.policy=warn]");
        return false;
    }
    logger_.error(kLifecycleTag, message);
    return true;
}

bool Subsystem::reject_native_control_for_role(const std::string& operation) {
    if (manifest_role_ != "app" && manifest_role_ != "compound") {
        return false;
    }
    logger_.error(
        kLifecycleTag,
        "subsystem role '" + manifest_role_ + "' cannot use native Control surface for " + operation
    );
    return true;
}

void Subsystem::set_core_client(std::shared_ptr<CoreClient> core_client) {
    core_client_ = std::move(core_client);
    install_lifecycle_transition_handler();
}

void Subsystem::install_lifecycle_transition_handler() {
    if (!core_client_) {
        return;
    }
    core_client_->set_lifecycle_transition_handler([this](const LifecycleTransitionRequest& request) {
        const bool ok = request_lifecycle_transition(request.transition, request.reason);
        logger_.info(
            kLifecycleTag,
            std::string("Lifecycle transition ") + (ok ? "accepted" : "rejected")
                + ": transition=" + request.transition + " request_id=" + request.request_id
        );
    });
}

bool ContractValidationReport::ok() const {
    return std::none_of(issues.begin(), issues.end(), [](const auto& issue) { return issue.level == "error"; });
}

std::string ContractValidationReport::format_errors() const {
    std::ostringstream stream;
    bool first = true;
    for (const auto& issue : issues) {
        if (issue.level != "error") {
            continue;
        }
        if (!first) {
            stream << "; ";
        }
        stream << issue.message;
        first = false;
    }
    return stream.str();
}

ContractValidationReport Subsystem::validate_declarations() const {
    std::lock_guard<std::recursive_mutex> registration(event_registration_mutex_);
    const auto prepared = std::atomic_load(&prepared_state_);
    std::unique_ptr<contract_loader::InventoryScope> inventory_scope;
    if (prepared) inventory_scope = std::make_unique<contract_loader::InventoryScope>(prepared->inventory);
    ContractValidationReport report;
    if (declared_apis_.empty() && declared_components_.empty()) {
        return report;
    }

    report.component_ids = declared_components_;
    std::vector<std::string> expanded_apis;
    std::unordered_set<std::string> seen_apis;

    auto add_api = [&](const std::string& api_id) {
        if (seen_apis.insert(api_id).second) {
            expanded_apis.push_back(api_id);
        }
    };

    for (const auto& api_id : declared_apis_) {
        add_api(api_id);
    }
    for (const auto& component_id : declared_components_) {
        const auto component = find_component_contract(component_id);
        if (component == nullptr) {
            push_issue(report, "unknown_component", "unknown declared component '" + component_id + "'");
            continue;
        }
        for (const auto& api_id : component->conformance_apis) {
            add_api(api_id);
        }
    }

    report.api_ids = expanded_apis;

    std::unordered_map<std::string, std::string> published_events;
    std::unordered_map<std::string, std::string> subscribed_events;
    std::unordered_set<std::string> control_topics;
    std::unordered_set<std::string> procedure_names;
    std::unordered_set<std::string> operation_names;
    std::unordered_map<std::string, ControlEndpointOptions> control_contracts;
    std::unordered_map<std::string, ProcedureOptions> procedure_contracts;
    std::unordered_map<std::string, OperationOptions> operation_contracts;
    bool raw_control_server = false;

    {
        std::lock_guard<std::mutex> lock(heartbeat_state_mutex_);
        for (const auto& info : event_publish_info_) {
            published_events[info.topic] = info.type;
        }
        for (const auto& info : event_subscribe_info_) {
            subscribed_events[info.topic] = info.type;
        }
        for (const auto& [topic, registration] : control_endpoint_handlers_) {
            control_topics.insert(topic);
            if (!registration.options.request_type.empty()
                || !registration.options.response_type.empty()
                || registration.options.timing_support != "none") {
                control_contracts[topic] = registration.options;
            }
        }
        const auto raw = std::atomic_load(&acknowledged_codec_diagnostics_)->dispatch->acquire();
        for (const auto& [topic, _] : raw->raw_endpoints) {
            control_topics.insert(topic);
            ControlEndpointOptions options; options.request_type = "bytes"; options.response_type = "bytes";
            control_contracts[topic] = std::move(options);
        }
        raw_control_server = control_server_enabled_ && static_cast<bool>(control_handler_);
        for (const auto& [name, _handler] : procedure_handlers_) {
            procedure_names.insert(name);
        }
        for (const auto& [name, options] : procedure_contracts_) {
            procedure_contracts[name] = options;
        }
        for (const auto& [name, _handler] : operation_handlers_) {
            operation_names.insert(name);
        }
        for (const auto& [name, options] : operation_contracts_) {
            operation_contracts[name] = options;
        }
    }

    for (const auto& api_id : expanded_apis) {
        const auto api_contract = find_api_contract(api_id);
        if (api_contract == nullptr) {
            push_issue(report, "unknown_api_contract", "unknown declared API contract '" + api_id + "'", api_id);
            continue;
        }

        for (const auto& event : api_contract->events) {
            const auto& actual_map = event.direction == "subscribe" ? subscribed_events : published_events;
            const auto actual = actual_map.find(event.topic);
            if (actual == actual_map.end()) {
                push_issue(
                    report,
                    event.direction == "subscribe" ? "missing_event_subscribe" : "missing_event_publish",
                    "missing required " + event.direction + " event '" + event.topic + "'",
                    api_id
                );
                continue;
            }
            if (!matches_type_ref(actual->second, event.type)) {
                push_issue(
                    report,
                    event.direction == "subscribe" ? "event_subscribe_type_mismatch" : "event_publish_type_mismatch",
                    (event.direction == "subscribe" ? "subscription '" : "event '") + event.topic
                        + "' must use type '" + event.type + "', found '" + actual->second + "'",
                    api_id
                );
            }
        }

        for (const auto& endpoint : api_contract->controls) {
            if (control_topics.find(endpoint.topic) == control_topics.end()) {
                if (raw_control_server) {
                    continue;
                }
                push_issue(report, "missing_control_endpoint", "missing required control endpoint '" + endpoint.topic + "'", api_id);
                continue;
            }
            const auto contract = control_contracts.find(endpoint.topic);
            if (contract == control_contracts.end()) {
                if (raw_control_server) {
                    continue;
                }
                push_issue(
                    report,
                    "missing_control_contract",
                    "control endpoint '" + endpoint.topic + "' is registered but request/response type metadata is missing",
                    api_id
                );
                continue;
            }
            if (!matches_type_ref(contract->second.request_type, endpoint.request)) {
                push_issue(
                    report,
                    "control_request_type_mismatch",
                    "control endpoint '" + endpoint.topic + "' request type must be '" + endpoint.request
                        + "', found '" + contract->second.request_type + "'",
                    api_id
                );
            }
            if (!matches_type_ref(contract->second.response_type, endpoint.response)) {
                push_issue(
                    report,
                    "control_response_type_mismatch",
                    "control endpoint '" + endpoint.topic + "' response type must be '" + endpoint.response
                        + "', found '" + contract->second.response_type + "'",
                    api_id
                );
            }
            if (contract->second.timing_support != endpoint.timing_support) {
                push_issue(
                    report,
                    "control_timing_support_mismatch",
                    "control endpoint '" + endpoint.topic + "' timing_support must be '" + endpoint.timing_support
                        + "', found '" + contract->second.timing_support + "'",
                    api_id
                );
            }
        }

        for (const auto& procedure : api_contract->procedures) {
            if (procedure_names.find(procedure.name) == procedure_names.end()) {
                push_issue(report, "missing_procedure", "missing required procedure '" + procedure.name + "'", api_id);
                continue;
            }
            const auto contract = procedure_contracts.find(procedure.name);
            if (contract == procedure_contracts.end()) {
                push_issue(
                    report,
                    "missing_procedure_contract",
                    "procedure '" + procedure.name + "' is registered but request/response type metadata is missing",
                    api_id
                );
                continue;
            }
            if (!matches_type_ref(contract->second.request_type, procedure.request)) {
                push_issue(
                    report,
                    "procedure_request_type_mismatch",
                    "procedure '" + procedure.name + "' request type must be '" + procedure.request
                        + "', found '" + contract->second.request_type + "'",
                    api_id
                );
            }
            if (!matches_type_ref(contract->second.response_type, procedure.response)) {
                push_issue(
                    report,
                    "procedure_response_type_mismatch",
                    "procedure '" + procedure.name + "' response type must be '" + procedure.response
                        + "', found '" + contract->second.response_type + "'",
                    api_id
                );
            }
            if (contract->second.timing_support != procedure.timing_support) {
                push_issue(
                    report,
                    "procedure_timing_support_mismatch",
                    "procedure '" + procedure.name + "' timing_support must be '" + procedure.timing_support
                        + "', found '" + contract->second.timing_support + "'",
                    api_id
                );
            }
        }

        for (const auto& operation : api_contract->operations) {
            if (operation_names.find(operation.name) == operation_names.end()) {
                push_issue(report, "missing_operation", "missing required operation '" + operation.name + "'", api_id);
                continue;
            }
            const auto contract = operation_contracts.find(operation.name);
            if (contract == operation_contracts.end()) {
                push_issue(
                    report,
                    "missing_operation_contract",
                    "operation '" + operation.name + "' is registered but goal/feedback/result type metadata is missing",
                    api_id
                );
                continue;
            }
            if (!matches_type_ref(contract->second.request_type, operation.request)) {
                push_issue(
                    report,
                    "operation_request_type_mismatch",
                    "operation '" + operation.name + "' request type must be '" + operation.request
                        + "', found '" + contract->second.request_type + "'",
                    api_id
                );
            }
            if (!matches_type_ref(contract->second.feedback_type, operation.feedback)) {
                push_issue(
                    report,
                    "operation_feedback_type_mismatch",
                    "operation '" + operation.name + "' feedback type must be '" + operation.feedback
                        + "', found '" + contract->second.feedback_type + "'",
                    api_id
                );
            }
            if (!matches_type_ref(contract->second.result_type, operation.result)) {
                push_issue(
                    report,
                    "operation_result_type_mismatch",
                    "operation '" + operation.name + "' result type must be '" + operation.result
                        + "', found '" + contract->second.result_type + "'",
                    api_id
                );
            }
            const std::string expected_state = operation.state.empty() ? "string" : operation.state;
            const std::string actual_state = contract->second.state_type.empty() ? "string" : contract->second.state_type;
            if (!matches_type_ref(actual_state, expected_state)) {
                push_issue(
                    report,
                    "operation_state_type_mismatch",
                    "operation '" + operation.name + "' state type must be '" + expected_state
                        + "', found '" + actual_state + "'",
                    api_id
                );
            }
            if (contract->second.timing_support != operation.timing_support) {
                push_issue(
                    report,
                    "operation_timing_support_mismatch",
                    "operation '" + operation.name + "' timing_support must be '" + operation.timing_support
                        + "', found '" + contract->second.timing_support + "'",
                    api_id
                );
            }
        }
    }

    for (const auto& [topic, _type] : published_events) {
        if (allowed_event_publish_topics_.find(topic) == allowed_event_publish_topics_.end()
            && !is_operation_transport_topic_for(topic, name_)) {
            push_issue(report, "undeclared_event_publish", "undeclared published event '" + topic + "'");
        }
    }
    for (const auto& [topic, _type] : subscribed_events) {
        if (allowed_event_subscribe_topics_.find(topic) == allowed_event_subscribe_topics_.end()
            && !is_operation_transport_topic_for(topic, name_)
            && operation_subscribe_topics_.find(topic) == operation_subscribe_topics_.end()) {
            push_issue(report, "undeclared_event_subscribe", "undeclared subscribed event '" + topic + "'");
        }
    }
    for (const auto& topic : control_topics) {
        if (allowed_control_topics_.find(topic) == allowed_control_topics_.end()) {
            push_issue(report, "undeclared_control_endpoint", "undeclared control endpoint '" + topic + "'");
        }
    }
    for (const auto& name : procedure_names) {
        if (allowed_procedure_names_.find(name) == allowed_procedure_names_.end()) {
            push_issue(report, "undeclared_procedure", "undeclared procedure '" + name + "'");
        }
    }
    for (const auto& name : operation_names) {
        if (allowed_operation_names_.find(name) == allowed_operation_names_.end()) {
            push_issue(report, "undeclared_operation", "undeclared operation '" + name + "'");
        }
    }

    return report;
}

ContractValidationReport Subsystem::validate_runtime_contract_types(const std::string& policy) const {
    const auto effective_policy = policy.empty() ? contract_policy_ : normalize_contract_policy(policy);
    if (effective_policy == "off") return {};
    const auto prepared = std::atomic_load(&prepared_state_);
    std::shared_ptr<const SchemaContext> context;
    try {
        context = prepared ? prepared->context
                           : schema_roots_ ? std::make_shared<SchemaContext>(*schema_roots_) : std::make_shared<SchemaContext>();
    } catch (const std::exception& error) {
        ContractValidationReport report;
        report.issues.push_back(ValidationIssue{effective_policy == "strict" ? "error" : "warning",
            "schema_runtime_unavailable",
            std::string("contract type validation requires r2kernel-comm schema runtime: ") + error.what(),
            "runtime_contract_types"});
        return report;
    }
    contract_loader::InventoryScope inventory_scope(
        prepared ? prepared->inventory : std::make_shared<contract_loader::Inventory>(context));
    return runtime_contract_type_report(*context, effective_policy);
}

// Every registered endpoint, declared publicly or only on an internal
// interface, needs resolvable type metadata: strict reports errors, any other
// policy warnings. Messages match the Python SDK so both read alike.
ContractValidationReport Subsystem::runtime_contract_type_report(const SchemaContext& context,
                                                                  const std::string& policy) const {
    ContractValidationReport report;
    if (policy == "off") return report;
    std::lock_guard<std::recursive_mutex> registration(event_registration_mutex_);
    const std::string level = policy == "strict" ? "error" : "warning";
    const auto add_issue = [&](const char* code, std::string message) {
        report.issues.push_back(ValidationIssue{level, code, std::move(message), "runtime_contract_types"});
    };
    const auto missing_contract = [&](const char* kind, const std::string& name, const char* detail) {
        add_issue("missing_runtime_contract_metadata",
            std::string(kind) + " '" + name + "' is registered without " + detail);
    };
    const auto ensure_type_ref = [&](const std::string& raw, const std::string& location) {
        const auto type_ref = trim_copy(raw);
        if (type_ref.empty()) add_issue("missing_type_ref", location + " is empty");
        else if (!runtime_type_ref_known(context, type_ref))
            add_issue("unknown_type_ref", location + " references unknown message type '" + type_ref + "'");
    };

    const auto control_info = control_contract_info_impl();
    const std::map<std::string, ControlEndpointOptions> controls(control_info.begin(), control_info.end());
    for (const auto& topic : control_endpoint_names_impl()) {
        const auto contract = controls.find(topic);
        if (contract == controls.end()) {
            missing_contract("control", topic, "request/response type metadata");
            continue;
        }
        ensure_type_ref(contract->second.request_type, "control '" + topic + "' request_type");
        ensure_type_ref(contract->second.response_type, "control '" + topic + "' response_type");
    }
    const auto procedure_info = procedure_contract_info_impl();
    const std::map<std::string, ProcedureOptions> procedures(procedure_info.begin(), procedure_info.end());
    for (const auto& name : procedure_names_impl()) {
        const auto contract = procedures.find(name);
        if (contract == procedures.end()) {
            missing_contract("procedure", name, "request/response type metadata");
            continue;
        }
        ensure_type_ref(contract->second.request_type, "procedure '" + name + "' request_type");
        ensure_type_ref(contract->second.response_type, "procedure '" + name + "' response_type");
    }
    const auto operation_info = operation_contract_info_impl();
    const std::map<std::string, OperationOptions> operations(operation_info.begin(), operation_info.end());
    for (const auto& name : operation_names_impl()) {
        const auto contract = operations.find(name);
        if (contract == operations.end()) {
            missing_contract("operation", name, "request/feedback/result/state type metadata");
            continue;
        }
        ensure_type_ref(contract->second.request_type, "operation '" + name + "' request_type");
        ensure_type_ref(contract->second.feedback_type, "operation '" + name + "' feedback_type");
        ensure_type_ref(contract->second.result_type, "operation '" + name + "' result_type");
        if (!trim_copy(contract->second.state_type).empty())
            ensure_type_ref(contract->second.state_type, "operation '" + name + "' state_type");
    }
    return report;
}
