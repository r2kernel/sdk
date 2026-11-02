// Operation feedback/result/state events use the Python SDK's SDAE wire. The
// golden fixtures (sdk/testdata/wire/operation-event-*.json) are the Python
// encoder's output (python tests/test_operation_event_wire_fixture.py). Here
// the C++ encoder must produce the same bytes from the same input (typed
// payloads through the prepared codec; tests/operation_typed_payload_test.cpp
// covers the encoded-bytes path), the C++ decoder must return the Python
// decoder's message, and a Subsystem must publish its events exactly as the
// Python provider does and deliver fixture events to its Operation subscribers.
#include "../src/wrapper/operation_event_wire.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "fake_control_socket.hpp"
#include "operation_event_fixtures.hpp"

#include <yaml-cpp/yaml.h>

#include <chrono>
#include <condition_variable>
#include <cstdlib>
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
namespace wire = r2kernel::wrapper::operation_event_wire;
namespace fs = std::filesystem;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

constexpr const char* kFixtures[] = {
    "operation-event-feedback-json.json",
    "operation-event-state-cancel-requested.json",
    "operation-event-result-json.json",
    "operation-event-result-typed.json",
    "operation-event-feedback-raw.json",
    "operation-event-feedback-typed.json",
    "operation-event-result-typed-message.json",
    "operation-event-state-typed.json",
    "operation-event-state-json-string-type.json",
    "operation-event-state-terminal.json",
};
constexpr const char* kTarget = "component.wire_fixture";
constexpr const char* kOperation = "move_to_pose";

YAML::Node fixture(const std::string& name) { return r2kernel_test::wire_fixture(name); }
using r2kernel_test::from_hex;
using r2kernel_test::has_flag;
using r2kernel_test::text_or_empty;
using r2kernel_test::to_json;

bool numeric(const std::string& text, double& value) {
    char* end = nullptr;
    value = std::strtod(text.c_str(), &end);
    return !text.empty() && end == text.c_str() + text.size();
}

// JSON values compared structurally; numbers by value (the codec may write a
// float32 1.0 as 1), other scalars by their text.
bool same_value(const YAML::Node& left, const YAML::Node& right) {
    if (left.Type() != right.Type()) return false;
    switch (left.Type()) {
    case YAML::NodeType::Null: return true;
    case YAML::NodeType::Scalar: {
        double a = 0, b = 0;
        if (left.Tag() != "!" && right.Tag() != "!" && numeric(left.Scalar(), a) && numeric(right.Scalar(), b)) return a == b;
        return left.Scalar() == right.Scalar();
    }
    case YAML::NodeType::Sequence:
        if (left.size() != right.size()) return false;
        for (std::size_t index = 0; index < left.size(); ++index)
            if (!same_value(left[index], right[index])) return false;
        return true;
    case YAML::NodeType::Map:
        if (left.size() != right.size()) return false;
        for (const auto& entry : left) {
            const auto key = entry.first.as<std::string>();
            if (!right[key] || !same_value(entry.second, right[key])) return false;
        }
        return true;
    default: return false;
    }
}

wire::TypedPayloadDecoder codec_decoder(const SchemaContext& context) {
    return [&context](const std::string& payload_type, const ByteBuffer& payload) {
        CodecWorkspace workspace;
        return std::string(context.prepare(payload_type).decode_json(payload, workspace));
    };
}

// The payload a C++ caller passes for a fixture: the Python json.dumps text
// for a JSON payload (so the bytes match), the input value as JSON otherwise.
std::string fixture_payload_json(const YAML::Node& document) {
    if (document["layout"]["payload_text"]) return document["layout"]["payload_text"].as<std::string>();
    return to_json(document["input"]["payload"]);
}

// The bytes the SDK publishers write for a fixture input
// (operation_event_wire::encode_payload with the prepared codec).
ByteBuffer publisher_bytes(const YAML::Node& document, const SchemaContext& context) {
    const auto input = document["input"];
    return wire::encode_payload(
        input["kind"].as<std::string>(), input["operation_id"].as<std::string>(), input["operation"].as<std::string>(),
        input["target"].as<std::string>(), input["ts"].as<std::int64_t>(), text_or_empty(input["payload_type"]),
        fixture_payload_json(document),
        [&context](const std::string& payload_type, const std::string& json) {
            CodecWorkspace workspace;
            return context.prepare(payload_type).encode_json(json, workspace).copy();
        },
        text_or_empty(input["state"]), input["cancel_requested"].as<bool>());
}

