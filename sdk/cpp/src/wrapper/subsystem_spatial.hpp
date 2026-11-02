// Included once, inside subsystem.cpp's anonymous namespace after
// subsystem_manifest.hpp (same translation unit). Spatial declaration
// parsing, status JSON and the subsystem conformance and deployment topology
// checks of the Python SDK (r2kernel/manifest/_spatial.py), with the same
// decisions and messages. Cold: used while manifests load.

template <std::size_t N>
std::array<double, N> spatial_numbers(const YAML::Node& value, const std::array<double, N>& fallback,
                                      const std::string& field_name) {
    if (!yaml_truthy(value)) return fallback;
    if (!value.IsSequence() || value.size() != N)
        throw ManifestError(field_name + " must contain exactly " + std::to_string(N) + " numbers");
    std::array<double, N> numbers {};
    for (std::size_t index = 0; index < N; ++index) {
        try {
            numbers[index] = value[index].as<double>();
        } catch (const YAML::Exception&) {
            throw ManifestError(field_name + " must contain only numbers");
        }
        if (!std::isfinite(numbers[index])) throw ManifestError(field_name + " must contain finite numbers");
    }
    return numbers;
}

std::optional<SpatialPortReference> parse_spatial_port_reference(const YAML::Node& value, const std::string& field_name,
                                                                 bool required) {
    if (!yaml_present(value)) {
        if (required) throw ManifestError(field_name + " must be a mapping");
        return std::nullopt;
    }
    if (!value.IsMap()) throw ManifestError(field_name + " must be a mapping");
    SpatialPortReference reference {yaml_string(value["requirement"]), yaml_string(value["port"])};
    if (reference.requirement.empty() || reference.port.empty())
        throw ManifestError(field_name + " must define requirement and port");
    return reference;
}

