// Public manifest model of a Subsystem and its deployment manifest (Python
// SDK r2kernel.manifest models, deployment discovery and resolution; same
// scenarios as tests/test_manifests_deployment.py and test_launch_bindings.py).
#include "r2kernel/wrapper/subsystem.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {

using namespace r2kernel::wrapper;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

void require_equal(const std::string& actual, const std::string& expected, const std::string& what) {
    if (actual != expected) throw std::runtime_error(what + ": expected <" + expected + "> got <" + actual + ">");
}

std::string manifest_error(const std::function<void()>& call, const std::string& what) {
    try {
        call();
    } catch (const ManifestError& error) {
        return error.what();
    }
    throw std::runtime_error(what + ": expected ManifestError");
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

void write_file(const std::filesystem::path& path, const std::string& text) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << text;
}

std::string client_manifest(const std::string& name = "client", const std::string& contract = "tutorial.test.service.v1") {
    return "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: " + name + ", role: component}\n"
           "requires:\n  service:\n    contract: " + contract + "\n    visibility: internal\n"
           "    endpoints: {procedures: [read]}\n"
           "  optional_feed:\n    contract: tutorial.test.feed.v1\n    visibility: internal\n    required: false\n";
}

std::string server_manifest(const std::string& name = "server", const std::string& contract = "tutorial.test.service.v1") {
    return "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: " + name + ", role: component}\n"
           "provides:\n  service:\n    contract: " + contract + "\n    visibility: internal\n"
           "    endpoints: {procedures: [read]}\n";
}

SubsystemOptions options_for(const std::filesystem::path& root, const std::string& name = "client") {
    SubsystemOptions options;
    options.name = name;
    options.user_data_path = root / "userdata";
    options.manifest_path = root / "client" / "subsystem.yaml";
    return options;
}

void write_deployment(const std::filesystem::path& path, const std::string& bindings) {
    write_file(path, "kind: r2kernel.deployment\nversion: 1\nsubsystems:\n"
                     "  client: {manifest: ./client/subsystem.yaml}\n  server: ./server/subsystem.yaml\n"
                     "bindings:\n" + bindings);
}

void test_subsystem_manifest_model(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml", client_manifest());
    Probe subsystem(options_for(root));
    const SubsystemManifest& manifest = subsystem.subsystem_manifest();
    require_equal(manifest.name, "client", "manifest name");
    require_equal(manifest.role, "component", "manifest role");
    require_equal(manifest.kind, "r2kernel.subsystem", "manifest kind");
    require(manifest.path == std::filesystem::weakly_canonical(root / "client" / "subsystem.yaml"), "manifest path");
    require(manifest.requirements.size() == 2 && manifest.provides.empty(), "requirements");
    const InterfaceDeclaration& service = manifest.requirements.at("service");
    require(service.required && service.visibility == "internal" && service.contract == "tutorial.test.service.v1",
            "service requirement");
    require(service.endpoints.at("procedures") == std::vector<std::string> {"read"}, "service endpoints");
    require(!manifest.requirements.at("optional_feed").required, "optional requirement");
    require(manifest.declared_apis().empty() && manifest.declared_components().empty(), "no public contracts");
    // No deployment manifest and no launch binding: the required interface
    // waits for a runtime binding.
    require(!subsystem.deployment_manifest().has_value(), "no deployment manifest found");
    require(subsystem.state() == DeviceState::waiting_binding, "required binding waits");
}

void test_launch_binding_does_not_wait(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml", client_manifest());
    auto options = options_for(root);
    options.launch_bindings["service"] = "server.service";
    Probe subsystem(std::move(options));
    require(subsystem.state() == DeviceState::booting, "a launch binding does not wait");
}

