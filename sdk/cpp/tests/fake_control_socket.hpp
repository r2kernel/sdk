#pragma once

// Test-only fake R2Kernel control socket: serves one connection at a time,
// records each request and answers with a configurable reply, or holds the
// next connection open without reading it (stall) to exercise deadlines, or
// refuses connections as Control's admission limits do (reply, close unread).
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace r2kernel_test {

inline std::filesystem::path unique_temp_path(const std::string& tag) {
    return std::filesystem::temp_directory_path() / ("r2kernel-cpp-test-" + tag + "-" + std::to_string(::getpid()) + "-" +
        std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
}

inline void fake_require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}

// Sets R2KERNEL_CONTROL_SOCKET to its own path for the process.
class FakeControl {
public:
    FakeControl() : path_(unique_temp_path("control").string() + ".sock") {
        fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
        fake_require(fd_ >= 0, "fake control socket");
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, path_.c_str(), path_.size() + 1);
        fake_require(::bind(fd_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "fake control bind");
        fake_require(::listen(fd_, 8) == 0, "fake control listen");
        ::setenv("R2KERNEL_CONTROL_SOCKET", path_.c_str(), 1);
        thread_ = std::thread([this] { serve(); });
    }
    ~FakeControl() {
        stop_ = true;
        ::shutdown(fd_, SHUT_RDWR);
        ::close(fd_);
        thread_.join();
        ::unlink(path_.c_str());
    }
    void respond(std::function<std::string(const std::string&)> reply) {
        std::lock_guard<std::mutex> lock(mutex_);
        reply_ = std::move(reply);
        stall_ms_ = 0;
    }
    // Holds the next connection open for milliseconds without reading it.
    void stall_next(int milliseconds) {
        std::lock_guard<std::mutex> lock(mutex_);
        stall_ms_ = milliseconds;
    }
    // Refuses the next count connections like Control over its admission limits:
    // writes reply and closes without reading the request.
    void refuse_next(int count, std::string reply) {
        std::lock_guard<std::mutex> lock(mutex_);
        refusals_ = count;
        refusal_ = std::move(reply);
    }
    int refused() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return refused_;
    }
    void wait_stall_finished() {
        std::unique_lock<std::mutex> lock(mutex_);
        stall_done_.wait(lock, [this] { return stall_ms_ == 0; });
    }
    std::vector<std::string> requests() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return requests_;
    }

private:
    void serve() {
        while (!stop_) {
            const int client = ::accept(fd_, nullptr, nullptr);
            if (client < 0) return;
            int stall_ms;
            std::function<std::string(const std::string&)> reply;
            std::string refusal;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                stall_ms = stall_ms_;
                reply = reply_;
                if (refusals_ > 0) {
                    --refusals_;
                    ++refused_;
                    refusal = refusal_;
                }
            }
            if (!refusal.empty()) {
                (void)::send(client, refusal.data(), refusal.size(), MSG_NOSIGNAL);
                ::close(client);
                continue;
            }
            if (stall_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(stall_ms));
                ::close(client);
                std::lock_guard<std::mutex> lock(mutex_);
                stall_ms_ = 0;
                stall_done_.notify_all();
                continue;
            }
            std::string request;
            char buffer[4096];
            for (;;) {
                const auto count = ::recv(client, buffer, sizeof(buffer), 0);
                if (count <= 0) break;
                request.append(buffer, static_cast<std::size_t>(count));
            }
            {
                std::lock_guard<std::mutex> lock(mutex_);
                requests_.push_back(request);
            }
            const std::string body = reply ? reply(request) : std::string("{\"ok\":true,\"res\":\"ok\"}");
            (void)::send(client, body.data(), body.size(), MSG_NOSIGNAL);
            ::close(client);
        }
    }
    std::string path_;
    int fd_ = -1;
    std::atomic<bool> stop_{false};
    std::thread thread_;
    mutable std::mutex mutex_;
    std::function<std::string(const std::string&)> reply_;
    int stall_ms_ = 0;
    int refusals_ = 0;
    int refused_ = 0;
    std::string refusal_;
    std::condition_variable stall_done_;
    std::vector<std::string> requests_;
};

}  // namespace r2kernel_test
