// Private lane storage. Only owned mappings are locked/unlocked; heap pages are
// never passed to munlock. Public response payload ownership remains unchanged.
class ControlShmPreparedMemory {
public:
    static constexpr std::size_t capacity = 64U * 1024U;
    static constexpr std::size_t buffer_count = 4;
    explicit ControlShmPreparedMemory(ControlShmMetrics& metrics) : metrics_(metrics) {
        metrics_.reserve_prepared_bytes(capacity * buffer_count);
        mapping_ = ::mmap(nullptr, capacity * buffer_count, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (mapping_ == MAP_FAILED) {
            metrics_.release_prepared_bytes(capacity * buffer_count);
            throw std::runtime_error("Control prepared memory allocation failed");
        }
        std::memset(mapping_, 0, capacity * buffer_count);
    }
    ~ControlShmPreparedMemory() {
        if (locked_) ::munlock(mapping_, capacity * buffer_count);
        ::munmap(mapping_, capacity * buffer_count);
        metrics_.release_prepared_bytes(capacity * buffer_count);
    }
    ControlShmPreparedMemory(const ControlShmPreparedMemory&) = delete;
    ControlShmPreparedMemory& operator=(const ControlShmPreparedMemory&) = delete;
    void prepare(bool required_lock) {
        if (required_lock && !locked_) {
            if (::mlock(mapping_, capacity * buffer_count) != 0) {
                // The kernel may have locked a prefix before reporting failure.
                ::munlock(mapping_, capacity * buffer_count);
                throw std::runtime_error("Control prepared metadata strict mlock failed");
            }
            locked_ = true;
        }
    }
    char* buffer(std::size_t index) { return static_cast<char*>(mapping_) + index * capacity; }
private:
    ControlShmMetrics& metrics_;
    void* mapping_ = MAP_FAILED;
    bool locked_ = false;
};

bool control_shm_caller_requires_lock() {
    int policy = 0;
    sched_param parameters{};
    if (::pthread_getschedparam(::pthread_self(), &policy, &parameters) != 0)
        throw std::runtime_error("Control caller scheduling policy query failed");
    return policy == SCHED_FIFO || policy == SCHED_RR;
}

class ControlShmMetadataWriter {
public:
    explicit ControlShmMetadataWriter(char* buffer) : buffer_(buffer) {}
    void bytes(std::string_view value) {
        if (value.size() > ControlShmPreparedMemory::capacity - size_)
            throw ControlShmProtocolError("Control prepared metadata capacity exceeded");
        std::memcpy(buffer_ + size_, value.data(), value.size());
        size_ += value.size();
    }
    template<class Scalar> void scalar(Scalar value) {
        bytes(std::string_view(reinterpret_cast<const char*>(&value), sizeof(value)));
    }
    void ticket(std::uint64_t value) {
        for (std::size_t index = 0; index < sizeof(value); ++index)
            scalar(static_cast<std::uint8_t>((value >> (index * 8)) & 255U));
    }
    void text(std::string_view value) {
        scalar(static_cast<std::uint32_t>(value.size())); bytes(value);
    }
    void header(std::uint8_t kind) {
        bytes(std::string_view(kLocalControlMagic, 4));
        scalar(kControlShmVersion); scalar(kind);
    }
    void lease(std::string_view token, std::size_t capacity, std::size_t length) {
        scalar(std::uint8_t{1}); text(token);
        scalar(std::uint32_t{0}); scalar(static_cast<std::uint32_t>(capacity));
        scalar(std::uint32_t{1}); scalar(static_cast<std::uint32_t>(length));
    }
    std::string_view view() const { return {buffer_, size_}; }
private:
    char* buffer_;
    std::size_t size_ = 0;
};

struct ControlShmPreparedFrame {
    std::string_view data;
    ControlShmDescriptor descriptor;
};

class ControlShmMetadataReader {
public:
    explicit ControlShmMetadataReader(std::string_view data) : data_(data) {}
    bool kind(std::uint8_t expected) const {
        return data_.size() >= 6 && std::memcmp(data_.data(), kLocalControlMagic, 4) == 0 &&
            static_cast<std::uint8_t>(data_[4]) == kControlShmVersion &&
            static_cast<std::uint8_t>(data_[5]) == expected;
    }
    template<class Scalar> Scalar scalar() {
        require(sizeof(Scalar)); Scalar value{};
        std::memcpy(&value, data_.data() + position_, sizeof(value));
        position_ += sizeof(value); return value;
    }
    std::uint64_t ticket() {
        std::uint64_t value = 0;
        for (std::size_t index = 0; index < sizeof(value); ++index)
            value |= static_cast<std::uint64_t>(scalar<std::uint8_t>()) << (index * 8);
        return value;
    }
    std::string_view text() {
        const auto length = scalar<std::uint32_t>(); require(length);
        const auto value = data_.substr(position_, length); position_ += length; return value;
    }
    void lease(LocalPayloadLease& output) {
        if (scalar<std::uint8_t>() != 1) throw ControlShmProtocolError("Control payload must use SHM");
        const auto token = text();
        if (token.empty() || token.size() > 128) throw ControlShmProtocolError("Invalid Control region token");
        output.region_name.assign(token.data(), token.size());
        output.slot_index = scalar<std::uint32_t>(); output.slot_size = scalar<std::uint32_t>();
        output.slot_count = scalar<std::uint32_t>(); output.length = scalar<std::uint32_t>();
    }
    void end() const {
        if (position_ != data_.size()) throw ControlShmProtocolError("Trailing Control metadata");
    }
private:
    void require(std::size_t length) const {
        if (position_ > data_.size() || length > data_.size() - position_)
            throw ControlShmProtocolError("Truncated Control metadata");
    }
    std::string_view data_;
    std::size_t position_ = 6;
};

void decode_control_shm_preparation_response(const ControlShmPreparedFrame& frame,
                                           LocalPayloadLease& response_lease) {
    ControlShmMetadataReader reader(frame.data);
    if (reader.kind(kLocalControlKindError)) {
        const auto error = reader.text(); reader.end();
        if (frame.descriptor.get() >= 0)
            throw ControlShmProtocolError("Descriptor in Control preparation transport error");
        throw std::runtime_error("Control preparation failed: " + std::string(error));
    }
    if (!reader.kind(kLocalControlKindPrepareResp))
        throw ControlShmProtocolError("Expected Control preparation response");
    const auto ok = reader.scalar<std::uint8_t>();
    const auto error = reader.text();
    if (ok == 0) {
        if (reader.scalar<std::uint8_t>() != 0)
            throw ControlShmProtocolError("Lease in failed Control preparation");
        reader.end();
        if (frame.descriptor.get() >= 0)
            throw ControlShmProtocolError("Descriptor in failed Control preparation");
        throw std::runtime_error("Control preparation failed: " + std::string(error));
    }
    if (ok != 1 || !error.empty())
        throw ControlShmProtocolError("Malformed Control preparation response");
    reader.lease(response_lease); reader.end();
}

r2kernel::wrapper::ControlResponse decode_prepared_control_shm_response(
    ControlShmPreparedFrame frame, ControlShmRegion& incoming,
    std::uint64_t& ticket, LocalPayloadLease& lease) {
    ControlShmMetadataReader reader(frame.data);
    if (reader.kind(kLocalControlKindError)) {
        const auto error = reader.text(); reader.end();
        if (frame.descriptor.get() >= 0) throw ControlShmProtocolError("Descriptor in transport error");
        throw std::runtime_error("Control v5 runtime error: " + std::string(error));
    }
    if (!reader.kind(kLocalControlKindTransactResp))
        throw ControlShmProtocolError("Expected Control v5 response");
    const auto ok = reader.scalar<std::uint8_t>();
    const auto status = reader.scalar<std::uint16_t>();
    const auto timestamp = reader.scalar<std::int64_t>();
    const auto error = reader.text();
    if (ok == 0) {
        const auto present = reader.scalar<std::uint8_t>();
        const auto inline_length = reader.scalar<std::uint32_t>(); reader.end();
        if (present != 0 || inline_length != 0 || frame.descriptor.get() >= 0)
            throw ControlShmProtocolError("Malformed failed Control response");
        throw std::runtime_error("Control v5 runtime error: " + std::string(error));
    }
    if (ok != 1 || !error.empty()) throw ControlShmProtocolError("Malformed successful Control response");
    reader.lease(lease); ticket = reader.ticket(); reader.end();
    if (ticket == 0) throw ControlShmProtocolError("Invalid Control response ticket");
    auto payload = incoming.read(lease, std::move(frame.descriptor));
    return {timestamp, status, std::move(payload)};
}
