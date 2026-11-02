#pragma once

// Counterparty adapters for real-socket tests, not shipping SDK completion code.
namespace r2kernel::core {
namespace {

// Control-side frame helpers of the fake Control peers (the SDK itself
// encodes SHM v5 frames in control_shm_*.hpp).
std::optional<std::uint16_t> read_u16(const std::string& data, std::size_t& pos) {
    std::uint16_t value = 0;
    if (!read_exact(data, pos, &value, sizeof(value))) {
        return std::nullopt;
    }
    return value;
}

void append_payload_lease_binary(std::string& out, const std::optional<LocalPayloadLease>& lease) {
    if (!lease.has_value()) {
        append_u8(out, 0);
        return;
    }
    append_u8(out, 1);
    append_string(out, lease->region_name);
    append_u32(out, static_cast<std::uint32_t>(lease->slot_index));
    append_u32(out, static_cast<std::uint32_t>(lease->slot_size));
    append_u32(out, static_cast<std::uint32_t>(lease->slot_count));
    append_u32(out, static_cast<std::uint32_t>(lease->length));
}

std::optional<LocalPayloadLease> read_payload_lease_binary(const std::string& data, std::size_t& pos) {
    const auto present = read_u8(data, pos);
    if (!present) {
        return std::nullopt;
    }
    if (*present == 0) {
        return std::nullopt;
    }
    LocalPayloadLease lease;
    const auto region_name = read_string_binary(data, pos);
    const auto slot_index = read_u32(data, pos);
    const auto slot_size = read_u32(data, pos);
    const auto slot_count = read_u32(data, pos);
    const auto length = read_u32(data, pos);
    if (!region_name || !slot_index || !slot_size || !slot_count || !length) {
        return std::nullopt;
    }
    lease.region_name = *region_name;
    lease.slot_index = *slot_index;
    lease.slot_size = *slot_size;
    lease.slot_count = *slot_count;
    lease.length = *length;
    return lease;
}

inline void append_u64_little_endian(std::string& out, std::uint64_t value) {
    for (std::size_t index = 0; index < sizeof(value); ++index)
        out.push_back(static_cast<char>((value >> (index * 8)) & 0xFFU));
}
inline std::optional<std::uint64_t> read_u64_little_endian(const std::string& data, std::size_t& pos) {
    if (pos + sizeof(std::uint64_t) > data.size()) return std::nullopt;
    std::uint64_t value = 0;
    for (std::size_t index = 0; index < sizeof(value); ++index)
        value |= static_cast<std::uint64_t>(static_cast<std::uint8_t>(data[pos + index])) << (index * 8);
    pos += sizeof(value);
    return value;
}
inline LocalPayloadLease read_control_shm_lease(const std::string& data, std::size_t& position, bool require_end = true) {
    if (position >= data.size() || static_cast<std::uint8_t>(data[position]) != 1)
        throw ControlShmProtocolError("Control v5 payload must use shared memory");
    auto lease = read_payload_lease_binary(data, position);
    if (!lease || (require_end && position != data.size()))
        throw ControlShmProtocolError("Invalid Control v5 lease/trailing metadata");
    return *lease;
}

inline void consume_control_shm_response(int socket, std::uint64_t response_ticket,
    std::uint64_t expected_ticket, const LocalPayloadLease& response_lease,
    ControlShmFraming::Deadline deadline, ControlShmMetrics& metrics) {
    if (response_ticket != expected_ticket || response_ticket == 0)
        throw ControlShmProtocolError("Control v5 response ticket does not match the next expected ticket");
    if (response_ticket == std::numeric_limits<std::uint64_t>::max())
        throw ControlShmProtocolError("Control v5 response ticket sequence is exhausted");
    std::string acknowledgement;
    append_control_shm_header(acknowledgement, kLocalControlKindResponseConsumed);
    append_u64_little_endian(acknowledgement, response_ticket);
    append_payload_lease_binary(acknowledgement, response_lease);
    ControlShmFraming::send_until(socket, acknowledgement, -1, deadline, metrics);
}

inline void receive_control_shm_release(int socket, std::uint64_t response_ticket,
    ControlShmFraming::Deadline deadline, ControlShmMetrics& metrics) {
    auto released = ControlShmFraming::receive_until(socket, deadline, metrics);
    if (released.descriptor.get() >= 0 ||
        !is_control_shm_frame(released.data, kLocalControlKindResponseReleased))
        throw ControlShmProtocolError("Malformed Control v5 response-released frame");
    std::size_t released_position = 6;
    const auto released_ticket = read_u64_little_endian(released.data, released_position);
    if (!released_ticket || *released_ticket != response_ticket || released_position != released.data.size())
        throw ControlShmProtocolError("Control v5 released ticket or frame length is invalid");
}
}
}
