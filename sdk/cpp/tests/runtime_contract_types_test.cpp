// Offline checks of the runtime contract type rule shared with the Python SDK
// (r2kernel.contract.validate_runtime_contract_types): every registered Control
// endpoint, Procedure and Operation, including internal ones that no public
// spec declares, needs resolvable type metadata before the subsystem is Ready.
#include "r2kernel/wrapper/subsystem.hpp"
#include "fake_control_socket.hpp"
#include "offline_core_client.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

using namespace r2kernel::wrapper;
namespace fs = std::filesystem;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

struct ScopedPolicy {
    explicit ScopedPolicy(const char* value) {
        if (value) ::setenv("R2KERNEL_CONTRACT_POLICY", value, 1);
        else ::unsetenv("R2KERNEL_CONTRACT_POLICY");
    }
    ~ScopedPolicy() { ::unsetenv("R2KERNEL_CONTRACT_POLICY"); }
};

struct TempTree {
    fs::path root = r2kernel_test::unique_temp_path("runtime-types");
    TempTree() { fs::create_directories(root); }
    ~TempTree() { std::error_code ignored; fs::remove_all(root, ignored); }
    fs::path write(const fs::path& relative, const std::string& text) const {
        const auto path = root / relative;
        fs::create_directories(path.parent_path());
        std::ofstream file(path);
        file << text;
        require(bool(file), "fixture write failed: " + path.string());
        return path;
    }
};

struct LogRecord {
    std::mutex mutex;
    std::vector<std::pair<std::string, std::string>> lines;
    bool contains(const std::string& level, const std::string& needle) {
        std::lock_guard<std::mutex> lock(mutex);
        for (const auto& [line_level, message] : lines)
            if (line_level == level && message.find(needle) != std::string::npos) return true;
        return false;
    }
};

// Registers the given endpoints from on_initialize, as the lifecycle requires.
class Fixture final : public Subsystem {
public:
    Fixture(SubsystemOptions options, std::function<bool(Fixture&)> hook)
        : Subsystem(std::move(options)), hook_(std::move(hook)) {}
    bool register_all() { return hook_(*this); }
private:
    bool on_initialize() override { return hook_(*this); }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
    std::function<bool(Fixture&)> hook_;
};

SubsystemOptions fixture_options(const TempTree& tree, const std::string& name, const std::string& endpoints,
                                 LogRecord& log, std::optional<std::vector<std::string>> roots = std::vector<std::string>{}) {
    SubsystemOptions options;
    options.name = name;
    options.manifest_path = tree.write(name + ".yaml",
        "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " + name + "\n  role: component\n"
        "provides:\n  local:\n    contract: fixture.runtime_types.private.v1\n    visibility: internal\n"
        "    endpoints:\n" + endpoints);
    options.schema_roots = std::move(roots);
    options.core_client = std::make_shared<r2kernel_test::OfflineCoreClient>();
    options.log_sink = [&log](std::string_view level, std::string_view, std::string_view, std::string_view message) {
        std::lock_guard<std::mutex> lock(log.mutex);
        log.lines.emplace_back(std::string(level), std::string(message));
    };
    return options;
}

ControlEndpointResponse no_control(const ControlEndpointRequest&) { return ControlEndpointResponse{}; }
ProcedureResponse no_procedure(ProcedureRequest) { return ProcedureResponse{}; }
std::optional<std::string> no_operation(OperationRequest, OperationContext&) { return std::nullopt; }

std::vector<std::string> messages_of(const ContractValidationReport& report, const std::string& level) {
    std::vector<std::string> messages;
    for (const auto& issue : report.issues) {
        require(issue.api_id == "runtime_contract_types", "issue lost its runtime_contract_types origin");
        if (issue.level == level) messages.push_back(issue.message);
    }
    return messages;
}

std::string joined(const std::vector<std::string>& messages) {
    std::string text;
    for (const auto& message : messages) text += (text.empty() ? "" : "; ") + message;
    return text;
}

