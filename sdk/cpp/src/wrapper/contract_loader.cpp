#include "contract_loader.hpp"
#include "r2kernel/wrapper/prepared_codec.hpp"
#include "timing_support.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#if !defined(_WIN32)
#include <unistd.h>
#endif
#include <mutex>
#include <string>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace r2kernel::wrapper::contract_loader {

namespace fs = std::filesystem;

namespace {

std::string trim(std::string value) {
    auto begin = value.begin();
    while (begin != value.end() && std::isspace(static_cast<unsigned char>(*begin)) != 0) {
        ++begin;
    }
    auto end = value.end();
    while (end != begin && std::isspace(static_cast<unsigned char>(*(end - 1))) != 0) {
        --end;
    }
    return std::string(begin, end);
}

std::string to_pascal_case(std::string_view text) {
    std::string out;
    bool capitalize = true;
    for (const char ch : text) {
        if (std::isalnum(static_cast<unsigned char>(ch)) == 0) {
            capitalize = true;
            continue;
        }
        if (capitalize) {
            out.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(ch))));
            capitalize = false;
        } else {
            out.push_back(ch);
        }
    }
    return out.empty() ? std::string("Endpoint") : out;
}

std::string generated_args_type(const std::string& owner_id, const std::string& endpoint_name) {
    return owner_id + "/" + to_pascal_case(endpoint_name) + "Args";
}

fs::path relative_spec_path(std::string_view top_level, std::string_view spec_id) {
    std::vector<std::string> parts;
    std::string current;
    for (const char ch : spec_id) {
        if (ch == '.') {
            if (!current.empty()) {
                parts.push_back(std::move(current));
                current.clear();
            }
        } else {
            current.push_back(ch);
        }
    }
    if (!current.empty()) {
        parts.push_back(std::move(current));
    }
    if (parts.size() < 2) {
        return {};
    }
    fs::path path(top_level);
    for (std::size_t i = 0; i + 1 < parts.size(); ++i) {
        path /= parts[i];
    }
    path /= parts.back() + ".yaml";
    return path;
}

std::string node_to_string(const YAML::Node& node) {
    if (!node || !node.IsScalar()) {
        return {};
    }
    return node.as<std::string>(std::string{});
}

// Spec contracts follow the same timing_support rule as endpoint options; an
// unknown value makes the contract invalid instead of reaching validation.
std::string contract_timing_support(const std::string& contract_id, const char* kind,
                                    const std::string& name, const YAML::Node& raw) {
    try {
        return detail::normalize_timing_support(node_to_string(raw));
    } catch (const std::invalid_argument& error) {
        throw std::invalid_argument("API contract " + contract_id + " " + kind + " '" + name + "': " + error.what());
    }
}


bool sequence_has_entries(const YAML::Node& node) {
    return node && node.IsSequence() && node.size() > 0;
}

ApiContractSpec parse_api_contract_yaml(const std::string& source) {
    YAML::Node doc = YAML::Load(source);
    ApiContractSpec spec;
    spec.id = trim(node_to_string(doc["id"]));

    if (const YAML::Node events = doc["events"]; events && events.IsSequence()) {
        for (const auto& raw : events) {
            ApiEventSpec event;
            event.topic = trim(node_to_string(raw["topic"]));
            event.type = trim(node_to_string(raw["type"]));
            event.direction = trim(node_to_string(raw["direction"]));
            if (event.direction.empty()) {
                event.direction = "publish";
            }
            spec.events.push_back(std::move(event));
        }
    }

    if (const YAML::Node controls = doc["controls"]; controls && controls.IsSequence()) {
        for (const auto& raw : controls) {
            ApiControlSpec ep;
            ep.topic = trim(node_to_string(raw["topic"]));
            ep.request = trim(node_to_string(raw["request"]));
            ep.response = trim(node_to_string(raw["response"]));
            ep.timing_support = contract_timing_support(spec.id, "control", ep.topic, raw["timing_support"]);
            if (ep.request.empty() && sequence_has_entries(raw["args"])) {
                ep.request = generated_args_type(spec.id, ep.topic);
            }
            spec.controls.push_back(std::move(ep));
        }
    }

    if (const YAML::Node procedures = doc["procedures"]; procedures && procedures.IsSequence()) {
        for (const auto& raw : procedures) {
            ApiProcedureSpec procedure;
            procedure.name = trim(node_to_string(raw["name"]));
            procedure.request = trim(node_to_string(raw["request"]));
            procedure.response = trim(node_to_string(raw["response"]));
            procedure.timing_support = contract_timing_support(spec.id, "procedure", procedure.name, raw["timing_support"]);
            if (procedure.request.empty() && sequence_has_entries(raw["args"])) {
                procedure.request = generated_args_type(spec.id, procedure.name);
            }
            spec.procedures.push_back(std::move(procedure));
        }
    }

    if (const YAML::Node operations = doc["operations"]; operations && operations.IsSequence()) {
        for (const auto& raw : operations) {
            ApiOperationSpec operation;
            operation.name = trim(node_to_string(raw["name"]));
            operation.request = trim(node_to_string(raw["request"]));
            operation.feedback = trim(node_to_string(raw["feedback"]));
            operation.result = trim(node_to_string(raw["result"]));
            operation.state = trim(node_to_string(raw["state"]));
            operation.timing_support = contract_timing_support(spec.id, "operation", operation.name, raw["timing_support"]);
            if (operation.request.empty() && sequence_has_entries(raw["args"])) {
                operation.request = generated_args_type(spec.id, operation.name);
            }
            spec.operations.push_back(std::move(operation));
        }
    }

    if (const YAML::Node messages = doc["messages"]; messages && messages.IsMap()) {
        for (const auto& item : messages) {
            std::string name = trim(node_to_string(item.first));
            if (!name.empty()) {
                spec.messages.push_back(std::move(name));
            }
        }
    }

    return spec;
}