// The encoder input of a fixture, as wire::encode receives it.
wire::Event fixture_event(const YAML::Node& document, const SchemaContext& context) {
    const auto input = document["input"];
    const auto layout = document["layout"];
    wire::Event event;
    event.kind = input["kind"].as<std::string>();
    event.operation_id = input["operation_id"].as<std::string>();
    event.operation = input["operation"].as<std::string>();
    event.target = input["target"].as<std::string>();
    event.payload_type = text_or_empty(input["payload_type"]);
    event.state = text_or_empty(input["state"]);
    event.cancel_requested = input["cancel_requested"].as<bool>();
    event.ts = input["ts"].as<std::int64_t>();
    if (has_flag(layout, "TYPED")) {
        // The C++ codec writes the bytes the Python codec wrote.
        CodecWorkspace workspace;
        event.payload = context.prepare(event.payload_type).encode_json(to_json(input["payload"]), workspace).copy();
        event.encoding = wire::PayloadEncoding::typed;
    } else if (has_flag(layout, "JSON")) {
        const auto text = layout["payload_text"].as<std::string>();
        event.payload.assign(text.begin(), text.end());
        event.encoding = wire::PayloadEncoding::json;
    } else if (has_flag(layout, "PAYLOAD")) {
        event.payload = from_hex(input["payload_bytes_hex"].as<std::string>());
        event.encoding = wire::PayloadEncoding::raw;
    }
    return event;
}

void encoder_writes_the_python_bytes() {
    const SchemaContext context(std::vector<std::string>{});
    std::size_t typed = 0;
    for (const char* name : kFixtures) {
        const auto document = fixture(name);
        const auto expected = from_hex(document["wire_hex"].as<std::string>());
        const auto event = fixture_event(document, context);
        require(wire::encode(event) == expected, std::string(name) + ": C++ encoder bytes differ from the Python fixture");
        if (event.encoding == wire::PayloadEncoding::raw) continue;  // the C++ API publishes JSON text only
        // The publishers' path: payload JSON plus the declared type.
        require(publisher_bytes(document, context) == expected,
            std::string(name) + ": publisher encoding differs from the Python fixture");
        if (event.encoding == wire::PayloadEncoding::typed) ++typed;
        if (event.payload_type.empty()) {
            require(wire::encode_json(event.kind, event.operation_id, event.operation, event.target, event.ts,
                        fixture_payload_json(document), event.state, event.cancel_requested) == expected,
                std::string(name) + ": untyped JSON encoding differs from the Python fixture");
        }
    }
    require(typed == 4, "expected four typed fixtures through the publisher path");
    // A message type with a null payload writes no payload; a non-message
    // declared type keeps JSON text.
    require(wire::is_message_type("msg.core.common.primitive.v1/StringValue") && !wire::is_message_type("string")
            && !wire::is_message_type("bytes"), "message type detection differs from Python ('/' in the type)");
}

