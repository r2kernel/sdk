#pragma once

// Operation feedback/result/state Event wire ("SDAE"), as the Python SDK
// encodes it (core/_runtime_operation_server.py _encode_operation_event_wire)
// and decodes it (_decode_operation_event_wire). Golden fixtures:
// sdk/testdata/wire/operation-event-*.json.
//
// Header, little-endian, 30 bytes (Python struct "<4sBBHqHHHHHI"):
//   magic "SDAE", u8 version 1, u8 kind (feedback 1, result 2, state 3),
//   u16 flags, i64 ts, u16 lengths of operation_id, operation, target,
//   payload_type and state, u32 payload length;
// then those six byte fields in that order.

#include "r2kernel/wrapper/core_client.hpp"

#include <cctype>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>
#include <vector>

namespace r2kernel::wrapper::operation_event_wire {

inline constexpr char kMagic[4] = {'S', 'D', 'A', 'E'};
inline constexpr std::uint8_t kVersion = 1;
inline constexpr std::size_t kHeaderSize = 30;

inline constexpr std::uint16_t kFlagPayload = 1U << 0U;
inline constexpr std::uint16_t kFlagJson = 1U << 1U;
inline constexpr std::uint16_t kFlagTyped = 1U << 2U;
inline constexpr std::uint16_t kFlagCancelRequested = 1U << 3U;
inline constexpr std::uint16_t kFlagState = 1U << 4U;

// How the payload bytes are written: absent, JSON text, codec-encoded message
// (payload_type names it), or bytes as given.
using PayloadEncoding = OperationEventPayloadEncoding;

struct Event {
    std::string kind;
    std::string operation_id;
    std::string operation;
    std::string target;
    std::string payload_type;
    std::string state;
    bool has_state = false;
    bool cancel_requested = false;
    std::int64_t ts = 0;
    PayloadEncoding encoding = PayloadEncoding::none;
    std::vector<std::uint8_t> payload;
};

// The fields of one event as the encoder reads them, without copies.
struct EventView {
    std::string_view kind;
    std::string_view operation_id;
    std::string_view operation;
    std::string_view target;
    std::string_view payload_type;
    std::string_view state;
    bool cancel_requested = false;
    std::int64_t ts = 0;
    PayloadEncoding encoding = PayloadEncoding::none;
    const std::uint8_t* payload = nullptr;
    std::size_t payload_size = 0;
};

inline std::uint8_t kind_code(std::string_view kind) {
    if (kind == "feedback") return 1;
    if (kind == "result") return 2;
    if (kind == "state") return 3;
    throw std::invalid_argument("unsupported Operation event kind: " + std::string(kind));
}

inline std::string kind_name(std::uint8_t code) {
    switch (code) {
    case 1: return "feedback";
    case 2: return "result";
    case 3: return "state";
    default: return "unknown";
    }
}

inline bool is_wire(const std::uint8_t* data, std::size_t size) {
    return size >= sizeof(kMagic) && std::memcmp(data, kMagic, sizeof(kMagic)) == 0;
}

inline bool is_wire(const std::vector<std::uint8_t>& payload) { return is_wire(payload.data(), payload.size()); }

namespace detail {
template <typename T>
std::uint8_t* put_le(std::uint8_t* out, T value) {
    using U = std::make_unsigned_t<T>;
    auto bits = static_cast<U>(value);
    for (std::size_t index = 0; index < sizeof(T); ++index) {
        out[index] = static_cast<std::uint8_t>(bits & 0xFFU);
        bits = static_cast<U>(bits >> 8U);
    }
    return out + sizeof(T);
}

template <typename T>
T get_le(const std::uint8_t* data) {
    using U = std::make_unsigned_t<T>;
    U bits = 0;
    for (std::size_t index = sizeof(T); index > 0; --index) {
        bits = static_cast<U>((bits << 8U) | data[index - 1]);
    }
    return static_cast<T>(bits);
}

inline std::uint16_t field_length(std::string_view name, std::size_t size) {
    if (size > std::numeric_limits<std::uint16_t>::max())
        throw std::length_error("Operation event " + std::string(name) + " exceeds 65535 bytes");
    return static_cast<std::uint16_t>(size);
}

inline std::string_view trim(std::string_view text) {
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.front()))) text.remove_prefix(1);
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.back()))) text.remove_suffix(1);
    return text;
}
}  // namespace detail

