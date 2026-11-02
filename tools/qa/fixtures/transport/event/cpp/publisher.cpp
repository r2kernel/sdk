#include "benchmark.hpp"
#include "start_gate.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <atomic>
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "publisher";
    std::string log_level = "INFO";
    std::string topic_prefix = "x";
    int topic_count = 1;
    std::size_t requested_payload_size = 8;
    std::size_t payload_size = 8;
    std::int64_t period_us = 1'000'000;
    std::uint64_t count = 0;
    std::uint64_t warmup = 0;
    std::size_t queue_depth = 0;
    std::int64_t startup_delay_ms = 0;
    std::string start_gate_file;
    std::string start_gate_session;
    int start_gate_timeout_ms = 30000;
    std::string publish_mode = "round-robin";
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
                << "Usage: publisher [--name NAME] [--log-level LEVEL] [--topic-count N] [--topic-prefix PREFIX]\n"
                << "                 [--payload-size BYTES] [--period-ms N | --period-us N]\n"
                << "                 [--count N] [--warmup N] [--queue-depth N] [--startup-delay-ms N]\n"
                << "                 [--start-gate-file PATH --start-gate-session ID] [--start-gate-timeout-ms N]\n"
                << "                 [--publish-mode round-robin|fanout] [--quiet]\n"
                << "                 [--report-format text|json] [--report-path PATH]\n";
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
    if (const auto value = string_arg(argc, argv, "topic-prefix")) {
        options.topic_prefix = *value;
    }
    if (const auto value = int_arg(argc, argv, "topic-count")) {
        options.topic_count = static_cast<int>(std::max<std::int64_t>(*value, 1));
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
    if (const auto value = string_arg(argc, argv, "start-gate-file")) {
        options.start_gate_file = *value;
    }
    if (const auto value = string_arg(argc, argv, "start-gate-session")) {
        options.start_gate_session = *value;
    }
    if (const auto value = int_arg(argc, argv, "start-gate-timeout-ms")) {
        options.start_gate_timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 1));
    }
    if (const auto value = string_arg(argc, argv, "publish-mode")) {
        options.publish_mode = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-format")) {
        options.report_format = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-path")) {
        options.report_path = *value;
    }
    options.quiet = flag_arg(argc, argv, "quiet");
    if (options.publish_mode != "round-robin" && options.publish_mode != "fanout") {
        throw std::runtime_error("publish-mode must be round-robin or fanout");
    }
    return options;
}

class PublishSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit PublishSubsystem(Options options, StartGate start_gate)
        : r2kernel::wrapper::Subsystem(make_options(options)),
          requested_payload_size_(options.requested_payload_size),
          options_(std::move(options)),
          start_gate_(std::move(start_gate)) {
        for (int i = 0; i < options_.topic_count; ++i) {
            topics_.push_back(r2kernel::tutorial::topic_name(options_.topic_prefix, options_.topic_count, i));
        }
        topic_seq_.assign(topics_.size(), 0);
    }

    ~PublishSubsystem() override {
        stop_worker();
    }

    bool done() const noexcept {
        return done_.load();
    }

    void print_summary() const {
        const double elapsed_s = elapsed_seconds();
        const double throughput = elapsed_s > 0.0 ? static_cast<double>(sent_.load()) / elapsed_s : 0.0;
        const auto publish_summary = publish_stats_.summary();
        std::cout << "publisher.summary messages_sent=" << sent_.load()
                  << " publish_failures=" << publish_failures_.load()
                  << " publish_avg_us=" << publish_summary.avg_us
                  << " publish_p99_us=" << publish_summary.p99_us
                  << " elapsed_s=" << elapsed_s
                  << " throughput_mps=" << throughput
                  << " topic_count=" << options_.topic_count
                  << " payload_size_requested=" << requested_payload_size_
                  << " payload_size_effective=" << options_.payload_size
                  << " period_us=" << options_.period_us
                  << " publish_mode=" << options_.publish_mode
                  << "\n";
    }

    void write_report() const {
        const double elapsed_s = elapsed_seconds();
        const double throughput = elapsed_s > 0.0 ? static_cast<double>(sent_.load()) / elapsed_s : 0.0;
        const auto summary = publish_stats_.summary();
        const std::vector<std::pair<std::string, std::string>> extra {
            {"messages_sent", std::to_string(sent_.load())},
            {"payload_size_requested", std::to_string(requested_payload_size_)},
            {"payload_size_effective", std::to_string(options_.payload_size)},
            {"period_us", std::to_string(options_.period_us)},
            {"warmup", std::to_string(options_.warmup)},
            {"startup_delay_ms", std::to_string(options_.startup_delay_ms)},
            {"start_gate_file", options_.start_gate_file},
            {"topic_count", std::to_string(options_.topic_count)},
            {"publish_mode", options_.publish_mode},
            {"elapsed_s", std::to_string(elapsed_s)},
            {"throughput_mps", std::to_string(throughput)},
        };
        const std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("event", "publisher", summary, publish_failures_.load(), std::nullopt, extra)
            : r2kernel::tutorial::render_text_report("event", "publisher", summary, publish_failures_.load(), std::nullopt, extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        for (const auto& topic : topics_) {
            if (!register_event_publish({topic, "bytes", options_.payload_size, 4, options_.queue_depth})) {
                return false;
            }
        }
        return true;
    }

    bool on_start() override {
        start_event_transport();
        worker_running_.store(true);
        worker_ = std::thread([this]() { publish_loop(); });
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
    double elapsed_seconds() const {
        const auto started_ns = started_ns_.load();
        const auto finished_ns = finished_ns_.load() > 0 ? finished_ns_.load() : now_ns();
        if (started_ns <= 0 || finished_ns <= started_ns) {
            return 0.0;
        }
        return static_cast<double>(finished_ns - started_ns) / 1'000'000'000.0;
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
                std::cerr << "Publisher start gate refused: " << error.what() << "\n";
                return false;
            }
            if (std::chrono::steady_clock::now() >= deadline) {
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

    void publish_loop() {
        started_ns_.store(now_ns());
        if (!wait_for_start_gate()) {
            ++publish_failures_;
            done_.store(true);
            finished_ns_.store(now_ns());
            g_running.store(false);
            worker_running_.store(false);
            return;
        }
        started_ns_.store(now_ns());
        const auto total_target = options_.count > 0 ? options_.warmup + options_.count : 0U;
        const auto period = std::chrono::microseconds(options_.period_us);
        auto next_tick = std::chrono::steady_clock::now();
        while (worker_running_.load()) {
            if (total_target > 0 && sent_.load() >= total_target) {
                done_.store(true);
                finished_ns_.store(now_ns());
                g_running.store(false);
                worker_running_.store(false);
                return;
            }

            const auto now = std::chrono::steady_clock::now();
            if (period.count() > 0 && now < next_tick) {
                std::this_thread::sleep_until(next_tick);
                continue;
            }

            const auto send_one = [this, total_target](std::size_t topic_index) {
                if (total_target > 0 && sent_.load() >= total_target) {
                    return;
                }
                const auto& topic = topics_[topic_index];
                const auto seq = ++topic_seq_[topic_index];
                const auto payload = r2kernel::tutorial::make_payload(seq, options_.payload_size);
                const auto timestamp = now_ns();
                const auto publish_started = std::chrono::steady_clock::now();
                if (publish_event(topic, payload, timestamp)) {
                    const auto publish_us = static_cast<double>(
                        std::chrono::duration_cast<std::chrono::nanoseconds>(
                            std::chrono::steady_clock::now() - publish_started
                        ).count()
                    ) / 1'000.0;
                    const auto sent_after = sent_.fetch_add(1) + 1;
                    if (sent_after > options_.warmup) {
                        publish_stats_.add_us(publish_us);
                    }
                    if (!options_.quiet) {
                        std::cout << name() << " topic=" << topic << " seq=" << seq
                                  << " ts=" << timestamp << " publish_us=" << publish_us << "\n";
                    }
                } else {
                    ++publish_failures_;
                }
            };

            if (options_.publish_mode == "fanout") {
                for (std::size_t i = 0; i < topics_.size(); ++i) {
                    send_one(i);
                }
            } else {
                const auto index = static_cast<std::size_t>(sent_.load() % static_cast<std::uint64_t>(topics_.size()));
                send_one(index);
            }

            if (period.count() > 0) {
                next_tick += period;
                if (std::chrono::steady_clock::now() > next_tick + period) {
                    next_tick = std::chrono::steady_clock::now() + period;
                }
            }
        }
    }

    void stop_worker() {
        worker_running_.store(false);
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    std::size_t requested_payload_size_ = 0;
    Options options_;
    StartGate start_gate_;
    std::vector<std::string> topics_;
    mutable r2kernel::tutorial::LatencyStats publish_stats_;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> done_ {false};
    std::atomic<std::uint64_t> sent_ {0};
    std::atomic<std::uint64_t> publish_failures_ {0};
    std::vector<std::uint64_t> topic_seq_;
    std::atomic<std::int64_t> started_ns_ {0};
    std::atomic<std::int64_t> finished_ns_ {0};
    std::thread worker_;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    const Options options = parse_args(argc, argv);
    PublishSubsystem subsystem(options, acknowledged_start_gate_or_exit(
        options.start_gate_file, options.start_gate_session, options.name, "Publisher"));
    subsystem.initialize();
    subsystem.start();

    while (g_running.load() && !subsystem.done()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    subsystem.terminate();
    subsystem.write_report();
    subsystem.print_summary();
    return 0;
}
