#include "manifest_binding.hpp"
#include "r2kernel/core/client.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cctype>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
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

class ProcedureServerSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    ProcedureServerSubsystem(std::string name, std::string log_level)
        : r2kernel::wrapper::Subsystem(make_options(std::move(name), std::move(log_level))) {}

protected:
    bool on_initialize() override {
        // JSON parameters travel as raw bytes, as in the Python tutorial.
        r2kernel::wrapper::ProcedureOptions types;
        types.request_type = "bytes";
        types.response_type = "bytes";
        const bool ok = register_procedure(
            "increment",
            [this](r2kernel::wrapper::ProcedureRequest request) {
                const long long arg = json_int_field(request.params_json, "arg").value_or(1);
                count_ += static_cast<int>(arg);
                logger().info("Procedure", "increment arg=" + std::to_string(arg) + " count=" + std::to_string(count_));
                r2kernel::wrapper::ProcedureResponse response;
                response.ok = true;
                response.request_id = request.request_id;
                response.result_json = "{\"count\":" + std::to_string(count_) + "}";
                response.timestamp_ns = now_ns();
                return response;
            },
            types
        );
        logger().info("Lifecycle", ok ? "procedure server initialized" : "procedure registration failed");
        return ok;
    }

    bool on_start() override {
        const bool ok = start_procedure_server();
        if (ok) {
            logger().info("Lifecycle", "procedure server ready");
        }
        return ok;
    }

    bool on_terminate() override {
        stop_procedure_server();
        logger().info("Lifecycle", "procedure server terminated");
        return true;
    }

    bool on_reset() override {
        stop_procedure_server();
        logger().info("Lifecycle", "procedure server reset");
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

    int count_ = 0;
};

void print_usage(const char* argv0) {
    std::cout
        << "Usage: " << argv0 << " [--name NAME] [--manifest PATH] [--log-level LEVEL]\n\n"
        << "Options:\n"
        << "  --name NAME          Subsystem name. Default: procedure_server_cpp\n"
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

    std::string name = "procedure_server_cpp";
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
        r2kernel::tutorial::validate_subsystem_provides(manifest, name, "procedure");
    } catch (const std::exception& error) {
        std::cerr << "Subsystem manifest validation failed: " << error.what() << std::endl;
        return 2;
    }

    ProcedureServerSubsystem subsystem(name, log_level);
    subsystem.initialize();
    subsystem.start();
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    subsystem.terminate();
    return 0;
}
