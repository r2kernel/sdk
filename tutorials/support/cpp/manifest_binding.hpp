#pragma once

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace r2kernel::tutorial {

struct BindingTarget {
    std::string subsystem;
    std::string interface_name;
    std::string contract;

    std::string ref() const {
        return subsystem + "." + interface_name;
    }
};

namespace manifest_detail {

struct InterfaceDeclarationLite {
    std::string contract;
    std::string visibility = "public";
};

struct ExportDeclarationLite {
    std::string contract;
    std::string requirement;
};

struct SubsystemManifestLite {
    std::string name;
    std::string role;
    std::map<std::string, InterfaceDeclarationLite> provides;
    std::map<std::string, InterfaceDeclarationLite> requires;
    std::map<std::string, ExportDeclarationLite> exports;
};

struct DeploymentManifestLite {
    std::map<std::string, std::filesystem::path> subsystem_manifests;
    std::map<std::string, bool> strict_manifest_names;
    std::map<std::string, std::string> bindings;
};

inline std::string trim_copy(const std::string& value) {
    const auto begin = std::find_if_not(value.begin(), value.end(), [](unsigned char ch) {
        return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
    });
    const auto end = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char ch) {
        return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
    }).base();
    if (begin >= end) {
        return {};
    }
    return std::string(begin, end);
}

inline int leading_spaces(const std::string& value) {
    int count = 0;
    for (char ch : value) {
        if (ch != ' ') {
            break;
        }
        ++count;
    }
    return count;
}

inline std::string strip_yaml_value(std::string value) {
    value = trim_copy(value);
    if (value.size() >= 2) {
        const char first = value.front();
        const char last = value.back();
        if ((first == '"' && last == '"') || (first == '\'' && last == '\'')) {
            return value.substr(1, value.size() - 2);
        }
    }
    return value;
}

inline std::string without_comment(std::string value) {
    if (!value.empty() && value.back() == '\r') {
        value.pop_back();
    }
    const auto comment = value.find('#');
    if (comment != std::string::npos) {
        value.resize(comment);
    }
    return value;
}

inline std::vector<std::string> read_lines(const std::filesystem::path& path) {
    std::ifstream stream(path);
    if (!stream) {
        throw std::runtime_error("failed to open manifest: " + path.string());
    }
    std::vector<std::string> lines;
    std::string line;
    while (std::getline(stream, line)) {
        lines.push_back(line);
    }
    return lines;
}

inline std::pair<std::string, std::string> split_ref(const std::string& ref, const std::string& field_name) {
    const auto dot = ref.rfind('.');
    if (dot == std::string::npos || dot == 0 || dot + 1 >= ref.size()) {
        throw std::runtime_error(field_name + " must use <subsystem>.<interface>: " + ref);
    }
    return {ref.substr(0, dot), ref.substr(dot + 1)};
}

inline std::filesystem::path load_package_subsystem_manifest_path(const std::filesystem::path& package_dir) {
    const auto manifest_path = package_dir / "package.yaml";
    std::filesystem::path default_subsystem_manifest = package_dir / "subsystem.yaml";
    for (const auto& raw_line : read_lines(manifest_path)) {
        const std::string line = without_comment(raw_line);
        const std::string text = trim_copy(line);
        if (text.empty() || leading_spaces(line) != 0) {
            continue;
        }
        const auto colon = text.find(':');
        if (colon == std::string::npos) {
            continue;
        }
        const std::string key = trim_copy(text.substr(0, colon));
        const std::string value = strip_yaml_value(text.substr(colon + 1));
        if (key == "subsystem_manifest" && !value.empty()) {
            std::filesystem::path subsystem_manifest = value;
            if (subsystem_manifest.is_relative()) {
                subsystem_manifest = package_dir / subsystem_manifest;
            }
            return subsystem_manifest.lexically_normal();
        }
    }
    return default_subsystem_manifest.lexically_normal();
}

