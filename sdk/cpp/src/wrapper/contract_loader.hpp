#pragma once

#include <memory>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace r2kernel::wrapper { class SchemaContext; }
namespace r2kernel::wrapper::contract_loader {

struct ApiEventSpec {
    std::string topic;
    std::string type;
    std::string direction;
};

struct ApiControlSpec {
    std::string topic;
    std::string request;
    std::string response;
    std::string timing_support;
};

struct ApiProcedureSpec {
    std::string name;
    std::string request;
    std::string response;
    std::string timing_support;
};

struct ApiOperationSpec {
    std::string name;
    std::string request;
    std::string feedback;
    std::string result;
    std::string state;
    std::string timing_support;
};

struct ApiContractSpec {
    std::string id;
    std::vector<ApiEventSpec> events;
    std::vector<ApiControlSpec> controls;
    std::vector<ApiProcedureSpec> procedures;
    std::vector<ApiOperationSpec> operations;
    // Names declared under the contract's local `messages:` map.
    std::vector<std::string> messages;
};

struct ComponentContractSpec {
    std::string id;
    std::vector<std::string> conformance_apis;
    std::string spatial_participation;
    struct SpatialPort {
        std::string name;
        std::string direction;
        std::string semantic;
        bool required = true;
    };
    std::vector<SpatialPort> spatial_ports;
};

class Inventory final {
public:
    explicit Inventory(std::vector<std::filesystem::path> roots);
    explicit Inventory(std::shared_ptr<const SchemaContext> context);
    std::shared_ptr<const ApiContractSpec> api(std::string_view id) const;
    std::shared_ptr<const ComponentContractSpec> component(std::string_view id) const;
private:
    struct Impl;
    std::shared_ptr<Impl> impl_;
};
class InventoryScope final {
public:
    explicit InventoryScope(std::shared_ptr<const Inventory> inventory);
    ~InventoryScope();
    InventoryScope(const InventoryScope&) = delete;
    InventoryScope& operator=(const InventoryScope&) = delete;
private:
    std::shared_ptr<const Inventory> previous_;
};
std::shared_ptr<const ApiContractSpec> find_api_contract(std::string_view id);
std::shared_ptr<const ComponentContractSpec> find_component_contract(std::string_view id);
void clear_inventory_cache();

}  // namespace r2kernel::wrapper::contract_loader
