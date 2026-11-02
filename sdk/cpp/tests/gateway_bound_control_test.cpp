// GatewaySubsystem bound Control sources against a fake Control socket (same
// scenarios as the Python SDK's tests/test_gateway_bound_owner.py).
#include "r2kernel/wrapper/subsystem.hpp"

#include "fake_control_socket.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using namespace r2kernel::wrapper;
using r2kernel_test::FakeControl;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

template <typename Exception>
std::string thrown_message(const std::function<void()>& call, const std::string& what) {
    try {
        call();
    } catch (const Exception& error) {
        return error.what();
    }
    throw std::runtime_error(what + ": expected an exception");
}

void require_thrown(const std::function<void()>& call, const std::string& fragment) {
    const auto message = thrown_message<std::runtime_error>(call, fragment);
    require(message.find(fragment) != std::string::npos, "expected <" + fragment + "> in <" + message + ">");
}

class ProbeGateway final : public GatewaySubsystem {
public:
    using GatewaySubsystem::GatewaySubsystem;

protected:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

using Fields = std::map<std::string, std::string>;

// A binding row; values are JSON strings unless they start with a JSON
// literal character.
std::string binding(const Fields& overrides = {}) {
    Fields fields {{"source", "component.cleaner.device_control"},
                   {"source_subsystem", "component.cleaner"},
                   {"requirement", "device_control"},
                   {"target", "gateway.device.device_control"},
                   {"target_subsystem", "gateway.device"},
                   {"provider_interface", "device_control"},
                   {"state", "bound"}};
    for (const auto& [key, value] : overrides) fields[key] = value;
    std::string out = "{";
    for (const auto& [key, value] : fields) {
        if (out.size() > 1) out += ",";
        const bool literal = !value.empty() && (value == "true" || value == "false" || value == "null" || value.front() == '{');
        out += "\"" + key + "\":" + (literal ? value : "\"" + value + "\"");
    }
    return out + "}";
}

std::string status(const std::vector<std::string>& bindings) {
    std::string out = R"({"ok":true,"res":"ok","bindings":[)";
    for (std::size_t index = 0; index < bindings.size(); ++index) out += (index ? "," : "") + bindings[index];
    return out + "]}";
}

std::vector<std::string> two_sources() {
    return {binding(), binding({{"source", "component.navigation.device_control"}, {"source_subsystem", "component.navigation"}})};
}

void reply_with(FakeControl& control, const std::string& reply) {
    control.respond([reply](const std::string&) { return reply; });
}

std::unique_ptr<ProbeGateway> make_gateway(const std::filesystem::path& root) {
    std::filesystem::create_directories(root);
    std::ofstream(root / "gateway.yaml") << "kind: r2kernel.subsystem\nversion: 1\n"
                                            "subsystem: {name: gateway.device, role: gateway}\n"
                                            "provides:\n  device_control:\n    contract: tutorial.gateway.control.v1\n"
                                            "    visibility: internal\n    endpoints:\n      controls: [set_value]\n";
    SubsystemOptions options;
    options.name = "gateway.device";
    options.user_data_path = root / "userdata";
    options.manifest_path = root / "gateway.yaml";
    return std::make_unique<ProbeGateway>(options);
}

void test_owner_and_sessions(FakeControl& control, const ProbeGateway& gateway) {
    reply_with(control, status({binding(),
                                binding({{"source", "component.other.device_control"}, {"source_subsystem", "component.other"},
                                         {"target", "component.other.device_control"}, {"target_subsystem", "component.other"}}),
                                R"({"source":"gateway.device.optional_control","source_subsystem":"gateway.device",)"
                                R"("requirement":"optional_control","target":"","target_subsystem":"","provider_interface":"",)"
                                R"("state":"unbound","runtime":true,"explicit_unbound":true})"}));
    const auto before = control.requests().size();
    require(gateway.bound_control_owner("device_control") == "component.cleaner", "single direct owner");
    require(control.requests().size() == before + 1
                && control.requests().back() == R"({"cmd":"binding_status","subsystem":"gateway.device"})",
            "one binding_status request");

    reply_with(control, status(two_sources()));
    require(gateway.bound_control_sessions("device_control")
                == std::map<std::string, std::string> {{"component.cleaner.device_control", "component.cleaner"},
                                                       {"component.navigation.device_control", "component.navigation"}},
            "every bound source");
    require(gateway.bound_control_session("device_control", "component.navigation") == "component.navigation.device_control",
            "one source's reference");
    require_thrown([&] { (void)gateway.bound_control_session("device_control", "component.unknown"); },
                   "No runtime binding from source subsystem");
    require_thrown([&] { (void)gateway.bound_control_owner("device_control"); }, "Ambiguous runtime bindings");
    reply_with(control, status({binding(), binding({{"source", "component.cleaner.emergency_control"},
                                                    {"requirement", "emergency_control"}})}));
    require_thrown([&] { (void)gateway.bound_control_session("device_control", "component.cleaner"); },
                   "Ambiguous runtime bindings from source subsystem");

    reply_with(control, status({}));
    require_thrown([&] { (void)gateway.bound_control_owner("device_control"); }, "No active runtime binding");
    reply_with(control, status({binding({{"target", "gateway.device.other_control"}, {"provider_interface", "other_control"}})}));
    require_thrown([&] { (void)gateway.bound_control_owner("device_control"); }, "No active runtime binding");
    reply_with(control, status({binding({{"state", "target_missing"}})}));
    require_thrown([&] { (void)gateway.bound_control_session("device_control", "component.cleaner"); }, "is not bound");

    const auto requests = control.requests().size();
    require_thrown([&] { (void)gateway.bound_control_owner("missing_control"); }, "is not declared");
    require(control.requests().size() == requests, "an undeclared interface sends nothing");
}

void test_malformed_status_fails_closed(FakeControl& control, const ProbeGateway& gateway,
                                        const std::filesystem::path& root) {
    for (const std::string reply :
         {std::string(""), std::string(R"({"ok":true,"res":"ok","bindings":{}})"),
          std::string(R"({"ok":false,"res":"error","bindings":[]})"),
          status({binding({{"source", "invalid"}})}),
          status({binding({{"source", "component.cleaner.control"}, {"source_subsystem", "component.other"}})}),
          status({binding({{"provider_interface", "other_control"}})})}) {
        reply_with(control, reply);
        (void)thrown_message<std::runtime_error>([&] { (void)gateway.bound_control_owner("device_control"); },
                                                 "malformed status: " + reply);
    }
    const std::string socket = std::getenv("R2KERNEL_CONTROL_SOCKET");
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent.sock").c_str(), 1);
    require_thrown([&] { (void)gateway.bound_control_owner("device_control"); }, "Runtime binding status is unavailable");
    ::setenv("R2KERNEL_CONTROL_SOCKET", socket.c_str(), 1);
}

void test_fresh_status_on_each_call(FakeControl& control, const ProbeGateway& gateway) {
    reply_with(control, status({binding()}));
    require(gateway.bound_control_owner("device_control") == "component.cleaner", "first owner");
    reply_with(control, status({binding({{"source", "component.updated.device_control"},
                                         {"source_subsystem", "component.updated"}})}));
    require(gateway.bound_control_owner("device_control") == "component.updated", "status is not cached");
}

void test_remote_authority(FakeControl& control, const ProbeGateway& gateway) {
    reply_with(control, status({binding({{"source_peer_id", "compute"}, {"source_session_id", "session-a"},
                                         {"target_session_id", "provider-a"}, {"source_registration_revision", "source-reg-a"},
                                         {"target_registration_revision", "target-reg-a"}, {"binding_revision", "binding-a"}})}));
    ControlSourceAuthority authenticated;
    authenticated.peer = "compute";
    authenticated.source = "component.cleaner";
    authenticated.source_session = "session-a";
    authenticated.source_reference = "component.cleaner.device_control";
    authenticated.target_session = "provider-a";
    authenticated.interface = "device_control";
    authenticated.binding_revision = "binding-a";
    authenticated.source_registration = "source-reg-a";
    authenticated.target_registration = "target-reg-a";
    authenticated.epoch = "epoch-1";
    authenticated.target = "gateway.device.device_control";
    authenticated.topic = "set_value";
    const auto authority = gateway.validate_control_authority("device_control", "component.cleaner", authenticated);
    require(authority.source == "component.cleaner" && authority.source_reference == "component.cleaner.device_control"
                && authority.peer == std::optional<std::string>("compute") && authority.interface_name == "device_control"
                && authority.source_session == std::optional<std::string>("session-a")
                && authority.binding_revision == std::optional<std::string>("binding-a"),
            "authority fields");
    const std::vector<std::string ControlSourceAuthority::*> fields = {
        &ControlSourceAuthority::peer, &ControlSourceAuthority::source_session,
        &ControlSourceAuthority::source_registration, &ControlSourceAuthority::target_registration,
        &ControlSourceAuthority::binding_revision, &ControlSourceAuthority::interface};
    for (const auto field : fields) {
        auto changed = authenticated;
        changed.*field = "another-owner-epoch";
        require_thrown([&] { (void)gateway.validate_control_authority("device_control", "component.cleaner", changed); },
                       "registration or binding epoch");
    }
    require_thrown([&] { (void)gateway.validate_control_authority("device_control", "component.cleaner", std::nullopt); },
                   "registration or binding epoch");
    (void)thrown_message<std::invalid_argument>(
        [&] { (void)gateway.validate_control_authority("device_control", "component.other", authenticated); },
        "another source subsystem");
    // A local source (no peer) needs only the matching source subsystem.
    reply_with(control, status({binding()}));
    ControlEndpointRequest request;
    request.sender = "component.cleaner";
    require(!gateway.validate_control_authority("device_control", request).peer.has_value(), "local source");
}

}  // namespace

int main() {
    const auto root = std::filesystem::temp_directory_path() / ("r2kernel-cpp-gateway-bound-" + std::to_string(::getpid()));
    ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
    ::unsetenv("R2KERNEL_BINDINGS");
    ::unsetenv("R2KERNEL_DEPLOYMENT_MANIFEST");
    int result = 0;
    try {
        FakeControl control;
        const auto gateway = make_gateway(root);
        test_owner_and_sessions(control, *gateway);
        test_malformed_status_fails_closed(control, *gateway, root);
        test_fresh_status_on_each_call(control, *gateway);
        test_remote_authority(control, *gateway);
    } catch (const std::exception& error) {
        std::cerr << "gateway_bound_control_test failed: " << error.what() << '\n';
        result = 1;
    }
    std::filesystem::remove_all(root);
    if (result == 0) std::cout << "gateway_bound_control_test passed\n";
    return result;
}