// Python _parse_spatial_declaration.
SpatialDeclaration parse_spatial_declaration(const YAML::Node& value, const std::filesystem::path& manifest_path) {
    SpatialDeclaration spatial;
    if (!yaml_present(value)) return spatial;
    if (!value.IsMap()) throw ManifestError("spatial must be a mapping: " + manifest_path.string());
    spatial.participation = yaml_string(value["participation"]);
    if (spatial.participation != "none" && spatial.participation != "required")
        throw ManifestError("spatial.participation must be one of none, required: " + manifest_path.string());
    const YAML::Node ports = value["ports"];
    if (yaml_truthy(ports) && !ports.IsMap()) throw ManifestError("spatial.ports must be a mapping: " + manifest_path.string());
    if (yaml_truthy(ports)) {
        for (const auto& item : ports) {
            SpatialPortDeclaration port;
            port.name = yaml_string(item.first);
            if (port.name.empty() || !item.second.IsMap())
                throw ManifestError("spatial.ports contains an invalid port: " + manifest_path.string());
            const std::string field = "spatial.ports." + port.name;
            port.direction = yaml_string(item.second["direction"]);
            if (port.direction != "import" && port.direction != "export")
                throw ManifestError(field + ".direction must be import or export");
            port.semantic = yaml_string(item.second["semantic"]);
            if (port.semantic.empty()) throw ManifestError(field + " must define semantic");
            if (std::string frame = yaml_string(item.second["frame"]); !frame.empty()) port.frame = std::move(frame);
            port.source = parse_spatial_port_reference(item.second["source"], field + ".source", false);
            if (!port.frame && !port.source) throw ManifestError(field + " must define frame or source");
            if (port.frame && port.source) throw ManifestError(field + " cannot define both frame and source");
            port.required = yaml_bool(item.second["required"], true);
            spatial.ports[port.name] = std::move(port);
        }
    }
    const YAML::Node mounts = value["mounts"];
    if (yaml_truthy(mounts) && !mounts.IsSequence())
        throw ManifestError("spatial.mounts must be a list: " + manifest_path.string());
    if (yaml_truthy(mounts)) {
        std::set<std::string> names;
        for (std::size_t index = 0; index < mounts.size(); ++index) {
            const YAML::Node raw = mounts[index];
            if (!raw.IsMap()) throw ManifestError("spatial.mounts[" + std::to_string(index) + "] must be a mapping");
            SpatialMountDeclaration mount;
            mount.name = yaml_truthy(raw["name"]) ? yaml_string(raw["name"]) : "mount_" + std::to_string(index);
            if (mount.name.empty() || !names.insert(mount.name).second)
                throw ManifestError("spatial.mounts contains an empty or duplicate name: " + python_repr(mount.name));
            const std::string field = "spatial.mounts." + mount.name;
            mount.parent = *parse_spatial_port_reference(raw["parent"], field + ".parent", true);
            mount.child = *parse_spatial_port_reference(raw["child"], field + ".child", true);
            const YAML::Node transform = raw["transform"];
            if (yaml_truthy(transform) && !transform.IsMap()) throw ManifestError(field + ".transform must be a mapping");
            const YAML::Node transform_map = yaml_truthy(transform) ? transform : YAML::Node(YAML::NodeType::Map);
            mount.translation = spatial_numbers<3>(transform_map["translation"], {0.0, 0.0, 0.0},
                                                   field + ".transform.translation");
            mount.rotation_xyzw = spatial_numbers<4>(transform_map["rotation_xyzw"], {0.0, 0.0, 0.0, 1.0},
                                                     field + ".transform.rotation_xyzw");
            double norm = 0.0;
            for (const double item : mount.rotation_xyzw) norm += item * item;
            if (std::sqrt(norm) < 1e-9) throw ManifestError(field + " rotation quaternion must be non-zero");
            spatial.mounts.push_back(std::move(mount));
        }
    }
    if (spatial.participation == "none" && (!spatial.ports.empty() || !spatial.mounts.empty()))
        throw ManifestError("spatial.participation none cannot declare ports or mounts");
    if (spatial.participation == "required" && spatial.ports.empty() && spatial.mounts.empty())
        throw ManifestError("spatial.participation required must declare at least one port or mount");
    return spatial;
}

std::string spatial_reference_json(const SpatialPortReference& reference) {
    return "{\"requirement\":\"" + json_escape(reference.requirement) + "\",\"port\":\"" + json_escape(reference.port)
         + "\"}";
}

template <std::size_t N>
std::string spatial_numbers_json(const std::array<double, N>& values) {
    std::ostringstream out;
    out << std::setprecision(17) << "[";
    for (std::size_t index = 0; index < N; ++index) out << (index == 0 ? "" : ",") << values[index];
    out << "]";
    return out.str();
}

// Python _spatial_declaration_status.
std::string spatial_status_json(const SpatialDeclaration& spatial) {
    std::ostringstream out;
    out << "{\"participation\":\"" << json_escape(spatial.participation) << "\",\"ports\":{";
    bool first = true;
    for (const auto& [name, port] : spatial.ports) {
        out << (first ? "" : ",") << "\"" << json_escape(name) << "\":{"
            << "\"direction\":\"" << json_escape(port.direction) << "\","
            << "\"semantic\":\"" << json_escape(port.semantic) << "\","
            << "\"frame\":" << (port.frame ? "\"" + json_escape(*port.frame) + "\"" : std::string("null")) << ","
            << "\"required\":" << (port.required ? "true" : "false");
        if (port.source) out << ",\"source\":" << spatial_reference_json(*port.source);
        out << "}";
        first = false;
    }
    out << "},\"mounts\":[";
    for (std::size_t index = 0; index < spatial.mounts.size(); ++index) {
        const auto& mount = spatial.mounts[index];
        out << (index == 0 ? "" : ",") << "{\"name\":\"" << json_escape(mount.name) << "\","
            << "\"parent\":" << spatial_reference_json(mount.parent) << ","
            << "\"child\":" << spatial_reference_json(mount.child) << ","
            << "\"transform\":{\"translation\":" << spatial_numbers_json(mount.translation)
            << ",\"rotation_xyzw\":" << spatial_numbers_json(mount.rotation_xyzw) << "}}";
    }
    out << "]}";
    return out.str();
}