void decoder_returns_the_python_message() {
    const SchemaContext context(std::vector<std::string>{});
    for (const char* name : kFixtures) {
        const auto document = fixture(name);
        const auto layout = document["layout"];
        const auto decoded = document["decoded"];
        const auto bytes = from_hex(document["wire_hex"].as<std::string>());
        require(wire::is_wire(bytes), std::string(name) + ": not recognized as SDAE");
        const auto event = wire::decode(bytes);
        const auto fields = layout["fields"];
        require(event.operation_id == fields["operation_id"].as<std::string>() && event.operation == fields["operation"].as<std::string>()
                && event.target == fields["target"].as<std::string>() && event.payload_type == fields["payload_type"].as<std::string>(),
            std::string(name) + ": string fields differ");
        require(event.kind == layout["kind"].as<std::string>() && event.ts == layout["ts"].as<std::int64_t>(),
            std::string(name) + ": kind or ts differs");
        require(event.has_state == has_flag(layout, "STATE") && event.cancel_requested == has_flag(layout, "CANCEL_REQUESTED"),
            std::string(name) + ": state or cancel flag differs");

        const auto message = wire::to_message(event, codec_decoder(context));
        require(message.operation_id == decoded["operation_id"].as<std::string>() && message.operation == decoded["operation"].as<std::string>()
                && message.target == decoded["target"].as<std::string>() && message.kind == decoded["kind"].as<std::string>(),
            std::string(name) + ": message identity differs");
        require(message.state == text_or_empty(decoded["state"]), std::string(name) + ": state differs");
        require(message.cancel_requested == (decoded["cancel_requested"] && decoded["cancel_requested"].as<bool>()),
            std::string(name) + ": cancel_requested differs");
        require(message.timestamp_ns == decoded["ts"].as<std::int64_t>(), std::string(name) + ": ts differs");
        require(message.payload_type == text_or_empty(decoded["payload_type"]), std::string(name) + ": payload_type differs");
        const auto expected_encoding = has_flag(layout, "TYPED") ? OperationEventPayloadEncoding::typed
            : has_flag(layout, "JSON") ? OperationEventPayloadEncoding::json
            : has_flag(layout, "PAYLOAD") ? OperationEventPayloadEncoding::raw : OperationEventPayloadEncoding::none;
        require(message.payload_encoding == expected_encoding, std::string(name) + ": payload_encoding differs");
        if (document["decoded_payload_representation"]) {
            require(message.payload_json == "\"" + decoded["payload"].as<std::string>() + "\"",
                std::string(name) + ": raw payload is not the hex JSON string: " + message.payload_json);
            require(message.payload_bytes == from_hex(decoded["payload"].as<std::string>()), std::string(name) + ": raw bytes differ");
        } else {
            require(message.payload_bytes.empty(), std::string(name) + ": payload_bytes set for a non-raw payload");
            if (decoded["payload"].IsNull()) {
                require(message.payload_json == "null", std::string(name) + ": absent payload is not null");
            } else {
                require(same_value(YAML::Load(message.payload_json), decoded["payload"]),
                    std::string(name) + ": payload differs: " + message.payload_json);
            }
        }
        if (layout["payload_text"])
            require(message.payload_json == layout["payload_text"].as<std::string>(), std::string(name) + ": JSON text was rewritten");
    }
    // A raw-bytes payload and a JSON string with the same text are told apart.
    const auto raw = wire::to_message(wire::decode(from_hex(fixture("operation-event-feedback-raw.json")["wire_hex"].as<std::string>())),
                                      codec_decoder(context));
    const auto json_string = wire::to_message(wire::decode(wire::encode_json("feedback", "id", kOperation, kTarget, 1, raw.payload_json)),
                                              codec_decoder(context));
    require(raw.payload_json == json_string.payload_json && raw.payload_encoding != json_string.payload_encoding,
        "a raw payload cannot be told from a JSON string payload");
}

void malformed_wire_is_refused() {
    const auto bytes = from_hex(fixture(kFixtures[0])["wire_hex"].as<std::string>());
    const auto refused = [](const ByteBuffer& candidate) {
        try { (void)wire::decode(candidate); } catch (const std::runtime_error&) { return true; }
        return false;
    };
    require(refused(ByteBuffer(bytes.begin(), bytes.end() - 1)), "truncated payload was accepted");
    require(refused(ByteBuffer(bytes.begin(), bytes.begin() + 29)), "short header was accepted");
    auto other_version = bytes;
    other_version[4] = 2;
    require(refused(other_version), "unknown version was accepted");
    const std::string json = "{\"operation_id\":\"x\"}";
    require(!wire::is_wire(ByteBuffer(json.begin(), json.end())), "JSON text taken for SDAE");
    bool too_long = false;
    try { (void)wire::encode_json("feedback", std::string(70000, 'x'), kOperation, kTarget, 1, "null"); }
    catch (const std::length_error&) { too_long = true; }
    require(too_long, "a field over 65535 bytes was encoded");
    bool unknown_kind = false;
    try { (void)wire::encode_json("progress", "id", kOperation, kTarget, 1, "null"); }
    catch (const std::invalid_argument&) { unknown_kind = true; }
    require(unknown_kind, "an unknown kind was encoded");
}

