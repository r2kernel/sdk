// Included in client.cpp's anonymous namespace. Wire constants plus the
// environment, JSON, payload-lease and binary-frame helpers of the local
// control protocol. Kept in client.cpp's translation unit so per-call
// encode/decode helpers retain same-TU inlining.

constexpr const char* kCommand = "cmd";
constexpr const char* kResult = "res";
constexpr const char* kOk = "ok";
constexpr const char* kRegisterSubsystem = "register_subsystem";
constexpr const char* kRegisterControlHandler = "register_control_handler";
constexpr const char* kResolveControlTarget = "resolve_control_target";
constexpr const char* kClockStatus = "clock_status";
constexpr const char* kRequestPublishSlots = "request_publish_slots";
constexpr const char* kRegisterSubscription = "register_subscription";
constexpr const char* kNotifyPublisher = "notify_publisher";
constexpr const char* kNotifySubscriber = "notify_subscriber";
constexpr const char* kLifecycleTransition = "lifecycle_transition";
constexpr const char* kInformSubsystemDown = "inform_subsystem_down";
constexpr const char* kSubsystem = "subsystem";
constexpr const char* kPublisher = "publisher";
constexpr const char* kTopic = "topic";
constexpr const char* kLocalControlEndpoint = "control_local_endpoint";
constexpr const char* kLocalControlProtocol = "local_protocol";
constexpr const char* kControlPortKey = "control_port";
constexpr const char* kPoolName = "topic_buffer_pool_name";
constexpr const char* kPoolSlotSize = "topic_buffer_pool_slot_size";
constexpr const char* kPoolSlotCount = "topic_buffer_pool_slot_count";
constexpr const char* kPublishSlotIndex = "publish_slot_index";
constexpr const char* kQueueDepth = "queue_depth";
constexpr const char* kEntrySlotCount = "entry_slot_count";
constexpr const char* kPublishers = "publishers";
constexpr const char* kSubscribers = "subscribers";
constexpr const char* kLockName = "lock_name";
constexpr const char* kInternal = "internal";
constexpr const char* kEventStartupBarrierMs = "event_startup_barrier_ms";
constexpr const char* kEventInlineEndpoint = "event_inline_endpoint";
constexpr const char* kEventInlinePayloadThreshold = "event_inline_payload_threshold";
constexpr const char* kClockSource = "clock_source";
constexpr const char* kClockOffsetNs = "clock_offset_ns";
constexpr const char* kClockCorrectionNs = "correction_ns";
constexpr const char* kClockCorrectionNsTopLevel = "clock_correction_ns";
constexpr const char* kClockCorrectionApplied = "correction_applied";
constexpr const char* kClockCorrectionAppliedTopLevel = "clock_correction_applied";
constexpr const char* kClockRttNs = "clock_rtt_ns";
constexpr const char* kClockUncertaintyNs = "clock_uncertainty_ns";
constexpr const char* kClockStable = "clock_stable";
constexpr const char* kClockAcceptedForOneWay = "clock_accepted_for_one_way";
constexpr std::size_t kControlMessageBufferCap = 16 * 1024 * 1024;
constexpr std::size_t kPayloadOffset = 64;
constexpr char kEventInlineMagic[] = {'S', 'E', 'V', 'T'};
constexpr std::uint8_t kEventInlineVersion = 2;
constexpr std::uint8_t kEventInlineKindPublishReq = 1;
constexpr std::uint8_t kEventInlineKindPublishResp = 2;
constexpr std::uint8_t kEventInlineKindDeliverReq = 3;
constexpr std::uint8_t kEventInlineKindDeliverResp = 4;
constexpr char kLocalControlMagic[] = {'S', 'R', 'L', 'C'};
constexpr std::uint8_t kLocalControlVersion = 2;
constexpr std::uint8_t kLocalControlKindHandshakeReq = 1;
constexpr std::uint8_t kLocalControlKindHandshakeResp = 2;
constexpr std::uint8_t kLocalControlKindTransactReq = 3;
constexpr std::uint8_t kLocalControlKindTransactResp = 4;
constexpr std::uint8_t kLocalControlKindResponseConsumed = 5;
constexpr std::uint8_t kLocalControlKindResponseReleased = 6;
constexpr std::uint8_t kLocalControlKindPrepareReq = 7;
constexpr std::uint8_t kLocalControlKindPrepareResp = 8;
constexpr std::uint8_t kLocalControlKindHandlerReq = 11;
constexpr std::uint8_t kLocalControlKindHandlerResp = 12;
constexpr std::uint8_t kLocalControlKindHandlerPrepareReq = 13;
constexpr std::uint8_t kLocalControlKindHandlerPrepareResp = 14;
constexpr std::uint8_t kLocalControlKindError = 255;
constexpr std::uint8_t kControlPriorityNormal = 128;
constexpr std::uint8_t kControlPriorityHigh = 192;
constexpr std::uint8_t kControlPriorityCritical = 255;

