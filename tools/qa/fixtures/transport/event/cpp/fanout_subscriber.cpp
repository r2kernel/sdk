#include "benchmark.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <fstream>
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
    std::string name_prefix = "subscriber_";
    std::string log_level = "INFO";
    std::string topic_prefix = "x";
    int subscriber_count = 1;
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

std::string json_escape(const std::string& value) {
    std::string out;
    out.reserve(value.size());
    for (const char c : value) {
        if (c == '\\' || c == '"') {
            out.push_back('\\');
        }
        out.push_back(c);
    }
    return out;
}

Options parse_args(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: fanout_subscriber [--name-prefix PREFIX] [--log-level LEVEL]\n"
                << "                         [--subscriber-count N] [--topic-prefix TOPIC]\n"
                << "                         [--payload-size BYTES] [--expected-count N] [--warmup N]\n"
                << "                         [--idle-timeout-ms N] [--quiet]\n"
                << "                         [--report-format text|json] [--report-path PATH]\n";
            std::exit(0);
        }
    }

    Options options;
    if (const auto value = string_arg(argc, argv, "name-prefix")) {
        options.name_prefix = *value;
    }
    if (const auto value = string_arg(argc, argv, "log-level")) {
        options.log_level = *value;
    }
    if (const auto value = string_arg(argc, argv, "topic-prefix")) {
        options.topic_prefix = *value;
    }
    if (const auto value = int_arg(argc, argv, "subscriber-count")) {
        options.subscriber_count = static_cast<int>(std::max<std::int64_t>(*value, 1));
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

class SubscriberEndpoint final : public r2kernel::wrapper::Subsystem {
public:
    SubscriberEndpoint(const Options& options, int index)
        : r2kernel::wrapper::Subsystem(make_options(options, index)),
          options_(options),
          index_(index) {}

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

    std::string report_json() const {
        const auto expected_total = options_.expected_count > 0 ? options_.warmup + options_.expected_count : 0;
        const auto observed_total = received_total_.load() + dropped_.load();
        const auto tail_missing = expected_total > observed_total ? expected_total - observed_total : 0;
        const std::vector<std::pair<std::string, std::string>> extra {
            {"endpoint_index", std::to_string(index_)},
            {"topic_count", "1"},
            {"payload_size_requested", std::to_string(options_.requested_payload_size)},
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
            {"per_topic", options_.topic_prefix + ":" + std::to_string(received_total_.load())},
            {"per_topic_first_seq", first_seq_ > 0 ? options_.topic_prefix + ":" + std::to_string(first_seq_) : ""},
            {"per_topic_last_seq", last_seq_ > 0 ? options_.topic_prefix + ":" + std::to_string(last_seq_) : ""},
        };
        return r2kernel::tutorial::render_json_report("event", "subscriber", stats_.summary(), std::nullopt, dropped_.load(), extra);
    }

    bool start() {
        initialize();
        r2kernel::wrapper::Subsystem::start();
        return true;
    }

    bool refresh_subscription() {
        return register_event_subscribe(make_subscribe_spec());
    }

    void stop() {
        terminate();
    }

protected:
    bool on_initialize() override {
        return register_event_subscribe(make_subscribe_spec());
    }

    bool on_start() override {
        return true;
    }

    bool on_terminate() override {
        return true;
    }

    bool on_reset() override {
        return true;
    }

private:
    enum class CompletionReason : int {
        running = 0,
        expected_count = 1,
        idle_timeout = 2,
        signal = 3,
    };

    static r2kernel::wrapper::SubsystemOptions make_options(const Options& source, int index) {
        const std::string name = source.name_prefix + std::to_string(index);
        r2kernel::wrapper::SubsystemOptions options;
        options.name = name;
        options.log_level = source.log_level;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions {name});
        return options;
    }

    r2kernel::wrapper::EventSubscribeSpec make_subscribe_spec() {
        return {
            options_.topic_prefix,
            "bytes",
            options_.payload_size,
            [this](const r2kernel::wrapper::EventPayload& payload, std::int64_t timestamp_ns) {
                on_event(payload, timestamp_ns);
            },
        };
    }

    void on_event(const r2kernel::wrapper::EventPayload& payload, std::int64_t timestamp_ns) {
        if (done()) {
            return;
        }
        last_event_steady_ns_.store(steady_now_ns());
        ++received_total_;
        const auto seq = r2kernel::tutorial::payload_seq(payload);
        if (last_seq_ == 0 && seq > 1) {
            const auto missing = seq - 1;
            prefix_missing_ += missing;
            dropped_ += missing;
        } else if (last_seq_ > 0 && seq > last_seq_ + 1) {
            const auto missing = seq - last_seq_ - 1;
            gap_missing_ += missing;
            dropped_ += missing;
        }
        if (first_seq_ == 0) {
            first_seq_ = seq;
        }
        last_seq_ = seq;
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
            std::cout << name() << " topic=" << options_.topic_prefix << " seq=" << seq
                      << " latency_us=" << latency_us << "\n";
        }
        if (options_.expected_count > 0 && observed_total >= options_.warmup + options_.expected_count) {
            complete(CompletionReason::expected_count);
        }
    }

    void complete(CompletionReason reason) noexcept {
        int expected = static_cast<int>(CompletionReason::running);
        completion_reason_.compare_exchange_strong(expected, static_cast<int>(reason));
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

    Options options_;
    int index_ = 0;
    mutable r2kernel::tutorial::LatencyStats stats_;
    std::atomic<int> completion_reason_ {static_cast<int>(CompletionReason::running)};
    std::atomic<std::uint64_t> received_total_ {0};
    std::atomic<std::uint64_t> measured_ {0};
    std::atomic<std::uint64_t> dropped_ {0};
    std::atomic<std::uint64_t> prefix_missing_ {0};
    std::atomic<std::uint64_t> gap_missing_ {0};
    std::atomic<std::int64_t> last_event_steady_ns_ {0};
    std::uint64_t first_seq_ = 0;
    std::uint64_t last_seq_ = 0;
};

