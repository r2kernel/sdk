// Cold control-socket requests of the core client TU against a fake control
// socket: requirement self-binding replies (interface binding_self_set/unset),
// JSON escaping of request fields, the Unix endpoint length rule and a refused
// Event inline peer verification inside publish(), and Control's admission
// refusals (reply, then close unread) being read and retried instead of
// failing the send with EPIPE.
#include "../src/core/client.cpp"
#include "../src/core/control_requests.hpp"
#include "fake_control_socket.hpp"

#include <iostream>
#include <sstream>

using namespace r2kernel::core;
using r2kernel::core::detail::parse_control_route;
using r2kernel::core::detail::request_self_binding_set;
using r2kernel::core::detail::request_self_binding_unset;
using r2kernel::wrapper::ControlRequestError;
using r2kernel_test::FakeControl;
using Clock = std::chrono::steady_clock;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

Clock::time_point budget(int milliseconds = 1000) { return Clock::now() + std::chrono::milliseconds(milliseconds); }

ClientOptions registered(const std::string& name) {
    ClientOptions options;
    options.subsystem_name = name;
    options.session_id = "session-1";
    return options;
}

void expect_refusal(FakeControl& control, const std::string& reply, const std::string& reason, const std::string& what) {
    control.respond([reply](const std::string&) { return reply; });
    try {
        (void)request_self_binding_set(registered("app"), "servo", "gw.servo", budget());
    } catch (const ControlRequestError& error) {
        require(error.reason() == reason && std::string(error.what()) == what,
            "unexpected refusal mapping: " + error.reason() + " / " + error.what());
        return;
    }
    throw std::runtime_error("refusal was not raised for " + reply);
}

// Control's resolve reply as the Python dict: quoted strings stay strings in
// the JSON maps, and a refused or incomplete reply is no route.
void control_route_reply_is_parsed() {
    const auto route = parse_control_route(R"({"res":"ok","subsystem":"gw","host":"127.0.0.1",)"
        R"("control_port":0,"route_kind":"local_control","control_relay_enabled":false,)"
        R"("peer_id":null,"session_id":"s-1","registration_revision":"5f0c2a9e41d84b7f9a31c6e2d0b8f4a7","codec_context_available":true,)"
        R"("controls":["servo"],"procedures":["echo"],"operations":[],)"
        R"("control_contracts":{"servo":{"request_type":"007"}},"codec_metadata":{}})");
    require(route && route->subsystem == "gw" && route->host == "127.0.0.1" && route->port == 0 &&
        route->route_kind == "local_control" && !route->relay_enabled && !route->peer_id && route->session_id == "s-1" &&
        route->registration_revision == std::optional<std::string>("5f0c2a9e41d84b7f9a31c6e2d0b8f4a7") && route->codec_context_available == true &&
        route->controls == std::vector<std::string>{"servo"} && route->procedures == std::vector<std::string>{"echo"} &&
        route->operations.empty() && route->control_contracts_json == R"({"servo":{"request_type":"007"}})" &&
        route->procedure_contracts_json == "{}" && route->json.find(R"("control_port":0)") != std::string::npos,
        "resolve reply was not parsed into a ControlRoute");
    require(!parse_control_route(R"({"res":"error","error":"unknown subsystem"})"), "refusal produced a route");
    require(!parse_control_route(R"({"res":"ok","host":"h"})"), "reply without a port produced a route");
}

