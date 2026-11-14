#pragma once

// Internal interface between client.cpp and control_requests.cpp (cold
// subsystem-owned control requests). Not exported (hidden by default).

#include "r2kernel/wrapper/core_client.hpp"
#include "r2kernel_comm.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace r2kernel::core {
struct ClientOptions;
namespace detail {

// One authenticated control-socket JSON exchange bounded by deadline
// (defined in client.cpp; server identity is verified as for other requests).
std::string exchange_control_request(const std::string& request,
    std::optional<std::chrono::steady_clock::time_point> deadline);

std::string request_self_binding_set(const ClientOptions& options, const std::string& requirement,
    const std::string& target, std::optional<std::chrono::steady_clock::time_point> deadline);
bool request_self_binding_unset(const ClientOptions& options,
    const std::string& requirement, std::optional<std::chrono::steady_clock::time_point> deadline);

// Control's resolve_control_target reply as a ControlRoute, or
// std::nullopt when the reply is not ok or names no host/port.
std::optional<r2kernel::wrapper::ControlRoute> parse_control_route(const std::string& response);

// R2KernelError::BufferTooSmall of the Rust core (core/src/error.rs).
constexpr int kCoreBufferTooSmall = -4;

// Branch hint for rare paths taken from per-message loops.
#if defined(__GNUC__)
#define R2KERNEL_SDK_UNLIKELY(condition) __builtin_expect(!!(condition), 0)
#else
#define R2KERNEL_SDK_UNLIKELY(condition) (condition)
#endif

// Topic, publisher and payload buffer sizes of the native Event receive loop.
struct EventReceiveSizes {
    std::size_t topic = 0;
    std::size_t publisher = 0;
    std::size_t payload = 0;
};

// Topic and publisher name buffer size of the native Event receive loop.
constexpr std::size_t kEventReceiveNameCapacity = 256;

// Buffer sizes for the single retry after kCoreBufferTooSmall (Core kept the
// Event and reported every required length): each buffer sized to its
// reported length once, never shrunk; no growth loop. std::nullopt with an
// explicit error on stderr when no reported length exceeds its buffer.
std::optional<EventReceiveSizes> grown_event_receive_sizes(EventReceiveSizes capacity,
    EventReceiveSizes reported);

// Topic and publisher buffers of the oversized-Event retry. The receive loop
// owns them across Events; only receive_oversized_event grows them (never
// shrinks), so a later oversized name reuses them without allocating.
struct EventRetryNameBuffers {
    std::vector<std::uint8_t> topic;
    std::vector<std::uint8_t> publisher;
};

// Cold path of the native Event receive loop (client.cpp recv_loop) after
// kCoreBufferTooSmall: grows the loop's own payload_buffer in place to the
// reported payload length (the loop keeps it, so later Events of that size
// fit on the first receive), receives the kept Event once more and hands it
// to its subscription callbacks, as the loop does. Topic and publisher names
// are received into names: the loop's fixed 256-byte name buffers are not
// grown, which keeps its per-message receive call unchanged. Lives outside
// client.cpp so the loop and the rest of that translation unit compile as
// before; an explicit error on stderr when the retry fails.
void receive_oversized_event(R2KernelEventClient* client, std::mutex& mutex,
    std::unordered_map<std::string, r2kernel::wrapper::EventSubscribeSpec>& subscriptions,
    std::vector<std::uint8_t>& payload_buffer, EventRetryNameBuffers& names,
    std::size_t topic_len, std::size_t publisher_len, std::size_t payload_len);

// Why an Event inline endpoint cannot be used (it does not fit
// sockaddr_un::sun_path), or an empty string.
std::string event_inline_endpoint_refusal(const std::string& endpoint);

// Registration-time check of the backend's Event inline endpoint: one that is
// refused disables inline delivery for this registration (empty result, the
// reason on stderr) instead of failing later inside a publish, after the slot
// is already marked ready.
std::string registered_event_inline_endpoint(const std::string& endpoint);

// Cold path of publish(), called once per registration: an Event inline
// publish whose connection failed with an exception (for example a refused
// Control peer verification) after the slot was marked ready. Logs on stderr
// that inline delivery is disabled for the endpoint until the subsystem
// registers again; Events are still delivered through the
// stamped_stream_runtime lock notification.
void report_event_inline_refusal(const std::string& endpoint, const std::string& topic,
    const char* reason);

}  // namespace detail
}  // namespace r2kernel::core
