#include "benchmark.hpp"
#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "rt_profile.hpp"

#include <atomic>
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <utility>
#include <vector>
#if defined(__unix__) || defined(__APPLE__)
#include <sys/resource.h>
#endif

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "control_gateway_cpp";
    std::filesystem::path manifest;
    std::string log_level = "INFO";
    int port = 0;
    std::uint64_t expected_count = 0;
    std::uint64_t warmup = 0;
    std::size_t request_size = 8;
    std::vector<std::size_t> request_sizes {8};
    std::size_t response_size = 8;
    std::int64_t processing_delay_us = 0;
    bool quiet = false;
    std::string report_format = "text";
    std::string report_path;
    fixture_rt::Options rt;
    std::int64_t worker_ready_timeout_ms = 30'000;
};

void handle_signal(int) {
    g_running.store(false);
}

std::optional<std::string> string_arg(int argc, char** argv, const std::string& name) {
    const std::string key = "--" + name;
    const std::string prefix = key + "=";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == key && i + 1 < argc) {
            return std::string(argv[++i]);
        }
        if (arg.rfind(prefix, 0) == 0) {
            return arg.substr(prefix.size());
        }
    }
    return std::nullopt;
}

std::optional<std::int64_t> int_arg(int argc, char** argv, const std::string& name) {
    const auto value = string_arg(argc, argv, name);
    if (!value) {
        return std::nullopt;
    }
    return std::stoll(*value);
}

std::vector<std::size_t> parse_sizes(const std::string& value) {
    std::vector<std::size_t> sizes;
    std::istringstream input(value);
    std::string item;
    while (std::getline(input, item, ',')) {
        if (item.empty()) {
            throw std::runtime_error("request-sizes contains an empty value");
        }
        const auto parsed = std::stoll(item);
        if (parsed < 8) {
            throw std::runtime_error("request-sizes entries must be at least 8 bytes");
        }
        sizes.push_back(static_cast<std::size_t>(parsed));
    }
    if (sizes.empty()) {
        throw std::runtime_error("request-sizes must contain one or more sizes");
    }
    return sizes;
}

std::string render_sizes(const std::vector<std::size_t>& sizes) {
    std::ostringstream output;
    for (std::size_t index = 0; index < sizes.size(); ++index) {
        if (index > 0) {
            output << ',';
        }
        output << sizes[index];
    }
    return output.str();
}

bool flag_arg(int argc, char** argv, const std::string& name) {
    const std::string key = "--" + name;
    for (int i = 1; i < argc; ++i) {
        if (argv[i] == key) {
            return true;
        }
    }
    return false;
}

Options parse_args(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: gateway [--name NAME] [--manifest PATH] [--log-level LEVEL] [--port PORT]\n"
                << "               [--expected-count N] [--warmup N]\n"
                << "               [--request-size BYTES | --request-sizes CSV] [--response-size BYTES]\n"
                << "               [--processing-delay-us N]\n"
                << "               [--rt-policy other|fifo|rr --rt-priority N --rt-cpus CSV] [--rt-require-isolated]\n"
                << "               [--worker-ready-path PATH --worker-ready-timeout-ms N]\n"
                << "               [--quiet] [--report-format text|json] [--report-path PATH]\n";
            std::exit(0);
        }
    }

    Options options;
    options.rt = fixture_rt::parse(argc, argv);
    if (const auto value = int_arg(argc, argv, "worker-ready-timeout-ms")) {
        if (*value <= 0) throw std::runtime_error("worker-ready-timeout-ms must be positive");
        options.worker_ready_timeout_ms = *value;
    }
    if (const auto value = string_arg(argc, argv, "name")) {
        options.name = *value;
    }
    if (const auto value = string_arg(argc, argv, "manifest")) {
        options.manifest = *value;
    }
    if (const auto value = string_arg(argc, argv, "log-level")) {
        options.log_level = *value;
    }
    if (const auto value = int_arg(argc, argv, "port")) {
        options.port = static_cast<int>(*value);
    }
    if (const auto value = int_arg(argc, argv, "expected-count")) {
        options.expected_count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "warmup")) {
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "request-size")) {
        options.request_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
        options.request_sizes = {options.request_size};
    } else if (const auto value = int_arg(argc, argv, "payload-size")) {
        options.request_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
        options.request_sizes = {options.request_size};
    }
    if (const auto value = string_arg(argc, argv, "request-sizes")) {
        options.request_sizes = parse_sizes(*value);
    }
    if (const auto value = int_arg(argc, argv, "response-size")) {
        options.response_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
    }
    if (const auto value = int_arg(argc, argv, "processing-delay-us")) {
        options.processing_delay_us = std::max<std::int64_t>(*value, 0);
    }
    if (const auto value = string_arg(argc, argv, "report-format")) {
        options.report_format = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-path")) {
        options.report_path = *value;
    }
    options.quiet = flag_arg(argc, argv, "quiet");
    if (options.name.empty()) {
        throw std::runtime_error("name must not be empty");
    }
    if (options.port <= 0 || options.port > 65535) {
        throw std::runtime_error(
            "--port must be between 1 and 65535; automatic Control port allocation is unavailable "
            "in the current C++ wrapper"
        );
    }
    if (options.manifest.empty()) {
        options.manifest = r2kernel::tutorial::default_subsystem_manifest_path(__FILE__, options.name);
    }
    r2kernel::tutorial::validate_subsystem_provides(options.manifest, options.name, "control");
    return options;
}

class GatewaySubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit GatewaySubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)), options_(std::move(options)) {}

    bool done() const noexcept {
        return done_.load();
    }

    bool poll_worker_ready() {
        if ((!options_.rt.enabled && !options_.rt.measurement_ns && options_.rt.worker_ready_path.empty())
            || worker_marker_written_) return !worker_setup_failed_;
        const auto workers = control_worker_status();
        if (workers.empty()) {
            if (fixture_rt::monotonic_ns() - server_started_ns_ > options_.worker_ready_timeout_ms * 1'000'000) {
                worker_setup_failed_ = true;
                ++failures_;
                setup_error_ = "dataworker readiness timed out";
                fixture_rt::write_json(options_.rt.worker_ready_path,
                    "{\"provider_ready\":false,\"ok\":false,\"error\":\"dataworker readiness timed out\",\"workers\":[]}");
            }
            return !worker_setup_failed_;
        }
        std::ostringstream profiles;
        profiles << '[';
        for (std::size_t index = 0; index < workers.size(); ++index) {
            const auto& worker = workers[index];
            if (index) profiles << ',';
            bool ok = worker.ready;
            std::string error = worker.error.value_or("");
            if (ok && options_.rt.enabled && (worker.policy != options_.rt.policy
                || worker.priority != options_.rt.priority || worker.cpus != options_.rt.cpus)) {
                ok = false; error = "dataworker RT profile readback mismatch";
            }
            try {
                profiles << fixture_rt::profile_json(worker.tid, worker.policy, worker.priority,
                    worker.cpus, ok, error, options_.rt.require_isolated && ok, options_.rt.enabled);
            } catch (const std::exception& isolation_error) {
                ok = false; error = isolation_error.what();
                profiles << fixture_rt::profile_json(worker.tid, worker.policy, worker.priority,
                    worker.cpus, false, error, false, options_.rt.enabled);
            }
            if (!ok) { worker_setup_failed_ = true; setup_error_ = error; ++failures_; }
        }
        profiles << ']';
        worker_profiles_ = profiles.str();
        const auto marker = "{\"phase\":\"profile_applied\",\"ready_monotonic_ns\":"
            + std::to_string(fixture_rt::monotonic_ns()) + ",\"provider_ready\":false,\"workers\":"
            + worker_profiles_ + ",\"ok\":" + (worker_setup_failed_ ? "false" : "true")
            + ",\"isolation_evidence\":" + fixture_rt::isolation_evidence(
                options_.rt.cpus, options_.rt.require_isolated) + "}";
        fixture_rt::write_json(options_.rt.worker_ready_path, marker);
        worker_marker_written_ = true;
        return !worker_setup_failed_;
    }

    bool worker_setup_failed() const { return worker_setup_failed_; }

    void write_report() const {
        const auto summary = stats_.summary();
        const double measured_elapsed_s = elapsed_seconds(measured_started_at_, measured_finished_at_);
        const double total_elapsed_ms = elapsed_ms(run_started_at_, run_finished_at_);
        const auto process_usage = process_resource_metrics();
        const std::vector<std::pair<std::string, std::string>> extra {
            {"handled", std::to_string(handled_.load())},
            {"measured", std::to_string(measured_.load())},
            {"handshakes", std::to_string(handshakes_.load())},
            {"warmup", std::to_string(options_.warmup)},
            {"request_payload_size", render_sizes(options_.request_sizes)},
            {"request_payload_sizes", render_sizes(options_.request_sizes)},
            {"response_payload_size", std::to_string(options_.response_size)},
            {"scheduled_requests", options_.expected_count == 0 ? "unbounded" : std::to_string(options_.expected_count + options_.warmup)},
            {"integrity_errors", std::to_string(integrity_errors_.load())},
            {"measured_elapsed_s", std::to_string(measured_elapsed_s)},
            {"achieved_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? static_cast<double>(measured_.load()) / measured_elapsed_s : 0.0)},
            {"latency_scope", "request_timestamp_to_gateway_arrival"},
            {"total_elapsed_ms", std::to_string(total_elapsed_ms)},
            {"process_user_cpu_s", std::get<0>(process_usage)},
            {"process_system_cpu_s", std::get<1>(process_usage)},
            {"process_peak_rss_bytes", std::get<2>(process_usage)},
            {"processing_delay_us", std::to_string(options_.processing_delay_us)},
        };
        std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("control", "gateway", summary, failures_.load(), 0, extra)
            : r2kernel::tutorial::render_text_report("control", "gateway", summary, failures_.load(), 0, extra);
        if (options_.rt.enabled || options_.rt.measurement_ns || !options_.rt.worker_ready_path.empty()) {
            const auto periodic = "{\"scope\":\"synthetic_echo_provider\",\"language\":\"cpp\",\"worker_ready\":"
                + std::string(worker_marker_written_ && !worker_setup_failed_ ? "true" : "false")
                + ",\"provider_ready\":false,\"thread_profiles\":" + worker_profiles_
                + ",\"setup_error\":" + (setup_error_.empty() ? "null" : fixture_rt::quote(setup_error_))
                + ",\"isolation_evidence\":" + fixture_rt::isolation_evidence(
                    options_.rt.cpus, options_.rt.require_isolated) + "}";
            if (options_.report_format == "json") content = fixture_rt::with_periodic(std::move(content), periodic);
            else content += "\nperiodic: " + periodic;
        }
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        for (const std::string topic : {"control_cmd", "control_hello"}) {
            if (!register_raw_control_endpoint(topic, [this](r2kernel::wrapper::ControlRequest request) {
                    return handle_request(std::move(request));
                })) {
                return false;
            }
        }
        logger().info("Lifecycle", "Control gateway initialized");
        return true;
    }

    bool on_start() override {
        run_started_at_ = std::chrono::steady_clock::now();
        server_started_ns_ = fixture_rt::monotonic_ns();
        if (options_.rt.enabled)
            configure_control_worker(fixture_rt::policy_name(options_.rt.policy), options_.rt.priority, options_.rt.cpus);
        if (options_.port > 0 && bind_control(options_.port) <= 0) {
            return false;
        }
        const bool ok = start_control_server();
        if (ok) {
            logger().info("Lifecycle", "Control gateway listening on port " + std::to_string(get_control_port()));
        }
        return ok;
    }

    bool on_terminate() override {
        stop_control_server();
        run_finished_at_ = std::chrono::steady_clock::now();
        logger().info("Lifecycle", "Control gateway terminated");
        return true;
    }

    bool on_reset() override {
        stop_control_server();
        return true;
    }

private:
    static double elapsed_ms(
        std::chrono::steady_clock::time_point started_at,
        std::chrono::steady_clock::time_point finished_at
    ) {
        if (started_at == std::chrono::steady_clock::time_point{} || finished_at <= started_at) {
            return 0.0;
        }
        return std::chrono::duration<double, std::milli>(finished_at - started_at).count();
    }

    static double elapsed_seconds(
        std::chrono::steady_clock::time_point started_at,
        std::chrono::steady_clock::time_point finished_at
    ) {
        if (started_at == std::chrono::steady_clock::time_point{} || finished_at <= started_at) {
            return 0.0;
        }
        return std::chrono::duration<double>(finished_at - started_at).count();
    }

    static std::tuple<std::string, std::string, std::string> process_resource_metrics() {
#if defined(__unix__) || defined(__APPLE__)
        rusage usage {};
        if (getrusage(RUSAGE_SELF, &usage) == 0) {
            const double user_seconds = static_cast<double>(usage.ru_utime.tv_sec)
                + static_cast<double>(usage.ru_utime.tv_usec) / 1'000'000.0;
            const double system_seconds = static_cast<double>(usage.ru_stime.tv_sec)
                + static_cast<double>(usage.ru_stime.tv_usec) / 1'000'000.0;
            std::int64_t peak_rss_bytes = static_cast<std::int64_t>(usage.ru_maxrss);
#if !defined(__APPLE__)
            peak_rss_bytes *= 1024;
#endif
            return {std::to_string(user_seconds), std::to_string(system_seconds), std::to_string(peak_rss_bytes)};
        }
        return {"unavailable", "unavailable", "unavailable"};
#else
        return {"unavailable", "unavailable", "unavailable"};
#endif
    }

    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = source.name;
        options.log_level = source.log_level;
        options.manifest_path = source.manifest;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions{source.name});
        return options;
    }

    r2kernel::wrapper::ControlResponse handle_request(r2kernel::wrapper::ControlRequest request) {
        const bool is_handshake = request.topic == "control_hello";
        bool request_integrity_ok = true;
        if (is_handshake) {
            ++handshakes_;
        } else {
            ++handled_;
            const auto seq = r2kernel::tutorial::payload_seq(request.payload);
            request_integrity_ok = false;
            for (const std::size_t request_size : options_.request_sizes) {
                if (request.payload.size() == request_size && request_size >= 8
                    && std::all_of(request.payload.begin() + 8, request.payload.end(),
                        [seq](std::uint8_t byte) { return byte == static_cast<std::uint8_t>(seq & 0xffU); })) {
                    request_integrity_ok = true;
                    break;
                }
            }
            if (!request_integrity_ok) {
                ++integrity_errors_;
                ++failures_;
            }
        }
        const auto seq = r2kernel::tutorial::payload_seq(request.payload);
        const double latency_us = static_cast<double>(now_ns() - request.timestamp_ns) / 1'000.0;
        if (!is_handshake && handled_.load() > options_.warmup) {
            ++measured_;
            if (measured_started_at_ == std::chrono::steady_clock::time_point{}) {
                measured_started_at_ = std::chrono::steady_clock::now();
            }
            if (!options_.rt.measurement_ns) stats_.add_us(latency_us);
            if (!options_.rt.measurement_ns && options_.expected_count > 0 && measured_.load() >= options_.expected_count) {
                done_.store(true);
                g_running.store(false);
            }
        }
        if (!options_.quiet && !options_.rt.measurement_ns) {
            std::cout << name() << " topic=" << request.topic << " seq=" << seq << " latency_us=" << latency_us << "\n";
        }
        if (options_.processing_delay_us > 0) {
            std::this_thread::sleep_for(std::chrono::microseconds(options_.processing_delay_us));
        }
        if (!is_handshake) {
            measured_finished_at_ = std::chrono::steady_clock::now();
        }

        r2kernel::wrapper::ControlResponse response;
        response.timestamp_ns = now_ns();
        response.status = request_integrity_ok ? 0 : 1;
        response.payload = is_handshake || (request_integrity_ok && request.payload.size() == options_.response_size)
            ? std::move(request.payload)
            : r2kernel::tutorial::make_payload(seq, options_.response_size);
        return response;
    }

    Options options_;
    mutable r2kernel::tutorial::LatencyStats stats_;
    std::atomic<bool> done_ {false};
    std::atomic<std::uint64_t> handled_ {0};
    std::atomic<std::uint64_t> measured_ {0};
    std::atomic<std::uint64_t> handshakes_ {0};
    std::atomic<std::uint64_t> integrity_errors_ {0};
    std::atomic<std::uint64_t> failures_ {0};
    std::chrono::steady_clock::time_point measured_started_at_ {};
    std::chrono::steady_clock::time_point measured_finished_at_ {};
    std::chrono::steady_clock::time_point run_started_at_ {};
    std::chrono::steady_clock::time_point run_finished_at_ {};
    std::int64_t server_started_ns_ = 0;
    bool worker_marker_written_ = false;
    bool worker_setup_failed_ = false;
    std::string worker_profiles_ = "[]";
    std::string setup_error_;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    Options options;
    try {
        options = parse_args(argc, argv);
    } catch (const std::exception& error) {
        std::cerr << "Gateway configuration failed: " << error.what() << std::endl;
        return 2;
    }
    GatewaySubsystem subsystem(options);
    subsystem.initialize();
    if (subsystem.state() == r2kernel::wrapper::DeviceState::error) {
        subsystem.write_report();
        return 1;
    }
    subsystem.start();
    if (subsystem.state() == r2kernel::wrapper::DeviceState::error) {
        subsystem.write_report();
        return 1;
    }
    while (g_running.load() && !subsystem.done()) {
        if (!subsystem.poll_worker_ready()) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    subsystem.write_report();
    return subsystem.worker_setup_failed() ? 1 : 0;
}