void self_binding_replies(FakeControl& control) {
    const std::string row = "{\"error\":\"nested\",\"ok\":false,\"origin\":\"self\",\"source\":\"app.servo\","
                            "\"state\":\"bound\",\"target\":\"gw.servo\"}";
    control.respond([row](const std::string&) { return "{\"binding\":" + row + ",\"ok\":true,\"res\":\"ok\"}"; });
    require(request_self_binding_set(registered("app"), "servo\x01\"", "gw.servo", budget()) == row,
        "bound row was not returned verbatim");
    const auto request = control.requests().back();
    for (const char* field : {"\"cmd\":\"binding_self_set\"", "\"subsystem\":\"app\"", "\"session_id\":\"session-1\"",
                              "\"requirement\":\"servo\\u0001\\\"\"", "\"target\":\"gw.servo\""})
        require(request.find(field) != std::string::npos, std::string("self-binding request lacks ") + field + ": " + request);
    for (const unsigned char ch : request) require(ch >= 0x20, "raw control character in core JSON request");

    // A nested "reason"/"error" inside the evaluated row never replaces the top-level fields.
    for (const char* code : {"self_binding_disabled", "self_binding_source_ineligible", "self_binding_requirement_invalid",
                             "self_binding_operator_owned", "self_binding_remote_forbidden", "self_binding_target_held",
                             "self_binding_unmanaged_source", "self_binding_rejected",
                             "self_binding_target_authority_unknown", "self_binding_registration_changed"}) {
        expect_refusal(control,
            std::string("{\"binding\":{\"reason\":\"nested\",\"error\":\"nested\"},\"error\":\"refused text\",\"ok\":false,"
                        "\"reason\":\"") + code + "\",\"res\":\"error\"}",
            code, std::string(code) + ": refused text");
    }
    expect_refusal(control, "{\"status\":\"error\",\"error\":\"management denied\"}", "", "management denied");

    control.respond([](const std::string&) { return "{\"ok\":true,\"res\":\"ok\"}"; });
    bool malformed = false;
    try { (void)request_self_binding_set(registered("app"), "servo", "gw.servo", budget()); }
    catch (const ControlRequestError&) { throw std::runtime_error("missing row mapped to a refusal"); }
    catch (const std::runtime_error&) { malformed = true; }
    require(malformed, "success reply without a binding row must fail explicitly");

    control.respond([](const std::string&) { return "{\"ok\":true,\"res\":\"ok\",\"removed\":false}"; });
    require(!request_self_binding_unset(registered("app"), "servo", budget()), "removed:false was not returned");
    require(control.requests().back().find("\"cmd\":\"binding_self_unset\"") != std::string::npos, "unset command");
    control.respond([](const std::string&) { return "{\"ok\":true,\"res\":\"ok\",\"removed\":true}"; });
    require(request_self_binding_unset(registered("app"), "servo", budget()), "removed:true was not returned");

    control.stall_next(1500);
    const auto started = Clock::now();
    bool timed_out = false;
    try { (void)request_self_binding_set(registered("app"), "servo", "gw.servo", budget(200)); }
    catch (const ControlRequestError&) { throw std::runtime_error("deadline expiry mapped to a backend refusal"); }
    catch (const std::runtime_error&) { timed_out = true; }
    require(timed_out && Clock::now() - started < std::chrono::milliseconds(1000), "self-binding deadline not enforced");
    control.wait_stall_finished();

    // Without a deadline each socket operation is bounded by 1 s, as in the
    // Python SDK; an already expired deadline fails before any request.
    control.respond([](const std::string&) { return "{\"ok\":true,\"res\":\"ok\",\"removed\":true}"; });
    require(request_self_binding_unset(registered("app"), "servo", std::nullopt), "deadline-free unset failed");
    control.stall_next(2500);
    const auto unbounded_started = Clock::now();
    bool per_operation_timeout = false;
    try { (void)request_self_binding_unset(registered("app"), "servo", std::nullopt); }
    catch (const ControlRequestError&) { throw std::runtime_error("socket timeout mapped to a backend refusal"); }
    catch (const std::runtime_error&) { per_operation_timeout = true; }
    const auto unbounded_elapsed = Clock::now() - unbounded_started;
    require(per_operation_timeout && unbounded_elapsed >= std::chrono::milliseconds(900) &&
        unbounded_elapsed < std::chrono::milliseconds(2400), "deadline-free self-binding was not bounded by 1 s");
    control.wait_stall_finished();
    const auto expired_requests_before = control.requests().size();
    bool expired = false;
    try { (void)request_self_binding_unset(registered("app"), "servo", Clock::now() - std::chrono::milliseconds(1)); }
    catch (const std::runtime_error&) { expired = true; }
    require(expired && control.requests().size() == expired_requests_before, "expired self-binding deadline reached Control");

    // Parity with the Python SDK: a status error without text names the command,
    // a success needs res "ok", and blank arguments never reach the socket.
    expect_refusal(control, "{\"status\":\"error\"}", "", "binding_self_set returned an error without text");
    for (const char* reply : {"{\"binding\":{\"origin\":\"self\"},\"ok\":true}",
                              "{\"binding\":{\"origin\":\"self\"},\"ok\":true,\"res\":\"error\"}"}) {
        control.respond([reply](const std::string&) { return std::string(reply); });
        bool unexpected = false;
        try { (void)request_self_binding_set(registered("app"), "servo", "gw.servo", budget()); }
        catch (const ControlRequestError&) { throw std::runtime_error("unexpected success reply mapped to a refusal"); }
        catch (const std::runtime_error& error) {
            unexpected = std::string(error.what()).find("binding_self_set returned an unexpected response") == 0;
        }
        require(unexpected, std::string("a success reply without res ok must fail explicitly: ") + reply);
    }
    const auto requests_before = control.requests().size();
    const auto invalid = [&](const std::function<void()>& call, const std::string& message) {
        bool rejected = false;
        try { call(); } catch (const std::invalid_argument& error) { rejected = error.what() == message; }
        require(rejected, "blank self-binding argument was not rejected with: " + message);
    };
    invalid([&] { (void)request_self_binding_set(registered("app"), "", "gw.servo", budget()); },
        "binding_self_set: requirement must be a non-empty string");
    invalid([&] { (void)request_self_binding_set(registered("app"), "servo", " \t", budget()); },
        "binding_self_set: target must be a non-empty string");
    invalid([&] { (void)request_self_binding_unset(registered("app"), "  ", budget()); },
        "binding_self_unset: requirement must be a non-empty string");
    require(control.requests().size() == requests_before, "blank arguments must not reach the control socket");

    ClientOptions unregistered = registered("app");
    unregistered.session_id.clear();
    bool refused = false;
    try { (void)request_self_binding_set(unregistered, "servo", "gw.servo", budget()); } catch (const std::runtime_error&) { refused = true; }
    require(refused, "self-binding without a registration session must fail explicitly");
}

