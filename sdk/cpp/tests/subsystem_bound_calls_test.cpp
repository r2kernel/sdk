// Bound calls (Python SDK transact_bound_control, call_bound_procedure,
// start_bound_operation; tests/test_runtime_binding_readiness.py): the
// runtime binding is resolved within the call's budget against a fake Control
// socket, then the call reaches the recording core client on that target;
// a binding failure or a spent budget never dispatches.
#include "subsystem_offline_fixture.hpp"
#include "fake_control_socket.hpp"

using namespace r2kernel::wrapper;
using namespace r2kernel_test;

namespace {

constexpr char kBound[] = R"({"ok":true,"res":"ok","binding":{"requirement":"service","source":"client.service",)"
                          R"("source_subsystem":"client","target":"server.service","contract":"tutorial.test.service.v1",)"
                          R"("required_contract":"tutorial.test.service.v1","provided_contract":"tutorial.test.service.v1",)"
                          R"("resolved_target_subsystem":"server","resolved_provider_interface":"service",)"
                          R"("state":"bound","runtime":true}})";
constexpr char kNotBound[] = R"({"ok":false,"res":"missing","source":"client.service","subsystem":"client",)"
                             R"("requirement":"service","reason":"not_bound"})";
constexpr char kResolveRequest[] = R"({"cmd":"binding_resolve","subsystem":"client","requirement":"service"})";

// Control answers binding_resolve with resolve_reply and anything else (the
// heartbeat) with an acknowledgement.
void answer(FakeControl& control, const std::string& resolve_reply) {
    control.respond([resolve_reply](const std::string& request) {
        return request == kResolveRequest ? resolve_reply : std::string(R"({"ok":true,"res":"ok"})");
    });
}

std::size_t resolve_requests(const FakeControl& control) {
    std::size_t count = 0;
    for (const auto& request : control.requests()) count += request == kResolveRequest ? 1 : 0;
    return count;
}

template <typename Call>
std::string failure(Call&& call) {
    try {
        call();
    } catch (const std::runtime_error& error) {
        return error.what();
    }
    throw std::runtime_error("bound call did not fail");
}

void bound_calls(FakeControl& control, ManifestFiles& files) {
    const auto manifest = files.write("client", "requires:\n  service:\n    contract: tutorial.test.service.v1\n"
                                                "    visibility: internal\n");
    auto client = std::make_shared<RecordingClient>();
    client->resolved_test_port = 41002;
    auto subsystem_options = options("client", manifest, client);
    subsystem_options.launch_bindings["service"] = "launch.service";
    Fixture subsystem(subsystem_options);

    answer(control, kBound);
    auto resolves = resolve_requests(control);
    const auto procedure = subsystem.call_bound_procedure("service", "read", R"({"x":1})", 500);
    require(procedure.has_value() && resolve_requests(control) == resolves + 1, "bound Procedure resolved once");
    require(client->last_payload.find(R"("target":"server")") != std::string::npos
                && client->last_payload.find(R"("procedure":"read")") != std::string::npos,
            "bound Procedure reached the runtime binding target");

    const auto operation = subsystem.start_bound_operation("service", "work", "{}", 500);
    require(operation.has_value() && client->last_payload.find(R"("target":"server")") != std::string::npos,
            "bound Operation start reached the runtime binding target");

    const int calls = client->normal_calls;
    const ByteBuffer payload {1, 2, 3};
    const auto response = subsystem.transact_bound_control("service", "servo", payload, 0.0F, 500.0F, 500);
    require(response.has_value() && response->payload == payload && client->normal_calls == calls + 1,
            "bound Control exchange dispatched once");

    // A binding that is not ready never falls back to the launch binding and
    // never dispatches.
    answer(control, kNotBound);
    const int before = client->normal_calls;
    require(failure([&] { (void)subsystem.call_bound_procedure("service", "read"); }).find("not ready") != std::string::npos,
            "unbound Procedure binding");
    require(failure([&] { (void)subsystem.start_bound_operation("service", "work"); }).find("not ready") != std::string::npos,
            "unbound Operation binding");
    require(failure([&] { (void)subsystem.transact_bound_control("service", "servo", payload, 0.0F, 500.0F); })
                .find("not ready") != std::string::npos,
            "unbound Control binding");
    require(client->normal_calls == before, "no dispatch after a binding failure");

    // The binding lookup spends the call's budget: a Control that stalls past
    // it fails the call without dispatch.
    resolves = resolve_requests(control);
    control.stall_next(300);
    const auto stalled = failure([&] { (void)subsystem.call_bound_procedure("service", "read", "null", 50); });
    require(stalled.find("deadline exceeded") != std::string::npos, ("stalled binding lookup: " + stalled).c_str());
    control.wait_stall_finished();
    require(client->normal_calls == before, "no dispatch after the budget is spent");
    require(resolve_requests(control) == resolves, "the stalled lookup was never answered");

    // An invalid budget is refused before the lookup.
    bool refused = false;
    try {
        (void)subsystem.call_bound_procedure("service", "read", "null", 0);
    } catch (const std::invalid_argument&) {
        refused = true;
    }
    require(refused && resolve_requests(control) == resolves, "invalid budget refused before lookup");
    require(failure([&] { (void)subsystem.call_bound_procedure("typo", "read"); }).find("does not declare") != std::string::npos,
            "undeclared requirement");
}

}  // namespace

int main() {
    ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
    ::setenv("R2KERNEL_CONTRACT_POLICY", "strict", 1);
    ::unsetenv("R2KERNEL_BINDINGS");
    ::unsetenv("R2KERNEL_DEPLOYMENT_MANIFEST");
    try {
        FakeControl control;
        ManifestFiles files;
        bound_calls(control, files);
    } catch (const std::exception& error) {
        std::cerr << "subsystem_bound_calls_test failed: " << error.what() << '\n';
        return 1;
    }
    std::cout << "subsystem_bound_calls_test passed\n";
    return 0;
}
