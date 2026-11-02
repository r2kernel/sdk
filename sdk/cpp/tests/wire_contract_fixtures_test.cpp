// The core client's readers parse the golden Control wire fixtures
// (sdk/testdata/wire). Control builds every fixture with its real code path
// and fails when its output drifts (backend udp::wire_contract_tests); the
// Python SDK tests (tests/test_wire_contract_fixtures.py) read the same text.
// Each check reads a fixture's `wire` text, exactly as Control sends it, with
// the reader the SDK uses for that message.
#include "../src/core/client.cpp"
#include "../src/core/control_requests.hpp"

#include <iostream>

using namespace r2kernel::core;
using r2kernel::core::detail::parse_control_route;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

YAML::Node fixture(const std::string& name) {
    return YAML::LoadFile(std::string(R2KERNEL_SDK_WIRE_DIR) + "/" + name);
}

std::string wire(const std::string& name) { return fixture(name)["wire"].as<std::string>(); }

std::string type_of(const std::string& name, const std::string& path) {
    return fixture(name)["types"][path].as<std::string>();
}

constexpr const char* kValueRef = "msg.core.common.primitive.v1/Int32Value";

// registration_revision is a 32-hex string; an absent codec status is null.
void route_replies() {
    for (const char* name : {"resolve-control-target-reply.json", "resolve-control-target-reply-before-status.json"}) {
        const auto message = fixture(name)["message"];
        const auto route = parse_control_route(wire(name));
        require(route.has_value(), std::string(name) + " produced no route");
        require(type_of(name, "registration_revision") == "string", "registration_revision is not a string");
        require(route->registration_revision == message["registration_revision"].as<std::string>(),
            std::string(name) + " lost its registration revision");
        require(route->port == 0 && route->route_kind == "local_control" && !route->peer_id.has_value(),
            std::string(name) + " local route mismatch");
        require(route->session_id == "session-wire-1", "session id mismatch");
    }
    const auto before = parse_control_route(wire("resolve-control-target-reply-before-status.json"));
    require(!before->codec_context_available.has_value(), "a null codec context is not absent");
    const auto reported = parse_control_route(wire("resolve-control-target-reply.json"));
    require(reported->codec_context_available == std::optional<bool>(true), "codec context lost");
    require(reported->procedures == std::vector<std::string>{"echo"} && reported->controls == std::vector<std::string>{"servo"},
        "advertised endpoints lost");
    require(!parse_control_route(wire("error-reply.json")), "a refusal produced a route");

    // The prepared-route check: null (not yet reported) is accepted as by
    // Python, an explicit false is refused, never a yaml-cpp BadConversion.
    const std::string fingerprint(64, 'a');
    const std::array<std::string, 4> echo{kValueRef, fingerprint, kValueRef, fingerprint};
    auto document = YAML::Load(wire("resolve-control-target-reply.json"));
    validate_prepared_control_peer(document, "component.wire_fixture", "procedure.call_typed/echo", echo);
    document["codec_context_available"] = YAML::Node(YAML::NodeType::Null);
    validate_prepared_control_peer(document, "component.wire_fixture", "procedure.call_typed/echo", echo);
    document["codec_context_available"] = false;
    try {
        validate_prepared_control_peer(document, "component.wire_fixture", "procedure.call_typed/echo", echo);
        throw std::logic_error("a peer without codec context was accepted");
    } catch (const std::runtime_error& error) {
        require(std::string(error.what()).find("codec context is unavailable") != std::string::npos, error.what());
    }
}

void registration_replies() {
    const std::string reply = wire("register-subsystem-reply.json");
    const auto message = fixture("register-subsystem-reply.json")["message"];
    require(json_string_field(reply, kResult) == std::optional<std::string>("ok"), "registration res");
    require(json_string_field(reply, kPoolName) == message["topic_buffer_pool_name"].as<std::string>(),
        "topic buffer pool name");
    require(json_int_field(reply, kPoolSlotSize) == message["topic_buffer_pool_slot_size"].as<long long>(), "slot size");
    require(json_int_field(reply, kPoolSlotCount) == message["topic_buffer_pool_slot_count"].as<long long>(), "slot count");
    require(json_int_field(reply, kEventInlinePayloadThreshold) == message["event_inline_payload_threshold"].as<long long>(),
        "inline threshold");
    require(json_int_field(reply, "event_startup_barrier_ms") == message["event_startup_barrier_ms"].as<long long>(),
        "startup barrier");
    require(json_string_field(reply, "control_local_endpoint") == message["control_local_endpoint"].as<std::string>(),
        "control endpoint");
    require(json_int_field(reply, "control_shm_max_channels") == message["control_shm_max_channels"].as<long long>(),
        "shm channel limit");
    // A null string member is absent, never the next member's name.
    require(type_of("register-subsystem-reply.json", "resource_service_socket") == "null", "fixture socket is not null");
    require(!json_string_field(reply, "resource_service_socket").has_value(), "a null socket read as text");
    require(!json_string_field(reply, "clock_observed_offset_ns").has_value(), "a null offset read as text");
    apply_clock_status_from_registration(reply);
    require(r2kernel_clock_reset() == 0, "clock reset");

    const std::string handler = wire("register-control-handler-reply.json");
    require(json_string_field(handler, kResult) == std::optional<std::string>("ok"), "handler res");
    require(json_int_field(handler, "control_port") ==
        fixture("register-control-handler-reply.json")["message"]["control_port"].as<long long>(), "handler port");
}

