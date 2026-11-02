// Subsystem queries answered by Control, against a fake Control socket (same
// scenarios as the Python SDK tests test_runtime_binding_readiness.py and
// test_launch_bindings.py): the frame graph lookup, the spatial status
// payload and the runtime binding resolution.
#include "r2kernel/wrapper/subsystem.hpp"

#include "fake_control_socket.hpp"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using r2kernel::wrapper::BindingReference;
using r2kernel::wrapper::BoundInterface;
using r2kernel::wrapper::Subsystem;
using r2kernel::wrapper::SubsystemOptions;
using r2kernel_test::FakeControl;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

void require_equal(const std::string& actual, const std::string& expected, const std::string& what) {
    if (actual != expected) throw std::runtime_error(what + ": expected <" + expected + "> got <" + actual + ">");
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

class Probe : public Subsystem {
public:
    using Subsystem::Subsystem;

protected:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

std::filesystem::path test_root() {
    return std::filesystem::temp_directory_path() / ("r2kernel-cpp-runtime-queries-" + std::to_string(::getpid()));
}

void write_file(const std::filesystem::path& path, const std::string& text) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << text;
}

SubsystemOptions options_for(const std::filesystem::path& root, const std::string& name, const std::string& manifest) {
    write_file(root / "subsystem.yaml", manifest);
    SubsystemOptions options;
    options.name = name;
    options.user_data_path = root / "userdata";
    options.manifest_path = root / "subsystem.yaml";
    return options;
}

const char* kComponentManifest = R"(kind: r2kernel.subsystem
version: 1
subsystem: {name: app.radar, role: component}
)";

void test_frame_lookup_and_spatial_status(FakeControl& control, const std::filesystem::path& root) {
    Probe subsystem(options_for(root / "frames", "app.radar", kComponentManifest));
    control.respond([](const std::string&) {
        return std::string(R"({"res":"ok","parent_frame":"cleaner/base","translation":[1,2,3]})");
    });
    const auto before = control.requests().size();
    require_equal(subsystem.lookup_frame_transform(" cleaner_base ", "cleaner_radar"),
                  R"({"res":"ok","parent_frame":"cleaner/base","translation":[1,2,3]})", "lookup reply");
    const auto requests = control.requests();
    require(requests.size() == before + 1, "one frame lookup request");
    require_equal(requests.back(), R"({"cmd":"frame_lookup","source":"cleaner_base","target":"cleaner_radar"})",
                  "frame lookup request");
    (void)thrown_message<std::invalid_argument>([&] { (void)subsystem.lookup_frame_transform(" ", "x"); },
                                                "blank source frame");
    require(control.requests().size() == before + 1, "a blank frame sends nothing");

    require_equal(subsystem.spatial_status_payload(), R"({"transforms":[]})", "empty spatial status");
    r2kernel::wrapper::FrameTransform transform;
    transform.parent_frame = "base";
    transform.child_frame = "radar";
    transform.translation = {1.0, 0.0, 0.5};
    transform.observed_at_sync_ns = 42;
    transform.is_static = true;
    subsystem.set_frame_transform(transform);
    require_equal(subsystem.spatial_status_payload(),
                  R"({"transforms":[{"parent_frame":"base","child_frame":"radar","translation":[1,0,0.5],)"
                  R"("rotation_xyzw":[0,0,0,1],"observed_at_sync_ns":42,"static":true,"stale_after_ms":0}]})",
                  "spatial status lists the owned edge");
}

const char* kClientManifest = R"(kind: r2kernel.subsystem
version: 1
subsystem: {name: client, role: component}
requires:
  service:
    contract: tutorial.test.service.v1
    visibility: internal
    endpoints: {procedures: [read]}
)";

std::string binding_row(const std::string& state = "bound", const std::string& resolved_target = "\"server\"",
                        const std::string& resolved_interface = "\"service\"",
                        const std::string& provided_contract = "tutorial.test.service.v1") {
    return R"({"requirement":"service","source":"client.service","source_subsystem":"client",)"
           R"("target":"server.service","target_subsystem":"server","provider_interface":"service",)"
           R"("contract":"tutorial.test.service.v1","required_contract":"tutorial.test.service.v1",)"
           R"("provided_contract":")" + provided_contract + R"(","resolved_target":"server.service",)"
           R"("resolved_target_subsystem":)" + resolved_target + R"(,"resolved_provider_interface":)"
           + resolved_interface + R"(,"state":")" + state + R"(","runtime":true})";
}

