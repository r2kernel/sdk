// Subsystem configuration document (Python SDK r2kernel/helper/config.py and
// the Subsystem config_* methods). Cold code in its own translation unit.
#include "subsystem_config.hpp"

#include "r2kernel/wrapper/subsystem.hpp"
#include "../core/json_text.hpp"

#include <yaml-cpp/yaml.h>

#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>

namespace r2kernel::wrapper::detail {
namespace {

using r2kernel::sdk_detail::JsonValue;

// Implicit scalar resolution of PyYAML's SafeLoader (YAML 1.1), matched by
// hand in the order and with the patterns of PyYAML's implicit resolvers.
bool in_set(char ch, std::string_view set) { return set.find(ch) != std::string_view::npos; }
constexpr std::string_view kDigits = "0123456789";
constexpr std::string_view kDigitsUnderscore = "0123456789_";

// All of text from pos on is in set (and at least min_count characters).
bool all_in(std::string_view text, std::size_t pos, std::string_view set, std::size_t min_count = 1) {
    if (text.size() < pos + min_count) return false;
    for (std::size_t index = pos; index < text.size(); ++index)
        if (!in_set(text[index], set)) return false;
    return true;
}

std::size_t skip_in(std::string_view text, std::size_t pos, std::string_view set) {
    while (pos < text.size() && in_set(text[pos], set)) ++pos;
    return pos;
}

std::size_t sign_length(std::string_view text) { return !text.empty() && in_set(text.front(), "+-") ? 1 : 0; }

// One or more (?::[0-5]?[0-9]) groups from pos; returns the end or npos.
std::size_t sexagesimal_groups(std::string_view text, std::size_t pos) {
    std::size_t groups = 0;
    while (pos < text.size() && text[pos] == ':') {
        ++pos;
        if (pos + 1 < text.size() && in_set(text[pos], "012345") && in_set(text[pos + 1], kDigits)) {
            pos += 2;
        } else if (pos < text.size() && in_set(text[pos], kDigits)) {
            pos += 1;
        } else {
            return std::string_view::npos;
        }
        ++groups;
    }
    return groups == 0 ? std::string_view::npos : pos;
}

// (?:[eE][-+][0-9]+)? at pos through the end of text.
bool optional_exponent_to_end(std::string_view text, std::size_t pos) {
    if (pos == text.size()) return true;
    return in_set(text[pos], "eE") && pos + 1 < text.size() && in_set(text[pos + 1], "+-")
        && all_in(text, pos + 2, kDigits);
}

bool yaml_bool_text(std::string_view text) {
    for (const std::string_view value : {"yes", "Yes", "YES", "no", "No", "NO", "true", "True", "TRUE", "false",
                                         "False", "FALSE", "on", "On", "ON", "off", "Off", "OFF"}) {
        if (text == value) return true;
    }
    return false;
}

bool yaml_int_text(std::string_view text) {
    const std::string_view rest = text.substr(sign_length(text));
    if (rest.empty()) return false;
    if (rest.substr(0, 2) == "0b") return all_in(rest, 2, "01_");
    if (rest.substr(0, 2) == "0x") return all_in(rest, 2, "0123456789abcdefABCDEF_");
    if (rest == "0") return true;
    if (rest.front() == '0') return all_in(rest, 1, "01234567_");
    if (!in_set(rest.front(), "123456789")) return false;
    const std::size_t end = skip_in(rest, 1, kDigitsUnderscore);
    return end == rest.size() || sexagesimal_groups(rest, end) == rest.size();
}

bool yaml_float_text(std::string_view text) {
    for (const std::string_view value : {".nan", ".NaN", ".NAN"}) {
        if (text == value) return true;
    }
    const std::string_view rest = text.substr(sign_length(text));
    for (const std::string_view value : {".inf", ".Inf", ".INF"}) {
        if (rest == value) return true;
    }
    if (!text.empty() && text.front() == '.')
        return text.size() > 1 && in_set(text[1], kDigitsUnderscore)
            && optional_exponent_to_end(text, skip_in(text, 1, kDigitsUnderscore));
    if (rest.empty() || !in_set(rest.front(), kDigits)) return false;
    std::size_t pos = skip_in(rest, 1, kDigitsUnderscore);
    if (pos < rest.size() && rest[pos] == '.')
        return optional_exponent_to_end(rest, skip_in(rest, pos + 1, kDigitsUnderscore));
    pos = sexagesimal_groups(rest, pos);
    return pos != std::string_view::npos && pos < rest.size() && rest[pos] == '.'
        && skip_in(rest, pos + 1, kDigitsUnderscore) == rest.size();
}

// Timestamps load as dates in PyYAML; they stay strings here and are quoted
// when written, so both SDKs read the value back as the same string.
bool yaml_timestamp_text(std::string_view text) {
    const auto digits = [&](std::size_t pos, std::size_t count) {
        return text.size() >= pos + count && all_in(text.substr(pos, count), 0, kDigits, count);
    };
    if (!digits(0, 4) || text.size() < 8 || text[4] != '-') return false;
    if (text.size() == 10 && digits(5, 2) && text[7] == '-' && digits(8, 2)) return true;
    std::size_t pos = 5;
    const auto one_or_two_digits = [&]() {
        if (!digits(pos, 1)) return false;
        pos += digits(pos + 1, 1) ? 2 : 1;
        return true;
    };
    if (!one_or_two_digits() || pos >= text.size() || text[pos++] != '-' || !one_or_two_digits()) return false;
    if (pos < text.size() && in_set(text[pos], "Tt")) {
        ++pos;
    } else {
        const std::size_t spaced = skip_in(text, pos, " \t");
        if (spaced == pos) return false;
        pos = spaced;
    }
    if (!one_or_two_digits() || pos >= text.size() || text[pos++] != ':' || !digits(pos, 2)) return false;
    pos += 2;
    if (pos >= text.size() || text[pos++] != ':' || !digits(pos, 2)) return false;
    pos += 2;
    if (pos < text.size() && text[pos] == '.') pos = skip_in(text, pos + 1, kDigits);
    pos = skip_in(text, pos, " \t");
    if (pos == text.size()) return true;
    if (text[pos] == 'Z') return pos + 1 == text.size();
    if (!in_set(text[pos], "+-")) return false;
    ++pos;
    if (!one_or_two_digits()) return false;
    if (pos == text.size()) return true;
    return text[pos] == ':' && pos + 3 == text.size() && digits(pos + 1, 2);
}

bool yaml_null_text(const std::string& text) {
    return text.empty() || text == "~" || text == "null" || text == "Null" || text == "NULL";
}

std::string without_underscores(const std::string& text) {
    std::string out;
    for (const char ch : text)
        if (ch != '_') out.push_back(ch);
    return out;
}

// Python's repr(float): the shortest round-trip digits, fixed notation when
// the decimal point position is in (-4, 16], else d.ddde+XX.
std::string python_float_repr(double value) {
    if (std::isnan(value)) return "NaN";
    if (std::isinf(value)) return value < 0 ? "-Infinity" : "Infinity";
    char buffer[64];
    const auto result = std::to_chars(buffer, buffer + sizeof(buffer), value, std::chars_format::scientific);
    const std::string scientific(buffer, result.ptr);
    const auto e = scientific.find('e');
    std::string mantissa = scientific.substr(0, e);
    const int exponent = std::stoi(scientific.substr(e + 1));
    std::string sign;
    if (!mantissa.empty() && mantissa.front() == '-') {
        sign = "-";
        mantissa.erase(0, 1);
    }
    std::string digits;
    for (const char ch : mantissa)
        if (ch != '.') digits.push_back(ch);
    const int point = exponent + 1;
    const int count = static_cast<int>(digits.size());
    if (point > -4 && point <= 16) {
        if (point <= 0) return sign + "0." + std::string(static_cast<std::size_t>(-point), '0') + digits;
        if (point >= count) return sign + digits + std::string(static_cast<std::size_t>(point - count), '0') + ".0";
        return sign + digits.substr(0, static_cast<std::size_t>(point)) + "." + digits.substr(static_cast<std::size_t>(point));
    }
    std::string out = sign + digits.substr(0, 1);
    if (count > 1) out += "." + digits.substr(1);
    const int magnitude = std::abs(exponent);
    out += std::string("e") + (exponent < 0 ? "-" : "+") + (magnitude < 10 ? "0" : "") + std::to_string(magnitude);
    return out;
}

std::optional<long long> parse_base(const std::string& digits, int base) {
    long long value = 0;
    const auto result = std::from_chars(digits.data(), digits.data() + digits.size(), value, base);
    if (result.ec != std::errc() || result.ptr != digits.data() + digits.size()) return std::nullopt;
    return value;
}

// PyYAML int constructor (signs, 0b, 0x, leading-zero octal, base 60).
JsonValue yaml_int(const std::string& raw) {
    std::string text = without_underscores(raw);
    std::string sign;
    if (!text.empty() && (text.front() == '-' || text.front() == '+')) {
        if (text.front() == '-') sign = "-";
        text.erase(0, 1);
    }
    std::optional<long long> value;
    if (text == "0") return JsonValue::from_number_text("0");
    if (text.rfind("0b", 0) == 0) {
        value = parse_base(text.substr(2), 2);
    } else if (text.rfind("0x", 0) == 0) {
        value = parse_base(text.substr(2), 16);
    } else if (text.front() == '0') {
        value = parse_base(text.substr(1), 8);
    } else if (text.find(':') != std::string::npos) {
        long long total = 0;
        std::stringstream parts(text);
        std::string part;
        while (std::getline(parts, part, ':')) total = total * 60 + std::stoll(part);
        value = total;
    } else {
        return JsonValue::from_number_text(sign + text);
    }
    if (!value) return JsonValue::from_string(raw);
    if (*value == 0) return JsonValue::from_number_text("0");
    return JsonValue::from_number_text(sign + std::to_string(*value));
}

JsonValue yaml_float(const std::string& raw) {
    std::string text = without_underscores(raw);
    double sign = 1.0;
    if (!text.empty() && (text.front() == '-' || text.front() == '+')) {
        if (text.front() == '-') sign = -1.0;
        text.erase(0, 1);
    }
    double value = 0.0;
    if (text == ".inf" || text == ".Inf" || text == ".INF") {
        value = std::numeric_limits<double>::infinity();
    } else if (text == ".nan" || text == ".NaN" || text == ".NAN") {
        value = std::numeric_limits<double>::quiet_NaN();
    } else if (text.find(':') != std::string::npos) {
        std::stringstream parts(text);
        std::string part;
        while (std::getline(parts, part, ':')) value = value * 60.0 + std::strtod(part.c_str(), nullptr);
    } else {
        value = std::strtod(text.c_str(), nullptr);
    }
    return JsonValue::from_number_text(python_float_repr(sign * value));
}

JsonValue resolve_plain_scalar(const std::string& text) {
    if (yaml_null_text(text)) return JsonValue {};
    if (yaml_bool_text(text)) {
        const char first = text.front();
        return JsonValue::from_bool(first == 'y' || first == 'Y' || first == 't' || first == 'T'
                                    || text == "on" || text == "On" || text == "ON");
    }
    if (yaml_int_text(text)) return yaml_int(text);
    if (yaml_float_text(text)) return yaml_float(text);
    return JsonValue::from_string(text);
}

JsonValue yaml_node_value(const YAML::Node& node) {
    switch (node.Type()) {
    case YAML::NodeType::Undefined:
    case YAML::NodeType::Null:
        return JsonValue {};
    case YAML::NodeType::Sequence: {
        JsonValue array = JsonValue::empty_array();
        for (const auto& item : node) array.items().push_back(yaml_node_value(item));
        return array;
    }
    case YAML::NodeType::Map: {
        JsonValue object = JsonValue::empty_object();
        for (const auto& item : node) {
            if (!item.first.IsScalar()) throw std::runtime_error("config mapping keys must be scalars");
            object.set(item.first.Scalar(), yaml_node_value(item.second));
        }
        return object;
    }
    case YAML::NodeType::Scalar:
        break;
    }
    const std::string& text = node.Scalar();
    const std::string& tag = node.Tag();
    if (tag == "?") return resolve_plain_scalar(text);
    if (tag == "!" || tag == "tag:yaml.org,2002:str") return JsonValue::from_string(text);
    return resolve_plain_scalar(text);
}

// Whether a string can be written as a YAML plain scalar and read back as
// the same string by PyYAML and yaml-cpp.
bool plain_string_safe(const std::string& text) {
    if (text.empty() || text.front() == ' ' || text.back() == ' ' || text.back() == ':') return false;
    if (std::string_view("-?:,[]{}#&*!|>'\"%@`").find(text.front()) != std::string_view::npos) return false;
    if (text.find(": ") != std::string::npos || text.find(" #") != std::string::npos) return false;
    for (const char ch : text) {
        const auto byte = static_cast<unsigned char>(ch);
        if (byte < 0x20 || byte == 0x7F) return false;
    }
    return resolve_plain_scalar(text).is_string() && !yaml_timestamp_text(text);
}

std::string yaml_string_scalar(const std::string& text) {
    if (plain_string_safe(text)) return text;
    bool printable = true;
    for (const char ch : text) {
        const auto byte = static_cast<unsigned char>(ch);
        if (byte < 0x20 || byte == 0x7F) printable = false;
    }
    if (!printable) return "\"" + r2kernel::sdk_detail::json_escape(text) + "\"";
    std::string out = "'";
    for (const char ch : text) {
        out.push_back(ch);
        if (ch == '\'') out.push_back('\'');
    }
    return out + "'";
}

// PyYAML float representation: lower case, ".0e" when an exponent has no point.
std::string yaml_number_scalar(const std::string& text) {
    if (text.find_first_of(".eEIN") == std::string::npos) return text;
    const double value = std::strtod(text.c_str(), nullptr);
    if (std::isnan(value)) return ".nan";
    if (std::isinf(value)) return value < 0 ? "-.inf" : ".inf";
    std::string repr = python_float_repr(value);
    const auto e = repr.find('e');
    if (e != std::string::npos && repr.find('.') == std::string::npos) repr.insert(e, ".0");
    return repr;
}

bool block_container(const JsonValue& value) {
    return (value.is_object() && !value.members().empty()) || (value.is_array() && !value.items().empty());
}

std::string yaml_scalar(const JsonValue& value) {
    switch (value.kind()) {
    case JsonValue::Kind::null:
        return "null";
    case JsonValue::Kind::boolean:
        return value.as_bool() ? "true" : "false";
    case JsonValue::Kind::number:
        return yaml_number_scalar(value.text());
    case JsonValue::Kind::string:
        return yaml_string_scalar(value.text());
    case JsonValue::Kind::array:
        return "[]";
    case JsonValue::Kind::object:
        return "{}";
    }
    return "null";
}

void emit_block(const JsonValue& value, std::size_t indent, bool inline_first, bool top_level, std::string& out);

// One "key: value" or "- value" body after its prefix has been written.
void emit_entry_value(const JsonValue& value, std::size_t indent, std::string& out) {
    if (!block_container(value)) {
        out += " " + yaml_scalar(value) + "\n";
        return;
    }
    out += "\n";
    // PyYAML writes a sequence under a mapping key at the key's indentation.
    emit_block(value, value.is_array() ? indent : indent + 2, false, false, out);
}

void emit_block(const JsonValue& value, std::size_t indent, bool inline_first, bool top_level, std::string& out) {
    bool first = true;
    const auto prefix = [&]() {
        if (!(first && inline_first)) out += std::string(indent, ' ');
        first = false;
    };
    if (value.is_object()) {
        for (const auto& [key, member] : value.members()) {
            if (top_level && !first) out += "\n";
            prefix();
            out += yaml_string_scalar(key) + ":";
            emit_entry_value(member, indent, out);
        }
        return;
    }
    for (const auto& item : value.items()) {
        prefix();
        out += "-";
        if (block_container(item)) {
            out += " ";
            emit_block(item, indent + 2, true, false, out);
        } else {
            out += " " + yaml_scalar(item) + "\n";
        }
    }
}

}  // namespace

JsonValue yaml_text_to_json(const std::string& text) {
    try {
        return yaml_node_value(YAML::Load(text));
    } catch (const YAML::Exception& error) {
        throw std::runtime_error(std::string("invalid YAML: ") + error.what());
    }
}

std::string json_to_yaml_text(const JsonValue& value) {
    if (!block_container(value)) return yaml_scalar(value) + "\n";
    std::string out;
    emit_block(value, 0, false, true, out);
    return out;
}

SubsystemConfig::SubsystemConfig(std::filesystem::path file) : file_(std::move(file)) {}

std::string SubsystemConfig::load() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::error_code error;
    if (std::filesystem::is_regular_file(file_, error)) {
        std::ifstream input(file_, std::ios::binary);
        if (!input) throw std::runtime_error("cannot read subsystem config " + file_.string());
        std::stringstream text;
        text << input.rdbuf();
        JsonValue document = yaml_text_to_json(text.str());
        data_ = document.truthy() ? std::move(document) : JsonValue::empty_object();
    } else {
        data_ = JsonValue::empty_object();
        save_unlocked();
    }
    return data_.dump();
}

