#pragma once

// Single C++ SDK JSON string escaper, shared by core/client.cpp,
// wrapper/subsystem.cpp and wrapper/prepared_codec.cpp. The anonymous
// namespace keeps internal linkage in each translation unit (no exported
// symbol, same per-unit inlining as the former per-unit copies).

#include <string>
#include <string_view>

namespace r2kernel::sdk_detail {
namespace {

// Escapes for a JSON string body (RFC 8259 section 7): quote, backslash and
// every control character below 0x20.
std::string json_escape(std::string_view value) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out;
    out.reserve(value.size() + 8);
    for (const char ch : value) {
        switch (ch) {
        case '\\':
            out += "\\\\";
            break;
        case '"':
            out += "\\\"";
            break;
        case '\n':
            out += "\\n";
            break;
        case '\r':
            out += "\\r";
            break;
        case '\t':
            out += "\\t";
            break;
        case '\b':
            out += "\\b";
            break;
        case '\f':
            out += "\\f";
            break;
        default: {
            const auto byte = static_cast<unsigned char>(ch);
            if (byte < 0x20) {
                out += "\\u00";
                out.push_back(kHex[byte >> 4]);
                out.push_back(kHex[byte & 0x0f]);
            } else {
                out.push_back(ch);
            }
            break;
        }
        }
    }
    return out;
}

}  // namespace
}  // namespace r2kernel::sdk_detail
