// frames.policy spatial_ports (api.locomotion.odometry.v2) and the reserved-ID
// rule for package spec roots, as in the Python SDK
// (tests/test_spatial_frame_ports.py): the loader, the provider publish check,
// the codec's refusal of missing frames, the v1/v2 transition and the
// start-time spec-root check. The manifest refusals and the binding cases are
// shared parity cases (sdk/testdata/parity, sdk_parity_cases_test.cpp).
#include "subsystem_offline_fixture.hpp"
#include "../src/wrapper/contract_loader.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"

#include <array>
#include <cstring>
#include <iterator>
#include <optional>

using namespace r2kernel::wrapper;
using namespace r2kernel_test;

namespace {

const std::filesystem::path kSpecRoot = R2KERNEL_SDK_SPEC_DIR;
constexpr char kOdometryV1[] = "msg.locomotion.mobile_base.v1/Odometry";
constexpr char kOdometryV2[] = "msg.locomotion.mobile_base.v2/Odometry";
constexpr char kPose[] = R"("pose":{"position":{"x":0,"y":0,"z":0},"orientation":{"x":0,"y":0,"z":0,"w":1}})";
constexpr char kPorts[] =
    "spatial:\n"
    "  participation: required\n"
    "  ports:\n"
    "    reference: {direction: export, semantic: odometry_reference, frame: odom}\n"
    "    body: {direction: export, semantic: mobile_body, frame: cleaner_base}\n";

template <typename Call>
std::optional<std::string> failure(Call&& call) {
    try { call(); } catch (const std::exception& error) { return std::string(error.what()); }
    return std::nullopt;
}

bool contains(const std::optional<std::string>& text, const std::string& part) {
    return text && text->find(part) != std::string::npos;
}

void write_text(const std::filesystem::path& path, const std::string& text) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream file(path, std::ios::binary);
    file << text;
    require(bool(file), "fixture write failed");
}

class ScopedVariable {
public:
    ScopedVariable(const char* name, const std::optional<std::string>& value) : name_(name) {
        if (const char* previous = std::getenv(name)) previous_ = previous;
        if (value) ::setenv(name, value->c_str(), 1);
        else ::unsetenv(name);
    }
    ~ScopedVariable() {
        if (previous_) ::setenv(name_, previous_->c_str(), 1);
        else ::unsetenv(name_);
    }

private:
    const char* name_;
    std::optional<std::string> previous_;
};

struct Provider {
    std::shared_ptr<RecordingClient> client = std::make_shared<RecordingClient>();
    std::shared_ptr<std::vector<std::string>> errors = std::make_shared<std::vector<std::string>>();
    std::unique_ptr<Fixture> subsystem;

    Provider(const std::string& name, const std::filesystem::path& manifest, std::function<bool(Fixture&)> hook = {}) {
        auto settings = options(name, manifest, client);
        settings.schema_roots = std::vector<std::string>{kSpecRoot.string()};
        settings.log_sink = [errors = errors](std::string_view level, std::string_view, std::string_view,
                                              std::string_view message) {
            if (level == "ERROR") errors->emplace_back(message);
        };
        subsystem = std::make_unique<Fixture>(std::move(settings), std::move(hook));
    }
    bool logged(const std::string& part) const {
        return std::any_of(errors->begin(), errors->end(), [&](const auto& line) { return line.find(part) != std::string::npos; });
    }
};

void loader_reads_frame_ports() {
    const contract_loader::Inventory inventory(std::vector<std::filesystem::path>{kSpecRoot});
    const auto api = inventory.api("api.locomotion.odometry.v2");
    require(api && api->frame_policy == contract_loader::kSpatialPortsFramePolicy, "odometry v2 is not spatial_ports");
    require(api->frame_ports.size() == 2 && api->frame_ports[0].field == "frame_id" && api->frame_ports[0].port == "reference"
            && api->frame_ports[1].field == "child_frame_id" && api->frame_ports[1].port == "body",
        "odometry v2 frames.ports differ");
    require(api->events.size() == 1 && api->events[0].type == kOdometryV2
            && api->events[0].frame_field_indexes == std::vector<std::size_t>{0, 1},
        "odometry v2 frame fields are not the first two wire fields");
    const auto v1 = inventory.api("api.locomotion.odometry.v1");
    require(v1 && v1->frame_policy == "endpoint_declared" && v1->frame_ports.empty(), "odometry v1 changed");
    const auto component = inventory.component("component.mobile_base.odometry.v2");
    require(component && std::find(component->conformance_apis.begin(), component->conformance_apis.end(),
                                    "api.locomotion.odometry.v2") != component->conformance_apis.end(),
        "component.mobile_base.odometry.v2 does not conform to odometry v2");
}

