// Included once, inside subsystem.cpp's anonymous namespace after
// subsystem_platform.hpp (same translation unit). Package-scoped subsystem
// manifests, deployment discovery and the deployment manifest loader of the
// Python SDK (r2kernel/manifest/loader.py load_deployment_manifest and the
// Subsystem's deployment discovery), with the same decisions and messages.
// Cold: used during construction.

// Python Path.expanduser(): a leading ~ or ~user names a home directory.
std::filesystem::path expand_user(const std::filesystem::path& path) {
    const std::string text = path.string();
    if (text.empty() || text[0] != '~') return path;
    const auto slash = text.find('/');
    const std::string user = text.substr(1, slash == std::string::npos ? std::string::npos : slash - 1);
    std::string home;
    if (user.empty()) {
        if (const char* env = std::getenv("HOME"); env != nullptr && *env != '\0') home = env;
    }
#if !defined(_WIN32)
    if (home.empty()) {
        struct passwd entry {};
        struct passwd* resolved = nullptr;
        std::array<char, 16384> buffer {};
        const int rc = user.empty() ? ::getpwuid_r(::getuid(), &entry, buffer.data(), buffer.size(), &resolved)
                                    : ::getpwnam_r(user.c_str(), &entry, buffer.data(), buffer.size(), &resolved);
        if (rc == 0 && resolved != nullptr && resolved->pw_dir != nullptr) home = resolved->pw_dir;
    }
#endif
    if (home.empty()) return path;
    return std::filesystem::path(home + (slash == std::string::npos ? std::string() : text.substr(slash)));
}

// Python Path.resolve() (non-strict): absolute, symbolic links of the
// existing part resolved.
std::filesystem::path resolved_path(const std::filesystem::path& path) {
    std::error_code error;
    auto absolute = std::filesystem::absolute(path, error);
    if (error) return path;
    absolute = absolute.lexically_normal();
    if (absolute.has_relative_path() && absolute.filename().empty()) absolute = absolute.parent_path();
    auto canonical = std::filesystem::weakly_canonical(absolute, error);
    if (error) return absolute;
    if (canonical.has_relative_path() && canonical.filename().empty()) canonical = canonical.parent_path();
    return canonical;
}

// The first existing candidate, resolved (Python _resolve_*_manifest_path).
std::optional<std::filesystem::path> first_existing_path(const std::vector<std::filesystem::path>& candidates) {
    for (const auto& candidate : candidates) {
        const auto path = resolved_path(candidate);
        std::error_code error;
        if (std::filesystem::exists(path, error)) return path;
    }
    return std::nullopt;
}

// Subsystem manifest discovery of the Python SDK: the explicit path (option,
// else R2KERNEL_SUBSYSTEM_MANIFEST), then <name>.subsystem.yaml and
// subsystem.yaml two directories above the resolved user data path, then
// both names in the working directory.
std::optional<std::filesystem::path> first_existing_manifest_path(
    const std::string& name,
    const std::filesystem::path& user_data_path,
    const std::filesystem::path& explicit_path
) {
    std::vector<std::filesystem::path> candidates;
    if (!explicit_path.empty()) {
        candidates.push_back(expand_user(explicit_path));
    } else if (const char* env = std::getenv("R2KERNEL_SUBSYSTEM_MANIFEST"); env != nullptr && *env != '\0') {
        candidates.push_back(expand_user(env));
    }
    const auto root = resolved_path(user_data_path).parent_path().parent_path();
    candidates.push_back(root / (name + ".subsystem.yaml"));
    candidates.push_back(root / "subsystem.yaml");
    std::error_code ec;
    const auto cwd = std::filesystem::current_path(ec);
    if (!ec) {
        candidates.push_back(cwd / (name + ".subsystem.yaml"));
        candidates.push_back(cwd / "subsystem.yaml");
    }
    return first_existing_path(candidates);
}

// Deployment discovery of the Python SDK: the explicit path (option, else
// R2KERNEL_DEPLOYMENT_MANIFEST), then r2kernel.deployment.yaml next to the
// subsystem manifest, in its parent directory and in the working directory.
std::optional<std::filesystem::path> deployment_manifest_location(const std::filesystem::path& explicit_path,
                                                                  const std::filesystem::path& subsystem_manifest) {
    std::vector<std::filesystem::path> candidates;
    if (!explicit_path.empty()) {
        candidates.push_back(expand_user(explicit_path));
    } else if (const char* env = std::getenv("R2KERNEL_DEPLOYMENT_MANIFEST"); env != nullptr && *env != '\0') {
        candidates.push_back(expand_user(env));
    }
    const auto parent = subsystem_manifest.parent_path();
    candidates.push_back(parent / "r2kernel.deployment.yaml");
    candidates.push_back(parent.parent_path() / "r2kernel.deployment.yaml");
    std::error_code cwd_error;
    const auto cwd = std::filesystem::current_path(cwd_error);
    if (!cwd_error) candidates.push_back(cwd / "r2kernel.deployment.yaml");
    return first_existing_path(candidates);
}

