// The fire-and-forget control-socket notification has one total deadline:
// a backend that accepts late and never replies, or reads a large body slowly
// and never replies, must not hold the caller past the bound (plus kernel
// timer granularity). Each call previously got the full timeout, so these
// scenarios took about twice the bound.
#include "../src/wrapper/best_effort_notification.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

using Clock = std::chrono::steady_clock;
using r2kernel::wrapper::detail::send_json_best_effort;

constexpr int kBoundMs = 100;
// Socket timeouts are rounded up to scheduler ticks (up to 4 ms at HZ=250)
// and the thread must be scheduled again after each wakeup.
constexpr double kAllowedOvershootMs = 25.0;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

class Listener {
public:
    Listener(const std::string& path, int backlog) : path_(path) {
        ::unlink(path_.c_str());
        fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        require(fd_ >= 0, "listener socket");
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        require(path_.size() < sizeof(address.sun_path), "listener path too long");
        std::snprintf(address.sun_path, sizeof(address.sun_path), "%s", path_.c_str());
        require(::bind(fd_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0, "bind");
        require(::listen(fd_, backlog) == 0, "listen");
    }
    ~Listener() {
        ::close(fd_);
        ::unlink(path_.c_str());
    }
    int fd() const { return fd_; }
    const std::string& path() const { return path_; }

    // Non-blocking client connections that occupy the accept queue.
    void fill_backlog() {
        for (int i = 0; i < 8; ++i) {
            const int client = ::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
            sockaddr_un address {};
            address.sun_family = AF_UNIX;
            std::snprintf(address.sun_path, sizeof(address.sun_path), "%s", path_.c_str());
            if (::connect(client, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
                ::close(client);
                break;
            }
            fillers_.push_back(client);
        }
        require(!fillers_.empty(), "could not occupy the accept queue");
    }
    void close_fillers() {
        for (const int client : fillers_) ::close(client);
        fillers_.clear();
    }

private:
    std::string path_;
    int fd_ = -1;
    std::vector<int> fillers_;
};

struct Outcome {
    double elapsed_ms = 0;
    std::optional<std::string> failure;
};

Outcome timed_send(const std::string& path, const std::string& body) {
    const auto started = Clock::now();
    Outcome outcome;
    outcome.failure = send_json_best_effort(path, body, kBoundMs);
    outcome.elapsed_ms = std::chrono::duration<double, std::milli>(Clock::now() - started).count();
    return outcome;
}

void require_bounded(const char* scenario, const Outcome& outcome) {
    std::printf("%s: %.1f ms, %s\n", scenario, outcome.elapsed_ms,
                outcome.failure ? outcome.failure->c_str() : "delivered");
    require(outcome.failure.has_value(), std::string(scenario) + ": a backend that never replies cannot count as delivered");
    require(outcome.elapsed_ms <= kBoundMs + kAllowedOvershootMs,
            std::string(scenario) + ": exceeded the " + std::to_string(kBoundMs) + " ms total bound: "
                + std::to_string(outcome.elapsed_ms) + " ms");
}

// The backend accepts only after most of the budget is spent and never
// replies: connect waits, then the reply wait gets only what is left.
void late_accept_then_no_reply(const std::string& directory) {
    Listener listener(directory + "/late-accept.sock", 0);
    listener.fill_backlog();
    require(::fcntl(listener.fd(), F_SETFL, ::fcntl(listener.fd(), F_GETFL) | O_NONBLOCK) == 0, "non-blocking listener");
    std::atomic<bool> done {false};
    std::thread backend([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(90));
        std::vector<int> held;
        while (!done.load()) {
            const int connection = ::accept4(listener.fd(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (connection >= 0) held.push_back(connection);
            else std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        for (const int connection : held) ::close(connection);
    });
    const auto outcome = timed_send(listener.path(), "{\"cmd\":\"heartbeat\"}");
    done.store(true);
    backend.join();
    listener.close_fillers();
    require_bounded("late accept, then no reply", outcome);
}

// The backend accepts at once, reads a large body only after most of the
// budget is spent and never replies: the remaining sends and the reply wait
// share what is left of the one deadline.
void large_body_slow_reader(const std::string& directory) {
    Listener listener(directory + "/slow-reader.sock", 4);
    std::atomic<bool> done {false};
    std::thread backend([&] {
        const int connection = ::accept4(listener.fd(), nullptr, nullptr, SOCK_CLOEXEC);
        if (connection < 0) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(90));
        std::vector<char> buffer(1 << 16);
        while (!done.load()) {
            if (::recv(connection, buffer.data(), buffer.size(), MSG_DONTWAIT) <= 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        }
        ::close(connection);
    });
    const std::string body(1024 * 1024, 'x');
    const auto outcome = timed_send(listener.path(), body);
    done.store(true);
    backend.join();
    require_bounded("large body, slow reader", outcome);
}

// A backend that reads the request and closes is reported as delivered.
void prompt_backend_is_delivered(const std::string& directory) {
    Listener listener(directory + "/prompt.sock", 4);
    std::thread backend([&] {
        const int connection = ::accept4(listener.fd(), nullptr, nullptr, SOCK_CLOEXEC);
        if (connection < 0) return;
        char buffer[4096];
        while (::recv(connection, buffer, sizeof(buffer), 0) > 0) {
        }
        ::close(connection);
    });
    const auto outcome = timed_send(listener.path(), std::string(256 * 1024, 'y'));
    backend.join();
    std::printf("prompt backend: %.1f ms, %s\n", outcome.elapsed_ms,
                outcome.failure ? outcome.failure->c_str() : "delivered");
    require(!outcome.failure.has_value(), "prompt backend must count as delivered");
}

}  // namespace

int main() {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    char directory[] = "/tmp/r2kernel-best-effort-XXXXXX";
    if (::mkdtemp(directory) == nullptr) {
        std::perror("mkdtemp");
        return 1;
    }
    int status = 0;
    try {
        late_accept_then_no_reply(directory);
        large_body_slow_reader(directory);
        prompt_backend_is_delivered(directory);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL: %s\n", error.what());
        status = 1;
    }
    ::rmdir(directory);
    return status;
}
