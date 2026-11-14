#pragma once

#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include "r2kernel_comm.h"
#include "r2kernel/export.hpp"
#include "r2kernel/wrapper/core_client.hpp"

namespace r2kernel::wrapper {

// Scratch belongs to one interface/calling thread. Reuse it between packets;
// views returned by PreparedCodec expire on its next operation.
class R2KERNEL_CPP_API CodecWorkspace final {
public:
    CodecWorkspace();
    ~CodecWorkspace();
    CodecWorkspace(CodecWorkspace&&) noexcept;
    CodecWorkspace& operator=(CodecWorkspace&&) noexcept;
    CodecWorkspace(const CodecWorkspace&) = delete;
    CodecWorkspace& operator=(const CodecWorkspace&) = delete;
private:
    friend class PreparedCodec;
    friend class SchemaContext;
    R2KernelCodecOutput* output_ = nullptr;
};

struct R2KERNEL_CPP_API CodecByteView {
    const std::uint8_t* data = nullptr;
    std::size_t size = 0;
    ByteBuffer copy() const;
};
struct CodecNodeView {
    const R2KernelCodecNode* data = nullptr;
    std::size_t size = 0;
};

// Immutable compiled plan. Copies share the plan, never mutable scratch.
class R2KERNEL_CPP_API PreparedCodec final {
public:
    PreparedCodec() = default;
    const std::string& message_ref() const noexcept;
    const std::string& fingerprint() const noexcept;
    const std::string& source() const noexcept;
    explicit operator bool() const noexcept { return static_cast<bool>(description_); }
    CodecByteView encode_nodes(const R2KernelCodecNode* nodes, std::size_t count,
                              CodecWorkspace& workspace) const;
    CodecNodeView decode_nodes(const ByteBuffer& payload, CodecWorkspace& workspace) const;
    CodecByteView encode_json(std::string_view json, CodecWorkspace& workspace) const;
    std::string_view decode_json(const ByteBuffer& payload, CodecWorkspace& workspace) const;
private:
    friend class SchemaContext;
    struct Description;
    std::shared_ptr<const Description> description_;
};

// NULL roots captures ambient precedence once. Explicit roots are exclusive;
// an empty list selects shipped standards. Recreate on explicit reinitialize.
class R2KERNEL_CPP_API SchemaContext final {
public:
    SchemaContext();
    explicit SchemaContext(const std::vector<std::string>& roots);
    PreparedCodec prepare(const std::string& message_ref) const;
    std::optional<std::string> spec_text(const std::string& relative_path) const;
private:
    explicit SchemaContext(const char* roots_json);
    std::shared_ptr<R2KernelSchemaContext> handle_;
};

} // namespace r2kernel::wrapper