// Flags follow the Python encoder: PAYLOAD with JSON or TYPED (none for raw
// bytes), CANCEL_REQUESTED, and STATE whenever the state text is not empty.
inline std::vector<std::uint8_t> encode(const EventView& event) {
    std::uint16_t flags = 0;
    switch (event.encoding) {
    case PayloadEncoding::none: break;
    case PayloadEncoding::json: flags |= kFlagPayload | kFlagJson; break;
    case PayloadEncoding::typed: flags |= kFlagPayload | kFlagTyped; break;
    case PayloadEncoding::raw: flags |= kFlagPayload; break;
    }
    if (event.cancel_requested) flags |= kFlagCancelRequested;
    if (!event.state.empty()) flags |= kFlagState;
    const std::size_t payload_size = event.encoding == PayloadEncoding::none ? 0 : event.payload_size;
    if (payload_size > std::numeric_limits<std::uint32_t>::max())
        throw std::length_error("Operation event payload exceeds 4 GiB");
    const std::string_view fields[] = {event.operation_id, event.operation, event.target, event.payload_type, event.state};
    const char* names[] = {"operation_id", "operation", "target", "payload_type", "state"};
    std::size_t total = kHeaderSize + payload_size;
    for (const auto field : fields) total += field.size();
    std::vector<std::uint8_t> out(total);
    std::uint8_t* cursor = out.data();
    std::memcpy(cursor, kMagic, sizeof(kMagic));
    cursor += sizeof(kMagic);
    *cursor++ = kVersion;
    *cursor++ = kind_code(event.kind);
    cursor = detail::put_le<std::uint16_t>(cursor, flags);
    cursor = detail::put_le<std::int64_t>(cursor, event.ts);
    for (std::size_t index = 0; index < 5; ++index)
        cursor = detail::put_le<std::uint16_t>(cursor, detail::field_length(names[index], fields[index].size()));
    cursor = detail::put_le<std::uint32_t>(cursor, static_cast<std::uint32_t>(payload_size));
    for (const auto field : fields) {
        if (!field.empty()) std::memcpy(cursor, field.data(), field.size());
        cursor += field.size();
    }
    if (payload_size != 0) std::memcpy(cursor, event.payload, payload_size);
    return out;
}

inline std::vector<std::uint8_t> encode(const Event& event) {
    EventView view;
    view.kind = event.kind;
    view.operation_id = event.operation_id;
    view.operation = event.operation;
    view.target = event.target;
    view.payload_type = event.payload_type;
    view.state = event.state;
    view.cancel_requested = event.cancel_requested;
    view.ts = event.ts;
    view.encoding = event.encoding;
    view.payload = event.payload.data();
    view.payload_size = event.payload.size();
    return encode(view);
}

// True when JSON payload text stands for no payload (Python's None).
inline bool is_null_payload(std::string_view payload_json) {
    const auto trimmed = detail::trim(payload_json);
    return trimmed.empty() || trimmed == "null";
}

// True when a declared payload type is a message reference, which the Python
// encoder writes with the codec (TYPED). Other declared types ("string",
// "bytes", ...) leave the payload as JSON text.
inline bool is_message_type(std::string_view payload_type) {
    return payload_type.find('/') != std::string_view::npos;
}

