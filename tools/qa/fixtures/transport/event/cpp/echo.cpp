#include "benchmark.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "event_echo";
    std::string log_level = "INFO";
    std::string request_topic = "event_ping_request";
    std::string response_topic = "event_ping_response";
    std::size_t requested_payload_size = 8;
    std::size_t payload_size = 8;
    std::uint64_t expected_count = 0;
    std::uint64_t warmup = 0;
    std::size_t queue_depth = 0;
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
                << "Usage: echo [--name NAME] [--log-level LEVEL]\n"
                << "            [--request-topic TOPIC] [--response-topic TOPIC]\n"
                << "            [--payload-size BYTES] [--expected-count N] [--warmup N]\n"
                << "            [--queue-depth N] [--quiet]\n"
                << "            [--report-format text|json] [--report-path PATH]\n";
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
    if (const auto value = int_arg(argc, argv, "expected-count")) {
        options.expected_count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "warmup")) {
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "queue-depth")) {
        options.queue_depth = static_cast<std::size_t>(std::max<std::int64_t>(*value, 0));
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

class EchoSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit EchoSubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)),
          requested_payload_size_(options.requested_payload_size),
          options_(std::move(options)) {}

    bool done() const noexcept {
        return done_.load();
    }

    void write_report() const {
        const auto summary = stats_.summary();
        const std::vector<std::pair<std::string, std::string>> extra {
            {"request_topic", options_.request_topic},
            {"response_topic", options_.response_topic},
            {"payload_size_requested", std::to_string(requested_payload_size_)},
            {"payload_size_effective", std::to_string(options_.payload_size)},
            {"received_total", std::to_string(received_total_.load())},
            {"echoed", std::to_string(echoed_.load())},
            {"warmup", std::to_string(options_.warmup)},
        };
        const std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("event_rtt", "echo", summary, publish_failures_.load(), 0, extra)
            : r2kernel::tutorial::render_text_report("event_rtt", "echo", summary, publish_failures_.load(), 0, extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        if (!register_event_publish({options_.response_topic, "bytes", options_.payload_size, 4, options_.queue_depth})) {
            return false;
        }
        return register_event_subscribe({
            options_.request_topic,
            "bytes",
            options_.payload_size,
            [this](const r2kernel::wrapper::EventPayload& payload, std::int64_t timestamp_ns) {
                on_request(payload, timestamp_ns);
            },
        });
    }

    bool on_start() override {
        start_event_transport();
        return true;
    }

    bool on_terminate() override {
        stop_event_transport();
        return true;
    }

    bool on_reset() override {
        stop_event_transport();
        return true;
    }

private:
    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = source.name;
        options.log_level = source.log_level;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions {source.name});
        return options;
    }

    void on_request(const r2kernel::wrapper::EventPayload& payload, std::int64_t /*timestamp_ns*/) {
        const auto seq = r2kernel::tutorial::payload_seq(payload);
        const auto handled = ++received_total_;
        const auto started_at = std::chrono::steady_clock::now();
        const bool ok = publish_event(options_.response_topic, payload, now_ns());
        const auto publish_us = static_cast<double>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - started_at
            ).count()
        ) / 1'000.0;

        if (ok) {
            ++echoed_;
            if (handled > options_.warmup) {
                stats_.add_us(publish_us);
            }
        } else {
            ++publish_failures_;
        }
        if (!options_.quiet) {
            std::cout << name() << " seq=" << seq << " publish_us=" << publish_us << " ok=" << ok << "\n";
        }
        const auto total_target = options_.expected_count > 0 ? options_.warmup + options_.expected_count : 0U;
        if (total_target > 0 && handled >= total_target) {
            done_.store(true);
            g_running.store(false);
        }
    }

    std::size_t requested_payload_size_ = 0;
    Options options_;
    mutable r2kernel::tutorial::LatencyStats stats_;
    std::atomic<bool> done_ {false};
    std::atomic<std::uint64_t> received_total_ {0};
    std::atomic<std::uint64_t> echoed_ {0};
    std::atomic<std::uint64_t> publish_failures_ {0};
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    const Options options = parse_args(argc, argv);
    EchoSubsystem subsystem(options);
    subsystem.initialize();
    subsystem.start();
    while (g_running.load() && !subsystem.done()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    subsystem.write_report();
    return 0;
}