std::filesystem::path deployment_relative(const std::filesystem::path& deployment, const std::string& ref) {
    return resolved_path(deployment.parent_path() / ref);
}

bool path_within(const std::filesystem::path& root, const std::filesystem::path& path) {
    return std::mismatch(root.begin(), root.end(), path.begin(), path.end()).first == root.end();
}

// Python _package_relative_path: a nonempty package-relative path without
// "..", inside the package, naming an existing file or directory.
std::filesystem::path package_relative_path(const std::filesystem::path& package_root, const std::string& raw,
                                            const std::string& field, bool directory) {
    const std::filesystem::path relative(raw);
    const bool parent_step = std::any_of(relative.begin(), relative.end(),
                                         [](const std::filesystem::path& part) { return part == ".."; });
    if (trim_copy(raw).empty() || relative.is_absolute() || parent_step)
        throw ManifestError(field + " must be a nonempty package-relative path without '..': " + python_repr(raw));
    const auto root = resolved_path(package_root);
    const auto resolved = resolved_path(root / relative);
    if (!path_within(root, resolved))
        throw ManifestError(field + " must remain inside package directory: " + python_repr(raw));
    if (!(directory ? std::filesystem::is_directory(resolved) : std::filesystem::is_regular_file(resolved)))
        throw ManifestError(field + " must name an existing " + (directory ? "directory" : "file") + ": " + python_repr(raw));
    return resolved;
}

void append_unique_root(std::vector<std::string>& roots, const std::filesystem::path& root) {
    std::error_code error;
    if (!std::filesystem::is_directory(root, error)) return;
    const std::string text = std::filesystem::weakly_canonical(root, error).string();
    if (std::find(roots.begin(), roots.end(), text) == roots.end()) roots.push_back(text);
}

std::vector<std::filesystem::path> environment_paths(const char* name) {
    std::vector<std::filesystem::path> out;
    std::stringstream entries(env_string_value(name));
    std::string entry;
    while (std::getline(entries, entry, ':'))
        if (!entry.empty()) out.emplace_back(entry);
    return out;
}

// Contract roots to inspect one package's manifest with (Python
// package_spec_sources): the package's contracts.spec_roots, then
// R2KERNEL_SPEC_PATH without the caller's own R2KERNEL_CONTRACT_SPEC_ROOTS,
// then R2KERNEL_SPEC_ROOT; the shipped standards follow in the core. Empty
// when the package declares no roots (the ambient roots apply).
std::vector<std::string> package_spec_scope(const std::filesystem::path& package_root, const YAML::Node& package) {
    const YAML::Node contracts = package["contracts"];
    if (!yaml_present(contracts)) return {};
    if (!contracts.IsMap()) throw ManifestError("Package contracts must be a mapping");
    const YAML::Node spec_roots = contracts["spec_roots"];
    if (!yaml_present(spec_roots)) return {};
    if (!spec_roots.IsSequence()) throw ManifestError("Package contracts.spec_roots must be a list of strings");
    std::vector<std::string> roots;
    for (const auto& root : spec_roots) {
        if (!root.IsScalar()) throw ManifestError("Package contracts.spec_roots must be a list of strings");
        append_unique_root(roots, package_relative_path(package_root, root.Scalar(), "contracts.spec_roots", true));
    }
    if (roots.empty()) return {};
    std::vector<std::string> caller;
    for (const auto& root : environment_paths("R2KERNEL_CONTRACT_SPEC_ROOTS")) append_unique_root(caller, root);
    for (const auto& root : environment_paths("R2KERNEL_SPEC_PATH")) {
        std::error_code error;
        const std::string text = std::filesystem::weakly_canonical(root, error).string();
        if (std::find(caller.begin(), caller.end(), text) == caller.end()) append_unique_root(roots, root);
    }
    if (const auto shared = env_string_value("R2KERNEL_SPEC_ROOT"); !shared.empty()) append_unique_root(roots, shared);
    return roots;
}

