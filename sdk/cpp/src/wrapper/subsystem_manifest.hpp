// Included once, inside subsystem.cpp's anonymous namespace (same translation
// unit). Subsystem manifest parsing and role policy (Python SDK
// r2kernel/manifest/loader.py and _contracts.py, with the same decisions and
// messages), manifest discovery, manifest status JSON and endpoint
// allow-lists. Cold: used during construction and validation.

using ApiEventSpec = contract_loader::ApiEventSpec;
using ApiControlSpec = contract_loader::ApiControlSpec;
using ApiProcedureSpec = contract_loader::ApiProcedureSpec;
using ApiOperationSpec = contract_loader::ApiOperationSpec;
using ApiContractSpec = contract_loader::ApiContractSpec;
using ComponentContractSpec = contract_loader::ComponentContractSpec;

// Public manifest model types (r2kernel/wrapper/manifest.hpp).
using ManifestInterfaceStatus = InterfaceDeclaration;
using ManifestExportStatus = ExportDeclaration;

struct AllowedEndpointSets {
    std::unordered_set<std::string> publishes;
    std::unordered_set<std::string> subscribes;
    std::unordered_set<std::string> controls;
    std::unordered_set<std::string> procedures;
    std::unordered_set<std::string> operations;
};

std::vector<std::string> interface_api_ids(
    const ManifestInterfaceStatus& declaration,
    bool include_internal_contracts = false
);
void validate_manifest_role_policy(const SubsystemManifest& manifest);
// Defined in subsystem_spatial.hpp.
SpatialDeclaration parse_spatial_declaration(const YAML::Node& value, const std::filesystem::path& manifest_path);
void validate_subsystem_spatial_conformance(const SubsystemManifest& manifest);
std::string spatial_status_json(const SpatialDeclaration& spatial);
// Defined in subsystem_deployment.hpp.
SubsystemManifest load_package_subsystem_manifest(const std::filesystem::path& path,
                                                  std::optional<std::filesystem::path> package_root);
std::filesystem::path expand_user(const std::filesystem::path& path);
std::filesystem::path resolved_path(const std::filesystem::path& path);

constexpr const char* kSubsystemRoles = "app, component, gateway, compound";

// Python repr() of a str, used in manifest error text.
std::string python_repr(const std::string& value) {
    return "'" + value + "'";
}

std::string join_names(const std::vector<std::string>& values) {
    std::string out;
    for (const auto& value : values) out += (out.empty() ? "" : ", ") + value;
    return out;
}

// Python str(value or "").strip() of a YAML scalar; empty for anything else.
std::string yaml_string(const YAML::Node& node) {
    if (!node || !node.IsScalar()) {
        return {};
    }
    return trim_copy(node.as<std::string>(std::string {}));
}

// A present, non-null value (Python `value is not None`).
bool yaml_present(const YAML::Node& node) {
    return node && !node.IsNull();
}

// Python `value or default`: absent, null, false, 0, "" and empty collections are falsy.
bool yaml_truthy(const YAML::Node& node) {
    if (!yaml_present(node)) return false;
    if (node.IsMap() || node.IsSequence()) return node.size() != 0;
    const std::string text = node.as<std::string>(std::string {});
    if (node.Tag() == "!") return !text.empty();
    return !(text.empty() || text == "false" || text == "False" || text == "FALSE" || text == "0");
}

bool yaml_bool(const YAML::Node& node, bool fallback) {
    if (!node) {
        return fallback;
    }
    try {
        return node.as<bool>();
    } catch (...) {
        const std::string text = trim_copy(node.as<std::string>(std::string {}));
        if (text == "true" || text == "yes" || text == "1") {
            return true;
        }
        if (text == "false" || text == "no" || text == "0") {
            return false;
        }
    }
    return fallback;
}

int yaml_version(const YAML::Node& node) {
    if (!yaml_truthy(node)) return 1;
    try {
        return node.as<int>();
    } catch (const YAML::Exception&) {
        throw ManifestError("manifest version must be an integer: " + node.as<std::string>(std::string {}));
    }
}

std::string yaml_node_json(const YAML::Node& node) {
    if (!node || node.IsNull()) {
        return "null";
    }
    if (node.IsSequence()) {
        std::ostringstream out;
        out << "[";
        for (std::size_t index = 0; index < node.size(); ++index) {
            if (index > 0) {
                out << ",";
            }
            out << yaml_node_json(node[index]);
        }
        out << "]";
        return out.str();
    }
    if (node.IsMap()) {
        std::ostringstream out;
        out << "{";
        bool first = true;
        for (const auto& item : node) {
            if (!first) {
                out << ",";
            }
            first = false;
            out << "\"" << json_escape(yaml_string(item.first)) << "\":" << yaml_node_json(item.second);
        }
        out << "}";
        return out.str();
    }
    const std::string text = node.as<std::string>(std::string {});
    if (node.Tag() != "!" && (text == "true" || text == "false" || text == "null")) {
        return text;
    }
    char* end = nullptr;
    std::strtod(text.c_str(), &end);
    if (node.Tag() != "!" && end != text.c_str() && end != nullptr && *end == '\0') {
        return text;
    }
    return "\"" + json_escape(text) + "\"";
}

