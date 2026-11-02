#pragma once

// Single C++ SDK source of the control socket location: the environment
// override and the packaged default path. Used by
// core/client.cpp and wrapper/subsystem.cpp. Namespace-scope constexpr has
// internal linkage, so no symbol is exported.

namespace r2kernel::sdk_environment {

constexpr char kControlSocketEnv[] = "R2KERNEL_CONTROL_SOCKET";
constexpr char kDefaultControlSocketPath[] = "/run/r2kernel/control.sock";

// Upper bound of one fire-and-forget control-socket notification (heartbeat,
// frame transform, subsystem-down). Bounds the calling thread when the backend
// stalls.
constexpr int kBestEffortControlTimeoutMs = 100;

}  // namespace r2kernel::sdk_environment
