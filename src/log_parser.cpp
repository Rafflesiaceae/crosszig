#include "crosszig/log_parser.hpp"

#include <boost/regex.hpp>
#include <boost/version.hpp>

namespace crosszig {

namespace {

// `<iso-timestamp> [<LEVEL>] <message>`. Built once at load time instead of on
// every call because compiling a regex is by far the expensive part.
const boost::regex& log_line_pattern() {
    static const boost::regex pattern(
        R"(^\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\[([A-Z]+)\]\s+(.*?)\s*$)");
    return pattern;
}

// `key=value` or `key="quoted value"`; the value is captured from whichever of
// the two alternatives matched.
const boost::regex& key_value_pattern() {
    static const boost::regex pattern(R"re((\w+)=(?:"([^"]*)"|(\S+)))re");
    return pattern;
}

}  // namespace

std::optional<LogEntry> parse_log_line(const std::string& line) {
    boost::smatch match;
    if (!boost::regex_match(line, match, log_line_pattern())) {
        return std::nullopt;
    }

    return LogEntry{match[1].str(), match[2].str(), match[3].str()};
}

std::vector<std::string> extract_key_values(const std::string& text) {
    std::vector<std::string> results;

    // regex_iterator walks all non-overlapping matches in one pass.
    for (boost::sregex_iterator it(text.begin(), text.end(), key_value_pattern()), end;
         it != end; ++it) {
        const boost::smatch& match = *it;
        // Group 2 is the quoted form, group 3 the bare form; exactly one matched.
        const std::string value = match[2].matched ? match[2].str() : match[3].str();
        results.push_back(match[1].str() + " -> " + value);
    }

    return results;
}

std::string boost_version() {
    // BOOST_VERSION is packed as major * 100000 + minor * 100 + patch.
    return std::to_string(BOOST_VERSION / 100000) + "." +
           std::to_string(BOOST_VERSION / 100 % 1000) + "." +
           std::to_string(BOOST_VERSION % 100);
}

}  // namespace crosszig
