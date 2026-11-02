// Local Control handler socket permissions and the verified Protocol host
// authority, driven by the cases the Python SDK also runs
// (sdk/testdata/parity, tests/test_sdk_parity_cases.py).
#include "../src/core/client.cpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <sys/stat.h>

namespace {

namespace fs = std::filesystem;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

class ScopedEnvironment {
public:
    ScopedEnvironment(const char* name, const YAML::Node& value) : name_(name) {
        if (const char* previous = std::getenv(name)) previous_ = previous;
        if (value && !value.IsNull()) ::setenv(name, value.as<std::string>().c_str(), 1);
        else ::unsetenv(name);
    }
    ~ScopedEnvironment() {
        if (previous_) ::setenv(name_, previous_->c_str(), 1);
        else ::unsetenv(name_);
    }
    ScopedEnvironment(const ScopedEnvironment&) = delete;
    ScopedEnvironment& operator=(const ScopedEnvironment&) = delete;

private:
    const char* name_;
    std::optional<std::string> previous_;
};

std::string octal(mode_t mode) {
    char text[8];
    std::snprintf(text, sizeof(text), "%04o", static_cast<unsigned>(mode & 07777));
    return text;
}

mode_t file_mode(const fs::path& path) {
    struct stat status {};
    require(::stat(path.c_str(), &status) == 0, "stat failed: " + path.string());
    return status.st_mode & 07777;
}

void socket_mode_cases(const fs::path& parity, const fs::path& scratch) {
    const YAML::Node absent_group(std::string("r2kernel-parity-absent-group"));
    ScopedEnvironment group("R2KERNEL_RUNTIME_IPC_GROUP", absent_group);
    for (const auto& item : YAML::LoadFile((parity / "ipc-socket-mode.yaml").string())["cases"]) {
        const std::string name = item["name"].as<std::string>();
        ScopedEnvironment mode("R2KERNEL_RUNTIME_IPC_SOCKET_MODE", item["env"]);
        const fs::path path = scratch / ("mode-" + name);
        std::ofstream(path).put('\n');
        ::chmod(path.c_str(), 0600);
        try {
            r2kernel::core::apply_control_handler_socket_policy(path.string());
            require(!item["error"], name + ": accepted a mode the Python SDK refuses");
            require(octal(file_mode(path)) == item["mode"].as<std::string>(),
                    name + ": mode " + octal(file_mode(path)) + ", expected " + item["mode"].as<std::string>());
        } catch (const std::invalid_argument& error) {
            require(bool(item["error"]), name + ": refused a mode the Python SDK accepts: " + error.what());
            require(std::string(error.what()).find(item["error"].as<std::string>()) != std::string::npos,
                    name + ": unexpected message: " + error.what());
        }
    }
    // A real listener of the Control handler gets the policy before listen().
    ScopedEnvironment unset_mode("R2KERNEL_RUNTIME_IPC_SOCKET_MODE", YAML::Node());
    const std::string endpoint = (scratch / "handler.sock").string();
    const auto socket = r2kernel::core::create_unix_listener(endpoint, r2kernel::core::apply_control_handler_socket_policy);
    require(file_mode(endpoint) == 0660, "Control handler socket mode is " + octal(file_mode(endpoint)));
    r2kernel::core::close_socket(socket);
    (void)::unlink(endpoint.c_str());
}

void host_authority_cases(const fs::path& parity) {
    using r2kernel::wrapper::current_protocol_host_authority;
    for (const auto& item : YAML::LoadFile((parity / "host-authority.yaml").string())["cases"]) {
        const std::string name = item["name"].as<std::string>();
        r2kernel::wrapper::ControlRequest request;
        request.sender = item["sender"].as<std::string>();
        request.topic = "anything";
        try {
            r2kernel::core::TransportAuthorityScope scope(request, item["verified"].as<bool>());
            require(!item["error"], name + ": accepted a sender the Python SDK refuses");
            const auto authority = current_protocol_host_authority();
            const YAML::Node expected = item["authority"];
            if (!expected || expected.IsNull()) {
                require(!authority, name + ": unexpected host authority");
            } else {
                require(authority && authority->kind == expected["kind"].as<std::string>()
                            && authority->peer == expected["peer"].as<std::string>()
                            && authority->epoch == expected["epoch"].as<std::string>(),
                        name + ": host authority differs from the Python SDK");
            }
            require(request.sender == item["handler_sender"].as<std::string>(), name + ": handler sender " + request.sender);
        } catch (const std::runtime_error& error) {
            require(bool(item["error"]), name + ": refused a sender the Python SDK accepts: " + error.what());
            require(std::string(error.what()).find(item["error"].as<std::string>()) != std::string::npos,
                    name + ": unexpected message: " + error.what());
        }
        require(!current_protocol_host_authority(), name + ": host authority outlived the request");
    }
}

}  // namespace

int main() {
    try {
        const fs::path parity = R2KERNEL_SDK_PARITY_DIR;
        const fs::path scratch = fs::temp_directory_path() / ("r2kernel-local-policy-" + std::to_string(::getpid()));
        fs::create_directories(scratch);
        try {
            socket_mode_cases(parity, scratch);
            host_authority_cases(parity);
        } catch (...) {
            fs::remove_all(scratch);
            throw;
        }
        fs::remove_all(scratch);
        std::cout << "Control handler socket policy and host authority match the Python SDK cases\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
