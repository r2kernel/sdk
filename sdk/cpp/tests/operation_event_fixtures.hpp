#pragma once

// Readers of the Python Operation event fixtures (sdk/testdata/wire/
// operation-event-*.json) shared by the C++ Operation event tests.
#include "r2kernel/wrapper/core_client.hpp"

#include <yaml-cpp/yaml.h>

#include <cstdint>
#include <stdexcept>
#include <string>

namespace r2kernel_test {

inline void fixture_require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

inline YAML::Node wire_fixture(const std::string& name) {
    return YAML::LoadFile(std::string(R2KERNEL_SDK_WIRE_DIR) + "/" + name);
}

inline r2kernel::wrapper::ByteBuffer from_hex(const std::string& text) {
    fixture_require(text.size() % 2 == 0, "odd hex length");
    r2kernel::wrapper::ByteBuffer out;
    for (std::size_t index = 0; index < text.size(); index += 2)
        out.push_back(static_cast<std::uint8_t>(std::stoul(text.substr(index, 2), nullptr, 16)));
    return out;
}

inline std::string text_or_empty(const YAML::Node& node) { return node && !node.IsNull() ? node.as<std::string>() : ""; }

inline bool has_flag(const YAML::Node& layout, const std::string& name) {
    for (const auto& flag : layout["flag_names"])
        if (flag.as<std::string>() == name) return true;
    return false;
}

// A fixture JSON value as JSON text. yaml-cpp tags quoted scalars "!", so a
// JSON string stays a string and other scalars (numbers, true/false) stay bare.
inline std::string to_json(const YAML::Node& node) {
    switch (node.Type()) {
    case YAML::NodeType::Null: return "null";
    case YAML::NodeType::Scalar: {
        if (node.Tag() != "!") return node.Scalar();
        std::string out = "\"";
        for (const char ch : node.Scalar()) {
            if (ch == '"' || ch == '\\') out.push_back('\\');
            out.push_back(ch);
        }
        return out + "\"";
    }
    case YAML::NodeType::Sequence: {
        std::string out = "[";
        for (std::size_t index = 0; index < node.size(); ++index) out += (index ? "," : "") + to_json(node[index]);
        return out + "]";
    }
    case YAML::NodeType::Map: {
        std::string out = "{";
        bool first = true;
        for (const auto& entry : node) {
            out += (first ? "\"" : ",\"") + entry.first.as<std::string>() + "\":" + to_json(entry.second);
            first = false;
        }
        return out + "}";
    }
    default: throw std::runtime_error("unsupported fixture value");
    }
}

}  // namespace r2kernel_test
