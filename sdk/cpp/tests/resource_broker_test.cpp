// ResourceBrokerClient against a private fake broker that passes real rights
// (same scenarios as the Python SDK's tests/test_resource_broker.py). A probe
// socketpair observes the transferred descriptor's lifetime: the probe reads
// EOF only once every copy the client received is closed.
#include "r2kernel/core/resource_broker_client.hpp"

#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

using r2kernel::core::BrokeredFdResult;
using r2kernel::core::ResourceBrokerClient;

namespace {

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

// One response fragment: data plus how many copies of the transferred
// descriptor travel with it.
struct Fragment {
    std::string data;
    int rights = 0;
};

class FakeBroker {
public:
    explicit FakeBroker(std::vector<Fragment> fragments)
        : path_(std::filesystem::temp_directory_path()
                / ("r2kernel-cpp-broker-" + std::to_string(::getpid()) + "-"
                   + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) + ".sock")) {
        listener_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        require(listener_ >= 0, "broker socket");
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, path_.c_str(), path_.string().size() + 1);
        require(::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "broker bind");
        require(::listen(listener_, 1) == 0, "broker listen");
        int pair[2];
        require(::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) == 0, "probe socketpair");
        probe_ = pair[0];
        transferred_ = pair[1];
        thread_ = std::thread([this, fragments = std::move(fragments)] { serve(fragments); });
    }
    ~FakeBroker() {
        if (thread_.joinable()) thread_.join();
        ::close(listener_);
        if (probe_ >= 0) ::close(probe_);
        ::unlink(path_.c_str());
    }
    std::string path() const { return path_.string(); }
    int probe() const { return probe_; }
    // Waits for the server; returns the single request line it read.
    std::string finish() {
        thread_.join();
        require(failure_.empty(), "fake broker failed: " + failure_);
        return request_;
    }
    // True when the probe reads EOF within 2 s: every transferred copy closed.
    bool probe_closed() const {
        pollfd descriptor {probe_, POLLIN, 0};
        if (::poll(&descriptor, 1, 2000) != 1) return false;
        char byte = 0;
        return ::recv(probe_, &byte, 1, 0) == 0;
    }

private:
    void serve(const std::vector<Fragment>& fragments) {
        try {
            const int connection = ::accept4(listener_, nullptr, nullptr, SOCK_CLOEXEC);
            require(connection >= 0, "broker accept");
            char buffer[8192];
            while (request_.find('\n') == std::string::npos) {
                const auto count = ::recv(connection, buffer, sizeof(buffer), 0);
                require(count > 0, "client did not submit a complete request");
                request_.append(buffer, static_cast<std::size_t>(count));
            }
            for (const auto& fragment : fragments) {
                iovec vector {const_cast<char*>(fragment.data.data()), fragment.data.size()};
                msghdr message {};
                message.msg_iov = &vector;
                message.msg_iovlen = 1;
                std::vector<char> control(CMSG_SPACE(sizeof(int) * static_cast<std::size_t>(fragment.rights)));
                if (fragment.rights > 0) {
                    message.msg_control = control.data();
                    message.msg_controllen = control.size();
                    cmsghdr* header = CMSG_FIRSTHDR(&message);
                    header->cmsg_level = SOL_SOCKET;
                    header->cmsg_type = SCM_RIGHTS;
                    header->cmsg_len = CMSG_LEN(sizeof(int) * static_cast<std::size_t>(fragment.rights));
                    for (int index = 0; index < fragment.rights; ++index)
                        std::memcpy(CMSG_DATA(header) + sizeof(int) * static_cast<std::size_t>(index), &transferred_, sizeof(int));
                }
                // Rejection can close the client before every hostile byte is written.
                if (::sendmsg(connection, &message, MSG_NOSIGNAL) < 0) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
            ::close(connection);
        } catch (const std::exception& error) {
            failure_ = error.what();
        }
        ::close(transferred_);
    }

    std::filesystem::path path_;
    int listener_ = -1;
    int probe_ = -1;
    int transferred_ = -1;
    std::thread thread_;
    std::string request_;
    std::string failure_;
};