void unix_endpoint_length_rule() {
    const std::string too_long(sizeof(sockaddr_un::sun_path), 'a');
    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    require(fd >= 0, "socket");
    bool rejected = false;
    try { reject_unix_endpoint(fd, "Event inline", too_long); }
    catch (const std::invalid_argument& error) {
        rejected = std::string(error.what()).find("Event inline endpoint exceeds the Unix socket path limit (107 bytes)") == 0;
    }
    require(rejected, "an endpoint longer than sun_path must be rejected with an explicit error");
    require(::fcntl(fd, F_GETFD) < 0 && errno == EBADF, "the rejected connection descriptor must be closed");
}

// An Event inline endpoint that cannot be connected is refused at registration
// (inline delivery disabled, explicit reason), never later inside a publish.
void event_inline_endpoint_checked_at_registration() {
    using r2kernel::core::detail::event_inline_endpoint_refusal;
    using r2kernel::core::detail::registered_event_inline_endpoint;
    const std::string fits(sizeof(sockaddr_un::sun_path) - 1, 'a');
    require(registered_event_inline_endpoint(fits) == fits && event_inline_endpoint_refusal(fits).empty(),
        "an endpoint that fits sun_path must be kept");
    const std::string too_long(sizeof(sockaddr_un::sun_path), 'a');
    require(registered_event_inline_endpoint(too_long).empty(),
        "an endpoint longer than sun_path must disable inline delivery");
    require(event_inline_endpoint_refusal(too_long).find("Event inline delivery disabled: the registered endpoint "
                                                         "exceeds the Unix socket path limit (107 bytes)") == 0,
        "refusal must name the endpoint limit");
}

