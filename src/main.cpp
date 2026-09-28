#include <iostream>
#include <string>
#include <vector>

#include "crosszig/log_parser.hpp"

namespace {

// Used when the caller does not pipe anything in, so that a bare run of the
// binary still demonstrates something.
const std::vector<std::string>& sample_lines() {
    static const std::vector<std::string> lines{
        R"(2026-09-28T16:33:00 [INFO] started target=win-64 host="linux-64")",
        R"(2026-09-28T16:33:01 [WARN] retrying attempt=2 reason="connection reset")",
        R"(this line does not look like a log line at all)",
    };
    return lines;
}

void report(const std::string& line) {
    const auto entry = crosszig::parse_log_line(line);
    if (!entry) {
        std::cout << "  skipped (no match): " << line << "\n";
        return;
    }

    std::cout << "  " << entry->timestamp << " | " << entry->level << " | " << entry->message
              << "\n";

    for (const auto& pair : crosszig::extract_key_values(entry->message)) {
        std::cout << "      " << pair << "\n";
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::cout << "crosszig (Boost " << crosszig::boost_version() << ")\n";

    // Any arguments are treated as log lines; otherwise fall back to stdin, and
    // to the built-in sample when stdin is a terminal or empty.
    std::vector<std::string> lines(argv + 1, argv + argc);
    if (lines.empty()) {
        for (std::string line; std::getline(std::cin, line);) {
            lines.push_back(line);
        }
    }
    if (lines.empty()) {
        lines = sample_lines();
    }

    for (const auto& line : lines) {
        report(line);
    }

    return 0;
}
