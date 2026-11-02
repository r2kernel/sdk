#pragma once

// Test-side decoding of the source-wrapped Control topic
// ("__r2kernel_source_v1__:<hex source>:<topic>") that Subsystem hands to a
// non-native CoreClient, as Control decodes it before dispatch.

#include <stdexcept>
#include <string>
#include <utility>

namespace r2kernel_test {

struct WrappedTopic {
    std::string source;
    std::string topic;
};

inline WrappedTopic unwrap_source_topic(const std::string& wrapped) {
    static const std::string prefix = "__r2kernel_source_v1__:";
    if (wrapped.compare(0, prefix.size(), prefix) != 0)
        throw std::runtime_error("Control topic was not source-wrapped: " + wrapped);
    const auto separator = wrapped.find(':', prefix.size());
    if (separator == std::string::npos || (separator - prefix.size()) % 2 != 0)
        throw std::runtime_error("malformed source-wrapped Control topic: " + wrapped);
    WrappedTopic out;
    for (std::size_t index = prefix.size(); index < separator; index += 2)
        out.source.push_back(static_cast<char>(std::stoi(wrapped.substr(index, 2), nullptr, 16)));
    out.topic = wrapped.substr(separator + 1);
    return out;
}

}  // namespace r2kernel_test