#if defined(_WIN32)
using SocketHandle = SOCKET;
constexpr SocketHandle kInvalidSocket = INVALID_SOCKET;
#else
using SocketHandle = int;
constexpr SocketHandle kInvalidSocket = -1;
#endif

void close_socket(SocketHandle sock) {
    if (sock == kInvalidSocket) {
        return;
    }
#if defined(_WIN32)
    closesocket(sock);
#else
    close(sock);
#endif
}

std::uint8_t default_control_priority(const std::string& topic, int explicit_priority) {
    if (explicit_priority >= 0) {
        return static_cast<std::uint8_t>(std::clamp(explicit_priority, 0, 255));
    }
    if (topic == "control_hello") {
        return kControlPriorityCritical;
    }
    if (topic == "operation.cancel" || topic == "operation.start") {
        return kControlPriorityHigh;
    }
    if (topic == "operation.query") {
        return static_cast<std::uint8_t>(kControlPriorityNormal + 16);
    }
    if (topic == "procedure.call") {
        return kControlPriorityNormal;
    }
    return kControlPriorityNormal;
}

int env_int_value(const char* name, int fallback) {
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0') {
        return fallback;
    }
    char* end = nullptr;
    const long parsed = std::strtol(raw, &end, 10);
    if (end == raw || (end != nullptr && *end != '\0')) {
        return fallback;
    }
    return static_cast<int>(std::max<long>(0, parsed));
}

std::optional<std::string> env_string_value(const char* name) {
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0') {
        return std::nullopt;
    }
    return std::string(raw);
}

int current_pid() {
#if defined(_WIN32)
    return 0;
#else
    return static_cast<int>(::getpid());
#endif
}

std::string make_session_id(const std::string& subsystem_name) {
    return subsystem_name + "-" + std::to_string(current_pid()) + "-" +
           std::to_string(r2kernel_clock_now_ns());
}

std::size_t event_queue_depth_for_size(std::size_t size, std::size_t explicit_depth = 0) {
    if (explicit_depth > 0) {
        return explicit_depth;
    }
    const int env_depth = env_int_value("R2KERNEL_EVENT_QUEUE_DEPTH", -1);
    if (env_depth > 0) {
        return static_cast<std::size_t>(env_depth);
    }
    if (size <= 4U * 1024U) {
        return 64;
    }
    if (size <= 64U * 1024U) {
        return 16;
    }
    return 4;
}

std::size_t entry_slot_count_for_size(std::size_t slot_size, std::size_t size) {
    const std::size_t safe_slot_size = std::max<std::size_t>(slot_size, 1);
    return std::max<std::size_t>(1, (size + kPayloadOffset + safe_slot_size - 1) / safe_slot_size);
}

std::string control_socket_path() {
    if (const auto value = env_string_value(sdk_environment::kControlSocketEnv)) {
        return *value;
    }
    return sdk_environment::kDefaultControlSocketPath;
}

std::string dirname_of(const std::string& path) {
    const auto pos = path.find_last_of('/');
    if (pos == std::string::npos || pos == 0) {
        return pos == 0 ? "/" : ".";
    }
    return path.substr(0, pos);
}

std::string notify_base_dir() {
    if (const auto value = env_string_value("R2KERNEL_CONTROL_NOTIFY_DIR")) {
        return *value;
    }
    const auto base = dirname_of(control_socket_path());
    if (::access(base.c_str(), W_OK) == 0) {
        return base;
    }
    return "/tmp";
}

std::string make_notify_endpoint() {
    auto base = notify_base_dir();
    if (base.empty()) {
        base = "/tmp";
    }
    return base + "/r2kernel-notify-" + std::to_string(current_pid()) + "-" + std::to_string(r2kernel_clock_now_ns()) + ".sock";
}

std::string trimmed_env_text(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    return value.substr(begin, value.find_last_not_of(" \t\r\n") - begin + 1);
}

