// ResourceBrokerClient against a private fake broker that passes real rights
// (same scenarios as the Python SDK's tests/test_resource_broker.py and
// tests/test_uplink_tcp.py). A probe socketpair observes the transferred
// descriptor's lifetime: the probe reads EOF only once every copy the client
// received is closed.
#include "r2kernel/core/client.hpp"
#include "r2kernel/core/resource_broker_client.hpp"

#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

#include <netinet/in.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

using r2kernel::core::BrokeredFdResult;
using r2kernel::core::BrokeredSocketResult;
using r2kernel::core::ResourceBrokerClient;
using r2kernel::core::ResourceBrokerError;
using r2kernel::core::UplinkTcpOptions;

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
    // delay holds the reply back after the request line was read.
    explicit FakeBroker(std::vector<Fragment> fragments, int supplied_fd = -1,
                        std::chrono::milliseconds delay = std::chrono::milliseconds(0))
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
        if (supplied_fd >= 0) {
            ::close(transferred_);
            transferred_ = ::dup(supplied_fd);
            require(transferred_ >= 0, "duplicate supplied descriptor");
        }
        thread_ = std::thread([this, fragments = std::move(fragments), delay] { serve(fragments, delay); });
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
    void serve(const std::vector<Fragment>& fragments, std::chrono::milliseconds delay) {
        try {
            const int connection = ::accept4(listener_, nullptr, nullptr, SOCK_CLOEXEC);
            require(connection >= 0, "broker accept");
            char buffer[8192];
            while (request_.find('\n') == std::string::npos) {
                const auto count = ::recv(connection, buffer, sizeof(buffer), 0);
                require(count > 0, "client did not submit a complete request");
                request_.append(buffer, static_cast<std::size_t>(count));
            }
            std::this_thread::sleep_for(delay);
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

int frontend_listener(int family) {
    const int fd = ::socket(family, SOCK_STREAM | SOCK_CLOEXEC, 0);
    require(fd >= 0, "frontend fixture socket");
    if (family == AF_UNIX) {
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        const std::string name = "r2k-sdk-frontend-" + std::to_string(::getpid()) + "-"
            + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
        std::memcpy(address.sun_path + 1, name.data(), name.size());
        require(::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "frontend Unix bind");
    } else {
        sockaddr_in address {};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        require(::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "frontend TCP bind");
    }
    require(::listen(fd, 2) == 0, "frontend fixture listen");
    return fd;
}

const std::string kFrontendMetadata =
    "{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":18091}";

void test_frontend_rejects_metadata_and_nonlistener_rights() {
    for (const std::string& metadata : {
            std::string("{}"),
            std::string("{\"domain\":\"inet\",\"transport\":\"unix_listener\"}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"tcp_listener\"}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"0.0.0.0\",\"bind_port\":18091}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":true}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":\"18091\"}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":1.5}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":0}"),
            std::string("{\"domain\":\"unix\",\"transport\":\"unix_listener\",\"bind_host\":\"127.0.0.1\",\"bind_port\":65536}"),
            kFrontendMetadata}) {
        FakeBroker broker({{"{\"ok\":true,\"result\":" + metadata + "}\n", 1}});
        ResourceBrokerClient client("frontend-test", broker.path());
        bool rejected = false;
        try { (void)client.open_frontend_tcp_listener(); }
        catch (const std::runtime_error&) { rejected = true; }
        (void)broker.finish();
        require(rejected, "frontend rejects invalid metadata or a connected nonlistener");
        require(broker.probe_closed(), "frontend rejection closes its received descriptor");
    }
}

