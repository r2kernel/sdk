#include "control_source_topic.hpp"

#include <string>
#include <utility>
#include <vector>

int main() {
    using r2kernel::core::detail::encode_source_topic;

    if (encode_source_topic("component.owner", "servo_joint") !=
        "__r2kernel_source_v1__:636f6d706f6e656e742e6f776e6572:servo_joint") {
        return 1;
    }
    if (encode_source_topic("component.팔", "arm:servo") !=
        "__r2kernel_source_v1__:636f6d706f6e656e742eed8c94:arm:servo") {
        return 2;
    }

    const std::vector<std::pair<std::string, std::string>> invalid = {
        {"", "topic"},
        {"__r2kernel_authority_v1__:fake", "topic"},
        {"__r2kernel_host_v1__:fake", "topic"},
        {std::string(129, 's'), "topic"},
        {std::string("source\0name", 11), "topic"},
        {"source", ""},
        {"source", std::string(1025, 't')},
        {"source", std::string("topic\0tail", 10)},
        {"source", "__r2kernel_source_v2__:61:topic"},
        {std::string("\xc0\xaf", 2), "topic"},
    };
    for (const auto& [source, topic] : invalid) {
        bool rejected = false;
        try {
            (void)encode_source_topic(source, topic);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        if (!rejected) return 3;
    }
    return 0;
}
