#pragma once

// Records the last warning/error the SDK logs on the current thread while a
// capture is active, so a lifecycle hook that returns false without an
// exception can be reported together with the last problem logged during it.
// Defined in logger.cpp; not exported (hidden by default).

#include <string>
#include <string_view>

namespace r2kernel::wrapper::detail {

class LoggedProblemCapture final {
public:
    LoggedProblemCapture();
    ~LoggedProblemCapture();
    LoggedProblemCapture(const LoggedProblemCapture&) = delete;
    LoggedProblemCapture& operator=(const LoggedProblemCapture&) = delete;
    // "<tag>: <message>" of the last WARN/ERROR record, or empty.
    const std::string& last() const noexcept { return last_; }
    // "<label> failed: <label> hook returned false (no exception)" plus
    // "; last logged problem: <last()>" when one was recorded.
    std::string false_hook_message(const char* label) const;
    // message plus "; last logged problem: <last()>" when one was recorded.
    std::string with_last_problem(std::string message) const;

private:
    friend class LoggedProblemSink;
    std::string last_;
    LoggedProblemCapture* previous_ = nullptr;
};

}  // namespace r2kernel::wrapper::detail
