#include "r2kernel/wrapper/logger.hpp"
#include "logged_problem_capture.hpp"

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <utility>

namespace r2kernel::wrapper {

namespace detail {
namespace {
thread_local LoggedProblemCapture* active_capture = nullptr;
}  // namespace

class LoggedProblemSink {
public:
    static void record(std::string_view tag, std::string_view message) {
        if (active_capture == nullptr) return;
        active_capture->last_.assign(tag.data(), tag.size());
        active_capture->last_ += ": ";
        active_capture->last_.append(message.data(), message.size());
    }
};

LoggedProblemCapture::LoggedProblemCapture() : previous_(active_capture) { active_capture = this; }
LoggedProblemCapture::~LoggedProblemCapture() { active_capture = previous_; }
std::string LoggedProblemCapture::false_hook_message(const char* label) const {
    // A false return carries no reason; name the last problem the SDK (or the
    // hook through this logger) logged on this thread while the hook ran.
    return with_last_problem(std::string(label) + " failed: " + label + " hook returned false (no exception)");
}
std::string LoggedProblemCapture::with_last_problem(std::string message) const {
    if (!last_.empty()) message += "; last logged problem: " + last_;
    return message;
}
}  // namespace detail

namespace {

std::mutex& log_mutex() {
    static std::mutex instance;
    return instance;
}

std::string upper_ascii(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (char ch : text) {
        if (ch >= 'a' && ch <= 'z') {
            out.push_back(static_cast<char>(ch - ('a' - 'A')));
        } else {
            out.push_back(ch);
        }
    }
    return out;
}

std::string normalize_level(std::string_view level) {
    const std::string normalized = upper_ascii(level);
    if (normalized == "TRACE") {
        return "TRACE";
    }
    if (normalized == "DEBUG") {
        return "DEBUG";
    }
    if (normalized == "INFO") {
        return "INFO";
    }
    if (normalized == "WARN" || normalized == "WARNING") {
        return "WARN";
    }
    if (normalized == "ERROR") {
        return "ERROR";
    }
    if (normalized == "FATAL" || normalized == "CRITICAL") {
        return "FATAL";
    }
    return "INFO";
}

int level_rank(std::string_view level) {
    const std::string normalized = normalize_level(level);
    if (normalized == "TRACE") {
        return 0;
    }
    if (normalized == "DEBUG") {
        return 1;
    }
    if (normalized == "INFO") {
        return 2;
    }
    if (normalized == "WARN") {
        return 3;
    }
    if (normalized == "ERROR") {
        return 4;
    }
    return 5;
}

void default_sink(std::string_view level,
                  std::string_view logger_name,
                  std::string_view tag,
                  std::string_view message) {
    const auto now = std::chrono::system_clock::now();
    const std::time_t now_c = std::chrono::system_clock::to_time_t(now);

    std::tm tm {};
#if defined(_WIN32)
    localtime_s(&tm, &now_c);
#else
    localtime_r(&now_c, &tm);
#endif

    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S")
        << " [" << level << "] "
        << logger_name << ": "
        << "[" << tag << "] "
        << message;

    std::lock_guard<std::mutex> guard(log_mutex());
    std::cerr << oss.str() << std::endl;
}

}  // namespace

Logger::Logger(std::string name, Sink sink)
    : Logger(std::move(name), "INFO", std::move(sink)) {}

Logger::Logger(std::string name, std::string level, Sink sink)
    : name_(std::move(name)),
      sink_(sink ? std::move(sink) : Sink(default_sink)) {
    set_level(level);
}

void Logger::set_level(std::string_view level) {
    level_ = normalize_level(level);
    min_level_rank_ = level_rank(level_);
}

void Logger::debug(std::string_view tag, std::string_view message) const {
    emit("DEBUG", tag, message);
}

void Logger::info(std::string_view tag, std::string_view message) const {
    emit("INFO", tag, message);
}

void Logger::warning(std::string_view tag, std::string_view message) const {
    emit("WARN", tag, message);
}

void Logger::error(std::string_view tag, std::string_view message) const {
    emit("ERROR", tag, message);
}

void Logger::emit(std::string_view level, std::string_view tag, std::string_view message) const {
    const std::string normalized = normalize_level(level);
    if (normalized == "WARN" || normalized == "ERROR") {
        detail::LoggedProblemSink::record(tag, message);
    }
    if (level_rank(normalized) < min_level_rank_) {
        return;
    }
    sink_(normalized, name_.empty() ? "r2kernel.wrapper" : std::string_view(name_), tag, message);
}

}  // namespace r2kernel::wrapper
