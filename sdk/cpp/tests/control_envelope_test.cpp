#include "r2kernel_core.h"
#include "r2kernel/wrapper/subsystem.hpp"
#include "../src/wrapper/control_dispatch_publication.hpp"

#include <array>
#include <atomic>
#include <condition_variable>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace {
using Bytes = std::vector<std::uint8_t>;
// The SDTR envelope is version 2 only.
enum class Kind { Request, Response };
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
void append_le(Bytes& output, std::uint64_t value, std::size_t length) {
    for (std::size_t index = 0; index < length; ++index) {
        output.push_back(static_cast<std::uint8_t>(value >> (8 * index)));
    }
}
Bytes fixture(Kind kind, const Bytes& payload) {
    const bool request = kind == Kind::Request;
    Bytes output {'S', 'D', 'T', 'R', 2,
        static_cast<std::uint8_t>(request ? 1 : 2),
        // Request: late policy reject (1), a code Core may validate; response:
        // an out-of-table final status byte carried unchanged.
        static_cast<std::uint8_t>(request ? 0x01 : 0xfd), 0,
        1, 2, 3, 4, 5, 6, 7, 8};
    Bytes times;
    if (request) {
        append_le(times, -1, 8);
        append_le(times, -2, 8);
        append_le(times, 3, 8);
        append_le(times, 4, 8);
        append_le(times, -4, 8);
    } else {
        for (const std::int64_t time : {-1, 2, -3, 4}) append_le(times, time, 8);
        append_le(times, 8, 8);
        append_le(times, -5, 8);
        append_le(times, 6, 8);
    }
    output.insert(output.end(), times.begin(), times.end());
    append_le(output, request ? 0x0605 : static_cast<std::uint32_t>(-7), 4);
    append_le(output, payload.size(), 4);
    output.insert(output.end(), payload.begin(), payload.end());
    return output;
}
int encode(Kind kind, const Bytes& payload, std::uint8_t* output,
           std::size_t capacity, std::size_t* output_len) {
    constexpr std::uint64_t request_id = 0x0807060504030201ULL;
    switch (kind) {
    case Kind::Request:
        return r2kernel_codec_encode_control_request_v2(request_id, -1, -2, 3, 4, -4,
            0x0605, 0x01, payload.data(), payload.size(), output, capacity, output_len);
    case Kind::Response:
        return r2kernel_codec_encode_control_response_v2(request_id, -1, 2, -3, 4, 8,
            -5, 6, -7, 0xfd, payload.data(), payload.size(), output, capacity, output_len);
    }
    throw std::runtime_error("unknown envelope kind");
}
int decode(Kind kind, const Bytes& wire, std::uint8_t* output,
           std::size_t capacity, std::size_t* output_len) {
    std::uint64_t request_id = 0;
    std::array<std::int64_t, 7> times {};
    std::uint16_t priority = 0;
    std::uint8_t policy = 0;
    std::int32_t status = 0;
    int result;
    switch (kind) {
    case Kind::Request:
        result = r2kernel_codec_decode_control_request_v2(wire.data(), wire.size(),
            &request_id, &times[0], &times[1], &times[2], &times[3], &times[4], &priority,
            &policy, output, capacity, output_len);
        break;
    case Kind::Response:
        result = r2kernel_codec_decode_control_response_v2(wire.data(), wire.size(),
            &request_id, &times[0], &times[1], &times[2], &times[3], &times[4], &times[5],
            &times[6], &status, &policy, output, capacity, output_len);
        break;
    default: throw std::runtime_error("unknown envelope kind");
    }
    if (result == 0) require(request_id == 0x0807060504030201ULL, "request metadata differs");
    return result;
}
void check(Kind kind, const Bytes& payload) {
    const Bytes expected = fixture(kind, payload);
    std::size_t output_len = 0;
    require(encode(kind, payload, nullptr, 0, &output_len) == 0, "C++ encode query failed");
    require(output_len == expected.size(), "C++ wire length differs");
    Bytes output(output_len);
    require(encode(kind, payload, output.data(), output.size(), &output_len) == 0,
        "C++ encode failed");
    require(output == expected, "C++ and SDTR v2 fixture bytes differ");
    require(decode(kind, expected, nullptr, 0, &output_len) == 0, "C++ decode query failed");
    require(output_len == payload.size(), "C++ payload length differs");
    Bytes restored(output_len);
    require(decode(kind, expected, restored.data(), restored.size(), &output_len) == 0,
        "C++ decode failed");
    require(restored == payload, "C++ payload differs");
}