// Python _load_yaml_mapping: label names the manifest in messages.
YAML::Node load_yaml_mapping(const std::filesystem::path& path, const std::string& label) {
    std::string title = label;
    if (!title.empty()) title[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(title[0])));
    std::error_code error;
    if (!std::filesystem::exists(path, error)) throw ManifestError(title + " file not found: " + path.string());
    YAML::Node document;
    try {
        document = YAML::LoadFile(path.string());
    } catch (const YAML::Exception& exception) {
        throw ManifestError(title + " is not valid YAML: " + path.string() + ": " + exception.what());
    }
    if (!yaml_truthy(document)) return YAML::Node(YAML::NodeType::Map);
    if (!document.IsMap()) throw ManifestError(title + " must be a mapping: " + path.string());
    return document;
}

// Python _string_mapping: keys and values stripped, empty keys dropped.
std::map<std::string, std::string> yaml_string_mapping(const YAML::Node& node, const std::string& field_name) {
    std::map<std::string, std::string> out;
    if (!yaml_truthy(node)) return out;
    if (!node.IsMap()) throw ManifestError(field_name + " must be a mapping");
    for (const auto& item : node) {
        const std::string key = yaml_string(item.first);
        if (key.empty()) continue;
        out[key] = item.second.IsScalar() ? yaml_string(item.second) : yaml_node_json(item.second);
    }
    return out;
}

// Python _mapping as JSON text: a mapping, or empty when absent.
std::string yaml_mapping_json(const YAML::Node& node, const std::string& field_name) {
    if (!yaml_truthy(node)) return "{}";
    if (!node.IsMap()) throw ManifestError(field_name + " must be a mapping");
    return yaml_node_json(node);
}

// Python _string_tuple: a string or a list of strings, blanks dropped.
std::vector<std::string> yaml_string_tuple(const YAML::Node& node) {
    std::vector<std::string> out;
    if (!yaml_present(node)) return out;
    if (node.IsScalar()) {
        if (const std::string text = yaml_string(node); !text.empty()) out.push_back(text);
        return out;
    }
    if (node.IsSequence()) {
        for (const auto& item : node) {
            const std::string text = item.IsScalar() ? yaml_string(item) : trim_copy(yaml_node_json(item));
            if (!text.empty()) out.push_back(text);
        }
        return out;
    }
    if (node.IsMap()) {
        for (const auto& item : node)
            if (const std::string text = yaml_string(item.first); !text.empty()) out.push_back(text);
        return out;
    }
    throw ManifestError("Expected a string or list of strings");
}

// The first truthy node (Python `a or b or ...`).
YAML::Node first_truthy(std::initializer_list<YAML::Node> nodes) {
    for (const auto& node : nodes)
        if (yaml_truthy(node)) return node;
    return YAML::Node();
}

// Python _endpoint_mapping.
std::map<std::string, std::vector<std::string>> yaml_endpoint_mapping(const YAML::Node& node, const std::string& field_name) {
    std::map<std::string, std::vector<std::string>> out;
    if (!yaml_truthy(node)) return out;
    if (!node.IsMap()) throw ManifestError(field_name + " must be a mapping");
    for (const auto& item : node) {
        const std::string kind = yaml_string(item.first);
        if (kind.empty()) throw ManifestError(field_name + " contains an empty endpoint kind");
        if (!yaml_present(item.second)) throw ManifestError(field_name + "." + kind + " must be a string or list");
        out[kind] = yaml_string_tuple(item.second);
    }
    return out;
}

bool is_known_public_contract(const std::string& contract) {
    try {
        if (starts_with(contract, "api.")) {
            return contract_loader::find_api_contract(contract) != nullptr;
        }
        if (starts_with(contract, "component.")) {
            return contract_loader::find_component_contract(contract) != nullptr;
        }
    } catch (const std::exception&) {
        return false;
    }
    return false;
}

// Capability contracts are not part of the 1.0 dictionary. A declaration that
// still names one is rejected instead of being silently ignored.
void reject_capability_contract(const std::string& contract, const std::string& context) {
    if (starts_with(contract, "capability.")) {
        throw ManifestError(context + " " + python_repr(contract)
                            + " is not supported; capability contracts were removed, declare an api.* or component.* contract");
    }
}

