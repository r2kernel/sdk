// In-process micro-benchmark of one Procedure call round trip through a
// loopback CoreClient (caller encode, provider dispatch and handler, caller
// decode; no transport): an untyped JSON Procedure, a typed Procedure called
// and handled with JSON text (converted with the prepared codecs on both
// sides), and the same typed Procedure with encoded payloads (the bytes
// call_procedure overload and a ProcedureOptions::native_payload handler).
// Every handler echoes a Pose. Informational: it does not run under CTest.
//
// Cases (ns per call; the minimum over batches):
//   call_json            call_procedure(params_json): the untyped JSON lane
//                        ("procedure.call"), JSON handler, no codec
//   call_typed_json      call_typed_procedure(params_json), JSON typed handler
//   call_typed_bytes     call_procedure(params bytes), native typed handler
//   call_typed_nodes     that + encode_nodes before and decode_nodes after
//                        (a caller that holds the value as codec nodes)
//
// Usage: r2kernel_cpp_procedure_payload_benchmark [batches] [scale]
// Prints one JSON object. R2KERNEL_CONTROL_SOCKET points to a missing path so
// heartbeat notifications fail fast offline.
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "../tests/offline_core_client.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include <unistd.h>

using namespace r2kernel::wrapper;
using Clock = std::chrono::steady_clock;

namespace {
constexpr char kPose[] = "msg.core.spatial.v1/Pose";
const std::string kPoseJson =
    "{\"position\":{\"x\":1.0,\"y\":2.5,\"z\":-0.25},\"orientation\":{\"x\":0.0,\"y\":0.0,\"z\":0.5,\"w\":1.0}}";
constexpr char kName[] = "bench.procedure";

class Bench final : public Subsystem {
public:
    using Subsystem::Subsystem;
private:
    bool on_initialize() override {
        ProcedureOptions typed;
        typed.request_type = kPose;
        typed.response_type = kPose;
        ProcedureOptions native = typed;
        native.native_payload = true;
        return register_procedure("untyped", [](ProcedureRequest request) {
                   ProcedureResponse response;
                   response.ok = true;
                   response.result_json = std::move(request.params_json);
                   return response;
               }, typed)
            && register_procedure("typed_json", [](ProcedureRequest request) {
                   ProcedureResponse response;
                   response.ok = true;
                   response.result_json = std::move(request.params_json);
                   return response;
               }, typed)
            && register_procedure("typed_native", [](ProcedureRequest request) {
                   ProcedureResponse response;
                   response.ok = true;
                   response.result_payload = std::move(request.params_payload);
                   return response;
               }, native);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

volatile std::size_t sink = 0;

double measure(const std::function<std::size_t()>& call, int iterations, int batches) {
    for (int i = 0; i < std::max(10, iterations / 10); ++i) sink = sink + call();
    std::vector<double> samples;
    for (int batch = 0; batch < batches; ++batch) {
        const auto started = Clock::now();
        for (int i = 0; i < iterations; ++i) sink = sink + call();
        samples.push_back(std::chrono::duration<double, std::nano>(Clock::now() - started).count() / iterations);
    }
    return *std::min_element(samples.begin(), samples.end());
}
}  // namespace

int main(int argc, char** argv) {
    const int batches = argc > 1 ? std::atoi(argv[1]) : 7;
    const int scale = argc > 2 ? std::atoi(argv[2]) : 1;
    const int iterations = 5000 * std::max(scale, 1);
    const auto root = std::filesystem::temp_directory_path() / ("r2kernel-procedure-bench-" + std::to_string(::getpid()));
    std::filesystem::create_directories(root);
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent.sock").c_str(), 1);
    std::map<std::string, double> results;
    try {
        const auto manifest = root / "bench.yaml";
        {
            std::ofstream file(manifest);
            file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << kName << "\n  role: component\n"
                    "provides:\n  local:\n    contract: bench.procedure.private.v1\n    visibility: internal\n"
                    "    endpoints:\n      procedures: [untyped, typed_json, typed_native]\n";
            if (!file) throw std::runtime_error("manifest write failed");
        }
        auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
        client->loopback = true;
        SubsystemOptions options;
        options.name = kName;
        options.manifest_path = manifest;
        options.user_data_path = root / "userdata";
        options.core_client = client;
        options.schema_roots = std::vector<std::string>{};
        options.log_sink = [](std::string_view, std::string_view, std::string_view, std::string_view) {};
        Bench bench(std::move(options));
        bench.initialize();
        if (bench.state() != DeviceState::ready || !bench.start_procedure_server())
            throw std::runtime_error("benchmark subsystem did not start");

        const auto codec = bench.prepared_codec(kPose);
        CodecWorkspace workspace;
        const ByteBuffer pose = codec.encode_json(kPoseJson, workspace).copy();
        const auto node_view = codec.decode_nodes(pose, workspace);
        const std::vector<R2KernelCodecNode> nodes(node_view.data, node_view.data + node_view.size);
        ProcedureCallOptions typed;
        typed.request_type = kPose;
        typed.response_type = kPose;
        const auto checked = [](const std::optional<ProcedureResponse>& response) {
            if (!response || !response->ok) throw std::runtime_error(response ? response->error_message : "no response");
            return response->result_json.size() + response->result_payload.size();
        };
        if (bench.call_procedure(kName, "typed_native", pose, typed)->result_payload != pose)
            throw std::runtime_error("native echo differs");
        results["call_json"] = measure([&] { return checked(bench.call_procedure(kName, "untyped", kPoseJson)); },
                                       iterations, batches);
        results["call_typed_json"] = measure([&] {
            return checked(bench.call_typed_procedure(kName, "typed_json", kPose, kPose, kPoseJson));
        }, iterations, batches);
        results["call_typed_bytes"] = measure([&] { return checked(bench.call_procedure(kName, "typed_native", pose, typed)); },
                                              iterations, batches);
        results["call_typed_nodes"] = measure([&] {
            const ByteBuffer params = codec.encode_nodes(nodes.data(), nodes.size(), workspace).copy();
            const auto response = bench.call_procedure(kName, "typed_native", params, typed);
            checked(response);
            return codec.decode_nodes(response->result_payload, workspace).size;
        }, iterations, batches);
        bench.terminate();
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        std::filesystem::remove_all(root);
        return 1;
    }
    std::filesystem::remove_all(root);
    std::cout << '{';
    bool first = true;
    for (const auto& [name, value] : results) {
        std::cout << (first ? "" : ",") << '"' << name << "\":" << std::fixed << std::setprecision(1) << value;
        first = false;
    }
    std::cout << "}\n";
    return 0;
}
