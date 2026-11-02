#pragma once

// JSON text of a yaml-cpp node that was parsed from JSON. Shared by the core
// route parser and the wrapper timing-header helpers; internal linkage in each
// translation unit, like json_text.hpp.

#include "json_text.hpp"

#include <yaml-cpp/yaml.h>

#include <cctype>
#include <string>

namespace r2kernel::sdk_detail {
namespace {

// Whether text is a JSON number (RFC 8259 section 6).
bool is_json_number(const std::string& text) {
    std::size_t i = 0;
    const auto digits = [&]() {
        const std::size_t start = i;
        while (i < text.size() && std::isdigit(static_cast<unsigned char>(text[i]))) ++i;
        return i > start;
    };
    if (i < text.size() && text[i] == '-') ++i;
    if (i < text.size() && text[i] == '0') ++i;
    else if (!digits()) return false;
    if (i < text.size() && text[i] == '.') {
        ++i;
        if (!digits()) return false;
    }
    if (i < text.size() && (text[i] == 'e' || text[i] == 'E')) {
        ++i;
        if (i < text.size() && (text[i] == '+' || text[i] == '-')) ++i;
        if (!digits()) return false;
    }
    return i == text.size();
}

// Quoted scalars stay strings; plain scalars keep their JSON literal or number
// type. A plain scalar that is neither is emitted as a string.
std::string yaml_json_text(const YAML::Node& node) {
    if (!node || node.IsNull()) return "null";
    if (node.IsSequence()) {
        std::string out = "[";
        for (std::size_t index = 0; index < node.size(); ++index) {
            if (index > 0) out += ',';
            out += yaml_json_text(node[index]);
        }
        return out + "]";
    }
    if (node.IsMap()) {
        std::string out = "{";
        bool first = true;
        for (const auto& item : node) {
            if (!first) out += ',';
            first = false;
            out += "\"" + json_escape(item.first.Scalar()) + "\":" + yaml_json_text(item.second);
        }
        return out + "}";
    }
    const std::string& text = node.Scalar();
    if (node.Tag() != "!" && (text == "true" || text == "false" || text == "null" || is_json_number(text)))
        return text;
    return "\"" + json_escape(text) + "\"";
}

}  // namespace
}  // namespace r2kernel::sdk_detail