void SubsystemConfig::save() const {
    std::lock_guard<std::mutex> lock(mutex_);
    save_unlocked();
}

void SubsystemConfig::save_unlocked() const {
    if (file_.has_parent_path()) std::filesystem::create_directories(file_.parent_path());
    std::ofstream output(file_, std::ios::binary | std::ios::trunc);
    output << json_to_yaml_text(data_);
    output.close();
    if (!output) throw std::runtime_error("cannot write subsystem config " + file_.string());
}

std::string SubsystemConfig::data_json() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return data_.dump();
}

std::string SubsystemConfig::get(const std::vector<std::string>& path, const std::string& default_json) const {
    if (path.empty()) return default_json;
    std::lock_guard<std::mutex> lock(mutex_);
    const JsonValue* current = &data_;
    for (const auto& key : path) {
        current = current->find(key);
        if (current == nullptr) return default_json;
    }
    return current->is_null() ? default_json : current->dump();
}

void SubsystemConfig::set(const std::vector<std::string>& path, const std::string& value_json, bool save_now) {
    JsonValue value;
    try {
        value = r2kernel::sdk_detail::parse_json(value_json);
    } catch (const std::runtime_error& error) {
        throw std::invalid_argument(std::string("config value must be JSON text: ") + error.what());
    }
    if (path.empty()) return;
    std::lock_guard<std::mutex> lock(mutex_);
    if (!data_.is_object()) throw std::runtime_error("subsystem config document is not a mapping: " + file_.string());
    JsonValue* current = &data_;
    for (std::size_t index = 0; index + 1 < path.size(); ++index) {
        JsonValue* next = current->find(path[index]);
        if (next == nullptr || !next->is_object()) {
            current->set(path[index], JsonValue::empty_object());
            next = current->find(path[index]);
        }
        current = next;
    }
    current->set(path.back(), std::move(value));
    if (save_now) save_unlocked();
}