void test_frontend_rejects_tcp_datagram_and_file_descriptors() {
    const int tcp = frontend_listener(AF_INET);
    const int datagram = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    const int file = ::open("/dev/null", O_RDONLY | O_CLOEXEC);
    require(datagram >= 0 && file >= 0, "frontend mismatch fixtures");
    const auto count = [] {
        return std::distance(std::filesystem::directory_iterator("/proc/self/fd"), std::filesystem::directory_iterator());
    };
    for (int fd : {tcp, datagram, file}) {
        const auto before = count();
        {
            FakeBroker broker({{"{\"ok\":true,\"result\":" + kFrontendMetadata + "}\n", 1}}, fd);
            ResourceBrokerClient client("frontend-test", broker.path());
            bool rejected = false;
            try { (void)client.open_frontend_tcp_listener(); }
            catch (const std::runtime_error&) { rejected = true; }
            (void)broker.finish();
            require(rejected, "frontend descriptor identity is checked");
        }
        require(count() == before, "frontend descriptor mismatch closes the received right");
        ::close(fd);
    }
}

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
    const int frontend = frontend_listener(AF_UNIX);
    FakeBroker broker({{"{\"ok\":true,\"result\":" + kFrontendMetadata + "}\n", 1}}, frontend);
    ::setenv(r2kernel::core::kResourceServiceSocketEnv, broker.path().c_str(), 1);
    const ResourceBrokerClient from_env;
    require(from_env.lease_id() == "env-lease" && from_env.socket_path() == broker.path(), "environment lease and socket");
    auto listener = from_env.open_frontend_tcp_listener();
    require(broker.finish().find("\"lease_id\":\"env-lease\"") != std::string::npos, "request uses the env lease");
    require(listener.socket >= 0 && listener.metadata_json == kFrontendMetadata, "frontend Unix listener result");
    int family = 0;
    socklen_t length = sizeof(family);
    require(::getsockopt(listener.socket, SOL_SOCKET, SO_DOMAIN, &family, &length) == 0 && family == AF_UNIX,
            "frontend result owns a Unix listener");
    listener.close();
    pollfd frontend_poll {frontend, POLLHUP, 0};
    require(::poll(&frontend_poll, 1, 1000) == 1 && (frontend_poll.revents & POLLHUP) != 0,
            "frontend result close shuts down the broker's listener copy");
    ::close(frontend);
    ::unsetenv(r2kernel::core::kResourceLeaseIdEnv);
    ::unsetenv(r2kernel::core::kResourceServiceSocketEnv);
}

// The retired per-execution grant operation is gone from both clients.
template <typename T, typename = void>
struct has_approved_tcp_stream : std::false_type {};
template <typename T>
struct has_approved_tcp_stream<T, std::void_t<decltype(&T::open_approved_tcp_stream)>> : std::true_type {};
static_assert(!has_approved_tcp_stream<ResourceBrokerClient>::value, "open_approved_tcp_stream is retired");
static_assert(!has_approved_tcp_stream<r2kernel::core::R2KernelClient>::value, "open_approved_tcp_stream is retired");

// Uplink result metadata as raw JSON member values; an empty value omits the member.
struct UplinkMetadata {
    std::string kind = "\"uplink_tcp\"";
    std::string connection_id = "\"0123456789abcdef0123456789abcdef\"";
    std::string lease_id = "\"lease1\"";
    std::string host = "\"api.example.com\"";
    std::string port = "443";

    std::string json() const {
        std::string members;
        const auto add = [&](const char* key, const std::string& value) {
            if (!value.empty()) members += std::string(members.empty() ? "" : ",") + "\"" + key + "\":" + value;
        };
        add("kind", kind);
        add("connection_id", connection_id);
        add("lease_id", lease_id);
        add("host", host);
        add("port", port);
        add("remote_address", "\"203.0.113.7\"");
        add("interface", "\"eth0\"");
        return "{" + members + "}";
    }
    std::string reply() const { return "{\"ok\":true,\"result\":" + json() + "}\n"; }
};

UplinkTcpOptions uplink_options(std::string host = "api.example.com", std::uint16_t port = 443) {
    UplinkTcpOptions options;
    options.host = std::move(host);
    options.port = port;
    return options;
}

