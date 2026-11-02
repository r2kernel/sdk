#include "json_value.hpp"

#include "json_text.hpp"

#include <cstdint>
#include <stdexcept>

namespace r2kernel::sdk_detail {
namespace {

constexpr int kMaxJsonDepth = 512;

[[noreturn]] void fail(const char* what, std::size_t offset) {
    throw std::runtime_error(std::string("invalid JSON: ") + what + " at offset " + std::to_string(offset));
}

// Length of the valid UTF-8 sequence starting at text[pos], or 0.
std::size_t utf8_sequence_length(std::string_view text, std::size_t pos) {
    const auto byte = [&](std::size_t index) { return static_cast<unsigned char>(text[index]); };
    const unsigned char lead = byte(pos);
    if (lead < 0x80) return 1;
    std::size_t length = 0;
    std::uint32_t code = 0;
    if (lead >= 0xC2 && lead <= 0xDF) {
        length = 2;
        code = lead & 0x1F;
    } else if (lead >= 0xE0 && lead <= 0xEF) {
        length = 3;
        code = lead & 0x0F;
    } else if (lead >= 0xF0 && lead <= 0xF4) {
        length = 4;
        code = lead & 0x07;
    } else {
        return 0;
    }
    if (pos + length > text.size()) return 0;
    for (std::size_t index = 1; index < length; ++index) {
        if ((byte(pos + index) & 0xC0) != 0x80) return 0;
        code = (code << 6) | (byte(pos + index) & 0x3F);
    }
    if ((length == 3 && (code < 0x800 || (code >= 0xD800 && code <= 0xDFFF)))
        || (length == 4 && (code < 0x10000 || code > 0x10FFFF))) {
        return 0;
    }
    return length;
}

void append_utf8(std::string& out, std::uint32_t code) {
    if (code < 0x80) {
        out.push_back(static_cast<char>(code));
    } else if (code < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (code >> 6)));
        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else if (code < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (code >> 12)));
        out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (code >> 18)));
        out.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    }
}

class Parser {
public:
    explicit Parser(std::string_view text) : text_(text) {}

    JsonValue document() {
        skip_whitespace();
        JsonValue value = parse_value(0);
        skip_whitespace();
        if (pos_ != text_.size()) fail("trailing data", pos_);
        return value;
    }

private:
    void skip_whitespace() {
        while (pos_ < text_.size()
               && (text_[pos_] == ' ' || text_[pos_] == '\t' || text_[pos_] == '\n' || text_[pos_] == '\r')) {
            ++pos_;
        }
    }

    bool consume_literal(std::string_view literal) {
        if (text_.substr(pos_, literal.size()) != literal) return false;
        pos_ += literal.size();
        return true;
    }

    JsonValue parse_value(int depth) {
        if (depth > kMaxJsonDepth) fail("nesting too deep", pos_);
        if (pos_ >= text_.size()) fail("unexpected end", pos_);
        const char ch = text_[pos_];
        if (ch == '{') return parse_object(depth + 1);
        if (ch == '[') return parse_array(depth + 1);
        if (ch == '"') return JsonValue::from_string(parse_string());
        if (consume_literal("true")) return JsonValue::from_bool(true);
        if (consume_literal("false")) return JsonValue::from_bool(false);
        if (consume_literal("null")) return JsonValue {};
        if (ch == '-' || (ch >= '0' && ch <= '9')) return parse_number();
        fail("unexpected character", pos_);
    }

    JsonValue parse_object(int depth) {
        ++pos_;
        JsonValue object = JsonValue::empty_object();
        skip_whitespace();
        if (pos_ < text_.size() && text_[pos_] == '}') {
            ++pos_;
            return object;
        }
        for (;;) {
            skip_whitespace();
            if (pos_ >= text_.size() || text_[pos_] != '"') fail("expected object key", pos_);
            std::string key = parse_string();
            skip_whitespace();
            if (pos_ >= text_.size() || text_[pos_] != ':') fail("expected ':'", pos_);
            ++pos_;
            skip_whitespace();
            object.set(std::move(key), parse_value(depth));
            skip_whitespace();
            if (pos_ < text_.size() && text_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < text_.size() && text_[pos_] == '}') {
                ++pos_;
                return object;
            }
            fail("expected ',' or '}'", pos_);
        }
    }