// R2KERNEL_RUNTIME_IPC_SOCKET_MODE: octal mode of the local Control handler
// socket, 0660 when unset or blank (Python _control_server.runtime_ipc_socket_mode).
// One to four octal digits; anything else throws std::invalid_argument.
mode_t control_handler_socket_mode() {
    const char* raw = std::getenv("R2KERNEL_RUNTIME_IPC_SOCKET_MODE");
    const std::string text = trimmed_env_text(raw != nullptr ? raw : "");
    if (text.empty()) return 0660;
    if (text.size() > 4 || !std::all_of(text.begin(), text.end(), [](char ch) { return ch >= '0' && ch <= '7'; }))
        throw std::invalid_argument("R2KERNEL_RUNTIME_IPC_SOCKET_MODE must be an octal file mode: " + text);
    return static_cast<mode_t>(std::strtoul(text.c_str(), nullptr, 8));
}

// IPC policy of the local Control handler socket (Python
// _control_server._apply_local_ipc_socket_policy): the group named by
// R2KERNEL_RUNTIME_IPC_GROUP (default r2kernel; blank disables it) owns the
// socket when that group exists, then the socket gets
// R2KERNEL_RUNTIME_IPC_SOCKET_MODE. A failing chown or chmod throws.
void apply_control_handler_socket_policy(const std::string& endpoint) {
    const char* configured = std::getenv("R2KERNEL_RUNTIME_IPC_GROUP");
    const std::string group = trimmed_env_text(configured != nullptr ? configured : "r2kernel");
    if (!group.empty()) {
        struct group entry {};
        struct group* resolved = nullptr;
        std::array<char, 16384> buffer {};
        if (::getgrnam_r(group.c_str(), &entry, buffer.data(), buffer.size(), &resolved) == 0 && resolved != nullptr
            && ::chown(endpoint.c_str(), static_cast<uid_t>(-1), entry.gr_gid) != 0) {
            throw std::runtime_error("Failed to give the Control handler socket to IPC group " + group + ": "
                                     + std::strerror(errno));
        }
    }
    if (::chmod(endpoint.c_str(), control_handler_socket_mode()) != 0)
        throw std::runtime_error("Failed to set the Control handler socket mode: " + std::string(std::strerror(errno)));
}

// Notification and Event sockets (Python event_client): the configured
// R2KERNEL_RUNTIME_IPC_GROUP (name or numeric ID) owns the socket with mode
// 0660; without a group the mode is 0660, or 0666 in /tmp.
void publish_notify_socket_permissions(const std::string& endpoint) {
    const char* ipc_group = std::getenv("R2KERNEL_RUNTIME_IPC_GROUP");
    bool permissions_ok = true;
    if (ipc_group != nullptr) {
        const std::string group = trimmed_env_text(ipc_group);
        if (group.empty()) {
            permissions_ok = false;
        } else {
            gid_t gid = 0;
            if (std::all_of(group.begin(), group.end(), [](char value) { return value >= '0' && value <= '9'; })) {
                errno = 0;
                const auto parsed = std::strtoull(group.c_str(), nullptr, 10);
                permissions_ok = errno == 0 && parsed <= std::numeric_limits<gid_t>::max();
                gid = static_cast<gid_t>(parsed);
            } else {
                struct group entry {};
                struct group* resolved = nullptr;
                std::array<char, 16384> buffer {};
                permissions_ok = ::getgrnam_r(group.c_str(), &entry, buffer.data(), buffer.size(), &resolved) == 0 && resolved != nullptr;
                if (permissions_ok) gid = entry.gr_gid;
            }
            permissions_ok = permissions_ok && ::chown(endpoint.c_str(), static_cast<uid_t>(-1), gid) == 0;
        }
    }
    const mode_t mode = ipc_group != nullptr || dirname_of(endpoint) != "/tmp" ? 0660 : 0666;
    if (!permissions_ok || ::chmod(endpoint.c_str(), mode) != 0) {
        throw std::runtime_error("Failed to publish Unix notification socket with configured IPC permissions");
    }
}

// Binds and listens on a Unix socket after publish_permissions set its
// ownership and mode; the default suits notification and Event sockets.
SocketHandle create_unix_listener(const std::string& endpoint,
                                  void (*publish_permissions)(const std::string&) = publish_notify_socket_permissions) {
    const SocketHandle sock = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock == kInvalidSocket) {
        throw std::runtime_error("Failed to create Unix notify socket");
    }
    (void)::unlink(endpoint.c_str());
    sockaddr_un addr {};
    addr.sun_family = AF_UNIX;
    if (endpoint.size() >= sizeof(addr.sun_path)) {
        close_socket(sock);
        throw std::runtime_error("Unix notify socket path is too long: " + endpoint);
    }
    std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", endpoint.c_str());
    if (::bind(sock, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) != 0) {
        close_socket(sock);
        throw std::runtime_error("Failed to bind Unix notify socket: " + endpoint);
    }
    try {
        publish_permissions(endpoint);
        if (::listen(sock, 16) != 0) {
            throw std::runtime_error("Failed to listen on Unix socket: " + endpoint);
        }
    } catch (...) {
        close_socket(sock);
        (void)::unlink(endpoint.c_str());
        throw;
    }
    return sock;
}