// A CoreClient that records published events and keeps subscriber callbacks.
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
    bool publish_event(const std::string& topic, const EventPayload& payload, std::int64_t, int) override {
        std::lock_guard<std::mutex> lock(mutex);
        published.emplace_back(topic, payload);
        changed.notify_all();
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

    EventCallback subscriber(const std::string& topic) {
        std::lock_guard<std::mutex> lock(mutex);
        const auto found = subscribers.find(topic);
        require(found != subscribers.end() && bool(found->second), "no subscriber callback for " + topic);
        return found->second;
    }
    // The decoded events published so far, once `done` holds for them (5 s at most).
    template <typename Done>
    std::vector<wire::Event> wait_for(Done done) {
        std::unique_lock<std::mutex> lock(mutex);
        std::vector<wire::Event> events;
        changed.wait_for(lock, std::chrono::seconds(5), [&] {
            events.clear();
            for (const auto& entry : published) {
                require(wire::is_wire(entry.second), entry.first + " was not published as SDAE");
                events.push_back(wire::decode(entry.second));
                require(entry.first == std::string("operation/") + kTarget + "/" + kOperation + "/" + events.back().kind,
                    entry.first + ": kind does not match the topic");
            }
            return done(events);
        });
        return events;
    }

    ControlHandler handler;
private:
    std::mutex mutex;
    std::condition_variable changed;
    std::map<std::string, EventCallback> subscribers;
    std::vector<std::pair<std::string, ByteBuffer>> published;
};