// Mirrors test_runtime_contract_types_accept_external_message_references: types
// from another message package resolve through the shipped spec standards.
void external_message_references_are_accepted(const TempTree& tree) {
    LogRecord log;
    Fixture subsystem(fixture_options(tree, "fixture.runtime_types.external",
        "      controls: [velocity]\n      procedures: [stop_velocity]\n", log), [](Fixture& current) {
        ControlEndpointOptions control;
        control.request_type = "msg.locomotion.velocity.v1/VelocityCommand";
        control.response_type = "msg.locomotion.velocity.v1/CommandAck";
        control.timing_support = "required";
        ProcedureOptions procedure;
        procedure.request_type = "msg.locomotion.velocity.v1/StopRequest";
        procedure.response_type = "msg.core.common.result.v1/Result";
        return current.register_control_endpoint("velocity", no_control, control)
            && current.register_procedure("stop_velocity", no_procedure, procedure);
    });
    require(subsystem.register_all(), "external reference fixture registration failed");
    const auto report = subsystem.validate_runtime_contract_types("strict");
    require(report.ok() && report.issues.empty(), "external message references were rejected: " + report.format_errors());
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "external message references prevented Ready");
    require(log.contains("INFO", "Runtime contract type validation passed [policy=strict]"), "strict pass was not logged");
    subsystem.terminate();
}

// Scalars, wrappers and API-local messages resolve; unversioned, bare,
// array-suffix (int32[]) and missing references do not.
void type_reference_forms(const TempTree& tree) {
    const auto spec = tree.root / "spec";
    tree.write("spec/messages/msg/test/runtime_types/v1.yaml",
        "version: 1\nid: msg.test.runtime_types.v1\nkind: message_set\nmessages:\n  Value:\n    fields:\n"
        "      - name: value\n        type: uint32\n");
    tree.write("spec/apis/api/test/runtime_types/v1.yaml",
        "id: api.test.runtime_types.v1\nname: api.test.runtime_types.v1\nversion: 1\nmessages:\n  LocalGoal:\n"
        "    fields:\n      - name: value\n        type: uint32\n");
    LogRecord log;
    Fixture subsystem(fixture_options(tree, "fixture.runtime_types.forms",
        "      procedures: [known, bare, unversioned, suffix, missing, missing_local]\n      operations: [wrapped]\n",
        log, std::vector<std::string>{spec.string()}), [](Fixture& current) {
        const auto procedure = [&](const std::string& name, const std::string& request) {
            ProcedureOptions options;
            options.request_type = request;
            options.response_type = "bool";
            return current.register_procedure(name, no_procedure, options);
        };
        OperationOptions wrapped;
        wrapped.request_type = "api.test.runtime_types.v1/LocalGoal";
        wrapped.feedback_type = "vector<msg.test.runtime_types.v1/Value>";
        wrapped.result_type = "array<uint8, 4>";
        wrapped.state_type = "uint32_array";
        return procedure("known", " msg.test.runtime_types.v1/Value ") && procedure("bare", "Value")
            && procedure("unversioned", "msg.test.runtime_types/Value") && procedure("suffix", "int32[]")
            && procedure("missing", "msg.test.runtime_types.v1/Missing")
            && procedure("missing_local", "api.test.runtime_types.v1/Missing")
            && current.register_operation("wrapped", no_operation, wrapped);
    });
    require(subsystem.register_all(), "type form fixture registration failed");
    const auto report = subsystem.validate_runtime_contract_types("strict");
    const std::vector<std::string> expected = {
        "procedure 'bare' request_type references unknown message type 'Value'",
        "procedure 'missing' request_type references unknown message type 'msg.test.runtime_types.v1/Missing'",
        "procedure 'missing_local' request_type references unknown message type 'api.test.runtime_types.v1/Missing'",
        "procedure 'suffix' request_type references unknown message type 'int32[]'",
        "procedure 'unversioned' request_type references unknown message type 'msg.test.runtime_types/Value'",
    };
    require(messages_of(report, "error") == expected, "unexpected type reference verdicts: " + report.format_errors());
    require(report.format_errors() == joined(expected), "format_errors diverged from the issue messages");
}

const char* const kInternalEndpoints =
    "      controls: [status]\n      procedures: [reset_counter]\n      operations: [calibrate]\n";

// Internal endpoints that no public api/component declares,
// registered without type metadata (an Operation's default leaves its
// request/feedback/result types empty).
bool register_untyped_internal_endpoints(Fixture& current) {
    return current.register_control_endpoint("status", no_control)
        && current.register_procedure("reset_counter", no_procedure)
        && current.register_operation("calibrate", no_operation);
}