inline SubsystemManifestLite load_subsystem_manifest(const std::filesystem::path& path) {
    SubsystemManifestLite manifest;
    std::string section;
    std::string current_interface;
    bool found_kind = false;

    for (const auto& raw_line : read_lines(path)) {
        const std::string line = without_comment(raw_line);
        const std::string text = trim_copy(line);
        if (text.empty()) {
            continue;
        }

        const int indent = leading_spaces(line);
        const auto colon = text.find(':');
        if (colon == std::string::npos) {
            continue;
        }
        const std::string key = trim_copy(text.substr(0, colon));
        const std::string value = strip_yaml_value(text.substr(colon + 1));

        if (indent == 0) {
            current_interface.clear();
            if (key == "kind") {
                found_kind = true;
                if (value != "r2kernel.subsystem") {
                    throw std::runtime_error("manifest kind must be r2kernel.subsystem: " + path.string());
                }
            }
            if (key == "subsystem" || key == "provides" || key == "requires" || key == "exports") {
                section = key;
            } else {
                section.clear();
            }
            continue;
        }

        if (section == "subsystem" && indent == 2 && key == "name") {
            manifest.name = value;
            continue;
        }
        if (section == "subsystem" && indent == 2 && key == "role") {
            manifest.role = value;
            continue;
        }

        if ((section == "provides" || section == "requires") && indent == 2) {
            current_interface = key;
            auto& interfaces = section == "provides" ? manifest.provides : manifest.requires;
            interfaces.emplace(current_interface, InterfaceDeclarationLite{});
            continue;
        }

        if ((section == "provides" || section == "requires") && indent == 4 && key == "contract" && !current_interface.empty()) {
            auto& interfaces = section == "provides" ? manifest.provides : manifest.requires;
            interfaces[current_interface].contract = value;
            continue;
        }
        if ((section == "provides" || section == "requires") && indent == 4 && key == "visibility" && !current_interface.empty()) {
            auto& interfaces = section == "provides" ? manifest.provides : manifest.requires;
            interfaces[current_interface].visibility = value;
            continue;
        }

        if (section == "exports" && indent == 2) {
            current_interface = key;
            manifest.exports.emplace(current_interface, ExportDeclarationLite{});
            continue;
        }
        if (section == "exports" && indent == 4 && key == "contract" && !current_interface.empty()) {
            manifest.exports[current_interface].contract = value;
            continue;
        }
        if (section == "exports" && indent == 6 && key == "requirement" && !current_interface.empty()) {
            manifest.exports[current_interface].requirement = value;
        }
    }

    if (!found_kind) {
        throw std::runtime_error("missing r2kernel.subsystem kind: " + path.string());
    }
    if (manifest.name.empty()) {
        throw std::runtime_error("missing subsystem.name in manifest: " + path.string());
    }
    return manifest;
}

inline DeploymentManifestLite load_deployment_manifest(const std::filesystem::path& path) {
    DeploymentManifestLite manifest;
    const auto base_dir = path.parent_path();
    std::string section;
    std::string current_subsystem;
    std::string current_binding;
    bool found_kind = false;

    for (const auto& raw_line : read_lines(path)) {
        const std::string line = without_comment(raw_line);
        const std::string text = trim_copy(line);
        if (text.empty()) {
            continue;
        }

        const int indent = leading_spaces(line);
        const auto colon = text.find(':');
        if (colon == std::string::npos) {
            continue;
        }
        const std::string key = trim_copy(text.substr(0, colon));
        const std::string value = strip_yaml_value(text.substr(colon + 1));

        if (indent == 0) {
            current_subsystem.clear();
            current_binding.clear();
            if (key == "kind") {
                found_kind = true;
                if (value != "r2kernel.deployment") {
                    throw std::runtime_error("manifest kind must be r2kernel.deployment: " + path.string());
                }
            }
            if (key == "subsystems" || key == "instances" || key == "bindings") {
                section = key;
            } else {
                section.clear();
            }
            continue;
        }

        if (section == "subsystems" && indent == 2) {
            current_subsystem = key;
            continue;
        }
        if (section == "subsystems" && indent == 4 && key == "manifest" && !current_subsystem.empty()) {
            std::filesystem::path manifest_path = value;
            if (manifest_path.is_relative()) {
                manifest_path = base_dir / manifest_path;
            }
            manifest.subsystem_manifests[current_subsystem] = manifest_path.lexically_normal();
            manifest.strict_manifest_names[current_subsystem] = true;
            continue;
        }

        if (section == "instances" && indent == 2) {
            current_subsystem = key;
            continue;
        }
        if (section == "instances" && indent == 4 && !current_subsystem.empty()) {
            if (key == "manifest") {
                std::filesystem::path manifest_path = value;
                if (manifest_path.is_relative()) {
                    manifest_path = base_dir / manifest_path;
                }
                manifest.subsystem_manifests[current_subsystem] = manifest_path.lexically_normal();
                manifest.strict_manifest_names[current_subsystem] = false;
            } else if (key == "package") {
                std::filesystem::path package_path = value;
                if (package_path.is_relative()) {
                    package_path = base_dir / package_path;
                }
                manifest.subsystem_manifests[current_subsystem] =
                    load_package_subsystem_manifest_path(package_path.lexically_normal());
                manifest.strict_manifest_names[current_subsystem] = false;
            }
            continue;
        }

        if (section == "bindings" && indent == 2) {
            current_binding = key;
            if (!value.empty()) {
                manifest.bindings[current_binding] = value;
            }
            continue;
        }
        if (section == "bindings" && indent == 4 && key == "to" && !current_binding.empty()) {
            manifest.bindings[current_binding] = value;
        }
    }

    if (!found_kind) {
        throw std::runtime_error("missing r2kernel.deployment kind: " + path.string());
    }
    return manifest;
}

