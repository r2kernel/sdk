#pragma once

// Internal strict JSON document (RFC 8259) of the C++ SDK. Unlike yaml-cpp,
// which the SDK uses for lenient reads, it keeps JSON value types, so replies
// can be checked the way the Python SDK checks json.loads results (a bool is
// not the string "true", null is not a missing member). Not exported.

#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace r2kernel::sdk_detail {

class JsonValue {
public:
    enum class Kind { null, boolean, number, string, array, object };
    using Member = std::pair<std::string, JsonValue>;

    JsonValue() = default;
    static JsonValue from_bool(bool value);
    // text must be a JSON number literal.
    static JsonValue from_number_text(std::string text);
    static JsonValue from_string(std::string value);
    static JsonValue empty_array();
    static JsonValue empty_object();

    Kind kind() const noexcept { return kind_; }
    bool is_null() const noexcept { return kind_ == Kind::null; }
    bool is_bool() const noexcept { return kind_ == Kind::boolean; }
    bool is_number() const noexcept { return kind_ == Kind::number; }
    bool is_string() const noexcept { return kind_ == Kind::string; }
    bool is_array() const noexcept { return kind_ == Kind::array; }
    bool is_object() const noexcept { return kind_ == Kind::object; }
    // Python truthiness: false, null, 0, "", [] and {} are false.
    bool truthy() const noexcept;

    bool as_bool() const noexcept { return boolean_; }
    // String value, or the literal text of a number.
    const std::string& text() const noexcept { return text_; }
    const std::vector<JsonValue>& items() const noexcept { return items_; }
    std::vector<JsonValue>& items() noexcept { return items_; }
    const std::vector<Member>& members() const noexcept { return members_; }

    // Object member, or nullptr when absent (or this is not an object).
    const JsonValue* find(std::string_view key) const noexcept;
    JsonValue* find(std::string_view key) noexcept;
    // Replaces an existing member in place or appends a new one (objects only).
    void set(std::string key, JsonValue value);
    // Removes a member; returns whether it existed.
    bool erase(std::string_view key);

    // Member value when it is a JSON string, else std::nullopt.
    std::optional<std::string> string_member(std::string_view key) const;
    // Member is exactly JSON true / false.
    bool member_is(std::string_view key, bool value) const noexcept;
    // Member is exactly the JSON string value.
    bool member_equals(std::string_view key, std::string_view value) const noexcept;

    // Compact JSON text (no insignificant whitespace, members in order).
    std::string dump() const;

private:
    Kind kind_ = Kind::null;
    bool boolean_ = false;
    std::string text_;
    std::vector<JsonValue> items_;
    std::vector<Member> members_;
};

// Parses one JSON document (surrounding whitespace allowed). Rejects invalid
// UTF-8, trailing data and nesting deeper than 512 levels with
// std::runtime_error. A duplicate object key keeps its first position and its
// last value, as Python's json.loads does.
JsonValue parse_json(std::string_view text);

}  // namespace r2kernel::sdk_detail
