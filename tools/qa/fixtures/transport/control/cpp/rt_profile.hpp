#pragma once

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <optional>
#include <sched.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace fixture_rt {

inline std::int64_t monotonic_ns() {
    timespec value {};
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) throw std::runtime_error("clock_gettime failed");
    return static_cast<std::int64_t>(value.tv_sec) * 1'000'000'000 + value.tv_nsec;
}

inline void sleep_until(std::int64_t ns) {
    const timespec target {static_cast<time_t>(ns / 1'000'000'000), static_cast<long>(ns % 1'000'000'000)};
    int error;
    do { error = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, nullptr); } while (error == EINTR);
    if (error != 0) throw std::runtime_error(std::string("clock_nanosleep: ") + std::strerror(error));
}

inline std::string quote(const std::string& value) {
    std::string out = "\"";
    for (const unsigned char c : value) {
        if (c == '"' || c == '\\') { out += '\\'; out += static_cast<char>(c); }
        else if (c < 32) {
            const char* hex = "0123456789abcdef";
            out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15];
        } else out += static_cast<char>(c);
    }
    return out + '"';
}

inline const char* policy_name(int policy) {
    return policy == SCHED_FIFO ? "fifo" : policy == SCHED_RR ? "rr" : policy == SCHED_OTHER ? "other" : "unknown";
}

inline std::vector<int> allowed_cpus() {
    cpu_set_t mask;
    CPU_ZERO(&mask);
    if (sched_getaffinity(0, sizeof(mask), &mask) != 0) throw std::runtime_error("sched_getaffinity failed");
    std::vector<int> result;
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) if (CPU_ISSET(cpu, &mask)) result.push_back(cpu);
    return result;
}

inline std::vector<int> cpu_list(const std::string& value, bool ranges) {
    std::vector<int> result;
    if (value.empty()) return result;
    std::istringstream input(value);
    std::string token;
    while (std::getline(input, token, ',')) {
        std::size_t consumed = 0;
        const int first = std::stoi(token, &consumed);
        int last = first;
        if (ranges && consumed < token.size() && token[consumed] == '-') {
            std::size_t end = 0;
            last = std::stoi(token.substr(consumed + 1), &end);
            consumed += end + 1;
        }
        if (consumed != token.size() || first < 0 || last < first || last >= CPU_SETSIZE)
            throw std::runtime_error("invalid CPU list");
        for (int cpu = first; cpu <= last; ++cpu) result.push_back(cpu);
    }
    if (value.back() == ',') throw std::runtime_error("invalid CPU list");
    std::sort(result.begin(), result.end());
    if (std::adjacent_find(result.begin(), result.end()) != result.end()) throw std::runtime_error("duplicate CPUs");
    return result;
}

inline std::string cpus_json(const std::vector<int>& cpus) {
    std::ostringstream out;
    out << '[';
    for (std::size_t i = 0; i < cpus.size(); ++i) { if (i) out << ','; out << cpus[i]; }
    out << ']';
    return out.str();
}

inline std::string isolation_evidence(const std::vector<int>& cpus, bool required) {
    if (!required) return "null";
    std::ifstream input("/sys/devices/system/cpu/isolated");
    if (!input) throw std::runtime_error("cannot read preexisting CPU isolation");
    std::string value;
    std::getline(input, value);
    const auto isolated = cpu_list(value, true);
    if (cpus.empty() || !std::includes(isolated.begin(), isolated.end(), cpus.begin(), cpus.end()))
        throw std::runtime_error("selected CPUs are not all preexisting isolated CPUs");
    return "{\"path\":\"/sys/devices/system/cpu/isolated\",\"mask\":" + quote(value) + ",\"verified\":true}";
}

struct Options {
    bool enabled = false;
    int policy = SCHED_OTHER;
    int priority = 0;
    std::vector<int> cpus;
    bool require_isolated = false;
    bool prepare_local = false;
    int preparation_timeout_ms = 1000;
    std::optional<std::int64_t> measurement_ns;
    std::filesystem::path samples_path;
    std::filesystem::path worker_ready_path;
};

