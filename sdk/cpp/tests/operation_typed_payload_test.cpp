// The JSON-free typed paths of the C++ SDK against the Python fixtures:
// - Operation events published from already encoded payloads (CodecByteView)
//   are the bytes of the Python encoder (operation-event-*.json), and
//   subscribers can take typed payloads as bytes without JSON conversion;
// - a provider publishes the Python provider's event sequence for every way an
//   Operation ends (operation-event-sequences.json), through the JSON and the
//   encoded-bytes APIs alike, and terminate() ends a running Operation as
//   stop_operation_server() does; queued and running are not published;
// - typed Procedures exchange encoded params and results without JSON
//   (ProcedureOptions::native_payload, the bytes call_procedure overload).
#include "../src/wrapper/operation_event_wire.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "fake_control_socket.hpp"
#include "offline_core_client.hpp"
#include "operation_event_fixtures.hpp"

#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using namespace r2kernel::wrapper;
using namespace r2kernel_test;
namespace wire = r2kernel::wrapper::operation_event_wire;
namespace fs = std::filesystem;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

constexpr const char* kTarget = "component.wire_fixture";
constexpr const char* kOperation = "move_to_pose";
constexpr const char* kTypedFixtures[] = {
    "operation-event-feedback-typed.json",
    "operation-event-result-typed.json",
    "operation-event-result-typed-message.json",
    "operation-event-state-typed.json",
};

CodecByteView view(const ByteBuffer& bytes) { return {bytes.data(), bytes.size()}; }

// Encoded payloads written as given equal the Python encoder's bytes; the
// node path (decode_nodes/encode_nodes, no JSON) writes those payloads too.
void encoded_payloads_write_the_python_bytes() {
    const SchemaContext context(std::vector<std::string>{});
    for (const char* name : kTypedFixtures) {
        const auto document = wire_fixture(name);
        const auto input = document["input"];
        const auto payload = from_hex(document["layout"]["payload_hex"].as<std::string>());
        const auto bytes = wire::encode_typed_payload(
            input["kind"].as<std::string>(), input["operation_id"].as<std::string>(), input["operation"].as<std::string>(),
            input["target"].as<std::string>(), input["ts"].as<std::int64_t>(), input["payload_type"].as<std::string>(),
            payload.data(), payload.size(), text_or_empty(input["state"]), input["cancel_requested"].as<bool>());
        require(bytes == from_hex(document["wire_hex"].as<std::string>()), std::string(name) + ": encoded-bytes event differs");

        const auto codec = context.prepare(input["payload_type"].as<std::string>());
        CodecWorkspace decoded, encoded;
        const auto nodes = codec.decode_nodes(payload, decoded);
        require(codec.encode_nodes(nodes.data, nodes.size, encoded).copy() == payload,
            std::string(name) + ": node encoding differs from the Python payload");
    }
    bool refused = false;
    try {
        const ByteBuffer bytes{1, 2};
        (void)wire::encode_typed_payload("state", "id", kOperation, kTarget, 1, "string", bytes.data(), bytes.size(), "x");
    } catch (const std::invalid_argument&) { refused = true; }
    require(refused, "encoded bytes were written for a declared type that is not a message");
}

// Subscriber delivery without JSON: typed and raw payloads stay bytes.
void bytes_delivery_skips_json() {
    for (const char* name : kTypedFixtures) {
        const auto document = wire_fixture(name);
        bool called = false;
        const auto message = wire::to_message(wire::decode(from_hex(document["wire_hex"].as<std::string>())),
            [&called](const std::string&, const ByteBuffer&) { called = true; return std::string("{}"); }, false);
        require(!called && message.payload_json.empty() && message.payload_encoding == OperationEventPayloadEncoding::typed
                && message.payload_bytes == from_hex(document["layout"]["payload_hex"].as<std::string>())
                && message.payload_type == document["input"]["payload_type"].as<std::string>(),
            std::string(name) + ": typed payload was not delivered as its bytes");
    }
    const auto raw = wire::to_message(wire::decode(from_hex(wire_fixture("operation-event-feedback-raw.json")["wire_hex"].as<std::string>())),
                                      {}, false);
    require(raw.payload_json.empty() && raw.payload_bytes == ByteBuffer({0x00, 0x01, 0xfe, 0xff}), "raw payload representation");
    const auto json_document = wire_fixture("operation-event-feedback-json.json");
    const auto json = wire::to_message(wire::decode(from_hex(json_document["wire_hex"].as<std::string>())), {}, false);
    require(json.payload_json == json_document["layout"]["payload_text"].as<std::string>() && json.payload_bytes.empty(),
        "a JSON payload is not delivered as JSON text");
}