void loader_refuses_invalid_spatial_ports(ManifestFiles& files) {
    const auto root = files.root / "loader-spec";
    write_text(root / "messages/msg/acme/pose/v1.yaml",
        "version: 1\nid: msg.acme.pose.v1\nkind: message_set\nmessages:\n  StampedPose:\n    fields:\n"
        "    - {name: frame_id, type: string}\n    - {name: label, type: string, optional: true}\n"
        "    - {name: count, type: uint32}\n");
    const std::string event = "events:\n- {topic: pose, type: msg.acme.pose.v1/StampedPose, direction: publish}\n";
    const std::vector<std::pair<std::string, std::string>> cases {
        {event + "frames: {policy: spatial_ports}\n", "requires a non-empty frames.ports mapping"},
        {event + "frames: {policy: spatial_ports, ports: {}}\n", "requires a non-empty frames.ports mapping"},
        {event + "frames: {policy: spatial_ports, ports: {frame_id: [body]}}\n", "must map message field names to spatial port names"},
        {event + "frames: {policy: spatial_ports, ports: {child_frame_id: body}}\n",
         "frames.ports field 'child_frame_id' must be a required string field of msg.acme.pose.v1/StampedPose (event 'pose')"},
        {event + "frames: {policy: spatial_ports, ports: {label: body}}\n", "field 'label' must be a required string field"},
        {event + "frames: {policy: spatial_ports, ports: {count: body}}\n", "field 'count' must be a required string field"},
        {"events:\n- {topic: pose, type: msg.acme.pose.v1/StampedPose, direction: subscribe}\n"
         "frames: {policy: spatial_ports, ports: {frame_id: body}}\n", "requires a published event"},
        {"events:\n- {topic: pose, type: msg.acme.unknown.v1/Pose, direction: publish}\n"
         "frames: {policy: spatial_ports, ports: {frame_id: body}}\n",
         "message msg.acme.unknown.v1/Pose of event 'pose' is not found"},
    };
    for (const auto& [body, error] : cases) {
        write_text(root / "apis/api/acme/pose/v1.yaml", "version: 1\nid: api.acme.pose.v1\nkind: api_contract\n" + body);
        const contract_loader::Inventory inventory(std::vector<std::filesystem::path>{root});
        const auto refused = failure([&]() { (void)inventory.api("api.acme.pose.v1"); });
        if (!contains(refused, error)) throw std::runtime_error("spatial_ports case not refused with '" + error + "': " + refused.value_or("accepted"));
    }
    write_text(root / "apis/api/acme/pose/v1.yaml",
        "version: 1\nid: api.acme.pose.v1\nkind: api_contract\n" + event + "frames: {policy: spatial_ports, ports: {frame_id: body}}\n");
    const auto accepted = contract_loader::Inventory(std::vector<std::filesystem::path>{root}).api("api.acme.pose.v1");
    require(accepted && accepted->events[0].frame_field_indexes == std::vector<std::size_t>{0}, "valid spatial_ports refused");
    // An unversioned message reference resolves through the API's imports.
    write_text(root / "apis/api/acme/pose/v1.yaml",
        "version: 1\nid: api.acme.pose.v1\nkind: api_contract\nimports: {msg.acme.pose: v1}\n"
        "events:\n- {topic: pose, type: msg.acme.pose/StampedPose, direction: publish}\n"
        "frames: {policy: spatial_ports, ports: {frame_id: body}}\n");
    const auto imported = contract_loader::Inventory(std::vector<std::filesystem::path>{root}).api("api.acme.pose.v1");
    require(imported && imported->events[0].frame_field_indexes == std::vector<std::size_t>{0},
        "an imported message reference was not resolved");
    write_text(root / "apis/api/acme/pose/v1.yaml",
        "version: 1\nid: api.acme.pose.v1\nkind: api_contract\n" + event + "frames: {policy: vendor_defined, ports: 7}\n");
    const auto metadata = contract_loader::Inventory(std::vector<std::filesystem::path>{root}).api("api.acme.pose.v1");
    require(metadata && metadata->frame_policy == "vendor_defined" && metadata->frame_ports.empty(),
        "other frame policies must stay metadata");
}

