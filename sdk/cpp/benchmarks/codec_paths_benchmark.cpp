// In-process codec path micro-benchmark. Builds against any SDK
// version that has SchemaContext/PreparedCodec, so the same source measures a
// baseline and a candidate library in separate, interleaved processes.
//
// Cases (ns per call; the minimum over batches, which is the least sensitive
// estimator on a shared host):
//   fallback_{encode,decode}    unmanaged Subsystem (no prepared state)
//   prepared_{encode,decode}_json  PreparedCodec with a reused CodecWorkspace
//   prepared_decode_nodes       PreparedCodec native decode
//   managed_{encode,decode}     initialized Subsystem (prepared branch)
//   typed_transact              typed Control transaction through an echo CoreClient
//   timed_{transact,dispatch}_{median,p99}  per-call ns of a timed typed Control
//                               transaction (request encode) and of dispatching a
//                               timed typed request (request decode, response encode)
//
// Usage: r2kernel_cpp_codec_paths_benchmark [batches] [scale]
// Prints one JSON object. The process sets R2KERNEL_CONTROL_SOCKET to a path
// that does not exist, so heartbeat notifications fail fast offline.
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "../tests/offline_core_client.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include <unistd.h>

using namespace r2kernel::wrapper;
using Clock = std::chrono::steady_clock;

namespace {
// ActuatorCommand is the per-cycle servo payload and is stable across dictionary
// revisions, so baseline and candidate runs stay comparable.
constexpr char kCommand[] = "msg.actuator.servo.v1/ActuatorCommand";
constexpr char kBool[] = "msg.core.common.primitive.v1/BoolValue";
const std::string kCommandJson =
    "{\"emergency\":false,\"servo_cmd\":1,\"drive_mode\":[8,8,8,8,8,8],"
    "\"target\":{\"names\":[\"j1\",\"j2\",\"j3\",\"j4\",\"j5\",\"j6\"],"
    "\"target_position\":[0.5,1.0,1.5,2.0,2.5,3.0]}}";


class Bench final : public Subsystem {
public:
    Bench(SubsystemOptions options, bool typed) : Subsystem(std::move(options)), typed_(typed) {}
private:
    bool on_initialize() override {
        if (!typed_) return true;
        ControlEndpointOptions types;
        types.request_type = kCommand;
        types.response_type = kBool;
        types.timing_support = "optional";
        return register_control_endpoint("servo", [](const ControlEndpointRequest& request) {
            ControlEndpointResponse response;
            response.payload_json = "{\"value\":true}";
            if (request.timed) response.execution_report = ControlExecutionReport{};
            return response;
        }, types);
    }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
    bool typed_;
};

std::filesystem::path write_manifest(const std::filesystem::path& root, const std::string& name) {
    const auto path = root / (name + ".yaml");
    std::ofstream file(path);
    file << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: " << name << "\n  role: gateway\n"
            "provides:\n  local:\n    contract: bench.codec.private.v1\n    visibility: internal\n"
            "    endpoints:\n      controls: [servo]\n";
    if (!file) throw std::runtime_error("manifest write failed");
    return path;
}

SubsystemOptions options(const std::filesystem::path& root, const std::string& name, std::shared_ptr<CoreClient> client) {
    SubsystemOptions result;
    result.name = name;
    result.manifest_path = write_manifest(root, name);
    result.core_client = std::move(client);
    result.schema_roots = std::vector<std::string>{};
    result.log_sink = [](std::string_view, std::string_view, std::string_view, std::string_view) {};
    return result;
}

volatile std::size_t sink = 0;

double measure(const std::function<std::size_t()>& call, int iterations, int batches) {
    for (int i = 0; i < std::max(10, iterations / 10); ++i) sink = sink + call();
    std::vector<double> samples;
    for (int batch = 0; batch < batches; ++batch) {
        const auto started = Clock::now();
        for (int i = 0; i < iterations; ++i) sink = sink + call();
        const auto elapsed = std::chrono::duration<double, std::nano>(Clock::now() - started).count();
        samples.push_back(elapsed / iterations);
    }
    return *std::min_element(samples.begin(), samples.end());
}

// Per-call latency distribution (median and p99 in ns) for the timed cases.
void measure_calls(std::map<std::string, double>& results, const std::string& name,
                   const std::function<std::size_t()>& call, int iterations) {
    for (int i = 0; i < std::max(10, iterations / 10); ++i) sink = sink + call();
    std::vector<double> samples(static_cast<std::size_t>(iterations));
    for (auto& sample : samples) {
        const auto started = Clock::now();
        sink = sink + call();
        sample = std::chrono::duration<double, std::nano>(Clock::now() - started).count();
    }
    std::sort(samples.begin(), samples.end());
    results[name + "_median"] = samples[samples.size() / 2];
    results[name + "_p99"] = samples[samples.size() * 99 / 100];
}
}  // namespace

