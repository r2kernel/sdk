// Offline checks of the Subsystem surface aligned with the Python SDK: typed
// receive, bind/resolve results, Procedure/Operation preparation, Operation
// start timeout, Event priority and the timing header helpers.
#include "subsystem_offline_fixture.hpp"
using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace {
constexpr char kBool[] = "msg.core.common.primitive.v1/BoolValue";

template <typename Call>
bool throws(Call&& call) {
    try { call(); } catch (const std::exception&) { return true; }
    return false;
}

std::shared_ptr<RecordingClient> ready_subsystem(ManifestFiles& files, std::unique_ptr<Fixture>& subsystem) {
    const auto manifest = files.write("fixture.parity", "provides:\n  local:\n    contract: fixture.parity.private.v1\n"
        "    visibility: internal\n    endpoints:\n      controls: [servo]\n");
    auto client = std::make_shared<RecordingClient>();
    subsystem = std::make_unique<Fixture>(options("fixture.parity", manifest, client), [](Fixture& current) {
        ControlEndpointOptions types; types.request_type = kBool; types.response_type = kBool;
        return current.register_control_endpoint("servo", [](const ControlEndpointRequest&) { return ControlEndpointResponse{}; }, types);
    });
    subsystem->initialize();
    require(subsystem->state() == DeviceState::ready, "parity fixture did not reach Ready");
    return client;
}

void typed_receive_and_control_results(ManifestFiles& files) {
    std::unique_ptr<Fixture> subsystem;
    const auto client = ready_subsystem(files, subsystem);
    client->next_recv = ControlResponse{7, 0, subsystem->encode_message_json(kBool, "{\"value\":true}")};
    const auto raw = subsystem->recv_control("peer", "bytes");
    require(raw && raw->payload == client->next_recv->payload, "bytes receive changed the payload");
    const auto decoded = subsystem->recv_control("peer", kBool);
    require(decoded && decoded->timestamp_ns == 7 &&
        YAML::Load(std::string(decoded->payload.begin(), decoded->payload.end()))["value"].as<bool>(),
        "typed receive did not decode the response payload");
    client->next_recv = ControlResponse{7, 0, ByteBuffer{0xff, 0xff, 0xff}};
    require(throws([&]() { (void)subsystem->recv_control("peer", kBool); }), "undecodable typed receive was accepted");
    require(subsystem->bind_control(41234) == 41234, "bind_control did not return the bound port");
    client->resolved_test_port = 41001;
    client->route_procedures = {"echo"};
    const auto route = subsystem->resolve_control_target("peer");
    require(route && route->host == "127.0.0.1" && route->port == 41001 && route->procedures == client->route_procedures,
        "resolve_control_target did not return the route");
    require(subsystem->publish_event("fixture.topic", ByteBuffer{1}) && client->last_publish_priority < 0,
        "Subsystem publish_event did not defer to the registered priority");
    subsystem->terminate();
}

void procedure_and_operation_preparation(ManifestFiles& files) {
    std::unique_ptr<Fixture> subsystem;
    const auto client = ready_subsystem(files, subsystem);
    client->route_controls = {"servo"};
    client->route_procedures = {"echo"};
    client->route_operations = {"work"};
    require(subsystem->prepare_procedure("peer", "echo", 64, 64, 500) && client->last_topic == "procedure.call_typed/echo"
        && client->last_timeout > 0 && client->last_timeout <= 500, "typed Procedure lane was not prepared");
    require(subsystem->prepare_procedure("peer", "echo", 64, 64, 500, false, false) && client->last_topic == "procedure.call",
        "untyped Procedure lane was not prepared");
    require(subsystem->prepare_operation("peer", "work", 64, 64) && client->last_topic == "operation.start_typed/work",
        "typed Operation lane was not prepared");
    require(subsystem->prepare_operation("peer", "work", 64, 64, 1000, false, false) && client->last_topic == "operation.start",
        "untyped Operation lane was not prepared");
    require(throws([&]() { (void)subsystem->prepare_procedure("peer", "missing", 64, 64); }),
        "unadvertised Procedure was prepared");
    require(throws([&]() { (void)subsystem->prepare_procedure("peer", "servo", 64, 64); }),
        "Control endpoint was prepared as a Procedure");
    require(throws([&]() { (void)subsystem->prepare_operation("peer", "missing", 64, 64); }),
        "unadvertised Operation was prepared");
    subsystem->terminate();
}

void operation_start_timeout_matches_python(ManifestFiles& files) {
    std::unique_ptr<Fixture> subsystem;
    const auto client = ready_subsystem(files, subsystem);
    client->resolved_test_port = 41001;
    (void)subsystem->start_operation("peer", "work");
    require(YAML::Load(client->last_payload)["timeout_ms"].IsNull(), "unset Operation timeout was not sent as null");
    require(client->last_source == "fixture.parity", "a non-native core client did not receive the source-wrapped topic");
    (void)subsystem->start_operation("peer", "work", "{}", 250);
    require(YAML::Load(client->last_payload)["timeout_ms"].as<int>() == 250, "explicit Operation timeout was not sent");
    subsystem->terminate();
}

void timing_headers(ManifestFiles& files) {
    std::unique_ptr<Fixture> subsystem;
    (void)ready_subsystem(files, subsystem);
    ControlTimingOptions timing;
    timing.execute_after_ns = 500;
    timing.generated_at_sync_ns = 1000;
    timing.valid_for_ns = 50;
    timing.request_id = "req-1";
    const auto header = YAML::Load(subsystem->build_timing_request_header("peer", timing));
    require(header["request_id"].as<std::string>() == "req-1" &&
        header["correlation_id"].as<std::string>().rfind("corr-", 0) == 0 &&
        header["source_subsystem"].as<std::string>() == "fixture.parity" &&
        header["target_subsystem"].as<std::string>() == "peer" && header["generated_at_sync_ns"].as<long long>() == 1000 &&
        header["priority"].as<int>() == 128 && header["execution"]["mode"].as<std::string>() == "after_offset" &&
        header["execution"]["execute_after_ns"].as<long long>() == 500 &&
        header["execution"]["max_lateness_ns"].as<long long>() == 1000000 &&
        header["execution"]["valid_until_sync_ns"].as<long long>() == 1050 &&
        header["execution"]["late_policy"].as<std::string>() == "execute_immediately",
        "request header does not match the Python shape");
    const auto header_json = subsystem->build_timing_request_header("peer", timing);
    const auto [normalized, accepted] = subsystem->normalize_timing_request_header(header_json, 2000);
    require(accepted == 2000 && YAML::Load(normalized)["execution"]["execute_at_sync_ns"].as<long long>() == 2500,
        "after_offset header was not normalized at the accepted time");
    ControlTimingOptions immediate;
    const auto [now_header, now_accepted] = subsystem->normalize_timing_request_header(
        subsystem->build_timing_request_header("peer", immediate), 3000);
    require(now_accepted == 3000 && YAML::Load(now_header)["execution"]["execute_at_sync_ns"].as<long long>() == 3000,
        "immediate header was not normalized");
    require(throws([&]() { (void)subsystem->normalize_timing_request_header("{\"execution\":{\"mode\":\"later\"}}", 1); }),
        "unsupported timing mode was accepted");
    ControlTimingOptions both = timing; both.valid_until_sync_ns = 9;
    require(throws([&]() { (void)subsystem->build_timing_request_header("peer", both); }),
        "valid_for_ns and valid_until_sync_ns were both accepted");
    ControlTimingOptions late = timing; late.late_policy = "Drop";
    require(throws([&]() { (void)subsystem->build_timing_request_header("peer", late); }), "unknown late policy was accepted");
    require(throws([&]() { (void)subsystem->build_timing_request_header(" "); }), "blank target was accepted");

    const auto response = YAML::Load(subsystem->build_timing_response_header(header_json,
        "{\"accepted_at_sync_ns\":2000,\"final_status\":\"executed\"}", 0, "executed", 4000));
    require(response["request_id"].as<std::string>() == "req-1" && response["received_at_sync_ns"].as<long long>() == 2000 &&
        response["responded_at_sync_ns"].as<long long>() == 4000 && response["status_code"].as<int>() == 0 &&
        response["status_message"].as<std::string>() == "executed" &&
        response["execution_report"]["final_status"].as<std::string>() == "executed",
        "response header does not match the Python shape");

    const auto wire = subsystem->pack_timing_request(header_json, kBool, "{\"value\":\"007\"}");
    const auto request = subsystem->unpack_timing_request(wire);
    require(request.payload_ref == kBool && YAML::Load(request.header_json)["request_id"].as<std::string>() == "req-1" &&
        request.payload_json == "{\"value\":\"007\"}", "timing request did not round-trip (string types must survive)");
    const auto response_wire = subsystem->pack_timing_response("{\"request_id\":\"req-1\"}", kBool, "{\"value\":true}");
    const auto unpacked = subsystem->unpack_timing_response(response_wire);
    require(unpacked.payload_json == "{\"value\":true}" && unpacked.header_json == "{\"request_id\":\"req-1\"}",
        "timing response did not round-trip");
    require(throws([&]() { (void)subsystem->unpack_timing_request("{\"request_header\":{},\"payload_ref\":\" \",\"payload\":{}}"); }),
        "timing request without payload_ref was accepted");
    require(throws([&]() { (void)subsystem->unpack_timing_response("{\"payload_ref\":\"x\",\"payload\":{}}"); }),
        "timing response without response_header was accepted");
    require(throws([&]() { (void)subsystem->pack_timing_request(header_json, kBool, "[1]"); }),
        "non-object timing payload was accepted");
    subsystem->terminate();
}
}  // namespace
int main() {
    return run_offline_suite("Subsystem Python parity", {
        typed_receive_and_control_results,
        procedure_and_operation_preparation,
        operation_start_timeout_matches_python,
        timing_headers});
}
