// Offline checks of cold SDK control-plane paths against a fake control socket:
// unprepared codec fallback, bounded best-effort notifications,
// requirement self-binding replies, JSON escaping, the timing_support rule and
// the late_policy rule.
#include "r2kernel/wrapper/subsystem.hpp"
#include "../src/wrapper/contract_loader.hpp"
#include "fake_control_socket.hpp"
#include "offline_core_client.hpp"
#include "r2kernel_core.h"

#include <yaml-cpp/yaml.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

using namespace r2kernel::wrapper;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using r2kernel_test::FakeControl;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

fs::path unique_path(const std::string& tag) { return r2kernel_test::unique_temp_path(tag); }

#ifdef R2KERNEL_SDK_WIRE_DIR
constexpr std::size_t kFixtureCalls = 1;
// The `wire` text of a golden Control fixture (sdk/testdata/wire), as Control sends it.
std::string wire_fixture(const std::string& name) {
    return YAML::LoadFile(std::string(R2KERNEL_SDK_WIRE_DIR) + "/" + name)["wire"].as<std::string>();
}

std::string replace_once(std::string text, const std::string& from, const std::string& to) {
    const auto position = text.find(from);
    require(position != std::string::npos, "fixture text has no " + from);
    return text.replace(position, from.size(), to);
}
#else
constexpr std::size_t kFixtureCalls = 0;
#endif

struct LogRecord {
    std::mutex mutex;
    std::vector<std::string> warnings;
    void add(std::string_view level, std::string_view message) {
        if (level != "WARN") return;
        std::lock_guard<std::mutex> lock(mutex);
        warnings.emplace_back(message);
    }
    std::size_t count_containing(const std::string& needle) {
        std::lock_guard<std::mutex> lock(mutex);
        std::size_t count = 0;
        for (const auto& warning : warnings)
            if (warning.find(needle) != std::string::npos) ++count;
        return count;
    }
};

class Plain final : public Subsystem {
public:
    using Subsystem::Subsystem;
    bool start_result = true;
private:
    bool on_initialize() override { return true; }
    bool on_start() override {
        if (!start_result) logger().warning("Fixture", "servo link unavailable");
        return start_result;
    }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
    bool on_pause() override {
        logger().warning("Fixture", "pause interlock engaged");
        return false;
    }
};

fs::path write_manifest(const std::string& name) {
    struct Directory {
        fs::path path = unique_path("manifests");
        Directory() { fs::create_directories(path); }
        ~Directory() { std::error_code ignored; fs::remove_all(path, ignored); }
    };
    static const Directory root;
    const auto path = root.path / (name + ".yaml");
    std::ofstream file(path);
    file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << name << "\n  role: component\n";
    require(bool(file), "manifest fixture write failed");
    return path;
}

SubsystemOptions plain_options(const std::string& name, LogRecord& log, std::shared_ptr<CoreClient> client = {}) {
    SubsystemOptions options;
    options.name = name;
    options.manifest_path = write_manifest(name);
    options.schema_roots = std::vector<std::string>{};
    options.core_client = std::move(client);
    options.log_sink = [&log](std::string_view level, std::string_view, std::string_view, std::string_view message) {
        log.add(level, message);
    };
    return options;
}

ByteBuffer two_call_encode(const std::string& reference, const std::string& json) {
    std::size_t length = 0;
    const auto* input = reinterpret_cast<const std::uint8_t*>(json.data());
    require(r2kernel_codec_encode_message_json(reference.c_str(), input, json.size(), nullptr, 0, &length) == 0,
        "two-call size query");
    ByteBuffer out(length);
    require(r2kernel_codec_encode_message_json(reference.c_str(), input, json.size(), out.data(), out.size(), &length) == 0,
        "two-call fill");
    out.resize(length);
    return out;
}

