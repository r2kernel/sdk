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

class OperationClientSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    OperationClientSubsystem(std::string name,
                             std::string log_level,
                             std::string target,
                             std::string operation_name,
                             int goal,
                             int delay_ms,
                             int query_period_ms,
                             int cancel_after_ms)
        : r2kernel::wrapper::Subsystem(make_options(std::move(name), std::move(log_level))),
          target_(std::move(target)),
          operation_name_(std::move(operation_name)),
          goal_(goal),
          delay_ms_(delay_ms),
          query_period_ms_(query_period_ms),
          cancel_after_ms_(cancel_after_ms) {}

    ~OperationClientSubsystem() override {
        stop_worker();
    }

    bool failed() const {
        return failed_.load();
    }

protected:
    bool on_initialize() override {
        logger().info("Lifecycle", "operation client initialized");
        return true;
    }

    bool on_start() override {
        worker_running_.store(true);
        worker_ = std::thread([this]() { run_work_loop(); });
        return true;
    }

    bool on_terminate() override {
        stop_worker();
        logger().info("Lifecycle", "operation client terminated");
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
    // error instead of terminating the process from a worker thread.
    void run_work_loop() {
        try {
            work_loop();
        } catch (const std::exception& error) {
            std::cout << name() << " operation_error=" << error.what() << std::endl;
            failed_.store(true);
            worker_running_.store(false);
            if (cancel_thread_.joinable()) {
                cancel_thread_.join();
            }
            g_running.store(false);
        }
    }

    void work_loop() {
        const std::string params_json =
            std::string("{\"goal\":") + std::to_string(goal_) +
            ",\"delay_ms\":" + std::to_string(delay_ms_) + "}";
        const auto start = start_operation(target_, operation_name_, params_json, 1000);
        if (!start.has_value() || !start->ok || !start->accepted || start->operation_id.empty()) {
            std::cout << name() << " operation_start_failed"
                      << " ok=" << (start.has_value() && start->ok ? "true" : "false")
                      << " accepted=" << (start.has_value() && start->accepted ? "true" : "false")
                      << " error_code=" << (start.has_value() ? start->error_code : "")
                      << " error_message=" << (start.has_value() ? start->error_message : "")
                      << std::endl;
            g_running.store(false);
            return;
        }
        operation_id_ = start->operation_id;
        std::cout << name() << " operation_started operation_id=" << operation_id_ << std::endl;

        subscribe_operation_state(target_, operation_name_, operation_id_, [this](const r2kernel::wrapper::OperationEventMessage& msg) {
            std::cout << name() << " state kind=" << msg.kind
                      << " state=" << msg.state
                      << " payload=" << msg.payload_json
                      << " cancel_requested=" << (msg.cancel_requested ? "true" : "false")
                      << std::endl;
        });
        subscribe_operation_feedback(target_, operation_name_, operation_id_, [this](const r2kernel::wrapper::OperationEventMessage& msg) {
            std::cout << name() << " feedback payload=" << msg.payload_json << std::endl;
        });
        subscribe_operation_result(target_, operation_name_, operation_id_, [this](const r2kernel::wrapper::OperationEventMessage& msg) {
            std::cout << name() << " result payload=" << msg.payload_json << std::endl;
        });

        if (cancel_after_ms_ > 0) {
            cancel_thread_ = std::thread([this]() {
                std::this_thread::sleep_for(std::chrono::milliseconds(cancel_after_ms_));
                if (!worker_running_.load() || operation_id_.empty()) {
                    return;
                }
                try {
                    const auto response = cancel_operation(target_, operation_id_, 1000);
                    if (response.has_value()) {
                        std::cout << name() << " cancel state=" << response->state
                                  << " cancel_requested=" << (response->cancel_requested ? "true" : "false")
                                  << std::endl;
                    }
                } catch (const std::exception& error) {
                    std::cout << name() << " operation_error=" << error.what() << std::endl;
                    failed_.store(true);
                }
            });
        }

        const auto period = std::chrono::milliseconds(query_period_ms_);
        while (worker_running_.load()) {
            const auto response = query_operation(target_, operation_id_, 1000);
            if (response.has_value()) {
                std::cout << name() << " query state=" << response->state
                          << " feedback_count=" << response->feedback_count
                          << " result=" << response->result_json
                          << std::endl;
                if (response->state == "succeeded" || response->state == "failed" || response->state == "canceled") {
                    break;
                }
            } else {
                std::cout << name() << " query=<none>" << std::endl;
            }
            std::this_thread::sleep_for(period);
        }

        if (cancel_thread_.joinable()) {
            cancel_thread_.join();
        }
        worker_running_.store(false);
        g_running.store(false);
    }

    std::string target_;
    std::string operation_name_;
    int goal_ = 10;
    int delay_ms_ = 100;
    int query_period_ms_ = 500;
    int cancel_after_ms_ = 0;
    std::string operation_id_;
    std::atomic<bool> worker_running_ {false};
    std::atomic<bool> failed_ {false};
    std::thread worker_;
    std::thread cancel_thread_;
};

