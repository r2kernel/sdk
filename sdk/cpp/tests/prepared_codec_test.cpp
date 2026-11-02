#include "r2kernel/wrapper/prepared_codec.hpp"
#include <array>
#include <chrono>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>
using namespace r2kernel::wrapper;
namespace {
void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
std::array<R2KernelCodecNode, 3> value(std::uint32_t index) {
    return {{{R2KERNEL_NODE_MESSAGE, 1, 2, 0, nullptr, 0},
             {R2KERNEL_NODE_UINT32, 0, 0, index, nullptr, 0},
             {R2KERNEL_NODE_BOOL, 0, 0, 1, nullptr, 0}}};
}
void native_parity_and_concurrency() {
    const auto codec = SchemaContext(std::vector<std::string>{}).prepare("msg.core.common.primitive.v1/IndexedBoolValue");
    require(codec.source() == "standard", "shipped codec must be build-prepared");
    require(codec.fingerprint().size() == 64, "missing wire fingerprint");
    CodecWorkspace native, json, decoded;
    const auto nodes = value(7);
    const auto wire = codec.encode_nodes(nodes.data(), nodes.size(), native).copy();
    const auto expected = codec.encode_json("{\"index\":7,\"value\":true}", json).copy();
    require(wire == expected, "native and JSON wire differ");
    const std::string reference = "msg.core.common.primitive.v1/IndexedBoolValue";
    const std::string input = "{\"index\":7,\"value\":true}";
    std::size_t length = 0;
    require(r2kernel_codec_encode_message_json(reference.c_str(), reinterpret_cast<const std::uint8_t*>(input.data()),
        input.size(), nullptr, 0, &length) == 0, "generic codec size query failed");
    ByteBuffer generic(length);
    require(r2kernel_codec_encode_message_json(reference.c_str(), reinterpret_cast<const std::uint8_t*>(input.data()),
        input.size(), generic.data(), generic.size(), &length) == 0, "generic codec encode failed");
    generic.resize(length);
    require(wire == generic, "prepared native wire differs from generic API");

    const auto restored = codec.decode_nodes(wire, decoded);
    require(restored.size == 3 && restored.data[1].bits == 7 && restored.data[2].bits == 1, "native field round trip differs");
    std::atomic<bool> passed{true};
    auto worker = [codec, &passed](std::uint32_t seed) {
        try {
            CodecWorkspace encode, decode;
            for (std::uint32_t index = seed; index < seed + 1000; ++index) {
                const auto fields = value(index);
                const auto packet = codec.encode_nodes(fields.data(), fields.size(), encode).copy();
                const auto result = codec.decode_nodes(packet, decode);
                if (result.data[1].bits != index) passed.store(false);
            }
        } catch (...) { passed.store(false); }
    };
    std::thread first(worker, 1), second(worker, 10000);
    first.join(); second.join();
    require(passed.load(), "shared immutable codec mixed independent workspaces");
}
void custom_snapshot_and_failure() {
    const auto root = std::filesystem::temp_directory_path() /
        ("r2kernel-cpp-prepared-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    struct Cleanup { std::filesystem::path root; ~Cleanup() { std::error_code error; std::filesystem::remove_all(root, error); } } cleanup{root};
    const auto path = root / "messages/msg/test/native/v1.yaml";
    std::filesystem::create_directories(path.parent_path());
    const auto write = [&](const char* type) {
        std::ofstream stream(path);
        stream << "version: 1\nid: msg.test.native.v1\nkind: message_set\nmessages:\n  Value:\n    fields:\n      - name: value\n        type: " << type << '\n';
    };
    write("uint32");
    const SchemaContext first(std::vector<std::string>{root.string()});
    const auto original = first.prepare("msg.test.native.v1/Value");
    write("bool");
    const auto changed = SchemaContext(std::vector<std::string>{root.string()}).prepare("msg.test.native.v1/Value");
    require(original.fingerprint() != changed.fingerprint(), "edited custom schema reused stale plan");
    require(first.prepare("msg.test.native.v1/Value").fingerprint() == original.fingerprint(), "immutable context changed after edit");
    { std::ofstream stream(path); stream << "messages: [malformed"; }
    bool failed = false;
    try { SchemaContext(std::vector<std::string>{root.string()}).prepare("msg.test.native.v1/Value"); } catch (const std::exception&) { failed = true; }
    require(failed, "malformed custom schema was hidden by old plan");
    std::filesystem::remove(path);
    failed = false;
    try { SchemaContext(std::vector<std::string>{root.string()}).prepare("msg.test.native.v1/Value"); } catch (const std::exception&) { failed = true; }
    require(failed, "deleted custom schema was hidden by old plan");
}
}
int main() {
    try { native_parity_and_concurrency(); custom_snapshot_and_failure(); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "prepared native codec parity, snapshot, failure and concurrency passed\n";
}
