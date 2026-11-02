// Contract scenarios of the inherited brokered socket helpers (interface
// inherited-fds; same scenario list as the Python SDK).
#include "r2kernel/core/inherited_fds.hpp"
#include "r2kernel/core/sandbox_diagnostics.hpp"

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <optional>
#include <string>
#include <vector>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

using r2kernel::InheritedFdError;
using r2kernel::InheritedSocketExpectation;
using r2kernel::InheritedSocketFamily;
using r2kernel::InheritedSocketType;

namespace {
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

int tcp_socket(bool listen_on) {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    require(fd >= 3, "socket");
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "bind");
    if (listen_on) require(::listen(fd, 4) == 0, "listen");
    return fd;
}

int bound_port(int fd) {
    sockaddr_in address{};
    socklen_t length = sizeof(address);
    require(::getsockname(fd, reinterpret_cast<sockaddr*>(&address), &length) == 0, "getsockname");
    return ntohs(address.sin_port);
}

void set_env(const std::string& value) { ::setenv(r2kernel::kInheritedFdsEnv, value.c_str(), 1); }

InheritedSocketExpectation expect(InheritedSocketFamily family, InheritedSocketType type, bool listening,
                                  std::optional<int> port = std::nullopt) {
    InheritedSocketExpectation result;
    result.family = family;
    result.type = type;
    result.listening = listening;
    result.port = port;
    return result;
}

const auto kListener = expect(InheritedSocketFamily::inet, InheritedSocketType::stream, true);

void expect_step(const std::string& step, const std::function<void()>& call) {
    try {
        call();
    } catch (const InheritedFdError& error) {
        require(error.step() == step, "expected step " + step + ", got " + error.step() + ": " + error.what());
        require(std::string(error.what()).rfind("inherited fd " + error.kind() + ": " + step + ": ", 0) == 0,
            std::string("message format: ") + error.what());
        return;
    }
    throw std::runtime_error("expected step " + step + " was not raised");
}

void scenarios() {
    const int listener = tcp_socket(true);
    const int unconnected = tcp_socket(false);
    const int udp = ::socket(AF_INET, SOCK_DGRAM, 0);
    const int udp6 = ::socket(AF_INET6, SOCK_DGRAM, 0);
    const int file = ::open("/dev/null", O_RDONLY);
    require(udp >= 3 && udp6 >= 3 && file >= 3, "fixture descriptors");
    const auto call = [](const std::string& kind, const InheritedSocketExpectation& want) {
        return [kind, want] { (void)r2kernel::inherited_socket(kind, want); };
    };

    ::unsetenv(r2kernel::kInheritedFdsEnv);
    expect_step("env_missing", call("frontend.api", kListener));
    for (const char* invalid : {"{\"frontend.api\":", "[]", "\"x\"", "{\"frontend.api\":4.0}", "{\"frontend.api\":\"4\"}",
                                "{\"frontend.api\":true}", "{\"frontend.api\":04}", "{} x", ""}) {
        set_env(invalid);
        expect_step("env_invalid", call("frontend.api", kListener));
    }
    set_env("{\"bad kind\":" + std::to_string(listener) + "}");
    expect_step("env_invalid", call("frontend.api", kListener));
    set_env("{\"-leading\":" + std::to_string(listener) + "}");
    expect_step("env_invalid", call("frontend.api", kListener));
    set_env("{\"frontend.api\":2}");
    expect_step("env_invalid", call("frontend.api", kListener));
    set_env("{\"a\":" + std::to_string(listener) + ",\"b\":" + std::to_string(listener) + "}");
    expect_step("env_invalid", call("a", kListener));
    set_env("{}");
    expect_step("kind_missing", call("frontend.api", kListener));
    set_env("{\"file\":" + std::to_string(file) + "}");
    expect_step("not_socket", call("file", kListener));
    set_env("{\"udp6\":" + std::to_string(udp6) + "}");
    expect_step("family_mismatch", call("udp6", expect(InheritedSocketFamily::inet, InheritedSocketType::dgram, false)));
    set_env("{\"udp\":" + std::to_string(udp) + "}");
    expect_step("type_mismatch", call("udp", expect(InheritedSocketFamily::inet, InheritedSocketType::stream, false)));
    set_env("{\"plain\":" + std::to_string(unconnected) + ",\"frontend.api\":" + std::to_string(listener) + "}");
    expect_step("listening_mismatch", call("plain", kListener));
    expect_step("listening_mismatch", call("frontend.api", expect(InheritedSocketFamily::inet, InheritedSocketType::stream, false)));
    const int port = bound_port(listener);
    expect_step("port_mismatch", call("frontend.api",
        expect(InheritedSocketFamily::inet, InheritedSocketType::stream, true, port == 65535 ? 1 : port + 1)));

    // Failures leave the descriptor open and its flags unchanged.
    const int flags_before = ::fcntl(listener, F_GETFD);
    require(flags_before >= 0 && (flags_before & FD_CLOEXEC) == 0, "fixture listener must start without FD_CLOEXEC");
    bool invalid_expectation = false;
    try { (void)r2kernel::inherited_socket("frontend.api", expect(InheritedSocketFamily::inet, InheritedSocketType::dgram, true)); }
    catch (const std::invalid_argument&) { invalid_expectation = true; }
    require(invalid_expectation, "listening without stream must be rejected as an invalid expectation");

    // Success: same descriptor, FD_CLOEXEC set, nothing else changed.
    const int status_flags = ::fcntl(listener, F_GETFL);
    require(r2kernel::inherited_socket("frontend.api",
        expect(InheritedSocketFamily::inet, InheritedSocketType::stream, true, port)) == listener, "returned fd");
    require((::fcntl(listener, F_GETFD) & FD_CLOEXEC) != 0, "FD_CLOEXEC must be set on success");
    require(::fcntl(listener, F_GETFL) == status_flags && bound_port(listener) == port, "socket otherwise unchanged");

    // Leader-side encoder applies the same rules.
    require(r2kernel::encode_inherited_fds({{"frontend.api", 7}, {"can:can0", 9}}) == "{\"can:can0\":9,\"frontend.api\":7}",
        "encoded value");
    expect_step("env_invalid", [] { (void)r2kernel::encode_inherited_fds({{"frontend.api", 1}}); });
    expect_step("env_invalid", [] { (void)r2kernel::encode_inherited_fds({{"a", 5}, {"b", 5}}); });
    expect_step("env_invalid", [] { (void)r2kernel::encode_inherited_fds({{"has space", 5}}); });
    for (const int fd : {listener, unconnected, udp, udp6, file}) ::close(fd);
}