std::string normalize_contract_policy(std::string raw) {
    raw = trim_copy(std::move(raw));
    std::transform(raw.begin(), raw.end(), raw.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (raw == "warn" || raw == "off") {
        return raw;
    }
    return "strict";
}

std::string contract_policy_from_env() {
    const char* raw = std::getenv("R2KERNEL_CONTRACT_POLICY");
    if (raw == nullptr) {
        return "strict";
    }
    return normalize_contract_policy(raw);
}

// Python _parse_interface_declaration.
ManifestInterfaceStatus parse_manifest_interface(
    const std::string& section,
    const std::string& name,
    const YAML::Node& node,
    bool default_required
) {
    const std::string field = section + "." + name;
    ManifestInterfaceStatus status;
    status.name = name;
    YAML::Node mapping(YAML::NodeType::Map);
    if (node.IsScalar()) {
        status.contract = yaml_string(node);
    } else if (node.IsMap()) {
        mapping = node;
        if (node["capability"]) {
            throw ManifestError(field + " field `capability` is not supported; capability contracts were removed, "
                                "declare an api.* or component.* contract");
        }
        status.contract = yaml_string(node["contract"]);
    } else {
        throw ManifestError(field + " must be a contract string or mapping");
    }
    if (status.contract.empty()) throw ManifestError(field + " is missing contract");
    reject_capability_contract(status.contract, field + " contract");
    status.visibility = yaml_string(mapping["visibility"]);
    if (status.visibility.empty()) status.visibility = "public";
    if (status.visibility != "public" && status.visibility != "internal") {
        throw ManifestError(field + " has unsupported visibility " + python_repr(status.visibility)
                            + "; expected one of public, internal");
    }
    if (status.visibility == "public" && !is_known_public_contract(status.contract)) {
        throw ManifestError(field + " uses unknown public contract " + python_repr(status.contract)
                            + "; public subsystem interfaces must reference a R2Kernel spec contract "
                              "(component.* or api.*). Use visibility: internal for private/tutorial wiring.");
    }
    status.labels = yaml_string_mapping(mapping["labels"], field + ".labels");
    status.endpoints = yaml_endpoint_mapping(mapping["endpoints"], field + ".endpoints");
    if (status.visibility == "public" && !status.endpoints.empty()) {
        throw ManifestError(field + " declares endpoints on a public interface; "
                                    "public interface endpoints are defined by the referenced R2Kernel spec contract");
    }
    status.required = yaml_bool(mapping["required"], default_required);
    status.cardinality = yaml_string(mapping["cardinality"]);
    if (status.cardinality.empty()) status.cardinality = "one";
    return status;
}

std::map<std::string, ManifestInterfaceStatus> parse_manifest_interface_section(
    const YAML::Node& node,
    const std::string& section
) {
    std::map<std::string, ManifestInterfaceStatus> out;
    if (!yaml_present(node)) {
        return out;
    }
    if (!node.IsMap()) {
        throw ManifestError(section + " must be a mapping");
    }
    for (const auto& item : node) {
        const std::string name = yaml_string(item.first);
        if (name.empty()) {
            throw ManifestError(section + " contains an empty interface name");
        }
        out[name] = parse_manifest_interface(section, name, item.second, true);
    }
    return out;
}

// Python _parse_export_declarations.
std::map<std::string, ManifestExportStatus> parse_manifest_exports(
    const YAML::Node& node,
    const std::string& role,
    const std::map<std::string, ManifestInterfaceStatus>& requirements,
    const std::filesystem::path& path
) {
    std::map<std::string, ManifestExportStatus> out;
    if (!yaml_present(node)) {
        return out;
    }
    if (role != "compound") {
        throw ManifestError("Only compound subsystems may declare exports: " + path.string());
    }
    if (!node.IsMap()) {
        throw ManifestError("exports must be a mapping: " + path.string());
    }
    for (const auto& item : node) {
        const std::string name = yaml_string(item.first);
        const YAML::Node declaration = item.second;
        if (name.empty() || !declaration.IsMap()) {
            throw ManifestError("exports contains an invalid declaration: " + path.string());
        }
        const std::string contract = yaml_string(declaration["contract"]);
        reject_capability_contract(contract, "exports." + name + " contract");
        if (!starts_with(contract, "api.") || !is_known_public_contract(contract)) {
            throw ManifestError("exports." + name + ".contract must reference a known api.* contract: "
                                + python_repr(contract));
        }
        const YAML::Node source = first_truthy({declaration["from"], declaration["source"]});
        if (!source.IsMap()) throw ManifestError("exports." + name + ".from must be a mapping");
        const std::string requirement = yaml_string(source["requirement"]);
        const auto required = requirements.find(requirement);
        if (required == requirements.end()) {
            throw ManifestError("exports." + name + ".from.requirement references unknown requirement "
                                + python_repr(requirement));
        }
        if (required->second.visibility != "public" || !starts_with(required->second.contract, "component.")) {
            throw ManifestError("exports." + name + " must project a public component.* requirement, got "
                                + required->second.contract);
        }
        const auto component = contract_loader::find_component_contract(required->second.contract);
        if (component == nullptr)
            throw ManifestError("Component spec is not available: " + required->second.contract);
        if (std::find(component->conformance_apis.begin(), component->conformance_apis.end(), contract)
            == component->conformance_apis.end()) {
            throw ManifestError("exports." + name + " selects " + contract + ", but " + required->second.contract
                                + " does not guarantee it in conformance_apis");
        }
        out.emplace(name, ManifestExportStatus{name, contract, requirement});
    }
    return out;
}

std::optional<std::pair<std::string, std::string>> split_binding_target(const std::string& value) {
    const std::string text = trim_copy(value);
    const auto pos = text.rfind('.');
    if (pos == std::string::npos || pos == 0 || pos + 1 >= text.size()) {
        return std::nullopt;
    }
    return std::make_pair(text.substr(0, pos), text.substr(pos + 1));
}

std::unordered_map<std::string, std::string> load_launch_bindings_env() {
    std::unordered_map<std::string, std::string> out;
    const char* raw = std::getenv("R2KERNEL_BINDINGS");
    if (raw == nullptr || *raw == '\0') {
        return out;
    }
    YAML::Node doc = YAML::Load(raw);
    if (!doc || !doc.IsMap()) {
        throw std::runtime_error("R2KERNEL_BINDINGS must be a JSON object");
    }
    for (const auto& item : doc) {
        const std::string requirement = yaml_string(item.first);
        const std::string target = yaml_string(item.second);
        if (requirement.empty()) {
            throw std::runtime_error("R2KERNEL_BINDINGS contains an empty requirement name");
        }
        if (!split_binding_target(target).has_value()) {
            throw std::runtime_error("R2KERNEL_BINDINGS." + requirement + " must use '<subsystem>.<interface>'");
        }
        out[requirement] = target;
    }
    return out;
}

void append_unique(std::vector<std::string>& values, const std::string& value) {
    if (value.empty()) {
        return;
    }
    if (std::find(values.begin(), values.end(), value) == values.end()) {
        values.push_back(value);
    }
}

// Parses and validates the subsystem manifest file at path with the contract
// specs currently in scope (Python _load_subsystem_manifest).
SubsystemManifest parse_subsystem_manifest_file(const std::filesystem::path& path) {
    const YAML::Node doc = load_yaml_mapping(path, "subsystem manifest");
    SubsystemManifest manifest;
    manifest.path = path;
    manifest.kind = yaml_string(doc["kind"]);
    if (manifest.kind != "r2kernel.subsystem")
        throw ManifestError("Subsystem manifest kind must be 'r2kernel.subsystem': " + path.string());
    manifest.version = yaml_version(doc["version"]);
    const YAML::Node subsystem = doc["subsystem"];
    if (!subsystem.IsMap()) throw ManifestError("Subsystem manifest must define subsystem mapping: " + path.string());
    manifest.name = yaml_string(subsystem["name"]);
    if (manifest.name.empty()) throw ManifestError("Subsystem manifest is missing subsystem.name: " + path.string());
    manifest.role = yaml_string(subsystem["role"]);
    if (manifest.role.empty()) throw ManifestError("Subsystem manifest is missing subsystem.role: " + path.string());
    if (manifest.role != "app" && manifest.role != "component" && manifest.role != "gateway" && manifest.role != "compound") {
        throw ManifestError("Subsystem " + manifest.name + " has unsupported role " + python_repr(manifest.role)
                            + "; expected one of " + kSubsystemRoles);
    }
    manifest.requirements = parse_manifest_interface_section(doc["requires"], "requires");
    manifest.provides = parse_manifest_interface_section(doc["provides"], "provides");
    manifest.exports = parse_manifest_exports(doc["exports"], manifest.role, manifest.requirements, path);
    std::vector<std::string> overlap;
    for (const auto& [name, _] : manifest.exports)
        if (manifest.provides.count(name) != 0) overlap.push_back(name);
    if (!overlap.empty()) {
        throw ManifestError("Subsystem " + manifest.name + " declares the same interface in provides and exports: "
                            + join_names(overlap));
    }
    manifest.spatial = parse_spatial_declaration(doc["spatial"], path);
    validate_manifest_role_policy(manifest);
    validate_subsystem_spatial_conformance(manifest);
    return manifest;
}

std::string json_string_array(const std::vector<std::string>& values) {
    std::ostringstream out;
    out << "[";
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i > 0) {
            out << ",";
        }
        out << "\"" << json_escape(values[i]) << "\"";
    }
    out << "]";
    return out.str();
}

