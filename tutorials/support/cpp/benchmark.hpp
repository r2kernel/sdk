#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <numeric>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "r2kernel/wrapper/core_client.hpp"
#include "r2kernel_core.h"

namespace r2kernel::tutorial {

struct Summary {
    std::size_t samples = 0;
    double min_us = 0.0;
    double avg_us = 0.0;
    double p50_us = 0.0;
    double p95_us = 0.0;
    double p99_us = 0.0;
    double max_us = 0.0;
};

class LatencyStats {
public:
    void add_us(double value_us) {
        samples_.push_back(value_us);
    }

    std::size_t samples() const noexcept {
        return samples_.size();
    }

    Summary summary() const {
        if (samples_.empty()) {
            return {};
        }
        std::vector<double> ordered = samples_;
        std::sort(ordered.begin(), ordered.end());
        const double total = std::accumulate(ordered.begin(), ordered.end(), 0.0);
        return Summary {
            ordered.size(),
            ordered.front(),
            total / static_cast<double>(ordered.size()),
            percentile(ordered, 50.0),
            percentile(ordered, 95.0),
            percentile(ordered, 99.0),
            ordered.back(),
        };
    }

private:
    static double percentile(const std::vector<double>& ordered, double pct) {
        if (ordered.empty()) {
            return 0.0;
        }
        if (ordered.size() == 1) {
            return ordered.front();
        }
        const double rank = (pct / 100.0) * static_cast<double>(ordered.size() - 1);
        const auto lo = static_cast<std::size_t>(rank);
        const auto hi = std::min(lo + 1, ordered.size() - 1);
        const double frac = rank - static_cast<double>(lo);
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac;
    }

    std::vector<double> samples_;
};

inline std::string topic_name(const std::string& prefix, int topic_count, int index) {
    if (topic_count <= 1) {
        return prefix;
    }
    return prefix + std::to_string(index);
}

inline r2kernel::wrapper::ByteBuffer make_payload(std::uint64_t seq, std::size_t payload_size) {
    const std::size_t size = std::max<std::size_t>(payload_size, 8U);
    r2kernel::wrapper::ByteBuffer payload(size, static_cast<std::uint8_t>(seq & 0xFFU));
    for (int i = 0; i < 8; ++i) {
        payload[static_cast<std::size_t>(i)] = static_cast<std::uint8_t>((seq >> (56 - i * 8)) & 0xFFU);
    }
    return payload;
}

inline std::size_t effective_payload_size(std::size_t payload_size) {
    return std::max<std::size_t>(payload_size, 8U);
}

inline std::uint64_t payload_seq(const r2kernel::wrapper::ByteBuffer& payload) {
    if (payload.size() < 8) {
        return 0;
    }
    std::uint64_t seq = 0;
    for (int i = 0; i < 8; ++i) {
        seq = (seq << 8) | static_cast<std::uint64_t>(payload[static_cast<std::size_t>(i)]);
    }
    return seq;
}

inline const char* clock_source_name(std::uint8_t source) {
    switch (source) {
        case R2KERNEL_CLOCK_SOURCE_LOCAL_SYSTEM: return "local_system";
        case R2KERNEL_CLOCK_SOURCE_SOFTWARE_PTP: return "software_ptp";
        case R2KERNEL_CLOCK_SOURCE_HARDWARE_PTP: return "hardware_ptp";
        case R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL: return "r2kernel_logical";
        case R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL_HW: return "r2kernel_logical_hw";
        case R2KERNEL_CLOCK_SOURCE_LOCAL_UNSYNCED:
        default: return "local_unsynced";
    }
}

inline std::string render_text_report(const std::string& kind,
                                      const std::string& role,
                                      const Summary& summary,
                                      std::optional<std::size_t> failures,
                                      std::optional<std::size_t> dropped,
                                      const std::vector<std::pair<std::string, std::string>>& extra) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(3);
    out << "report.kind=" << kind << "\n";
    out << "report.role=" << role << "\n";
    out << "report.samples=" << summary.samples << "\n";
    if (failures.has_value()) {
        out << "report.failures=" << *failures << "\n";
    }
    if (dropped.has_value()) {
        out << "report.dropped=" << *dropped << "\n";
    }
    out << "report.min_us=" << summary.min_us << "\n";
    out << "report.avg_us=" << summary.avg_us << "\n";
    out << "report.p50_us=" << summary.p50_us << "\n";
    out << "report.p95_us=" << summary.p95_us << "\n";
    out << "report.p99_us=" << summary.p99_us << "\n";
    out << "report.max_us=" << summary.max_us;
    for (const auto& [key, value] : extra) {
        out << "\nreport.extra." << key << "=" << value;
    }
    R2KernelClockSnapshot clock {};
    if (r2kernel_clock_snapshot(&clock) == 0) {
        out << "\nreport.clock.source=" << clock_source_name(clock.source);
        out << "\nreport.clock.source_code=" << static_cast<int>(clock.source);
        out << "\nreport.clock.offset_ns=" << clock.offset_ns;
        out << "\nreport.clock.rtt_ns=" << clock.rtt_ns;
        out << "\nreport.clock.uncertainty_ns=" << clock.uncertainty_ns;
        out << "\nreport.clock.stable=" << (clock.stable != 0 ? "true" : "false");
        out << "\nreport.clock.accepted_for_one_way="
            << (clock.accepted_for_one_way != 0 ? "true" : "false");
    }
    return out.str();
}