std::vector<std::string> SubsystemConfig::split_path(const std::string& path) {
    std::vector<std::string> out;
    std::string part;
    for (const char ch : path) {
        if (ch == '.' || ch == '/') {
            if (!part.empty()) out.push_back(std::move(part));
            part.clear();
        } else {
            part.push_back(ch);
        }
    }
    if (!part.empty()) out.push_back(std::move(part));
    return out;
}

std::shared_ptr<SubsystemConfig> load_subsystem_config(const std::filesystem::path& user_data_path) {
    auto config = std::make_shared<SubsystemConfig>(std::filesystem::absolute(user_data_path) / "config");
    (void)config->load();
    return config;
}

std::vector<std::string> SubsystemConfig::literal_path(const std::vector<std::string>& keys) {
    std::vector<std::string> out;
    for (const auto& key : keys)
        if (!key.empty()) out.push_back(key);
    return out;
}

}  // namespace r2kernel::wrapper::detail

namespace r2kernel::wrapper {

using detail::SubsystemConfig;

std::string Subsystem::config_data() const { return config_->data_json(); }

std::string Subsystem::config_get(const std::string& path, const std::string& default_json) const {
    return config_->get(SubsystemConfig::split_path(path), default_json);
}

std::string Subsystem::config_get(const std::vector<std::string>& keys, const std::string& default_json) const {
    return config_->get(SubsystemConfig::literal_path(keys), default_json);
}

void Subsystem::config_set(const std::string& value_json, const std::string& path, bool save) {
    config_->set(SubsystemConfig::split_path(path), value_json, save);
}

void Subsystem::config_set(const std::string& value_json, const std::vector<std::string>& keys, bool save) {
    config_->set(SubsystemConfig::literal_path(keys), value_json, save);
}

std::string Subsystem::config_reload() { return config_->load(); }

void Subsystem::config_save() { config_->save(); }

}  // namespace r2kernel::wrapper