// Records published events and keeps the dispatch handler and subscribers.
class EventClient final : public CoreClient {
public:
    std::int64_t now_ns() const override { return 1; }
    bool refresh_clock_status() override { return true; }
    void start_event_transport() override {}
    void stop_event_transport() override {}
    void publish_prepared_status(const std::string&) override {}
    bool register_event_publish(const EventPublishSpec&) override { return true; }
    bool register_event_subscribe(const EventSubscribeSpec& spec) override {
        std::lock_guard<std::mutex> lock(mutex);
        subscribers[spec.topic] = spec.callback;
        return true;
    }
    bool publish_event(const std::string&, const EventPayload& payload, std::int64_t, int) override {
        std::lock_guard<std::mutex> lock(mutex);
        published.push_back(payload);
        return true;
    }
    int bind_control(int port, const std::string&) override { return port; }
    bool start_control_server(const ControlHandler& started) override { handler = started; return true; }
    void stop_control_server() override {}
    int get_control_port() const override { return 41001; }
    std::optional<ControlRoute> resolve_control_target(const std::string&) override {
        ControlRoute route; route.host = "127.0.0.1"; route.port = 41001; return route;
    }
    bool connect_control(const std::string&, const std::string&, int) override { return true; }
    bool handshake_control(const std::string&, int) override { return true; }
    bool send_control(const std::string&, const std::string&, const ByteBuffer&, std::int64_t, float, float, int) override { return true; }
    std::optional<ControlResponse> recv_control(const std::string&, int) override { return ControlResponse{}; }
    void prepare_control_route(const std::string&, const std::string&, const std::string&, const std::string&,
        const std::string&, const std::string&) override {}
    std::optional<ControlResponse> transact_control(const std::string&, const std::string&, const ByteBuffer&,
        std::int64_t, float, float, int, int) override { return ControlResponse{}; }

    std::vector<wire::Event> events() {
        std::lock_guard<std::mutex> lock(mutex);
        std::vector<wire::Event> out;
        for (const auto& payload : published) out.push_back(wire::decode(payload));
        return out;
    }
    void clear() { std::lock_guard<std::mutex> lock(mutex); published.clear(); }
    EventCallback subscriber(const std::string& topic) {
        std::lock_guard<std::mutex> lock(mutex);
        return subscribers.at(topic);
    }
    ControlHandler handler;
private:
    std::mutex mutex;
    std::vector<ByteBuffer> published;
    std::map<std::string, EventCallback> subscribers;
};

// What the scenario handler does (operation-event-sequences.json "handler").
struct Script {
    std::string scenario;
    bool encoded = false;  // publish through the CodecByteView overloads
    std::mutex mutex;
    std::condition_variable cv;
    bool approaching = false;
    bool released = false;  // the held terminal-state scenarios wait for it
};

// "terminal_<state>_then_cancel_request" / "terminal_failed_then_stop_operation_server":
// the terminal state the handler publishes before it is held, or empty.
std::string held_terminal_state(const std::string& scenario) {
    const std::string prefix = "terminal_";
    for (const char* state : {"failed", "canceled", "succeeded"}) {
        const auto start = prefix + state + "_then_";
        if (scenario.rfind(start, 0) == 0
            && (scenario == start + "cancel_request" || scenario == start + "stop_operation_server"))
            return state;
    }
    return {};
}