// Python _validate_subsystem_spatial_conformance: a public component.*
// provider declares the spatial ports its component spec requires.
void validate_subsystem_spatial_conformance(const SubsystemManifest& manifest) {
    for (const auto& [_, declaration] : manifest.provides) {
        if (declaration.visibility != "public" || !starts_with(declaration.contract, "component.")) continue;
        const auto component = contract_loader::find_component_contract(declaration.contract);
        if (component == nullptr) throw ManifestError("Component spec is not available: " + declaration.contract);
        if (component->spatial_participation != "required") continue;
        if (manifest.spatial.participation != "required") {
            throw ManifestError("Subsystem " + manifest.name + " implements " + component->id
                                + ", which requires spatial participation");
        }
        for (const auto& expected : component->spatial_ports) {
            if (!expected.required) continue;
            const auto actual = manifest.spatial.ports.find(expected.name);
            if (actual == manifest.spatial.ports.end()) {
                throw ManifestError("Subsystem " + manifest.name + " is missing spatial port " + python_repr(expected.name)
                                    + " required by " + component->id);
            }
            if (actual->second.direction != expected.direction) {
                throw ManifestError("Subsystem " + manifest.name + " spatial port " + python_repr(expected.name)
                                    + " direction " + python_repr(actual->second.direction) + " does not match "
                                    + component->id + " direction " + python_repr(expected.direction));
            }
        }
    }
}

const SpatialPortDeclaration& resolve_spatial_target_port(const DeploymentManifest& deployment,
                                                          const SubsystemManifest& owner,
                                                          const SpatialPortReference& reference) {
    const BoundInterface bound = deployment.resolve(owner.name, reference.requirement);
    const SubsystemManifest& target = deployment.subsystems.at(bound.target_subsystem);
    const auto port = target.spatial.ports.find(reference.port);
    if (port == target.spatial.ports.end()) {
        throw ManifestError("Spatial reference " + owner.name + "." + reference.requirement + "." + reference.port
                            + " targets " + target.name + ", which does not declare that port");
    }
    return port->second;
}

// Python _validate_deployment_spatial_topology: port sources and mounts
// reference ports of bound subsystems with compatible directions.
void validate_deployment_spatial_topology(const DeploymentManifest& deployment) {
    for (const auto& [_, subsystem] : deployment.subsystems) {
        if (subsystem.spatial.participation == "none") continue;
        for (const auto& [name, port] : subsystem.spatial.ports) {
            if (!port.source) continue;
            const auto& target = resolve_spatial_target_port(deployment, subsystem, *port.source);
            const bool same_direction_alias = target.direction == port.direction;
            const bool imported_export = port.direction == "import" && target.direction == "export";
            if (!same_direction_alias && !imported_export) {
                throw ManifestError("Spatial port " + subsystem.name + "." + name + " cannot source " + target.direction
                                    + " port " + port.source->requirement + "." + port.source->port + " as "
                                    + port.direction);
            }
        }
        for (const auto& mount : subsystem.spatial.mounts) {
            const auto& parent = resolve_spatial_target_port(deployment, subsystem, mount.parent);
            const auto& child = resolve_spatial_target_port(deployment, subsystem, mount.child);
            if (parent.direction != "export") {
                throw ManifestError("Spatial mount " + subsystem.name + "." + mount.name
                                    + " parent must reference an export port");
            }
            if (child.direction != "import") {
                throw ManifestError("Spatial mount " + subsystem.name + "." + mount.name
                                    + " child must reference an import port");
            }
        }
    }
}