void test_explicit_and_discovered_deployment(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml", client_manifest());
    write_file(root / "server" / "subsystem.yaml", server_manifest());
    write_deployment(root / "custom.deployment.yaml", "  client.service:\n    to: server.service\n");
    auto options = options_for(root);
    options.deployment_manifest_path = root / "custom.deployment.yaml";
    {
        Probe subsystem(options);
        const auto& deployment = subsystem.deployment_manifest();
        require(deployment.has_value(), "explicit deployment manifest loaded");
        require(deployment->subsystems.size() == 2 && deployment->bindings.size() == 1, "deployment content");
        const BoundInterface bound = deployment->resolve("client", "service");
        require(bound == BoundInterface {"service", "server", "service", "tutorial.test.service.v1"}, "resolved binding");
        require_equal(bound.target(), "server", "bound target");
        require(subsystem.state() == DeviceState::booting, "a deployment binding does not wait");
        require_equal(manifest_error([&] { (void)deployment->resolve("client", "optional_feed"); }, "optional"),
                      "Optional binding is not configured: client.optional_feed", "optional binding error");
    }
    ::setenv("R2KERNEL_DEPLOYMENT_MANIFEST", (root / "custom.deployment.yaml").c_str(), 1);
    {
        Probe subsystem(options_for(root));
        require(subsystem.deployment_manifest().has_value(), "R2KERNEL_DEPLOYMENT_MANIFEST is read");
    }
    ::unsetenv("R2KERNEL_DEPLOYMENT_MANIFEST");
    // Discovery: r2kernel.deployment.yaml in the manifest's parent directory.
    write_deployment(root / "r2kernel.deployment.yaml", "  client.service: server.service\n");
    {
        Probe subsystem(options_for(root));
        require(subsystem.deployment_manifest().has_value()
                    && subsystem.deployment_manifest()->path == std::filesystem::weakly_canonical(root / "r2kernel.deployment.yaml"),
                "deployment manifest discovered next to the subsystem manifest directory");
    }
    std::filesystem::remove(root / "r2kernel.deployment.yaml");
}

void test_invalid_deployments_fail_construction(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml", client_manifest());
    write_file(root / "server" / "subsystem.yaml", server_manifest("server", "tutorial.test.other.v1"));
    auto options = options_for(root);
    options.deployment_manifest_path = root / "deployment.yaml";
    write_deployment(options.deployment_manifest_path, "  client.service: server.service\n");
    require_equal(manifest_error([&] { Probe subsystem(options); }, "contract mismatch"),
                  "Binding contract mismatch: client.service requires tutorial.test.service.v1, but server.service "
                  "provides tutorial.test.other.v1",
                  "contract mismatch");
    write_file(root / "server" / "subsystem.yaml", server_manifest());
    write_deployment(options.deployment_manifest_path, "  {}\n");
    require_equal(manifest_error([&] { Probe subsystem(options); }, "missing binding"),
                  "Required binding is missing: client.service", "missing required binding");
    write_deployment(options.deployment_manifest_path, "  client.service: {select: {}}\n");
    require_equal(manifest_error([&] { Probe subsystem(options); }, "selector"),
                  "Selector bindings are not supported yet: client.service", "selector binding");
    write_file(options.deployment_manifest_path, "kind: r2kernel.robot\n");
    (void)manifest_error([&] { Probe subsystem(options); }, "deployment kind");
}

void test_instances_use_package_manifests(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml", client_manifest());
    write_file(root / "packages" / "server" / "package.yaml", "name: server\nsubsystem_manifest: manifests/server.yaml\n");
    write_file(root / "packages" / "server" / "manifests" / "server.yaml", server_manifest("server.template"));
    write_file(root / "deployment.yaml",
               "kind: r2kernel.deployment\nversion: 1\nsubsystems:\n  client: ./client/subsystem.yaml\n"
               "instances:\n  server.left: {package: ./packages/server, launch: external}\n"
               "bindings:\n  client.service: server.left.service\n");
    auto options = options_for(root);
    options.deployment_manifest_path = root / "deployment.yaml";
    Probe subsystem(options);
    const auto& deployment = *subsystem.deployment_manifest();
    require_equal(deployment.subsystems.at("server.left").name, "server.left", "instance renamed");
    require_equal(deployment.resolve("client", "service").target(), "server.left", "dotted instance target");
}

