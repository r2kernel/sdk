// C++ drive controller: sends timed api.actuator.drive.v1 drive_command requests and reports RTT.
#include "benchmark.hpp"
#include "drive_payload.hpp"
#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
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
    std::string name = "drive_controller_cpp";
    std::string log_level = "INFO";
    std::string binding = "gateway";
    std::filesystem::path manifest;
    std::filesystem::path deployment_manifest = r2kernel::tutorial::default_deployment_manifest_path(__FILE__);
    std::string target;
    std::int64_t payload_size = 256;
    std::size_t channels = drive_fixture::channels_for_payload(256);
    std::int64_t period_us = 1'000'000;
    std::uint64_t count = 0;
    std::uint64_t warmup = 0;
    int timeout_ms = 1000;
    int route_ready_timeout_ms = 30'000;
    float deadline_ms = 1000.0F;
    double validity_ms = 30'000.0;
    int priority = -1;
    std::filesystem::path start_gate_path;
    std::filesystem::path ready_path;
    std::int64_t gate_timeout_ms = 60'000;
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
        std::cout << "Usage: controller [--name NAME] [--log-level LEVEL] [--binding NAME] [--target SUBSYSTEM]\n"
                  << "                  [--manifest PATH] [--deployment-manifest PATH]\n"
                  << "                  [--topic-count 1] [--topic-prefix drive_command] [--request-mode round-robin]\n"
                  << "                  [--payload-size BYTES | --request-size BYTES] [--period-ms N | --period-us N]\n"
                  << "                  [--count N] [--warmup N] [--timeout-ms N] [--deadline-ms N] [--validity-ms N]\n"
                  << "                  [--route-ready-timeout-ms N] [--priority N]\n"
                  << "                  [--start-gate-path PATH --ready-path PATH] [--gate-timeout-ms N]\n"
                  << "                  [--quiet] [--report-format text|json] [--report-path PATH]\n";
        std::exit(0);
    }
    Options options;
    if (const auto value = string_arg(argc, argv, "name")) options.name = *value;
    if (const auto value = string_arg(argc, argv, "log-level")) options.log_level = *value;
    if (const auto value = string_arg(argc, argv, "binding")) options.binding = *value;
    if (const auto value = string_arg(argc, argv, "target")) options.target = *value;
    if (const auto value = string_arg(argc, argv, "manifest")) options.manifest = *value;
    if (const auto value = string_arg(argc, argv, "deployment-manifest")) options.deployment_manifest = *value;
    if (const auto value = int_arg(argc, argv, "topic-count"); value && *value != 1)
        throw std::runtime_error("api.actuator.drive.v1 declares one control; --topic-count must be 1");
    if (const auto value = string_arg(argc, argv, "topic-prefix"); value && *value != drive_fixture::kTopic)
        throw std::runtime_error(std::string("--topic-prefix must be ") + drive_fixture::kTopic);
    if (const auto value = string_arg(argc, argv, "request-mode"); value && *value != "round-robin")
        throw std::runtime_error("--request-mode must be round-robin");
    if (const auto value = int_arg(argc, argv, "payload-size")) options.payload_size = *value;
    else if (const auto value = int_arg(argc, argv, "request-size")) options.payload_size = *value;
    if (options.payload_size <= 0) throw std::runtime_error("--payload-size must be positive");
    options.channels = drive_fixture::channels_for_payload(options.payload_size);
    if (const auto value = int_arg(argc, argv, "period-us")) options.period_us = std::max<std::int64_t>(*value, 0);
    else if (const auto value = int_arg(argc, argv, "period-ms")) options.period_us = std::max<std::int64_t>(*value, 0) * 1000;
    if (const auto value = int_arg(argc, argv, "count"))
        options.count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    if (const auto value = int_arg(argc, argv, "warmup"))
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    if (const auto value = int_arg(argc, argv, "timeout-ms"))
        options.timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 1));
    if (const auto value = int_arg(argc, argv, "route-ready-timeout-ms"))
        options.route_ready_timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 0));
    if (const auto value = string_arg(argc, argv, "deadline-ms")) {
        options.deadline_ms = std::stof(*value);
    } else {
        options.deadline_ms = static_cast<float>(std::max<std::int64_t>(
            {options.timeout_ms, options.period_us / 1000 * 2, 1000}));
    }
    if (const auto value = string_arg(argc, argv, "validity-ms")) options.validity_ms = std::stod(*value);
    if (!std::isfinite(options.validity_ms) || options.validity_ms <= 0.0)
        throw std::runtime_error("--validity-ms must be positive");
    if (const auto value = int_arg(argc, argv, "priority")) options.priority = static_cast<int>(*value);
    if (const auto value = string_arg(argc, argv, "start-gate-path")) options.start_gate_path = *value;
    if (const auto value = string_arg(argc, argv, "ready-path")) options.ready_path = *value;
    if (const auto value = int_arg(argc, argv, "gate-timeout-ms")) options.gate_timeout_ms = std::max<std::int64_t>(*value, 1);
    if (const auto value = string_arg(argc, argv, "report-format")) options.report_format = *value;
    if (const auto value = string_arg(argc, argv, "report-path")) options.report_path = *value;
    options.quiet = flag_arg(argc, argv, "quiet");
    if (options.start_gate_path.empty() != options.ready_path.empty())
        throw std::runtime_error("--start-gate-path and --ready-path must be configured together");
    if (options.name.empty() || options.binding.empty()) throw std::runtime_error("name and binding must not be empty");
    if (options.manifest.empty())
        options.manifest = r2kernel::tutorial::default_subsystem_manifest_path(__FILE__, options.name);
    if (options.target.empty()) {
        options.target = r2kernel::tutorial::resolve_binding_target(
            options.deployment_manifest, options.name, options.binding).subsystem;
    }
    return options;
}

