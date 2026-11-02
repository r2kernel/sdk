#pragma once

// Single C++ SDK rule for timing_support values, shared by subsystem.cpp
// (endpoint options, dispatch) and contract_loader.cpp (API contract specs):
// surrounding whitespace is ignored, a blank value means "none", and any value
// other than none|optional|required is rejected with std::invalid_argument.
// The anonymous namespace keeps internal linkage in each translation unit.

#include <cctype>
#include <stdexcept>
#include <string>
#include <utility>

namespace r2kernel::wrapper::detail {
namespace {

std::string normalize_timing_support(std::string value) {
    auto begin = value.begin();
    while (begin != value.end() && std::isspace(static_cast<unsigned char>(*begin)) != 0) {
        ++begin;
    }
    auto end = value.end();
    while (end != begin && std::isspace(static_cast<unsigned char>(*(end - 1))) != 0) {
        --end;
    }
    value = std::string(begin, end);
    if (value.empty()) {
        return "none";
    }
    if (value == "none" || value == "optional" || value == "required") {
        return value;
    }
    throw std::invalid_argument("timing_support must be one of: none, optional, required");
}

}  // namespace
}  // namespace r2kernel::wrapper::detail