// Export resolution through a compound, on an in-memory deployment.
void test_export_resolution() {
    DeploymentManifest deployment;
    SubsystemManifest app;
    app.name = "app";
    app.role = "app";
    app.requirements["motion"] = InterfaceDeclaration {"motion", "api.motion.v1"};
    SubsystemManifest compound;
    compound.name = "robot";
    compound.role = "compound";
    compound.requirements["base"] = InterfaceDeclaration {"base", "component.base.v1"};
    compound.exports["motion"] = ExportDeclaration {"motion", "api.motion.v1", "base"};
    SubsystemManifest base;
    base.name = "base";
    base.role = "component";
    base.provides["control"] = InterfaceDeclaration {"control", "component.base.v1"};
    deployment.subsystems = {{"app", app}, {"robot", compound}, {"base", base}};
    deployment.bindings[{"app", "motion"}] = {"robot", "motion"};
    deployment.bindings[{"robot", "base"}] = {"base", "control"};
    require(deployment.resolve("app", "motion") == BoundInterface {"motion", "base", "control", "api.motion.v1"},
            "an export resolves to the component provider");
    deployment.subsystems.at("base").provides.at("control").visibility = "internal";
    require_equal(manifest_error([&] { (void)deployment.resolve("app", "motion"); }, "internal export target"),
                  "Export target must be public: base.control", "internal export target");
    deployment.subsystems.at("base").role = "gateway";
    (void)manifest_error([&] { (void)deployment.resolve("app", "motion"); }, "gateway behind export");
    deployment.bindings.erase({"robot", "base"});
    require_equal(manifest_error([&] { (void)deployment.resolve("app", "motion"); }, "missing export source"),
                  "Export source binding is missing: robot.base", "missing export source");
}

// Python _CommunicationMixin.declared_runtime_endpoints: provided internal
// endpoints, plus the inverted Event endpoints of internal requirements.
void test_declared_runtime_endpoints(const std::filesystem::path& root) {
    write_file(root / "client" / "subsystem.yaml",
               "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: client, role: component}\n"
               "provides:\n  local:\n    contract: tutorial.test.local.v1\n    visibility: internal\n"
               "    endpoints:\n      publishes: [state, alarm]\n      subscribes: [command]\n"
               "      controls: [drive]\n      procedures: [read, reset]\n      operations: [move]\n"
               "requires:\n  feed:\n    contract: tutorial.test.feed.v1\n    visibility: internal\n    required: false\n"
               "    endpoints:\n      publishes: [samples]\n      subscribes: [requests]\n      procedures: [remote]\n");
    Probe subsystem(options_for(root));
    using Names = std::vector<std::string>;
    require(subsystem.declared_runtime_endpoints("publishes") == Names {"alarm", "requests", "state"}, "publishes");
    require(subsystem.declared_runtime_endpoints("subscribes") == Names {"command", "samples"}, "subscribes");
    require(subsystem.declared_runtime_endpoints("controls") == Names {"drive"}, "controls");
    require(subsystem.declared_runtime_endpoints("procedures") == Names {"read", "reset"}, "procedures");
    require(subsystem.declared_runtime_endpoints("operations") == Names {"move"}, "operations");
    require(subsystem.declared_runtime_endpoints("services").empty(), "unknown kind");
}

void test_binding_reference() {
    const BindingReference reference = BindingReference::parse(" component.left.arm . control ", "binding");
    require(reference.subsystem == "component.left.arm" && reference.interface_name == "control", "last dot split");
    require_equal(reference.format(), "component.left.arm.control", "format");
    require_equal(manifest_error([] { (void)BindingReference::parse("nodot", "R2KERNEL_BINDINGS.x"); }, "no dot"),
                  "R2KERNEL_BINDINGS.x must use '<subsystem>.<interface>': 'nodot'", "missing dot");
    (void)manifest_error([] { (void)BindingReference::parse("trailing.", "binding"); }, "empty interface");
}

}  // namespace

int main() {
    const auto root = std::filesystem::temp_directory_path() / ("r2kernel-cpp-manifest-" + std::to_string(::getpid()));
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent-control.sock").c_str(), 1);
    ::unsetenv("R2KERNEL_BINDINGS");
    ::unsetenv("R2KERNEL_DEPLOYMENT_MANIFEST");
    int status = 0;
    try {
        test_subsystem_manifest_model(root / "model");
        test_launch_binding_does_not_wait(root / "launch");
        test_explicit_and_discovered_deployment(root / "deployment");
        test_invalid_deployments_fail_construction(root / "invalid");
        test_instances_use_package_manifests(root / "instances");
        test_export_resolution();
        test_declared_runtime_endpoints(root / "endpoints");
        test_binding_reference();
    } catch (const std::exception& error) {
        std::cerr << "subsystem_manifest_test failed: " << error.what() << '\n';
        status = 1;
    }
    std::filesystem::remove_all(root);
    if (status == 0) std::cout << "subsystem_manifest_test passed\n";
    return status;
}