std::string frame_transform_json(const FrameTransform& transform) {
    std::ostringstream out;
    out << "{"
        << "\"parent_frame\":\"" << json_escape(transform.parent_frame) << "\","
        << "\"child_frame\":\"" << json_escape(transform.child_frame) << "\","
        << "\"translation\":[" << transform.translation[0] << "," << transform.translation[1] << ","
        << transform.translation[2] << "],"
        << "\"rotation_xyzw\":[" << transform.rotation_xyzw[0] << "," << transform.rotation_xyzw[1]
        << "," << transform.rotation_xyzw[2] << "," << transform.rotation_xyzw[3] << "],"
        << "\"observed_at_sync_ns\":" << transform.observed_at_sync_ns << ","
        << "\"static\":" << (transform.is_static ? "true" : "false") << ","
        << "\"stale_after_ms\":" << (transform.is_static ? 0U : transform.stale_after_ms)
        << "}";
    return out.str();
}

std::string frame_transforms_json(const std::vector<FrameTransform>& transforms) {
    std::ostringstream out;
    out << "[";
    for (std::size_t index = 0; index < transforms.size(); ++index) {
        if (index > 0) {
            out << ",";
        }
        out << frame_transform_json(transforms[index]);
    }
    out << "]";
    return out.str();
}

