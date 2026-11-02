// Manifest and binding model (Python SDK r2kernel/manifest/model.py): binding
// references and static deployment resolution. Cold code in its own unit.
#include "r2kernel/wrapper/manifest.hpp"

#include <algorithm>

namespace r2kernel::wrapper {
namespace {

std::string trimmed(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\n\r\f\v");
    if (begin == std::string::npos) return {};
    const auto end = value.find_last_not_of(" \t\n\r\f\v");
    return value.substr(begin, end - begin + 1);
}

// Python repr() of a str, used in manifest error text.
std::string python_repr(const std::string& value) {
    return "'" + value + "'";
}

bool starts_with(const std::string& value, const char* prefix) { return value.rfind(prefix, 0) == 0; }

std::vector<std::string> public_contracts(const SubsystemManifest& manifest, const char* prefix) {
    std::vector<std::string> out;
    for (const auto& [_, declaration] : manifest.provides) {
        if (declaration.visibility != "public" || !starts_with(declaration.contract, prefix)) continue;
        if (std::find(out.begin(), out.end(), declaration.contract) == out.end()) out.push_back(declaration.contract);
    }
    return out;
}

const SubsystemManifest* find_manifest(const DeploymentManifest& deployment, const std::string& name) {
    const auto it = deployment.subsystems.find(name);
    return it == deployment.subsystems.end() ? nullptr : &it->second;
}

BoundInterface resolve_target(const DeploymentManifest& deployment, const BindingReference& source,
                              const InterfaceDeclaration& required, const BindingReference& target,
                              const std::vector<std::string>& export_path) {
    const std::string target_ref = target.format();
    if (std::find(export_path.begin(), export_path.end(), target_ref) != export_path.end()) {
        std::string chain;
        for (const auto& item : export_path) chain += item + " -> ";
        throw ManifestError("Export binding cycle: " + chain + target_ref);
    }
    const SubsystemManifest* provider = find_manifest(deployment, target.subsystem);
    if (provider == nullptr) throw ManifestError("Binding target subsystem is not declared: " + target.subsystem);
    const SubsystemManifest* consumer = find_manifest(deployment, source.subsystem);
    if (consumer != nullptr && (consumer->role == "app" || consumer->role == "compound") && provider->role == "gateway") {
        throw ManifestError("Subsystem " + source.subsystem + " with role " + python_repr(consumer->role)
                            + " cannot bind directly to gateway subsystem " + target.subsystem
                            + "; bind through a component");
    }
    if (consumer != nullptr && consumer->role == "compound" && provider->role != "component") {
        throw ManifestError("Compound subsystem " + source.subsystem
                            + " may bind only to component-provided interfaces; " + target_ref
                            + " is provided by role " + python_repr(provider->role));
    }
    const auto provided = provider->provides.find(target.interface_name);
    const auto exported = provider->exports.find(target.interface_name);
    const bool is_provided = provided != provider->provides.end();
    const bool is_exported = exported != provider->exports.end();
    if (!is_provided && !is_exported)
        throw ManifestError("Binding target does not provide or export interface: " + target_ref);
    const std::string& declared_contract = is_provided ? provided->second.contract : exported->second.contract;
    if (consumer != nullptr && consumer->role == "app" && !is_exported && provider->role != "component") {
        throw ManifestError("Application subsystem " + source.subsystem
                            + " may bind directly only to component-provided interfaces or compound exports; "
                            + target_ref + " is provided by role " + python_repr(provider->role));
    }
    if (required.cardinality != "one")
        throw ManifestError("Unsupported binding cardinality for " + source.format() + ": " + required.cardinality);
    if (required.contract != declared_contract) {
        throw ManifestError("Binding contract mismatch: " + source.format() + " requires " + required.contract
                            + ", but " + target_ref + " provides " + declared_contract);
    }
    if (is_exported) {
        const BindingReference export_source {provider->name, exported->second.requirement};
        const auto export_requirement = provider->requirements.find(exported->second.requirement);
        const auto inner_target = deployment.bindings.find(export_source);
        if (inner_target == deployment.bindings.end())
            throw ManifestError("Export source binding is missing: " + export_source.format());
        if (export_requirement == provider->requirements.end())
            throw ManifestError("Export requirement is not declared: " + export_source.format());
        auto path = export_path;
        path.push_back(target_ref);
        const BoundInterface resolved =
            resolve_target(deployment, export_source, export_requirement->second, inner_target->second, path);
        const SubsystemManifest& physical = deployment.subsystems.at(resolved.target_subsystem);
        const auto physical_interface = physical.provides.find(resolved.provider_interface);
        const std::string physical_ref = resolved.target_subsystem + "." + resolved.provider_interface;
        if (physical_interface == physical.provides.end())
            throw ManifestError("Export target must resolve to a provided interface: " + physical_ref);
        if (physical.role != "component") {
            throw ManifestError("Export target " + physical_ref + " must be provided by a component subsystem, got role "
                                + python_repr(physical.role));
        }
        if (physical_interface->second.visibility != "public")
            throw ManifestError("Export target must be public: " + physical_ref);
        return BoundInterface {source.interface_name, resolved.target_subsystem, resolved.provider_interface,
                               required.contract};
    }
    return BoundInterface {source.interface_name, target.subsystem, target.interface_name, provided->second.contract};
}

}  // namespace

ManifestError::~ManifestError() = default;

std::vector<std::string> SubsystemManifest::declared_apis() const { return public_contracts(*this, "api."); }

std::vector<std::string> SubsystemManifest::declared_components() const {
    return public_contracts(*this, "component.");
}

BindingReference BindingReference::parse(const std::string& value, const std::string& field_name) {
    const std::string text = trimmed(value);
    const auto dot = text.rfind('.');
    if (dot == std::string::npos)
        throw ManifestError(field_name + " must use '<subsystem>.<interface>': " + python_repr(value));
    BindingReference reference {trimmed(text.substr(0, dot)), trimmed(text.substr(dot + 1))};
    if (reference.subsystem.empty() || reference.interface_name.empty())
        throw ManifestError(field_name + " must use '<subsystem>.<interface>': " + python_repr(value));
    return reference;
}

std::string BindingReference::format() const { return subsystem + "." + interface_name; }

BoundInterface DeploymentManifest::resolve(const std::string& consumer, const std::string& requirement,
                                           const std::optional<BindingReference>& target_override) const {
    const SubsystemManifest* consumer_manifest = find_manifest(*this, consumer);
    if (consumer_manifest == nullptr) throw ManifestError("Deployment does not declare subsystem: " + consumer);
    const auto required = consumer_manifest->requirements.find(requirement);
    if (required == consumer_manifest->requirements.end())
        throw ManifestError("Subsystem " + consumer + " does not declare required interface: " + requirement);
    const BindingReference source {consumer, requirement};
    std::optional<BindingReference> target = target_override;
    if (!target) {
        const auto bound = bindings.find(source);
        if (bound != bindings.end()) target = bound->second;
    }
    if (!target) {
        if (required->second.required) throw ManifestError("Required binding is missing: " + source.format());
        throw ManifestError("Optional binding is not configured: " + source.format());
    }
    return resolve_target(*this, source, required->second, *target, {});
}

}  // namespace r2kernel::wrapper