void check_dispatch_publication() {
    struct Registration { std::function<int()> handler; };
    using Publication = r2kernel::wrapper::detail::ControlDispatchPublication<Registration, std::function<int()>>;
    Publication publication;
    std::atomic<int> destroyed{0};
    std::thread::id destructor_thread;
    struct Lifetime {
        std::atomic<int>& destroyed;
        std::thread::id& destructor_thread;
        ~Lifetime() { destructor_thread = std::this_thread::get_id(); ++destroyed; }
    };
    auto lifetime = std::shared_ptr<Lifetime>(new Lifetime{destroyed, destructor_thread});
    publication.register_endpoint("first", {[lifetime] { return 1; }});
    publication.register_endpoint("second", {[] { return 22; }});
    lifetime.reset();
    std::mutex mutex;
    std::condition_variable progress;
    bool acquired = false, released = false;
    std::atomic<bool> passed{true};
    std::thread packet([&] {
        {
            const auto previous = publication.acquire();
            {
                std::lock_guard<std::mutex> lock(mutex);
                acquired = true;
            }
            progress.notify_one();
            {
                std::unique_lock<std::mutex> lock(mutex);
                progress.wait(lock, [&] { return released; });
            }
            if (previous->endpoints.at("first").handler() != 1) passed = false;
            const auto next = publication.acquire();
            if (next->endpoints.at("first").handler() != 2
                || next->endpoints.at("second").handler() != 22) passed = false;
        }
        // The last packet reader must not destroy the retired user capture.
        if (destroyed.load() != 0) passed = false;
    });
    {
        std::unique_lock<std::mutex> lock(mutex);
        progress.wait(lock, [&] { return acquired; });
    }
    publication.register_endpoint("first", {[] { return 2; }});
    publication.collect_retired();
    const bool retained_while_active = destroyed.load() == 0;
    {
        std::lock_guard<std::mutex> lock(mutex);
        released = true;
    }
    progress.notify_one();
    packet.join();
    require(retained_while_active, "in-flight dispatch capture retired early");
    require(passed.load(), "dispatch replacement or packet retirement differs");
    publication.collect_retired();
    require(destroyed.load() == 1 && destructor_thread == std::this_thread::get_id(),
        "last dispatch capture destructor did not run on management thread");

    // Reentry takes a fresh publication without changing the outer invocation.
    publication.register_endpoint("nested", {[&publication] {
        publication.register_endpoint("nested", {[] { return 4; }});
        const auto nested = publication.acquire();
        return nested->endpoints.at("nested").handler() + 1;
    }});
    {
        const auto outer = publication.acquire();
        require(outer->endpoints.at("nested").handler() == 5, "nested dispatch reused stale handler");
    }
    publication.collect_retired();
    struct ReenterOnRelease {
        std::function<void()> release;
        ~ReenterOnRelease() { release(); }
    };
    auto reenter = std::shared_ptr<ReenterOnRelease>(new ReenterOnRelease{[&publication] {
        publication.register_endpoint("destructor_reentry", {[] { return 9; }});
    }});
    publication.register_endpoint("retiring", {[reenter] { return 8; }});
    reenter.reset();
    publication.register_endpoint("retiring", {[] { return 10; }});
    publication.collect_retired();
    require(publication.acquire()->endpoints.at("destructor_reentry").handler() == 9,
        "management destructor could not reenter registration");
    std::atomic<int> owner_destroyed{0};
    std::thread::id owner_destructor_thread;
    auto owner = std::shared_ptr<Lifetime>(new Lifetime{owner_destroyed, owner_destructor_thread});
    publication.retire_owner(owner);
    std::thread owner_reader([owner = std::move(owner)] {});
    owner_reader.join();
    require(owner_destroyed.load() == 0, "packet released opaque ACK owner last");
    publication.collect_retired();
    require(owner_destroyed.load() == 1 && owner_destructor_thread == std::this_thread::get_id(),
        "opaque ACK owner did not retire on management thread");
    publication.set_raw_handler([] { return 7; });
    const auto raw = publication.acquire();
    publication.set_raw_handler(nullptr);
    require(raw->raw_handler() == 7 && !publication.acquire()->raw_handler,
        "raw handler stop changed in-flight dispatch or remained published");
}