std::string json_string_map_object(const std::map<std::string, std::string>& values) {
    std::ostringstream out;
    out << "{";
    bool first = true;
    for (const auto& [key, value] : values) {
        if (!first) {
            out << ",";
        }
        first = false;
        out << "\"" << json_escape(key) << "\":\"" << json_escape(value) << "\"";
    }
    out << "}";
    return out.str();
}

std::string json_endpoint_map_object(const std::map<std::string, std::vector<std::string>>& values) {
    std::ostringstream out;
    out << "{";
    bool first = true;
    for (const auto& [key, endpoints] : values) {
        if (!first) {
            out << ",";
        }
        first = false;
        out << "\"" << json_escape(key) << "\":" << json_string_array(endpoints);
    }
    out << "}";
    return out.str();
}

std::string interface_status_json(const ManifestInterfaceStatus& status) {
    auto endpoints = status.endpoints;
    if (status.visibility == "public") {
        for (const auto& api_id : interface_api_ids(status)) {
            const auto api = contract_loader::find_api_contract(api_id);
            if (!api) throw std::runtime_error("Public interface contract projection is unavailable: " + api_id);
            for (const auto& control : api->controls) append_unique(endpoints["controls"], control.topic);
            for (const auto& procedure : api->procedures) append_unique(endpoints["procedures"], procedure.name);
            for (const auto& operation : api->operations) append_unique(endpoints["operations"], operation.name);
            for (const auto& event : api->events) append_unique(endpoints[event.direction == "publish" ? "publishes" : "subscribes"], event.topic);
        }
    }
    std::ostringstream out;
    out << "{"
        << "\"contract\":\"" << json_escape(status.contract) << "\","
        << "\"visibility\":\"" << json_escape(status.visibility) << "\","
        << "\"required\":" << (status.required ? "true" : "false") << ","
        << "\"cardinality\":\"" << json_escape(status.cardinality) << "\","
        << "\"labels\":" << json_string_map_object(status.labels) << ","
        << "\"endpoints\":" << json_endpoint_map_object(endpoints);
    out << "}";
    return out.str();
}

std::string interface_section_json(const std::map<std::string, ManifestInterfaceStatus>& values) {
    std::ostringstream out;
    out << "{";
    bool first = true;
    for (const auto& [name, status] : values) {
        if (!first) {
            out << ",";
        }
        first = false;
        out << "\"" << json_escape(name) << "\":" << interface_status_json(status);
    }
    out << "}";
    return out.str();
}

std::string export_section_json(const std::map<std::string, ManifestExportStatus>& values) {
    std::ostringstream out;
    out << "{";
    bool first = true;
    for (const auto& [name, status] : values) {
        if (!first) {
            out << ",";
        }
        first = false;
        out << "\"" << json_escape(name) << "\":{"
            << "\"contract\":\"" << json_escape(status.contract) << "\","
            << "\"source\":{\"requirement\":\"" << json_escape(status.requirement) << "\"},"
            << "\"declarative\":true}";
    }
    out << "}";
    return out.str();
}