// Runs open_uplink_tcp and returns the exception text (empty on success; the result is closed).
std::string uplink_error(const ResourceBrokerClient& client, const UplinkTcpOptions& options) {
    try {
        auto result = client.open_uplink_tcp(options);
        result.close();
    } catch (const std::runtime_error& error) {
        return error.what();
    }
    return {};
}

void test_uplink_success_round_trips_bytes() {
    // The fake broker's default right is a connected AF_UNIX stream whose peer is the probe.
    const UplinkMetadata metadata;
    FakeBroker broker({{metadata.reply(), 1}});
    ResourceBrokerClient client("lease1", broker.path());
    BrokeredSocketResult result = client.open_uplink_tcp(uplink_options());
    require(broker.finish() == "{\"op\":\"open_uplink_tcp\",\"lease_id\":\"lease1\",\"host\":\"api.example.com\","
                               "\"port\":443,\"timeout_ms\":10000}\n", "uplink request line");
    require(result.metadata_json == metadata.json(), "uplink metadata: " + result.metadata_json);
    require((::fcntl(result.socket, F_GETFD) & FD_CLOEXEC) != 0, "uplink descriptor is close-on-exec");
    char buffer[16] = {};
    require(::send(result.socket, "request", 7, MSG_NOSIGNAL) == 7, "send through the uplink");
    require(::recv(broker.probe(), buffer, sizeof(buffer), 0) == 7 && std::string(buffer, 7) == "request",
            "the relay end receives the request");
    require(::send(broker.probe(), "response", 8, MSG_NOSIGNAL) == 8, "the relay end answers");
    require(::recv(result.socket, buffer, sizeof(buffer), 0) == 8 && std::string(buffer, 8) == "response",
            "the uplink receives the response");
    result.close();
    require(result.socket == -1 && broker.probe_closed(), "closing the uplink releases the only copy");
}

void test_uplink_request_carries_the_normalized_host() {
    struct Case { std::string host, sent, reported; };
    for (const Case& item : std::vector<Case>{{"API.Example.COM.", "api.example.com", "api.example.com"},
                                              {"203.0.113.7", "203.0.113.7", "203.0.113.7"},
                                              {"2001:DB8:0::7", "2001:db8:0::7", "2001:db8:0::7"},
                                              {"2001:DB8:0::7", "2001:db8:0::7", "2001:db8::7"}}) {
        UplinkMetadata metadata;
        metadata.host = "\"" + item.reported + "\"";
        metadata.port = "8001";
        FakeBroker broker({{metadata.reply(), 1}});
        ResourceBrokerClient client("lease1", broker.path());
        UplinkTcpOptions options = uplink_options(item.host, 8001);
        options.timeout = std::chrono::milliseconds(2500);
        const std::string error = uplink_error(client, options);
        const std::string request = broker.finish();
        require(error.empty(), "normalized host " + item.host + ": " + error);
        require(request.find("\"host\":\"" + item.sent + "\",\"port\":8001,\"timeout_ms\":2500}") != std::string::npos,
                "normalized host request: " + request);
    }
}

