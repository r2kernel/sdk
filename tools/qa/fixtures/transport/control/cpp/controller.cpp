#include "benchmark.hpp"
#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"
#include "rt_profile.hpp"

#include <atomic>
#include <array>
#include <algorithm>
#include <chrono>
#include <ctime>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <limits>
#include <tuple>
#include <utility>
#include <vector>
#if defined(__unix__) || defined(__APPLE__)
#include <sys/resource.h>
#endif

namespace {

std::atomic<bool> g_running {true};

struct Options {
    std::string name = "control_controller_cpp";
    std::string log_level = "INFO";
    std::string binding = "gateway";
    std::filesystem::path manifest;
    std::filesystem::path deployment_manifest = r2kernel::tutorial::default_deployment_manifest_path(__FILE__);
    std::string target;
    std::string topic_prefix = "control_cmd";
    int topic_count = 1;
    std::size_t payload_size = 8;
    std::vector<std::size_t> request_sizes;
    std::size_t request_size_block_length = 1;
    std::size_t response_size = 8;
    std::int64_t period_us = 1'000'000;
    std::uint64_t count = 0;
    std::uint64_t warmup = 0;
    int timeout_ms = 1000;
    int route_ready_timeout_ms = 30'000;
    float deadline_ms = 1000.0F;
    int priority = -1;
    std::string request_mode = "round-robin";
    std::filesystem::path start_gate_path;
    std::filesystem::path ready_path;
    std::int64_t gate_timeout_ms = 60'000;
    std::filesystem::path size_block_progress_path;
    std::int64_t size_block_ack_timeout_ms = 30'000;
    bool quiet = false;
    std::string report_format = "text";
    std::string report_path;
    fixture_rt::Options rt;
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

std::vector<std::size_t> parse_size_list(const std::string& value) {
    std::vector<std::size_t> sizes;
    std::istringstream input(value);
    std::string item;
    while (std::getline(input, item, ',')) {
        if (item.empty()) throw std::runtime_error("request-sizes contains an empty value");
        const auto parsed = std::stoll(item);
        if (parsed < 8) throw std::runtime_error("request-sizes entries must be at least 8 bytes");
        sizes.push_back(static_cast<std::size_t>(parsed));
    }
    if (sizes.empty()) throw std::runtime_error("request-sizes must contain one or more sizes");
    return sizes;
}

std::string render_size_list(const std::vector<std::size_t>& sizes) {
    std::ostringstream output;
    for (std::size_t index = 0; index < sizes.size(); ++index) {
        if (index > 0) output << ',';
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
                << "Usage: controller [--name NAME] [--log-level LEVEL] [--binding NAME]\n"
                << "                  [--target SUBSYSTEM] [--manifest PATH] [--deployment-manifest PATH]\n"
                << "                  [--topic-count N] [--topic-prefix PREFIX] [--request-size BYTES | --request-sizes CSV]\n"
                << "                  [--request-size-block-length N] [--response-size BYTES]\n"
                << "                  [--period-ms N | --period-us N] [--count N] [--warmup N]\n"
                << "                  [--timeout-ms N] [--deadline-ms N] [--request-mode round-robin|fanout]\n"
                << "                  [--route-ready-timeout-ms N]\n"
                << "                  [--priority N]\n"
                << "                  [--start-gate-path PATH --ready-path PATH] [--size-block-progress-path PATH]\n"
                << "                  [--size-block-ack-timeout-ms N]\n"
                << "                  [--rt-policy other|fifo|rr --rt-priority N --rt-cpus CSV] [--rt-require-isolated]\n"
                << "                  [--prepare-local --preparation-timeout-ms N] [--measurement-seconds 1..30]\n"
                << "                  [--samples-path PATH]\n"
                << "                  [--quiet] [--report-format text|json] [--report-path PATH]\n";
            std::exit(0);
        }
    }

