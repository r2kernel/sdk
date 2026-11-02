#pragma once

// Deterministic msg.actuator.drive.v1 payloads shared by the C++ drive
// fixtures; ../python/drive_payload.py builds the same values. Request n
// channels for sequence seq:
//   channels[i] = i, mode[i] = (seq + i) & 0xff,
//   target[i] = seq + 0.25 * i, feedforward[i] = 0.5 * i.
// The gateway answers channels, position = target, velocity = feedforward and
// enabled[i] = (seq + i) % 2 == 0; the optional effort and fault_code fields
// are omitted.

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <optional>
#include <sstream>
#include <string>

namespace drive_fixture {

inline constexpr const char* kContract = "api.actuator.drive.v1";
inline constexpr const char* kTopic = "drive_command";
inline constexpr const char* kRequestType = "msg.actuator.drive.v1/DriveCommand";
inline constexpr const char* kResponseType = "msg.actuator.drive.v1/DriveState";
// Request data bytes per channel: uint32 channel + uint8 mode + two float64 values.
inline constexpr std::size_t kRequestBytesPerChannel = 21;
// seq must stay exactly representable as float64 target[0].
inline constexpr std::uint64_t kMaxSequence = std::uint64_t {1} << 53;

inline std::size_t channels_for_payload(std::int64_t payload_size) {
    const auto channels = payload_size > 0 ? static_cast<std::size_t>(payload_size) / kRequestBytesPerChannel : 0;
    return channels > 0 ? channels : 1;
}

// float64 values keep a decimal point so the codec never sees an integer literal.
inline std::string json_double(double value) {
    std::ostringstream out;
    out << std::setprecision(17) << value;
    auto text = out.str();
    if (text.find_first_of(".eEn") == std::string::npos) text += ".0";
    return text;
}

template <typename Element>
std::string json_array(std::size_t channels, Element element) {
    std::string out = "[";
    for (std::size_t index = 0; index < channels; ++index) {
        if (index) out += ',';
        out += element(index);
    }
    return out + "]";
}

inline std::string command_json(std::uint64_t seq, std::size_t channels) {
    return "{\"channels\":" + json_array(channels, [](std::size_t i) { return std::to_string(i); })
        + ",\"mode\":" + json_array(channels, [seq](std::size_t i) { return std::to_string((seq + i) & 0xffU); })
        + ",\"target\":" + json_array(channels, [seq](std::size_t i) {
              return json_double(static_cast<double>(seq) + 0.25 * static_cast<double>(i)); })
        + ",\"feedforward\":" + json_array(channels, [](std::size_t i) {
              return json_double(0.5 * static_cast<double>(i)); })
        + "}";
}

inline std::string state_json(std::uint64_t seq, std::size_t channels) {
    return "{\"channels\":" + json_array(channels, [](std::size_t i) { return std::to_string(i); })
        + ",\"position\":" + json_array(channels, [seq](std::size_t i) {
              return json_double(static_cast<double>(seq) + 0.25 * static_cast<double>(i)); })
        + ",\"velocity\":" + json_array(channels, [](std::size_t i) {
              return json_double(0.5 * static_cast<double>(i)); })
        + ",\"enabled\":" + json_array(channels, [seq](std::size_t i) {
              return std::string((seq + i) % 2 == 0 ? "true" : "false"); })
        + "}";
}

// Body between the brackets of the flat array stored under key, if present.
inline std::optional<std::string> array_body(const std::string& json, const std::string& key) {
    const auto name = json.find("\"" + key + "\"");
    if (name == std::string::npos) return std::nullopt;
    const auto open = json.find('[', name);
    const auto close = open == std::string::npos ? std::string::npos : json.find(']', open);
    if (close == std::string::npos) return std::nullopt;
    return json.substr(open + 1, close - open - 1);
}

inline std::size_t array_length(const std::string& body) {
    if (body.find_first_not_of(" \t\r\n") == std::string::npos) return 0;
    std::size_t commas = 0;
    for (const char c : body) commas += c == ',' ? 1 : 0;
    return commas + 1;
}

struct CommandShape {
    std::uint64_t seq = 0;
    std::size_t channels = 0;
};

// The (seq, channels) a decoded DriveCommand claims; the caller confirms the
// whole request by comparing its bytes with the encoding of command_json.
inline std::optional<CommandShape> command_shape(const std::string& json) {
    const auto channels = array_body(json, "channels");
    const auto target = array_body(json, "target");
    if (!channels || !target) return std::nullopt;
    const auto length = array_length(*channels);
    if (length == 0) return std::nullopt;
    char* end = nullptr;
    const double first = std::strtod(target->c_str(), &end);
    if (end == target->c_str() || !std::isfinite(first) || first < 0.0 || first != std::floor(first)
        || first >= static_cast<double>(kMaxSequence)) {
        return std::nullopt;
    }
    return CommandShape {static_cast<std::uint64_t>(first), length};
}

}  // namespace drive_fixture