void lifecycle_messages() {
    const std::string with_reason = wire("lifecycle-transition-notify.json");
    require(json_string_field(with_reason, "cmd") == std::optional<std::string>(kLifecycleTransition), "lifecycle cmd");
    require(json_string_field(with_reason, "transition") == std::optional<std::string>("start"), "transition");
    require(json_string_field(with_reason, "reason") == std::optional<std::string>("operator request"), "reason");
    const std::string without = wire("lifecycle-transition-notify-without-reason.json");
    // Control sends "reason": null without an operator reason; the next member
    // is "request_id", which the reader returned before.
    require(!json_string_field(without, "reason").has_value(), "a null reason read as text");
    require(json_string_field(without, "request_id") ==
        fixture("lifecycle-transition-notify-without-reason.json")["message"]["request_id"].as<std::string>(), "request id");
    require(json_string_field(without, "previous_lifecycle") == std::optional<std::string>("Ready"), "previous lifecycle");
}

// Event resources: slot geometry, subscriber/publisher locks and the
// replay_existing flag that selects subscribe_ex or subscribe_latest_ex.
void event_resources() {
    const auto slots_message = fixture("request-publish-slots-reply.json")["message"];
    const std::string slots = wire("request-publish-slots-reply.json");
    require(json_int_field(slots, kPublishSlotIndex) == slots_message["publish_slot_index"].as<long long>(), "slot index");
    require(json_int_field(slots, kQueueDepth) == slots_message["queue_depth"].as<long long>(), "queue depth");
    const auto locks = json_subscriber_locks(slots);
    require(locks.size() == slots_message["subscribers"].size(), "subscriber locks");
    for (std::size_t index = 0; index < locks.size(); ++index) {
        const auto row = slots_message["subscribers"][index];
        require(locks[index].subscriber == row["subscriber"].as<std::string>()
                && locks[index].lock_name == row["lock_name"].as<std::string>()
                && locks[index].internal == (row["internal"] && row["internal"].as<bool>()), "subscriber lock row");
    }
    const auto publishers = json_publishers(wire("register-subscription-reply.json"));
    const auto row = fixture("register-subscription-reply.json")["message"]["publishers"][0];
    require(publishers.size() == 1 && publishers[0].publisher == row["publisher"].as<std::string>()
            && publishers[0].lock_name == row["lock_name"].as<std::string>()
            && publishers[0].queue_depth == row["queue_depth"].as<std::size_t>(), "publisher row");
    for (const auto& [name, replay] : {std::pair<const char*, bool>{"notify-publisher.json", true},
                                       std::pair<const char*, bool>{"notify-publisher-late-subscriber.json", false}}) {
        const std::string notice = wire(name);
        require(json_string_field(notice, "cmd") == std::optional<std::string>(kNotifyPublisher), "notify_publisher cmd");
        require(json_bool_field(notice, "replay_existing") == std::optional<bool>(replay), std::string(name) + " replay");
        require(json_string_field(notice, kLockName) == fixture(name)["message"]["lock_name"].as<std::string>(), "lock");
        require(json_int_field(notice, kEntrySlotCount) == 1, "entry slots");
    }
    const std::string subscriber = wire("notify-subscriber.json");
    require(json_string_field(subscriber, kSubsystem) == std::optional<std::string>("component.wire_late")
            && json_string_field(subscriber, kTopic) == std::optional<std::string>("scan"), "notify_subscriber");
    // A pending binding of a restarted source reports registration_changed
    // (accepted as not ready by subsystem_runtime_queries_test).
    require(fixture("binding-resolve-pending-registration-changed.json")["message"]["binding"]["state"].as<std::string>()
            == "registration_changed", "pending binding state");
}

void acknowledgements_and_errors() {
    const auto acknowledgement = YAML::Load(wire("update-subsystem-status-reply.json"));
    require(acknowledgement["res"].as<std::string>() == "ok" && acknowledgement["ok"].as<bool>(), "status acknowledgement");
    const std::string error = wire("error-reply.json");
    require(!json_string_field(error, kResult).has_value(), "an error reply has a res member");
    require(json_string_field(error, "error").value_or("").find("unknown subsystem") != std::string::npos, "error text");
    require(json_string_field(wire("registration-refusal-reply.json"), "reason") ==
        std::optional<std::string>("registration_gate_busy"), "registration refusal reason");
    require(is_retryable_admission_refusal(wire("admission-refusal-reply.json")), "admission refusal not retried");
    require(!is_retryable_admission_refusal(error), "an error reply read as an admission refusal");
}
}  // namespace

int main() {
    try {
        route_replies();
        registration_replies();
        lifecycle_messages();
        event_resources();
        acknowledgements_and_errors();
        std::cout << "wire contract fixture checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
