// Inherited brokered socket descriptors. Cold setup-time code
// in its own translation unit; the contract is shared with the Python SDK.
#include "r2kernel/core/inherited_fds.hpp"

#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <system_error>
#include <set>
#include <string_view>
#include <utility>

#if defined(__linux__)
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#if !defined(AF_CAN)
#define AF_CAN 29
#endif
#endif

namespace r2kernel {

InheritedFdError::InheritedFdError(std::string step, std::string kind, const std::string& detail)
    : std::runtime_error("inherited fd " + kind + ": " + step + ": " + detail),
      step_(std::move(step)),
      kind_(std::move(kind)) {}

InheritedFdError::~InheritedFdError() = default;

namespace {

constexpr std::size_t kMaxKindLength = 128;
constexpr int kFirstInheritableFd = 3;

bool valid_kind(std::string_view kind) {
    if (kind.empty() || kind.size() > kMaxKindLength) return false;
    const auto alnum = [](char ch) {
        return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
    };
    if (!alnum(kind.front())) return false;
    for (const char ch : kind.substr(1)) {
        if (!alnum(ch) && ch != '_' && ch != '.' && ch != ':' && ch != '-') return false;
    }
    return true;
}

// Strict parser for the one accepted shape: a JSON object whose values are
// JSON integers. Any other JSON (or invalid JSON) is rejected; a later
// duplicate key replaces an earlier one, as in standard JSON decoders.
class InheritedFdsParser {
public:
    explicit InheritedFdsParser(std::string_view text) : text_(text) {}

    // Returns false with reason() set when the text is not a valid encoding.
    bool parse(std::map<std::string, long long>& out) {
        skip_space();
        if (!consume('{')) return fail("not a JSON object");
        skip_space();
        if (consume('}')) return finish();
        for (;;) {
            skip_space();
            std::string key;
            bool ascii_only = true;
            if (!parse_string(key, ascii_only)) return false;
            skip_space();
            if (!consume(':')) return fail("expected ':' after a kind");
            skip_space();
            long long fd = 0;
            if (!parse_integer(fd)) return false;
            out[ascii_only ? key : std::string("\x01")] = fd;  // non-ASCII never forms a valid kind
            skip_space();
            if (consume(',')) continue;
            if (consume('}')) return finish();
            return fail("expected ',' or '}'");
        }
    }

    const std::string& reason() const { return reason_; }

private:
    bool finish() {
        skip_space();
        return pos_ == text_.size() || fail("trailing data after the JSON object");
    }
    bool fail(std::string reason) {
        reason_ = std::move(reason);
        return false;
    }
    void skip_space() {
        while (pos_ < text_.size() &&
               (text_[pos_] == ' ' || text_[pos_] == '\t' || text_[pos_] == '\n' || text_[pos_] == '\r'))
            ++pos_;
    }
    bool consume(char expected) {
        if (pos_ < text_.size() && text_[pos_] == expected) {
            ++pos_;
            return true;
        }
        return false;
    }
    static int hex_value(char ch) {
        if (ch >= '0' && ch <= '9') return ch - '0';
        if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
        if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
        return -1;
    }
    bool parse_string(std::string& out, bool& ascii_only) {
        if (!consume('"')) return fail("expected a string kind");
        while (pos_ < text_.size()) {
            const auto ch = static_cast<unsigned char>(text_[pos_++]);
            if (ch == '"') return true;
            if (ch < 0x20) return fail("control character in a string");
            if (ch >= 0x80) {
                ascii_only = false;
                continue;
            }
            if (ch != '\\') {
                out.push_back(static_cast<char>(ch));
                continue;
            }
            if (pos_ >= text_.size()) break;
            const char escape = text_[pos_++];
            switch (escape) {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            case 'u': {
                if (pos_ + 4 > text_.size()) return fail("truncated \\u escape");
                int code = 0;
                for (int i = 0; i < 4; ++i) {
                    const int digit = hex_value(text_[pos_++]);
                    if (digit < 0) return fail("invalid \\u escape");
                    code = code * 16 + digit;
                }
                if (code < 0x80) out.push_back(static_cast<char>(code));
                else ascii_only = false;
                break;
            }
            default:
                return fail("invalid string escape");
            }
        }
        return fail("unterminated string");
    }
    bool parse_integer(long long& value) {
        const std::size_t start = pos_;
        bool negative = false;
        if (consume('-')) negative = true;
        if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') return fail("descriptor is not an integer");
        if (text_[pos_] == '0' && pos_ + 1 < text_.size() && text_[pos_ + 1] >= '0' && text_[pos_ + 1] <= '9')
            return fail("descriptor has a leading zero");
        unsigned long long magnitude = 0;
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
            magnitude = magnitude * 10 + static_cast<unsigned>(text_[pos_++] - '0');
            if (magnitude > static_cast<unsigned long long>(INT_MAX) + 1) return fail("descriptor out of range");
        }
        if (pos_ < text_.size() && (text_[pos_] == '.' || text_[pos_] == 'e' || text_[pos_] == 'E'))
            return fail("descriptor is not an integer");
        (void)start;
        value = negative ? -static_cast<long long>(magnitude) : static_cast<long long>(magnitude);
        return true;
    }

