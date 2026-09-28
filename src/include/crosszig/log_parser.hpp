#pragma once

#include <string>
#include <vector>

namespace crosszig {

/// A single log line broken up into its parts.
struct LogEntry {
    std::string timestamp;
    std::string level;
    std::string message;
};

/// Parses a line of the shape `2026-09-28T16:33:00 [LEVEL] some message` into
/// `out`. Returns false and leaves `out` untouched when the line does not match
/// that shape.
bool parse_log_line(const std::string& line, LogEntry& out);

/// Returns every `key=value` pair found in `text`, formatted as `key -> value`.
std::vector<std::string> extract_key_values(const std::string& text);

/// Version of the Boost library this was compiled against, e.g. `1.89.0`.
/// Mostly useful to prove that the cross-compiled binary really links Boost.
std::string boost_version();

}  // namespace crosszig
