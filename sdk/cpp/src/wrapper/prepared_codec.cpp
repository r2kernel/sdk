#include "r2kernel/wrapper/prepared_codec.hpp"
#include "../core/json_text.hpp"
#include <yaml-cpp/yaml.h>
#include <stdexcept>
#include <utility>

namespace r2kernel::wrapper {
namespace {
void require_success(int result, const char* operation) {
    if (result != 0) throw std::runtime_error(std::string(operation) + " failed rc=" + std::to_string(result));
}
std::string roots_json(const std::vector<std::string>& roots) {
    std::string json = "["; bool first = true;
    for (const auto& root : roots) {
        if (!first) json += ',';
        first = false;
        json += '"';
        json += sdk_detail::json_escape(root);
        json += '"';
    }
    return json + ']';
}
CodecByteView bytes_view(R2KernelCodecOutput* output) {
    std::size_t size = 0;
    const auto* bytes = r2kernel_codec_output_bytes(output, &size);
    return {bytes, size};
}
}
struct PreparedCodec::Description {
    std::shared_ptr<R2KernelPreparedCodec> handle;
    std::string message_ref;
    std::string fingerprint;
    std::string source;
};
namespace {
const std::string& empty_metadata() { static const std::string empty; return empty; }
}
const std::string& PreparedCodec::message_ref() const noexcept { return description_ ? description_->message_ref : empty_metadata(); }
const std::string& PreparedCodec::fingerprint() const noexcept { return description_ ? description_->fingerprint : empty_metadata(); }
const std::string& PreparedCodec::source() const noexcept { return description_ ? description_->source : empty_metadata(); }

CodecWorkspace::CodecWorkspace() { require_success(r2kernel_codec_output_create(&output_), "codec workspace create"); }
CodecWorkspace::~CodecWorkspace() { r2kernel_codec_output_free(output_); }
CodecWorkspace::CodecWorkspace(CodecWorkspace&& other) noexcept : output_(std::exchange(other.output_, nullptr)) {}
CodecWorkspace& CodecWorkspace::operator=(CodecWorkspace&& other) noexcept {
    if (this != &other) { r2kernel_codec_output_free(output_); output_ = std::exchange(other.output_, nullptr); }
    return *this;
}
ByteBuffer CodecByteView::copy() const { return size == 0 ? ByteBuffer{} : ByteBuffer(data, data + size); }
SchemaContext::SchemaContext() : SchemaContext(static_cast<const char*>(nullptr)) {}
SchemaContext::SchemaContext(const std::vector<std::string>& roots) : SchemaContext(roots_json(roots).c_str()) {}
SchemaContext::SchemaContext(const char* roots) {
    R2KernelSchemaContext* handle = nullptr;
    require_success(r2kernel_schema_context_create(roots, &handle), "schema context create");
    handle_.reset(handle, r2kernel_schema_context_free);
}
std::optional<std::string> SchemaContext::spec_text(const std::string& relative_path) const {
    CodecWorkspace workspace;
    const auto result = r2kernel_schema_context_spec_text(handle_.get(), relative_path.c_str(), workspace.output_);
    if (result == -2) return std::nullopt;
    require_success(result, "schema context source");
    const auto bytes = bytes_view(workspace.output_);
    return std::string(reinterpret_cast<const char*>(bytes.data), bytes.size);
}
PreparedCodec SchemaContext::prepare(const std::string& message_ref) const {
    PreparedCodec result;
    R2KernelPreparedCodec* handle = nullptr;
    require_success(r2kernel_codec_prepare(handle_.get(), message_ref.c_str(), &handle), ("codec prepare " + message_ref).c_str());
    auto metadata = std::make_shared<PreparedCodec::Description>();
    metadata->handle.reset(handle, r2kernel_codec_prepared_free);
    metadata->message_ref = message_ref;
    CodecWorkspace workspace;
    require_success(r2kernel_codec_prepared_describe(handle, workspace.output_), "codec describe");
    const auto bytes = bytes_view(workspace.output_);
    const auto description = YAML::Load(std::string(reinterpret_cast<const char*>(bytes.data), bytes.size));
    metadata->fingerprint = description["fingerprint"].as<std::string>();
    metadata->source = description["source"].as<std::string>();
    result.description_ = std::move(metadata);
    return result;
}
CodecByteView PreparedCodec::encode_nodes(const R2KernelCodecNode* nodes, std::size_t count, CodecWorkspace& workspace) const {
    require_success(r2kernel_codec_prepared_encode_nodes(description_ ? description_->handle.get() : nullptr, nodes, count, workspace.output_), "prepared native encode");
    return bytes_view(workspace.output_);
}
CodecNodeView PreparedCodec::decode_nodes(const ByteBuffer& payload, CodecWorkspace& workspace) const {
    require_success(r2kernel_codec_prepared_decode_nodes(description_ ? description_->handle.get() : nullptr, payload.data(), payload.size(), workspace.output_), "prepared native decode");
    std::size_t count = 0;
    const auto* nodes = r2kernel_codec_output_nodes(workspace.output_, &count);
    return {nodes, count};
}
CodecByteView PreparedCodec::encode_json(std::string_view json, CodecWorkspace& workspace) const {
    require_success(r2kernel_codec_prepared_encode_json(description_ ? description_->handle.get() : nullptr, reinterpret_cast<const std::uint8_t*>(json.data()), json.size(), workspace.output_), "prepared JSON encode");
    return bytes_view(workspace.output_);
}
std::string_view PreparedCodec::decode_json(const ByteBuffer& payload, CodecWorkspace& workspace) const {
    require_success(r2kernel_codec_prepared_decode_json(description_ ? description_->handle.get() : nullptr, payload.data(), payload.size(), workspace.output_), "prepared JSON decode");
    const auto bytes = bytes_view(workspace.output_);
    return {reinterpret_cast<const char*>(bytes.data), bytes.size};
}
} // namespace r2kernel::wrapper