// Declares the typed fixtures' types: feedback Pose, result Result, state
// Float32Vector. The handler publishes the fixtures' feedback and state
// payloads, then waits for a cancel request and returns the result payload.
class Provider final : public Subsystem {
public:
    using Subsystem::Subsystem;
    std::string feedback_json;
    std::string state_json;
    std::string result_json;
private:
    bool on_initialize() override {
        OperationOptions options;
        options.request_type = "bytes";
        options.feedback_type = "msg.core.spatial.v1/Pose";
        options.result_type = "msg.core.common.result.v1/Result";
        options.state_type = "msg.core.common.primitive.v1/Float32Vector";
        return register_operation(kOperation, [this](OperationRequest, OperationContext& context) -> std::optional<std::string> {
            context.publish_feedback(feedback_json);
            context.publish_state("approaching", state_json);
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (!context.is_cancel_requested() && std::chrono::steady_clock::now() < deadline)
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            // Ignored after the cancel request, as in the Python SDK.
            context.publish_state("still_running", "null");
            return result_json;
        }, options);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

struct LogLines {
    std::mutex mutex;
    std::string text;
    bool contains(const std::string& needle) { std::lock_guard<std::mutex> lock(mutex); return text.find(needle) != std::string::npos; }
};

std::string reply_text(const ControlResponse& reply) { return std::string(reply.payload.begin(), reply.payload.end()); }

ControlRequest control_request(const std::string& topic, const std::string& body) {
    ControlRequest request;
    request.sender = "peer";
    request.topic = topic;
    request.payload.assign(body.begin(), body.end());
    return request;
}

void subsystem_publishes_and_subscribes_sdae() {
    const auto directory = r2kernel_test::unique_temp_path("operation_event_wire");
    fs::create_directories(directory);
    const auto manifest = directory / "provider.yaml";
    {
        std::ofstream file(manifest);
        file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << kTarget << "\n  role: component\n"
                "provides:\n  local:\n    contract: fixture.operation_event_wire.private.v1\n    visibility: internal\n"
                "    endpoints:\n      operations: [" << kOperation << "]\n";
        require(bool(file), "manifest fixture write failed");
    }
    auto client = std::make_shared<EventClient>();
    LogLines log;
    SubsystemOptions options;
    options.name = kTarget;
    options.manifest_path = manifest;
    options.schema_roots = std::vector<std::string>{};
    options.core_client = client;
    options.log_sink = [&log](std::string_view, std::string_view, std::string_view, std::string_view message) {
        std::lock_guard<std::mutex> lock(log.mutex);
        log.text += std::string(message) + "\n";
    };
    const auto feedback = fixture("operation-event-feedback-typed.json");
    const auto state = fixture("operation-event-state-typed.json");
    const auto result = fixture("operation-event-result-typed-message.json");
    const auto operation_id = feedback["input"]["operation_id"].as<std::string>();
    {
        Provider provider(std::move(options));
        provider.feedback_json = to_json(feedback["input"]["payload"]);
        provider.state_json = to_json(state["input"]["payload"]);
        provider.result_json = to_json(result["input"]["payload"]);
        provider.initialize();
        require(provider.state() == DeviceState::ready, "provider did not initialize: " + log.text);

        // Subscriber side: Python-encoded fixture events reach the callback.
        std::mutex received_mutex;
        std::map<std::string, OperationEventMessage> received;
        std::size_t delivered = 0;
        std::string current;
        const auto record = [&](const OperationEventMessage& message) {
            std::lock_guard<std::mutex> lock(received_mutex);
            received[current] = message;
            ++delivered;
        };
        require(provider.subscribe_operation_feedback(kTarget, kOperation, operation_id, record)
                && provider.subscribe_operation_result(kTarget, kOperation, operation_id, record)
                && provider.subscribe_operation_state(kTarget, kOperation, operation_id, record),
            "Operation subscriptions failed");
        const std::string prefix = std::string("operation/") + kTarget + "/" + kOperation + "/";
        // The typed StringValue fixture decodes only with a prepared codec for it;
        // this provider declares the other fixtures' types.
        for (const char* name : kFixtures) {
            if (std::string(name) == "operation-event-result-typed.json") continue;
            const auto document = fixture(name);
            const auto before = delivered;
            current = name;
            client->subscriber(prefix + document["input"]["kind"].as<std::string>())(from_hex(document["wire_hex"].as<std::string>()), 7);
            require(delivered == before + 1, std::string(name) + ": the subscriber did not deliver the Python event: " + log.text);
            const auto& message = received.at(name);
            const auto decoded = document["decoded"];
            require(message.operation_id == operation_id && message.kind == decoded["kind"].as<std::string>()
                    && message.timestamp_ns == decoded["ts"].as<std::int64_t>() && message.state == text_or_empty(decoded["state"])
                    && message.payload_type == text_or_empty(decoded["payload_type"]),
                std::string(name) + ": delivered message differs");
            if (!decoded["payload"].IsNull() && !document["decoded_payload_representation"])
                require(same_value(YAML::Load(message.payload_json), decoded["payload"]),
                    std::string(name) + ": delivered payload differs: " + message.payload_json);
        }
        require(received.at("operation-event-state-cancel-requested.json").cancel_requested
                && received.at("operation-event-state-cancel-requested.json").payload_json == "null",
            "state fixture lost cancel_requested");
        require(received.at("operation-event-feedback-typed.json").payload_encoding == OperationEventPayloadEncoding::typed,
            "typed feedback was not decoded with the prepared codec");
        const auto& raw = received.at("operation-event-feedback-raw.json");
        require(raw.payload_json == "\"0001feff\"" && raw.payload_encoding == OperationEventPayloadEncoding::raw
                && raw.payload_bytes == ByteBuffer({0x00, 0x01, 0xfe, 0xff}), "raw payload representation: " + raw.payload_json);

        // Another operation's event is filtered; a legacy JSON text event is still read.
        const auto feedback_topic = prefix + "feedback";
        const auto before_filtered = delivered;
        client->subscriber(feedback_topic)(wire::encode_json("feedback", "another", kOperation, kTarget, 8, "{}"), 8);
        require(delivered == before_filtered, "another operation's event passed the operation_id filter");
        const std::string legacy = "{\"operation_id\":\"" + operation_id + "\",\"kind\":\"feedback\",\"payload\":{\"x\":1},\"ts\":9}";
        current = "legacy";
        client->subscriber(feedback_topic)(ByteBuffer(legacy.begin(), legacy.end()), 9);
        require(delivered == before_filtered + 1 && received.at("legacy").payload_json == "{\"x\":1}"
                && received.at("legacy").timestamp_ns == 9 && received.at("legacy").payload_encoding == OperationEventPayloadEncoding::json,
            "legacy JSON event was not read");
        auto broken = from_hex(feedback["wire_hex"].as<std::string>());
        broken.pop_back();
        client->subscriber(feedback_topic)(broken, 10);
        require(delivered == before_filtered + 1 && log.contains("Failed to decode Operation event '" + feedback_topic + "'"),
            "a truncated event was not logged and dropped");

        // Provider side: events are published as the Python provider writes them.
        require(provider.start_operation_server() && client->handler, "Operation server did not start");
        const auto reply = reply_text(client->handler(control_request("operation.start",
            std::string("{\"operation\":\"") + kOperation + "\",\"request_id\":\"r1\",\"params\":{}}")));
        require(reply.find("\"accepted\":true") != std::string::npos, "Operation was not accepted: " + reply);
        const auto started_id = reply.substr(reply.find("\"operation_id\":\"") + 16, reply.find('"', reply.find("\"operation_id\":\"") + 16)
                                             - reply.find("\"operation_id\":\"") - 16);
        auto events = client->wait_for([](const std::vector<wire::Event>& seen) {
            for (const auto& event : seen) if (event.state == "approaching") return true;
            return false;
        });
        const auto payload_matches = [](const wire::Event& event, const YAML::Node& document) {
            return event.encoding == wire::PayloadEncoding::typed
                && event.payload == from_hex(document["layout"]["payload_hex"].as<std::string>())
                && event.payload_type == document["input"]["payload_type"].as<std::string>();
        };
        bool saw_feedback = false, saw_state = false;
        for (const auto& event : events) {
            require(event.target == kTarget && event.operation == kOperation && event.operation_id == started_id,
                event.kind + ": published identity differs");
            if (event.kind == "feedback") saw_feedback = payload_matches(event, feedback) && !event.has_state;
            if (event.kind == "state" && event.state == "approaching") saw_state = payload_matches(event, state) && event.has_state;
        }
        require(saw_feedback, "published feedback is not the Python typed payload");
        require(saw_state, "published state is not the Python typed payload");

        // A cancel request publishes cancel_requested (no payload, the state_type
        // as payload_type); the Operation then ends canceled with a null-payload
        // terminal state, followed by its typed result.
        const auto cancel_body = "{\"operation_id\":\"" + started_id + "\",\"request_id\":\"c1\"}";
        const auto cancel_reply = reply_text(client->handler(control_request("operation.cancel", cancel_body)));
        require(cancel_reply.find("\"ok\":true") != std::string::npos && cancel_reply.find("\"cancel_requested\":true") != std::string::npos,
            "cancel was not accepted: " + cancel_reply);
        events = client->wait_for([](const std::vector<wire::Event>& seen) {
            return !seen.empty() && seen.back().kind == "result";
        });
        std::vector<std::string> states;
        for (const auto& event : events) {
            if (event.kind != "state") continue;
            states.push_back(event.state);
            require(event.payload_type == state["input"]["payload_type"].as<std::string>(), "state payload_type differs");
            if (event.state == "cancel_requested" || event.state == "canceled") {
                require(event.encoding == wire::PayloadEncoding::none && event.cancel_requested,
                    event.state + " state carries a payload or lost cancel_requested");
            }
        }
        require(states.size() >= 3 && states[states.size() - 3] == "approaching" && states[states.size() - 2] == "cancel_requested"
                && states.back() == "canceled",
            "published state sequence differs (a state after cancel was not suppressed?)");
        require(events.back().kind == "result" && payload_matches(events.back(), result) && !events.back().has_state,
            "the typed result does not follow the terminal state");

        // A finished Operation keeps its state: no further cancel_requested event.
        const auto published = events.size();
        const auto late = reply_text(client->handler(control_request("operation.cancel", cancel_body)));
        require(late.find("\"ok\":true") != std::string::npos, "late cancel reply: " + late);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        require(client->wait_for([](const std::vector<wire::Event>&) { return true; }).size() == published,
            "a finished Operation published another event on cancel");
        provider.terminate();
    }
    std::error_code ignored;
    fs::remove_all(directory, ignored);
}
}  // namespace

int main() {
    try {
        encoder_writes_the_python_bytes();
        decoder_returns_the_python_message();
        malformed_wire_is_refused();
        subsystem_publishes_and_subscribes_sdae();
        std::cout << "operation event wire checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