void test_uplink_metadata_mismatch_closes_the_descriptor() {
    std::vector<std::pair<std::string, UplinkMetadata>> cases;
    const auto with = [&](const std::string& name, const std::function<void(UplinkMetadata&)>& change) {
        UplinkMetadata metadata;
        change(metadata);
        cases.emplace_back(name, metadata);
    };
    with("kind", [](UplinkMetadata& m) { m.kind = "\"approved_tcp_stream\""; });
    with("kind null", [](UplinkMetadata& m) { m.kind = "null"; });
    with("kind missing", [](UplinkMetadata& m) { m.kind.clear(); });
    with("lease foreign", [](UplinkMetadata& m) { m.lease_id = "\"foreign\""; });
    with("lease null", [](UplinkMetadata& m) { m.lease_id = "null"; });
    with("lease missing", [](UplinkMetadata& m) { m.lease_id.clear(); });
    with("connection null", [](UplinkMetadata& m) { m.connection_id = "null"; });
    with("connection empty", [](UplinkMetadata& m) { m.connection_id = "\"\""; });
    with("connection uppercase", [](UplinkMetadata& m) { m.connection_id = "\"" + std::string(32, 'A') + "\""; });
    with("connection short", [](UplinkMetadata& m) { m.connection_id = "\"" + std::string(31, '1') + "\""; });
    with("connection long", [](UplinkMetadata& m) { m.connection_id = "\"" + std::string(33, '1') + "\""; });
    with("connection not hex", [](UplinkMetadata& m) { m.connection_id = "\"" + std::string(32, 'g') + "\""; });
    with("connection number", [](UplinkMetadata& m) { m.connection_id = "123"; });
    with("connection missing", [](UplinkMetadata& m) { m.connection_id.clear(); });
    with("port other", [](UplinkMetadata& m) { m.port = "444"; });
    with("port string", [](UplinkMetadata& m) { m.port = "\"443\""; });
    with("port fraction", [](UplinkMetadata& m) { m.port = "443.0"; });
    with("port exponent", [](UplinkMetadata& m) { m.port = "4.43e2"; });
    with("port bool", [](UplinkMetadata& m) { m.port = "true"; });
    with("port null", [](UplinkMetadata& m) { m.port = "null"; });
    with("port missing", [](UplinkMetadata& m) { m.port.clear(); });
    with("host other", [](UplinkMetadata& m) { m.host = "\"other.example.com\""; });
    with("host not normalized", [](UplinkMetadata& m) { m.host = "\"API.example.com\""; });
    with("host trailing dot", [](UplinkMetadata& m) { m.host = "\"api.example.com.\""; });
    with("host address", [](UplinkMetadata& m) { m.host = "\"203.0.113.7\""; });
    with("host null", [](UplinkMetadata& m) { m.host = "null"; });
    with("host number", [](UplinkMetadata& m) { m.host = "1"; });
    with("host missing", [](UplinkMetadata& m) { m.host.clear(); });
    for (const auto& [name, metadata] : cases) {
        FakeBroker broker({{metadata.reply(), 1}});
        ResourceBrokerClient client("lease1", broker.path());
        const std::string error = uplink_error(client, uplink_options());
        (void)broker.finish();
        require(error.rfind("Invalid app uplink response: metadata", 0) == 0, "metadata " + name + ": " + error);
        require(broker.probe_closed(), "metadata " + name + " closes the received descriptor");
    }
}

void test_uplink_descriptor_identity_is_checked() {
    const auto count = [] {
        return std::distance(std::filesystem::directory_iterator("/proc/self/fd"), std::filesystem::directory_iterator());
    };
    const int inet = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    const int inet_listener = frontend_listener(AF_INET);
    sockaddr_in address {};
    socklen_t length = sizeof(address);
    require(::getsockname(inet_listener, reinterpret_cast<sockaddr*>(&address), &length) == 0, "inet listener address");
    const int inet_connected = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    require(inet_connected >= 0 && ::connect(inet_connected, reinterpret_cast<sockaddr*>(&address), length) == 0,
            "connected inet fixture");
    const int unix_listener = frontend_listener(AF_UNIX);
    const int unconnected = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int datagram[2] = {-1, -1};
    require(::socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, datagram) == 0, "datagram fixture");
    const int file = ::open("/dev/null", O_RDONLY | O_CLOEXEC);
    require(inet >= 0 && unconnected >= 0 && file >= 0, "descriptor fixtures");
    for (int fd : {inet, inet_connected, unix_listener, unconnected, datagram[0], file}) {
        const auto before = count();
        {
            FakeBroker broker({{UplinkMetadata().reply(), 1}}, fd);
            ResourceBrokerClient client("lease1", broker.path());
            const std::string error = uplink_error(client, uplink_options());
            (void)broker.finish();
            require(error.rfind("Invalid app uplink response: the descriptor", 0) == 0,
                    "descriptor " + std::to_string(fd) + ": " + error);
        }
        require(count() == before, "a refused uplink descriptor is closed");
    }
    for (int fd : {inet, inet_connected, inet_listener, unix_listener, unconnected, datagram[0], datagram[1], file})
        ::close(fd);
}