ByteBuffer odometry_v2(const std::string& frame_id, const std::string& child_frame_id) {
    const SchemaContext context(std::vector<std::string>{kSpecRoot.string()});
    CodecWorkspace workspace;
    return context.prepare(kOdometryV2)
        .encode_json(R"({"frame_id":")" + frame_id + R"(","child_frame_id":")" + child_frame_id + R"(",)" + kPose + "}", workspace)
        .copy();
}

void provider_publish_check(ManifestFiles& files) {
    const auto manifest = files.write("component.mobile",
        std::string(kPorts) + "provides:\n  mobile_base: {contract: component.mobile_base.odometry.v2}\n");
    Provider provider("component.mobile", manifest);
    provider.client->last_publish_priority = 7;
    require(provider.subsystem->publish_event("odometry", odometry_v2("odom", "cleaner_base"))
            && provider.client->last_publish_priority == -1,
        "a sample with the provider's frames was not sent");
    for (const auto& [frame_id, child_frame_id, field, value] :
         std::vector<std::array<std::string, 4>>{{"odom", "base_footprint", "child_frame_id", "base_footprint"},
                                                 {"map", "cleaner_base", "frame_id", "map"}}) {
        provider.client->last_publish_priority = 7;
        provider.errors->clear();
        require(!provider.subsystem->publish_event("odometry", odometry_v2(frame_id, child_frame_id)),
            "a mismatched frame was published");
        require(provider.client->last_publish_priority == 7, "a mismatched sample reached the transport");
        require(provider.logged("frame_mismatch: odometry." + field + " is '" + value + "'")
                && provider.logged("(api.locomotion.odometry.v2); the sample was not published"),
            "the mismatch was not reported as frame_mismatch");
    }
    provider.client->last_publish_priority = 7;
    require(!provider.subsystem->publish_event("odometry", ByteBuffer{4, 0, 0, 0, 'o'}) && provider.logged("frame_id is missing"),
        "a truncated sample was published");
    // Topics without a spatial_ports API are not inspected.
    require(provider.subsystem->publish_event("mobile_base_state", ByteBuffer{1}), "an unrelated topic was refused");
}

void codec_refuses_missing_frames() {
    const SchemaContext context(std::vector<std::string>{kSpecRoot.string()});
    const auto codec = context.prepare(kOdometryV2);
    CodecWorkspace workspace;
    for (const char* sample : {R"({"child_frame_id":"cleaner_base",)", R"({"frame_id":"odom",)"}) {
        const auto refused = failure([&]() { (void)codec.encode_json(std::string(sample) + kPose + "}", workspace); });
        require(refused.has_value(), "the C++ JSON codec encoded a v2 sample without a required frame");
    }
    // The native node encoder refuses an absent required frame as well.
    const std::string frame = "odom", child = "cleaner_base";
    const auto text = [](const std::string& value) {
        return R2KernelCodecNode{R2KERNEL_NODE_STRING, 0, 0, 0, reinterpret_cast<const std::uint8_t*>(value.data()), value.size()};
    };
    const auto number = [](float value) {
        std::uint32_t bits = 0;
        std::memcpy(&bits, &value, sizeof bits);
        return R2KernelCodecNode{R2KERNEL_NODE_FLOAT32, 0, 0, bits, nullptr, 0};
    };
    std::array<R2KernelCodecNode, 14> nodes {{
        {R2KERNEL_NODE_MESSAGE, 1, 4, 0, nullptr, 0}, text(frame), text(child),
        {R2KERNEL_NODE_MESSAGE, 5, 2, 0, nullptr, 0}, {R2KERNEL_NODE_ABSENT, 0, 0, 0, nullptr, 0},
        {R2KERNEL_NODE_MESSAGE, 7, 3, 0, nullptr, 0}, {R2KERNEL_NODE_MESSAGE, 10, 4, 0, nullptr, 0},
        number(0), number(0), number(0), number(0), number(0), number(0), number(1)}};
    const auto wire = codec.encode_nodes(nodes.data(), nodes.size(), workspace).copy();
    require(wire == odometry_v2("odom", "cleaner_base"), "native and JSON v2 wire differ");
    nodes[1] = {R2KERNEL_NODE_ABSENT, 0, 0, 0, nullptr, 0};
    require(failure([&]() { (void)codec.encode_nodes(nodes.data(), nodes.size(), workspace); }).has_value(),
        "the C++ node codec encoded a v2 sample without frame_id");
    // v1 keeps its optional frames.
    CodecWorkspace v1_workspace;
    (void)context.prepare(kOdometryV1).encode_json(std::string("{") + kPose + "}", v1_workspace);
}