    std::string_view text_;
    std::size_t pos_ = 0;
    std::string reason_;
};

// Applies the encoding rules shared by the leader and child helpers.
void validate_encoding(const std::map<std::string, long long>& fds, const std::string& kind) {
    std::set<long long> seen;
    for (const auto& [name, fd] : fds) {
        if (!valid_kind(name)) throw InheritedFdError("env_invalid", kind, "invalid kind name");
        if (fd < kFirstInheritableFd || fd > INT_MAX)
            throw InheritedFdError("env_invalid", kind, "descriptor of " + name + " must be an integer >= 3");
        if (!seen.insert(fd).second)
            throw InheritedFdError("env_invalid", kind, "descriptor " + std::to_string(fd) + " is named twice");
    }
}

void check_expectation(const InheritedSocketExpectation& expect) {
    if (expect.listening && expect.type != InheritedSocketType::stream)
        throw std::invalid_argument("inherited socket expectation: listening requires type stream");
    if (expect.port && expect.family != InheritedSocketFamily::inet && expect.family != InheritedSocketFamily::inet6)
        throw std::invalid_argument("inherited socket expectation: port applies to inet/inet6 only");
    if (expect.port && (*expect.port < 0 || *expect.port > 65535))
        throw std::invalid_argument("inherited socket expectation: port must be 0..65535");
}

const char* family_name(InheritedSocketFamily family) {
    switch (family) {
    case InheritedSocketFamily::inet: return "inet";
    case InheritedSocketFamily::inet6: return "inet6";
    case InheritedSocketFamily::can: return "can";
    case InheritedSocketFamily::unix_socket: return "unix";
    }
    return "unknown";
}

const char* type_name(InheritedSocketType type) {
    switch (type) {
    case InheritedSocketType::stream: return "stream";
    case InheritedSocketType::dgram: return "dgram";
    case InheritedSocketType::raw: return "raw";
    }
    return "unknown";
}

}  // namespace

std::string encode_inherited_fds(const std::map<std::string, int>& fds) {
    std::map<std::string, long long> wide(fds.begin(), fds.end());
    validate_encoding(wide, "<encode>");
    std::string out = "{";
    bool first = true;
    for (const auto& [name, fd] : fds) {
        if (!first) out += ',';
        first = false;
        out += '"' + name + "\":" + std::to_string(fd);  // valid kinds need no JSON escaping
    }
    return out + '}';
}