// Offline wrapper wiring check; this client does not test Core networking or
// provider codec negotiation. The separate runtime suite covers those paths.
class DispatchClient final : public r2kernel::wrapper::CoreClient {
public:
    using Request = r2kernel::wrapper::ControlRequest;
    using Response = r2kernel::wrapper::ControlResponse;
    using Handler = r2kernel::wrapper::ControlHandler;
    std::int64_t now_ns() const override { return 1; }
    bool refresh_clock_status() override { return true; }
    void start_event_transport() override {}
    void stop_event_transport() override {}
    void publish_prepared_status(const std::string&) override {}
    bool register_event_publish(const r2kernel::wrapper::EventPublishSpec&) override { return true; }
    bool register_event_subscribe(const r2kernel::wrapper::EventSubscribeSpec&) override { return true; }
    bool publish_event(const std::string&, const r2kernel::wrapper::EventPayload&, std::int64_t, int) override { return true; }
    int bind_control(int port, const std::string&) override { return port; }
    bool start_control_server(const Handler& handler) override { dispatch = handler; return true; }
    void stop_control_server() override { dispatch = nullptr; }
    int get_control_port() const override { return 41001; }
    std::optional<r2kernel::wrapper::ControlRoute> resolve_control_target(const std::string&) override { return r2kernel::wrapper::ControlRoute{}; }
    bool connect_control(const std::string&, const std::string&, int) override { return true; }
    bool handshake_control(const std::string&, int) override { return true; }
    bool send_control(const std::string&, const std::string&, const Bytes&, std::int64_t, float, float, int) override { return true; }
    std::optional<Response> recv_control(const std::string&, int) override { return Response{}; }
    Handler dispatch;
};

