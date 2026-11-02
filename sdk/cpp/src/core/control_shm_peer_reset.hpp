#pragma once

// Peer closure of a Control v5 lane of R2KernelClient. The socket send and
// receive primitives classify a failure where it happens, by the type they
// throw (a readiness wait that reports a pending socket error leaves it to the
// send or receive that follows). The classification is cold, implemented in
// control_shm_peer_reset.cpp so it stays out of client.cpp's per-call codegen.

#include <cerrno>
#include <exception>
#include <stdexcept>

#if defined(__GNUC__)
#define R2KERNEL_SDK_PEER_RESET_COLD __attribute__((cold, noinline))
#else
#define R2KERNEL_SDK_PEER_RESET_COLD
#endif

namespace r2kernel::core::detail {

// Control closed the lane cleanly before the next expected frame.
class ControlShmPeerClosed : public std::runtime_error {
public:
    ControlShmPeerClosed() : std::runtime_error("Control v5 peer closed its connection") {}
};

// A lane socket send or receive failed, with the errno it reported (0 when
// there is none).
class ControlShmSocketError : public std::runtime_error {
public:
    ControlShmSocketError(const char* message, int error_number)
        : std::runtime_error(message), error_number_(error_number) {}
    int error_number() const noexcept { return error_number_; }
    // Control reset the lane: EPIPE or ECONNRESET.
    bool peer_reset() const noexcept { return error_number_ == EPIPE || error_number_ == ECONNRESET; }
private:
    int error_number_;
};

// Whether a failed lane exchange found the lane closed by Control: a clean
// close before a frame (ControlShmPeerClosed), or a send or receive that
// reported EPIPE or ECONNRESET (ControlShmSocketError::peer_reset). Every other
// failure, including a failure that Control reported in a response before it
// closed the lane, is not a peer closure. `socket` is not read: the parameter
// keeps the code of the per-call caller (transact_control_shm_session in
// client.cpp, which is at GCC's inlining limit) as it was.
R2KERNEL_SDK_PEER_RESET_COLD bool control_shm_peer_closed(const std::exception& error, int socket) noexcept;

}  // namespace r2kernel::core::detail
