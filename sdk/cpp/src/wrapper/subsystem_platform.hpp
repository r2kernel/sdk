// Included once, inside subsystem.cpp's anonymous namespace after
// subsystem_spatial.hpp (same translation unit). Robot and application
// manifests and the deployment-wide platform, application and Control
// binding-group checks of the Python SDK (r2kernel/manifest/loader.py and
// _platform.py), with the same decisions and messages. Cold: used while
// manifests load.

// Python _parse_robot_platform_slots.
std::map<std::string, RobotPlatformSlot> parse_robot_platform_slots(const YAML::Node& value,
                                                                    const std::filesystem::path& manifest_path) {
    std::map<std::string, RobotPlatformSlot> slots;
    if (!yaml_truthy(value)) return slots;
    if (!value.IsMap()) throw ManifestError("Robot slots must be a mapping: " + manifest_path.string());
    for (const auto& item : value) {
        RobotPlatformSlot slot;
        slot.name = yaml_string(item.first);
        if (slot.name.empty()) throw ManifestError("Robot slot key is empty: " + manifest_path.string());
        const YAML::Node raw = item.second;
        if (!raw.IsMap()) throw ManifestError("Robot slot " + slot.name + " must be a mapping: " + manifest_path.string());
        slot.role = yaml_string(raw["role"]);
        if (slot.role != "component" && slot.role != "gateway" && slot.role != "compound") {
            throw ManifestError("Robot slot " + slot.name + " has unsupported role " + python_repr(slot.role)
                                + "; robot slots must be one of component, gateway, compound");
        }
        if (std::string contract = yaml_string(raw["contract"]); !contract.empty()) {
            reject_capability_contract(contract, "Robot slot " + slot.name + " contract");
            if (!is_known_public_contract(contract)) {
                throw ManifestError("Robot slot " + slot.name + " uses unknown public contract " + python_repr(contract)
                                    + "; robot slots must reference R2Kernel spec contracts (component.* or api.*). "
                                      "Keep private/tutorial wiring inside subsystem bindings.");
            }
            slot.contract = std::move(contract);
        }
        if (std::string part = yaml_string(raw["part"]); !part.empty()) slot.part = std::move(part);
        slot.required = yaml_bool(raw["required"], true);
        YAML::Node metadata(YAML::NodeType::Map);
        for (const auto& entry : raw) {
            const std::string key = yaml_string(entry.first);
            if (key != "role" && key != "contract" && key != "part" && key != "required") metadata[key] = entry.second;
        }
        slot.metadata_json = yaml_node_json(metadata);
        slots[slot.name] = std::move(slot);
    }
    return slots;
}

// Python load_robot_platform_manifest.
RobotPlatformManifest parse_robot_platform_manifest(const std::filesystem::path& raw_path) {
    const auto path = resolved_path(expand_user(raw_path));
    const YAML::Node document = load_yaml_mapping(path, "robot manifest");
    RobotPlatformManifest manifest;
    manifest.path = path;
    manifest.kind = yaml_string(document["kind"]);
    if (manifest.kind != "r2kernel.robot") throw ManifestError("Robot manifest kind must be 'r2kernel.robot': " + path.string());
    manifest.version = yaml_version(document["version"]);
    const YAML::Node robot = document["robot"];
    if (!robot.IsMap()) throw ManifestError("Robot manifest must define robot mapping: " + path.string());
    manifest.name = yaml_string(robot["name"]);
    if (manifest.name.empty()) throw ManifestError("Robot manifest is missing robot.name: " + path.string());
    if (document["capabilities"] || robot["capabilities"]) {
        throw ManifestError("robot manifest field `capabilities` is not supported; declare api.* requirements "
                            "on subsystems and bind them in the deployment: " + path.string());
    }
    manifest.slots = parse_robot_platform_slots(document["slots"], path);
    manifest.platforms = yaml_string_tuple(
        first_truthy({robot["profiles"], robot["platforms"], document["profiles"], document["platforms"]}));
    manifest.kinematics_json = yaml_mapping_json(document["kinematics"], "kinematics");
    const YAML::Node parts = document["parts"];
    if (yaml_truthy(parts)) {
        if (!parts.IsMap()) throw ManifestError("parts must be a mapping");
        for (const auto& item : parts) {
            const std::string name = yaml_string(item.first);
            if (name.empty()) throw ManifestError("parts contains an empty key");
            if (!yaml_present(item.second)) {
                manifest.parts_json[name] = "{}";
            } else if (item.second.IsMap()) {
                manifest.parts_json[name] = yaml_node_json(item.second);
            } else {
                throw ManifestError("parts." + name + " must be a mapping");
            }
        }
    }
    return manifest;
}