// Event receive buffers grow once to the lengths Core reports with
// BufferTooSmall (it keeps the Event); a report without a larger length is an
// explicit failure, never a growth loop.
void event_receive_buffers_grow_to_reported_lengths() {
    using r2kernel::core::detail::EventReceiveSizes;
    using r2kernel::core::detail::grown_event_receive_sizes;
    const auto grown = grown_event_receive_sizes({256, 256, 1024}, {300, 12, 4096});
    require(grown && grown->topic == 300 && grown->publisher == 256 && grown->payload == 4096,
        "buffers must be sized exactly to the reported lengths, never shrunk");
    require(!grown_event_receive_sizes(*grown, {300, 12, 4096}),
        "a report that fits the buffers must not count as growth");
    require(!grown_event_receive_sizes({256, 256, 1024}, {0, 0, 0}),
        "a BufferTooSmall without reported lengths (older core) must not count as growth");
}

// The Event inline connection's Control peer verification runs after
// publish() marked the slot ready. A refusal must not leave publish() by
// exception: publish() reports it, succeeds, and posts the
// stamped_stream_runtime lock as the non-inline path does.
// The refusal also disables inline delivery for this registration, so
// later publishes neither retry the peer proof nor log again.
void inline_publish_failure_posts_the_lock(FakeControl& control) {
    const std::string tag = std::to_string(::getpid());
    const std::string pool = "/r2k-cpp-test-inline-pool-" + tag;
    const std::string lock_name = "/r2k-cpp-test-inline-lock-" + tag;
    R2KernelShmHandle* shm = nullptr;
    require(r2kernel_shm_create(pool.c_str(), 4096, 4, &shm) == 0 && shm != nullptr, "shared-memory pool");
    R2KernelLockHandle* lock = nullptr;
    require(r2kernel_lock_create(lock_name.c_str(), &lock) == 0 && lock != nullptr, "runtime lock");
    // A listening inline endpoint: connect succeeds, the peer proof then fails.
    const std::string inline_path = r2kernel_test::unique_temp_path("inline").string() + ".sock";
    const int listener = ::socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, inline_path.c_str(), inline_path.size() + 1);
    require(listener >= 0 && ::bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0
            && ::listen(listener, 8) == 0, "inline endpoint listener");
    control.respond([&](const std::string& request) -> std::string {
        if (request.find("\"cmd\":\"register_subsystem\"") != std::string::npos)
            return "{\"res\":\"ok\",\"ok\":true,\"topic_buffer_pool_name\":\"" + pool + "\","
                   "\"topic_buffer_pool_slot_size\":4096,\"topic_buffer_pool_slot_count\":4,\"manager_peer_auth_required\":false,"
                   "\"control_local_endpoint\":\"/tmp/r2k-cpp-test-unused.sock\","
                   "\"control_local_payload_policy\":\"forwarded_shm_v5\","
                   "\"control_local_protocol\":\"binary_shm_v5\",\"control_shm_min_region_bytes\":4096,"
                   "\"control_shared_buffer_slot_size\":16777216,\"control_shm_max_channels\":128,"
                   "\"control_shm_max_owned_bytes\":67108864,\"control_shm_max_mapped_bytes\":67108864,"
                   "\"event_inline_endpoint\":\"" + inline_path + "\",\"event_inline_payload_threshold\":1024}";
        if (request.find("\"cmd\":\"request_publish_slots\"") != std::string::npos)
            return "{\"res\":\"ok\",\"ok\":true,\"publish_slot_index\":0,\"queue_depth\":1,\"entry_slot_count\":1,"
                   "\"subscribers\":[{\"subscriber\":\"stamped_stream_runtime\",\"lock_name\":\"" + lock_name + "\"}]}";
        return "{\"res\":\"ok\",\"ok\":true}";
    });
    {
        ClientOptions options;
        options.subsystem_name = "inline_pub";
        R2KernelClient client(options);
        r2kernel::wrapper::EventPublishSpec spec;
        spec.topic = "inline/topic";
        spec.size = 64;
        require(client.register_event_publish(spec), "publish registration");
        // Peer verification required, with no reachable resource broker.
        const std::string broker = r2kernel_test::unique_temp_path("no-broker").string() + ".sock";
        ::setenv("R2KERNEL_RESOURCE_SERVICE_SOCKET", broker.c_str(), 1);
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "required", 1);
        std::ostringstream captured;
        auto* const previous = std::cerr.rdbuf(captured.rdbuf());
        std::string thrown;
        int published = 0;
        try {
            for (int publish = 0; publish < 3; ++publish)
                published += client.publish_event(spec.topic, r2kernel::wrapper::EventPayload(16, 0x5a), 0, 0) ? 1 : 0;
        } catch (const std::exception& error) {
            thrown = error.what();
        }
        std::cerr.rdbuf(previous);
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
        ::unsetenv("R2KERNEL_RESOURCE_SERVICE_SOCKET");
        require(thrown.empty(), "inline publish failure escaped publish(): " + thrown);
        require(published == 3, "every publish must succeed through the lock notification");
        for (int post = 0; post < 3; ++post)
            require(r2kernel_lock_trywait(lock) == 0, "the stamped_stream_runtime lock was not posted for every publish");
        require(r2kernel_lock_trywait(lock) != 0, "the lock must be posted exactly once per publish");
        require(::fcntl(listener, F_SETFL, O_NONBLOCK) == 0, "non-blocking listener");
        int connections = 0;
        for (int accepted; (accepted = ::accept(listener, nullptr, nullptr)) >= 0; ++connections) ::close(accepted);
        require(connections == 1, "a refused inline endpoint was contacted again: " + std::to_string(connections)
            + " connections");
        const std::string log = captured.str();
        std::size_t reports = 0;
        for (auto at = log.find("Event inline"); at != std::string::npos; at = log.find("Event inline", at + 1)) ++reports;
        require(reports == 1 && log.find("Event inline delivery disabled for endpoint " + inline_path
                                         + " until the subsystem registers again") == 0,
            "the refusal must be reported exactly once: " + log);
    }
    ::close(listener);
    ::unlink(inline_path.c_str());
    r2kernel_lock_close(lock);
    r2kernel_lock_unlink(lock_name.c_str());
    r2kernel_shm_close(shm);
    r2kernel_shm_unlink(pool.c_str());
}
void admission_refusals_are_read_and_retried(FakeControl& control) {
    const std::string refusal = "{\"status\":\"error\",\"error\":\"Control Unix socket refused the connection: "
        "per-UID connection limit reached; retry later\",\"error_code\":\"per_uid_connection_limit\",\"retryable\":true}";
    const std::string ok = "{\"res\":\"ok\",\"ok\":true}";
    control.respond([ok](const std::string&) { return ok; });
    // A small request is usually sent before the close (the read then resets); a large
    // one is still being sent when the refused connection closes (EPIPE).
    for (const std::size_t size : {std::size_t {64}, std::size_t {512 * 1024}}) {
        const int before = control.refused();
        control.refuse_next(3, refusal);
        const auto reply = exchange_control_json(
            "{\"cmd\":\"clock_status\",\"pad\":\"" + std::string(size, 'x') + "\"}", "", 0, 1000);
        require(reply == ok, "a refused request is admitted on retry, got " + reply.substr(0, 120));
        require(control.refused() - before == 3, "every refusal is retried");
    }
    control.refuse_next(1000000, refusal);
    const auto started = Clock::now();
    const auto reply = exchange_control_json("{\"cmd\":\"clock_status\"}", "", 0, 200);
    require(is_retryable_admission_refusal(reply), "a refusal outlasting the budget is returned, got " + reply);
    require(Clock::now() - started < std::chrono::seconds(1), "refusal retries stay within the request budget");
    control.refuse_next(0, {});
}
}  // namespace

int main() {
    try {
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
        FakeControl control;
        control_route_reply_is_parsed();
        self_binding_replies(control);
        unix_endpoint_length_rule();
        event_inline_endpoint_checked_at_registration();
        event_receive_buffers_grow_to_reported_lengths();
        inline_publish_failure_posts_the_lock(control);
        admission_refusals_are_read_and_retried(control);
        std::cout << "core control request checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