class Provider final : public Subsystem {
public:
    Provider(SubsystemOptions options, Script& script, const YAML::Node& sequences)
        : Subsystem(std::move(options)), script_(script) {
        const auto contract = sequences["contract"];
        options_.request_type = contract["request_type"].as<std::string>();
        options_.feedback_type = contract["feedback_type"].as<std::string>();
        options_.result_type = contract["result_type"].as<std::string>();
        options_.state_type = contract["state_type"].as<std::string>();
        const auto payloads = sequences["payloads"];
        const SchemaContext context(std::vector<std::string>{});
        CodecWorkspace workspace;
        for (const auto& [kind, type] : {std::pair<std::string, std::string>{"feedback", options_.feedback_type},
                                         {"state", options_.state_type}, {"result", options_.result_type}}) {
            json_[kind] = to_json(payloads[kind]);
            encoded_[kind] = context.prepare(type).encode_json(json_[kind], workspace).copy();
        }
    }
    std::map<std::string, ByteBuffer> encoded_;

private:
    bool on_initialize() override {
        return register_operation(kOperation, [this](OperationRequest, OperationContext& context) -> std::optional<std::string> {
            const auto& name = script_.scenario;
            const bool encoded = script_.encoded;
            if (const auto terminal = held_terminal_state(name); !terminal.empty()) {
                context.publish_state(terminal);
                std::unique_lock<std::mutex> lock(script_.mutex);
                script_.approaching = true;
                script_.cv.notify_all();
                script_.cv.wait_for(lock, std::chrono::seconds(5), [&] { return script_.released; });
                lock.unlock();
                if (!encoded) return json_["result"];
                context.succeed(view(encoded_["result"]));
                return std::nullopt;
            }
            if (name == "returns" || name == "raises") {
                if (encoded) context.publish_feedback(view(encoded_["feedback"]));
                else context.publish_feedback(json_["feedback"]);
            }
            if (name == "raises") throw std::runtime_error("fixture failure");
            if (name == "fails_with_payload") {
                if (encoded) context.fail("blocked", "blocked", view(encoded_["result"]));
                else context.fail("blocked", "blocked", json_["result"]);
                return std::nullopt;
            }
            // The handler's own terminal state ends the Operation; the result
            // or exception that follows is ignored, as in the Python SDK.
            if (name == "terminal_state_then_returns") {
                context.publish_state("failed");
                if (!encoded) return json_["result"];
                context.succeed(view(encoded_["result"]));
                return std::nullopt;
            }
            if (name == "terminal_state_then_raises") {
                context.publish_state("canceled");
                throw std::runtime_error("fixture failure after the terminal state");
            }
            if (encoded) context.publish_state("approaching", view(encoded_["state"]));
            else context.publish_state("approaching", json_["state"]);
            {
                std::lock_guard<std::mutex> lock(script_.mutex);
                script_.approaching = true;
            }
            script_.cv.notify_all();
            if (name != "returns") {
                const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
                while (!context.is_cancel_requested() && std::chrono::steady_clock::now() < deadline)
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                context.publish_state("still_running");
            }
            if (!encoded) return json_["result"];
            // An encoded result completes the Operation itself; the handler's
            // return then adds nothing, as in the Python SDK.
            if (context.is_cancel_requested()) context.canceled(view(encoded_["result"]));
            else context.succeed(view(encoded_["result"]));
            return std::nullopt;
        }, options_);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
    Script& script_;
    OperationOptions options_;
    std::map<std::string, std::string> json_;
};

fs::path write_manifest(const fs::path& directory, const std::string& name, const std::string& endpoints) {
    fs::create_directories(directory);
    const auto manifest = directory / (name + ".yaml");
    std::ofstream file(manifest);
    file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << name << "\n  role: component\n"
            "provides:\n  local:\n    contract: fixture.typed_payload.private.v1\n    visibility: internal\n"
            "    endpoints:\n" << endpoints;
    require(bool(file), "manifest fixture write failed");
    return manifest;
}

SubsystemOptions subsystem_options(const fs::path& manifest, const std::string& name, std::shared_ptr<CoreClient> client) {
    SubsystemOptions options;
    options.name = name;
    options.manifest_path = manifest;
    options.schema_roots = std::vector<std::string>{};
    options.core_client = std::move(client);
    options.log_sink = [](std::string_view, std::string_view, std::string_view, std::string_view) {};
    return options;
}

ControlRequest control_request(const std::string& topic, const std::string& body) {
    ControlRequest request;
    request.sender = "peer";
    request.topic = topic;
    request.payload.assign(body.begin(), body.end());
    return request;
}

std::string reply_text(const ControlResponse& reply) { return std::string(reply.payload.begin(), reply.payload.end()); }

std::string json_string_field(const std::string& text, const std::string& field) {
    const auto key = "\"" + field + "\":\"";
    const auto start = text.find(key);
    require(start != std::string::npos, "no " + field + " in " + text);
    const auto begin = start + key.size();
    return text.substr(begin, text.find('"', begin) - begin);
}

std::vector<std::string> flag_names(const wire::Event& event) {
    std::vector<std::string> names;
    if (event.encoding != wire::PayloadEncoding::none) names.push_back("PAYLOAD");
    if (event.encoding == wire::PayloadEncoding::json) names.push_back("JSON");
    if (event.encoding == wire::PayloadEncoding::typed) names.push_back("TYPED");
    if (event.cancel_requested) names.push_back("CANCEL_REQUESTED");
    if (event.has_state) names.push_back("STATE");
    return names;
}

void require_sequence(const std::vector<wire::Event>& events, const YAML::Node& expected, const std::string& label) {
    require(events.size() == expected.size(), label + ": " + std::to_string(events.size()) + " events, the Python provider published "
                                                  + std::to_string(expected.size()));
    for (std::size_t index = 0; index < events.size(); ++index) {
        const auto& event = events[index];
        const auto want = expected[index];
        std::vector<std::string> want_flags;
        for (const auto& flag : want["flag_names"]) want_flags.push_back(flag.as<std::string>());
        const std::string where = label + " event " + std::to_string(index) + " (" + event.kind + " " + event.state + ")";
        require(event.kind == want["kind"].as<std::string>() && event.state == want["state"].as<std::string>(), where + ": kind/state differ");
        require(flag_names(event) == want_flags, where + ": flags differ");
        require(event.payload_type == want["payload_type"].as<std::string>(), where + ": payload_type differs");
        require(event.payload == from_hex(want["payload_hex"].as<std::string>()), where + ": payload differs");
        require(event.target == kTarget && event.operation == kOperation, where + ": identity differs");
    }
}

// The Python provider's sequence for each scenario, through both APIs, plus
// terminate() of a running Operation (the stop_operation_server sequence).
void provider_publishes_the_python_sequences() {
    const auto sequences = wire_fixture("operation-event-sequences.json");
    const auto directory = unique_temp_path("operation_typed_payload");
    const auto manifest = write_manifest(directory, kTarget, std::string("      operations: [") + kOperation + "]\n");
    std::vector<std::pair<std::string, std::string>> runs;  // scenario, ending
    for (const auto& entry : sequences["scenarios"]) runs.emplace_back(entry.first.as<std::string>(), entry.first.as<std::string>());
    runs.emplace_back("stop_operation_server", "terminate");
    for (const bool encoded : {false, true}) {
        for (const auto& [scenario, ending] : runs) {
            const std::string label = std::string(encoded ? "encoded " : "JSON ") + ending;
            const auto expected = sequences["scenarios"][scenario];
            auto client = std::make_shared<EventClient>();
            Script script;
            script.scenario = scenario;
            script.encoded = encoded;
            Provider provider(subsystem_options(manifest, kTarget, client), script, sequences);
            provider.initialize();
            require(provider.state() == DeviceState::ready && provider.start_operation_server() && client->handler,
                label + ": provider did not start");
            const auto reply = reply_text(client->handler(control_request("operation.start",
                std::string("{\"operation\":\"") + kOperation + "\",\"request_id\":\"r\",\"params\":{}}")));
            require(reply.find("\"accepted\":true") != std::string::npos
                    && json_string_field(reply, "state") == expected["start_reply_state"].as<std::string>(),
                label + ": start reply differs: " + reply);
            const auto operation_id = json_string_field(reply, "operation_id");
            const auto wait_approaching = [&] {
                std::unique_lock<std::mutex> lock(script.mutex);
                require(script.cv.wait_for(lock, std::chrono::seconds(5), [&] { return script.approaching; }),
                    label + ": handler did not publish its state");
            };
            const auto release_handler = [&] {
                {
                    std::lock_guard<std::mutex> lock(script.mutex);
                    script.released = true;
                }
                script.cv.notify_all();
            };
            if (!held_terminal_state(scenario).empty()) {
                // The handler published its terminal state and is held: the
                // Operation is finished, so neither path may cancel it.
                wait_approaching();
                const auto held = expected["while_held"];
                const auto field = [](const std::string& text, const std::string& name) {
                    return text.find("\"" + name + "\":true") != std::string::npos;
                };
                if (held["cancel_reply_cancel_requested"]) {
                    const auto cancel = reply_text(client->handler(control_request("operation.cancel",
                        "{\"operation_id\":\"" + operation_id + "\",\"request_id\":\"c\"}")));
                    const auto query = reply_text(client->handler(control_request("operation.query",
                        "{\"operation_id\":\"" + operation_id + "\",\"request_id\":\"q\"}")));
                    require(field(cancel, "cancel_requested") == held["cancel_reply_cancel_requested"].as<bool>(),
                        label + ": cancel reply differs from Python: " + cancel);
                    require(json_string_field(query, "state") == held["query_state"].as<std::string>()
                            && field(query, "cancel_requested") == held["query_cancel_requested"].as<bool>(),
                        label + ": query while held differs from Python: " + query);
                } else {
                    provider.stop_operation_server();
                    const auto query = reply_text(client->handler(control_request("operation.query",
                        "{\"operation_id\":\"" + operation_id + "\",\"request_id\":\"q\"}")));
                    require(field(query, "cancel_requested") == held["cancel_requested_after_stop"].as<bool>(),
                        label + ": stop_operation_server changed the finished Operation: " + query);
                }
                release_handler();
            } else if (ending == "cancel_request") {
                wait_approaching();
                const auto cancel = reply_text(client->handler(control_request("operation.cancel",
                    "{\"operation_id\":\"" + operation_id + "\",\"request_id\":\"c\"}")));
                require(cancel.find("\"cancel_requested\":true") != std::string::npos, label + ": cancel reply " + cancel);
            } else if (ending == "stop_operation_server") {
                wait_approaching();
                provider.stop_operation_server();
            } else if (ending == "terminate") {
                wait_approaching();
            }
            // terminate() ends a running Operation; otherwise it waits for the
            // finished one and joins its worker.
            provider.terminate();
            require_sequence(client->events(), expected["events"], label);

            const auto query = reply_text(client->handler(control_request("operation.query",
                "{\"operation_id\":\"" + operation_id + "\",\"request_id\":\"q\"}")));
            require(json_string_field(query, "state") == expected["final_state"].as<std::string>()
                    && (query.find("\"cancel_requested\":true") != std::string::npos) == expected["cancel_requested"].as<bool>(),
                label + ": query differs: " + query);
            // A stored encoded result is decoded for the query only.
            if (expected["result_stored"].as<bool>())
                require(query.find("\"success\":true") != std::string::npos && query.find("\"reached\"") != std::string::npos,
                    label + ": query result is not the decoded result: " + query);
            else
                require(query.find("\"reached\"") == std::string::npos, label + ": query stored a result: " + query);
        }
    }
    std::error_code ignored;
    fs::remove_all(directory, ignored);
}

// An encoded payload for a declared type that is not a message fails the
// Operation; a subscriber can take typed payloads as bytes.
void encoded_payload_rules_and_bytes_subscription() {
    const auto directory = unique_temp_path("operation_typed_rules");
    const auto manifest = write_manifest(directory, kTarget, std::string("      operations: [") + kOperation + "]\n");
    auto client = std::make_shared<EventClient>();
    class Untyped final : public Subsystem {
    public:
        using Subsystem::Subsystem;
        std::string error;
    private:
        bool on_initialize() override {
            OperationOptions options;
            // Declared, but not message types: "bytes" and the default "string".
            options.request_type = options.feedback_type = options.result_type = "bytes";
            return register_operation(kOperation, [this](OperationRequest, OperationContext& context) -> std::optional<std::string> {
                const ByteBuffer bytes{1, 2, 3};
                try { context.publish_state("approaching", view(bytes)); }
                catch (const std::invalid_argument& exc) { error += exc.what(); }
                context.publish_feedback(view(bytes));  // throws: the Operation fails
                return std::nullopt;
            }, options);
        }
        bool on_start() override { return true; }
        bool on_terminate() override { return true; }
        bool on_reset() override { return true; }
    } provider(subsystem_options(manifest, kTarget, client));
    provider.initialize();
    require(provider.state() == DeviceState::ready && provider.start_operation_server(), "untyped provider did not start");
    (void)client->handler(control_request("operation.start", std::string("{\"operation\":\"") + kOperation + "\",\"request_id\":\"r\"}"));
    provider.terminate();
    const auto events = client->events();
    require(provider.error.find("state_type") != std::string::npos, "encoded state for state_type string was accepted");
    // Nothing encoded was published; the Operation failed (no payload).
    bool published_encoded = false;
    for (const auto& event : events) published_encoded = published_encoded || event.encoding != wire::PayloadEncoding::none;
    require(!published_encoded && !events.empty() && events.back().state == "failed",
        "encoded feedback without a message feedback_type did not fail the Operation");

    // Subscription without JSON: the typed payload arrives as its bytes.
    const auto feedback = wire_fixture("operation-event-feedback-typed.json");
    auto subscriber_client = std::make_shared<EventClient>();
    class Subscriber final : public Subsystem {
    public:
        using Subsystem::Subsystem;
    private:
        bool on_initialize() override { return true; }
        bool on_start() override { return true; }
        bool on_terminate() override { return true; }
        bool on_reset() override { return true; }
    } subscriber(subsystem_options(manifest, kTarget, subscriber_client));
    subscriber.initialize();
    std::vector<OperationEventMessage> received;
    OperationSubscribeOptions options;
    options.payload_json = false;
    require(subscriber.subscribe_operation_feedback(kTarget, kOperation, "", [&received](const OperationEventMessage& message) {
        received.push_back(message);
    }, options), "bytes subscription failed");
    subscriber_client->subscriber(std::string("operation/") + kTarget + "/" + kOperation + "/feedback")(
        from_hex(feedback["wire_hex"].as<std::string>()), 1);
    const auto payload = from_hex(feedback["layout"]["payload_hex"].as<std::string>());
    require(received.size() == 1 && received[0].payload_encoding == OperationEventPayloadEncoding::typed
            && received[0].payload_json.empty() && received[0].payload_bytes == payload
            && received[0].payload_type == "msg.core.spatial.v1/Pose", "typed feedback was not delivered as bytes");
    // The subscriber decodes the bytes into its own workspace, without JSON.
    CodecWorkspace workspace;
    const auto nodes = SchemaContext(std::vector<std::string>{}).prepare("msg.core.spatial.v1/Pose")
                           .decode_nodes(received[0].payload_bytes, workspace);
    require(nodes.size > 1 && nodes.data[0].kind == R2KERNEL_NODE_MESSAGE, "received Pose bytes do not decode");
    subscriber.terminate();
    std::error_code ignored;
    fs::remove_all(directory, ignored);
}

// Typed Procedures exchange encoded payloads (both lanes, both handler kinds).
void typed_procedures_without_json() {
    constexpr const char* kRequest = "msg.core.common.primitive.v1/IndexedBoolValue";
    constexpr const char* kResponse = "msg.core.common.primitive.v1/BoolValue";
    const SchemaContext context(std::vector<std::string>{});
    CodecWorkspace workspace;
    const auto request_bytes = context.prepare(kRequest).encode_json("{\"index\":7,\"value\":true}", workspace).copy();
    const auto true_bytes = context.prepare(kResponse).encode_json("{\"value\":true}", workspace).copy();
    const auto false_bytes = context.prepare(kResponse).encode_json("{\"value\":false}", workspace).copy();

    const auto directory = unique_temp_path("procedure_typed_payload");
    const std::string name = "component.procedure_fixture";
    const auto manifest = write_manifest(directory, name, "      procedures: [native, json_handler, unchecked]\n");
    auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
    client->loopback = true;
    class Procedures final : public Subsystem {
    public:
        using Subsystem::Subsystem;
        ByteBuffer seen_params;
        std::string seen_params_json = "unset";
        ByteBuffer reply;
        bool unchecked_registered = true;
    private:
        bool on_initialize() override {
            ProcedureOptions native;
            native.request_type = kRequest;
            native.response_type = kResponse;
            native.native_payload = true;
            ProcedureOptions json = native;
            json.native_payload = false;
            ProcedureOptions unchecked;
            unchecked.native_payload = true;  // without types: refused
            unchecked_registered = register_procedure("unchecked", [](ProcedureRequest) { return ProcedureResponse{}; }, unchecked);
            return register_procedure("native", [this](ProcedureRequest request) {
                seen_params = request.params_payload;
                seen_params_json = request.params_json;
                ProcedureResponse response;
                response.ok = true;
                response.result_payload = reply;
                return response;
            }, native) && register_procedure("json_handler", [this](ProcedureRequest request) {
                seen_params_json = request.params_json;
                ProcedureResponse response;
                response.ok = true;
                response.result_json = "{\"value\":false}";
                return response;
            }, json);
        }
        bool on_start() override { return true; }
        bool on_terminate() override { return true; }
        bool on_reset() override { return true; }
    } procedures(subsystem_options(manifest, name, client));
    procedures.reply = true_bytes;
    procedures.initialize();
    require(procedures.state() == DeviceState::ready && procedures.start_procedure_server() && client->handler,
        "procedure provider did not start");
    require(!procedures.unchecked_registered, "native_payload without types was registered");

    ProcedureCallOptions options;
    options.request_type = kRequest;
    options.response_type = kResponse;
    // Bytes in, bytes out: the handler sees the encoded params, the caller the encoded result.
    auto response = procedures.call_procedure(name, "native", request_bytes, options);
    require(response && response->ok && response->result_payload == true_bytes && response->result_json == "null",
        "encoded call of a native handler: " + (response ? response->error_message : std::string("no response")));
    require(procedures.seen_params == request_bytes && procedures.seen_params_json.empty(),
        "the native handler did not receive the encoded params");
    // A JSON typed call reaches the same handler; the caller decodes.
    response = procedures.call_typed_procedure(name, "native", kRequest, kResponse, "{\"index\":7,\"value\":true}");
    require(response && response->ok && response->result_json.find("true") != std::string::npos
            && procedures.seen_params == request_bytes, "JSON typed call of a native handler");
    // An untyped JSON call is converted for the native handler.
    procedures.seen_params.clear();
    response = procedures.call_procedure(name, "native", "{\"index\":7,\"value\":true}");
    require(response && response->ok && response->result_json.find("true") != std::string::npos
            && procedures.seen_params == request_bytes, "untyped JSON call of a native handler: "
            + (response ? response->result_json + " " + response->error_message : std::string("no response")));
    // An encoded call of a JSON handler: the SDK converts on the provider only.
    response = procedures.call_procedure(name, "json_handler", request_bytes, options);
    require(response && response->ok && response->result_payload == false_bytes
            && procedures.seen_params_json.find("\"index\":7") != std::string::npos, "encoded call of a JSON handler");
    // Both types are required for an encoded call.
    ProcedureCallOptions untyped;
    response = procedures.call_procedure(name, "native", request_bytes, untyped);
    require(response && !response->ok && response->error_code == "invalid_argument", "encoded call without types was sent");
    procedures.terminate();
    std::error_code ignored;
    fs::remove_all(directory, ignored);
}
}  // namespace

int main() {
    try {
        encoded_payloads_write_the_python_bytes();
        bytes_delivery_skips_json();
        provider_publishes_the_python_sequences();
        encoded_payload_rules_and_bytes_subscription();
        typed_procedures_without_json();
        std::cout << "typed payload checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
