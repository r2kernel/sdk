// Manifest validation and manifest discovery, driven by the cases the Python
// SDK also runs (sdk/testdata/parity, tests/test_sdk_parity_cases.py):
// both SDKs accept and reject the same manifests with the same messages and
// pick the same manifest files.
#include "r2kernel/wrapper/manifest.hpp"
#include "r2kernel/wrapper/subsystem.hpp"

#include <yaml-cpp/yaml.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {

namespace fs = std::filesystem;
using namespace r2kernel::wrapper;

class Probe : public Subsystem {
public:
    using Subsystem::Subsystem;

protected:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

void write_file(const fs::path& path, const std::string& text) {
    fs::create_directories(path.parent_path());
    std::ofstream file(path);
    file << text;
    if (!file) throw std::runtime_error("cannot write " + path.string());
}

std::string replace_root(std::string text, const fs::path& root) {
    for (auto at = text.find("<root>"); at != std::string::npos; at = text.find("<root>"))
        text.replace(at, 6, root.string());
    return text;
}

class ScopedEnvironment {
public:
    void set(const std::string& name, const std::optional<std::string>& value) {
        if (saved_.count(name) == 0) {
            const char* previous = std::getenv(name.c_str());
            saved_[name] = previous ? std::optional<std::string>(previous) : std::nullopt;
        }
        if (value) ::setenv(name.c_str(), value->c_str(), 1);
        else ::unsetenv(name.c_str());
    }
    ~ScopedEnvironment() {
        for (const auto& [name, value] : saved_) {
            if (value) ::setenv(name.c_str(), value->c_str(), 1);
            else ::unsetenv(name.c_str());
        }
    }

private:
    std::map<std::string, std::optional<std::string>> saved_;
};

class ScopedDirectory {
public:
    explicit ScopedDirectory(const fs::path& path) : previous_(fs::current_path()) { fs::current_path(path); }
    ~ScopedDirectory() { fs::current_path(previous_); }

private:
    fs::path previous_;
};

void load_case(const std::string& kind, const fs::path& path) {
    if (kind == "subsystem") (void)load_subsystem_manifest(path);
    else if (kind == "robot") (void)load_robot_platform_manifest(path);
    else if (kind == "application") (void)load_application_manifest(path);
    else if (kind == "deployment") (void)load_deployment_manifest(path);
    else throw std::runtime_error("unknown loader " + kind);
}

int checked_cases = 0;

int validation_cases(const fs::path& parity, const std::string& file_name, const fs::path& scratch) {
    int failures = 0;
    for (const auto& item : YAML::LoadFile((parity / file_name).string())["cases"]) {
        const std::string name = item["name"].as<std::string>();
        ++checked_cases;
        const fs::path root = scratch / ("validation-" + name);
        for (const auto& file : item["files"]) write_file(root / file.first.as<std::string>(), file.second.as<std::string>());
        const bool accept = item["expect"].as<std::string>() == "accept";
        std::string outcome;
        try {
            load_case(item["load"].as<std::string>(), root / item["path"].as<std::string>());
            if (!accept) outcome = "accepted; the Python SDK rejects it with: " + item["error"].as<std::string>();
        } catch (const ManifestError& error) {
            const std::string message = error.what();
            if (accept) outcome = "rejected; the Python SDK accepts it: " + message;
            else if (message.find(item["error"].as<std::string>()) == std::string::npos)
                outcome = "message differs from the Python SDK: " + message;
        } catch (const std::exception& error) {
            outcome = std::string("failed without ManifestError: ") + error.what();
        }
        if (!outcome.empty()) {
            std::cerr << file_name << " " << name << ": " << outcome << '\n';
            ++failures;
        }
    }
    return failures;
}

int search_cases(const fs::path& parity, const fs::path& scratch) {
    int failures = 0;
    for (const auto& item : YAML::LoadFile((parity / "manifest-search.yaml").string())["cases"]) {
        const std::string name = item["name"].as<std::string>();
        ++checked_cases;
        const fs::path root = fs::weakly_canonical(scratch) / ("search-" + name);
        for (const auto& file : item["files"]) {
            const std::string relative = file.as<std::string>();
            const bool subsystem = relative.size() >= 14 && relative.compare(relative.size() - 14, 14, "subsystem.yaml") == 0;
            write_file(root / relative, subsystem ? "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: probe, role: component}\n"
                                                  : "kind: r2kernel.deployment\nversion: 1\nsubsystems: {}\n");
        }
        fs::create_directories(root / item["cwd"].as<std::string>());
        ScopedEnvironment environment;
        environment.set("HOME", (root / "home").string());
        for (const char* variable : {"R2KERNEL_SUBSYSTEM_MANIFEST", "R2KERNEL_DEPLOYMENT_MANIFEST", "R2KERNEL_BINDINGS"})
            environment.set(variable, std::nullopt);
        if (const YAML::Node env = item["env"]; env && env.IsMap())
            for (const auto& variable : env)
                environment.set(variable.first.as<std::string>(), replace_root(variable.second.as<std::string>(), root));
        ScopedDirectory directory(root / item["cwd"].as<std::string>());
        SubsystemOptions options;
        options.name = "probe";
        const std::string user_data = item["user_data"].as<std::string>();
        options.user_data_path = user_data.rfind("./", 0) == 0 ? fs::path(user_data) : root / user_data;
        if (item["manifest_path"]) options.manifest_path = replace_root(item["manifest_path"].as<std::string>(), root);
        if (item["deployment_manifest_path"])
            options.deployment_manifest_path = replace_root(item["deployment_manifest_path"].as<std::string>(), root);
        std::string outcome;
        try {
            Probe subsystem(options);
            if (item["error"]) {
                outcome = "constructed; the Python SDK fails with: " + item["error"].as<std::string>();
            } else {
                const fs::path expected = root / item["subsystem_manifest"].as<std::string>();
                if (subsystem.subsystem_manifest().path != expected)
                    outcome = "used " + subsystem.subsystem_manifest().path.string() + ", expected " + expected.string();
                const YAML::Node deployment = item["deployment_manifest"];
                const auto& loaded = subsystem.deployment_manifest();
                if (!deployment || deployment.IsNull()) {
                    if (loaded) outcome += " loaded deployment " + loaded->path.string() + ", expected none";
                } else if (!loaded || loaded->path != root / deployment.as<std::string>()) {
                    outcome += " deployment " + (loaded ? loaded->path.string() : std::string("none")) + ", expected "
                             + (root / deployment.as<std::string>()).string();
                }
            }
        } catch (const std::exception& error) {
            const std::string message = error.what();
            if (!item["error"]) outcome = "failed: " + message;
            else if (message.find(item["error"].as<std::string>()) == std::string::npos)
                outcome = "message differs from the Python SDK: " + message;
        }
        if (!outcome.empty()) {
            std::cerr << "manifest-search " << name << ": " << outcome << '\n';
            ++failures;
        }
    }
    return failures;
}

}  // namespace

int main() {
    const fs::path parity = R2KERNEL_SDK_PARITY_DIR;
    const fs::path scratch = fs::temp_directory_path() / ("r2kernel-sdk-parity-" + std::to_string(::getpid()));
    fs::create_directories(scratch);
    ::setenv("R2KERNEL_RESOURCE_SERVICE_MODE", "disabled", 1);
#ifdef R2KERNEL_SDK_SPEC_DIR
    // The installed standard dictionary the package spec-root cases compare
    // with; the Python SDK finds the same tree beside its sources.
    ::setenv("R2KERNEL_SPEC_ROOT", R2KERNEL_SDK_SPEC_DIR, 0);
#endif
    int failures = 0;
    try {
        failures += validation_cases(parity, "manifest-validation.yaml", scratch);
        failures += validation_cases(parity, "deployment-validation.yaml", scratch);
        failures += search_cases(parity, scratch);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        failures += 1;
    }
    std::error_code ignored;
    fs::remove_all(scratch, ignored);
    if (failures != 0) {
        std::cerr << failures << " case(s) differ from the Python SDK\n";
        return 1;
    }
    std::cout << checked_cases << " manifest validation and discovery cases match the Python SDK\n";
    return 0;
}