void test_uplink_refusals_carry_the_code() {
    for (const std::string code : {"invalid_request", "uplink_role_refused", "app_uplink_off", "uplink_unavailable",
                                   "destination_not_allowed", "destination_blocked", "resolve_failed",
                                   "connect_timeout", "connect_failed", "uplink_limit", "not_found"}) {
        FakeBroker broker({{"{\"ok\":false,\"result\":null,\"error_code\":\"" + code + "\",\"error\":\"refused: "
                            + code + "\"}\n", 1}});
        ResourceBrokerClient client("lease1", broker.path());
        std::string caught;
        try {
            (void)client.open_uplink_tcp(uplink_options());
        } catch (const std::runtime_error& error) {
            // Caught as std::runtime_error across the library boundary, then identified by its exported type.
            const auto* refused = dynamic_cast<const ResourceBrokerError*>(&error);
            require(refused != nullptr, "a refusal is a ResourceBrokerError: " + std::string(error.what()));
            caught = refused->code() + "|" + error.what();
        }
        (void)broker.finish();
        require(caught == code + "|refused: " + code, "refusal code and text: " + caught);
        require(broker.probe_closed(), "a refusal closes the descriptor that came with it");
    }
    for (const auto& [reply, text] : std::vector<std::pair<std::string, std::string>>{
            {"{\"ok\":false,\"error\":\"denied\"}\n", "denied"},
            {"{\"ok\":false,\"error_code\":7,\"error\":\"denied\"}\n", "denied"},
            {"{\"ok\":false}\n", "R2Kernel resource request failed"}}) {
        FakeBroker broker({{reply, 0}});
        ResourceBrokerClient client("lease1", broker.path());
        std::string caught = "none";
        try { (void)client.open_uplink_tcp(uplink_options()); }
        catch (const ResourceBrokerError& refused) { caught = refused.code() + "|" + refused.what(); }
        (void)broker.finish();
        require(caught == "|" + text, "refusal without a usable code: " + caught);
    }
    FakeBroker broker({{"{\"ok\":false,\"result\":null,\"error_code\":\"policy\",\"error\":\"denied by policy\"}\n", 0}});
    ResourceBrokerClient client("lease1", broker.path());
    std::string caught;
    try { (void)client.open_device("/dev/null"); }
    catch (const ResourceBrokerError& refused) { caught = refused.code() + "|" + refused.what(); }
    (void)broker.finish();
    require(caught == "policy|denied by policy", "other operations carry the code: " + caught);
}

void test_uplink_reply_budget_is_the_timeout_plus_two_seconds() {
    using std::chrono::milliseconds;
    UplinkTcpOptions options = uplink_options();
    options.timeout = milliseconds(1);
    {
        // After the 1 ms connect timeout, but within its 2 s margin: accepted.
        FakeBroker broker({{UplinkMetadata().reply(), 1}}, -1, milliseconds(1200));
        ResourceBrokerClient client("lease1", broker.path());
        const std::string error = uplink_error(client, options);
        require(error.empty(), "a reply within the margin is accepted: " + error);
        require(broker.finish().find("\"timeout_ms\":1}") != std::string::npos, "timeout_ms is sent");
    }
    {
        // Later than the 2.001 s budget, though within the generic 5 s: refused at the budget.
        FakeBroker broker({{UplinkMetadata().reply(), 1}}, -1, milliseconds(3500));
        ResourceBrokerClient client("lease1", broker.path());
        const auto started = std::chrono::steady_clock::now();
        const std::string error = uplink_error(client, options);
        const auto elapsed = std::chrono::steady_clock::now() - started;
        (void)broker.finish();
        require(error == "R2Kernel resource request deadline exceeded", "a late reply times out: " + error);
        require(elapsed >= milliseconds(1900) && elapsed < milliseconds(3400), "the budget is timeout + 2 s");
        require(broker.probe_closed(), "a late reply's descriptor is never adopted");
    }
}