void test_fragmented_success_transfers_one_usable_noninheritable_fd(int rights_fragment) {
    FakeBroker broker({{"{\"ok\":t", rights_fragment == 0 ? 1 : 0},
                       {"rue,\"result\":{\"device\":\"private\"}}\n", rights_fragment == 1 ? 1 : 0}});
    ResourceBrokerClient client("private-test-lease", broker.path());
    BrokeredFdResult result = client.open_device("/dev/null");
    const std::string request = broker.finish();
    require(request == "{\"op\":\"open_device\",\"lease_id\":\"private-test-lease\",\"path\":\"/dev/null\","
                       "\"read\":true,\"write\":true,\"nonblocking\":true}\n",
            "open_device request line: " + request);
    require(result.metadata_json == "{\"device\":\"private\"}", "metadata: " + result.metadata_json);
    require(result.fd >= 0, "descriptor");
    require((::fcntl(result.fd, F_GETFD) & FD_CLOEXEC) != 0, "brokered descriptor is close-on-exec");
    require(::send(result.fd, "usable", 6, MSG_NOSIGNAL) == 6, "brokered descriptor is usable");
    char received[6] = {};
    require(::recv(broker.probe(), received, sizeof(received), 0) == 6 && std::string(received, 6) == "usable",
            "probe receives through the brokered descriptor");
    result.close();
    require(result.fd == -1, "close resets the descriptor");
    require(broker.probe_closed(), "closing the result releases the only copy");
}