std::string missing_response(const std::string& reason, const std::string& binding = {},
                             const std::string& source = "client.service") {
    return R"({"ok":false,"res":"missing","source":")" + source
        + R"(","subsystem":"client","requirement":"service","reason":")" + reason + "\""
        + (binding.empty() ? "" : ",\"binding\":" + binding) + "}";
}

void reply_with(FakeControl& control, const std::string& reply) {
    control.respond([reply](const std::string&) { return reply; });
}

void test_runtime_binding_resolution(FakeControl& control, const std::filesystem::path& root) {
    ::setenv("R2KERNEL_BINDINGS", R"({"service":"server.service"})", 1);
    Probe subsystem(options_for(root / "runtime", "client", kClientManifest));
    ::unsetenv("R2KERNEL_BINDINGS");
    const BoundInterface expected {"service", "server", "service", "tutorial.test.service.v1"};

    reply_with(control, R"({"ok":true,"res":"ok","binding":)" + binding_row() + "}");
    const auto before = control.requests().size();
    require(subsystem.resolve_runtime_binding("service") == expected, "bound runtime binding");
    require(control.requests().size() == before + 1, "one binding_resolve request");
    require_equal(control.requests().back(), R"({"cmd":"binding_resolve","subsystem":"client","requirement":"service"})",
                  "binding_resolve request");

    for (const auto& reply : {missing_response("not_bound"),
                              missing_response("source_registration_pending", binding_row("source_missing")),
                              missing_response("source_registration_pending", binding_row("source_role_missing")),
                              // A restarted source before its first status: Control reports the binding pinned
                              // to its old registration (sdk/testdata/wire/binding-resolve-pending-registration-changed.json).
                              missing_response("source_registration_pending", binding_row("registration_changed"))}) {
        reply_with(control, reply);
        require(!subsystem.resolve_runtime_binding("service").has_value(), "not ready: " + reply);
    }

    // The launch binding answers resolve_binding, never resolve_runtime_binding.
    reply_with(control, missing_response("not_bound"));
    require(subsystem.resolve_binding("service") == expected, "launch binding fallback");
    require_equal(subsystem.binding_target("service"), "server", "binding target");
    require(!subsystem.resolve_runtime_binding("service").has_value(), "no runtime fallback to the launch binding");
    require_equal(thrown_message<std::runtime_error>(
                      [&] { (void)subsystem.resolve_binding("service", std::chrono::steady_clock::now() + std::chrono::seconds(1)); },
                      "bounded resolve without a runtime binding"),
                  "Runtime binding is not ready: client.service", "a deadline accepts only a runtime binding");

    reply_with(control, R"({"ok":false,"res":"unbound","source":"client.service","subsystem":"client",)"
                        R"("requirement":"service","reason":"runtime_unbound","runtime":true})");
    require_equal(thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_runtime_binding("service"); }, "unbound"),
                  "Runtime binding is explicitly unbound: client.service", "explicit unbind");
    require_equal(thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_binding("service"); }, "unbound launch"),
                  "Runtime binding is explicitly unbound: client.service", "an explicit unbind blocks the launch binding");
    for (const std::string state : {"contract_mismatch", "invalid_registered_role", "target_missing"}) {
        reply_with(control, R"({"ok":true,"res":"ok","binding":)" + binding_row(state) + "}");
        const auto message = thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_runtime_binding("service"); }, state);
        require(message.find("state=" + state) != std::string::npos, "non-callable state: " + message);
    }
    reply_with(control, R"({"ok":true,"res":"ok","binding":{"state":"export_unbound","target_subsystem":"compound.platform",)"
                        R"("provider_interface":"service","resolved_target_subsystem":"","resolved_provider_interface":""}})");
    require(thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_binding("service"); }, "export unbound")
                .find("state=export_unbound") != std::string::npos,
            "an unresolved runtime export blocks the static fallback");

    const std::vector<std::string> malformed = {
        "",
        R"({"ok":true,"res":"ok","binding":[]})",
        R"({"ok":true,"res":"ok","binding":)" + binding_row("bound", R"({"name":"server"})") + "}",
        R"({"ok":true,"res":"ok","binding":)" + binding_row("bound", "\"server\"", "7") + "}",
        R"({"ok":true,"res":"ok","binding":)" + binding_row("bound", "\"server\"", "\"service\"", "component.wrong.v1") + "}",
        missing_response("unknown_reason"),
        missing_response("source_registration_pending", binding_row("contract_mismatch")),
        missing_response("not_bound", {}, "another.source"),
        "not json",
    };
    for (const auto& reply : malformed) {
        reply_with(control, reply);
        (void)thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_runtime_binding("service"); },
                                                 "malformed reply: " + reply);
    }

    const auto requests = control.requests().size();
    require_equal(thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_runtime_binding("typo"); }, "undeclared"),
                  "Subsystem client does not declare required interface: typo", "undeclared requirement");
    require_equal(thrown_message<std::runtime_error>(
                      [&] { (void)subsystem.resolve_runtime_binding("service", std::chrono::steady_clock::now()); },
                      "expired deadline"),
                  "Control binding resolution deadline exceeded", "an expired deadline");
    require(control.requests().size() == requests, "no request after validation or deadline failures");

    const std::string socket = std::getenv("R2KERNEL_CONTROL_SOCKET");
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent.sock").c_str(), 1);
    const auto transport = thrown_message<std::runtime_error>([&] { (void)subsystem.resolve_runtime_binding("service"); },
                                                              "unavailable Control");
    require(transport.find("Runtime binding resolve failed for client.service") == 0, "transport failure: " + transport);
    require(subsystem.resolve_binding("service") == expected, "an unavailable Control falls back to the launch binding");
    require(subsystem.runtime_bindings().empty(), "no runtime bindings without Control");
    ::setenv("R2KERNEL_CONTROL_SOCKET", socket.c_str(), 1);
}