// Python load_subsystem_manifest: the manifest at path, read with the
// contract roots of its package (package_root, else the nearest parent
// directory holding package.yaml).
SubsystemManifest load_package_subsystem_manifest(const std::filesystem::path& raw_path,
                                                  std::optional<std::filesystem::path> package_root) {
    const auto path = resolved_path(expand_user(raw_path));
    if (!package_root) {
        for (auto parent = path.parent_path(); !parent.empty(); parent = parent.parent_path()) {
            if (std::filesystem::is_regular_file(parent / "package.yaml")) {
                package_root = parent;
                break;
            }
            if (parent == parent.root_path()) break;
        }
    }
    std::optional<contract_loader::InventoryScope> package_scope;
    if (package_root) {
        const auto root = resolved_path(*package_root);
        if (!path_within(root, path))
            throw ManifestError("Subsystem manifest must remain inside package directory: " + path.string());
        const auto spec_roots = package_spec_scope(root, load_yaml_mapping(root / "package.yaml", "package manifest"));
        if (!spec_roots.empty()) {
            package_scope.emplace(
                std::make_shared<contract_loader::Inventory>(std::make_shared<SchemaContext>(spec_roots)));
        }
    }
    try {
        return parse_subsystem_manifest_file(path);
    } catch (const ManifestError&) {
        throw;
    } catch (const std::exception& error) {
        throw ManifestError(error.what());
    }
}

// Python _optional_manifest_ref: deployment.robot / deployment.application.
std::optional<std::filesystem::path> optional_manifest_ref(const std::filesystem::path& deployment, const YAML::Node& value,
                                                           const std::string& field_name) {
    if (!yaml_present(value)) return std::nullopt;
    std::string text;
    if (value.IsScalar()) {
        text = yaml_string(value);
    } else if (value.IsMap()) {
        text = yaml_string(value["manifest"]);
    } else {
        throw ManifestError(field_name + " must be a path string or mapping with manifest");
    }
    if (text.empty()) return std::nullopt;
    return deployment_relative(deployment, text);
}

std::string deployment_manifest_ref(const YAML::Node& entry) {
    if (entry && entry.IsScalar()) return entry.as<std::string>(std::string {});
    if (entry && entry.IsMap()) {
        const std::string manifest = yaml_string(entry["manifest"]);
        if (!manifest.empty()) return manifest;
    }
    throw ManifestError("Deployment subsystem entry must be a manifest path or mapping with manifest");
}

// Python _parse_deployment_instance: one instances.<name> entry and its
// manifest (explicit, or the package's subsystem_manifest) read with the
// package's contract roots.
DeploymentInstance parse_deployment_instance(const std::filesystem::path& deployment, const std::string& name,
                                             const YAML::Node& entry) {
    if (!entry.IsMap()) throw ManifestError("Deployment instance " + name + " must be a mapping");
    DeploymentInstance instance;
    instance.name = name;
    if (const std::string package = yaml_string(entry["package"]); !package.empty())
        instance.package = deployment_relative(deployment, package);
    std::filesystem::path manifest_path;
    if (const std::string manifest = yaml_string(entry["manifest"]); !manifest.empty()) {
        manifest_path = deployment_relative(deployment, manifest);
    } else if (instance.package) {
        const YAML::Node package_document = load_yaml_mapping(*instance.package / "package.yaml", "package manifest");
        std::string relative = yaml_string(package_document["subsystem_manifest"]);
        if (relative.empty()) relative = "subsystem.yaml";
        manifest_path = package_relative_path(*instance.package, relative, "subsystem_manifest", false);
    } else {
        throw ManifestError("Deployment instance " + name + " must define manifest or package");
    }
    if (entry["slot"]) throw ManifestError("Deployment instance " + name + " uses deprecated `slot`; use `slots` mapping");
    instance.slots = yaml_string_mapping(entry["slots"], "instances." + name + ".slots");
    for (const auto& [slot_name, interface_name] : instance.slots) {
        if (interface_name.empty())
            throw ManifestError("instances." + name + ".slots." + slot_name + " must name a provided interface");
    }
    instance.launch = yaml_string(entry["launch"]);
    if (instance.launch.empty()) instance.launch = "managed";
    if (instance.launch != "managed" && instance.launch != "external" && instance.launch != "federated") {
        throw ManifestError("Deployment instance " + name + " has unsupported launch mode " + python_repr(instance.launch)
                            + "; expected one of managed, external, federated");
    }
    if (std::string host = yaml_string(entry["host"]); !host.empty()) instance.host = std::move(host);
    instance.params = yaml_string_mapping(entry["params"], "instances." + name + ".params");
    instance.manifest = load_package_subsystem_manifest(manifest_path, instance.package);
    return instance;
}