ComponentContractSpec parse_component_yaml(const std::string& source) {
    YAML::Node doc = YAML::Load(source);
    ComponentContractSpec spec;
    spec.id = trim(node_to_string(doc["id"]));
    if (const YAML::Node conformance = doc["conformance_apis"]; conformance && conformance.IsSequence()) {
        for (const auto& raw : conformance) {
            std::string value = trim(raw.as<std::string>(std::string{}));
            if (!value.empty()) {
                spec.conformance_apis.push_back(std::move(value));
            }
        }
    }
    const YAML::Node spatial = doc["spatial"];
    spec.spatial_participation = trim(node_to_string(spatial["participation"]));
    if (const YAML::Node ports = spatial["ports"]; ports && ports.IsMap()) {
        for (const auto& item : ports) {
            ComponentContractSpec::SpatialPort port;
            port.name = trim(node_to_string(item.first));
            port.direction = trim(node_to_string(item.second["direction"]));
            port.semantic = trim(node_to_string(item.second["semantic"]));
            port.required = item.second["required"] ? item.second["required"].as<bool>(true) : true;
            spec.spatial_ports.push_back(std::move(port));
        }
    }
    return spec;
}

thread_local std::shared_ptr<const Inventory> selected_inventory;
} // namespace

struct Inventory::Impl {
    std::shared_ptr<const SchemaContext> context;
    mutable std::mutex mutex;
    mutable std::unordered_map<std::string, std::shared_ptr<const ApiContractSpec>> apis;
    mutable std::unordered_map<std::string, std::shared_ptr<const ComponentContractSpec>> components;
};
Inventory::Inventory(std::vector<fs::path> roots) : impl_(std::make_shared<Impl>()) {
    std::vector<std::string> names;
    for (const auto& root : roots) names.push_back(root.string());
    impl_->context = std::make_shared<SchemaContext>(names);
}
Inventory::Inventory(std::shared_ptr<const SchemaContext> context) : impl_(std::make_shared<Impl>()) {
    impl_->context = std::move(context);
}
std::shared_ptr<const ApiContractSpec> Inventory::api(std::string_view id) const {
    const std::string key(id); std::lock_guard<std::mutex> lock(impl_->mutex);
    if (auto found = impl_->apis.find(key); found != impl_->apis.end()) return found->second;
    const auto source = impl_->context->spec_text(relative_spec_path("apis", id).generic_string());
    if (!source) return {};
    auto result = std::make_shared<ApiContractSpec>(parse_api_contract_yaml(*source));
    if (result->id != id) throw std::runtime_error("API contract id mismatch: " + key);
    impl_->apis.emplace(key, result); return result;
}
std::shared_ptr<const ComponentContractSpec> Inventory::component(std::string_view id) const {
    const std::string key(id); std::lock_guard<std::mutex> lock(impl_->mutex);
    if (auto found = impl_->components.find(key); found != impl_->components.end()) return found->second;
    const auto source = impl_->context->spec_text(relative_spec_path("components", id).generic_string());
    if (!source) return {};
    auto result = std::make_shared<ComponentContractSpec>(parse_component_yaml(*source));
    if (result->id != id) throw std::runtime_error("component contract id mismatch: " + key);
    impl_->components.emplace(key, result); return result;
}
InventoryScope::InventoryScope(std::shared_ptr<const Inventory> inventory)
    : previous_(std::exchange(selected_inventory, std::move(inventory))) {}
InventoryScope::~InventoryScope() { selected_inventory = std::move(previous_); }
std::shared_ptr<const ApiContractSpec> find_api_contract(std::string_view id) {
    const auto inventory = selected_inventory ? selected_inventory : std::make_shared<Inventory>(std::make_shared<SchemaContext>());
    return inventory->api(id);
}
std::shared_ptr<const ComponentContractSpec> find_component_contract(std::string_view id) {
    const auto inventory = selected_inventory ? selected_inventory : std::make_shared<Inventory>(std::make_shared<SchemaContext>());
    return inventory->component(id);
}
// Retained for source compatibility; no process-global cache remains.
void clear_inventory_cache() {}
} // namespace r2kernel::wrapper::contract_loader
