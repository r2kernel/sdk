// In-process A/B micro-benchmark of the SDK JSON string escaper: the former
// core/client.cpp copy (baseline, kept here verbatim for comparison) against
// the shared src/core/json_text.hpp escaper, on representative per-call field
// values. Rounds alternate the order of A and B; each sample is the minimum
// ns/call over batches. Prints one JSON object with per-round samples.
//
// Usage: r2kernel_cpp_json_escape_benchmark [rounds] [batches] [iterations]
#include "../src/core/json_text.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

namespace {
// Former core/client_protocol.hpp json_escape, unchanged (baseline A).
std::string baseline_json_escape(std::string_view value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (const char ch : value) {
        switch (ch) {
        case '\\':
            out += "\\\\";
            break;
        case '"':
            out += "\\\"";
            break;
        case '\n':
            out += "\\n";
            break;
        case '\r':
            out += "\\r";
            break;
        case '\t':
            out += "\\t";
            break;
        default:
            out.push_back(ch);
            break;
        }
    }
    return out;
}

using Escape = std::string (*)(std::string_view);
volatile std::size_t sink = 0;

double measure(Escape escape, const std::vector<std::string>& fields, int batches, int iterations) {
    double best = 1e300;
    for (int batch = 0; batch < batches; ++batch) {
        const auto started = std::chrono::steady_clock::now();
        for (int i = 0; i < iterations; ++i)
            for (const auto& field : fields) sink = sink + escape(field).size();
        const auto elapsed = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - started).count();
        best = std::min(best, elapsed / (static_cast<double>(iterations) * fields.size()));
    }
    return best;
}
}  // namespace

int main(int argc, char** argv) {
    const int rounds = argc > 1 ? std::atoi(argv[1]) : 20;
    const int batches = argc > 2 ? std::atoi(argv[2]) : 9;
    const int iterations = argc > 3 ? std::atoi(argv[3]) : 20000;
    // Typical per-call fields: target, method/topic, request id, type ref.
    const std::vector<std::string> ascii = {"robot.arm.controller", "move_joint", "1734567890123456789",
                                            "operation/robot.arm/work/state", "msg.core.common.primitive.v1/BoolValue"};
    const std::vector<std::string> escapes = {"line1\nline2", "say \"hi\"", "C:\\path\\file", "tab\tsep",
                                              "ctl\x01\x1f end"};
    const Escape candidate = &r2kernel::sdk_detail::json_escape;
    std::cout << "{\"rounds\":[";
    for (int round = 0; round < rounds; ++round) {
        double a_ascii, b_ascii, a_esc, b_esc;
        if (round % 2 == 0) {
            a_ascii = measure(baseline_json_escape, ascii, batches, iterations);
            b_ascii = measure(candidate, ascii, batches, iterations);
            a_esc = measure(baseline_json_escape, escapes, batches, iterations);
            b_esc = measure(candidate, escapes, batches, iterations);
        } else {
            b_ascii = measure(candidate, ascii, batches, iterations);
            a_ascii = measure(baseline_json_escape, ascii, batches, iterations);
            b_esc = measure(candidate, escapes, batches, iterations);
            a_esc = measure(baseline_json_escape, escapes, batches, iterations);
        }
        std::cout << (round ? "," : "") << "{\"A\":{\"ascii\":" << a_ascii << ",\"escapes\":" << a_esc
                  << "},\"B\":{\"ascii\":" << b_ascii << ",\"escapes\":" << b_esc << "}}";
    }
    std::cout << "]}\n";
}
