// C++ drive gateway: provides the public api.actuator.drive.v1 Control and reports arrival latency.
#include "benchmark.hpp"
#include "drive_payload.hpp"
#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#if defined(__unix__) || defined(__APPLE__)
#include <sys/resource.h>
#endif

namespace {

std::atomic<bool> g_running {true};

void handle_signal(int) {
    g_running.store(false);
}

struct Options {
    std::string name = "drive_gateway_cpp";
    std::filesystem::path manifest;
    std::string log_level = "INFO";
    int port = 0;
    std::uint64_t expected_count = 0;
    std::uint64_t warmup = 0;
    std::optional<std::size_t> channels;
    bool quiet = false;
    std::string report_format = "text";
    std::string report_path;
};

std::optional<std::string> string_arg(int argc, char** argv, const std::string& name) {
    const std::string key = "--" + name;
    const std::string prefix = key + "=";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == key && i + 1 < argc) return std::string(argv[i + 1]);
        if (arg.rfind(prefix, 0) == 0) return arg.substr(prefix.size());
    }
    return std::nullopt;
}

std::optional<std::int64_t> int_arg(int argc, char** argv, const std::string& name) {
    const auto value = string_arg(argc, argv, name);
    return value ? std::optional<std::int64_t>(std::stoll(*value)) : std::nullopt;
}

bool flag_arg(int argc, char** argv, const std::string& name) {
    const std::string key = "--" + name;
    for (int i = 1; i < argc; ++i) {
        if (argv[i] == key) return true;
    }
    return false;
}

Options parse_args(int argc, char** argv) {
    if (flag_arg(argc, argv, "help")) {
        std::cout << "Usage: gateway --port PORT [--name NAME] [--manifest PATH] [--log-level LEVEL]\n"
                  << "               [--expected-count N] [--warmup N] [--payload-size BYTES | --request-size BYTES]\n"
                  << "               [--quiet] [--report-format text|json] [--report-path PATH]\n";
        std::exit(0);
    }
    Options options;
    if (const auto value = string_arg(argc, argv, "name")) options.name = *value;
    if (const auto value = string_arg(argc, argv, "manifest")) options.manifest = *value;
    if (const auto value = string_arg(argc, argv, "log-level")) options.log_level = *value;
    if (const auto value = int_arg(argc, argv, "port")) options.port = static_cast<int>(*value);
    if (const auto value = int_arg(argc, argv, "expected-count"))
        options.expected_count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    if (const auto value = int_arg(argc, argv, "warmup"))
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    if (const auto value = int_arg(argc, argv, "payload-size")) options.channels = drive_fixture::channels_for_payload(*value);
    else if (const auto value = int_arg(argc, argv, "request-size")) options.channels = drive_fixture::channels_for_payload(*value);
    if (const auto value = string_arg(argc, argv, "report-format")) options.report_format = *value;
    if (const auto value = string_arg(argc, argv, "report-path")) options.report_path = *value;
    options.quiet = flag_arg(argc, argv, "quiet");
    if (options.name.empty()) throw std::runtime_error("name must not be empty");
    if (options.port <= 0 || options.port > 65535) {
        throw std::runtime_error("--port must be between 1 and 65535; the C++ wrapper does not allocate Control ports");
    }
    if (options.manifest.empty()) {
        options.manifest = r2kernel::tutorial::default_subsystem_manifest_path(__FILE__, options.name);
    }
    r2kernel::tutorial::validate_subsystem_provides(options.manifest, options.name, "control");
    return options;
}

double seconds_between(std::chrono::steady_clock::time_point from, std::chrono::steady_clock::time_point to) {
    if (from == std::chrono::steady_clock::time_point {} || to <= from) return 0.0;
    return std::chrono::duration<double>(to - from).count();
}

class DriveGatewaySubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit DriveGatewaySubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)), options_(std::move(options)) {}

    bool done() const noexcept { return done_.load(); }

    void write_report() const {
        std::lock_guard<std::mutex> guard(mutex_);
        const auto summary = stats_.summary();
        const double measured_elapsed_s = seconds_between(measured_started_at_, measured_finished_at_);
        std::string user_cpu = "unavailable", system_cpu = "unavailable", peak_rss = "unavailable";
#if defined(__unix__) || defined(__APPLE__)
        rusage usage {};
        if (getrusage(RUSAGE_SELF, &usage) == 0) {
            user_cpu = std::to_string(usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6);
            system_cpu = std::to_string(usage.ru_stime.tv_sec + usage.ru_stime.tv_usec / 1e6);
            std::int64_t rss = usage.ru_maxrss;
#if !defined(__APPLE__)
            rss *= 1024;
#endif
            peak_rss = std::to_string(rss);
        }
#endif
        const std::vector<std::pair<std::string, std::string>> extra {
            {"contract", drive_fixture::kContract},
            {"topic", drive_fixture::kTopic},
            {"handled", std::to_string(handled_)},
            {"measured", std::to_string(measured_)},
            {"warmup", std::to_string(options_.warmup)},
            {"expected_channels", options_.channels ? std::to_string(*options_.channels) : "any"},
            {"integrity_errors", std::to_string(integrity_errors_)},
            {"last_error", last_error_},
            {"measured_elapsed_s", std::to_string(measured_elapsed_s)},
            {"achieved_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? measured_ / measured_elapsed_s : 0.0)},
            {"total_elapsed_ms", std::to_string(seconds_between(run_started_at_, run_finished_at_) * 1000.0)},
            {"scheduled_requests", options_.expected_count == 0 ? "unbounded"
                : std::to_string(options_.expected_count + options_.warmup)},
            {"latency_scope", "request_sent_at_sync_to_gateway_acceptance"},
            {"process_user_cpu_s", user_cpu},
            {"process_system_cpu_s", system_cpu},
            {"process_peak_rss_bytes", peak_rss},
        };
        const auto content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("control", "gateway", summary, failures_, 0, extra)
            : r2kernel::tutorial::render_text_report("control", "gateway", summary, failures_, 0, extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        r2kernel::wrapper::ControlEndpointOptions endpoint;
        endpoint.request_type = drive_fixture::kRequestType;
        endpoint.response_type = drive_fixture::kResponseType;
        endpoint.timing_support = "required";
        if (!register_control_endpoint(drive_fixture::kTopic,
                [this](const r2kernel::wrapper::ControlEndpointRequest& request) { return handle(request); },
                endpoint)) {
            return false;
        }
        logger().info("Lifecycle", std::string("Drive gateway initialized contract=") + drive_fixture::kContract);
        return true;
    }

    bool on_start() override {
        run_started_at_ = std::chrono::steady_clock::now();
        if (bind_control(options_.port) <= 0 || !start_control_server()) return false;
        logger().info("Lifecycle", "Drive gateway listening on port " + std::to_string(get_control_port()));
        return true;
    }

    bool on_terminate() override {
        stop_control_server();
        run_finished_at_ = std::chrono::steady_clock::now();
        logger().info("Lifecycle", "Drive gateway terminated");
        return true;
    }

    bool on_reset() override {
        stop_control_server();
        return true;
    }

private:
    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = source.name;
        options.log_level = source.log_level;
        options.manifest_path = source.manifest;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions {source.name});
        return options;
    }

    r2kernel::wrapper::ControlEndpointResponse handle(const r2kernel::wrapper::ControlEndpointRequest& request) {
        const auto accepted_ns = request.accepted_at_sync_ns;
        const auto shape = drive_fixture::command_shape(request.payload_json);
        const bool valid = shape && (!options_.channels || shape->channels == *options_.channels)
            && request.payload == encode_message_json(drive_fixture::kRequestType,
                   drive_fixture::command_json(shape->seq, shape->channels));
        r2kernel::wrapper::ControlEndpointResponse response;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            ++handled_;
            if (!valid) {
                ++integrity_errors_;
                ++failures_;
                last_error_ = "DriveCommand differs from the fixture pattern";
            } else if (handled_ > options_.warmup) {
                ++measured_;
                const auto now = std::chrono::steady_clock::now();
                if (measured_started_at_ == std::chrono::steady_clock::time_point {}) measured_started_at_ = now;
                measured_finished_at_ = now;
                stats_.add_us(static_cast<double>(accepted_ns - request.timing.sent_at_sync_ns) / 1'000.0);
                if (options_.expected_count > 0 && measured_ >= options_.expected_count) done_.store(true);
            }
        }
        if (!options_.quiet && shape) {
            std::cout << name() << " topic=" << drive_fixture::kTopic << " seq=" << shape->seq
                      << " channels=" << shape->channels << (valid ? "" : " integrity_error=true") << "\n";
        }
        // A rejected command still answers an encodable (empty) DriveState with a failed status.
        response.ok = valid;
        response.payload_json = valid ? drive_fixture::state_json(shape->seq, shape->channels)
                                      : drive_fixture::state_json(0, 0);
        if (!valid) response.status_message = "invalid_drive_command";
        r2kernel::wrapper::ControlExecutionReport report;
        report.accepted_at_sync_ns = accepted_ns;
        report.scheduled_for_sync_ns = request.timing.execute_at_sync_ns > 0 ? request.timing.execute_at_sync_ns : accepted_ns;
        report.dispatched_at_sync_ns = accepted_ns;
        report.completed_at_sync_ns = now_ns();
        report.lateness_ns = std::max<std::int64_t>(0, report.dispatched_at_sync_ns - report.scheduled_for_sync_ns);
        report.final_status = !valid ? "failed" : report.lateness_ns > 0 ? "executed_late" : "executed";
        response.execution_report = report;
        return response;
    }

    Options options_;
    mutable std::mutex mutex_;
    r2kernel::tutorial::LatencyStats stats_;
    std::atomic<bool> done_ {false};
    std::uint64_t handled_ = 0;
    std::uint64_t measured_ = 0;
    std::uint64_t integrity_errors_ = 0;
    std::size_t failures_ = 0;
    std::string last_error_;
    std::chrono::steady_clock::time_point measured_started_at_ {};
    std::chrono::steady_clock::time_point measured_finished_at_ {};
    std::chrono::steady_clock::time_point run_started_at_ {};
    std::chrono::steady_clock::time_point run_finished_at_ {};
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    Options options;
    try {
        options = parse_args(argc, argv);
    } catch (const std::exception& error) {
        std::cerr << "Drive gateway configuration failed: " << error.what() << std::endl;
        return 2;
    }
    DriveGatewaySubsystem subsystem(options);
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
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    subsystem.terminate();
    subsystem.write_report();
    return 0;
}
