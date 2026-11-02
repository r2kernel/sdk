// In-process micro-benchmark of Operation event (SDAE) payload encoding: the
// cost of publishing and receiving one event whose payload is written as JSON
// text (no declared type), the same value encoded with its declared message
// type from JSON text through a prepared codec (the JSON API; Python's wire),
// and that typed event without JSON (the CodecByteView API and
// OperationSubscribeOptions::payload_json false), plus the event sizes.
// Informational: it does not run under CTest.
//
// Cases (ns per call; the minimum over batches, the least noise-sensitive
// estimator on a shared host), for a feedback Pose and a JointState-sized
// Float32Vector state payload:
//   <payload>_encode_json         encode_payload, JSON text written as is
//   <payload>_encode_typed        encode_payload, JSON text -> codec bytes
//   <payload>_encode_typed_bytes  encode_typed_payload from encoded bytes
//   <payload>_encode_typed_nodes  PreparedCodec::encode_nodes into a reused
//                                 workspace + encode_typed_payload
//   <payload>_decode_json         decode + to_message, JSON text out
//   <payload>_decode_typed        decode + to_message, codec bytes -> JSON text
//   <payload>_decode_typed_bytes  decode + to_message without JSON (bytes out)
//   <payload>_decode_typed_nodes  that + PreparedCodec::decode_nodes into a
//                                 reused workspace
//   <payload>_bytes_{json,typed}  SDAE event size
//
// Usage: r2kernel_cpp_operation_event_payload_benchmark [batches] [scale]
// Prints one JSON object.
#include "../src/wrapper/operation_event_wire.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

using namespace r2kernel::wrapper;
namespace wire = r2kernel::wrapper::operation_event_wire;
using Clock = std::chrono::steady_clock;

namespace {
volatile std::size_t g_sink = 0;

double measure(const std::function<std::size_t()>& call, int iterations, int batches) {
    std::vector<double> samples;
    samples.reserve(static_cast<std::size_t>(batches));
    for (int batch = 0; batch < batches; ++batch) {
        const auto start = Clock::now();
        std::size_t sink = 0;
        for (int index = 0; index < iterations; ++index) sink += call();
        const auto elapsed = std::chrono::duration<double, std::nano>(Clock::now() - start).count();
        g_sink = g_sink + sink;
        samples.push_back(elapsed / iterations);
    }
    return *std::min_element(samples.begin(), samples.end());
}

struct Payload {
    const char* name;
    const char* kind;
    const char* type;
    std::string json;
};

std::string vector_json(std::size_t count) {
    std::string out = "{\"values\":[";
    for (std::size_t index = 0; index < count; ++index) {
        if (index) out += ",";
        out += std::to_string(0.25 * static_cast<double>(index + 1));
    }
    return out + "]}";
}
}  // namespace

int main(int argc, char** argv) {
    const int batches = argc > 1 ? std::atoi(argv[1]) : 7;
    const int scale = argc > 2 ? std::atoi(argv[2]) : 1;
    const int iterations = 20000 * std::max(scale, 1);
    const SchemaContext context(std::vector<std::string>{});
    const std::vector<Payload> payloads = {
        {"pose_feedback", "feedback", "msg.core.spatial.v1/Pose",
         "{\"position\":{\"x\":1.0,\"y\":2.5,\"z\":-0.25},\"orientation\":{\"x\":0.0,\"y\":0.0,\"z\":0.5,\"w\":1.0}}"},
        {"vector32_state", "state", "msg.core.common.primitive.v1/Float32Vector", vector_json(32)},
    };
    const std::string operation_id = "6f1c2a4b9d8e4f0a8b7c6d5e4f3a2b1c";
    std::map<std::string, double> results;
    for (const auto& payload : payloads) {
        const PreparedCodec codec = context.prepare(payload.type);
        // As Subsystem::encode_message_json/decode_message_json: one reused
        // thread workspace, the encoded bytes copied out.
        CodecWorkspace workspace;
        const auto encode_typed = [&codec, &workspace](const std::string&, const std::string& json) {
            return codec.encode_json(json, workspace).copy();
        };
        const auto decode_typed = [&codec, &workspace](const std::string&, const ByteBuffer& bytes) {
            return std::string(codec.decode_json(bytes, workspace));
        };
        const std::string state = std::string(payload.kind) == "state" ? "running" : "";
        const auto encode = [&](bool typed) {
            return wire::encode_payload(payload.kind, operation_id, "move_to_pose", "component.bench", 1,
                                        typed ? payload.type : "", payload.json, encode_typed, state);
        };
        const auto json_event = encode(false);
        const auto typed_event = encode(true);
        // The value as a typed producer holds it: encoded bytes, or codec nodes
        // (decoded once here; Pose and Float32Vector nodes borrow no payload).
        const ByteBuffer typed_bytes = encode_typed(payload.type, payload.json);
        CodecWorkspace node_workspace;
        const auto node_view = codec.decode_nodes(typed_bytes, node_workspace);
        const std::vector<R2KernelCodecNode> nodes(node_view.data, node_view.data + node_view.size);
        const auto encode_bytes = [&](const std::uint8_t* data, std::size_t size) {
            return wire::encode_typed_payload(payload.kind, operation_id, "move_to_pose", "component.bench", 1,
                                              payload.type, data, size, state);
        };
        if (encode_bytes(typed_bytes.data(), typed_bytes.size()) != typed_event) {
            std::cerr << payload.name << ": encoded-bytes event differs from the typed event\n";
            return 1;
        }
        const std::string prefix = payload.name;
        results[prefix + "_bytes_json"] = static_cast<double>(json_event.size());
        results[prefix + "_bytes_typed"] = static_cast<double>(typed_event.size());
        results[prefix + "_encode_json"] = measure([&] { return encode(false).size(); }, iterations, batches);
        results[prefix + "_encode_typed"] = measure([&] { return encode(true).size(); }, iterations, batches);
        results[prefix + "_encode_typed_bytes"] = measure([&] {
            return encode_bytes(typed_bytes.data(), typed_bytes.size()).size();
        }, iterations, batches);
        results[prefix + "_encode_typed_nodes"] = measure([&] {
            const auto view = codec.encode_nodes(nodes.data(), nodes.size(), workspace);
            return encode_bytes(view.data, view.size).size();
        }, iterations, batches);
        results[prefix + "_decode_json"] = measure([&] {
            return wire::to_message(wire::decode(json_event), decode_typed).payload_json.size();
        }, iterations, batches);
        results[prefix + "_decode_typed"] = measure([&] {
            return wire::to_message(wire::decode(typed_event), decode_typed).payload_json.size();
        }, iterations, batches);
        results[prefix + "_decode_typed_bytes"] = measure([&] {
            return wire::to_message(wire::decode(typed_event), decode_typed, false).payload_bytes.size();
        }, iterations, batches);
        results[prefix + "_decode_typed_nodes"] = measure([&] {
            const auto message = wire::to_message(wire::decode(typed_event), decode_typed, false);
            return codec.decode_nodes(message.payload_bytes, workspace).size;
        }, iterations, batches);
    }
    std::cout << "{";
    bool first = true;
    for (const auto& [name, value] : results) {
        std::cout << (first ? "" : ",") << "\"" << name << "\":" << std::fixed << std::setprecision(1) << value;
        first = false;
    }
    std::cout << "}\n";
    return 0;
}
