#pragma once

namespace r2kernel::wrapper {

enum class DeviceState {
    created,
    booting,
    registering,
    disconnected,
    waiting_binding,
    waiting_target,
    ready,
    operating,
    pausing,
    paused,
    resuming,
    stopping,
    stopped,
    resetting,
    degraded,
    faulted,
    error,
};

}  // namespace r2kernel::wrapper