inline const InterfaceDeclarationLite& require_interface(
    const std::map<std::string, InterfaceDeclarationLite>& interfaces,
    const std::string& interface_name,
    const std::string& subsystem,
    const std::string& direction
) {
    const auto found = interfaces.find(interface_name);
    if (found == interfaces.end()) {
        throw std::runtime_error("subsystem " + subsystem + " does not " + direction + " interface " + interface_name);
    }
    if (found->second.contract.empty()) {
        throw std::runtime_error("subsystem " + subsystem + " interface " + interface_name + " is missing contract");
    }
    return found->second;
}

inline const ExportDeclarationLite& require_export(
    const std::map<std::string, ExportDeclarationLite>& exports,
    const std::string& interface_name,
    const std::string& subsystem
) {
    const auto found = exports.find(interface_name);
    if (found == exports.end()) {
        throw std::runtime_error("subsystem " + subsystem + " does not export interface " + interface_name);
    }
    if (found->second.contract.empty() || found->second.requirement.empty()) {
        throw std::runtime_error("subsystem " + subsystem + " export " + interface_name + " is incomplete");
    }
    return found->second;
}

inline void validate_manifest_name(const SubsystemManifestLite& manifest, const std::string& expected_name, const std::filesystem::path& path) {
    if (manifest.name != expected_name) {
        throw std::runtime_error(
            "manifest subsystem.name mismatch for " + path.string() +
            ": expected " + expected_name + ", got " + manifest.name
        );
    }
}

inline void validate_manifest_name_if_strict(
    const DeploymentManifestLite& deployment,
    const SubsystemManifestLite& manifest,
    const std::string& expected_name,
    const std::filesystem::path& path
) {
    const auto strict = deployment.strict_manifest_names.find(expected_name);
    if (strict == deployment.strict_manifest_names.end() || strict->second) {
        validate_manifest_name(manifest, expected_name, path);
    }
}

}  // namespace manifest_detail

inline std::filesystem::path default_deployment_manifest_path(const char* source_file) {
    return std::filesystem::path(source_file).parent_path().parent_path() / "r2kernel.deployment.yaml";
}

inline std::filesystem::path default_subsystem_manifest_path(const char* source_file, const std::string& name) {
    const auto directory = std::filesystem::path(source_file).parent_path();
    const auto fixed_name = directory / "subsystem.yaml";
    if (std::filesystem::exists(fixed_name)) {
        return fixed_name;
    }
    return directory / (name + ".subsystem.yaml");
}

inline void validate_subsystem_provides(
    const std::filesystem::path& manifest_path,
    const std::string& subsystem_name,
    const std::string& interface_name
) {
    const auto manifest = manifest_detail::load_subsystem_manifest(manifest_path);
    manifest_detail::validate_manifest_name(manifest, subsystem_name, manifest_path);
    (void)manifest_detail::require_interface(manifest.provides, interface_name, subsystem_name, "provide");
}