inline Options parse(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        const auto get = [&]() -> std::string {
            if (++i >= argc) throw std::runtime_error(argument + " requires a value");
            return argv[i];
        };
        if (argument == "--rt-policy") {
            options.enabled = true;
            const auto value = get();
            if (value == "other") options.policy = SCHED_OTHER;
            else if (value == "fifo") options.policy = SCHED_FIFO;
            else if (value == "rr") options.policy = SCHED_RR;
            else throw std::runtime_error("rt-policy must be other, fifo or rr");
        } else if (argument == "--rt-priority") {
            options.enabled = true;
            const auto value = get();
            std::size_t end = 0;
            options.priority = std::stoi(value, &end);
            if (end != value.size()) throw std::runtime_error("invalid rt-priority");
        } else if (argument == "--rt-cpus") {
            options.enabled = true;
            const auto value = get();
            if (value.empty()) throw std::runtime_error("rt-cpus must not be empty");
            options.cpus = cpu_list(value, false);
        } else if (argument == "--rt-require-isolated") {
            options.enabled = true; options.require_isolated = true;
        } else if (argument == "--prepare-local") options.prepare_local = true;
        else if (argument == "--preparation-timeout-ms") {
            const auto value = get();
            std::size_t end = 0;
            options.preparation_timeout_ms = std::stoi(value, &end);
            if (end != value.size() || options.preparation_timeout_ms <= 0) throw std::runtime_error("invalid preparation-timeout-ms");
        } else if (argument == "--measurement-seconds") {
            const auto value = get();
            std::size_t end = 0;
            const double seconds = std::stod(value, &end);
            if (end != value.size() || !std::isfinite(seconds) || seconds < 1 || seconds > 30)
                throw std::runtime_error("measurement-seconds must be finite and between 1 and 30");
            options.measurement_ns = static_cast<std::int64_t>(seconds * 1'000'000'000);
        } else if (argument == "--samples-path") options.samples_path = get();
        else if (argument == "--worker-ready-path") options.worker_ready_path = get();
    }
    const auto allowed = allowed_cpus();
    if (options.cpus.empty()) options.cpus = allowed;
    if (!std::includes(allowed.begin(), allowed.end(), options.cpus.begin(), options.cpus.end()))
        throw std::runtime_error("rt-cpus outside current allowed CPU mask");
    if ((options.policy == SCHED_OTHER && options.priority != 0)
        || (options.policy != SCHED_OTHER && (options.priority < 1 || options.priority > 99)))
        throw std::runtime_error("rt-priority must be 0 for other and 1..99 for fifo/rr");
    isolation_evidence(options.cpus, options.require_isolated);
    return options;
}

inline std::string profile_json(std::int64_t tid, int policy, int priority, const std::vector<int>& cpus,
                                bool ok, const std::string& error, bool isolated, bool explicit_profile) {
    return "{\"tid\":" + std::to_string(tid) + ",\"policy\":" + std::to_string(policy)
        + ",\"policy_name\":" + quote(policy_name(policy)) + ",\"priority\":" + std::to_string(priority)
        + ",\"cpus\":" + cpus_json(cpus) + ",\"ok\":" + (ok ? "true" : "false")
        + ",\"explicit\":" + (explicit_profile ? "true" : "false")
        + ",\"error\":" + quote(error) + ",\"isolation_evidence\":" + isolation_evidence(cpus, isolated) + "}";
}

inline std::string apply(const Options& options) {
    if (options.enabled) {
        cpu_set_t mask;
        CPU_ZERO(&mask);
        for (const auto cpu : options.cpus) CPU_SET(cpu, &mask);
        if (sched_setaffinity(0, sizeof(mask), &mask) != 0)
            throw std::runtime_error(std::string("sched_setaffinity: ") + std::strerror(errno));
        sched_param parameter {}; parameter.sched_priority = options.priority;
        if (sched_setscheduler(0, options.policy, &parameter) != 0)
            throw std::runtime_error(std::string("sched_setscheduler: ") + std::strerror(errno));
    }
    const int policy = sched_getscheduler(0);
    sched_param parameter {};
    if (policy < 0 || sched_getparam(0, &parameter) != 0) throw std::runtime_error("scheduler readback failed");
    const auto cpus = allowed_cpus();
    if (options.enabled && (policy != options.policy || parameter.sched_priority != options.priority || cpus != options.cpus))
        throw std::runtime_error("thread RT profile readback mismatch");
    return profile_json(syscall(SYS_gettid), policy, parameter.sched_priority, cpus, true, "",
                        options.require_isolated, options.enabled);
}

inline void write_json(const std::filesystem::path& path, const std::string& content) {
    if (path.empty()) return;
    if (!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
    auto temporary = path; temporary += ".tmp";
    std::ofstream out(temporary);
    out << content << '\n';
    out.close();
    if (!out) throw std::runtime_error("failed to write " + path.string());
    std::filesystem::rename(temporary, path);
}

inline std::optional<std::int64_t> gate_start(const std::filesystem::path& path) {
    if (path.empty()) return std::nullopt;
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot read start gate");
    const std::string content((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    const auto key = content.find("\"common_start_monotonic_ns\"");
    if (key == std::string::npos) return std::nullopt;
    const auto colon = content.find(':', key);
    if (colon == std::string::npos) throw std::runtime_error("invalid common monotonic start gate");
    const auto first = content.find_first_not_of(" \t\r\n", colon + 1);
    if (first == std::string::npos || content[first] < '0' || content[first] > '9')
        throw std::runtime_error("invalid common monotonic start gate");
    std::size_t end = 0;
    const auto value = std::stoll(content.substr(first), &end);
    const auto following = content.find_first_not_of(" \t\r\n", first + end);
    if (value <= 0 || following == std::string::npos || (content[following] != ',' && content[following] != '}'))
        throw std::runtime_error("invalid common monotonic start gate");
    return value;
}

inline std::string with_periodic(std::string report, const std::string& periodic) {
    const auto close = report.rfind("}\n}");
    if (close == std::string::npos) throw std::runtime_error("unexpected fixture JSON report layout");
    report.insert(close, ", \"periodic\": " + periodic);
    return report;
}

}  // namespace fixture_rt