using r2kernel::sdk_detail::json_escape;


std::string make_json_request(std::string_view body) {
    return "{" + std::string(body) + "}";
}

std::optional<std::string> json_string_field(const std::string& text, std::string_view key) {
    const std::string pattern = "\"" + std::string(key) + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return std::nullopt;
    }
    const std::size_t colon = text.find(':', key_pos + pattern.size());
    if (colon == std::string::npos) {
        return std::nullopt;
    }
    // Only a string value is a string: a null (e.g. a lifecycle request without
    // a reason) or a number must not yield the next member's name.
    std::size_t quote = colon + 1;
    while (quote < text.size() && std::isspace(static_cast<unsigned char>(text[quote]))) {
        ++quote;
    }
    if (quote >= text.size() || text[quote] != '"') {
        return std::nullopt;
    }
    std::string value;
    for (std::size_t i = quote + 1; i < text.size(); ++i) {
        const char ch = text[i];
        if (ch == '\\') {
            if (i + 1 < text.size()) {
                value.push_back(text[i + 1]);
                ++i;
            }
            continue;
        }
        if (ch == '"') {
            return value;
        }
        value.push_back(ch);
    }
    return std::nullopt;
}

std::optional<long long> json_int_field(const std::string& text, std::string_view key) {
    const std::string pattern = "\"" + std::string(key) + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return std::nullopt;
    }
    const std::size_t colon = text.find(':', key_pos + pattern.size());
    if (colon == std::string::npos) {
        return std::nullopt;
    }
    std::size_t pos = colon + 1;
    while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos]))) {
        ++pos;
    }
    const std::size_t start = pos;
    if (pos < text.size() && (text[pos] == '-' || text[pos] == '+')) {
        ++pos;
    }
    while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) {
        ++pos;
    }
    if (pos == start || (pos == start + 1 && (text[start] == '-' || text[start] == '+'))) {
        return std::nullopt;
    }
    try {
        return std::stoll(text.substr(start, pos - start));
    } catch (...) {
        return std::nullopt;
    }
}

std::optional<bool> json_bool_field(const std::string& text, std::string_view key) {
    const std::string pattern = "\"" + std::string(key) + "\"";
    const std::size_t key_pos = text.find(pattern);
    if (key_pos == std::string::npos) {
        return std::nullopt;
    }
    const std::size_t colon = text.find(':', key_pos + pattern.size());
    if (colon == std::string::npos) {
        return std::nullopt;
    }
    std::size_t pos = colon + 1;
    while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos]))) {
        ++pos;
    }
    if (text.compare(pos, 4, "true") == 0) {
        return true;
    }
    if (text.compare(pos, 5, "false") == 0) {
        return false;
    }
    return std::nullopt;
}

std::uint8_t clock_source_code(const std::string& source) {
    if (source == "local_system") {
        return R2KERNEL_CLOCK_SOURCE_LOCAL_SYSTEM;
    }
    if (source == "software_ptp") {
        return R2KERNEL_CLOCK_SOURCE_SOFTWARE_PTP;
    }
    if (source == "hardware_ptp") {
        return R2KERNEL_CLOCK_SOURCE_HARDWARE_PTP;
    }
    if (source == "r2kernel_logical") {
        return R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL;
    }
    if (source == "r2kernel_logical_hw") {
        return R2KERNEL_CLOCK_SOURCE_R2KERNEL_LOGICAL_HW;
    }
    return R2KERNEL_CLOCK_SOURCE_LOCAL_UNSYNCED;
}

void apply_clock_status_from_registration(const std::string& response) {
    const auto source = json_string_field(response, kClockSource).value_or("local_unsynced");
    const auto fallback_offset_ns = json_int_field(response, kClockOffsetNs).value_or(0);
    auto correction_ns_value = json_int_field(response, kClockCorrectionNs);
    if (!correction_ns_value.has_value()) {
        correction_ns_value = json_int_field(response, kClockCorrectionNsTopLevel);
    }
    const auto correction_ns = correction_ns_value.value_or(fallback_offset_ns);
    auto correction_applied_value = json_bool_field(response, kClockCorrectionApplied);
    if (!correction_applied_value.has_value()) {
        correction_applied_value = json_bool_field(response, kClockCorrectionAppliedTopLevel);
    }
    const auto correction_applied =
        correction_applied_value.value_or(source == "r2kernel_logical" || source == "r2kernel_logical_hw");
    const auto applied_offset_ns = correction_applied ? correction_ns : 0;
    (void)r2kernel_clock_configure(
        applied_offset_ns,
        json_int_field(response, kClockRttNs).value_or(-1),
        json_int_field(response, kClockUncertaintyNs).value_or(-1),
        clock_source_code(source),
        json_bool_field(response, kClockStable).value_or(false) ? 1 : 0,
        json_bool_field(response, kClockAcceptedForOneWay).value_or(false) ? 1 : 0
    );
}