#if defined(__linux__)
int inherited_socket(const std::string& kind, const InheritedSocketExpectation& expect) {
    check_expectation(expect);
    const char* raw = std::getenv(kInheritedFdsEnv);
    if (raw == nullptr) throw InheritedFdError("env_missing", kind, std::string(kInheritedFdsEnv) + " is not set");
    std::map<std::string, long long> fds;
    InheritedFdsParser parser(raw);
    if (!parser.parse(fds)) throw InheritedFdError("env_invalid", kind, parser.reason());
    validate_encoding(fds, kind);
    const auto found = fds.find(kind);
    if (found == fds.end()) throw InheritedFdError("kind_missing", kind, "kind is not listed");
    const int fd = static_cast<int>(found->second);

    struct stat status {};
    if (::fstat(fd, &status) != 0)
        throw InheritedFdError("not_socket", kind, "fstat(" + std::to_string(fd) + ") failed: " + std::strerror(errno));
    if (!S_ISSOCK(status.st_mode))
        throw InheritedFdError("not_socket", kind, "descriptor " + std::to_string(fd) + " is not a socket");

    const auto int_option = [&](int option, const char* step, const char* name) {
        int value = 0;
        socklen_t length = sizeof(value);
        if (::getsockopt(fd, SOL_SOCKET, option, &value, &length) != 0)
            throw InheritedFdError(step, kind, std::string(name) + " unavailable: " + std::strerror(errno));
        return value;
    };
    const int expected_family = expect.family == InheritedSocketFamily::inet ? AF_INET
        : expect.family == InheritedSocketFamily::inet6 ? AF_INET6
        : expect.family == InheritedSocketFamily::unix_socket ? AF_UNIX : AF_CAN;
    const int domain = int_option(SO_DOMAIN, "family_mismatch", "SO_DOMAIN");
    if (domain != expected_family)
        throw InheritedFdError("family_mismatch", kind, std::string("expected ") + family_name(expect.family)
            + ", socket domain is " + std::to_string(domain));
    const int expected_type = expect.type == InheritedSocketType::stream ? SOCK_STREAM
        : expect.type == InheritedSocketType::dgram ? SOCK_DGRAM : SOCK_RAW;
    const int type = int_option(SO_TYPE, "type_mismatch", "SO_TYPE");
    if (type != expected_type)
        throw InheritedFdError("type_mismatch", kind, std::string("expected ") + type_name(expect.type)
            + ", socket type is " + std::to_string(type));
    const bool listening = int_option(SO_ACCEPTCONN, "listening_mismatch", "SO_ACCEPTCONN") != 0;
    if (listening != expect.listening)
        throw InheritedFdError("listening_mismatch", kind,
            std::string("expected ") + (expect.listening ? "a listening" : "a non-listening") + " socket");
    if (expect.port) {
        sockaddr_storage address {};
        socklen_t length = sizeof(address);
        if (::getsockname(fd, reinterpret_cast<sockaddr*>(&address), &length) != 0)
            throw InheritedFdError("port_mismatch", kind, std::string("getsockname failed: ") + std::strerror(errno));
        const int port = address.ss_family == AF_INET
            ? ntohs(reinterpret_cast<const sockaddr_in&>(address).sin_port)
            : ntohs(reinterpret_cast<const sockaddr_in6&>(address).sin6_port);
        if (port != *expect.port)
            throw InheritedFdError("port_mismatch", kind,
                "expected port " + std::to_string(*expect.port) + ", socket is bound to " + std::to_string(port));
    }
    const int flags = ::fcntl(fd, F_GETFD);
    if (flags < 0 || ::fcntl(fd, F_SETFD, flags | FD_CLOEXEC) != 0)
        throw std::system_error(errno, std::generic_category(), "inherited fd " + kind + ": FD_CLOEXEC");
    return fd;
}
#else
int inherited_socket(const std::string& kind, const InheritedSocketExpectation& expect) {
    check_expectation(expect);
    throw InheritedFdError("not_socket", kind, "inherited sockets require Linux");
}
#endif

}  // namespace r2kernel