void one_subsystem_cannot_provide_v1_and_v2(ManifestFiles& files) {
    const auto both = files.write("component.both",
        std::string(kPorts) + "provides:\n  odometry_v1: {contract: api.locomotion.odometry.v1}\n"
                              "  odometry_v2: {contract: api.locomotion.odometry.v2}\n");
    for (const auto& [registered, refused] : std::vector<std::pair<std::string, std::string>>{
             {kOdometryV2, kOdometryV1}, {kOdometryV1, kOdometryV2}}) {
        Provider provider("component.both", both, [registered = registered](Fixture& current) {
            return current.register_event_publish({"odometry", registered, 256});
        });
        provider.subsystem->initialize();
        require(provider.subsystem->state() == DeviceState::error, "one subsystem provided odometry v1 and v2");
        require(provider.logged("event 'odometry' must use type '" + refused + "', found '" + registered + "'"),
            "the v1/v2 conflict was not reported as an Event type mismatch");
        provider.subsystem->terminate();
    }
    for (const auto& [api, type] : std::vector<std::pair<std::string, std::string>>{
             {"api.locomotion.odometry.v1", kOdometryV1}, {"api.locomotion.odometry.v2", kOdometryV2}}) {
        const std::string name = "component.instance_" + api.substr(api.size() - 2);
        const auto manifest = files.write(name, std::string(kPorts) + "provides:\n  odometry: {contract: " + api + "}\n");
        Provider provider(name, manifest, [type = type](Fixture& current) {
            return current.register_event_publish({"odometry", type, 256});
        });
        provider.subsystem->initialize();
        require(provider.subsystem->state() == DeviceState::ready, "a separate odometry provider instance was refused");
        provider.subsystem->terminate();
    }
}

void launcher_spec_roots_are_checked_at_construction(ManifestFiles& files) {
    const auto manifest = files.write("component.mobile",
        std::string(kPorts) + "provides:\n  mobile_base: {contract: component.mobile_base.odometry.v2}\n");
    const auto package_spec = files.root / "launcher-spec";
    const auto standard = std::filesystem::path("apis/api/locomotion/odometry/v1.yaml");
    write_text(package_spec / standard, "version: 1\nid: api.locomotion.odometry.v1\n");
    ScopedVariable roots("R2KERNEL_CONTRACT_SPEC_ROOTS", package_spec.string());
    for (const std::optional<std::string>& dictionary : {std::optional<std::string>(kSpecRoot.string()), std::optional<std::string>()}) {
        // Without an installed dictionary directory the core's shipped standards supply the definition.
        ScopedVariable shared("R2KERNEL_SPEC_ROOT", dictionary);
        const auto refused = failure([&]() { Provider provider("component.mobile", manifest); });
        require(contains(refused, "api.locomotion.odometry.v1 differs from the installed standard definition"),
            "a differing standard definition in a launcher spec root was accepted");
    }
    std::filesystem::copy_file(kSpecRoot / standard, package_spec / standard, std::filesystem::copy_options::overwrite_existing);
    write_text(package_spec / "apis/api/locomotion/odometry/v3.yaml", "version: 1\nid: api.locomotion.odometry.v3\n");
    ScopedVariable shared("R2KERNEL_SPEC_ROOT", kSpecRoot.string());
    require(contains(failure([&]() { Provider provider("component.mobile", manifest); }),
                     "api.locomotion.odometry.v3 uses the standard prefix api.locomotion"),
        "a new ID under a standard prefix was accepted");
    std::filesystem::remove(package_spec / "apis/api/locomotion/odometry/v3.yaml");
    Provider identical("component.mobile", manifest);
    require(identical.subsystem->name() == "component.mobile", "an identical standard copy was refused");
}