// Python load_application_manifest: an app-role subsystem manifest and its
// application section.
ApplicationManifest parse_application_manifest(const std::filesystem::path& raw_path) {
    const auto path = resolved_path(expand_user(raw_path));
    const YAML::Node document = load_yaml_mapping(path, "application manifest");
    ApplicationManifest manifest;
    manifest.path = path;
    manifest.kind = yaml_string(document["kind"]);
    manifest.version = yaml_version(document["version"]);
    if (manifest.kind != "r2kernel.subsystem")
        throw ManifestError("Application manifest kind must be an app-role 'r2kernel.subsystem': " + path.string());
    const SubsystemManifest subsystem = load_package_subsystem_manifest(path, std::nullopt);
    if (subsystem.role != "app") {
        throw ManifestError("Application subsystem manifest " + path.string() + " has role " + python_repr(subsystem.role)
                            + "; expected 'app'");
    }
    YAML::Node application = document["application"];
    if (!yaml_truthy(application)) application = YAML::Node(YAML::NodeType::Map);
    if (!application.IsMap())
        throw ManifestError("Application subsystem application section must be a mapping: " + path.string());
    YAML::Node robot_requires = application["requires_robot"];
    if (!yaml_truthy(robot_requires)) robot_requires = YAML::Node(YAML::NodeType::Map);
    if (!robot_requires.IsMap())
        throw ManifestError("Application subsystem application.requires_robot must be a mapping: " + path.string());
    if (robot_requires["capabilities"]) {
        throw ManifestError("application robot requirement field `capabilities` is not supported; declare api.* "
                            "requirements and bind them in the deployment: " + path.string());
    }
    manifest.name = subsystem.name;
    manifest.subsystem_manifest = path;
    manifest.platforms = yaml_string_tuple(first_truthy({application["profiles"], application["platforms"]}));
    manifest.required_robot_platforms =
        yaml_string_tuple(first_truthy({robot_requires["profiles"], robot_requires["platforms"]}));
    manifest.frontend_json = yaml_mapping_json(application["frontend"], "application.frontend");
    return manifest;
}

// Python _validate_deployment_instance_slot_assignment.
void validate_instance_slot_assignment(const DeploymentManifest& deployment, const DeploymentInstance& instance,
                                       const std::string& slot_name, const std::string& interface_name) {
    const auto slot = deployment.robot->slots.find(slot_name);
    if (slot == deployment.robot->slots.end()) {
        throw ManifestError("Deployment instance " + instance.name + " references unknown robot slot "
                            + python_repr(slot_name));
    }
    if (instance.manifest.role != slot->second.role) {
        throw ManifestError("Deployment instance " + instance.name + " role " + python_repr(instance.manifest.role)
                            + " does not match robot slot " + slot_name + " role " + python_repr(slot->second.role));
    }
    const auto provided = instance.manifest.provides.find(interface_name);
    if (provided == instance.manifest.provides.end()) {
        throw ManifestError("Deployment instance " + instance.name + " slot " + python_repr(slot_name)
                            + " references missing provided interface " + python_repr(interface_name));
    }
    if (provided->second.visibility != "public") {
        throw ManifestError("Deployment instance " + instance.name + " slot " + python_repr(slot_name)
                            + " uses interface " + python_repr(interface_name) + " with visibility "
                            + provided->second.visibility + "; robot slots require public interfaces");
    }
    if (slot->second.contract && *slot->second.contract != provided->second.contract) {
        throw ManifestError("Deployment instance " + instance.name + " slot " + python_repr(slot_name)
                            + " requires contract " + *slot->second.contract + ", but interface "
                            + python_repr(interface_name) + " provides " + provided->second.contract);
    }
}