// Named shared-memory region slot carrying a Control SHM payload descriptor.
struct LocalPayloadLease {
    std::string region_name;
    std::size_t slot_index = 0;
    std::size_t slot_size = 0;
    std::size_t slot_count = 1;
    std::size_t length = 0;
};

void append_u8(std::string& out, std::uint8_t value) {
    out.push_back(static_cast<char>(value));
}

void append_u16(std::string& out, std::uint16_t value) {
    out.append(reinterpret_cast<const char*>(&value), sizeof(value));
}

void append_u32(std::string& out, std::uint32_t value) {
    out.append(reinterpret_cast<const char*>(&value), sizeof(value));
}


void append_i64(std::string& out, std::int64_t value) {
    out.append(reinterpret_cast<const char*>(&value), sizeof(value));
}

void append_bytes(std::string& out, const std::uint8_t* data, std::size_t size) {
    append_u32(out, static_cast<std::uint32_t>(std::min<std::size_t>(size, std::numeric_limits<std::uint32_t>::max())));
    if (size > 0) {
        out.append(reinterpret_cast<const char*>(data), size);
    }
}

void append_string(std::string& out, const std::string& value) {
    append_bytes(out, reinterpret_cast<const std::uint8_t*>(value.data()), value.size());
}

void append_header(std::string& out, std::uint8_t kind) {
    out.append(kLocalControlMagic, sizeof(kLocalControlMagic));
    append_u8(out, kLocalControlVersion);
    append_u8(out, kind);
}

void append_event_inline_header(std::string& out, std::uint8_t kind) {
    out.append(kEventInlineMagic, sizeof(kEventInlineMagic));
    append_u8(out, kEventInlineVersion);
    append_u8(out, kind);
}

bool read_exact(const std::string& data, std::size_t& pos, void* out, std::size_t size) {
    if (pos + size > data.size()) {
        return false;
    }
    std::memcpy(out, data.data() + pos, size);
    pos += size;
    return true;
}

std::optional<std::uint8_t> read_u8(const std::string& data, std::size_t& pos) {
    std::uint8_t value = 0;
    if (!read_exact(data, pos, &value, sizeof(value))) {
        return std::nullopt;
    }
    return value;
}

std::optional<std::uint32_t> read_u32(const std::string& data, std::size_t& pos) {
    std::uint32_t value = 0;
    if (!read_exact(data, pos, &value, sizeof(value))) {
        return std::nullopt;
    }
    return value;
}


std::optional<std::int64_t> read_i64_binary(const std::string& data, std::size_t& pos) {
    std::int64_t value = 0;
    if (!read_exact(data, pos, &value, sizeof(value))) {
        return std::nullopt;
    }
    return value;
}

std::optional<std::string> read_string_binary(const std::string& data, std::size_t& pos) {
    const auto len = read_u32(data, pos);
    if (!len || pos + *len > data.size()) {
        return std::nullopt;
    }
    std::string value(data.data() + pos, data.data() + pos + *len);
    pos += *len;
    return value;
}

std::optional<std::vector<std::uint8_t>> read_bytes_binary(const std::string& data, std::size_t& pos) {
    const auto len = read_u32(data, pos);
    if (!len || pos + *len > data.size()) {
        return std::nullopt;
    }
    std::vector<std::uint8_t> value(*len);
    if (*len > 0) {
        std::memcpy(value.data(), data.data() + pos, *len);
    }
    pos += *len;
    return value;
}

// Cold rejection of a Unix endpoint that does not fit sockaddr_un::sun_path
// (it would otherwise be truncated to another path): closes fd and throws.
// Called only from the connect branch, never per message.
[[noreturn]]
#if defined(__GNUC__)
__attribute__((cold, noinline))
#endif
void reject_unix_endpoint(int fd, const char* purpose, const std::string& endpoint) {
    ::close(fd);
    throw std::invalid_argument(std::string(purpose) + " endpoint exceeds the Unix socket path limit ("
        + std::to_string(sizeof(sockaddr_un::sun_path) - 1) + " bytes): " + endpoint);
}