void print_usage(const char* argv0) {
    std::cout
        << "Usage: " << argv0 << " [--name NAME] [--binding NAME] [--deployment-manifest PATH]\n"
        << "       " << argv0 << " [--operation-name NAME] [--goal N] [--delay-ms N]\n"
        << "       " << argv0 << " [--query-period-ms N] [--cancel-after-ms N] [--log-level LEVEL]\n\n"
        << "Options:\n"
        << "  --name NAME          Subsystem name. Default: operation_client_cpp\n"
        << "  --binding NAME       Required interface binding. Default: operation\n"
        << "  --deployment-manifest PATH\n"
        << "                       Deployment manifest. Default: ../r2kernel.deployment.yaml\n"
        << "  --operation-name NAME   Operation name. Default: count_to\n"
        << "  --goal N             Count goal. Default: 10\n"
        << "  --delay-ms N         Server-side delay per step. Default: 100\n"
        << "  --query-period-ms N  Query period. Default: 500\n"
        << "  --cancel-after-ms N  Cancel delay. 0 disables cancellation. Default: 0\n"
        << "  --log-level LEVEL    SDK log level. Default: INFO\n"
        << "  -h, --help           Show this help\n";
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

    std::string name = "operation_client_cpp";
    std::string binding = "operation";
    std::filesystem::path deployment_manifest = r2kernel::tutorial::default_deployment_manifest_path(__FILE__);
    std::string log_level = "INFO";
    std::string operation_name = "count_to";
    int goal = 10;
    int delay_ms = 100;
    int query_period_ms = 500;
    int cancel_after_ms = 0;
    for (int i = 1; i < argc; ++i) {
        const std::string current = argv[i];
        if (current == "-h" || current == "--help") {
            print_usage(argv[0]);
            return 0;
        } else if (current == "--name") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            name = argv[++i];
        } else if (current.rfind("--name=", 0) == 0) {
            name = current.substr(7);
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
        } else if (current == "--log-level") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            log_level = argv[++i];
        } else if (current.rfind("--log-level=", 0) == 0) {
            log_level = current.substr(12);
        } else if (current == "--operation-name") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            operation_name = argv[++i];
        } else if (current.rfind("--operation-name=", 0) == 0) {
            operation_name = current.substr(14);
        } else if (current == "--goal") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            goal = std::stoi(argv[++i]);
        } else if (current.rfind("--goal=", 0) == 0) {
            goal = std::stoi(current.substr(7));
        } else if (current == "--delay-ms") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            delay_ms = std::stoi(argv[++i]);
        } else if (current.rfind("--delay-ms=", 0) == 0) {
            delay_ms = std::stoi(current.substr(11));
        } else if (current == "--query-period-ms") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            query_period_ms = std::stoi(argv[++i]);
        } else if (current.rfind("--query-period-ms=", 0) == 0) {
            query_period_ms = std::stoi(current.substr(18));
        } else if (current == "--cancel-after-ms") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            cancel_after_ms = std::stoi(argv[++i]);
        } else if (current.rfind("--cancel-after-ms=", 0) == 0) {
            cancel_after_ms = std::stoi(current.substr(18));
        } else {
            std::cerr << "Unknown argument: " << current << std::endl;
            print_usage(argv[0]);
            return 2;
        }
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
        OperationClientSubsystem subsystem(name, log_level, target.subsystem, operation_name, goal, delay_ms, query_period_ms, cancel_after_ms);
        subsystem.initialize();
        subsystem.start();
        while (g_running.load()) {
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