void unprepared_codec_fallback() {
    const auto root = unique_path("spec");
    struct Cleanup { fs::path root; ~Cleanup() { std::error_code ignored; fs::remove_all(root, ignored); } } cleanup{root};
    const auto path = root / "messages/msg/test/fallback/v1.yaml";
    fs::create_directories(path.parent_path());
    const auto write = [&](const char* type) {
        std::ofstream stream(path);
        stream << "version: 1\nid: msg.test.fallback.v1\nkind: message_set\nmessages:\n  Value:\n    fields:\n"
                  "      - name: value\n        type: " << type << '\n';
    };
    write("uint32");
    ::setenv("R2KERNEL_SPEC_ROOT", root.c_str(), 1);
    LogRecord log;
    Plain subsystem(plain_options("fixture.fallback", log));
    const std::string reference = "msg.test.fallback.v1/Value";
    const auto encoded = subsystem.encode_message_json(reference, "{\"value\":7}");
    require(encoded == two_call_encode(reference, "{\"value\":7}"), "fallback encode differs from the two-call ABI");
    require(subsystem.decode_message_json(reference, encoded) == "{\"value\":7}", "fallback decode round trip");
    write("bool");
    const auto edited = subsystem.encode_message_json(reference, "{\"value\":true}");
    require(edited == two_call_encode(reference, "{\"value\":true}") && edited != encoded,
        "fallback did not re-read the edited spec root on the next call");
    bool rejected = false;
    try { (void)subsystem.encode_message_json(reference, "{\"value\":"); } catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "malformed fallback input was not rejected");
    require(log.count_containing("unprepared codec path re-reads spec roots per call") == 1,
        "fallback warning must be logged exactly once");
    ::unsetenv("R2KERNEL_SPEC_ROOT");
}

void bounded_best_effort_notification(FakeControl& control) {
    LogRecord log;
    Plain subsystem(plain_options("fixture.notify", log));
    control.respond({});
    FrameTransform transform;
    transform.parent_frame = "world";
    transform.child_frame = "tool\t\x01";
    transform.observed_at_sync_ns = 1;
    subsystem.set_frame_transform(transform);
    const auto requests = control.requests();
    require(!requests.empty(), "frame transform was not delivered");
    const auto& request = requests.back();
    require(request.find("\"child_frame\":\"tool\\t\\u0001\"") != std::string::npos,
        "wrapper JSON escaping is not valid JSON: " + request);
    for (const unsigned char ch : request) require(ch >= 0x20, "raw control character in wrapper JSON");

    control.stall_next(1500);
    transform.child_frame = "tool";
    const auto started = Clock::now();
    subsystem.set_frame_transform(transform);
    const auto elapsed = Clock::now() - started;
    require(elapsed < std::chrono::milliseconds(1000), "best-effort notification blocked on a stalled backend");
    require(log.count_containing("frame transform update was not delivered") == 1,
        "stalled notification must be reported explicitly");
    control.wait_stall_finished();
}

void self_binding_requires_native_client() {
    LogRecord log;
    Plain unmanaged(plain_options("plain", log));
    bool refused = false;
    try { (void)unmanaged.bind_requirement("servo", "gw.servo"); }
    catch (const std::logic_error& error) {
        refused = std::string(error.what()) == "bind_requirement requires a registered subsystem: no core client is "
                                               "bound yet (call initialize() first)";
    }
    require(refused, "self-binding before a core client exists must name the missing initialization");
    Plain offline(plain_options("offline", log, std::make_shared<r2kernel_test::OfflineCoreClient>()));
    refused = false;
    try { (void)offline.bind_requirement("servo", "gw.servo"); }
    catch (const std::logic_error& error) {
        refused = std::string(error.what()).find("requires the native r2kernel::core::R2KernelClient") != std::string::npos;
    }
    require(refused, "self-binding without the native client must fail explicitly");
    refused = false;
    try { (void)unmanaged.unbind_requirement("servo"); } catch (const std::logic_error&) { refused = true; }
    require(refused, "self-unbinding without the native client must fail explicitly");
}