    Options options;
    options.rt = fixture_rt::parse(argc, argv);
    if (const auto value = string_arg(argc, argv, "name")) {
        options.name = *value;
    }
    if (const auto value = string_arg(argc, argv, "log-level")) {
        options.log_level = *value;
    }
    if (const auto value = string_arg(argc, argv, "binding")) {
        options.binding = *value;
    }
    if (const auto value = string_arg(argc, argv, "target")) {
        options.target = *value;
    }
    if (const auto value = string_arg(argc, argv, "manifest")) {
        options.manifest = *value;
    }
    if (const auto value = string_arg(argc, argv, "deployment-manifest")) {
        options.deployment_manifest = *value;
    }
    if (const auto value = string_arg(argc, argv, "topic-prefix")) {
        options.topic_prefix = *value;
    }
    if (const auto value = int_arg(argc, argv, "topic-count")) {
        options.topic_count = static_cast<int>(std::max<std::int64_t>(*value, 1));
    }
    if (const auto value = int_arg(argc, argv, "request-size")) {
        options.payload_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
    } else if (const auto value = int_arg(argc, argv, "payload-size")) {
        options.payload_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
    }
    if (const auto value = string_arg(argc, argv, "request-sizes")) {
        options.request_sizes = parse_size_list(*value);
        options.payload_size = options.request_sizes.front();
    }
    if (const auto value = int_arg(argc, argv, "request-size-block-length")) {
        if (*value <= 0) throw std::runtime_error("request-size-block-length must be positive");
        options.request_size_block_length = static_cast<std::size_t>(*value);
    }
    if (const auto value = int_arg(argc, argv, "response-size")) {
        options.response_size = static_cast<std::size_t>(std::max<std::int64_t>(*value, 8));
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
    if (const auto value = int_arg(argc, argv, "timeout-ms")) {
        options.timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 1));
    }
    if (const auto value = int_arg(argc, argv, "route-ready-timeout-ms")) {
        options.route_ready_timeout_ms = static_cast<int>(std::max<std::int64_t>(*value, 0));
    }
    if (const auto value = string_arg(argc, argv, "deadline-ms")) {
        options.deadline_ms = std::stof(*value);
    } else {
        options.deadline_ms = static_cast<float>(std::max<std::int64_t>(options.period_us / 1000 * 2, 1000));
    }
    if (const auto value = int_arg(argc, argv, "priority")) {
        options.priority = static_cast<int>(*value);
    }
    if (const auto value = string_arg(argc, argv, "request-mode")) {
        options.request_mode = *value;
    }
    if (const auto value = string_arg(argc, argv, "start-gate-path")) {
        options.start_gate_path = *value;
    }
    if (const auto value = string_arg(argc, argv, "ready-path")) {
        options.ready_path = *value;
    }
    if (const auto value = int_arg(argc, argv, "gate-timeout-ms")) {
        options.gate_timeout_ms = std::max<std::int64_t>(*value, 1);
    }
    if (const auto value = string_arg(argc, argv, "size-block-progress-path")) {
        options.size_block_progress_path = *value;
    }
    if (const auto value = int_arg(argc, argv, "size-block-ack-timeout-ms")) {
        if (*value <= 0) throw std::runtime_error("size-block-ack-timeout-ms must be positive");
        options.size_block_ack_timeout_ms = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-format")) {
        options.report_format = *value;
    }
    if (const auto value = string_arg(argc, argv, "report-path")) {
        options.report_path = *value;
    }
    options.quiet = flag_arg(argc, argv, "quiet");
    if (options.request_mode != "round-robin" && options.request_mode != "fanout") {
        throw std::runtime_error("request-mode must be round-robin or fanout");
    }
    if (options.start_gate_path.empty() != options.ready_path.empty()) {
        throw std::runtime_error("start-gate-path and ready-path must be configured together");
    }
    if (options.rt.measurement_ns && (options.period_us <= 0 || options.period_us > 30'000'000
        || !std::isfinite(options.deadline_ms) || options.deadline_ms <= 0
        || options.request_mode != "round-robin" || !options.size_block_progress_path.empty())) {
        throw std::runtime_error("periodic measurement requires a positive period/deadline, round-robin, and no size-stage gate");
    }
    if (options.name.empty()) {
        throw std::runtime_error("name must not be empty");
    }
    if (options.binding.empty()) {
        throw std::runtime_error("binding must not be empty");
    }
    if (options.target.empty()) {
        const auto target = r2kernel::tutorial::resolve_binding_target(options.deployment_manifest, options.name, options.binding);
        options.target = target.subsystem;
    }
    return options;
}

class ControllerSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    explicit ControllerSubsystem(Options options)
        : r2kernel::wrapper::Subsystem(make_options(options)), options_(std::move(options)) {
        for (int i = 0; i < options_.topic_count; ++i) {
            topics_.push_back(r2kernel::tutorial::topic_name(options_.topic_prefix, options_.topic_count, i));
        }
    }

    ~ControllerSubsystem() override {
        stop_worker();
    }

    bool done() const noexcept {
        return done_.load();
    }

    int completion_exit_code() const {
        if (!options_.rt.measurement_ns) return setup_error_.empty() ? 0 : 1;
        if (!periodic_functional_passed()) return 1;
        return periodic_performance_passed() ? 0 : 2;
    }

    void write_report() const {
        const auto summary = stats_.summary();
        const auto total_elapsed_ms = elapsed_ms(run_started_at_, run_finished_at_);
        const auto first_success_ms = first_success_delay_ms_;
        const auto first_measured_success_ms = first_measured_success_delay_ms_;
        const double measured_elapsed_s = elapsed_seconds(measured_started_at_, measured_finished_at_);
        const auto measured_attempts = attempts_.load() > options_.warmup ? attempts_.load() - options_.warmup : 0;
        const auto process_usage = process_resource_metrics();
        const std::vector<std::pair<std::string, std::string>> extra {
            {"attempts", std::to_string(attempts_.load())},
            {"success", std::to_string(success_.load())},
            {"topic_count", std::to_string(options_.topic_count)},
            {"payload_size", std::to_string(options_.payload_size)},
            {"request_payload_size", std::to_string(options_.payload_size)},
            {"request_payload_sizes", options_.request_sizes.empty()
                ? std::to_string(options_.payload_size)
                : render_size_list(options_.request_sizes)},
            {"request_size_block_length", std::to_string(options_.request_size_block_length)},
            {"response_payload_size", std::to_string(options_.response_size)},
            {"period_us", std::to_string(options_.period_us)},
            {"timeout_ms", std::to_string(options_.timeout_ms)},
            {"route_ready_timeout_ms", std::to_string(options_.route_ready_timeout_ms)},
            {"deadline_ms", std::to_string(options_.deadline_ms)},
            {"priority", std::to_string(options_.priority)},
            {"request_mode", options_.request_mode},
            {"warmup", std::to_string(options_.warmup)},
            {"scheduled_requests", options_.count == 0 ? "unbounded" : std::to_string(options_.count + options_.warmup)},
            {"integrity_errors", std::to_string(integrity_errors_.load())},
            {"deadline_violations", std::to_string(deadline_violations_.load())},
            {"period_violations", std::to_string(period_violations_.load())},
            {"late_cycles", std::to_string(late_cycles_.load())},
            {"last_error", last_error_},
            {"measured_elapsed_s", std::to_string(measured_elapsed_s)},
            {"achieved_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? static_cast<double>(measured_attempts) / measured_elapsed_s : 0.0)},
            {"successful_rate_hz", std::to_string(measured_elapsed_s > 0.0 ? static_cast<double>(summary.samples) / measured_elapsed_s : 0.0)},
            {"rtt_measurement_scope", "caller_wall_clock_around_transact_control"},
            {"runtime_rtt_scope", "not_exposed_by_native_api"},
            {"process_user_cpu_s", std::get<0>(process_usage)},
            {"process_system_cpu_s", std::get<1>(process_usage)},
            {"measured_process_cpu_s", measured_cpu_start_ >= 0 && measured_cpu_finish_ >= measured_cpu_start_
                ? std::to_string(static_cast<double>(measured_cpu_finish_ - measured_cpu_start_) / CLOCKS_PER_SEC)
                : "unavailable"},
            {"process_peak_rss_bytes", std::get<2>(process_usage)},
            {"total_elapsed_ms", std::to_string(total_elapsed_ms)},
            {"first_success_ms", first_success_ms >= 0.0 ? std::to_string(first_success_ms) : "null"},
            {"first_measured_success_ms", first_measured_success_ms >= 0.0 ? std::to_string(first_measured_success_ms) : "null"},
            {"measured_start_monotonic_ns", monotonic_ns_string(measured_started_at_)},
            {"measured_end_monotonic_ns", monotonic_ns_string(measured_finished_at_)},
        };
        std::string content = options_.report_format == "json"
            ? r2kernel::tutorial::render_json_report("control", "controller", summary, failures_.load(), 0, extra)
            : r2kernel::tutorial::render_text_report("control", "controller", summary, failures_.load(), 0, extra);
        if (options_.rt.measurement_ns) {
            const auto periodic = periodic_json();
            if (options_.report_format == "json") content = fixture_rt::with_periodic(std::move(content), periodic);
            else content += "\nperiodic: " + periodic;
            fixture_rt::write_json(options_.rt.samples_path, periodic);
        }
        std::cout << content << "\n";
        r2kernel::tutorial::maybe_write_report_file(options_.report_path, content);
    }

protected:
    bool on_initialize() override {
        logger().info("Lifecycle", "Control controller initialized");
        return true;
    }

    bool on_start() override {
        std::string host;
        int port = 0;
        const auto route_deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(options_.route_ready_timeout_ms);
        bool route_ready = false;
        do {
            if (const auto route = resolve_control_target(options_.target)) {
                host = route->host;
                port = route->port;
            }
            route_ready = !host.empty() && port >= 0;
            if (route_ready || std::chrono::steady_clock::now() >= route_deadline) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        } while (true);
        if (!route_ready) {
            logger().error(
                "Lifecycle",
                "Control target " + options_.target + " did not publish a route within "
                    + std::to_string(options_.route_ready_timeout_ms) + "ms"
            );
            return false;
        }
        if (!connect_control(options_.target, host, port)) {
            return false;
        }
        if (!options_.rt.prepare_local && !handshake_control(options_.target, options_.timeout_ms)) {
            logger().error(
                "Lifecycle",
                "Control handshake failed target=" + options_.target
                    + " host=" + host
                    + " port=" + std::to_string(port)
            );
            return false;
        }
        worker_running_.store(true);
        worker_ = std::thread([this]() {
            try {
                thread_profile_ = fixture_rt::apply(options_.rt);
                if (options_.rt.prepare_local) prepare_routes();
                if (options_.rt.measurement_ns) periodic_loop();
                else work_loop();
            } catch (const std::exception& error) {
                last_error_ = setup_error_ = error.what();
                ++failures_;
                finish_worker();
            }
        });
        return true;
    }

    bool on_terminate() override {
        stop_worker();
        logger().info("Lifecycle", "Control controller terminated");
        return true;
    }

    bool on_reset() override {
        stop_worker();
        return true;
    }

private:

    void finish_worker() {
        measured_cpu_finish_ = std::clock();
        run_finished_at_ = std::chrono::steady_clock::now();
        done_.store(true);
        g_running.store(false);
        worker_running_.store(false);
    }

    void prepare_routes() {
        preparation_capacity_ = options_.request_sizes.empty() ? options_.payload_size
            : *std::max_element(options_.request_sizes.begin(), options_.request_sizes.end());
        const auto started = fixture_rt::monotonic_ns();
        preparation_ok_ = true;
        for (const auto& topic : topics_) {
            if (!prepare_control(options_.target, topic, preparation_capacity_, options_.response_size,
                                 options_.rt.preparation_timeout_ms, false)) {
                preparation_ok_ = false;
                break;
            }
        }
        preparation_duration_ns_ = fixture_rt::monotonic_ns() - started;
        if (!preparation_ok_) throw std::runtime_error("provider-ready Control preparation failed");
    }

    std::string preparation_json() const {
        return "{\"requested\":" + std::string(options_.rt.prepare_local ? "true" : "false")
            + ",\"ok\":" + (options_.rt.prepare_local ? (preparation_ok_ ? "true" : "false") : "null")
            + ",\"duration_ns\":" + std::to_string(preparation_duration_ns_)
            + ",\"request_capacity\":" + std::to_string(preparation_capacity_)
            + ",\"response_capacity\":" + std::to_string(options_.response_size) + "}";
    }

    static void fill_payload(r2kernel::wrapper::ByteBuffer& payload, std::uint64_t sequence) {
        std::fill(payload.begin(), payload.end(), static_cast<std::uint8_t>(sequence & 0xffU));
        for (int i = 0; i < 8; ++i)
            payload[static_cast<std::size_t>(i)] = static_cast<std::uint8_t>((sequence >> (56 - i * 8)) & 0xffU);
    }

    void periodic_loop() {
        run_started_at_ = std::chrono::steady_clock::now();
        const auto period = options_.period_us * 1000;
        const auto duration = *options_.rt.measurement_ns;
        const auto deadline = static_cast<std::int64_t>(static_cast<double>(options_.deadline_ms) * 1'000'000);
        scheduled_slots_ = static_cast<std::uint64_t>((duration + period - 1) / period);
        // Reserve all storage before warmup/gate, and serialize only after the wall window.
        periodic_samples_.reserve(static_cast<std::size_t>(scheduled_slots_));
        periodic_samples_.resize(static_cast<std::size_t>(scheduled_slots_));
        periodic_samples_.clear();
        std::vector<r2kernel::wrapper::ByteBuffer> payloads;
        if (options_.request_sizes.empty()) payloads.push_back(r2kernel::tutorial::make_payload(0, options_.payload_size));
        else for (const auto size : options_.request_sizes) payloads.push_back(r2kernel::tutorial::make_payload(0, size));
        auto expected = r2kernel::tutorial::make_payload(0, options_.response_size);
        PeriodicSample warmup_sample;
        const auto attempt = [&](std::uint64_t slot, std::int64_t scheduled, bool measured) -> const PeriodicSample& {
            const auto task_started = fixture_rt::monotonic_ns();
            if (measured) periodic_samples_.emplace_back();
            else warmup_sample = PeriodicSample {};
            auto& sample = measured ? periodic_samples_.back() : warmup_sample;
            sample.started_ns = task_started;
            sample.sequence = seq_.fetch_add(1);
            sample.slot = slot;
            sample.scheduled_ns = scheduled;
            const auto attempt_index = attempts_.fetch_add(1);
            const auto measured_index = measured ? attempt_index - options_.warmup : 0;
            auto& payload = payloads[static_cast<std::size_t>(
                (measured_index / options_.request_size_block_length) % payloads.size())];
            fill_payload(payload, sample.sequence);
            fill_payload(expected, sample.sequence);
            std::optional<r2kernel::wrapper::ControlResponse> response;
            sample.sdk_started_ns = fixture_rt::monotonic_ns();
            try {
                response = transact_control(options_.target,
                    topics_[static_cast<std::size_t>(attempt_index % topics_.size())], payload,
                    static_cast<float>(options_.period_us / 1000.0), options_.deadline_ms,
                    options_.timeout_ms, options_.priority);
                sample.sdk_returned_ns = fixture_rt::monotonic_ns();
            } catch (const std::exception& error) {
                sample.sdk_returned_ns = fixture_rt::monotonic_ns();
                std::strncpy(sample.error.data(), error.what(), sample.error.size() - 1);
            }
            sample.ok = response && response->status == 0;
            sample.integrity_ok = response && response->payload == expected;
            sample.returned_ns = fixture_rt::monotonic_ns();
            if (response && !sample.integrity_ok) ++integrity_errors_;
            if (sample.ok && sample.integrity_ok) ++success_;
            else if (sample.error[0] == '\0') {
                if (!response) std::snprintf(sample.error.data(), sample.error.size(), "%s", "transact_control returned no response");
                else if (!sample.integrity_ok) std::snprintf(sample.error.data(), sample.error.size(), "%s", "response payload integrity mismatch");
                else std::snprintf(sample.error.data(), sample.error.size(), "Control response status=%d", response->status);
            }
            return sample;
        };
        for (std::uint64_t index = 0; index < options_.warmup && worker_running_.load(); ++index) {
            const auto& sample = attempt(index, 0, false);
            if (index == 0) first_warmup_ns_ = sample.returned_ns - sample.started_ns;
            if (!sample.ok || !sample.integrity_ok) throw std::runtime_error(
                std::string("warmup failed: ") + sample.error.data());
        }
        if (!mark_ready_and_wait_for_release()) { finish_worker(); return; }
        periodic_start_ns_ = common_start_ns_.value_or(fixture_rt::monotonic_ns());
        if (periodic_start_ns_ > std::numeric_limits<std::int64_t>::max() - duration)
            throw std::runtime_error("common monotonic gate overflows wall window");
        periodic_end_ns_ = periodic_start_ns_ + duration;
        measured_cpu_start_ = std::clock();
        measured_started_at_ = std::chrono::steady_clock::now();
        std::uint64_t slot = 0;
        while (slot < scheduled_slots_ && worker_running_.load()) {
            auto now = fixture_rt::monotonic_ns();
            if (now >= periodic_end_ns_) break;
            if (now > periodic_start_ns_) {
                const auto current = static_cast<std::uint64_t>((now - periodic_start_ns_) / period);
                if (current > slot) { skipped_slots_ += current - slot; slot = current; }
            }
            if (slot >= scheduled_slots_) break;
            const auto scheduled = periodic_start_ns_ + static_cast<std::int64_t>(slot) * period;
            if (now < scheduled) fixture_rt::sleep_until(scheduled);
            now = fixture_rt::monotonic_ns();
            if (now >= periodic_end_ns_) break;
            // A delayed wake dispatches the current slot, never a burst of overdue slots.
            const auto current = static_cast<std::uint64_t>((now - periodic_start_ns_) / period);
            if (current > slot) { skipped_slots_ += current - slot; slot = current; }
            if (slot >= scheduled_slots_) break;
            const auto& sample = attempt(slot, periodic_start_ns_ + static_cast<std::int64_t>(slot) * period, true);
            if (sample.returned_ns - sample.scheduled_ns > deadline) ++completion_violations_;
            if (sample.sdk_returned_ns - sample.sdk_started_ns > deadline) ++caller_violations_;
            if (sample.returned_ns > periodic_end_ns_) ++completed_after_window_;
            const auto next = static_cast<std::uint64_t>(
                (sample.returned_ns - periodic_start_ns_ + period - 1) / period);
            const auto bounded_next = std::min(scheduled_slots_, std::max(slot + 1, next));
            skipped_slots_ += bounded_next - slot - 1;
            slot = bounded_next;
        }
        skipped_slots_ += scheduled_slots_ - slot;
        // Maintain an explicit finite wall window even when the final slot completes early.
        if (worker_running_.load() && fixture_rt::monotonic_ns() < periodic_end_ns_)
            fixture_rt::sleep_until(periodic_end_ns_);
        measured_finished_at_ = std::chrono::steady_clock::now();
        for (const auto& sample : periodic_samples_) {
            if (sample.ok && sample.integrity_ok)
                stats_.add_us(static_cast<double>(sample.sdk_returned_ns - sample.sdk_started_ns) / 1000.0);
            else { ++failures_; last_error_ = sample.error.data(); }
        }
        deadline_violations_.store(caller_violations_);
        late_cycles_.store(skipped_slots_);
        finish_worker();
    }

    bool periodic_functional_passed() const {
        return setup_error_.empty() && (!options_.rt.prepare_local || preparation_ok_)
            && !periodic_samples_.empty()
            && std::all_of(periodic_samples_.begin(), periodic_samples_.end(),
                [](const auto& sample) { return sample.ok && sample.integrity_ok; });
    }

    bool periodic_performance_passed() const {
        return periodic_functional_passed() && periodic_samples_.size() == scheduled_slots_
            && skipped_slots_ == 0 && completion_violations_ == 0 && caller_violations_ == 0
            && completed_after_window_ == 0;
    }

    std::string periodic_json() const {
        std::uint64_t successes = 0;
        std::uint64_t in_window = 0;
        for (const auto& sample : periodic_samples_) {
            if (sample.ok && sample.integrity_ok) {
                ++successes;
                if (sample.returned_ns <= periodic_end_ns_) ++in_window;
            }
        }
        std::ostringstream out;
        out << "{\"scope\":\"scheduled_tick_to_owned_response_and_validated_release\",\"language\":\"cpp\""
            << ",\"application\":\"synthetic_payload_echo\",\"period_ns\":" << options_.period_us * 1000
            << ",\"completion_deadline_ns\":" << static_cast<std::int64_t>(static_cast<double>(options_.deadline_ms) * 1'000'000)
            << ",\"start_monotonic_ns\":" << periodic_start_ns_ << ",\"end_monotonic_ns\":" << periodic_end_ns_
            << ",\"scheduled_slots\":" << scheduled_slots_ << ",\"attempted\":" << periodic_samples_.size()
            << ",\"successes\":" << successes << ",\"failures\":" << periodic_samples_.size() - successes
            << ",\"skipped_slots\":" << skipped_slots_ << ",\"completion_deadline_violations\":" << completion_violations_
            << ",\"caller_deadline_violations\":" << caller_violations_
            << ",\"completed_after_window\":" << completed_after_window_
            << ",\"attempt_rate_hz\":" << (options_.rt.measurement_ns
                ? static_cast<double>(periodic_samples_.size()) * 1'000'000'000 / *options_.rt.measurement_ns : 0.0)
            << ",\"achieved_rate_hz\":" << (options_.rt.measurement_ns
                ? static_cast<double>(in_window) * 1'000'000'000 / *options_.rt.measurement_ns : 0.0)
            << ",\"successful_completions_in_window\":" << in_window
            << ",\"preparation\":" << preparation_json() << ",\"first_warmup_ns\":"
            << (first_warmup_ns_ < 0 ? "null" : std::to_string(first_warmup_ns_))
            << ",\"thread_profile\":" << thread_profile_ << ",\"setup_error\":"
            << (setup_error_.empty() ? "null" : fixture_rt::quote(setup_error_))
            << ",\"functional_passed\":" << (periodic_functional_passed() ? "true" : "false")
            << ",\"performance_passed\":" << (periodic_performance_passed() ? "true" : "false")
            << ",\"samples\":[";
        for (std::size_t index = 0; index < periodic_samples_.size(); ++index) {
            const auto& sample = periodic_samples_[index];
            if (index) out << ',';
            out << "{\"sequence\":" << sample.sequence << ",\"slot\":" << sample.slot
                << ",\"scheduled_ns\":" << sample.scheduled_ns << ",\"started_ns\":" << sample.started_ns
                << ",\"returned_ns\":" << sample.returned_ns << ",\"ok\":" << (sample.ok ? "true" : "false")
                << ",\"integrity_ok\":" << (sample.integrity_ok ? "true" : "false")
                << ",\"sdk_started_ns\":" << sample.sdk_started_ns << ",\"sdk_returned_ns\":" << sample.sdk_returned_ns
                << ",\"caller_wall_ns\":" << sample.sdk_returned_ns - sample.sdk_started_ns
                << ",\"task_wall_ns\":" << sample.returned_ns - sample.started_ns
                << ",\"sdk_rtt_ns\":null,\"error\":" << fixture_rt::quote(sample.error.data()) << '}';
        }
        out << "]}";
        return out.str();
    }
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

    static double elapsed_seconds(
        std::chrono::steady_clock::time_point started_at,
        std::chrono::steady_clock::time_point finished_at
    ) {
        if (started_at == std::chrono::steady_clock::time_point{} || finished_at <= started_at) {
            return 0.0;
        }
        return std::chrono::duration<double>(finished_at - started_at).count();
    }

    static std::string monotonic_ns_string(std::chrono::steady_clock::time_point value) {
        if (value == std::chrono::steady_clock::time_point{}) return "null";
        return std::to_string(std::chrono::duration_cast<std::chrono::nanoseconds>(value.time_since_epoch()).count());
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

    bool mark_ready_and_wait_for_release() {
        if (options_.start_gate_path.empty() && options_.ready_path.empty()) return true;
        if (options_.start_gate_path.empty() || options_.ready_path.empty()) {
            last_error_ = "matrix start gate and ready marker must be configured together";
            ++failures_;
            return false;
        }
        fixture_rt::write_json(options_.ready_path,
            "{\"attempts\":" + std::to_string(attempts_.load())
            + ",\"ready_monotonic_ns\":" + std::to_string(fixture_rt::monotonic_ns())
            + ",\"thread_profile\":" + thread_profile_
            + ",\"preparation\":" + preparation_json() + ",\"first_warmup_ns\":"
            + (first_warmup_ns_ < 0 ? "null" : std::to_string(first_warmup_ns_)) + "}");
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(options_.gate_timeout_ms);
        while (worker_running_.load() && !std::filesystem::exists(options_.start_gate_path)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                last_error_ = "matrix start gate timed out";
                ++failures_;
                run_finished_at_ = std::chrono::steady_clock::now();
                done_.store(true);
                g_running.store(false);
                worker_running_.store(false);
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        if (worker_running_.load()) common_start_ns_ = fixture_rt::gate_start(options_.start_gate_path);
        return worker_running_.load();
    }

    std::size_t request_size_for_attempt(std::uint64_t attempt_index) {
        if (options_.request_sizes.empty()) return options_.payload_size;
        if (attempt_index < options_.warmup) return options_.request_sizes.front();
        const auto measured_attempt_index = attempt_index - options_.warmup;
        const auto block_stage = measured_attempt_index / options_.request_size_block_length;
        const auto block_index = static_cast<std::size_t>(block_stage % options_.request_sizes.size());
        return options_.request_sizes[block_index];
    }

    bool mark_size_stage_complete(std::uint64_t attempt_index, std::size_t request_size) {
        if (options_.size_block_progress_path.empty() || attempt_index < options_.warmup) return true;
        const auto measured_attempt_index = attempt_index - options_.warmup;
        if (measured_attempt_index % options_.request_size_block_length != 0) return true;
        const auto block_stage = measured_attempt_index / options_.request_size_block_length;
        const auto block_index = static_cast<std::size_t>(block_stage % options_.request_sizes.size());
        const auto write_progress = [&](const std::filesystem::path& path) {
            std::filesystem::create_directories(path.parent_path());
            const auto temporary_path = path.string() + ".tmp";
            std::ofstream progress(temporary_path);
            progress << "{\"block_index\":" << block_index
                     << ",\"cycle_index\":" << block_stage / options_.request_sizes.size()
                     << ",\"request_size\":" << request_size
                     << ",\"attempts_completed\":" << attempts_.load()
                     << ",\"monotonic_ns\":" << monotonic_ns_string(std::chrono::steady_clock::now()) << "}\n";
            progress.close();
            std::filesystem::rename(temporary_path, path);
        };
        auto stage_path = options_.size_block_progress_path;
        stage_path += ".stage-" + std::to_string(block_stage) + ".json";
        write_progress(stage_path);
        auto acknowledgement = stage_path;
        acknowledgement += ".ack";
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(options_.size_block_ack_timeout_ms);
        while (worker_running_.load() && !std::filesystem::exists(acknowledgement)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                last_error_ = "request-size stage acknowledgement timed out: " + stage_path.filename().string();
                ++failures_;
                run_finished_at_ = std::chrono::steady_clock::now();
                done_.store(true);
                g_running.store(false);
                worker_running_.store(false);
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        return worker_running_.load();
    }

    void work_loop() {
        run_started_at_ = std::chrono::steady_clock::now();
        const auto total_target = options_.count > 0 ? options_.warmup + options_.count : 0U;
        const auto period = std::chrono::microseconds(options_.period_us);
        auto next_tick = std::chrono::steady_clock::now();
        bool start_gate_released = false;
        while (worker_running_.load()) {
            if (!start_gate_released && attempts_.load() >= options_.warmup) {
                if (!mark_ready_and_wait_for_release()) return;
                start_gate_released = true;
                next_tick = std::chrono::steady_clock::now();
            }
            if (total_target > 0 && attempts_.load() >= total_target) {
                measured_cpu_finish_ = std::clock();
                run_finished_at_ = std::chrono::steady_clock::now();
                done_.store(true);
                g_running.store(false);
                worker_running_.store(false);
                return;
            }

            const auto now = std::chrono::steady_clock::now();
            if (period.count() > 0 && now < next_tick) {
                std::this_thread::sleep_until(next_tick);
                continue;
            }

            const auto transact_one = [this, total_target](std::size_t topic_index) {
                if (total_target > 0 && attempts_.load() >= total_target) {
                    return false;
                }
                const auto seq = ++seq_;
                ++attempts_;
                const auto attempt_index = attempts_.load() - 1;
                const auto payload_size = request_size_for_attempt(attempt_index);
                auto payload = r2kernel::tutorial::make_payload(seq, payload_size);
                const auto started_at = std::chrono::steady_clock::now();
                const bool measured = attempts_.load() > options_.warmup;
                if (measured && measured_cpu_start_ < 0) measured_cpu_start_ = std::clock();
                if (measured && measured_started_at_ == std::chrono::steady_clock::time_point{}) {
                    measured_started_at_ = started_at;
                }
                std::optional<r2kernel::wrapper::ControlResponse> response;
                try {
                    response = transact_control(
                        options_.target,
                        topics_[topic_index],
                        payload,
                        static_cast<float>(options_.period_us / 1000.0),
                        options_.deadline_ms,
                        options_.timeout_ms,
                        options_.priority
                    );
                } catch (const std::exception& error) {
                    last_error_ = std::string("transact_control exception: ") + error.what();
                }
                const auto rtt_us =
                    static_cast<double>(
                        std::chrono::duration_cast<std::chrono::nanoseconds>(
                            std::chrono::steady_clock::now() - started_at
                        ).count()
                    ) / 1'000.0;
                if (measured) {
                    measured_finished_at_ = std::chrono::steady_clock::now();
                    if (rtt_us > static_cast<double>(options_.deadline_ms) * 1'000.0) {
                        ++deadline_violations_;
                    }
                    if (options_.period_us > 0 && rtt_us > static_cast<double>(options_.period_us)) {
                        ++period_violations_;
                    }
                }
                const auto expected_response = r2kernel::tutorial::make_payload(seq, options_.response_size);
                const bool integrity_ok = response && response->payload == expected_response;
                if (response && response->status == 0 && integrity_ok) {
                    ++success_;
                    if (first_success_delay_ms_ < 0.0) {
                        first_success_delay_ms_ = elapsed_ms(run_started_at_, std::chrono::steady_clock::now());
                    }
                    if (measured) {
                        stats_.add_us(rtt_us);
                        if (first_measured_success_delay_ms_ < 0.0) {
                            first_measured_success_delay_ms_ =
                                elapsed_ms(run_started_at_, std::chrono::steady_clock::now());
                        }
                    }
                    if (!options_.quiet) {
                        std::cout << name() << " topic=" << topics_[topic_index] << " seq=" << seq
                                  << " rtt_us=" << rtt_us << "\n";
                    }
                } else {
                    if (response && !integrity_ok) {
                        ++integrity_errors_;
                        last_error_ = "response payload integrity mismatch";
                    }
                    if (measured) {
                        ++failures_;
                    }
                    if (!response && last_error_.empty()) {
                        last_error_ = "transact_control returned no response";
                    }
                    if (response && response->status != 0) {
                        last_error_ = "Control response status=" + std::to_string(response->status);
                    }
                    if (!options_.quiet) {
                        std::cout << name() << " topic=" << topics_[topic_index] << " seq=" << seq;
                        if (response) {
                            std::cout << " failed status=" << response->status;
                        } else {
                            std::cout << " failed";
                        }
                        std::cout << "\n";
                    }
                }
                return mark_size_stage_complete(attempt_index, payload_size);
            };

            if (options_.request_mode == "fanout") {
                for (std::size_t i = 0; i < topics_.size(); ++i) {
                    if (!transact_one(i)) break;
                }
            } else {
                const auto index = static_cast<std::size_t>(attempts_.load() % static_cast<std::uint64_t>(topics_.size()));
                transact_one(index);
            }

            if (period.count() > 0) {
                next_tick += period;
                const auto finished_at = std::chrono::steady_clock::now();
                if (finished_at > next_tick) {
                    const auto lag = finished_at - next_tick;
                    const auto missed = static_cast<std::uint64_t>(lag / period) + 1U;
                    late_cycles_.fetch_add(missed);
                    next_tick += period * static_cast<std::int64_t>(missed);
                }
            }
        }
    }

    void stop_worker() {
        worker_running_.store(false);
        if (worker_.joinable()) {
            worker_.join();
        }
        if (run_finished_at_ <= run_started_at_) {
            run_finished_at_ = std::chrono::steady_clock::now();
        }
    }

    Options options_;
    std::vector<std::string> topics_;
    mutable r2kernel::tutorial::LatencyStats stats_;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> done_ {false};
    std::atomic<std::uint64_t> attempts_ {0};
    std::atomic<std::uint64_t> success_ {0};
    std::atomic<std::uint64_t> failures_ {0};
    std::atomic<std::uint64_t> integrity_errors_ {0};
    std::atomic<std::uint64_t> deadline_violations_ {0};
    std::atomic<std::uint64_t> period_violations_ {0};
    std::atomic<std::uint64_t> late_cycles_ {0};
    std::atomic<std::uint64_t> seq_ {0};
    std::string last_error_;
    std::chrono::steady_clock::time_point run_started_at_ {};
    std::chrono::steady_clock::time_point run_finished_at_ {};
    std::chrono::steady_clock::time_point measured_started_at_ {};
    std::chrono::steady_clock::time_point measured_finished_at_ {};
    double first_success_delay_ms_ {-1.0};
    double first_measured_success_delay_ms_ {-1.0};
    std::clock_t measured_cpu_start_ {-1};
    std::clock_t measured_cpu_finish_ {-1};
    std::thread worker_;
    struct PeriodicSample {
        std::uint64_t sequence = 0;
        std::uint64_t slot = 0;
        std::int64_t scheduled_ns = 0;
        std::int64_t started_ns = 0;
        std::int64_t returned_ns = 0;
        std::int64_t sdk_started_ns = 0;
        std::int64_t sdk_returned_ns = 0;
        bool ok = false;
        bool integrity_ok = false;
        std::array<char, 256> error {};
    };
    std::vector<PeriodicSample> periodic_samples_;
    std::string thread_profile_ = "null";
    std::string setup_error_;
    bool preparation_ok_ = false;
    std::int64_t preparation_duration_ns_ = 0;
    std::size_t preparation_capacity_ = 0;
    std::optional<std::int64_t> common_start_ns_;
    std::int64_t first_warmup_ns_ = -1;
    std::int64_t periodic_start_ns_ = 0;
    std::int64_t periodic_end_ns_ = 0;
    std::uint64_t scheduled_slots_ = 0;
    std::uint64_t skipped_slots_ = 0;
    std::uint64_t completion_violations_ = 0;
    std::uint64_t caller_violations_ = 0;
    std::uint64_t completed_after_window_ = 0;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    Options options;
    try {
        options = parse_args(argc, argv);
    } catch (const std::exception& error) {
        std::cerr << "Controller configuration failed: " << error.what() << std::endl;
        return 2;
    }
    ControllerSubsystem subsystem(options);
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
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    subsystem.write_report();
    return subsystem.completion_exit_code();
}
