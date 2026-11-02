#pragma once

#include <yaml-cpp/yaml.h>
#include <fstream>
#include <algorithm>
#include <cstdlib>
#include <sstream>
#include <stdexcept>
#include <string>
#include <chrono>
#include <cerrno>
#include <limits>
#if defined(__linux__)
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#endif

namespace r2kernel::core::detail {
inline bool managed_peer_authentication_required() {
    const auto raw=std::getenv("R2KERNEL_RESOURCE_SERVICE_MODE");
    const std::string mode=raw?raw:"required";
    if(mode!="required" && mode!="auto" && mode!="disabled") throw std::runtime_error("Invalid R2KERNEL_RESOURCE_SERVICE_MODE");
    return mode!="disabled";
}
inline std::string configured_resource_broker_socket() {
    const auto raw=std::getenv("R2KERNEL_RESOURCE_SERVICE_SOCKET");
    return raw && *raw ? raw : "/run/r2kernel/resourced.sock";
}
inline void verify_manager_peer_connection(int connection, const std::string& broker_path,
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1)) {
#if defined(__linux__)
    struct ucred candidate{};
    socklen_t size = sizeof(candidate);
    if (::getsockopt(connection, SOL_SOCKET, SO_PEERCRED, &candidate, &size) != 0 || size != sizeof(candidate) || candidate.pid <= 0)
        throw std::runtime_error("Managed handler Unix peer credentials are unavailable");
    std::ifstream process("/proc/" + std::to_string(candidate.pid) + "/stat");
    std::string stat; std::getline(process, stat);
    const auto end = stat.rfind(')');
    if (!process || end == std::string::npos) throw std::runtime_error("Managed handler peer birth identity is unavailable");
    std::istringstream fields(stat.substr(end + 1));
    std::string field;
    for (int index = 0; index <= 19; ++index) if (!(fields >> field)) throw std::runtime_error("Managed handler peer birth identity is malformed");
    std::size_t consumed = 0;
    const auto ticks = std::stoull(field, &consumed);
    if (consumed != field.size() || broker_path.empty() || broker_path.size() >= sizeof(sockaddr_un::sun_path))
        throw std::runtime_error("Managed handler root broker policy is unavailable");
    const int descriptor = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (descriptor < 0) throw std::runtime_error("Managed handler root broker socket creation failed");
    struct Descriptor { int value; ~Descriptor(){::close(value);} } owned{descriptor};
    const auto check_deadline = [&] {
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error("Managed handler peer proof deadline exceeded");
    };
    const auto wait = [&](short events) {
        for (;;) {
            check_deadline();
            const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (remaining <= 0) { check_deadline(); continue; }
            timespec timeout{static_cast<time_t>(remaining / 1000000000),
                             static_cast<long>(remaining % 1000000000)};
            pollfd socket{descriptor, events, 0};
            const int result = ::ppoll(&socket, 1, &timeout, nullptr);
            if (result < 0 && errno == EINTR) continue;
            check_deadline();
            if (result == 0) continue;
            if (result < 0 || (socket.revents & (POLLERR | POLLNVAL)))
                throw std::runtime_error("Managed handler root broker I/O failed");
            if (socket.revents & (events | POLLHUP)) return;
        }
    };
    sockaddr_un address{}; address.sun_family=AF_UNIX;
    std::copy(broker_path.begin(),broker_path.end(),address.sun_path);
    check_deadline();
    if (::connect(descriptor,reinterpret_cast<sockaddr*>(&address),sizeof(address)) != 0) {
        if (errno != EINPROGRESS)
            throw std::runtime_error("Managed handler cannot contact the configured root broker");
        wait(POLLOUT);
        int error = 0; socklen_t length = sizeof(error);
        if (::getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0)
            throw std::runtime_error("Managed handler cannot contact the configured root broker");
    }
    check_deadline();
    struct ucred broker{}; size=sizeof(broker);
    if (::getsockopt(descriptor,SOL_SOCKET,SO_PEERCRED,&broker,&size)!=0 || size!=sizeof(broker) || broker.uid!=0)
        throw std::runtime_error("Managed handler peer proof requires a root broker process");
    const std::string request="{\"op\":\"verify_control_manager_peer\",\"pid\":"+std::to_string(candidate.pid)+",\"process_start_ticks\":"+std::to_string(ticks)+"}\n";
    std::size_t sent=0;
    while(sent<request.size()) {
        check_deadline();
        const auto count=::send(descriptor,request.data()+sent,request.size()-sent,MSG_NOSIGNAL | MSG_DONTWAIT);
        if(count<0 && errno==EINTR) continue;
        if(count<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) { wait(POLLOUT); continue; }
        if(count<=0) throw std::runtime_error("Managed handler root broker request failed");
        sent+=static_cast<std::size_t>(count);
        check_deadline();
    }
    ::shutdown(descriptor,SHUT_WR);
    std::string response; char buffer[1024];
    while(true) {
        check_deadline();
        const auto count=::recv(descriptor,buffer,sizeof(buffer),MSG_DONTWAIT);
        if(count<0 && errno==EINTR) continue;
        if(count<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) { wait(POLLIN); continue; }
        if(count<0) throw std::runtime_error("Managed handler root broker response failed");
        check_deadline();
        if(count==0) break;
        response.append(buffer,static_cast<std::size_t>(count));
        if(response.size()>8192) throw std::runtime_error("Managed handler peer proof exceeds its response limit");
    }
    const auto proof=YAML::Load(response);
    if(!proof["ok"] || !proof["ok"].as<bool>() || !proof["result"] || !proof["result"]["authorized"] || !proof["result"]["authorized"].as<bool>())
        throw std::runtime_error("Managed handler rejected a peer without backend manager authority");
    check_deadline();
#else
    (void)connection; (void)broker_path; (void)deadline;
    throw std::runtime_error("Managed handler peer authentication requires Linux Unix credentials");
#endif
}
inline void verify_control_server_connection(int connection,
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1)) {
    if(managed_peer_authentication_required())
        verify_manager_peer_connection(connection,configured_resource_broker_socket(),deadline);
}
}  // namespace r2kernel::core::detail