const std::vector<std::string> kUntypedInternalMessages = {
    "control 'status' is registered without request/response type metadata",
    "procedure 'reset_counter' is registered without request/response type metadata",
    "operation 'calibrate' request_type is empty",
    "operation 'calibrate' feedback_type is empty",
    "operation 'calibrate' result_type is empty",
};

void internal_endpoint_without_type_metadata_fails_under_strict(const TempTree& tree) {
    ScopedPolicy policy(nullptr);  // strict is the default
    LogRecord log;
    Fixture subsystem(fixture_options(tree, "fixture.runtime_types.strict", kInternalEndpoints, log),
                      register_untyped_internal_endpoints);
    require(subsystem.register_all(), "untyped internal fixture registration failed");
    require(subsystem.validate_declarations().ok(), "public declaration validation covers internal endpoints");
    const auto report = subsystem.validate_runtime_contract_types();
    require(!report.ok() && messages_of(report, "error") == kUntypedInternalMessages,
        "strict runtime type validation accepted untyped internal endpoints: " + report.format_errors());
    subsystem.initialize();
    require(subsystem.state() == DeviceState::error, "untyped internal endpoints reached Ready under strict");
    require(log.contains("ERROR", joined(kUntypedInternalMessages)), "strict failure did not log the type errors");
    subsystem.terminate();

    // An unknown message type is refused before codec preparation names it.
    LogRecord unknown_log;
    Fixture unknown(fixture_options(tree, "fixture.runtime_types.unknown", "      controls: [status]\n", unknown_log),
                    [](Fixture& current) {
        ControlEndpointOptions options;
        options.request_type = "msg.test.missing.v1/Nothing";
        options.response_type = "bool";
        return current.register_control_endpoint("status", no_control, options);
    });
    unknown.initialize();
    require(unknown.state() == DeviceState::error, "an unknown control request type reached Ready under strict");
    require(unknown_log.contains("ERROR",
            "control 'status' request_type references unknown message type 'msg.test.missing.v1/Nothing'"),
        "unknown type failure did not name the endpoint and type");
    unknown.terminate();
}

void warn_policy_reports_warnings(const TempTree& tree) {
    ScopedPolicy policy("warn");
    LogRecord log;
    Fixture subsystem(fixture_options(tree, "fixture.runtime_types.warn", kInternalEndpoints, log),
                      register_untyped_internal_endpoints);
    require(subsystem.register_all(), "warn fixture registration failed");
    const auto report = subsystem.validate_runtime_contract_types();
    require(report.ok() && messages_of(report, "warning") == kUntypedInternalMessages,
        "warn policy did not downgrade runtime type errors to warnings");
    require(!subsystem.validate_runtime_contract_types("strict").ok(), "an explicit strict policy was ignored");
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "warn policy blocked Ready");
    require(log.contains("WARN", "Runtime contract type validation warnings: " + joined(kUntypedInternalMessages)),
        "warn policy did not log the runtime type warnings");
    subsystem.terminate();
}

void off_policy_skips_validation(const TempTree& tree) {
    ScopedPolicy policy("off");
    LogRecord log;
    Fixture subsystem(fixture_options(tree, "fixture.runtime_types.off", kInternalEndpoints, log),
                      register_untyped_internal_endpoints);
    require(subsystem.register_all(), "off fixture registration failed");
    require(subsystem.validate_runtime_contract_types().issues.empty(), "off policy still reported runtime type issues");
    require(!subsystem.validate_runtime_contract_types("bogus").ok(), "an unknown policy did not fall back to strict");
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready, "off policy blocked Ready");
    require(!log.contains("WARN", "Runtime contract type validation"), "off policy logged runtime type warnings");
    subsystem.terminate();
}
}  // namespace

int main() {
    try {
        ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
        ::unsetenv("R2KERNEL_SPEC_ROOT");
        ::unsetenv("R2KERNEL_CONTRACT_POLICY");
        const TempTree tree;
        external_message_references_are_accepted(tree);
        type_reference_forms(tree);
        internal_endpoint_without_type_metadata_fails_under_strict(tree);
        warn_policy_reports_warnings(tree);
        off_policy_skips_validation(tree);
        std::cout << "runtime contract type checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