// An event as the SDK publishers write it, following the Python encoder
// (_encode_operation_event_wire): "null" (or empty) payload JSON is no
// payload; with a declared message type the payload is the codec bytes that
// encode_typed(payload_type, payload_json) returns; otherwise the JSON text is
// written as given. The declared type, trimmed, is written as payload_type
// whatever the payload encoding.
template <typename TypedEncoder>
std::vector<std::uint8_t> encode_payload(std::string_view kind,
                                         std::string_view operation_id,
                                         std::string_view operation,
                                         std::string_view target,
                                         std::int64_t ts,
                                         std::string_view payload_type,
                                         const std::string& payload_json,
                                         TypedEncoder&& encode_typed,
                                         std::string_view state = {},
                                         bool cancel_requested = false) {
    EventView event;
    event.kind = kind;
    event.operation_id = operation_id;
    event.operation = operation;
    event.target = target;
    event.payload_type = detail::trim(payload_type);
    event.state = state;
    event.cancel_requested = cancel_requested;
    event.ts = ts;
    if (is_null_payload(payload_json)) return encode(event);
    if (is_message_type(event.payload_type)) {
        const ByteBuffer typed = encode_typed(std::string(event.payload_type), payload_json);
        event.encoding = PayloadEncoding::typed;
        event.payload = typed.data();
        event.payload_size = typed.size();
        return encode(event);
    }
    event.encoding = PayloadEncoding::json;
    event.payload = reinterpret_cast<const std::uint8_t*>(payload_json.data());
    event.payload_size = payload_json.size();
    return encode(event);
}

// An event whose payload is already encoded with its declared message type
// (the JSON-free typed path): the bytes are written as given with TYPED, so
// the event equals the one encode_payload writes for the same value. A
// declared type that is not a message reference throws std::invalid_argument:
// such a payload is JSON text, and bytes for it would be read as raw bytes.
inline std::vector<std::uint8_t> encode_typed_payload(std::string_view kind,
                                                      std::string_view operation_id,
                                                      std::string_view operation,
                                                      std::string_view target,
                                                      std::int64_t ts,
                                                      std::string_view payload_type,
                                                      const std::uint8_t* payload,
                                                      std::size_t payload_size,
                                                      std::string_view state = {},
                                                      bool cancel_requested = false) {
    EventView event;
    event.kind = kind;
    event.operation_id = operation_id;
    event.operation = operation;
    event.target = target;
    event.payload_type = detail::trim(payload_type);
    event.state = state;
    event.cancel_requested = cancel_requested;
    event.ts = ts;
    if (!is_message_type(event.payload_type))
        throw std::invalid_argument("an encoded Operation " + std::string(kind)
            + " payload needs a declared message type, not '" + std::string(event.payload_type) + "'");
    event.encoding = PayloadEncoding::typed;
    event.payload = payload;
    event.payload_size = payload_size;
    return encode(event);
}

// An event with a JSON payload and no declared type: the text is written as
// given, and "null" (or empty) means no payload, as Python's None.
inline std::vector<std::uint8_t> encode_json(std::string_view kind,
                                             std::string_view operation_id,
                                             std::string_view operation,
                                             std::string_view target,
                                             std::int64_t ts,
                                             const std::string& payload_json,
                                             std::string_view state = {},
                                             bool cancel_requested = false) {
    return encode_payload(
        kind, operation_id, operation, target, ts, {}, payload_json,
        [](const std::string&, const std::string&) -> ByteBuffer {
            throw std::logic_error("an Operation event without a declared type has no codec");
        },
        state, cancel_requested);
}

