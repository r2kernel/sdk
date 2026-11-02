// Offline Subsystem declaration and registration regressions: the
// internal/public contract boundary, custom-client route validation and
// canonical Procedure/Operation metadata.
#include "subsystem_offline_fixture.hpp"
using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace {
void internal_and_public_boundary(ManifestFiles& files) {
    const auto manifest = files.write("fixture.internal", "provides:\n  local:\n    contract: fixture.private.v1\n    visibility: internal\n    endpoints:\n      controls: [private_control]\n");
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.internal", manifest, client), [](Fixture& current) {
        ControlEndpointOptions types; types.request_type = "msg.core.common.primitive.v1/BoolValue";
        types.response_type = types.request_type;
        return current.register_control_endpoint("private_control", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, types);
    });
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "non-spec internal contract prevented Ready");
    require(subsystem.prepared_codec("msg.core.common.primitive.v1/BoolValue").fingerprint().size() == 64, "internal registered endpoint type was not prepared");
    require(!YAML::Load(subsystem.contract_codecs_json())["fixture.private.v1"], "non-spec internal contract claimed a spec graph");
    subsystem.terminate();
    bool rejected = false;
    const auto public_manifest = files.write("fixture.public", "provides:\n  local:\n    contract: fixture.private.v1\n");
    try { Fixture invalid(options("fixture.public", public_manifest, std::make_shared<RecordingClient>())); }
    catch (const std::exception&) { rejected = true; }
    require(rejected, "unknown public contract validation was weakened");
    const auto missing = files.write("fixture.missing", "provides:\n  local:\n    contract: api.fixture.missing.v1\n    visibility: internal\n    endpoints:\n      controls: [private_control]\n");
    Fixture unknown(options("fixture.missing", missing, std::make_shared<RecordingClient>()));
    unknown.initialize();
    require(unknown.state() == DeviceState::error, "spec namespace internal contract bypassed missing spec validation");
    unknown.terminate();
}

void custom_client_zero_port_is_rejected(ManifestFiles& files) {
    const auto manifest = files.write("fixture.port_zero", "");
    auto client = std::make_shared<RecordingClient>();
    client->resolved_test_port = 0;
    Fixture subsystem(options("fixture.port_zero", manifest, client));
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "port-zero rejection fixture did not initialize");
    // A custom client cannot claim the native client's negotiated local SHM
    // route. Check every Procedure/Operation facade before transport dispatch.
    const auto procedure = subsystem.call_procedure("peer", "echo", "{}", 10);
    ProcedureCallOptions typed_procedure;
    typed_procedure.request_type = "msg.core.common.primitive.v1/BoolValue";
    typed_procedure.response_type = typed_procedure.request_type;
    const auto typed = subsystem.call_procedure("peer", "echo", "{}", typed_procedure);
    const auto operation = subsystem.start_operation("peer", "work", "{}", 10);
    OperationStartOptions typed_operation;
    typed_operation.request_type = typed_procedure.request_type;
    const auto typed_start = subsystem.start_operation("peer", "work", "{}", typed_operation);
    const auto cancel = subsystem.cancel_operation("peer", "operation-id", 10);
    const auto query = subsystem.query_operation("peer", "operation-id", 10);
    require(procedure && typed && operation && typed_start && cancel && query &&
        !procedure->ok && !typed->ok && !operation->ok && !operation->accepted &&
        !typed_start->ok && !typed_start->accepted && !cancel->ok && !query->ok &&
        client->connect_calls == 0,
        "custom CoreClient port zero reached Procedure/Operation transport");
    subsystem.terminate();
}

void canonical_procedure_operation_metadata_and_request_only_start(ManifestFiles& files) {
    const auto manifest = files.write("fixture.typed_helpers", "provides:\n  local:\n    contract: fixture.typed_helpers.private.v1\n    visibility: internal\n    endpoints:\n      procedures: [echo, plain]\n      operations: [work]\n");
    auto client = std::make_shared<RecordingClient>();
    client->resolved_test_port = 41001;
    Fixture subsystem(options("fixture.typed_helpers", manifest, client), [](Fixture& current) {
        const std::string type = "msg.core.common.primitive.v1/BoolValue";
        const auto echo = [](ProcedureRequest request) { ProcedureResponse response; response.ok = true; response.result_json = request.params_json; return response; };
        OperationOptions operation; operation.request_type = type; operation.feedback_type = type; operation.result_type = type;
        ProcedureOptions plain; plain.request_type = "bytes"; plain.response_type = "bytes";
        return current.register_typed_procedure("echo", echo, type, type) && current.register_procedure("plain", echo, plain) &&
            current.register_operation("work", [](OperationRequest, OperationContext&) { return std::optional<std::string>{}; }, operation);
    });
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "typed helper metadata fixture did not initialize");
    const auto status = client->status();
    require(status["procedures"].size() == 2 && status["operations"].size() == 1 &&
        !status["services"] && !status["actions"] &&
        status["reported_lifecycle"].as<std::string>() == status["lifecycle"].as<std::string>(),
        "heartbeat status does not match the Python SDK keys");
    const std::string type = "msg.core.common.primitive.v1/BoolValue";
    require(status["procedure_contracts"]["echo"]["request_type"].as<std::string>() == type &&
        status["procedure_contracts"]["echo"]["response_type"].as<std::string>() == type &&
        status["procedure_contracts"]["echo"]["timing_support"].as<std::string>() == "none" &&
        status["operation_contracts"]["work"]["request_type"].as<std::string>() == type &&
        status["operation_contracts"]["work"]["feedback_type"].as<std::string>() == type &&
        status["operation_contracts"]["work"]["result_type"].as<std::string>() == type &&
        status["operation_contracts"]["work"]["state_type"].as<std::string>() == "string" &&
        status["operation_contracts"]["work"]["timing_support"].as<std::string>() == "none",
        "registered typed helper contracts were not published canonically");
    require(!status["control_contracts"]["procedure.call_typed/echo"] && !status["control_contracts"]["operation.start_typed/work"],
        "helper metadata invented a native Control endpoint");
    OperationStartOptions start; start.request_type = type;
    const auto result = subsystem.start_operation("peer", "work", "{\"value\":true}", start);
    require(result && result->ok && result->accepted && result->operation_id == "offline-operation" &&
        client->normal_calls == 1 && client->prepared_routes == 1 &&
        client->last_prepared_request == type && client->last_prepared_response.empty(),
        "typed Operation start did not prepare only its request codec before its JSON receipt");
    ControlTransactOptions invalid; invalid.request_type = type;
    bool rejected = false;
    try { (void)subsystem.transact_control("peer", "echo", ByteBuffer{1}, invalid); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected && client->normal_calls == 1, "general Control silently accepted a missing response codec");
    subsystem.terminate();
}
}  // namespace
int main() {
    return run_offline_suite("Subsystem registration", {
        internal_and_public_boundary,
        custom_client_zero_port_is_rejected,
        canonical_procedure_operation_metadata_and_request_only_start});
}
