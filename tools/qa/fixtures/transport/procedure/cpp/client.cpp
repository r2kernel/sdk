#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>

namespace {

std::atomic<bool> g_running {true};

void handle_signal(int) {
    g_running.store(false);
}

class ProcedureClientSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    ProcedureClientSubsystem(std::string name, std::string log_level, std::string target, int arg, int period_ms, int count, bool quiet)
        : r2kernel::wrapper::Subsystem(make_options(std::move(name), std::move(log_level))),
          target_(std::move(target)),
          arg_(arg),
          period_ms_(period_ms),
          count_(count),
          quiet_(quiet) {}

    ~ProcedureClientSubsystem() override {
        stop_worker();
    }

    bool active() const {
        return worker_running_.load();
    }

    bool failed() const {
        return failed_.load();
    }

protected:
    bool on_initialize() override {
        logger().info("Lifecycle", "procedure client initialized");
        return true;
    }

    bool on_start() override {
        worker_running_.store(true);
        worker_ = std::thread([this]() { run_work_loop(); });
        return true;
    }

    bool on_terminate() override {
        stop_worker();
        logger().info("Lifecycle", "procedure client terminated");
        return true;
    }

    bool on_reset() override {
        stop_worker();
        return true;
    }

private:
    static r2kernel::wrapper::SubsystemOptions make_options(const std::string& name, const std::string& log_level) {
        r2kernel::wrapper::SubsystemOptions options;
        options.name = name;
        options.log_level = log_level;
        options.core_client = std::make_shared<r2kernel::core::R2KernelClient>(r2kernel::core::ClientOptions{name});
        return options;
    }

