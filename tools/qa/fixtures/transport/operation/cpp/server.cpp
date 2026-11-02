#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <atomic>
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cctype>
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

std::optional<long long> json_int_field(const std::string& text, const std::string& key) {
    const std::string pattern = "\"" + key + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return std::nullopt;
    }
    const std::size_t colon = text.find(':', key_pos + pattern.size());
    if (colon == std::string::npos) {
        return std::nullopt;
    }
    std::size_t pos = colon + 1;
    while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos]))) {
        ++pos;
    }
    const std::size_t start = pos;
    if (pos < text.size() && (text[pos] == '-' || text[pos] == '+')) {
        ++pos;
    }
    while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) {
        ++pos;
    }
    if (pos == start) {
        return std::nullopt;
    }
    try {
        return std::stoll(text.substr(start, pos - start));
    } catch (...) {
        return std::nullopt;
    }
}

class OperationServerSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    OperationServerSubsystem(std::string name, std::string log_level)
        : r2kernel::wrapper::Subsystem(make_options(std::move(name), std::move(log_level))) {}

protected:
    bool on_initialize() override {
        // JSON goal/feedback/result travel as raw bytes, as in the Python tutorial.
        r2kernel::wrapper::OperationOptions types;
        types.request_type = "bytes";
        types.feedback_type = "bytes";
        types.result_type = "bytes";
        const bool ok = register_operation(
            "count_to",
            [this](r2kernel::wrapper::OperationRequest request, r2kernel::wrapper::OperationContext& context) -> std::optional<std::string> {
                const long long goal = std::max<long long>(0, json_int_field(request.params_json, "goal").value_or(5));
                const long long delay_ms = std::max<long long>(1, json_int_field(request.params_json, "delay_ms").value_or(200));
                logger().info("Operation", "count_to start operation_id=" + context.operation_id() +
                    " goal=" + std::to_string(goal) + " delay_ms=" + std::to_string(delay_ms));
                for (long long current = 1; current <= goal; ++current) {
                    if (context.is_cancel_requested()) {
                        const std::string payload =
                            "{\"current\":" + std::to_string(current - 1) +
                            ",\"goal\":" + std::to_string(goal) +
                            ",\"canceled\":true}";
                        logger().info("Operation", "count_to cancel operation_id=" + context.operation_id() +
                            " current=" + std::to_string(current - 1));
                        context.canceled(payload);
                        return std::nullopt;
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
                    const std::string feedback =
                        "{\"current\":" + std::to_string(current) +
                        ",\"goal\":" + std::to_string(goal) + "}";
                    context.publish_feedback(feedback);
                }
                logger().info("Operation", "count_to done operation_id=" + context.operation_id() +
                    " goal=" + std::to_string(goal));
                return std::string("{\"current\":") + std::to_string(goal) +
                    ",\"goal\":" + std::to_string(goal) + "}";
            },
            types
        );
        logger().info("Lifecycle", ok ? "operation server initialized" : "operation registration failed");
        return ok;
    }

    bool on_start() override {
        const bool ok = start_operation_server();
        if (ok) {
            logger().info("Lifecycle", "operation server ready");
        }
        return ok;
    }

    bool on_terminate() override {
        stop_operation_server();
        logger().info("Lifecycle", "operation server terminated");
        return true;
    }

    bool on_reset() override {
        stop_operation_server();
        logger().info("Lifecycle", "operation server reset");
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
};

void print_usage(const char* argv0) {
    std::cout
        << "Usage: " << argv0 << " [--name NAME] [--manifest PATH] [--log-level LEVEL]\n\n"
        << "Options:\n"
        << "  --name NAME          Subsystem name. Default: operation_server_cpp\n"
        << "  --manifest PATH      Subsystem manifest. Default: ./<name>.subsystem.yaml\n"
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

    std::string name = "operation_server_cpp";
    std::optional<std::filesystem::path> manifest_path;
    std::string log_level = "INFO";
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
        } else if (current == "--manifest") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            manifest_path = std::filesystem::path(argv[++i]);
        } else if (current.rfind("--manifest=", 0) == 0) {
            manifest_path = std::filesystem::path(current.substr(11));
        } else if (current == "--log-level") {
            if (!require_value(argc, i, current)) {
                return 2;
            }
            log_level = argv[++i];
        } else if (current.rfind("--log-level=", 0) == 0) {
            log_level = current.substr(12);
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
    const auto manifest = manifest_path.value_or(r2kernel::tutorial::default_subsystem_manifest_path(__FILE__, name));
    try {
        r2kernel::tutorial::validate_subsystem_provides(manifest, name, "operation");
    } catch (const std::exception& error) {
        std::cerr << "Subsystem manifest validation failed: " << error.what() << std::endl;
        return 2;
    }

    OperationServerSubsystem subsystem(name, log_level);
    subsystem.initialize();
    subsystem.start();
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    return 0;
}
