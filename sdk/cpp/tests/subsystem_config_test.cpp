// Subsystem configuration document (Python SDK helper/config.py and the
// Subsystem config_* methods): file location, PyYAML scalar typing, block
// layout and nested get/set semantics.
#include "r2kernel/wrapper/subsystem.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unistd.h>

namespace {

using r2kernel::wrapper::Subsystem;
using r2kernel::wrapper::SubsystemOptions;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
}

void require_equal(const std::string& actual, const std::string& expected, const std::string& what) {
    if (actual != expected) throw std::runtime_error(what + ": expected <" + expected + "> got <" + actual + ">");
}

class ConfigProbe final : public Subsystem {
public:
    using Subsystem::Subsystem;

protected:
    bool on_initialize() override { return true; }
    bool on_start() override { return true; }
    bool on_terminate() override { return true; }
    bool on_reset() override { return true; }
};

std::string read_file(const std::filesystem::path& path) {
    std::ifstream input(path);
    std::stringstream text;
    text << input.rdbuf();
    return text.str();
}

void write_file(const std::filesystem::path& path, const std::string& text) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << text;
}

SubsystemOptions options_for(const std::filesystem::path& root) {
    write_file(root / "subsystem.yaml",
               "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n  name: config.probe\n  role: component\n");
    SubsystemOptions options;
    options.name = "config.probe";
    options.user_data_path = root / "userdata";
    options.manifest_path = root / "subsystem.yaml";
    return options;
}

void test_missing_file_is_created_and_values_round_trip(const std::filesystem::path& root) {
    ConfigProbe subsystem(options_for(root));
    const auto file = root / "userdata" / "config";
    require_equal(read_file(file), "{}\n", "a missing config is created from the empty default");
    require_equal(subsystem.config_data(), "{}", "empty config data");
    require_equal(subsystem.config_get("a.b"), "null", "missing value returns the default");
    require_equal(subsystem.config_get("a.b", "7"), "7", "missing value returns the given default");
    subsystem.config_set("5", "a.b");
    require_equal(subsystem.config_get("a/b"), "5", "slash path");
    require_equal(subsystem.config_get(std::vector<std::string> {"a", "b"}), "5", "literal keys");
    require_equal(subsystem.config_get(std::vector<std::string> {"a.b"}), "null", "a literal key is not split");
    require_equal(read_file(file), "{}\n", "config_set without save leaves the file");
    subsystem.config_set("null", "a.c");
    require_equal(subsystem.config_get("a.c", "\"fallback\""), "\"fallback\"", "a null value returns the default");
    subsystem.config_set("1", "x");
    subsystem.config_set("2", "x.y");
    require_equal(subsystem.config_get("x"), "{\"y\":2}", "a non-object intermediate value is replaced");
    subsystem.config_set("3", "", true);
    require_equal(subsystem.config_data(), "{\"a\":{\"b\":5,\"c\":null},\"x\":{\"y\":2}}", "an empty path changes nothing");
    bool rejected = false;
    try {
        subsystem.config_set("not json", "a");
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "a value that is not JSON is refused");
    subsystem.config_save();
    require_equal(read_file(file), "a:\n  b: 5\n  c: null\n\nx:\n  y: 2\n", "saved layout");
    write_file(file, "a: {b: 6}\n");
    require_equal(subsystem.config_reload(), "{\"a\":{\"b\":6}}", "reload reads the file again");
}

// The same document written by PyYAML (yaml.dump with the SDK's dumper):
// strings that would load as other types are quoted, floats keep a point.
void test_layout_matches_python(const std::filesystem::path& root) {
    ConfigProbe subsystem(options_for(root));
    subsystem.config_set("1", "a");
    subsystem.config_set(R"({"c":[1,2,{"x":"yes"}],"d":"true","e":null,"f":1.5,"g":1e20,"h":"","i":[]})", "b");
    subsystem.config_set("\"012\"", "j");
    subsystem.config_set("{}", "k");
    subsystem.config_set("true", "n", true);
    require_equal(read_file(root / "userdata" / "config"),
                  "a: 1\n\nb:\n  c:\n  - 1\n  - 2\n  - x: 'yes'\n  d: 'true'\n  e: null\n  f: 1.5\n  g: 1.0e+20\n"
                  "  h: ''\n  i: []\n\nj: '012'\n\nk: {}\n\nn: true\n",
                  "PyYAML layout");
    require_equal(subsystem.config_reload(),
                  R"({"a":1,"b":{"c":[1,2,{"x":"yes"}],"d":"true","e":null,"f":1.5,"g":1e+20,"h":"","i":[]},)"
                  R"("j":"012","k":{},"n":true})",
                  "the saved file reads back unchanged");
}

// PyYAML SafeLoader (YAML 1.1) scalar resolution.
void test_python_scalar_types(const std::filesystem::path& root) {
    write_file(root / "userdata" / "config",
               "a: 0o17\nb: 0x1F\nc: 1_000\nd: 1:30\ne: 1e3\nf: 1.0e+3\ng: .5\nh: ON\ni: ~\nk: 017\nl: +12\n"
               "m: -0\nq: 'x'\nr: off\ns: -.inf\nt: 2001-12-14\nu: |\n  text\n");
    ConfigProbe subsystem(options_for(root));
    require_equal(subsystem.config_data(),
                  R"({"a":"0o17","b":31,"c":1000,"d":90,"e":"1e3","f":1000.0,"g":0.5,"h":true,"i":null,"k":15,)"
                  R"("l":12,"m":0,"q":"x","r":false,"s":-Infinity,"t":"2001-12-14","u":"text\n"})",
                  "YAML 1.1 scalar types");
    subsystem.config_save();
    require_equal(read_file(root / "userdata" / "config"),
                  "a: 0o17\n\nb: 31\n\nc: 1000\n\nd: 90\n\ne: 1e3\n\nf: 1000.0\n\ng: 0.5\n\nh: true\n\ni: null\n\n"
                  "k: 15\n\nl: 12\n\nm: 0\n\nq: x\n\nr: false\n\ns: -.inf\n\nt: '2001-12-14'\n\nu: \"text\\n\"\n",
                  "written back with the same types");
    write_file(root / "userdata" / "config", "0\n");
    require_equal(subsystem.config_reload(), "{}", "a false-y document is the empty mapping");
}

}  // namespace

int main() {
    const auto root = std::filesystem::temp_directory_path()
        / ("r2kernel-cpp-config-" + std::to_string(::getpid()));
    ::setenv("R2KERNEL_CONTROL_SOCKET", (root / "absent-control.sock").c_str(), 1);
    try {
        test_missing_file_is_created_and_values_round_trip(root / "round-trip");
        test_layout_matches_python(root / "layout");
        test_python_scalar_types(root / "scalars");
    } catch (const std::exception& error) {
        std::cerr << "subsystem_config_test failed: " << error.what() << '\n';
        std::filesystem::remove_all(root);
        return 1;
    }
    std::filesystem::remove_all(root);
    std::cout << "subsystem_config_test passed\n";
    return 0;
}
