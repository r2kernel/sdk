#pragma once

// Event fixture start gate, StartGate v1 contract (see the fixture README;
// the Python fixture implements the same contract in python/start_gate.py).
// Startup-only, never on a per-message path, and it never removes caller paths.

#include <cerrno>
#include <cstdlib>
#include <filesystem>
#include <fcntl.h>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

// code() is the contract error class shared with the Python fixture:
// invalid_path, parent_unsafe, directory_changed, gate_exists, gate_unsafe,
// gate_inaccessible, invalid_identity, ack_exists, ack_unsafe, ack_write_failed,
// gate_closed.
class StartGateError : public std::runtime_error {
public:
    StartGateError(std::string code, const std::string& message)
        : std::runtime_error(message), code_(std::move(code)) {}
    const std::string& code() const noexcept { return code_; }

private:
    std::string code_;
};

class StartGate {
public:
    static constexpr const char* kAckContentVersion = "r2kernel-event-start-v1";

    // An empty path disables the gate (acknowledge with an empty session and
    // opened() are then no-ops); the Python fixture expresses this as no gate.
    explicit StartGate(const std::string& path) : path_(path) {
        if (path_.empty()) return;
        const std::filesystem::path gate(path_);
        const auto name = gate.filename().string();
        if (!normal_absolute(path_) || name.empty() || name == "." || name == "..") {
            throw StartGateError("invalid_path", "start gate requires an absolute path in a fresh private run directory");
        }
        parent_ = gate.parent_path();
        name_ = name;
        ack_name_ = name + ".ack";
        const struct stat parent = check_parent();
        dir_fd_ = ::open(parent_.c_str(), O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (dir_fd_ < 0) throw StartGateError("parent_unsafe", "start gate parent must be a real owner-only directory (0700)");
        struct stat pinned {};
        if (::fstat(dir_fd_, &pinned) != 0 || pinned.st_dev != parent.st_dev || pinned.st_ino != parent.st_ino) {
            close_dir();
            throw StartGateError("directory_changed", "start gate run directory changed");
        }
        pinned_ = pinned;
        struct stat existing {};
        if (::fstatat(dir_fd_, name_.c_str(), &existing, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) {
            close_dir();
            throw StartGateError("gate_exists", "start gate already exists or is inaccessible; use a fresh private run directory");
        }
    }

    StartGate(const StartGate&) = delete;
    StartGate& operator=(const StartGate&) = delete;
    StartGate(StartGate&& other) noexcept
        : path_(std::move(other.path_)), parent_(std::move(other.parent_)), name_(std::move(other.name_)),
          ack_name_(std::move(other.ack_name_)), pinned_(other.pinned_), dir_fd_(std::exchange(other.dir_fd_, -1)),
          ack_fd_(std::exchange(other.ack_fd_, -1)), after_ack_create_(std::move(other.after_ack_create_)),
          closed_(std::exchange(other.closed_, true)) {}
    ~StartGate() {
        if (ack_fd_ >= 0) ::close(ack_fd_);
        close_dir();
    }

    // Publisher side: creates, checks and locks this run's acknowledgement.
    void acknowledge(const std::string& session, const std::string& publisher_name) {
        require_open();
        if (path_.empty() && session.empty()) return;
        if (path_.empty() || !valid_identity(session) || !valid_identity(publisher_name)) {
            throw StartGateError("invalid_identity",
                "start gate requires a unique session and publisher name (1-256 ASCII letters, digits, _, . or -)");
        }
        check_identity();
        if (gate_present()) {
            throw StartGateError("gate_exists", "start gate already exists; use a fresh private run directory");
        }
        const int fd = ::openat(dir_fd_, ack_name_.c_str(), O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd < 0) throw StartGateError("ack_exists", "start gate acknowledgement already exists or is inaccessible");
        try {
            // Locked first, before any other check or the content: a helper treats
            // the acknowledgement as live only once it has content, which follows the lock.
            if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
                throw StartGateError("ack_write_failed", "start gate acknowledgement could not be locked");
            }
            if (::fchmod(fd, 0600) != 0) {
                throw StartGateError("ack_unsafe", "start gate acknowledgement mode could not be set");
            }
            require_private_file(fd);
            write_all(fd, std::string(kAckContentVersion) + "\n" + session + "\n" + publisher_name + "\n");
            if (after_ack_create_) after_ack_create_();
            check_identity();
        } catch (...) {
            ::close(fd);
            throw;
        }
        ack_fd_ = fd;
    }

    // Publisher side: true once the helper created an empty private gate.
    bool opened() const {
        require_open();
        if (path_.empty()) return true;
        check_identity();
        struct stat metadata {};
        if (::fstatat(dir_fd_, name_.c_str(), &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
            if (errno == ENOENT) return false;
            throw StartGateError("gate_inaccessible", "start gate inaccessible");
        }
        if (!S_ISREG(metadata.st_mode) || metadata.st_uid != ::geteuid()
            || (metadata.st_mode & 07777) != 0600 || metadata.st_size != 0 || metadata.st_nlink != 1) {
            throw StartGateError("gate_unsafe", "start gate must be an empty owner-only regular file (0600), not a link");
        }
        return true;
    }

    // Test-only: runs after the acknowledgement is created and written, before
    // the final run-directory recheck.
    void set_after_ack_create_for_test(std::function<void()> hook) { after_ack_create_ = std::move(hook); }

private:
    // An absolute path without empty ("//"), "." or trailing-slash components,
    // the form Python's pathlib keeps verbatim (parity with python/start_gate.py).
    static bool normal_absolute(const std::string& path) {
        if (path.empty() || path.front() != '/') return false;
        std::size_t start = 1;
        for (;;) {
            const auto end = path.find('/', start);
            const auto component = path.substr(start, end == std::string::npos ? std::string::npos : end - start);
            if (component.empty() || component == ".") return false;
            if (end == std::string::npos) return true;
            start = end + 1;
        }
    }

    // A moved-from gate no longer owns its descriptors; using it is an error,
    // never a silent "no gate".
    void require_open() const {
        if (closed_) throw StartGateError("gate_closed", "start gate was moved or closed; use the gate that owns it");
    }

    static bool valid_identity(const std::string& value) {
        if (value.empty() || value.size() > 256) return false;
        for (const unsigned char ch : value) {
            if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')
                || (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '-')) return false;
        }
        return true;
    }

    // The parent path is a real (no symlink component) owner-only directory.
    struct stat check_parent() const {
        struct stat metadata {};
        std::error_code error;
        if (::lstat(parent_.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode)
            || metadata.st_uid != ::geteuid() || (metadata.st_mode & 07777) != 0700
            || std::filesystem::canonical(parent_, error) != parent_ || error) {
            throw StartGateError("parent_unsafe", "start gate parent must be a real owner-only directory (0700)");
        }
        return metadata;
    }

    // The parent path still resolves to the directory pinned at construction.
    void check_identity() const {
        const struct stat current = check_parent();
        if (current.st_dev != pinned_.st_dev || current.st_ino != pinned_.st_ino) {
            throw StartGateError("directory_changed", "start gate run directory changed");
        }
    }

    bool gate_present() const {
        struct stat metadata {};
        if (::fstatat(dir_fd_, name_.c_str(), &metadata, AT_SYMLINK_NOFOLLOW) == 0) return true;
        if (errno == ENOENT) return false;
        throw StartGateError("gate_inaccessible", "start gate inaccessible");
    }

    static void require_private_file(int fd) {
        struct stat metadata {};
        if (::fstat(fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_uid != ::geteuid()
            || (metadata.st_mode & 07777) != 0600 || metadata.st_nlink != 1) {
            throw StartGateError("ack_unsafe", "start gate acknowledgement must be a private regular file (0600), not a link");
        }
    }

    static void write_all(int fd, const std::string& content) {
        std::size_t offset = 0;
        while (offset < content.size()) {
            const auto written = ::write(fd, content.data() + offset, content.size() - offset);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) {
                throw StartGateError("ack_write_failed", "start gate acknowledgement could not be written safely");
            }
            offset += static_cast<std::size_t>(written);
        }
    }

    void close_dir() {
        if (dir_fd_ >= 0) ::close(dir_fd_);
        dir_fd_ = -1;
    }

    std::string path_;
    std::filesystem::path parent_;
    std::string name_;
    std::string ack_name_;
    struct stat pinned_ {};
    int dir_fd_ = -1;
    int ack_fd_ = -1;
    std::function<void()> after_ack_create_;
    bool closed_ = false;
};

// Exit status of a gated C++ fixture whose start gate refused at startup
// (before registration); 1 stays a run failure.
constexpr int kStartGateRefusedExitStatus = 3;

// Startup of a gated C++ fixture (publisher, ping): constructs the gate and
// acknowledges this run. A refusal is reported on stderr with its contract
// code ("<role> start gate refused [<code>]: <message>") and the process exits
// with kStartGateRefusedExitStatus instead of aborting on an uncaught
// exception.
inline StartGate acknowledged_start_gate_or_exit(const std::string& path, const std::string& session,
                                                 const std::string& publisher_name, const char* role) {
    try {
        StartGate gate(path);
        gate.acknowledge(session, publisher_name);
        return gate;
    } catch (const StartGateError& error) {
        std::cerr << role << " start gate refused [" << error.code() << "]: " << error.what() << std::endl;
        std::exit(kStartGateRefusedExitStatus);
    }
}