std::string render_group_json(const Options& options, const std::vector<std::unique_ptr<SubscriberEndpoint>>& endpoints) {
    std::ostringstream out;
    out << "{\n";
    out << "  \"kind\": \"event_fanout\",\n";
    out << "  \"role\": \"single_process_subscriber_group\",\n";
    out << "  \"process_model\": \"single_process\",\n";
    out << "  \"subscriber_count\": " << endpoints.size() << ",\n";
    out << "  \"topic\": \"" << json_escape(options.topic_prefix) << "\",\n";
    out << "  \"subscribers\": [\n";
    for (std::size_t i = 0; i < endpoints.size(); ++i) {
        if (i > 0) {
            out << ",\n";
        }
        out << endpoints[i]->report_json();
    }
    out << "\n  ]\n";
    out << "}";
    return out.str();
}

void maybe_write_report_file(const std::string& path, const std::string& content) {
    if (path.empty()) {
        return;
    }
    std::ofstream out(path);
    out << content << '\n';
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    const Options options = parse_args(argc, argv);
    std::vector<std::unique_ptr<SubscriberEndpoint>> endpoints;
    endpoints.reserve(static_cast<std::size_t>(options.subscriber_count));
    for (int i = 0; i < options.subscriber_count; ++i) {
        endpoints.push_back(std::make_unique<SubscriberEndpoint>(options, i));
    }

    for (auto& endpoint : endpoints) {
        if (!endpoint->start()) {
            std::cerr << "failed to start subscriber endpoint\n";
            g_running.store(false);
            break;
        }
    }
    for (auto& endpoint : endpoints) {
        if (!endpoint->refresh_subscription()) {
            std::cerr << "failed to refresh subscriber endpoint\n";
            g_running.store(false);
            break;
        }
    }

    while (g_running.load()) {
        bool all_done = true;
        for (auto& endpoint : endpoints) {
            if (endpoint->idle_timeout_expired()) {
                endpoint->complete_idle_timeout();
            }
            all_done = all_done && endpoint->done();
        }
        if (all_done) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    if (!g_running.load()) {
        for (auto& endpoint : endpoints) {
            if (!endpoint->done()) {
                endpoint->complete_signal();
            }
        }
    }

    for (auto& endpoint : endpoints) {
        endpoint->stop();
    }

    const std::string content = render_group_json(options, endpoints);
    std::cout << content << "\n";
    maybe_write_report_file(options.report_path, content);
    return 0;
}