void start_hook_false_is_reported() {
    LogRecord log;
    std::vector<std::string> errors;
    std::mutex errors_mutex;
    auto options = plain_options("fixture.start", log, std::make_shared<r2kernel_test::OfflineCoreClient>());
    options.log_sink = [&](std::string_view level, std::string_view, std::string_view, std::string_view message) {
        std::lock_guard<std::mutex> lock(errors_mutex);
        if (level == "ERROR") errors.emplace_back(message);
    };
    Plain subsystem(std::move(options));
    subsystem.start_result = false;
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "start fixture did not initialize");
    subsystem.start();
    require(subsystem.state() == DeviceState::error, "a false start hook must enter Error");
    bool reported = false;
    {
        std::lock_guard<std::mutex> lock(errors_mutex);
        for (const auto& error : errors)
            reported = reported || error == "start failed: start hook returned false (no exception); "
                                            "last logged problem: Fixture: servo link unavailable";
    }
    subsystem.terminate();
    require(reported, "false start hook was not reported with the last logged problem");
}

// A refused lifecycle transition (pause/resume/stop/fault/degrade, and reset
// through request_lifecycle_transition) names the last problem its hook logged.
void refused_transition_is_reported() {
    LogRecord log;
    Plain subsystem(plain_options("fixture.pause", log, std::make_shared<r2kernel_test::OfflineCoreClient>()));
    subsystem.initialize();
    subsystem.start();
    require(subsystem.state() == DeviceState::operating, "pause fixture did not start");
    require(!subsystem.pause(), "a false pause hook must refuse the transition");
    require(subsystem.state() == DeviceState::operating, "a refused pause must restore the previous state");
    require(log.count_containing("Lifecycle transition pause was not applied; "
                                 "last logged problem: Fixture: pause interlock engaged") == 1,
        "refused pause was not reported with the last logged problem");
    subsystem.terminate();
}