void test_rejected_response_closes_all_rights(const std::vector<Fragment>& fragments, const std::string& name) {
    FakeBroker broker(fragments);
    ResourceBrokerClient client("private-test-lease", broker.path());
    bool rejected = false;
    try {
        (void)client.open_device("/dev/null");
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    (void)broker.finish();
    require(rejected, "rejected response: " + name);
    require(broker.probe_closed(), "rejected response closes every right: " + name);
}

void test_rejections() {
    const std::string valid = "{\"ok\":true,\"result\":{}}\n";
    const std::vector<std::pair<std::string, std::vector<Fragment>>> cases = {
        {"refused", {{"{\"ok\":false,\"error\":\"denied\"}\n", 1}}},
        {"refused without right", {{"{\"ok\":false,\"error\":\"not attached to a process yet\"}\n", 0}}},
        {"invalid json", {{"{\"ok\":true,\"result\":{", 1}, {"bad json}\n", 0}}},
        {"invalid utf-8", {{"{\"ok\":true,\"result\":{\"name\":\"\xff\"}}\n", 1}}},
        {"eof before newline", {{"{\"ok\":true,\"result\":{}}", 1}}},
        {"two rights", {{valid, 2}}},
        {"rights in two fragments", {{"{\"ok\":", 1}, {"true,\"result\":{}}\n", 1}}},
        {"control truncation", {{valid, 80}}},
        {"no right", {{valid, 0}}},
        {"array", {{"[]\n", 1}}},
        {"ok not bool", {{"{\"ok\":1,\"result\":{}}\n", 1}}},
        {"result not object", {{"{\"ok\":true,\"result\":[]}\n", 1}}},
        {"two responses", {{"{\"ok\":true,\"result\":{}}\n{}\n", 1}}},
        {"oversize", {{"{\"ok\":true,\"result\":{\"data\":\"", 1}, {std::string(1024 * 1024, 'x') + "\"}}\n", 0}}},
    };
    for (const auto& [name, fragments] : cases) test_rejected_response_closes_all_rights(fragments, name);
}

void test_refusal_text_and_request_shapes() {
    {
        FakeBroker broker({{"{\"ok\":false,\"error\":\"denied by policy\"}\n", 0}});
        ResourceBrokerClient client("lease-1", broker.path());
        r2kernel::core::TcpSocketOptions options;
        options.interface_name = "eth0";
        options.connect_host = "10.0.0.2";
        options.connect_port = 502;
        std::string error;
        try {
            (void)client.open_tcp_socket(options);
        } catch (const std::runtime_error& refused) {
            error = refused.what();
        }
        require(error == "denied by policy", "broker refusal text verbatim: " + error);
        require(broker.finish() == "{\"op\":\"open_tcp_socket\",\"lease_id\":\"lease-1\",\"interface\":\"eth0\","
                                   "\"bind_host\":\"0.0.0.0\",\"bind_port\":0,\"connect_host\":\"10.0.0.2\","
                                   "\"connect_port\":502,\"listen\":false,\"backlog\":128}\n",
                "open_tcp_socket request line");
    }
    const auto request_of = [](const std::function<void(const ResourceBrokerClient&)>& call) {
        FakeBroker broker({{"{\"ok\":false}\n", 0}});
        ResourceBrokerClient client("lease-1", broker.path());
        std::string error;
        try {
            call(client);
        } catch (const std::runtime_error& refused) {
            error = refused.what();
        }
        require(error == "R2Kernel resource request failed", "default refusal text: " + error);
        return broker.finish();
    };
    require(request_of([](const ResourceBrokerClient& client) {
                r2kernel::core::UdpSocketOptions options;
                options.interface_name = "eth1";
                options.bind_port = 9000;
                (void)client.open_udp_socket(options);
            }) == "{\"op\":\"open_udp_socket\",\"lease_id\":\"lease-1\",\"interface\":\"eth1\","
                  "\"bind_host\":\"0.0.0.0\",\"bind_port\":9000}\n",
            "open_udp_socket request line");
    require(request_of([](const ResourceBrokerClient& client) {
                r2kernel::core::TcpSocketOptions options;
                options.interface_name = "eth0";
                options.listen = true;
                (void)client.open_tcp_socket(options);
            }) == "{\"op\":\"open_tcp_socket\",\"lease_id\":\"lease-1\",\"interface\":\"eth0\","
                  "\"bind_host\":\"0.0.0.0\",\"bind_port\":0,\"connect_host\":null,\"connect_port\":null,"
                  "\"listen\":true,\"backlog\":128}\n",
            "open_tcp_socket listener request line");
    require(request_of([](const ResourceBrokerClient& client) { (void)client.open_frontend_tcp_listener(); })
                == "{\"op\":\"open_frontend_tcp_listener\",\"lease_id\":\"lease-1\",\"kind\":\"api\","
                   "\"bind_host\":\"\",\"bind_port\":0,\"backlog\":128}\n",
            "open_frontend_tcp_listener request line");
    require(request_of([](const ResourceBrokerClient& client) {
                r2kernel::core::CanSocketOptions options;
                options.interface_name = "can0";
                options.can_fd_frames = true;
                (void)client.open_can_socket(options);
            }) == "{\"op\":\"open_can_socket\",\"lease_id\":\"lease-1\",\"interface\":\"can0\","
                  "\"receive_own_messages\":false,\"can_fd_frames\":true,\"nonblocking\":true}\n",
            "open_can_socket request line");
}

void test_environment_lease_and_socket() {
    ::unsetenv(r2kernel::core::kResourceLeaseIdEnv);
    ::unsetenv(r2kernel::core::kResourceServiceSocketEnv);
    std::string error;
    try {
        ResourceBrokerClient client;
    } catch (const std::runtime_error& missing) {
        error = missing.what();
    }
    require(error.find("R2KERNEL_RESOURCE_LEASE_ID is not set") == 0, "missing lease is refused: " + error);
    ::setenv(r2kernel::core::kResourceLeaseIdEnv, "env-lease", 1);
    require(ResourceBrokerClient().socket_path() == r2kernel::core::kDefaultResourceSocket, "default broker socket");
    // R2KernelClient's open_* pass-throughs build exactly this client from the
    // environment (the R2KernelClient itself needs a running Control).
    FakeBroker broker({{"{\"ok\":true,\"result\":{\"kind\":\"api\"}}\n", 1}});
    ::setenv(r2kernel::core::kResourceServiceSocketEnv, broker.path().c_str(), 1);
    const ResourceBrokerClient from_env;
    require(from_env.lease_id() == "env-lease" && from_env.socket_path() == broker.path(), "environment lease and socket");
    auto listener = from_env.open_frontend_tcp_listener();
    require(broker.finish().find("\"lease_id\":\"env-lease\"") != std::string::npos, "request uses the env lease");
    require(listener.socket >= 0 && listener.metadata_json == "{\"kind\":\"api\"}", "frontend listener result");
    listener.close();
    require(broker.probe_closed(), "the result owns the descriptor");
    ::unsetenv(r2kernel::core::kResourceLeaseIdEnv);
    ::unsetenv(r2kernel::core::kResourceServiceSocketEnv);
}

}  // namespace

int main() {
    try {
        test_fragmented_success_transfers_one_usable_noninheritable_fd(0);
        test_fragmented_success_transfers_one_usable_noninheritable_fd(1);
        test_rejections();
        test_refusal_text_and_request_shapes();
        test_environment_lease_and_socket();
    } catch (const std::exception& error) {
        std::cerr << "resource_broker_test failed: " << error.what() << '\n';
        return 1;
    }
    std::cout << "resource_broker_test passed\n";
    return 0;
}