inline BindingTarget resolve_binding_target_impl(
    const manifest_detail::DeploymentManifestLite& deployment,
    const std::string& source_ref,
    const std::string& required_contract,
    const std::string& target_ref,
    std::vector<std::string> export_path
) {
    if (std::find(export_path.begin(), export_path.end(), target_ref) != export_path.end()) {
        throw std::runtime_error("export binding cycle at " + target_ref);
    }
    const auto [target_subsystem, target_interface] = manifest_detail::split_ref(target_ref, "binding target");
    const auto target_path = deployment.subsystem_manifests.find(target_subsystem);
    if (target_path == deployment.subsystem_manifests.end()) {
        throw std::runtime_error("deployment is missing subsystem manifest for " + target_subsystem);
    }
    const auto target = manifest_detail::load_subsystem_manifest(target_path->second);
    manifest_detail::validate_manifest_name_if_strict(deployment, target, target_subsystem, target_path->second);

    const auto provided = target.provides.find(target_interface);
    if (provided != target.provides.end()) {
        if (provided->second.contract != required_contract) {
            throw std::runtime_error(
                "binding contract mismatch for " + source_ref + " -> " + target_ref
                + ": required " + required_contract + ", provided " + provided->second.contract
            );
        }
        if (!export_path.empty()) {
            if (target.role != "component") {
                throw std::runtime_error("export target must be provided by a component subsystem: " + target_ref);
            }
            if (provided->second.visibility != "public") {
                throw std::runtime_error("export target must be public: " + target_ref);
            }
        }
        return BindingTarget{target_subsystem, target_interface, required_contract};
    }

    const auto& exported = manifest_detail::require_export(target.exports, target_interface, target_subsystem);
    if (target.role != "compound") {
        throw std::runtime_error("only a compound subsystem may supply an export: " + target_ref);
    }
    if (exported.contract != required_contract) {
        throw std::runtime_error(
            "binding contract mismatch for " + source_ref + " -> " + target_ref
            + ": required " + required_contract + ", exported " + exported.contract
        );
    }
    const auto& inner_requirement = manifest_detail::require_interface(
        target.requires,
        exported.requirement,
        target_subsystem,
        "require"
    );
    if (inner_requirement.visibility != "public" || inner_requirement.contract.rfind("component.", 0) != 0) {
        throw std::runtime_error("export must project a public component.* requirement: " + target_ref);
    }
    const std::string inner_source = target_subsystem + "." + exported.requirement;
    const auto inner_binding = deployment.bindings.find(inner_source);
    if (inner_binding == deployment.bindings.end()) {
        throw std::runtime_error("missing deployment binding for export source " + inner_source);
    }
    export_path.push_back(target_ref);
    auto resolved = resolve_binding_target_impl(
        deployment,
        inner_source,
        inner_requirement.contract,
        inner_binding->second,
        std::move(export_path)
    );
    resolved.contract = required_contract;
    return resolved;
}

inline BindingTarget resolve_binding_target(
    const std::filesystem::path& deployment_manifest_path,
    const std::string& consumer_subsystem,
    const std::string& required_interface
) {
    const auto deployment = manifest_detail::load_deployment_manifest(deployment_manifest_path);
    const std::string source_ref = consumer_subsystem + "." + required_interface;
    const auto bound = deployment.bindings.find(source_ref);
    if (bound == deployment.bindings.end()) {
        throw std::runtime_error("missing deployment binding for " + source_ref);
    }

    const auto consumer_path = deployment.subsystem_manifests.find(consumer_subsystem);
    if (consumer_path == deployment.subsystem_manifests.end()) {
        throw std::runtime_error("deployment is missing subsystem manifest for " + consumer_subsystem);
    }
    const auto consumer = manifest_detail::load_subsystem_manifest(consumer_path->second);
    manifest_detail::validate_manifest_name_if_strict(deployment, consumer, consumer_subsystem, consumer_path->second);

    const auto& required_contract = manifest_detail::require_interface(
        consumer.requires,
        required_interface,
        consumer_subsystem,
        "require"
    );
    return resolve_binding_target_impl(
        deployment,
        source_ref,
        required_contract.contract,
        bound->second,
        {}
    );
}

}  // namespace r2kernel::tutorial