void contract_timing_support_rule() {
    const auto root = unique_path("api");
    struct Cleanup { fs::path root; ~Cleanup() { std::error_code ignored; fs::remove_all(root, ignored); } } cleanup{root};
    const auto path = root / "apis/api/test/timing/v1.yaml";
    fs::create_directories(path.parent_path());
    { std::ofstream stream(path); stream << "id: api.test.timing.v1\ncontrols:\n  - topic: move\n    timing_support: sometimes\n"; }
    contract_loader::Inventory inventory(std::vector<fs::path>{root});
    bool rejected = false;
    try { (void)inventory.api("api.test.timing.v1"); }
    catch (const std::invalid_argument& error) {
        rejected = std::string(error.what()).find("timing_support must be one of") != std::string::npos
            && std::string(error.what()).find("api.test.timing.v1") != std::string::npos;
    }
    require(rejected, "unknown contract timing_support must be rejected explicitly");
    { std::ofstream stream(path); stream << "id: api.test.timing.v1\ncontrols:\n  - topic: move\n    timing_support: ' optional '\n"; }
    contract_loader::Inventory accepted(std::vector<fs::path>{root});
    require(accepted.api("api.test.timing.v1")->controls.at(0).timing_support == "optional", "valid timing_support");
}
// A late policy is exactly drop, reject or execute_immediately. An
// unknown name is refused before anything is sent, and a received unknown wire
// code is refused before the endpoint handler runs; neither becomes
// execute_immediately.
class LatePolicyEndpoint final : public Subsystem {
public:
    using Subsystem::Subsystem;
    std::vector<std::string> received;
private:
    bool on_initialize() override {
        // Raw byte types: the fixture checks timing, not payload codecs.
        ControlEndpointOptions options;
        options.request_type = "bytes";
        options.response_type = "bytes";
        options.native_payload = true;
        options.timing_support = "optional";
        OperationOptions operation_options;
        operation_options.request_type = "bytes";
        operation_options.feedback_type = "bytes";
        operation_options.result_type = "bytes";
        operation_options.timing_support = "optional";
        if (!register_operation("move", [](OperationRequest, OperationContext&) -> std::optional<std::string> {
                return std::string("{}");
            }, operation_options)) {
            return false;
        }
        return register_control_endpoint("servo", [this](const ControlEndpointRequest& request) {
            received.push_back(request.timing.late_policy);
            return ControlEndpointResponse{};
        }, options);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

ByteBuffer timed_wire(std::uint8_t late_policy_code) {
    const ByteBuffer payload{'{', '}'};
    std::size_t length = 0;
    const auto encode = [&](std::uint8_t* out, std::size_t capacity) {
        return r2kernel_codec_encode_control_request_v2(7, 10, 10, 20, 0, 5, 3, 0,
            payload.data(), payload.size(), out, capacity, &length);
    };
    require(encode(nullptr, 0) == 0, "timed wire size query");
    ByteBuffer wire(length);
    require(encode(wire.data(), wire.size()) == 0, "timed wire encode");
    // Core refuses to encode an unknown code, so a peer's frame is forged at byte 6.
    wire[6] = late_policy_code;
    return wire;
}

void late_policy_rule() {
    LogRecord log;
    auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
    auto options = plain_options("fixture.late_policy", log, client);
    {
        std::ofstream manifest(options.manifest_path, std::ios::app);
        manifest << "provides:\n  local:\n    contract: fixture.late_policy.private.v1\n    visibility: internal\n"
                    "    endpoints:\n      controls: [servo]\n      operations: [move]\n";
    }
    LatePolicyEndpoint subsystem(std::move(options));
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "late_policy fixture did not initialize");

    ControlTransactOptions transact;
    transact.timing = ControlTimingOptions{};
    for (const std::string policy : {"bogus", "", " drop", "DROP"}) {
        transact.timing->late_policy = policy;
        bool refused = false;
        try { (void)subsystem.transact_control("peer", "servo", ByteBuffer{'{', '}'}, transact); }
        catch (const std::invalid_argument& error) {
            refused = std::string(error.what()).find("late_policy must be one of drop, reject, execute_immediately")
                == 0;
        }
        require(refused, "unknown late_policy '" + policy + "' was not refused");
    }
    require(client->transacts == 0, "a request with an unknown late_policy reached the transport");
    const std::uint8_t codes[] = {0, 1, 2};
    const char* names[] = {"drop", "reject", "execute_immediately"};
    for (std::size_t index = 0; index < 3; ++index) {
        transact.timing->late_policy = names[index];
        (void)subsystem.transact_control("peer", "servo", ByteBuffer{'{', '}'}, transact);
        require(client->last_payload.size() > 6 && client->last_payload[6] == codes[index],
            std::string("late_policy wire code for ") + names[index]);
    }

    require(subsystem.start_control_server() && client->handler, "dispatch server was not started");
    const auto dispatch = [&](std::uint8_t code) {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "servo";
        request.payload = timed_wire(code);
        return client->handler(std::move(request));
    };
    const auto refused = dispatch(3);
    require(refused.status != 0 && subsystem.received.empty(), "an unknown late_policy wire code reached the handler");
    (void)dispatch(1);
    require(subsystem.received == std::vector<std::string>{"reject"}, "a valid late_policy wire code was not delivered");

    // The JSON operation.start form carries the policy as text; an unknown one is refused, not passed on.
    require(subsystem.start_operation_server(), "operation server was not started");
    const auto start_operation = [&](const std::string& policy) {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "operation.start";
        const std::string body = "{\"operation\":\"move\",\"request_id\":\"r1\",\"params\":{},\"timed\":true,"
            "\"timed_late_policy\":\"" + policy + "\"}";
        request.payload.assign(body.begin(), body.end());
        const auto response = client->handler(std::move(request));
        return std::string(response.payload.begin(), response.payload.end());
    };
    const auto refused_start = start_operation("bogus");
    require(refused_start.find("\"error_code\":\"invalid_argument\"") != std::string::npos
            && refused_start.find("late_policy must be one of") != std::string::npos,
        "an unknown operation.start late_policy was not refused: " + refused_start);
    const auto accepted_start = start_operation("drop");
    require(accepted_start.find("\"accepted\":true") != std::string::npos,
        "a valid operation.start late_policy was not accepted: " + accepted_start);
    // The JSON start names the Operation in `operation`. Unknown keys are ignored, so `action` names nothing.
    {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "operation.start";
        const std::string body = "{\"kind\":\"operation_start_request\",\"action\":\"move\",\"request_id\":\"a1\",\"params\":{}}";
        request.payload.assign(body.begin(), body.end());
        const auto response = client->handler(std::move(request));
        const std::string text(response.payload.begin(), response.payload.end());
        require(text.find("\"error_code\":\"invalid_argument\"") != std::string::npos
                && text.find("missing operation name") != std::string::npos
                && text.find("\"accepted\":false") != std::string::npos,
            "an operation.start naming its Operation in `action` was not refused: " + text);
    }
#ifdef R2KERNEL_SDK_WIRE_DIR
    // Control's proxied start (sorted keys): `params` reuses the request's member
    // names and comes first; only the top-level members are the request's.
    {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "operation.start";
        const std::string body = replace_once(wire_fixture("operation-start-request.json"),
            "\"operation\":\"run\"", "\"operation\":\"move\"");
        request.payload.assign(body.begin(), body.end());
        const auto response = client->handler(std::move(request));
        const std::string text(response.payload.begin(), response.payload.end());
        require(text.find("\"accepted\":true") != std::string::npos
                && text.find("\"request_id\":\"req-2\"") != std::string::npos,
            "the proxied operation.start fixture was read from its params: " + text);
    }
#endif
    subsystem.terminate();
}
// Procedure and Operation servers start without a bind_control() call: the
// handler is served locally and Control assigns the route port (port 0).
void dispatch_server_starts_without_an_explicit_bind() {
    for (const bool operations : {false, true}) {
        LogRecord log;
        auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
        client->listen_port = 0;
        Plain subsystem(plain_options(operations ? "fixture.unbound_operations" : "fixture.unbound_procedures", log, client));
        subsystem.initialize();
        require(subsystem.state() == DeviceState::ready, "unbound dispatch fixture did not initialize");
        const bool started = operations ? subsystem.start_operation_server() : subsystem.start_procedure_server();
        require(started && client->handler, "dispatch server did not start without an explicit bind");
        require(client->bind_calls == 1 && client->bound_port == 0, "dispatch server did not ask Control for the route port");
        subsystem.terminate();
    }
}

class TypedProcedureEndpoint final : public Subsystem {
public:
    using Subsystem::Subsystem;
    int mode = 0;
    std::size_t calls = 0;
    ProcedureRequest last_request;
private:
    bool on_initialize() override {
        ProcedureOptions options;
        options.request_type = "msg.core.common.primitive.v1/Int32Value";
        options.response_type = options.request_type;
        options.timing_support = "optional";
        return register_procedure("echo", [this](const ProcedureRequest& request) {
            ++calls;
            last_request = request;
            if (mode == 2) throw std::runtime_error("servo fault");
            if (mode == 4) throw 7;
            ProcedureResponse response;
            response.ok = mode == 0;
            response.result_json = (mode == 0 || mode == 5) ? request.params_json : "null";
            if (mode != 0) {
                response.error_code = "interlock";
                response.error_message = "servo fault";
            }
            if (mode == 3) {
                response.ok = true;
                response.status_code = 9;
            }
            return response;
        }, options);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

void typed_procedure_error_reaches_public_consumer() {
    LogRecord log;
    auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
    client->loopback = true;
    auto options = plain_options("fixture.typed_procedure", log, client);
    {
        std::ofstream manifest(options.manifest_path, std::ios::app);
        manifest << "provides:\n  local:\n    contract: fixture.typed_procedure.private.v1\n    visibility: internal\n"
                    "    endpoints:\n      procedures: [echo]\n";
    }
    TypedProcedureEndpoint subsystem(std::move(options));
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "typed Procedure fixture did not initialize");
    require(subsystem.start_procedure_server(), "typed Procedure server was not started");
    ProcedureCallOptions call;
    call.request_type = "msg.core.common.primitive.v1/Int32Value";
    call.response_type = call.request_type;
    auto reply = subsystem.call_procedure("peer", "echo", "{\"value\":7}", call);
    require(reply && reply->ok && reply->result_json == "{\"value\":7}", "typed success did not round trip");
    // The JSON call names the Procedure in `procedure`. Unknown keys are ignored, so `method` names nothing.
    {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "procedure.call";
        const std::string body = "{\"kind\":\"procedure_request\",\"method\":\"echo\",\"request_id\":\"m1\",\"params\":{\"value\":7}}";
        request.payload.assign(body.begin(), body.end());
        const auto response = client->handler(std::move(request));
        const std::string text(response.payload.begin(), response.payload.end());
        require(text.find("\"error_code\":\"invalid_argument\"") != std::string::npos
                && text.find("missing procedure name") != std::string::npos && subsystem.calls == 1,
            "a procedure.call naming its Procedure in `method` was not refused: " + text);
    }
#ifdef R2KERNEL_SDK_WIRE_DIR
    // Control's proxied call (sorted keys): `params` reuses the request's member
    // names and comes first; only the top-level members are the request's.
    {
        ControlRequest request;
        request.sender = "peer";
        request.topic = "procedure.call";
        const std::string body = wire_fixture("procedure-request.json");
        request.payload.assign(body.begin(), body.end());
        const auto response = client->handler(std::move(request));
        const std::string text(response.payload.begin(), response.payload.end());
        require(subsystem.calls == 2 && subsystem.last_request.procedure == "echo"
                && subsystem.last_request.request_id == "req-1" && subsystem.last_request.target == "component.wire_fixture"
                && subsystem.last_request.timeout_ms == 250
                && subsystem.last_request.params_json.find("\"request_id\":\"inner\"") != std::string::npos
                && text.find("\"request_id\":\"req-1\"") != std::string::npos,
            "the proxied procedure.call fixture was read from its params: " + text);
    }
#endif
    for (const int mode : {1, 2, 3, 4}) {
        subsystem.mode = mode;
        reply = subsystem.call_procedure("peer", "echo", "{\"value\":7}", call);
        require(reply && !reply->ok && reply->error_code == "procedure_transport_failed"
                && reply->error_message.find(mode == 4 ? "unknown exception" : "servo fault") != std::string::npos,
            "typed handler failure lost its diagnostic or was decoded as success");
    }
    require(subsystem.calls == kFixtureCalls + 5, "typed consumer did not invoke the real provider handler");
    subsystem.mode = 0;
    call.timing = ControlTimingOptions{};
    reply = subsystem.call_procedure("peer", "echo", "{\"value\":7}", call);
    require(reply && reply->ok && reply->status_code.value_or(1) == 0,
        "timed typed success did not preserve response metadata");
    subsystem.mode = 5;
    reply = subsystem.call_procedure("peer", "echo", "{\"value\":7}", call);
    require(reply && !reply->ok && reply->status_code.value_or(0) != 0
            && reply->error_code == "procedure_response_error",
        "timed typed failure did not preserve response status");
    subsystem.stop_procedure_server();
    call.timing.reset();
    reply = subsystem.call_procedure("peer", "echo", "{\"value\":7}", call);
    require(reply && !reply->ok && reply->error_message.find("not running") != std::string::npos,
        "stopped typed Procedure reached the handler");
    require(subsystem.calls == kFixtureCalls + 7, "stop admitted a new Procedure handler");
    subsystem.terminate();
}
}  // namespace

int main() {
    try {
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
        unprepared_codec_fallback();
        FakeControl control;
        bounded_best_effort_notification(control);
        self_binding_requires_native_client();
        contract_timing_support_rule();
        start_hook_false_is_reported();
        refused_transition_is_reported();
        late_policy_rule();
        dispatch_server_starts_without_an_explicit_bind();
        typed_procedure_error_reaches_public_consumer();
        std::cout << "subsystem control-plane checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