void test_runtime_and_effective_bindings(FakeControl& control, const std::filesystem::path& root) {
    auto options = options_for(root / "status", "client", kClientManifest);
    options.launch_bindings["service"] = "server.service";
    Probe subsystem(options);
    require(subsystem.launch_bindings().at("service") == BindingReference {"server", "service"}, "launch bindings");
    reply_with(control, R"({"ok":true,"res":"ok","bindings":[)"
                        R"({"source_subsystem":"client","requirement":"service","target":"other.server.service"},)"
                        R"({"source_subsystem":"client","requirement":"unknown","target":"x.y"},)"
                        R"({"source_subsystem":"someone","requirement":"service","target":"x.y"},)"
                        R"({"source_subsystem":"client","requirement":"service"},7]})");
    const auto runtime = subsystem.runtime_bindings();
    require_equal(control.requests().back(), R"({"cmd":"binding_status","subsystem":"client"})", "binding_status request");
    require(runtime.size() == 1 && runtime.at("service") == BindingReference {"other.server", "service"},
            "only this subsystem's declared runtime bindings");
    require(subsystem.effective_bindings().at("service") == BindingReference {"other.server", "service"},
            "runtime bindings override launch bindings");
    reply_with(control, R"({"ok":true,"res":"ok","bindings":[{"source_subsystem":"client","requirement":"service","target":"nodot"}]})");
    require(subsystem.runtime_bindings().empty(), "an invalid runtime target is ignored");
    require(subsystem.effective_bindings().at("service") == BindingReference {"server", "service"}, "launch binding stays");

    options = options_for(root / "unbound", "client", kClientManifest);
    Probe waiting(options);
    reply_with(control, missing_response("not_bound"));
    require_equal(thrown_message<std::runtime_error>([&] { (void)waiting.resolve_binding("service"); }, "missing"),
                  "Required binding is missing: client.service", "no launch or deployment binding");
}

}  // namespace

int main() {
    const auto root = test_root();
    ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
    ::unsetenv("R2KERNEL_BINDINGS");
    ::unsetenv("R2KERNEL_DEPLOYMENT_MANIFEST");
    int status = 0;
    try {
        FakeControl control;
        test_frame_lookup_and_spatial_status(control, root);
        test_runtime_binding_resolution(control, root);
        test_runtime_and_effective_bindings(control, root);
    } catch (const std::exception& error) {
        std::cerr << "subsystem_runtime_queries_test failed: " << error.what() << '\n';
        status = 1;
    }
    std::filesystem::remove_all(root);
    if (status == 0) std::cout << "subsystem_runtime_queries_test passed\n";
    return status;
}