std::string build_manifest_status_json(
    const SubsystemManifest& manifest,
    const std::unordered_map<std::string, std::string>& launch_bindings,
    const DeploymentManifest* deployment = nullptr
) {
    if (manifest.name.empty()) {
        return {};
    }
    std::ostringstream bindings;
    std::vector<std::string> binding_errors;
    bindings << "[";
    bool first_binding = true;
    for (const auto& [requirement, declaration] : manifest.requirements) {
        const auto binding = launch_bindings.find(requirement);
        if (binding == launch_bindings.end() && deployment != nullptr) {
            // Without a launch binding, a deployment manifest resolves it.
            try {
                const BoundInterface resolved = deployment->resolve(manifest.name, requirement);
                if (!first_binding) {
                    bindings << ",";
                }
                first_binding = false;
                bindings << "{"
                         << "\"requirement\":\"" << json_escape(resolved.requirement) << "\","
                         << "\"source\":\"" << json_escape(manifest.name + "." + resolved.requirement) << "\","
                         << "\"target\":\"" << json_escape(resolved.target_subsystem + "." + resolved.provider_interface) << "\","
                         << "\"target_subsystem\":\"" << json_escape(resolved.target_subsystem) << "\","
                         << "\"provider_interface\":\"" << json_escape(resolved.provider_interface) << "\","
                         << "\"contract\":\"" << json_escape(resolved.contract) << "\""
                         << "}";
            } catch (const ManifestError& error) {
                if (declaration.required) {
                    binding_errors.push_back(error.what());
                }
            }
            continue;
        }
        if (binding == launch_bindings.end()) {
            if (declaration.required) {
                binding_errors.push_back("Required binding is missing: " + manifest.name + "." + requirement);
            }
            continue;
        }
        const auto target = split_binding_target(binding->second);
        if (!target.has_value()) {
            if (declaration.required) {
                binding_errors.push_back("Invalid binding target: " + manifest.name + "." + requirement);
            }
            continue;
        }
        if (!first_binding) {
            bindings << ",";
        }
        first_binding = false;
        bindings << "{"
                 << "\"requirement\":\"" << json_escape(requirement) << "\","
                 << "\"source\":\"" << json_escape(manifest.name + "." + requirement) << "\","
                 << "\"target\":\"" << json_escape(binding->second) << "\","
                 << "\"target_subsystem\":\"" << json_escape(target->first) << "\","
                 << "\"provider_interface\":\"" << json_escape(target->second) << "\","
                 << "\"contract\":\"" << json_escape(declaration.contract) << "\""
                 << "}";
    }
    bindings << "]";

    std::ostringstream out;
    out << "\"manifest\":{"
        << "\"kind\":\"" << json_escape(manifest.kind) << "\","
        << "\"version\":" << manifest.version << ","
        << "\"path\":\"" << json_escape(manifest.path.string()) << "\","
        << "\"name\":\"" << json_escape(manifest.name) << "\","
        << "\"role\":\"" << json_escape(manifest.role) << "\""
        << "},"
        << "\"spatial\":" << spatial_status_json(manifest.spatial) << ","
        << "\"interfaces\":{"
        << "\"provides\":" << interface_section_json(manifest.provides) << ","
        << "\"requires\":" << interface_section_json(manifest.requirements) << ","
        << "\"exports\":" << export_section_json(manifest.exports)
        << "},"
        << "\"bindings\":" << bindings.str();
    if (!binding_errors.empty()) {
        out << ",\"binding_errors\":" << json_string_array(binding_errors);
    }
    return out.str();
}

std::string type_basename(std::string_view ref) {
    const auto slash = ref.rfind('/');
    if (slash == std::string_view::npos) {
        return std::string(ref);
    }
    return std::string(ref.substr(slash + 1));
}

bool matches_type_ref(std::string_view actual, std::string_view expected) {
    const std::string lhs = trim_copy(std::string(actual));
    const std::string rhs = trim_copy(std::string(expected));
    if (lhs.empty() || rhs.empty()) {
        return false;
    }
    if (lhs == rhs) {
        return true;
    }
    return type_basename(lhs) == type_basename(rhs);
}

std::shared_ptr<const ApiContractSpec> find_api_contract(std::string_view id) {
    return contract_loader::find_api_contract(id);
}

std::shared_ptr<const ComponentContractSpec> find_component_contract(std::string_view id) {
    return contract_loader::find_component_contract(id);
}

void add_api_endpoints(AllowedEndpointSets& allowed, const ApiContractSpec& api, bool invert_events) {
    for (const auto& event : api.events) {
        if ((event.direction == "subscribe") == invert_events) {
            allowed.publishes.insert(event.topic);
        } else {
            allowed.subscribes.insert(event.topic);
        }
    }
    if (invert_events) {
        return;
    }
    for (const auto& endpoint : api.controls) {
        allowed.controls.insert(endpoint.topic);
    }
    for (const auto& procedure : api.procedures) {
        allowed.procedures.insert(procedure.name);
    }
    for (const auto& operation : api.operations) {
        allowed.operations.insert(operation.name);
    }
}