// Reads an SDAE event; a bad magic/version or a field past the end throws.
// The payload encoding follows the Python decoder: TYPED needs a payload_type,
// otherwise JSON, otherwise raw bytes.
inline Event decode(const std::uint8_t* data, std::size_t size) {
    if (size < kHeaderSize || !is_wire(data, size) || data[4] != kVersion)
        throw std::runtime_error("invalid Operation event wire header");
    Event event;
    event.kind = kind_name(data[5]);
    const auto flags = detail::get_le<std::uint16_t>(data + 6);
    event.ts = detail::get_le<std::int64_t>(data + 8);
    std::size_t lengths[6];
    for (std::size_t index = 0; index < 5; ++index)
        lengths[index] = detail::get_le<std::uint16_t>(data + 16 + 2 * index);
    lengths[5] = detail::get_le<std::uint32_t>(data + 26);
    std::size_t offset = kHeaderSize;
    const auto take = [&](std::size_t length) {
        if (length > size - offset) throw std::runtime_error("truncated Operation event wire");
        const auto* begin = data + offset;
        offset += length;
        return std::string_view(reinterpret_cast<const char*>(begin), length);
    };
    event.operation_id = std::string(take(lengths[0]));
    event.operation = std::string(take(lengths[1]));
    event.target = std::string(take(lengths[2]));
    event.payload_type = std::string(take(lengths[3]));
    const auto state = take(lengths[4]);
    const auto payload = take(lengths[5]);
    if ((flags & kFlagState) != 0) {
        event.has_state = true;
        event.state = std::string(state);
    }
    event.cancel_requested = (flags & kFlagCancelRequested) != 0;
    if ((flags & kFlagPayload) != 0) {
        if ((flags & kFlagTyped) != 0 && !event.payload_type.empty()) event.encoding = PayloadEncoding::typed;
        else if ((flags & kFlagJson) != 0) event.encoding = PayloadEncoding::json;
        else event.encoding = PayloadEncoding::raw;
        event.payload.assign(payload.begin(), payload.end());
    }
    return event;
}

inline Event decode(const std::vector<std::uint8_t>& payload) { return decode(payload.data(), payload.size()); }

inline std::string hex(const std::vector<std::uint8_t>& bytes) {
    static constexpr char kDigits[] = "0123456789abcdef";
    std::string out;
    out.reserve(bytes.size() * 2);
    for (const auto byte : bytes) {
        out.push_back(kDigits[byte >> 4U]);
        out.push_back(kDigits[byte & 0x0FU]);
    }
    return out;
}

using TypedPayloadDecoder = std::function<std::string(const std::string& payload_type, const ByteBuffer& payload)>;

// The message a C++ subscriber receives. payload_json is the JSON text as
// sent, the codec's JSON for a typed payload decoded by its declared
// payload_type (decode_typed throws when the type cannot be decoded), a JSON
// string of lowercase hex for raw bytes (as the codec writes bytes fields), or
// "null" without a payload. payload_type and payload_encoding are copied, and
// raw bytes are also kept in payload_bytes. Without payload_json, typed and raw
// payloads are kept only as bytes in payload_bytes (decode_typed is not
// called) and their payload_json is empty.
inline OperationEventMessage to_message(Event event, const TypedPayloadDecoder& decode_typed, bool payload_json = true) {
    OperationEventMessage message;
    message.operation_id = std::move(event.operation_id);
    message.operation = std::move(event.operation);
    message.target = std::move(event.target);
    message.kind = std::move(event.kind);
    message.state = std::move(event.state);
    message.cancel_requested = event.cancel_requested;
    message.timestamp_ns = event.ts;
    message.payload_encoding = event.encoding;
    if (!payload_json && (event.encoding == PayloadEncoding::typed || event.encoding == PayloadEncoding::raw)) {
        message.payload_json.clear();
        message.payload_bytes = std::move(event.payload);
        message.payload_type = std::move(event.payload_type);
        return message;
    }
    switch (event.encoding) {
    case PayloadEncoding::none: message.payload_json = "null"; break;
    case PayloadEncoding::json:
        message.payload_json = event.payload.empty() ? "null" : std::string(event.payload.begin(), event.payload.end());
        break;
    case PayloadEncoding::typed: message.payload_json = decode_typed(event.payload_type, event.payload); break;
    case PayloadEncoding::raw:
        message.payload_json = "\"" + hex(event.payload) + "\"";
        message.payload_bytes = std::move(event.payload);
        break;
    }
    message.payload_type = std::move(event.payload_type);
    return message;
}

}  // namespace r2kernel::wrapper::operation_event_wire
