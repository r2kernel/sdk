// Offline raw Control regressions: truthful raw endpoint metadata, named raw
// registration, and the result-ready/reclamation wrappers.
#include "subsystem_offline_fixture.hpp"
using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace {
void raw_control_metadata(ManifestFiles& files) {
    const auto manifest = files.write("fixture.raw", "provides:\n  servo: api.actuator.servo.v1\n  radar: api.sensor.radar.source.v1\n  local:\n    contract: fixture.raw.private.v1\n    visibility: internal\n    endpoints:\n      controls: [private_control]\nrequires:\n  digital:\n    contract: api.io.digital.timed_output.v1\n    required: false\n", "gateway");
    auto client = std::make_shared<RecordingClient>();
    Fixture gateway(options("fixture.raw", manifest, client), [](Fixture& current) {
        ControlEndpointOptions typed; typed.request_type = "msg.actuator.servo.v1/ServoControlRequest";
        typed.response_type = "msg.actuator.servo.v1/ServoControlResponse"; typed.timing_support = "required";
        return current.register_control_endpoint("servo_control", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, typed)
            && current.start_control_server([](ControlRequest request) { return ControlResponse{1, 0, request.payload}; });
    });
    gateway.initialize();
    require(gateway.state() == DeviceState::ready, "raw public gateway failed validation");
    const auto contracts = client->status()["control_contracts"];
    require(contracts["private_control"]["request_type"].as<std::string>() == "bytes", "internal raw Control topic was omitted");
    require(contracts["radar_cycle"]["request_type"].as<std::string>() == "bytes"
        && contracts["radar_cycle"]["response_type"].as<std::string>() == "bytes", "raw server advertised spec typed bytes");
    require(!contracts["servo_link"] && !contracts["servo_joint"], "removed servo topics were advertised");
    require(contracts["servo_control"]["request_type"].as<std::string>() == "msg.actuator.servo.v1/ServoControlRequest"
        && contracts["servo_control"]["response_type"].as<std::string>() == "msg.actuator.servo.v1/ServoControlResponse", "raw catch-all replaced explicit typed endpoint metadata");
    require(!contracts["digital_output_control"], "raw server advertised a required peer endpoint as its own");
    gateway.terminate();
}

void named_raw_control_registration(ManifestFiles& files) {
    const auto manifest = files.write("fixture.named_raw", "provides:\n  local:\n    contract: fixture.raw.private.v1\n    visibility: internal\n    endpoints:\n      controls: [raw, typed]\n");
    auto client = std::make_shared<RecordingClient>();
    int calls = 0;
    Fixture subsystem(options("fixture.named_raw", manifest, client), [&](Fixture& current) {
        require(current.register_raw_control_endpoint("raw", [&](ControlRequest request) {
            ++calls;
            require(request.timestamp_ns == 42 && request.sender == "verified.sender" && request.payload == ByteBuffer({0,1,2,3,4,5,6,7}), "named raw changed full request");
            require(request.period_ms == 20.0F && request.deadline_ms == 30.0F, "named raw changed timing fields");
            return ControlResponse{request.timestamp_ns, 0, request.payload};
        }), "named raw registration failed");
        ControlEndpointOptions typed; typed.request_type = "msg.core.common.primitive.v1/BoolValue"; typed.response_type = typed.request_type;
        require(!current.register_control_endpoint("raw", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, typed), "typed registration replaced raw");
        require(current.register_control_endpoint("typed", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, typed), "mixed typed registration failed");
        require(!current.register_raw_control_endpoint("typed", [](ControlRequest) { return ControlResponse{}; }), "raw registration replaced typed");
        require(!current.register_raw_control_endpoint("unknown", [](ControlRequest) { return ControlResponse{}; }), "undeclared raw registration accepted");
        require(!current.register_raw_control_endpoint("operation.query", [](ControlRequest) { return ControlResponse{}; }), "raw replaced SDK Protocol");
        return current.get_control_info() == std::vector<std::string>({"raw", "typed"});
    });
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "named raw registration was not visible before lifecycle validation");
    require(client->status()["controls"].size() == 2 && client->status()["control_contracts"]["raw"]["request_type"].as<std::string>() == "bytes", "named raw heartbeat is untruthful");
    require(subsystem.start_control_server() && static_cast<bool>(client->dispatch), "named raw server did not enable");
    const auto response = client->dispatch(ControlRequest{"verified.sender", "raw", {0,1,2,3,4,5,6,7}, 42, 20.0F, 30.0F});
    require(response.timestamp_ns == 42 && response.payload == ByteBuffer({0,1,2,3,4,5,6,7}) && calls == 1, "named raw dispatch changed timestamp or bytes");
    subsystem.stop_control_server();
    require(!client->dispatch, "raw callbacks remained enabled after stop");
    subsystem.terminate();
    for (const auto& labels : std::vector<std::string>{"      request_type: bytes\n", "      request_type: bytes\n      response_type: msg.fixture.v1/Result\n", "      request_type: msg.fixture.v1/Request\n      response_type: bytes\n"}) {
        const auto labelled = files.write("fixture.raw_labels", "provides:\n  local:\n    contract: fixture.raw.private.v1\n    visibility: internal\n    endpoints:\n      controls: [raw]\n    labels:\n" + labels);
        Fixture current(options("fixture.raw_labels", labelled, std::make_shared<RecordingClient>()));
        require(!current.register_raw_control_endpoint("raw", [](ControlRequest) { return ControlResponse{}; }), "Raw contradicted private typed/one-sided labels");
        require(current.get_control_info().empty(), "Rejected raw labels published an endpoint");
    }
    const auto byte_labels = files.write("fixture.raw_bytes", "provides:\n  local:\n    contract: fixture.raw.private.v1\n    visibility: internal\n    endpoints:\n      controls: [raw]\n    labels:\n      request_type: bytes\n      response_type: bytes\n");
    Fixture labelled_bytes(options("fixture.raw_bytes", byte_labels, std::make_shared<RecordingClient>()));
    require(labelled_bytes.register_raw_control_endpoint("raw", [](ControlRequest request) { return ControlResponse{request.timestamp_ns,0,request.payload}; }), "Explicit bytes labels rejected raw registration");
    require(!subsystem.register_raw_control_endpoint("procedure.private", [](ControlRequest) { return ControlResponse{}; })
        && !subsystem.register_raw_control_endpoint("operation.private", [](ControlRequest) { return ControlResponse{}; }), "Raw claimed reserved Protocol family");
    const auto public_manifest = files.write("fixture.named_public", "provides:\n  servo: api.actuator.servo.v1\n", "gateway");
    Fixture public_gateway(options("fixture.named_public", public_manifest, std::make_shared<RecordingClient>()));
    require(!public_gateway.register_raw_control_endpoint("servo_control", [](ControlRequest) { return ControlResponse{}; }), "Raw bypassed typed authoritative API declaration");
}