std::vector<std::string> interface_api_ids(
    const ManifestInterfaceStatus& declaration,
    bool include_internal_contracts
) {
    std::vector<std::string> api_ids;
    if (declaration.visibility != "public" && !include_internal_contracts) {
        return api_ids;
    }
    if (starts_with(declaration.contract, "api.")) {
        api_ids.push_back(declaration.contract);
    } else if (starts_with(declaration.contract, "component.")) {
        if (const auto component = contract_loader::find_component_contract(declaration.contract)) {
            api_ids.insert(api_ids.end(), component->conformance_apis.begin(), component->conformance_apis.end());
        }
    }
    return api_ids;
}

// Python _strict_contract_control_endpoints: the Control topics a public
// contract carries; a missing spec is an error.
std::vector<std::string> strict_contract_control_endpoints(const std::string& contract, const std::string& field_name) {
    const auto inspect_error = [&](const std::string& detail) {
        return ManifestError("Cannot inspect contract " + python_repr(contract) + " for " + field_name + ": " + detail);
    };
    std::vector<std::string> api_ids;
    try {
        if (starts_with(contract, "api.")) {
            api_ids.push_back(contract);
        } else if (starts_with(contract, "component.")) {
            const auto component = contract_loader::find_component_contract(contract);
            if (component == nullptr) throw inspect_error("component spec not found");
            api_ids = component->conformance_apis;
        } else {
            return {};
        }
        std::vector<std::string> controls;
        for (const auto& api_id : api_ids) {
            const auto api = contract_loader::find_api_contract(api_id);
            if (api == nullptr) throw inspect_error("API spec not found: " + api_id);
            for (const auto& control : api->controls) append_unique(controls, control.topic);
        }
        return controls;
    } catch (const ManifestError&) {
        throw;
    } catch (const std::exception& error) {
        throw inspect_error(error.what());
    }
}

// Python _gateway_contract_endpoint_inventory: the API contracts a Gateway's
// public contract exposes.
std::vector<std::shared_ptr<const ApiContractSpec>> gateway_contract_apis(const std::string& contract,
                                                                          const std::string& field_name) {
    const auto inspect_error = [&](const std::string& detail) {
        return ManifestError("Cannot inspect contract " + python_repr(contract) + " for Gateway " + field_name + ": " + detail);
    };
    std::vector<std::string> api_ids;
    try {
        if (starts_with(contract, "api.")) {
            api_ids.push_back(contract);
        } else if (starts_with(contract, "component.")) {
            const auto component = contract_loader::find_component_contract(contract);
            if (component == nullptr) throw inspect_error("component spec not found");
            api_ids = component->conformance_apis;
        } else {
            throw inspect_error("Unknown public contract kind: " + python_repr(contract));
        }
        if (api_ids.empty()) throw inspect_error("Public contract " + python_repr(contract) + " does not expose any API contracts");
        std::vector<std::shared_ptr<const ApiContractSpec>> apis;
        for (const auto& api_id : api_ids) {
            auto api = contract_loader::find_api_contract(api_id);
            if (api == nullptr) throw inspect_error("API spec not found: " + api_id);
            apis.push_back(std::move(api));
        }
        return apis;
    } catch (const ManifestError&) {
        throw;
    } catch (const std::exception& error) {
        throw inspect_error(error.what());
    }
}

