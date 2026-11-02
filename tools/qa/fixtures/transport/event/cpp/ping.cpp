#include "benchmark.hpp"
#include "start_gate.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "event_ping";
    std::string log_level = "INFO";
    std::string request_topic = "event_ping_request";
    std::string response_topic = "event_ping_response";
    std::size_t requested_payload_size = 8;
    std::size_t payload_size = 8;
    std::int64_t period_us = 1'000'000;
    std::uint64_t count = 0;
    std::uint64_t warmup = 0;
    std::size_t queue_depth = 0;
    std::int64_t startup_delay_ms = 0;
    int timeout_ms = 1000;
    std::string start_gate_file;
    std::string start_gate_session;
    int start_gate_timeout_ms = 30000;
    bool quiet = false;
    std::string report_format = "text";
    std::string report_path;
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
                << "Usage: ping [--name NAME] [--log-level LEVEL]\n"
                << "            [--request-topic TOPIC] [--response-topic TOPIC]\n"
                << "            [--payload-size BYTES] [--period-ms N | --period-us N]\n"
                << "            [--count N] [--warmup N] [--queue-depth N]\n"
                << "            [--startup-delay-ms N] [--timeout-ms N]\n"
                << "            [--start-gate-file PATH --start-gate-session ID] [--start-gate-timeout-ms N]\n"
                << "            [--quiet] [--report-format text|json] [--report-path PATH]\n";
            std::exit(0);
        }
    }

    Options options;
    if (const auto value = string_arg(argc, argv, "name")) {
        options.name = *value;
    }
    if (const auto value = string_arg(argc, argv, "log-level")) {
        options.log_level = *value;
    }
    if (const auto value = string_arg(argc, argv, "request-topic")) {
        options.request_topic = *value;
    }
    if (const auto value = string_arg(argc, argv, "response-topic")) {
        options.response_topic = *value;
    }
    if (const auto value = int_arg(argc, argv, "payload-size")) {
        options.requested_payload_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 0));
        options.payload_size = r2kernel::tutorial::effective_payload_size(options.requested_payload_size);
    }
    if (const auto value = int_arg(argc, argv, "period-us")) {
        options.period_us = std::max<std::int64_t>(*value, 0);
    } else if (const auto value = int_arg(argc, argv, "period-ms")) {
        options.period_us = std::max<std::int64_t>(*value, 0) * 1000;
    }
    if (const auto value = int_arg(argc, argv, "count")) {
        options.count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "warmup")) {
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "queue-depth")) {
        options.queue_depth = static_cast<std::size_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "startup-delay-ms")) {
        options.startup_delay_ms = std::max<std::int64_t>(*value, 0);
    }
    if (const auto value = int_arg(argc, argv, "timeout-ms")) {
        options.timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 1));
    }
    if (const auto value = string_arg(argc, argv, "start-gate-file")) {
        options.start_gate_file = *value;
    }
    if (const auto value = string_arg(argc, argv, "start-gate-session")) {
        options.start_gate_session = *value;
    }
    if (const auto value = int_arg(argc, argv, "start-gate-timeout-ms")) {
        options.start_gate_timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 1));
    }
    if (const auto value = string_arg(argc, argv, "report-format")) {
        options.report_format = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-path")) {
        options.report_path = *value;
    }
    options.quiet = flag_arg(argc, argv, "quiet");
    return options;
}

class PingSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit PingSubsystem(Options options, StartGate start_gate)
        : r2kernel::wrapper::Subsystem(make_options(options)),
          requested_payload_size_(options.requested_payload_size),
          options_(std::move(options)),
          start_gate_(std::move(start_gate)) {}

    ~PingSubsystem() override {
        stop_worker();
    }

    bool done() const noexcept {
        return done_.load();
    }

    void write_report() const {
        const auto summary = stats_.summary();
        const auto total_elapsed_ms = elapsed_ms(run_started_at_, run_finished_at_);
        const auto first_success_ms = first_success_delay_ms_;
        const auto first_measured_success_ms = first_measured_success_delay_ms_;
        const std::vector<std::pair<std::string, std::string>> extra {
            {"request_topic", options_.request_topic},
            {"response_topic", options_.response_topic},
            {"attempts", std::to_string(attempts_.load())},
            {"success", std::to_string(success_.load())},
            {"payload_size_requested", std::to_string(requested_payload_size_)},
            {"payload_size_effective", std::to_string(options_.payload_size)},
            {"period_us", std::to_string(options_.period_us)},
            {"timeout_ms", std::to_string(options_.timeout_ms)},
            {"warmup", std::to_string(options_.warmup)},
            {"total_elapsed_ms", std::to_string(total_elapsed_ms)},
            {"first_success_ms", first_success_ms >= 0.0 ? std::to_string(first_success_ms) : "null"},
            {"first_measured_success_ms", first_measured_success_ms >= 0.0 ? std::to_string(first_measured_success_ms) : "null"},
            {"unexpected_responses", std::to_string(unexpected_responses_.load())},
        };
        const std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("event_rtt", "ping", summary, failures_.load(), 0, extra)
            : r2kernel::tutorial::render_text_report("event_rtt", "ping", summary, failures_.load(), 0, extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        if (!register_event_publish({options_.request_topic, "bytes", options_.payload_size, 4, options_.queue_depth})) {
            return false;
        }
        return register_event_subscribe({
            options_.response_topic,
            "bytes",
            options_.payload_size,
            [this](const r2kernel::wrapper::EventPayload& payload, std::int64_t timestamp_ns) {
                on_response(payload, timestamp_ns);
            },
        });
    }

    bool on_start() override {
        start_event_transport();
        worker_running_.store(true);
        worker_ = std::thread([this]() { work_loop(); });
        return true;
    }

    bool on_terminate() override {
        stop_worker();
        stop_event_transport();
        return true;
    }

    bool on_reset() override {
        stop_worker();
        stop_event_transport();
        return true;
    }

private:
    static double elapsed_ms(
        std::chrono::steady_clock::time_point started_at,
        std::chrono::steady_clock::time_point finished_at
    ) {
        if (finished_at <= started_at) {
            return 0.0;
        }
        return static_cast<double>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(finished_at - started_at).count()
        ) / 1'000'000.0;
    }

    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = source.name;
        options.log_level = source.log_level;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions {source.name});
        return options;
    }

    bool wait_for_start_gate() {
        if (options_.startup_delay_ms > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(options_.startup_delay_ms));
        }
        if (options_.start_gate_file.empty()) {
            return true;
        }
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(options_.start_gate_timeout_ms);
        while (worker_running_.load() && g_running.load()) {
            try {
                if (start_gate_.opened()) return true;
            } catch (const std::exception& error) {
                std::cerr << "Ping start gate refused: " << error.what() << "\n";
                return false;
            }
            if (std::chrono::steady_clock::now() >= deadline) {
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

    void work_loop() {
        if (!wait_for_start_gate()) {
            ++failures_;
            done_.store(true);
            g_running.store(false);
            worker_running_.store(false);
            return;
        }

        run_started_at_ = std::chrono::steady_clock::now();
        const auto total_target = options_.count > 0 ? options_.warmup + options_.count : 0U;
        const auto period = std::chrono::microseconds(options_.period_us);
        auto next_tick = std::chrono::steady_clock::now();
        while (worker_running_.load()) {
            if (total_target > 0 && attempts_.load() >= total_target) {
                finish_run();
                return;
            }

            const auto now = std::chrono::steady_clock::now();
            if (period.count() > 0 && now < next_tick) {
                std::this_thread::sleep_until(next_tick);
                continue;
            }

            const auto seq = ++seq_;
            ++attempts_;
            {
                std::lock_guard<std::mutex> lock(response_mutex_);
                waiting_seq_ = seq;
                response_received_ = false;
            }

            const auto started_at = std::chrono::steady_clock::now();
            const auto payload = r2kernel::tutorial::make_payload(seq, options_.payload_size);
            const bool published = publish_event(options_.request_topic, payload, now_ns());
            bool response_ok = false;
            std::chrono::steady_clock::time_point response_at;
            if (published) {
                std::unique_lock<std::mutex> lock(response_mutex_);
                response_ok = response_cv_.wait_for(
                    lock,
                    std::chrono::milliseconds(options_.timeout_ms),
                    [this, seq]() {
                        return !worker_running_.load()
                            || !g_running.load()
                            || (response_received_ && waiting_seq_ == seq);
                    }
                );
                if (response_ok && response_received_ && waiting_seq_ == seq) {
                    response_at = response_at_;
                } else {
                    response_ok = false;
                }
                waiting_seq_ = 0;
                response_received_ = false;
            }

            const bool measured = attempts_.load() > options_.warmup;
            if (published && response_ok) {
                ++success_;
                if (first_success_delay_ms_ < 0.0) {
                    first_success_delay_ms_ = elapsed_ms(run_started_at_, std::chrono::steady_clock::now());
                }
                if (measured) {
                    if (first_measured_success_delay_ms_ < 0.0) {
                        first_measured_success_delay_ms_ = elapsed_ms(run_started_at_, std::chrono::steady_clock::now());
                    }
                    const auto rtt_us = static_cast<double>(
                        std::chrono::duration_cast<std::chrono::nanoseconds>(response_at - started_at).count()
                    ) / 1'000.0;
                    stats_.add_us(rtt_us);
                    if (!options_.quiet) {
                        std::cout << name() << " seq=" << seq << " rtt_us=" << rtt_us << "\n";
                    }
                }
            } else {
                ++failures_;
                if (!options_.quiet) {
                    std::cout << name() << " seq=" << seq << " timeout_or_publish_failure\n";
                }
            }

            if (period.count() > 0) {
                next_tick += period;
                if (std::chrono::steady_clock::now() > next_tick + period) {
                    next_tick = std::chrono::steady_clock::now() + period;
                }
            }
        }
    }

    void finish_run() {
        run_finished_at_ = std::chrono::steady_clock::now();
        done_.store(true);
        g_running.store(false);
        worker_running_.store(false);
    }

    void on_response(const r2kernel::wrapper::EventPayload& payload, std::int64_t /*timestamp_ns*/) {
        const auto seq = r2kernel::tutorial::payload_seq(payload);
        std::lock_guard<std::mutex> lock(response_mutex_);
        if (waiting_seq_ == seq && !response_received_) {
            response_at_ = std::chrono::steady_clock::now();
            response_received_ = true;
            response_cv_.notify_one();
            return;
        }
        ++unexpected_responses_;
    }

    void stop_worker() {
        worker_running_.store(false);
        response_cv_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    std::size_t requested_payload_size_ = 0;
    Options options_;
    StartGate start_gate_;
    mutable r2kernel::tutorial::LatencyStats stats_;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> done_ {false};
    std::atomic<std::uint64_t> attempts_ {0};
    std::atomic<std::uint64_t> success_ {0};
    std::atomic<std::uint64_t> failures_ {0};
    std::atomic<std::uint64_t> unexpected_responses_ {0};
    std::uint64_t seq_ = 0;
    std::chrono::steady_clock::time_point run_started_at_;
    std::chrono::steady_clock::time_point run_finished_at_;
    double first_success_delay_ms_ = -1.0;
    double first_measured_success_delay_ms_ = -1.0;
    std::mutex response_mutex_;
    std::condition_variable response_cv_;
    std::uint64_t waiting_seq_ = 0;
    bool response_received_ = false;
    std::chrono::steady_clock::time_point response_at_;
    std::thread worker_;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    const Options options = parse_args(argc, argv);
    PingSubsystem subsystem(options, acknowledged_start_gate_or_exit(
        options.start_gate_file, options.start_gate_session, options.name, "Ping"));
    subsystem.initialize();
    subsystem.start();
    while (g_running.load() && !subsystem.done()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    subsystem.write_report();
    return 0;
}