// Cross-audit SDK-104-01: the guard refuses what the core refuses (aliases,
// unreadable entries, misplaced documents) with the Python SDK's messages.
void launcher_spec_root_aliases_are_refused(ManifestFiles& files) {
    const auto manifest = files.write("component.mobile",
        std::string(kPorts) + "provides:\n  mobile_base: {contract: component.mobile_base.odometry.v2}\n");
    ScopedVariable shared("R2KERNEL_SPEC_ROOT", kSpecRoot.string());
    for (const bool modified : {false, true}) {
        const auto spec = files.root / (modified ? "alias-modified" : "alias-identical");
        const auto target = spec / "apis/api/aaa/odometry/v2.yaml";
        std::filesystem::create_directories(target.parent_path());
        std::filesystem::copy_file(kSpecRoot / "apis/api/locomotion/odometry/v2.yaml", target);
        if (modified) write_text(target, "version: 1\nid: api.locomotion.odometry.v2\nframes: {policy: vendor_defined}\n");
        std::filesystem::create_directory_symlink("aaa", spec / "apis/api/locomotion");
        ScopedVariable roots("R2KERNEL_CONTRACT_SPEC_ROOTS", spec.string());
        require(contains(failure([&]() { Provider provider("component.mobile", manifest); }),
                         "Package spec root " + spec.string()
                             + ": apis/api/locomotion is the same directory as apis/api/aaa; spec directories must not alias each other"),
            "an aliased spec directory was accepted");
    }
    const auto spec = files.root / "unreadable";
    std::filesystem::create_directories(spec / "apis/api");
    std::filesystem::create_directory_symlink("missing", spec / "apis/api/broken");
    write_text(spec / "apis/api/stray.yaml", "version: 1\n");
    ScopedVariable roots("R2KERNEL_CONTRACT_SPEC_ROOTS", spec.string());
    const auto refused = failure([&]() { Provider provider("component.mobile", manifest); });
    require(contains(refused, "Package spec root " + spec.string() + ": apis/api/broken cannot be read: No such file or directory"),
        "a broken link in a spec root was skipped");
    require(contains(refused, "Package spec root " + spec.string()
                                  + ": apis/api/stray.yaml is not at <tree>/<kind>/<segment>/.../<version>.yaml"),
        "a misplaced spec document was skipped");
}

// Cross-audit SDK-108-01: a message or API document must declare the ID of its
// path, with the Python SDK's messages.
void launcher_spec_root_document_ids_are_checked(ManifestFiles& files) {
    const auto manifest = files.write("component.mobile",
        std::string(kPorts) + "provides:\n  mobile_base: {contract: component.mobile_base.odometry.v2}\n");
    ScopedVariable shared("R2KERNEL_SPEC_ROOT", kSpecRoot.string());
    std::ifstream source(kSpecRoot / "apis/api/locomotion/odometry/v2.yaml", std::ios::binary);
    const std::string standard((std::istreambuf_iterator<char>(source)), std::istreambuf_iterator<char>());
    const auto spec = files.root / "vendor-path";
    write_text(spec / "apis/api/aaa/odometry/v2.yaml", standard);
    {
        ScopedVariable roots("R2KERNEL_CONTRACT_SPEC_ROOTS", spec.string());
        require(contains(failure([&]() { Provider provider("component.mobile", manifest); }),
                         "Package spec root " + spec.string()
                             + ": apis/api/aaa/odometry/v2.yaml declares id api.locomotion.odometry.v2; a document at this"
                               " path must declare api.aaa.odometry.v2"),
            "a standard ID at a vendor path was accepted");
    }
    std::string corrected = standard;
    const std::string from = "id: api.locomotion.odometry.v2";
    corrected.replace(corrected.find(from), from.size(), "id: api.aaa.odometry.v2");
    write_text(spec / "apis/api/aaa/odometry/v2.yaml", corrected);
    write_text(spec / "messages/msg/acme/pose/v1.yaml", "version: 1\nkind: message_set\nmessages: {}\n");
    ScopedVariable roots("R2KERNEL_CONTRACT_SPEC_ROOTS", spec.string());
    const auto refused = failure([&]() { Provider provider("component.mobile", manifest); });
    require(contains(refused, "Package spec root " + spec.string() + ": messages/msg/acme/pose/v1.yaml has no id"),
        "a message document without an id was accepted");
    require(!contains(refused, "apis/api/aaa/odometry/v2.yaml"), "the corrected vendor document was refused");
}

void run_loader_checks(ManifestFiles&) {
    loader_reads_frame_ports();
    codec_refuses_missing_frames();
}

}  // namespace

int main() {
    return run_offline_suite("spatial frame ports", {
        run_loader_checks,
        loader_refuses_invalid_spatial_ports,
        provider_publish_check,
        one_subsystem_cannot_provide_v1_and_v2,
        launcher_spec_roots_are_checked_at_construction,
        launcher_spec_root_aliases_are_refused,
        launcher_spec_root_document_ids_are_checked,
    });
}
