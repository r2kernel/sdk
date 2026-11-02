#pragma once

// Brokered sockets a package leader passes to its own child processes. The
// leader encodes {kind: fd} into R2KERNEL_INHERITED_FDS and keeps those
// descriptors open across exec (for example with posix_spawn file actions);
// the child validates each descriptor before using it. There is no broker
// fallback: every mismatch is an InheritedFdError.

#include <map>
#include <optional>
#include <stdexcept>
#include <string>

#include "r2kernel/export.hpp"

namespace r2kernel {

// Environment variable carrying the JSON object {"<kind>": <fd>}.
constexpr char kInheritedFdsEnv[] = "R2KERNEL_INHERITED_FDS";

// step() is one of: env_missing, env_invalid, kind_missing, not_socket,
// family_mismatch, type_mismatch, listening_mismatch, port_mismatch.
// what() is "inherited fd <kind>: <step>: <detail>".
class R2KERNEL_CPP_API InheritedFdError : public std::runtime_error {
public:
    InheritedFdError(std::string step, std::string kind, const std::string& detail);
    ~InheritedFdError() override;
    const std::string& step() const noexcept { return step_; }
    const std::string& kind() const noexcept { return kind_; }

private:
    std::string step_;
    std::string kind_;
};

enum class InheritedSocketFamily { inet, inet6, can };
enum class InheritedSocketType { stream, dgram, raw };

struct InheritedSocketExpectation {
    InheritedSocketFamily family = InheritedSocketFamily::inet;
    InheritedSocketType type = InheritedSocketType::stream;
    // Must be false unless type is stream.
    bool listening = false;
    // Local port to require (inet/inet6 only).
    std::optional<int> port;
};

// Validates the inherited descriptor named kind against expect, in the order
// env_missing, env_invalid, kind_missing, not_socket, family_mismatch,
// type_mismatch, listening_mismatch, port_mismatch. On success it sets
// FD_CLOEXEC and returns the descriptor (ownership passes to the caller). On
// failure the descriptor is neither closed nor modified. Every call validates
// again. An expectation that is itself inconsistent throws
// std::invalid_argument.
R2KERNEL_CPP_API int inherited_socket(const std::string& kind, const InheritedSocketExpectation& expect);

// Leader side: validates kinds ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}), descriptors
// (>= 3, no descriptor named twice) and returns the R2KERNEL_INHERITED_FDS
// value. It does not spawn and does not change descriptor flags; failures throw
// InheritedFdError with step env_invalid.
R2KERNEL_CPP_API std::string encode_inherited_fds(const std::map<std::string, int>& fds);

}  // namespace r2kernel