void result_wrapper_and_unsupported_client(ManifestFiles& files) {
    const auto manifest = files.write("fixture.result", "provides:\n  local:\n    contract: fixture.result.private.v1\n    visibility: internal\n    endpoints:\n      controls: [echo]\n", "gateway");
    auto client = std::make_shared<RecordingClient>();
    Fixture subsystem(options("fixture.result", manifest, client), [](Fixture& current) {
        ControlEndpointOptions types; types.request_type = "msg.core.common.primitive.v1/BoolValue";
        types.response_type = types.request_type;
        return current.register_control_endpoint("echo", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, types);
    });
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "result wrapper fixture did not initialize");
    const ByteBuffer raw{0, 128, 255};
    const auto raw_result = subsystem.transact_control_result("peer", "echo", raw, 1.0F, 100.0F, 432, 7);
    require(raw_result && raw_result->payload == raw && client->last_timeout == 432 && client->last_priority == 7,
        "raw result wrapper changed arguments");
    subsystem.wait_control_reclaimed("peer", "echo", 321);
    require(client->reclaim_calls == 1 && client->last_timeout == 321, "reclamation facade changed timeout");
    ControlTransactOptions spec;
    spec.request_type = "msg.core.common.primitive.v1/BoolValue"; spec.response_type = spec.request_type;
    const auto encoded = subsystem.encode_message_json(spec.request_type, "{\"value\":true}");
    const auto binary_result = subsystem.transact_control_result("peer", "echo", encoded, spec);
    const auto json_result = subsystem.transact_control_result("peer", "echo", std::string("{\"value\":true}"), spec);
    require(binary_result && json_result && binary_result->payload == encoded && json_result->payload == encoded
        && client->prepared_routes == 2, "result options/JSON paths skipped shared codec preparation");
    client->reject_route = true;
    bool rejected = false;
    try { (void)subsystem.transact_control_result("peer", "echo", encoded, spec); }
    catch (const std::exception&) { rejected = true; }
    require(rejected && client->result_calls == 3, "result transport bypassed peer schema failure");
    rejected = false;
    try { (void)client->CoreClient::transact_control_result("peer", "echo", raw, 1, 1, 1, 10, 1); }
    catch (const std::exception&) { rejected = true; }
    require(rejected, "unsupported base result client silently fell back");
    rejected = false;
    try { client->CoreClient::wait_control_reclaimed("peer", "echo", 10); }
    catch (const std::exception&) { rejected = true; }
    require(rejected, "unsupported base reclamation client silently succeeded");
    subsystem.terminate();
}
}  // namespace
int main() {
    return run_offline_suite("Raw Control metadata", {
        raw_control_metadata,
        named_raw_control_registration,
        result_wrapper_and_unsupported_client});
}