class DispatchSubsystem final : public r2kernel::wrapper::Subsystem {
public:
    using Subsystem::Subsystem;
private:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

void check_subsystem_dispatch() {
    using namespace r2kernel::wrapper;
    const auto root = std::filesystem::temp_directory_path() /
        ("r2kernel-dispatch-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    struct Cleanup { std::filesystem::path root; ~Cleanup() { std::error_code error; std::filesystem::remove_all(root, error); } } cleanup{root};
    std::filesystem::create_directories(root);
    const auto manifest = root / "subsystem.yaml";
    {
        std::ofstream output(manifest);
        output << "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: fixture.dispatch\n  role: component\n"
                  "provides:\n  local:\n    contract: fixture.dispatch.v1\n    visibility: internal\n"
                  "    endpoints:\n      controls: [first, second]\n";
        require(bool(output), "dispatch manifest fixture write failed");
    }
    auto client = std::make_shared<DispatchClient>();
    SubsystemOptions options;
    options.name = "fixture.dispatch";
    options.manifest_path = manifest;
    options.user_data_path = root;
    options.core_client = client;
    options.schema_roots = std::vector<std::string>{};
    options.log_sink = [](std::string_view, std::string_view, std::string_view, std::string_view) {};
    DispatchSubsystem subsystem(std::move(options));
    require(subsystem.start_control_server([counter = 0](ControlRequest request) mutable {
        request.payload.push_back(static_cast<std::uint8_t>(++counter));
        return ControlResponse{1, 0, std::move(request.payload)};
    }), "pre-initialize raw dispatch failed to start");
    ControlRequest request;
    request.topic = "raw";
    request.payload = {0, 255, 7};
    require(client->dispatch(request).payload == Bytes({0, 255, 7, 1})
        && client->dispatch(request).payload == Bytes({0, 255, 7, 1}),
        "pre-initialize raw mutable capture did not start afresh per request");
    subsystem.stop_control_server();
    ControlEndpointOptions endpoint_options;
    endpoint_options.request_type = " bytes ";
    endpoint_options.response_type = " bytes ";
    endpoint_options.native_payload = true;
    const auto register_value = [&](std::uint8_t value) {
        return subsystem.register_control_endpoint("first", [value](const ControlEndpointRequest& input) {
            ControlEndpointResponse response;
            response.payload = input.payload;
            response.payload.push_back(value);
            return response;
        }, endpoint_options);
    };
    require(register_value(1), "initial endpoint registration failed");
    require(subsystem.register_control_endpoint("second", [](const ControlEndpointRequest&) {
        return ControlEndpointResponse{};
    }, endpoint_options), "second declared endpoint registration failed");
    subsystem.initialize();
    require(subsystem.state() == DeviceState::ready && subsystem.start_control_server(),
        "prepared ACK discarded dispatch publication");
    request.topic = "first";
    auto original = client->dispatch(request);
    require(original.status == 0 && original.payload == Bytes({0, 255, 7, 1}), "native endpoint response differs");
    require(register_value(2), "endpoint replacement failed");
    const auto replacement = client->dispatch(request);
    require(replacement.payload == Bytes({0, 255, 7, 2}) && original.payload == Bytes({0, 255, 7, 1}),
        "replacement was stale or changed caller-owned response");
    require(subsystem.register_control_endpoint("first", [counter = 0](const ControlEndpointRequest&) mutable {
        ControlEndpointResponse response;
        response.payload = {static_cast<std::uint8_t>(++counter)};
        return response;
    }, endpoint_options), "mutable endpoint registration failed");
    require(client->dispatch(request).payload == Bytes({1}) && client->dispatch(request).payload == Bytes({1}),
        "mutable endpoint capture persisted across requests");
    std::atomic<bool> independent_lanes{true};
    const auto lane = [&] {
        for (int index = 0; index < 128; ++index)
            if (client->dispatch(request).payload != Bytes({1})) independent_lanes = false;
    };
    std::thread first_lane(lane), second_lane(lane);
    first_lane.join(); second_lane.join();
    require(independent_lanes.load(), "concurrent callbacks shared mutable by-value captures");
    std::atomic<int> nested_destroyed{0};
    std::thread::id nested_destructor_thread;
    struct NestedLifetime {
        std::atomic<int>& destroyed;
        std::thread::id& destructor_thread;
        ~NestedLifetime() { destructor_thread = std::this_thread::get_id(); ++destroyed; }
    };
    require(subsystem.register_control_endpoint("second", [&](const ControlEndpointRequest&) {
        auto lifetime = std::shared_ptr<NestedLifetime>(new NestedLifetime{nested_destroyed, nested_destructor_thread});
        require(subsystem.register_control_endpoint("first", [lifetime](const ControlEndpointRequest&) {
            return ControlEndpointResponse{};
        }, endpoint_options), "nested interim registration failed");
        lifetime.reset();
        require(register_value(3), "nested replacement failed");
        auto nested_request = request;
        nested_request.topic = "first";
        ControlEndpointResponse response;
        response.payload = client->dispatch(std::move(nested_request)).payload;
        return response;
    }, endpoint_options), "nested endpoint registration failed");
    auto nested_request = request;
    nested_request.topic = "second";
    ControlResponse nested_response;
    std::thread nested_packet([&] { nested_response = client->dispatch(nested_request); });
    const auto packet_thread = nested_packet.get_id();
    nested_packet.join();
    require(nested_response.payload == Bytes({0, 255, 7, 3}), "nested wrapper dispatch did not acquire latest registration");
    require(nested_destroyed.load() == 0 || nested_destructor_thread != packet_thread,
        "nested registration retired handler on callback thread");
    require(register_value(2), "management registration after nested callback failed");
    require(nested_destroyed.load() == 1 && nested_destructor_thread != packet_thread,
        "nested callback retirement did not return to management");
    subsystem.stop_control_server();
    subsystem.initialize();
    require(subsystem.start_control_server(), "dispatch restart after reinitialize failed");
    require(client->dispatch(request).payload == replacement.payload, "reinitialize discarded endpoint generation");
    subsystem.stop_control_server();
}
} // namespace

int main() {
    try {
        check_dispatch_publication();
        check_subsystem_dispatch();
        Bytes large(1024 * 1024);
        for (std::size_t index = 0; index < large.size(); ++index) {
            large[index] = static_cast<std::uint8_t>(index % 251);
        }
        for (const auto kind : {Kind::Request, Kind::Response}) {
            check(kind, {});
            check(kind, {0, 255, 'c', 'u', 's', 't', 'o', 'm', 0, 'o', 'p', 'a', 'q', 'u', 'e', 128});
            check(kind, large);
        }
        std::cout << "Control envelope C ABI, dispatch retirement, mutable captures and lifecycle fixture tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