    void stop_worker() {
        worker_running_.store(false);
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    // A failed request (a Control transport failure throws) ends the run with an
    // error instead of terminating the process from the worker thread.
    void run_work_loop() {
        try {
            work_loop();
        } catch (const std::exception& error) {
            std::cout << name() << " procedure_error=" << error.what() << std::endl;
            failed_.store(true);
            worker_running_.store(false);
        }
    }

    void work_loop() {
        const auto period = std::chrono::milliseconds(period_ms_);
        int attempts = 0;
        while (worker_running_.load()) {
            const auto response = call_procedure(target_, "increment", "{\"arg\":" + std::to_string(arg_) + "}", 1000);
            ++attempts;
            if (!quiet_) {
                if (response.has_value()) {
                    std::cout << name() << " procedure_response={ok=" << (response->ok ? "true" : "false")
                              << ", request_id=" << response->request_id
                              << ", result=" << response->result_json
                              << ", error_code=" << response->error_code
                              << ", error_message=" << response->error_message << "}" << std::endl;
                } else {
                    std::cout << name() << " procedure_response=<none>" << std::endl;
                }
            }
            if (count_ > 0 && attempts >= count_) {
                break;
            }
            std::this_thread::sleep_for(period);
        }
        worker_running_.store(false);
    }

    std::string target_;
    int arg_ = 1;
    int period_ms_ = 1000;
    int count_ = 0;
    bool quiet_ = false;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> failed_ {false};
    std::thread worker_;
};

void print_usage(const char* argv0) {
    std::cout
        << "Usage: " << argv0 << " [--name NAME] [--binding NAME] [--deployment-manifest PATH]\n"
        << "       " << argv0 << " [--arg N] [--count N] [--period-ms N]\n"
        << "       " << argv0 << " [--log-level LEVEL] [--quiet]\n\n"
        << "Options:\n"
        << "  --name NAME          Subsystem name. Default: procedure_client_cpp\n"
        << "  --binding NAME       Required interface binding. Default: procedure\n"
        << "  --deployment-manifest PATH\n"
        << "                       Deployment manifest. Default: ../r2kernel.deployment.yaml\n"
        << "  --arg N              Integer argument for increment. Default: 1\n"
        << "  --count N            Number of requests before exit. 0 means run until interrupted.\n"
        << "  --period-ms N        Request period in milliseconds. Default: 1000\n"
        << "  --log-level LEVEL    SDK log level. Default: INFO\n"
        << "  --quiet              Suppress per-response stdout\n"
        << "  -h, --help           Show this help\n";
}

bool parse_int_option(const std::string& name, const std::string& text, int& value) {
    try {
        size_t consumed = 0;
        const int parsed = std::stoi(text, &consumed);
        if (consumed != text.size()) {
            std::cerr << "Invalid integer for " << name << ": " << text << std::endl;
            return false;
        }
        value = parsed;
        return true;
    } catch (const std::exception&) {
        std::cerr << "Invalid integer for " << name << ": " << text << std::endl;
        return false;
    }
}

bool require_value(int argc, int index, const std::string& option) {
    if (index + 1 < argc) {
        return true;
    }
    std::cerr << "Missing value for " << option << std::endl;
    return false;
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    std::string name = "procedure_client_cpp";
    std::string binding = "procedure";
    std::filesystem::path deployment_manifest = r2kernel::tutorial::default_deployment_manifest_path(__FILE__);
    std::string log_level = "INFO";
    int arg = 1;
    int period_ms = 1000;
    int count = 0;
    bool quiet = false;
    for (int i = 1; i < argc; ++i) {
        const std::string current = argv[i];
        if (current == "-h" || current == "--help") {
            print_usage(argv[0]);
            return 0;
        } else if (current == "--quiet") {
            quiet = true;
        } else if (current == "--name") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            name = argv[++i];
        } else if (current.rfind("--name=", 0) == 0) {
            name = current.substr(7);
        } else if (current == "--log-level") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            log_level = argv[++i];
        } else if (current.rfind("--log-level=", 0) == 0) {
            log_level = current.substr(12);
        } else if (current == "--binding") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            binding = argv[++i];
        } else if (current.rfind("--binding=", 0) == 0) {
            binding = current.substr(10);
        } else if (current == "--deployment-manifest") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            deployment_manifest = argv[++i];
        } else if (current.rfind("--deployment-manifest=", 0) == 0) {
            deployment_manifest = current.substr(22);
        } else if (current == "--arg") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            if (!parse_int_option(current, argv[++i], arg)) {
                return 2;
            }
        } else if (current.rfind("--arg=", 0) == 0) {
            if (!parse_int_option("--arg", current.substr(6), arg)) {
                return 2;
            }
        } else if (current == "--period-ms") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            if (!parse_int_option(current, argv[++i], period_ms)) {
                return 2;
            }
        } else if (current.rfind("--period-ms=", 0) == 0) {
            if (!parse_int_option("--period-ms", current.substr(12), period_ms)) {
                return 2;
            }
        } else if (current == "--count") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            if (!parse_int_option(current, argv[++i], count)) {
                return 2;
            }
        } else if (current.rfind("--count=", 0) == 0) {
            if (!parse_int_option("--count", current.substr(8), count)) {
                return 2;
            }
        } else {
            std::cerr << "Unknown argument: " << current << std::endl;
            print_usage(argv[0]);
            return 2;
        }
    }
    if (period_ms < 0) {
        std::cerr << "--period-ms must be >= 0" << std::endl;
        return 2;
    }
    if (count < 0) {
        std::cerr << "--count must be >= 0" << std::endl;
        return 2;
    }
    if (name.empty()) {
        std::cerr << "--name must not be empty" << std::endl;
        return 2;
    }
    if (binding.empty()) {
        std::cerr << "--binding must not be empty" << std::endl;
        return 2;
    }

    try {
        const auto target = r2kernel::tutorial::resolve_binding_target(deployment_manifest, name, binding);
        ProcedureClientSubsystem subsystem(name, log_level, target.subsystem, arg, period_ms, count, quiet);
        subsystem.initialize();
        subsystem.start();
        while (g_running.load() && subsystem.active()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        subsystem.terminate();
        if (subsystem.failed()) {
            return 1;
        }
    } catch (const std::exception& error) {
        std::cerr << "Binding resolution failed: " << error.what() << std::endl;
        return 2;
    }
    return 0;
}