    JsonValue parse_array(int depth) {
        ++pos_;
        JsonValue array = JsonValue::empty_array();
        skip_whitespace();
        if (pos_ < text_.size() && text_[pos_] == ']') {
            ++pos_;
            return array;
        }
        for (;;) {
            skip_whitespace();
            array.items().push_back(parse_value(depth));
            skip_whitespace();
            if (pos_ < text_.size() && text_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < text_.size() && text_[pos_] == ']') {
                ++pos_;
                return array;
            }
            fail("expected ',' or ']'", pos_);
        }
    }

    std::uint32_t parse_hex4() {
        if (pos_ + 4 > text_.size()) fail("truncated \\u escape", pos_);
        std::uint32_t code = 0;
        for (int index = 0; index < 4; ++index) {
            const char ch = text_[pos_++];
            code <<= 4;
            if (ch >= '0' && ch <= '9') code |= static_cast<std::uint32_t>(ch - '0');
            else if (ch >= 'a' && ch <= 'f') code |= static_cast<std::uint32_t>(ch - 'a' + 10);
            else if (ch >= 'A' && ch <= 'F') code |= static_cast<std::uint32_t>(ch - 'A' + 10);
            else fail("invalid \\u escape", pos_ - 1);
        }
        return code;
    }

    std::string parse_string() {
        ++pos_;
        std::string out;
        for (;;) {
            if (pos_ >= text_.size()) fail("unterminated string", pos_);
            const auto ch = static_cast<unsigned char>(text_[pos_]);
            if (ch == '"') {
                ++pos_;
                return out;
            }
            if (ch < 0x20) fail("control character in string", pos_);
            if (ch != '\\') {
                const std::size_t length = utf8_sequence_length(text_, pos_);
                if (length == 0) fail("invalid UTF-8", pos_);
                out.append(text_.substr(pos_, length));
                pos_ += length;
                continue;
            }
            if (++pos_ >= text_.size()) fail("unterminated escape", pos_);
            const char escape = text_[pos_++];
            switch (escape) {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            case 'u': {
                std::uint32_t code = parse_hex4();
                if (code >= 0xD800 && code <= 0xDBFF && text_.substr(pos_, 2) == "\\u") {
                    const std::size_t saved = pos_;
                    pos_ += 2;
                    const std::uint32_t low = parse_hex4();
                    if (low >= 0xDC00 && low <= 0xDFFF) {
                        code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                    } else {
                        pos_ = saved;
                    }
                }
                // A lone surrogate is kept (as Python does) in its 3-byte form.
                append_utf8(out, code);
                break;
            }
            default:
                fail("invalid escape", pos_ - 1);
            }
        }
    }

    JsonValue parse_number() {
        const std::size_t start = pos_;
        const auto digit = [&]() { return pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9'; };
        if (text_[pos_] == '-') ++pos_;
        if (!digit()) fail("invalid number", pos_);
        if (text_[pos_] == '0') {
            ++pos_;
        } else {
            while (digit()) ++pos_;
        }
        if (pos_ < text_.size() && text_[pos_] == '.') {
            ++pos_;
            if (!digit()) fail("invalid number fraction", pos_);
            while (digit()) ++pos_;
        }
        if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
            if (!digit()) fail("invalid number exponent", pos_);
            while (digit()) ++pos_;
        }
        return JsonValue::from_number_text(std::string(text_.substr(start, pos_ - start)));
    }

    std::string_view text_;
    std::size_t pos_ = 0;
};

