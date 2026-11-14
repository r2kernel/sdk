#pragma once

// Offline CoreClient for SDK tests and benchmarks: accepts every registration,
// publishes nothing and answers each Control transaction with `response`. It
// keeps the dispatch handler the Subsystem starts and the last sent payload.
// Loopback dispatch decodes the source-wrapped topic as Control does.
#include "r2kernel/wrapper/core_client.hpp"
#include "wrapped_topic.hpp"

#include <optional>
#include <string>
#include <utility>

namespace r2kernel_test {

using namespace r2kernel::wrapper;

class OfflineCoreClient final : public CoreClient {
public:
    std::int64_t now_ns() const override { return 1; }
    bool refresh_clock_status() override { return true; }
    void start_event_transport() override {}
    void stop_event_transport() override {}
    void publish_prepared_status(const std::string&) override {}
    bool register_event_publish(const EventPublishSpec&) override { return true; }
    bool register_event_subscribe(const EventSubscribeSpec&) override { return true; }
    bool publish_event(const std::string&, const EventPayload&, std::int64_t, int) override { return true; }
    int bind_control(int port, const std::string&) override { ++bind_calls; bound_port = port; return port; }
    bool start_control_server(const ControlHandler& started) override { handler = started; return true; }
    void stop_control_server() override {}
    int get_control_port() const override { return listen_port; }
    std::optional<ControlRoute> resolve_control_target(const std::string&) override {
        ControlRoute route; route.host = "127.0.0.1"; route.port = 41001; return route;
    }
    bool connect_control(const std::string&, const std::string&, int) override { return true; }
    bool handshake_control(const std::string&, int) override { return true; }
    bool send_control(const std::string&, const std::string&, const ByteBuffer&, std::int64_t, float, float, int) override { return true; }
    std::optional<ControlResponse> recv_control(const std::string&, int) override { return ControlResponse{}; }
    void prepare_control_route(const std::string&, const std::string&, const std::string&, const std::string&,
        const std::string&, const std::string&) override {}
    std::optional<ControlResponse> transact_control(const std::string&, const std::string& topic,
        const ByteBuffer& payload, std::int64_t timestamp, float period_ms, float deadline_ms, int, int) override {
        ++transacts;
        last_payload = payload;
        const auto routed = unwrap_source_topic(topic);
        if (loopback && handler) {
            ControlRequest request;
            request.sender = routed.source;
            request.topic = routed.topic;
            request.payload = payload;
            request.timestamp_ns = timestamp;
            request.period_ms = period_ms;
            request.deadline_ms = deadline_ms;
            return handler(std::move(request));
        }
        return ControlResponse{timestamp, response_status, response};
    }
    ByteBuffer response;
    std::uint16_t response_status = 0;
    bool loopback = false;
    ControlHandler handler;
    int listen_port = 41001;
    int bind_calls = 0;
    int bound_port = -1;
    std::size_t transacts = 0;
    ByteBuffer last_payload;
};

}  // namespace r2kernel_test