void test_uplink_invalid_options_are_refused_before_any_request() {
    const ResourceBrokerClient client("lease1", "/nonexistent/r2kernel-uplink-broker.sock");
    const auto refused = [&](const UplinkTcpOptions& options, const std::string& name) {
        try {
            (void)client.open_uplink_tcp(options);
        } catch (const std::invalid_argument&) {
            return;
        } catch (const std::exception& error) {
            throw std::runtime_error(name + " reached the broker: " + error.what());
        }
        throw std::runtime_error(name + " was accepted");
    };
    for (const std::string& host : std::vector<std::string>{
            "", std::string(254, 'a'), "exa mple.com", "under_score.example", "[2001:db8::1]", "fe80::1%eth0",
            "2001:db8::zz", "a..b", ".", "..", "example.com..", ".example.com", "h\xc3\xa9llo.example",
            std::string("api\0x", 5)})
        refused(uplink_options(host), "host '" + host + "'");
    UplinkTcpOptions options = uplink_options();
    options.port = 0;
    refused(options, "port 0");
    for (const auto timeout : {std::chrono::milliseconds(0), std::chrono::milliseconds(-1),
                               std::chrono::milliseconds(120001)}) {
        options = uplink_options();
        options.timeout = timeout;
        refused(options, "timeout " + std::to_string(timeout.count()));
    }
    for (const auto& [timeout, sent] : std::vector<std::pair<std::chrono::milliseconds, std::string>>{
            {std::chrono::milliseconds(1), "1"}, {std::chrono::milliseconds(120000), "120000"}}) {
        FakeBroker broker({{"{\"ok\":false,\"error_code\":\"invalid_request\",\"error\":\"x\"}\n", 0}});
        options = uplink_options(std::string(126, 'a') + "." + std::string(126, 'b'));  // 253 bytes
        options.timeout = timeout;
        (void)uplink_error(ResourceBrokerClient("lease1", broker.path()), options);
        require(broker.finish().find("\"timeout_ms\":" + sent + "}") != std::string::npos, "timeout bound " + sent);
    }
}

void test_client_exposes_the_uplink() {
    // R2KernelClient needs a running Control; its pass-through builds the environment client tested above.
    BrokeredSocketResult (r2kernel::core::R2KernelClient::*method)(const UplinkTcpOptions&) const =
        &r2kernel::core::R2KernelClient::open_uplink_tcp;
    require(method != nullptr, "R2KernelClient::open_uplink_tcp is exported");
}

}  // namespace

int main() {
    try {
        test_fragmented_success_transfers_one_usable_noninheritable_fd(0);
        test_fragmented_success_transfers_one_usable_noninheritable_fd(1);
        test_rejections();
        test_refusal_text_and_request_shapes();
        test_environment_lease_and_socket();
        test_uplink_success_round_trips_bytes();
        test_uplink_request_carries_the_normalized_host();
        test_uplink_metadata_mismatch_closes_the_descriptor();
        test_uplink_descriptor_identity_is_checked();
        test_uplink_refusals_carry_the_code();
        test_uplink_reply_budget_is_the_timeout_plus_two_seconds();
        test_uplink_invalid_options_are_refused_before_any_request();
        test_client_exposes_the_uplink();
        test_frontend_rejects_metadata_and_nonlistener_rights();
        test_frontend_rejects_tcp_datagram_and_file_descriptors();
    } catch (const std::exception& error) {
        std::cerr << "resource_broker_test failed: " << error.what() << '\n';
        return 1;
    }
    std::cout << "resource_broker_test passed\n";
    return 0;
}
