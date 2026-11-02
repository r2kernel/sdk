#pragma once

#include <cstddef>
#include <stdexcept>
#include <string>
#include <string_view>

namespace r2kernel::core::detail {

inline constexpr std::string_view kManagedSourceTopicPrefix = "__r2kernel_source_v1__:";
inline constexpr std::string_view kReservedManagedSourcePrefix = "__r2kernel_source_";

inline bool valid_utf8_without_nul(std::string_view value) {
    std::size_t index = 0;
    while (index < value.size()) {
        const auto first = static_cast<unsigned char>(value[index]);
        if (first == 0) return false;
        if (first <= 0x7f) {
            ++index;
            continue;
        }
        std::size_t continuation_count = 0;
        unsigned char second_min = 0x80;
        unsigned char second_max = 0xbf;
        if (first >= 0xc2 && first <= 0xdf) {
            continuation_count = 1;
        } else if (first >= 0xe0 && first <= 0xef) {
            continuation_count = 2;
            if (first == 0xe0) second_min = 0xa0;
            if (first == 0xed) second_max = 0x9f;
        } else if (first >= 0xf0 && first <= 0xf4) {
            continuation_count = 3;
            if (first == 0xf0) second_min = 0x90;
            if (first == 0xf4) second_max = 0x8f;
        } else {
            return false;
        }
        if (index + continuation_count >= value.size()) return false;
        const auto second = static_cast<unsigned char>(value[index + 1]);
        if (second < second_min || second > second_max) return false;
        for (std::size_t offset = 2; offset <= continuation_count; ++offset) {
            const auto byte = static_cast<unsigned char>(value[index + offset]);
            if (byte < 0x80 || byte > 0xbf) return false;
        }
        index += continuation_count + 1;
    }
    return true;
}

inline void validate_managed_source_field(std::string_view value, const char* name, std::size_t maximum_bytes) {
    if (value.empty() || value.size() > maximum_bytes || !valid_utf8_without_nul(value)) {
        throw std::invalid_argument(std::string(name) + " must contain bounded, NUL-free UTF-8 bytes");
    }
}

inline std::string encode_source_topic(std::string_view source_name, std::string_view topic) {
    if (source_name.rfind("__r2kernel_authority_", 0) == 0 || source_name.rfind("__r2kernel_host_", 0) == 0)
        throw std::invalid_argument("source name uses the reserved authenticated-source prefix");
    validate_managed_source_field(source_name, "source name", 128);
    validate_managed_source_field(topic, "topic", 1024);
    if (topic.size() >= kReservedManagedSourcePrefix.size() &&
        topic.compare(0, kReservedManagedSourcePrefix.size(), kReservedManagedSourcePrefix) == 0) {
        throw std::invalid_argument("original topic uses the reserved managed-source prefix");
    }

    static constexpr char hex_digits[] = "0123456789abcdef";
    std::string result(kManagedSourceTopicPrefix);
    result.reserve(kManagedSourceTopicPrefix.size() + source_name.size() * 2 + 1 + topic.size());
    for (const unsigned char byte : source_name) {
        result.push_back(hex_digits[byte >> 4]);
        result.push_back(hex_digits[byte & 0x0f]);
    }
    result.push_back(':');
    result.append(topic);
    return result;
}

}  // namespace r2kernel::core::detail