// Python _validate_deployment_platform_application: the application needs a
// robot and its own subsystem, robot slots are filled once by matching
// instances, and the robot provides the profiles the application requires.
void validate_deployment_platform_application(const DeploymentManifest& deployment) {
    if (deployment.application && !deployment.robot) {
        throw ManifestError("Deployment " + deployment.path.string() + " declares an application but no robot manifest");
    }
    if (deployment.application && deployment.application->subsystem_manifest) {
        const auto& application_path = *deployment.application->subsystem_manifest;
        const bool included = std::any_of(deployment.subsystems.begin(), deployment.subsystems.end(), [&](const auto& item) {
            return item.second.path == application_path && item.second.role == "app";
        });
        if (!included) {
            throw ManifestError("Deployment " + deployment.path.string() + " declares application "
                                + python_repr(deployment.application->name)
                                + " but does not include its application subsystem manifest: " + application_path.string());
        }
    }
    if (deployment.robot) {
        std::set<std::string> assigned_slots;
        for (const auto& [_, instance] : deployment.instances) {
            if (instance.slots.empty()) {
                if (instance.manifest.role != "app" && instance.manifest.role != "compound" && instance.launch == "managed") {
                    throw ManifestError("Deployment instance " + instance.name
                                        + " is a managed robot subsystem but is not assigned to any robot slot");
                }
                continue;
            }
            if (instance.manifest.role == "app")
                throw ManifestError("Deployment instance " + instance.name + " is an app and cannot fill robot slots");
            for (const auto& [slot_name, interface_name] : instance.slots) {
                if (!assigned_slots.insert(slot_name).second)
                    throw ManifestError("Robot slot " + python_repr(slot_name) + " is assigned more than once");
                validate_instance_slot_assignment(deployment, instance, slot_name, interface_name);
            }
        }
        std::vector<std::string> missing;
        for (const auto& [name, slot] : deployment.robot->slots)
            if (slot.required && assigned_slots.count(name) == 0) missing.push_back(name);
        if (!missing.empty())
            throw ManifestError("Deployment is missing required robot slot instance(s): " + join_names(missing));
    } else {
        std::vector<std::string> slot_instances;
        for (const auto& [name, instance] : deployment.instances)
            if (!instance.slots.empty()) slot_instances.push_back(name);
        if (!slot_instances.empty())
            throw ManifestError("Deployment instances with `slots` require a robot manifest: " + join_names(slot_instances));
    }
    if (deployment.robot && deployment.application) {
        std::vector<std::string> missing;
        for (const auto& platform : deployment.application->required_robot_platforms) {
            const auto& provided = deployment.robot->platforms;
            if (std::find(provided.begin(), provided.end(), platform) == provided.end()) missing.push_back(platform);
        }
        if (!missing.empty()) {
            throw ManifestError("Application " + deployment.application->name + " requires robot profile(s) "
                                + join_names(missing) + ", but robot " + deployment.robot->name
                                + " does not provide them");
        }
    }
}

// Python _validate_control_binding_group_identity: one source subsystem binds
// at most one requirement to a Control provider interface.
void validate_control_binding_group_identity(const DeploymentManifest& deployment) {
    std::map<std::tuple<std::string, std::string, std::string>, BindingReference> seen;
    for (const auto& [source, _] : deployment.bindings) {
        const BoundInterface resolved = deployment.resolve(source.subsystem, source.interface_name);
        const SubsystemManifest& provider = deployment.subsystems.at(resolved.target_subsystem);
        const auto declaration = provider.provides.find(resolved.provider_interface);
        if (declaration == provider.provides.end()) continue;
        const auto key = std::make_tuple(source.subsystem, resolved.target_subsystem, resolved.provider_interface);
        const auto previous = seen.find(key);
        if (previous == seen.end()) {
            seen.emplace(key, source);
            continue;
        }
        const auto explicit_controls = declaration->second.endpoints.find("controls");
        const bool has_control_endpoints =
            (explicit_controls != declaration->second.endpoints.end() && !explicit_controls->second.empty())
            || !strict_contract_control_endpoints(declaration->second.contract,
                                                  "provides." + resolved.provider_interface).empty();
        if (!has_control_endpoints) continue;
        throw ManifestError("Ambiguous Control binding group identity: " + previous->second.format() + " and "
                            + source.format() + " both bind to " + resolved.target_subsystem + "."
                            + resolved.provider_interface
                            + "; a source subsystem may bind only one requirement to a Control provider interface");
    }
}
