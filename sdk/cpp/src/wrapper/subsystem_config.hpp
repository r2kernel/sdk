#pragma once

// Subsystem configuration document <user_data_path>/config: the YAML file of
// the Python SDK's r2kernel.helper.config.Config, read and written with the
// same semantics (PyYAML safe_load scalar types, block style, one blank line
// between top-level entries). Values cross the public API as JSON text.
// Internal (hidden visibility).

#include "../core/json_value.hpp"

#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace r2kernel::wrapper::detail {

class SubsystemConfig {
public:
    explicit SubsystemConfig(std::filesystem::path file);

    // Reads the file (an empty or false-y document is {}); a missing file is
    // created from the empty default. Returns the document as JSON text.
    std::string load();
    void save() const;
    std::string data_json() const;
    std::string get(const std::vector<std::string>& path, const std::string& default_json) const;
    // Sets a nested value; intermediate members that are not objects are
    // replaced by objects. An empty path does nothing.
    void set(const std::vector<std::string>& path, const std::string& value_json, bool save_now);

    // Dotted or slash-delimited path components (empty components dropped).
    static std::vector<std::string> split_path(const std::string& path);
    // Literal keys with empty keys dropped.
    static std::vector<std::string> literal_path(const std::vector<std::string>& keys);

private:
    void save_unlocked() const;

    std::filesystem::path file_;
    mutable std::mutex mutex_;
    r2kernel::sdk_detail::JsonValue data_ = r2kernel::sdk_detail::JsonValue::empty_object();
};

// The configuration of a Subsystem whose user data lives at user_data_path:
// <absolute user_data_path>/config, loaded (and created when missing) now.
std::shared_ptr<SubsystemConfig> load_subsystem_config(const std::filesystem::path& user_data_path);

// YAML 1.1 (PyYAML SafeLoader) reading and block-style writing of a JSON
// document; exposed for the configuration tests.
r2kernel::sdk_detail::JsonValue yaml_text_to_json(const std::string& text);
std::string json_to_yaml_text(const r2kernel::sdk_detail::JsonValue& value);

}  // namespace r2kernel::wrapper::detail
