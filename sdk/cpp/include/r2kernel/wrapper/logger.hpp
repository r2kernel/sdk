#pragma once

#include <functional>
#include <string>
#include <string_view>

#include "r2kernel/export.hpp"

namespace r2kernel::wrapper {

class R2KERNEL_CPP_API Logger {
public:
    using Sink = std::function<void(std::string_view level,
                                    std::string_view logger_name,
                                    std::string_view tag,
                                    std::string_view message)>;

    explicit Logger(std::string name = {}, Sink sink = {});
    Logger(std::string name, std::string level, Sink sink = {});

    void debug(std::string_view tag, std::string_view message) const;
    void info(std::string_view tag, std::string_view message) const;
    void warning(std::string_view tag, std::string_view message) const;
    void error(std::string_view tag, std::string_view message) const;

    const std::string& name() const noexcept { return name_; }
    const std::string& level() const noexcept { return level_; }
    void set_level(std::string_view level);

private:
    void emit(std::string_view level, std::string_view tag, std::string_view message) const;

    std::string name_;
    std::string level_;
    int min_level_rank_ = 1;
    Sink sink_;
};

}  // namespace r2kernel::wrapper