std::map<BindingReference, BindingReference> deployment_bindings(const YAML::Node& node) {
    std::map<BindingReference, BindingReference> bindings;
    if (!yaml_truthy(node)) return bindings;
    if (!node.IsMap()) throw ManifestError("Deployment bindings must be a mapping");
    for (const auto& item : node) {
        const auto source = BindingReference::parse(item.first.as<std::string>(std::string {}), "binding key");
        std::string target_text;
        if (item.second.IsScalar()) {
            target_text = item.second.as<std::string>(std::string {});
        } else if (item.second.IsMap()) {
            if (item.second["select"]) throw ManifestError("Selector bindings are not supported yet: " + source.format());
            target_text = yaml_string(item.second["to"]);
        } else {
            throw ManifestError("Binding value must be a string or mapping: " + source.format());
        }
        bindings[source] = BindingReference::parse(target_text, "binding " + source.format() + ".to");
    }
    return bindings;
}

// Required requirements without a launch binding, comma separated (the
// runtime bindings a subsystem without a deployment manifest waits for).
std::string unbound_required_interfaces(const SubsystemManifest& manifest,
                                        const std::unordered_map<std::string, std::string>& launch_bindings) {
    std::string names;
    for (const auto& [name, requirement] : manifest.requirements) {
        if (!requirement.required || launch_bindings.count(name) != 0) continue;
        names += (names.empty() ? "" : ", ") + name;
    }
    return names;
}

std::vector<BoundInterface> required_bindings_for_subsystem(const SubsystemManifest& subsystem,
                                                            const DeploymentManifest& deployment) {
    std::vector<BoundInterface> resolved;
    for (const auto& [name, requirement] : subsystem.requirements) {
        if (requirement.required) resolved.push_back(deployment.resolve(subsystem.name, name));
    }
    return resolved;
}

// Python load_deployment_manifest: robot and application manifests,
// subsystems, instances and bindings, then the platform and application
// checks, every binding, the Control binding groups, every required binding
// and the spatial topology.
DeploymentManifest parse_deployment_manifest(const std::filesystem::path& raw_path) {
    const auto path = resolved_path(expand_user(raw_path));
    const YAML::Node document = load_yaml_mapping(path, "deployment manifest");
    DeploymentManifest deployment;
    deployment.path = path;
    deployment.kind = yaml_string(document["kind"]);
    if (deployment.kind != "r2kernel.deployment")
        throw ManifestError("Deployment manifest kind must be 'r2kernel.deployment': " + path.string());
    deployment.version = yaml_version(document["version"]);
    if (const auto robot = optional_manifest_ref(path, document["robot"], "deployment.robot"))
        deployment.robot = parse_robot_platform_manifest(*robot);
    if (const auto application = optional_manifest_ref(path, document["application"], "deployment.application"))
        deployment.application = parse_application_manifest(*application);
    const YAML::Node subsystems = document["subsystems"];
    if (yaml_truthy(subsystems) && !subsystems.IsMap())
        throw ManifestError("Deployment manifest subsystems must be a mapping: " + path.string());
    if (yaml_truthy(subsystems)) {
        for (const auto& item : subsystems) {
            const SubsystemManifest manifest =
                load_package_subsystem_manifest(deployment_relative(path, deployment_manifest_ref(item.second)), std::nullopt);
            const std::string name = yaml_string(item.first);
            if (name.empty()) throw ManifestError("Deployment subsystem key is empty: " + path.string());
            if (manifest.name != name) {
                throw ManifestError("Deployment subsystem key " + python_repr(name) + " does not match manifest subsystem.name "
                                    + python_repr(manifest.name) + ": " + manifest.path.string());
            }
            deployment.subsystems[name] = manifest;
        }
    }
    const YAML::Node instances = document["instances"];
    if (yaml_truthy(instances) && !instances.IsMap())
        throw ManifestError("Deployment manifest instances must be a mapping: " + path.string());
    if (yaml_truthy(instances)) {
        for (const auto& item : instances) {
            const std::string name = yaml_string(item.first);
            if (name.empty()) throw ManifestError("Deployment instance key is empty: " + path.string());
            if (deployment.subsystems.count(name) != 0)
                throw ManifestError("Deployment instance duplicates subsystem name: " + name);
            DeploymentInstance instance = parse_deployment_instance(path, name, item.second);
            SubsystemManifest renamed = instance.manifest;
            renamed.name = name;
            deployment.subsystems[name] = std::move(renamed);
            deployment.instances[name] = std::move(instance);
        }
    }
    deployment.bindings = deployment_bindings(document["bindings"]);
    validate_deployment_platform_application(deployment);
    for (const auto& [source, _] : deployment.bindings) (void)deployment.resolve(source.subsystem, source.interface_name);
    validate_control_binding_group_identity(deployment);
    for (const auto& [_, subsystem] : deployment.subsystems) (void)required_bindings_for_subsystem(subsystem, deployment);
    validate_deployment_spatial_topology(deployment);
    return deployment;
}
