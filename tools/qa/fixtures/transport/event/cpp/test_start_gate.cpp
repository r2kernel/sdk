// StartGate v1 conformance: runs the shared scenario table given as argv[1]
// (../start_gate_conformance.json; CTest target r2kernel_tutorial_start_gate_test
// in the Tutorial/root QA build passes it) against the C++ fixture gate.
#include "start_gate.hpp"

#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include <sys/wait.h>

namespace fs = std::filesystem;

namespace {

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

// Minimal reader for the conformance table: objects, arrays, strings without
// escapes, and booleans. Anything else is a malformed table.
struct Value {
    std::string text;
    bool boolean = false;
    std::vector<Value> items;
    std::map<std::string, Value> members;
};

class TableReader {
public:
    explicit TableReader(std::string text) : text_(std::move(text)) {}
    Value read() {
        Value value = parse();
        skip();
        require(pos_ == text_.size(), "conformance table: trailing data");
        return value;
    }

private:
    void skip() { while (pos_ < text_.size() && std::isspace(static_cast<unsigned char>(text_[pos_]))) ++pos_; }
    bool take(char ch) {
        skip();
        if (pos_ < text_.size() && text_[pos_] == ch) { ++pos_; return true; }
        return false;
    }
    std::string string() {
        require(take('"'), "conformance table: expected a string");
        const auto end = text_.find('"', pos_);
        require(end != std::string::npos, "conformance table: unterminated string");
        std::string out = text_.substr(pos_, end - pos_);
        require(out.find('\\') == std::string::npos, "conformance table: escapes are not supported");
        pos_ = end + 1;
        return out;
    }
    Value parse() {
        skip();
        Value value;
        if (take('{')) {
            if (take('}')) return value;
            do {
                std::string key = string();
                require(take(':'), "conformance table: expected ':'");
                value.members[key] = parse();
            } while (take(','));
            require(take('}'), "conformance table: expected '}'");
        } else if (take('[')) {
            if (take(']')) return value;
            do value.items.push_back(parse()); while (take(','));
            require(take(']'), "conformance table: expected ']'");
        } else if (pos_ < text_.size() && text_[pos_] == '"') {
            value.text = string();
        } else if (text_.compare(pos_, 4, "true") == 0) {
            pos_ += 4; value.boolean = true;
        } else if (text_.compare(pos_, 5, "false") == 0) {
            pos_ += 5;
        } else {
            throw std::runtime_error("conformance table: unsupported value");
        }
        return value;
    }
    std::string text_;
    std::size_t pos_ = 0;
};

void make_private_dir(const fs::path& path) {
    fs::create_directory(path);
    require(::chmod(path.c_str(), 0700) == 0, "chmod run directory");
}

bool ack_lock_free(const fs::path& ack) {
    const int fd = ::open(ack.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    require(fd >= 0, "open acknowledgement");
    const bool free = ::flock(fd, LOCK_EX | LOCK_NB) == 0;
    ::close(fd);
    return free;
}

// Runs acknowledged_start_gate_or_exit in a child process; true when the child
// exited with kStartGateRefusedExitStatus and stderr starts with expected.
bool refused_startup_exit(const std::string& path, const std::string& session, const char* role,
                          const std::string& expected) {
    int pipe_fds[2];
    require(::pipe(pipe_fds) == 0, "pipe");
    const pid_t child = ::fork();
    require(child >= 0, "fork");
    if (child == 0) {
        ::dup2(pipe_fds[1], STDERR_FILENO);
        ::close(pipe_fds[0]);
        ::close(pipe_fds[1]);
        (void)acknowledged_start_gate_or_exit(path, session, "publisher", role);
        std::_Exit(0);
    }
    ::close(pipe_fds[1]);
    std::string output;
    char buffer[512];
    for (ssize_t count; (count = ::read(pipe_fds[0], buffer, sizeof(buffer))) > 0;) output.append(buffer, count);
    ::close(pipe_fds[0]);
    int status = 0;
    require(::waitpid(child, &status, 0) == child, "waitpid");
    return WIFEXITED(status) && WEXITSTATUS(status) == kStartGateRefusedExitStatus && output.rfind(expected, 0) == 0;
}

// Runs one scenario in a fresh run directory below root; returns the error
// code observed ("ok" on success, "skipped" when it needs root).
std::string run_scenario(const fs::path& root, const Value& scenario) {
    const auto member = [&](const char* key) -> const Value* {
        const auto found = scenario.members.find(key);
        return found == scenario.members.end() ? nullptr : &found->second;
    };
    if (const auto* root_only = member("requires_root"); root_only && root_only->boolean && ::geteuid() != 0) return "skipped";
    const fs::path base = root / member("name")->text;
    make_private_dir(base);
    fs::path run = base / "run";
    make_private_dir(run);
    std::string gate = (run / "ready").string();
    std::optional<StartGate> start_gate;
    const auto constructed = [&]() -> StartGate& {
        require(start_gate.has_value(), "scenario step needs a constructed gate");
        return *start_gate;
    };
    const auto replace_run_directory = [&] {
        fs::rename(run, base / "old-run");
        make_private_dir(run);
    };
    try {
        for (const auto& item : member("setup")->items) {
            const std::string action = item.text;
            if (action == "construct") start_gate.emplace(gate);
            else if (action == "acknowledge") constructed().acknowledge("current-run", "publisher");
            else if (action == "create_gate") {
                std::ofstream(run / "ready").close();
                require(::chmod((run / "ready").c_str(), 0600) == 0, "chmod gate");
            } else if (action == "create_ack") std::ofstream(run / "ready.ack").close();
            else if (action == "create_ack_symlink") fs::create_symlink(base / "elsewhere", run / "ready.ack");
            else if (action == "replace_run_directory") replace_run_directory();
            else if (action == "replace_run_directory_after_ack_create") constructed().set_after_ack_create_for_test(replace_run_directory);
            else if (action == "symlink_parent") {
                fs::create_directory_symlink(run, base / "linked-run");
                gate = (base / "linked-run" / "ready").string();
            } else if (action == "chmod_parent_0750") require(::chmod(run.c_str(), 0750) == 0, "chmod 0750");
            else if (action == "chown_parent_foreign") require(::chown(run.c_str(), 65534, 65534) == 0, "chown");
            else if (action == "relative_gate_path") gate = "relative/ready";
            else if (action == "dot_component_gate_path") gate = run.string() + "/./ready";
            else if (action == "double_slash_gate_path") gate = base.string() + "//run/ready";
            else if (action == "trailing_slash_gate_path") gate = run.string() + "/ready/";
            else if (action == "release_publisher") start_gate.reset();
            else if (action == "close_publisher") { StartGate taken(std::move(constructed())); }
            else if (action.rfind("gate_kind:", 0) == 0) {
                const std::string kind = action.substr(10);
                const fs::path path = run / "ready";
                if (kind == "symlink") fs::create_symlink(base / "missing", path);
                else if (kind == "directory") fs::create_directory(path);
                else if (kind == "fifo") require(::mkfifo(path.c_str(), 0600) == 0, "mkfifo");
                else {
                    std::ofstream file(path);
                    if (kind == "content") file << "stale";
                    file.close();
                    require(::chmod(path.c_str(), kind == "public" ? 0644 : 0600) == 0, "chmod gate kind");
                    if (kind == "hardlink") fs::create_hard_link(path, run / "other");
                }
            } else throw std::runtime_error("unknown setup action " + action);
        }
        const std::string step = member("step")->text;
        if (step == "construct") start_gate.emplace(gate);
        else if (step == "acknowledge") constructed().acknowledge("current-run", "publisher");
        else if (step == "acknowledge_invalid_session") constructed().acknowledge("bad/session", "publisher");
        else if (step == "acknowledge_invalid_publisher") constructed().acknowledge("current-run", "publisher name");
        else if (step == "opened") (void)constructed().opened();
        else if (step == "ack_lock_free") require(ack_lock_free(run / "ready.ack"), "exited publisher kept the lock");
        else if (step == "acknowledge_then_open") {
            constructed().acknowledge("current-run", "publisher");
            std::ifstream ack(run / "ready.ack");
            const std::string content((std::istreambuf_iterator<char>(ack)), std::istreambuf_iterator<char>());
            require(content == "r2kernel-event-start-v1\ncurrent-run\npublisher\n", "acknowledgement content");
            struct stat metadata {};
            require(::lstat((run / "ready.ack").c_str(), &metadata) == 0 && (metadata.st_mode & 07777) == 0600
                && metadata.st_nlink == 1, "acknowledgement mode");
            require(!ack_lock_free(run / "ready.ack"), "live publisher must hold the acknowledgement lock");
            require(!constructed().opened(), "gate must start closed");
            const int fd = ::open((run / "ready").c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
            require(fd >= 0, "create gate");
            ::close(fd);
            StartGate moved(std::move(constructed()));
            require(moved.opened(), "created gate must open");
        } else throw std::runtime_error("unknown step " + step);
    } catch (const StartGateError& error) {
        return error.code();
    }
    return "ok";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: " << (argc > 0 ? argv[0] : "test_start_gate") << " <start_gate_conformance.json>\n";
        return 2;
    }
    const fs::path table_path(argv[1]);
    try {
        std::ifstream input(table_path);
        require(bool(input), "conformance table not found: " + table_path.string());
        const std::string text((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
        const Value table = TableReader(text).read();
        char directory[] = "/tmp/r2kernel-gate-test-XXXXXX";
        require(::mkdtemp(directory) != nullptr, "mkdtemp");
        const fs::path root(directory);
        int failures = 0, skipped = 0, passed = 0;
        for (const auto& scenario : table.members.at("scenarios").items) {
            const auto consumer = scenario.members.find("consumer_only");
            if (consumer != scenario.members.end() && consumer->second.boolean) { ++skipped; continue; }
            const std::string expected = scenario.members.at("expect").text;
            const std::string observed = run_scenario(root, scenario);
            if (observed == "skipped") { ++skipped; continue; }
            if (observed != expected) {
                ++failures;
                std::cerr << "FAIL " << scenario.members.at("name").text << ": expected " << expected << ", got " << observed << '\n';
            } else {
                ++passed;
            }
        }
        // A gated fixture's startup refusal exits with its contract code, never aborts.
        require(refused_startup_exit("relative/ready", "current-run", "Publisher",
                    "Publisher start gate refused [invalid_path]: "),
            "a refused start gate must exit with kStartGateRefusedExitStatus and its contract code");
        require(refused_startup_exit("", "current-run", "Ping", "Ping start gate refused [invalid_identity]: "),
            "a session without a gate path must exit with kStartGateRefusedExitStatus");
        // Disabled gate (empty path) is a no-op in the C++ fixture.
        StartGate disabled("");
        disabled.acknowledge("", "publisher");
        require(disabled.opened(), "disabled gate must report open");
        // Caller paths are never removed on refusal.
        std::error_code ignored;
        fs::permissions(root, fs::perms::owner_all, ignored);
        fs::remove_all(root, ignored);
        std::cout << "StartGate v1 conformance: " << passed << " passed, " << skipped << " skipped, " << failures << " failed\n";
        return failures == 0 ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