int main(int argc, char** argv) {
    const int batches = argc > 1 ? std::atoi(argv[1]) : 7;
    const int scale = argc > 2 ? std::atoi(argv[2]) : 1;
    const auto root = std::filesystem::temp_directory_path() /
        ("r2kernel-codec-bench-" + std::to_string(::getpid()));
    std::filesystem::create_directories(root);
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent.sock").c_str(), 1);
    ::setenv("R2KERNEL_CONTRACT_POLICY", "strict", 1);
    std::map<std::string, double> results;
    try {
        Bench unmanaged(options(root, "bench.unmanaged", nullptr), false);
        const auto wire = unmanaged.encode_message_json(kCommand, kCommandJson);
        results["fallback_encode"] = measure([&] { return unmanaged.encode_message_json(kCommand, kCommandJson).size(); }, 200 * scale, batches);
        results["fallback_decode"] = measure([&] { return unmanaged.decode_message_json(kCommand, wire).size(); }, 200 * scale, batches);

        const auto codec = SchemaContext(std::vector<std::string>{}).prepare(kCommand);
        CodecWorkspace workspace;
        results["prepared_encode_json"] = measure([&] { return codec.encode_json(kCommandJson, workspace).size; }, 20000 * scale, batches);
        results["prepared_decode_json"] = measure([&] { return codec.decode_json(wire, workspace).size(); }, 20000 * scale, batches);
        results["prepared_decode_nodes"] = measure([&] { return codec.decode_nodes(wire, workspace).size; }, 20000 * scale, batches);

        auto client = std::make_shared<r2kernel_test::OfflineCoreClient>();
        client->response = SchemaContext(std::vector<std::string>{}).prepare(kBool).encode_json("{\"value\":true}", workspace).copy();
        Bench managed(options(root, "bench.managed", client), true);
        managed.initialize();
        if (managed.state() != DeviceState::ready) throw std::runtime_error("managed benchmark subsystem not ready");
        results["managed_encode"] = measure([&] { return managed.encode_message_json(kCommand, kCommandJson).size(); }, 20000 * scale, batches);
        results["managed_decode"] = measure([&] { return managed.decode_message_json(kCommand, wire).size(); }, 20000 * scale, batches);
        ControlTransactOptions typed;
        typed.request_type = kCommand;
        typed.response_type = kBool;
        results["typed_transact"] = measure([&] {
            const auto response = managed.transact_control("peer", "servo", kCommandJson, typed);
            return response ? response->payload.size() : 0;
        }, 20000 * scale, batches);
        ControlTransactOptions timed = typed;
        timed.timing = ControlTimingOptions{};
        timed.timing->late_policy = "reject";
        measure_calls(results, "timed_transact", [&] {
            const auto response = managed.transact_control("peer", "servo", kCommandJson, timed);
            return response ? response->payload.size() : 0;
        }, 20000 * scale);
        if (!managed.start_control_server() || !client->handler) throw std::runtime_error("dispatch server not started");
        ControlRequest request;
        request.sender = "peer";
        request.topic = "servo";
        request.payload = client->last_payload;
        if (client->handler(request).status != 0) throw std::runtime_error("timed dispatch fixture failed");
        measure_calls(results, "timed_dispatch", [&] { return client->handler(request).payload.size(); }, 20000 * scale);
        managed.terminate();
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        std::filesystem::remove_all(root);
        return 1;
    }
    std::filesystem::remove_all(root);
    std::cout << '{';
    bool first = true;
    for (const auto& [name, value] : results) {
        std::cout << (first ? "" : ",") << '"' << name << "\":" << value;
        first = false;
    }
    std::cout << "}\n";
}