void dump_into(const JsonValue& value, std::string& out) {
    switch (value.kind()) {
    case JsonValue::Kind::null:
        out += "null";
        return;
    case JsonValue::Kind::boolean:
        out += value.as_bool() ? "true" : "false";
        return;
    case JsonValue::Kind::number:
        out += value.text();
        return;
    case JsonValue::Kind::string:
        out += '"';
        out += json_escape(value.text());
        out += '"';
        return;
    case JsonValue::Kind::array: {
        out += '[';
        bool first = true;
        for (const auto& item : value.items()) {
            if (!first) out += ',';
            first = false;
            dump_into(item, out);
        }
        out += ']';
        return;
    }
    case JsonValue::Kind::object: {
        out += '{';
        bool first = true;
        for (const auto& [key, member] : value.members()) {
            if (!first) out += ',';
            first = false;
            out += '"';
            out += json_escape(key);
            out += "\":";
            dump_into(member, out);
        }
        out += '}';
        return;
    }
    }
}

}  // namespace

JsonValue JsonValue::from_bool(bool value) {
    JsonValue result;
    result.kind_ = Kind::boolean;
    result.boolean_ = value;
    return result;
}

JsonValue JsonValue::from_number_text(std::string text) {
    JsonValue result;
    result.kind_ = Kind::number;
    result.text_ = std::move(text);
    return result;
}

JsonValue JsonValue::from_string(std::string value) {
    JsonValue result;
    result.kind_ = Kind::string;
    result.text_ = std::move(value);
    return result;
}

JsonValue JsonValue::empty_array() {
    JsonValue result;
    result.kind_ = Kind::array;
    return result;
}

JsonValue JsonValue::empty_object() {
    JsonValue result;
    result.kind_ = Kind::object;
    return result;
}

bool JsonValue::truthy() const noexcept {
    switch (kind_) {
    case Kind::null:
        return false;
    case Kind::boolean:
        return boolean_;
    case Kind::number:
        return text_.find_first_not_of("-+0.eE") != std::string::npos;
    case Kind::string:
        return !text_.empty();
    case Kind::array:
        return !items_.empty();
    case Kind::object:
        return !members_.empty();
    }
    return false;
}

const JsonValue* JsonValue::find(std::string_view key) const noexcept {
    if (kind_ != Kind::object) return nullptr;
    for (const auto& member : members_) {
        if (member.first == key) return &member.second;
    }
    return nullptr;
}

JsonValue* JsonValue::find(std::string_view key) noexcept {
    return const_cast<JsonValue*>(static_cast<const JsonValue*>(this)->find(key));
}

void JsonValue::set(std::string key, JsonValue value) {
    if (kind_ != Kind::object) throw std::logic_error("JsonValue::set requires an object");
    if (JsonValue* existing = find(key)) {
        *existing = std::move(value);
        return;
    }
    members_.emplace_back(std::move(key), std::move(value));
}

bool JsonValue::erase(std::string_view key) {
    for (auto it = members_.begin(); it != members_.end(); ++it) {
        if (it->first == key) {
            members_.erase(it);
            return true;
        }
    }
    return false;
}

std::optional<std::string> JsonValue::string_member(std::string_view key) const {
    const JsonValue* member = find(key);
    if (member == nullptr || !member->is_string()) return std::nullopt;
    return member->text();
}

bool JsonValue::member_is(std::string_view key, bool value) const noexcept {
    const JsonValue* member = find(key);
    return member != nullptr && member->is_bool() && member->as_bool() == value;
}

bool JsonValue::member_equals(std::string_view key, std::string_view value) const noexcept {
    const JsonValue* member = find(key);
    return member != nullptr && member->is_string() && member->text() == value;
}

std::string JsonValue::dump() const {
    std::string out;
    dump_into(*this, out);
    return out;
}

JsonValue parse_json(std::string_view text) {
    return Parser(text).document();
}

}  // namespace r2kernel::sdk_detail
