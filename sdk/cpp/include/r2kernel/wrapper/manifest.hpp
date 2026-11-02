#pragma once

// Manifest and binding model of a Subsystem and its loaders (Python SDK
// r2kernel.manifest). Field names follow Python except where a Python name is
// reserved or a platform macro in C++ (requires -> requirements, interface ->
// interface_name) and where Python holds free-form mappings (kinematics,
// parts, frontend, slot metadata), which C++ keeps as JSON text (*_json).

#include <array>
#include <filesystem>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

#include "r2kernel/export.hpp"

namespace r2kernel::wrapper {

// A manifest or binding declaration is invalid (Python ManifestError).
class R2KERNEL_CPP_API ManifestError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
    ~ManifestError() override;
};

struct InterfaceDeclaration {
    std::string name;
    std::string contract;
    std::string visibility = "public";
    bool required = true;
    std::string cardinality = "one";
    std::map<std::string, std::string> labels;
    // Endpoint kind (publishes, subscribes, controls, procedures, operations)
    // to endpoint names; declared only on internal interfaces.
    std::map<std::string, std::vector<std::string>> endpoints;
};

// A compound export: API projection of one of its requirements.
struct ExportDeclaration {
    std::string name;
    std::string contract;
    std::string requirement;
};

// A spatial port of a required (bound) subsystem.
struct SpatialPortReference {
    std::string requirement;
    std::string port;
};

struct SpatialPortDeclaration {
    std::string name;
    std::string direction;  // import or export
    std::string semantic;
    std::optional<std::string> frame;
    bool required = true;
    // An alias of a port of a bound subsystem (instead of frame).
    std::optional<SpatialPortReference> source;
};

struct SpatialMountDeclaration {
    std::string name;
    SpatialPortReference parent;
    SpatialPortReference child;
    std::array<double, 3> translation {0.0, 0.0, 0.0};
    std::array<double, 4> rotation_xyzw {0.0, 0.0, 0.0, 1.0};
};

struct SpatialDeclaration {
    std::string participation = "none";  // none or required
    std::map<std::string, SpatialPortDeclaration> ports;
    std::vector<SpatialMountDeclaration> mounts;
};

struct R2KERNEL_CPP_API SubsystemManifest {
    std::filesystem::path path;
    std::string kind;
    int version = 1;
    std::string name;
    std::string role;
    std::map<std::string, InterfaceDeclaration> provides;
    std::map<std::string, InterfaceDeclaration> requirements;
    std::map<std::string, ExportDeclaration> exports;
    SpatialDeclaration spatial;

    // Contracts of public provided interfaces starting with api. / component.
    std::vector<std::string> declared_apis() const;
    std::vector<std::string> declared_components() const;
};

// "<subsystem>.<interface>"; the subsystem part may itself contain dots.
struct R2KERNEL_CPP_API BindingReference {
    std::string subsystem;
    std::string interface_name;

    // Splits at the last dot after trimming; throws ManifestError naming
    // field_name when either part is empty.
    static BindingReference parse(const std::string& value, const std::string& field_name);
    std::string format() const;

    bool operator==(const BindingReference& other) const {
        return subsystem == other.subsystem && interface_name == other.interface_name;
    }
    bool operator!=(const BindingReference& other) const { return !(*this == other); }
    bool operator<(const BindingReference& other) const {
        return std::tie(subsystem, interface_name) < std::tie(other.subsystem, other.interface_name);
    }
};

// A requirement resolved to the subsystem and interface that serve it.
struct BoundInterface {
    std::string requirement;
    std::string target_subsystem;
    std::string provider_interface;
    std::string contract;

    // The target subsystem name (what Control, Procedure and Operation calls use).
    const std::string& target() const noexcept { return target_subsystem; }

    bool operator==(const BoundInterface& other) const {
        return requirement == other.requirement && target_subsystem == other.target_subsystem
            && provider_interface == other.provider_interface && contract == other.contract;
    }
    bool operator!=(const BoundInterface& other) const { return !(*this == other); }
};

// One instances.<name> entry of a deployment manifest.
struct DeploymentInstance {
    std::string name;
    // The subsystem manifest as loaded; DeploymentManifest::subsystems holds
    // it under the instance name.
    SubsystemManifest manifest;
    std::optional<std::filesystem::path> package;
    // Robot slot name to the provided interface that fills it.
    std::map<std::string, std::string> slots;
    std::string launch = "managed";  // managed, external or federated
    std::optional<std::string> host;
    std::map<std::string, std::string> params;
};

struct RobotPlatformSlot {
    std::string name;
    std::string role;
    std::optional<std::string> contract;
    std::optional<std::string> part;
    bool required = true;
    // The slot's other keys as a JSON object.
    std::string metadata_json = "{}";
};

// An r2kernel.robot manifest.
struct RobotPlatformManifest {
    std::filesystem::path path;
    std::string kind;
    int version = 1;
    std::string name;
    std::vector<std::string> platforms;  // robot profiles
    std::string kinematics_json = "{}";
    // Part name to the part's JSON object.
    std::map<std::string, std::string> parts_json;
    std::map<std::string, RobotPlatformSlot> slots;
};

// The application view of an app-role subsystem manifest.
struct ApplicationManifest {
    std::filesystem::path path;
    std::string kind;
    int version = 1;
    std::string name;
    std::optional<std::filesystem::path> subsystem_manifest;
    std::vector<std::string> platforms;
    std::vector<std::string> required_robot_platforms;
    std::string frontend_json = "{}";
};

// Static deployment bindings (r2kernel.deployment manifest). subsystems holds
// every declared subsystem and instance (an instance under its instance name).
struct R2KERNEL_CPP_API DeploymentManifest {
    std::filesystem::path path;
    std::string kind;
    int version = 1;
    std::optional<RobotPlatformManifest> robot;
    std::optional<ApplicationManifest> application;
    std::map<std::string, SubsystemManifest> subsystems;
    std::map<BindingReference, BindingReference> bindings;
    std::map<std::string, DeploymentInstance> instances;

    // Resolves consumer.requirement through the deployment bindings (or
    // target_override), following compound exports to the component provider,
    // with the Python SDK's role, contract and export checks. Throws
    // ManifestError.
    BoundInterface resolve(const std::string& consumer, const std::string& requirement,
                           const std::optional<BindingReference>& target_override = std::nullopt) const;
};

// Manifest loaders of the Python SDK (r2kernel.manifest). Each reads and
// validates one file with the contract specs of the environment (and, for a
// manifest inside a package, the package's contracts.spec_roots) and throws
// ManifestError with the Python SDK's message.
R2KERNEL_CPP_API SubsystemManifest load_subsystem_manifest(
    const std::filesystem::path& path, const std::optional<std::filesystem::path>& package_root = std::nullopt);
R2KERNEL_CPP_API RobotPlatformManifest load_robot_platform_manifest(const std::filesystem::path& path);
R2KERNEL_CPP_API ApplicationManifest load_application_manifest(const std::filesystem::path& path);
// Loads a deployment manifest with its robot, application, subsystem and
// instance manifests and validates it as a whole: bindings, robot slots,
// application profiles, Control binding groups and the spatial topology.
R2KERNEL_CPP_API DeploymentManifest load_deployment_manifest(const std::filesystem::path& path);
// Resolves every required requirement of subsystem in deployment.
R2KERNEL_CPP_API std::vector<BoundInterface> validate_required_bindings_for_subsystem(
    const SubsystemManifest& subsystem, const DeploymentManifest& deployment);

}  // namespace r2kernel::wrapper
