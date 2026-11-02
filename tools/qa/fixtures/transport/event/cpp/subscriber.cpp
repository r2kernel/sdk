#include "benchmark.hpp"
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
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "subscriber";
    std::string log_level = "INFO";
    std::string topic_prefix = "x";
    int topic_count = 1;
    std::size_t requested_payload_size = 8;
    std::size_t payload_size = 8;
    std::uint64_t expected_count = 0;
    std::uint64_t warmup = 0;
    std::int64_t idle_timeout_ms = 0;
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

std::int64_t steady_now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()
    ).count();
}

Options parse_args(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: subscriber [--name NAME] [--log-level LEVEL] [--topic-count N] [--topic-prefix PREFIX]\n"
                << "                  [--payload-size BYTES] [--expected-count N] [--warmup N]\n"
                << "                  [--idle-timeout-ms N]\n"
                << "                  [--quiet] [--report-format text|json] [--report-path PATH]\n";
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
    if (const auto value = int_arg(argc, argv, "expected-count")) {
        options.expected_count = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "warmup")) {
        options.warmup = static_cast<std::uint64_t>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = int_arg(argc, argv, "idle-timeout-ms")) {
        options.idle_timeout_ms = std::max<std::int64_t>(*value, 0);
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

class SubscriberSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit SubscriberSubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)),
          requested_payload_size_(options.requested_payload_size),
          options_(std::move(options)) {
        for (int i = 0; i < options_.topic_count; ++i) {
            const auto topic = r2kernel::tutorial::topic_name(options_.topic_prefix, options_.topic_count, i);
            topics_.push_back(topic);
            per_topic_[topic] = 0;
        }
    }

    bool done() const noexcept {
        return completion_reason_.load() != static_cast<int>(CompletionReason::running);
    }

    bool idle_timeout_expired() const noexcept {
        if (options_.idle_timeout_ms <= 0 || received_total_.load() == 0 || done()) {
            return false;
        }
        const auto elapsed_ns = steady_now_ns() - last_event_steady_ns_.load();
        return elapsed_ns >= options_.idle_timeout_ms * 1'000'000LL;
    }

    void complete_idle_timeout() noexcept {
        complete(CompletionReason::idle_timeout);
    }

    void complete_signal() noexcept {
        complete(CompletionReason::signal);
    }

    void write_report() const {
        const auto summary = stats_.summary();
        const auto per_topic = per_topic_string();
        const auto per_topic_first_seq = per_topic_seq_string(first_seq_);
        const auto per_topic_last_seq = per_topic_seq_string(last_seq_);
        const auto expected_total = options_.expected_count > 0 ? options_.warmup + options_.expected_count : 0;
        const auto observed_total = received_total_.load() + dropped_.load();
        const auto tail_missing = expected_total > observed_total ? expected_total - observed_total : 0;
        const std::vector<std::pair<std::string, std::string>> extra {
            {"topic_count", std::to_string(options_.topic_count)},
            {"payload_size_requested", std::to_string(requested_payload_size_)},
            {"payload_size_effective", std::to_string(options_.payload_size)},
            {"expected_count", std::to_string(options_.expected_count)},
            {"expected_total", std::to_string(expected_total)},
            {"received_total", std::to_string(received_total_.load())},
            {"observed_total", std::to_string(observed_total)},
            {"measured", std::to_string(measured_.load())},
            {"warmup", std::to_string(options_.warmup)},
            {"prefix_missing", std::to_string(prefix_missing_.load())},
            {"gap_missing", std::to_string(gap_missing_.load())},
            {"tail_missing", std::to_string(tail_missing)},
            {"completion_reason", completion_reason_string()},
            {"idle_timeout_ms", std::to_string(options_.idle_timeout_ms)},
            {"per_topic", per_topic},
            {"per_topic_first_seq", per_topic_first_seq},
            {"per_topic_last_seq", per_topic_last_seq},
        };
        const std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("event", "subscriber", summary, std::nullopt, dropped_.load(), extra)
            : r2kernel::tutorial::render_text_report("event", "subscriber", summary, std::nullopt, dropped_.load(), extra);
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        for (const auto& topic : topics_) {
            if (!register_event_subscribe({
                topic,
                "bytes",
                options_.payload_size,
                [this, topic](const r2kernel::wrapper::EventPayload& payload, std::int64_t timestamp_ns) {
                    on_event(topic, payload, timestamp_ns);
                },
            })) {
                return false;
            }
        }
        return true;
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
    enum class CompletionReason : int {
        running = 0,
        expected_count = 1,
        idle_timeout = 2,
        signal = 3,
    };

    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = source.name;
        options.log_level = source.log_level;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions {source.name});
        return options;
    }

    void on_event(const std::string& topic,
                  const r2kernel::wrapper::EventPayload& payload,
                  std::int64_t timestamp_ns) {
        if (done()) {
            return;
        }
        last_event_steady_ns_.store(steady_now_ns());
        ++received_total_;
        ++per_topic_[topic];
        const auto seq = r2kernel::tutorial::payload_seq(payload);
        const auto last_it = last_seq_.find(topic);
        if (last_it == last_seq_.end() && seq > 1) {
            const auto missing = seq - 1;
            prefix_missing_ += missing;
            dropped_ += missing;
        } else if (last_it != last_seq_.end() && seq > last_it->second + 1) {
            const auto missing = seq - last_it->second - 1;
            gap_missing_ += missing;
            dropped_ += missing;
        }
        if (last_it == last_seq_.end()) {
            first_seq_[topic] = seq;
        }
        last_seq_[topic] = seq;
        const auto observed_total = received_total_.load() + dropped_.load();

        if (received_total_.load() <= options_.warmup) {
            if (options_.expected_count > 0 && observed_total >= options_.warmup + options_.expected_count) {
                complete(CompletionReason::expected_count);
            }
            return;
        }

        const double latency_us = static_cast<double>(now_ns() - timestamp_ns) / 1'000.0;
        stats_.add_us(latency_us);
        ++measured_;
        if (!options_.quiet) {
            std::cout << name() << " topic=" << topic << " seq=" << seq << " latency_us=" << latency_us << "\n";
        }
        if (options_.expected_count > 0 && observed_total >= options_.warmup + options_.expected_count) {
            complete(CompletionReason::expected_count);
        }
    }

    void complete(CompletionReason reason) noexcept {
        int expected = static_cast<int>(CompletionReason::running);
        if (completion_reason_.compare_exchange_strong(expected, static_cast<int>(reason))) {
            g_running.store(false);
        }
    }

    std::string completion_reason_string() const {
        switch (static_cast<CompletionReason>(completion_reason_.load())) {
        case CompletionReason::running:
            return "running";
        case CompletionReason::expected_count:
            return "expected_count";
        case CompletionReason::idle_timeout:
            return "idle_timeout";
        case CompletionReason::signal:
            return "signal";
        }
        return "unknown";
    }

    std::string per_topic_string() const {
        std::ostringstream out;
        bool first = true;
        for (const auto& [topic, count] : per_topic_) {
            if (!first) {
                out << ',';
            }
            first = false;
            out << topic << ':' << count;
        }
        return out.str();
    }

    std::string per_topic_seq_string(const std::unordered_map<std::string, std::uint64_t>& values) const {
        std::ostringstream out;
        bool first = true;
        for (const auto& [topic, seq] : values) {
            if (!first) {
                out << ',';
            }
            first = false;
            out << topic << ':' << seq;
        }
        return out.str();
    }

    std::size_t requested_payload_size_ = 0;
    Options options_;
    std::vector<std::string> topics_;
    mutable r2kernel::tutorial::LatencyStats stats_;
    mutable std::unordered_map<std::string, std::uint64_t> first_seq_;
    mutable std::unordered_map<std::string, std::uint64_t> last_seq_;
    mutable std::unordered_map<std::string, std::uint64_t> per_topic_;
    std::atomic<int> completion_reason_ {static_cast<int>(CompletionReason::running)};
    std::atomic<std::uint64_t> received_total_ {0};
    std::atomic<std::uint64_t> measured_ {0};
    std::atomic<std::uint64_t> dropped_ {0};
    std::atomic<std::uint64_t> prefix_missing_ {0};
    std::atomic<std::uint64_t> gap_missing_ {0};
    std::atomic<std::int64_t> last_event_steady_ns_ {0};
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    const Options options = parse_args(argc, argv);
    SubscriberSubsystem subsystem(options);
    subsystem.initialize();
    subsystem.start();

    while (g_running.load() && !subsystem.done()) {
        if (subsystem.idle_timeout_expired()) {
            subsystem.complete_idle_timeout();
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (!subsystem.done()) {
        subsystem.complete_signal();
    }

    subsystem.terminate();
    subsystem.write_report();
    return 0;
}