double seconds_between(std::chrono::steady_clock::time_point from, std::chrono::steady_clock::time_point to) {
    if (from == std::chrono::steady_clock::time_point {} || to <= from) return 0.0;
    return std::chrono::duration<double>(to - from).count();
}

std::string nullable_count(const std::optional<std::size_t>& value) {
    return value ? std::to_string(*value) : "null";
}

class DriveControllerSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit DriveControllerSubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)), options_(std::move(options)) {}

    ~DriveControllerSubsystem() override { stop_worker(); }

    bool done() const noexcept { return done_.load(); }
    std::size_t failures() const noexcept { return failures_; }

    void write_report() const {
        const auto summary = stats_.summary();
        const double measured_elapsed_s = seconds_between(measured_started_at_, measured_finished_at_);
        const auto measured_attempts = attempts_ > options_.warmup ? attempts_ - options_.warmup : 0;
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
            {"attempts", std::to_string(attempts_)},
            {"success", std::to_string(success_)},
            {"topic_count", "1"},
            {"payload_size", std::to_string(options_.payload_size)},
            {"channels", std::to_string(options_.channels)},
            {"request_encoded_bytes", nullable_count(request_encoded_bytes_)},
            {"period_us", std::to_string(options_.period_us)},
            {"timeout_ms", std::to_string(options_.timeout_ms)},
            {"route_ready_timeout_ms", std::to_string(options_.route_ready_timeout_ms)},
            {"deadline_ms", std::to_string(options_.deadline_ms)},
            {"validity_ms", std::to_string(options_.validity_ms)},
            {"priority", options_.priority < 0 ? "auto" : std::to_string(options_.priority)},
            {"request_mode", "round-robin"},
            {"warmup", std::to_string(options_.warmup)},
            {"warmup_failures", std::to_string(warmup_failures_)},
            {"scheduled_requests", options_.count == 0 ? "unbounded" : std::to_string(options_.count + options_.warmup)},
            {"integrity_errors", std::to_string(integrity_errors_)},
            {"deadline_violations", std::to_string(deadline_violations_)},
            {"period_violations", std::to_string(period_violations_)},
            {"late_cycles", std::to_string(late_cycles_)},
            {"last_error", last_error_},
            {"measured_elapsed_s", std::to_string(measured_elapsed_s)},
            {"achieved_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? measured_attempts / measured_elapsed_s : 0.0)},
            {"successful_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? summary.samples / measured_elapsed_s : 0.0)},
            {"rtt_measurement_scope", "caller_wall_clock_around_transact_control"},
            {"runtime_rtt_scope", "not_exposed_by_native_api"},
            {"total_elapsed_ms", std::to_string(seconds_between(run_started_at_, run_finished_at_) * 1000.0)},
            {"process_user_cpu_s", user_cpu},
            {"process_system_cpu_s", system_cpu},
            {"measured_process_cpu_s", measured_cpu_start_ >= 0 && measured_cpu_finish_ >= measured_cpu_start_
                ? std::to_string(static_cast<double>(measured_cpu_finish_ - measured_cpu_start_) / CLOCKS_PER_SEC)
                : "unavailable"},
            {"process_peak_rss_bytes", peak_rss},
        };
        const auto content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("control", "controller", summary, failures_, 0, extra)
            : r2kernel::tutorial::render_text_report("control", "controller", summary, failures_, 0, extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        logger().info("Lifecycle", "Drive controller initialized target=" + options_.target
            + " channels=" + std::to_string(options_.channels));
        return true;
    }

    bool on_start() override {
        worker_running_.store(true);
        worker_ = std::thread([this]() { run(); });
        return true;
    }

    bool on_terminate() override {
        stop_worker();
        logger().info("Lifecycle", "Drive controller terminated");
        return true;
    }

    bool on_reset() override {
        stop_worker();
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

    bool running() const { return worker_running_.load() && g_running.load(); }

    void stop_worker() {
        worker_running_.store(false);
        if (worker_.joinable()) worker_.join();
    }

    void run() {
        run_started_at_ = std::chrono::steady_clock::now();
        try {
            try {
                request_encoded_bytes_ = encode_message_json(drive_fixture::kRequestType,
                    drive_fixture::command_json(1, options_.channels)).size();
            } catch (const std::exception&) {
            }
            // Ready before route resolution and the first request: a cross-host
            // driver binds the requirement, then opens the gate.
            if (wait_for_release() && connect_route()) {
                for (std::uint64_t index = 0; index < options_.warmup && running(); ++index) {
                    if (!attempt(false)) ++warmup_failures_;
                }
                measured_loop();
            }
        } catch (const std::exception& error) {
            last_error_ = error.what();
            ++failures_;
        }
        run_finished_at_ = std::chrono::steady_clock::now();
        done_.store(true);
    }

    void measured_loop() {
        const auto period = std::chrono::microseconds(options_.period_us);
        auto next_tick = std::chrono::steady_clock::now();
        std::uint64_t measured = 0;
        while (running() && (options_.count == 0 || measured < options_.count)) {
            const auto now = std::chrono::steady_clock::now();
            if (now < next_tick) {
                std::this_thread::sleep_for(std::min<std::chrono::steady_clock::duration>(
                    next_tick - now, std::chrono::milliseconds(50)));
                continue;
            }
            if (measured_started_at_ == std::chrono::steady_clock::time_point {}) {
                measured_started_at_ = std::chrono::steady_clock::now();
                measured_cpu_start_ = std::clock();
            }
            attempt(true);
            ++measured;
            measured_finished_at_ = std::chrono::steady_clock::now();
            if (options_.period_us > 0) {
                next_tick += period;
                const auto finished = std::chrono::steady_clock::now();
                if (finished > next_tick) {
                    const auto missed = (finished - next_tick) / period + 1;
                    late_cycles_ += static_cast<std::uint64_t>(missed);
                    next_tick += period * missed;
                }
            }
        }
        measured_cpu_finish_ = std::clock();
    }

    bool attempt(bool measured) {
        const auto seq = ++seq_;
        if (seq >= drive_fixture::kMaxSequence) throw std::runtime_error("drive sequence exhausted");
        ++attempts_;
        r2kernel::wrapper::ControlTimingOptions timing;
        timing.generated_at_sync_ns = now_ns();
        timing.valid_for_ns = static_cast<std::int64_t>(options_.validity_ms * 1'000'000.0);
        r2kernel::wrapper::ControlTransactOptions transact;
        transact.period_ms = static_cast<float>(options_.period_us) / 1000.0F;
        transact.deadline_ms = options_.deadline_ms;
        transact.timeout_ms = std::max(options_.timeout_ms, static_cast<int>(std::ceil(options_.deadline_ms)));
        transact.priority = options_.priority;
        transact.request_type = drive_fixture::kRequestType;
        transact.response_type = drive_fixture::kResponseType;
        transact.timing = timing;
        const auto command = drive_fixture::command_json(seq, options_.channels);

        std::optional<r2kernel::wrapper::ControlResponse> response;
        std::string error;
        const auto started = std::chrono::steady_clock::now();
        try {
            response = transact_control(options_.target, drive_fixture::kTopic, command, transact);
        } catch (const std::exception& exc) {
            error = exc.what();
        }
        const auto elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - started).count();

        bool ok = false;
        bool integrity_ok = false;
        if (response && response->status == 0) {
            try {
                // An empty response type keeps the raw DriveState bytes for a byte-exact comparison.
                const auto decoded = decode_control_response(*response, "");
                ok = decoded.timing.status_code == 0;
                if (!ok) error = "drive_command status=" + decoded.timing.final_status;
                integrity_ok = ok && decoded.payload == encode_message_json(drive_fixture::kResponseType,
                    drive_fixture::state_json(seq, options_.channels));
            } catch (const std::exception& exc) {
                error = exc.what();
            }
        } else if (error.empty()) {
            error = response ? "Control transport status=" + std::to_string(response->status)
                             : "Control transaction returned no response";
        }
        if (measured) {
            const double elapsed_us = static_cast<double>(elapsed_ns) / 1'000.0;
            if (elapsed_ns > static_cast<std::int64_t>(options_.deadline_ms * 1'000'000.0)) ++deadline_violations_;
            if (options_.period_us > 0 && elapsed_ns > options_.period_us * 1'000) ++period_violations_;
            if (integrity_ok) stats_.add_us(elapsed_us);
        }
        if (integrity_ok) {
            ++success_;
            if (!options_.quiet) {
                std::cout << name() << " topic=" << drive_fixture::kTopic << " seq=" << seq
                          << " rtt_us=" << static_cast<double>(elapsed_ns) / 1'000.0 << "\n";
            }
            return true;
        }
        if (ok) {
            ++integrity_errors_;
            error = "DriveState differs from the fixture pattern";
        }
        last_error_ = error;
        if (measured) ++failures_;
        if (!options_.quiet) {
            std::cout << name() << " topic=" << drive_fixture::kTopic << " seq=" << seq << " failed error=" << error << "\n";
        }
        return false;
    }

    bool connect_route() {
        std::string host;
        int port = 0;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(options_.route_ready_timeout_ms);
        while (running()) {
            if (const auto route = resolve_control_target(options_.target)) {
                host = route->host;
                port = route->port;
            }
            if (!host.empty() && port >= 0) {
                if (connect_control(options_.target, host, port)) return true;
                last_error_ = "connect_control failed for " + options_.target;
                ++failures_;
                return false;
            }
            if (std::chrono::steady_clock::now() >= deadline) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        last_error_ = "Control target " + options_.target + " did not publish a route within "
            + std::to_string(options_.route_ready_timeout_ms) + "ms";
        ++failures_;
        return false;
    }

    bool wait_for_release() {
        if (options_.start_gate_path.empty()) return true;
        if (!options_.ready_path.parent_path().empty()) std::filesystem::create_directories(options_.ready_path.parent_path());
        const auto temporary = options_.ready_path.string() + ".tmp";
        {
            std::ofstream out(temporary);
            out << "{\"attempts\":" << attempts_ << ",\"ready_monotonic_ns\":"
                << std::chrono::duration_cast<std::chrono::nanoseconds>(
                       std::chrono::steady_clock::now().time_since_epoch()).count()
                << "}";
        }
        std::filesystem::rename(temporary, options_.ready_path);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(options_.gate_timeout_ms);
        while (running() && !std::filesystem::exists(options_.start_gate_path)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                last_error_ = "start gate timed out";
                ++failures_;
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        return running();
    }

    Options options_;
    std::thread worker_;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> done_ {false};
    r2kernel::tutorial::LatencyStats stats_;
    std::uint64_t seq_ = 0;
    std::uint64_t attempts_ = 0;
    std::uint64_t success_ = 0;
    std::size_t failures_ = 0;
    std::uint64_t warmup_failures_ = 0;
    std::uint64_t integrity_errors_ = 0;
    std::uint64_t deadline_violations_ = 0;
    std::uint64_t period_violations_ = 0;
    std::uint64_t late_cycles_ = 0;
    std::optional<std::size_t> request_encoded_bytes_;
    std::string last_error_;
    std::chrono::steady_clock::time_point measured_started_at_ {};
    std::chrono::steady_clock::time_point measured_finished_at_ {};
    std::chrono::steady_clock::time_point run_started_at_ {};
    std::chrono::steady_clock::time_point run_finished_at_ {};
    std::clock_t measured_cpu_start_ = -1;
    std::clock_t measured_cpu_finish_ = -1;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    Options options;
    try {
        options = parse_args(argc, argv);
    } catch (const std::exception& error) {
        std::cerr << "Drive controller configuration failed: " << error.what() << std::endl;
        return 2;
    }
    DriveControllerSubsystem subsystem(options);
    subsystem.initialize();
    if (subsystem.state() == r2kernel::wrapper::DeviceState::error) {
        subsystem.write_report();
        return 1;
    }
    subsystem.start();
    if (subsystem.state() == r2kernel::wrapper::DeviceState::error) {
        subsystem.terminate();
        subsystem.write_report();
        return 1;
    }
    while (g_running.load() && !subsystem.done()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    subsystem.terminate();
    subsystem.write_report();
    return subsystem.failures() == 0 ? 0 : 1;
}