inline std::string render_json_report(const std::string& kind,
                                      const std::string& role,
                                      const Summary& summary,
                                      std::optional<std::size_t> failures,
                                      std::optional<std::size_t> dropped,
                                      const std::vector<std::pair<std::string, std::string>>& extra) {
    auto escape = [](const std::string& value) {
        std::string out;
        out.reserve(value.size());
        for (char c : value) {
            if (c == '\\' || c == '"') {
                out.push_back('\\');
            }
            out.push_back(c);
        }
        return out;
    };

    std::ostringstream out;
    out << std::fixed << std::setprecision(3);
    out << "{\n";
    out << "  \"kind\": \"" << escape(kind) << "\",\n";
    out << "  \"role\": \"" << escape(role) << "\",\n";
    out << "  \"samples\": " << summary.samples << ",\n";
    if (failures.has_value()) {
        out << "  \"failures\": " << *failures << ",\n";
    }
    if (dropped.has_value()) {
        out << "  \"dropped\": " << *dropped << ",\n";
    }
    out << "  \"min_us\": " << summary.min_us << ",\n";
    out << "  \"avg_us\": " << summary.avg_us << ",\n";
    out << "  \"p50_us\": " << summary.p50_us << ",\n";
    out << "  \"p95_us\": " << summary.p95_us << ",\n";
    out << "  \"p99_us\": " << summary.p99_us << ",\n";
    out << "  \"max_us\": " << summary.max_us << ",\n";
    R2KernelClockSnapshot clock {};
    if (r2kernel_clock_snapshot(&clock) == 0) {
        out << "  \"clock\": {";
        out << "\"source\": \"" << clock_source_name(clock.source) << "\", ";
        out << "\"source_code\": " << static_cast<int>(clock.source) << ", ";
        out << "\"offset_ns\": " << clock.offset_ns << ", ";
        out << "\"rtt_ns\": " << clock.rtt_ns << ", ";
        out << "\"uncertainty_ns\": " << clock.uncertainty_ns << ", ";
        out << "\"stable\": " << (clock.stable != 0 ? "true" : "false") << ", ";
        out << "\"accepted_for_one_way\": "
            << (clock.accepted_for_one_way != 0 ? "true" : "false");
        out << "},\n";
    }
    out << "  \"extra\": {";
    for (std::size_t i = 0; i < extra.size(); ++i) {
        if (i > 0) {
            out << ", ";
        }
        out << "\"" << escape(extra[i].first) << "\": \"" << escape(extra[i].second) << "\"";
    }
    out << "}\n}";
    return out.str();
}

inline void maybe_write_report_file(const std::string& path, const std::string& content) {
    if (path.empty()) {
        return;
    }
    std::ofstream out(path);
    out << content << '\n';
}

}  // namespace r2kernel::tutorial