// Python _validate_role_endpoint_policy: compounds are declarative, Gateways
// use only Control endpoints, apps and compounds use none.
void validate_manifest_role_policy(const SubsystemManifest& manifest) {
    if (manifest.role == "compound" && !manifest.provides.empty()) {
        throw ManifestError("Compound subsystem " + manifest.name
                            + " is declarative and cannot declare provides interfaces");
    }
    const std::vector<std::pair<std::string, const std::map<std::string, ManifestInterfaceStatus>*>> sections {
        {"provides", &manifest.provides}, {"requires", &manifest.requirements}};
    if (manifest.role == "gateway") {
        for (const auto& [section, interfaces] : sections) {
            for (const auto& [name, declaration] : *interfaces) {
                const std::string field = section + "." + name;
                std::vector<std::string> non_control;
                for (const auto& [kind, endpoints] : declaration.endpoints)
                    if (kind != "controls" && !endpoints.empty()) non_control.push_back(kind);
                if (!non_control.empty()) {
                    throw ManifestError("Gateway may use only Control endpoints: " + field + " declares "
                                        + join_names(non_control));
                }
                if (declaration.visibility == "internal" && !starts_with(declaration.contract, "api.")
                    && !starts_with(declaration.contract, "component.")) {
                    const auto controls = declaration.endpoints.find("controls");
                    if (controls == declaration.endpoints.end() || controls->second.empty()) {
                        throw ManifestError("Gateway private contract " + python_repr(declaration.contract) + " in "
                                            + field + " requires explicit internal Control endpoints");
                    }
                    continue;
                }
                std::vector<std::string> controls, events, procedures, operations;
                for (const auto& api : gateway_contract_apis(declaration.contract, field)) {
                    for (const auto& control : api->controls) append_unique(controls, control.topic);
                    for (const auto& event : api->events) append_unique(events, event.topic);
                    for (const auto& procedure : api->procedures) append_unique(procedures, procedure.name);
                    for (const auto& operation : api->operations) append_unique(operations, operation.name);
                }
                if (controls.empty()) {
                    throw ManifestError("Gateway public contract " + python_repr(declaration.contract) + " in " + field
                                        + " must declare at least one Control endpoint");
                }
                for (const auto* names : {&events, &procedures, &operations})
                    non_control.insert(non_control.end(), names->begin(), names->end());
                if (!non_control.empty()) {
                    throw ManifestError("Gateway may use only Control endpoints: " + field + " (" + declaration.contract
                                        + ") also declares " + join_names(non_control));
                }
            }
        }
    }
    if (manifest.role == "compound") {
        for (const auto& [name, declaration] : manifest.requirements) {
            if (!starts_with(declaration.contract, "component.")) {
                throw ManifestError("Compound requirement " + python_repr(name)
                                    + " must reference a component.* contract; got " + python_repr(declaration.contract));
            }
        }
    }
    if (manifest.role != "app" && manifest.role != "compound") {
        return;
    }
    for (const auto& [section, interfaces] : sections) {
        for (const auto& [name, declaration] : *interfaces) {
            const std::string field = section + "." + name;
            const auto explicit_controls = declaration.endpoints.find("controls");
            if (explicit_controls != declaration.endpoints.end() && !explicit_controls->second.empty()) {
                throw ManifestError("Subsystem " + manifest.name + " with role " + python_repr(manifest.role)
                                    + " cannot declare or consume internal Control endpoints in " + field);
            }
            const auto controls = strict_contract_control_endpoints(declaration.contract, field);
            if (controls.empty()) continue;
            if (manifest.role == "compound" && section == "requires" && starts_with(declaration.contract, "component.")) {
                continue;
            }
            throw ManifestError("Subsystem " + manifest.name + " with role " + python_repr(manifest.role)
                                + " cannot declare or consume Control endpoints through " + field + " ("
                                + declaration.contract + "): " + join_names(controls));
        }
    }
    if (manifest.role == "compound") {
        for (const auto& [name, declaration] : manifest.exports) {
            const auto controls = strict_contract_control_endpoints(declaration.contract, "exports." + name);
            if (!controls.empty()) {
                throw ManifestError("Compound subsystem " + manifest.name + " cannot export Control endpoints through exports."
                                    + name + " (" + declaration.contract + "): " + join_names(controls));
            }
        }
    }
}

void add_manifest_interface_endpoints(
    AllowedEndpointSets& allowed,
    const ManifestInterfaceStatus& declaration,
    bool requires_side
) {
    if (declaration.visibility == "public") {
        for (const auto& api_id : interface_api_ids(declaration)) {
            if (const auto api = contract_loader::find_api_contract(api_id)) {
                add_api_endpoints(allowed, *api, requires_side);
            }
        }
        return;
    }

    auto add_all = [](std::unordered_set<std::string>& dest, const std::vector<std::string>& values) {
        for (const auto& value : values) {
            if (!value.empty()) {
                dest.insert(value);
            }
        }
    };

    const auto publishes = declaration.endpoints.find("publishes");
    const auto subscribes = declaration.endpoints.find("subscribes");
    if (requires_side) {
        if (publishes != declaration.endpoints.end()) {
            add_all(allowed.subscribes, publishes->second);
        }
        if (subscribes != declaration.endpoints.end()) {
            add_all(allowed.publishes, subscribes->second);
        }
        return;
    }
    if (publishes != declaration.endpoints.end()) {
        add_all(allowed.publishes, publishes->second);
    }
    if (subscribes != declaration.endpoints.end()) {
        add_all(allowed.subscribes, subscribes->second);
    }
    if (const auto it = declaration.endpoints.find("controls"); it != declaration.endpoints.end()) {
        add_all(allowed.controls, it->second);
    }
    if (const auto it = declaration.endpoints.find("procedures"); it != declaration.endpoints.end()) {
        add_all(allowed.procedures, it->second);
    }
    if (const auto it = declaration.endpoints.find("operations"); it != declaration.endpoints.end()) {
        add_all(allowed.operations, it->second);
    }
}

AllowedEndpointSets allowed_endpoint_sets(const SubsystemManifest& manifest) {
    AllowedEndpointSets allowed;
    for (const auto& [_, declaration] : manifest.provides) {
        add_manifest_interface_endpoints(allowed, declaration, false);
    }
    for (const auto& [_, declaration] : manifest.requirements) {
        add_manifest_interface_endpoints(allowed, declaration, true);
    }
    return allowed;
}