// Leader encodes and keeps the listener open across exec; the child validates it.
void child_round_trip(const char* self) {
    const int listener = tcp_socket(true);
    const int port = bound_port(listener);
    const std::string value = r2kernel::encode_inherited_fds({{"frontend.api", listener}});
    const std::string assignment = std::string(r2kernel::kInheritedFdsEnv) + "=" + value;
    std::vector<char*> env;
    for (char** item = environ; *item; ++item)
        if (std::strncmp(*item, r2kernel::kInheritedFdsEnv, std::strlen(r2kernel::kInheritedFdsEnv)) != 0) env.push_back(*item);
    env.push_back(const_cast<char*>(assignment.c_str()));
    env.push_back(nullptr);
    const std::string port_text = std::to_string(port);
    char* argv[] = {const_cast<char*>(self), const_cast<char*>("--child"), const_cast<char*>(port_text.c_str()), nullptr};
    pid_t pid = 0;
    require(::posix_spawn(&pid, self, nullptr, nullptr, argv, env.data()) == 0, "posix_spawn");
    int status = 0;
    require(::waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
        "child did not validate the inherited listener");
    ::close(listener);
}

int child_main(int port) {
    try {
        const int fd = r2kernel::inherited_socket("frontend.api",
            expect(InheritedSocketFamily::inet, InheritedSocketType::stream, true, port));
        return (::fcntl(fd, F_GETFD) & FD_CLOEXEC) != 0 ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "child: " << error.what() << '\n';
        return 2;
    }
}
void sandbox_denial_diagnostics() {
    ::unsetenv(r2kernel::kSandboxNetworkEnv);
    ::unsetenv(r2kernel::kSandboxCanEnv);
    require(!r2kernel::sandbox_socket_denial(AF_INET, EPERM), "no diagnosis without the sandbox variable");
    ::setenv(r2kernel::kSandboxNetworkEnv, "brokered", 1);
    const auto inet = r2kernel::sandbox_socket_denial(AF_INET, EPERM);
    require(inet && inet->rfind("socket(AF_INET) denied by resources.sandbox.network=brokered; ", 0) == 0,
        "AF_INET denial explanation");
    require(r2kernel::sandbox_socket_denial(AF_INET6, EPERM).has_value(), "AF_INET6 denial explanation");
    require(!r2kernel::sandbox_socket_denial(AF_INET, EACCES), "only EPERM is the sandbox denial");
    require(!r2kernel::sandbox_socket_denial(AF_UNIX, EPERM), "AF_UNIX is never sandboxed");
    require(!r2kernel::sandbox_socket_denial(29, EPERM), "AF_CAN needs R2KERNEL_SANDBOX_CAN");
    ::setenv(r2kernel::kSandboxCanEnv, "brokered", 1);
    const auto can = r2kernel::sandbox_socket_denial(29, EPERM);
    require(can && can->rfind("socket(AF_CAN) denied by the brokered CAN sandbox", 0) == 0, "AF_CAN denial explanation");
    ::unsetenv(r2kernel::kSandboxNetworkEnv);
    ::unsetenv(r2kernel::kSandboxCanEnv);
}
}  // namespace

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--child") return child_main(std::atoi(argv[2]));
    try {
        scenarios();
        sandbox_denial_diagnostics();
        child_round_trip(argv[0]);
        std::cout << "inherited fd contract scenarios passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
