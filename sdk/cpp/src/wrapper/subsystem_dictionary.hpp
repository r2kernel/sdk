// Included once, inside subsystem.cpp's anonymous namespace after
// subsystem_platform.hpp (same translation unit). The installed standard
// dictionary and the reserved-ID rule for package spec roots of the Python SDK
// (r2kernel/contract/dictionary.py), with the same decisions and messages.
// Cold: used while manifests load and at construction.
//
// Package spec roots are searched before the installed dictionary, and
// unqualified IDs are reserved for that dictionary. A package spec root may
// contain a standard ID only as an identical copy of the installed definition,
// and may not add an ID under a standard prefix (<kind>.<first segment> of any
// standard ID, such as api.locomotion).

constexpr std::array<const char*, 3> kSpecDocumentTrees {"messages", "apis", "components"};
constexpr std::size_t kMaxSpecTreeDepth = 16;

bool is_dictionary_root(const std::filesystem::path& path) {
    std::error_code error;
    if (!std::filesystem::is_regular_file(path / "defaults.yaml", error)) return false;
    return std::all_of(kSpecDocumentTrees.begin(), kSpecDocumentTrees.end(), [&](const char* tree) {
        std::error_code tree_error;
        return std::filesystem::is_directory(path / tree, tree_error);
    });
}

// The complete standard dictionary this process resolves standard IDs from:
// R2KERNEL_SPEC_ROOT (a complete replacement tree), $R2KERNEL_PREFIX/spec, then
// the spec directories beside the executable. The C++ SDK names no install
// prefix of its own. Without a dictionary, the core library's shipped standards
// are compared by path and the prefix rule cannot be applied; package
// validation, launch and installation still apply the full rule.
std::optional<std::filesystem::path> installed_dictionary_root() {
    std::vector<std::filesystem::path> candidates;
    if (const auto shared = env_string_value("R2KERNEL_SPEC_ROOT"); !shared.empty()) candidates.push_back(expand_user(shared));
    if (const auto prefix = env_string_value("R2KERNEL_PREFIX"); !prefix.empty())
        candidates.push_back(expand_user(prefix) / "spec");
#if !defined(_WIN32)
    std::error_code error;
    if (const auto executable = std::filesystem::read_symlink("/proc/self/exe", error); !error) {
        candidates.push_back(executable.parent_path() / "spec");
        candidates.push_back(executable.parent_path().parent_path() / "spec");
    }
#endif
    for (const auto& candidate : candidates)
        if (is_dictionary_root(candidate)) return resolved_path(candidate);
    return std::nullopt;
}

// Python _spec_tree: the documents under root and the problems that make the
// tree unusable. Like the core's spec snapshot, the walk refuses rather than
// skips: a directory reached twice (an alias), an entry that cannot be read, a
// path that leaves the root, a tree deeper than the limit, or a YAML document
// outside <tree>/<kind>/<segment>/.../<version>.yaml.
struct SpecTree {
    std::vector<std::filesystem::path> documents;
    std::vector<std::string> problems;
};

inline std::string spec_tree_reason(const std::error_code& error) { return std::strerror(error.value()); }

void collect_spec_tree(const std::filesystem::path& root, const std::filesystem::path& base,
                       const std::filesystem::path& relative, std::size_t depth,
                       std::map<std::filesystem::path, std::filesystem::path>& seen, SpecTree& tree) {
    const auto directory = root / relative;
    const std::string shown = relative.generic_string();
    if (depth > kMaxSpecTreeDepth) {
        tree.problems.push_back(shown + " is nested deeper than " + std::to_string(kMaxSpecTreeDepth) + " directories");
        return;
    }
    std::error_code error;
    const auto real = std::filesystem::canonical(directory, error);
    if (error) {
        tree.problems.push_back(shown + " cannot be read: " + spec_tree_reason(error));
        return;
    }
    const auto inside = std::mismatch(base.begin(), base.end(), real.begin(), real.end()).first == base.end();
    if (!inside) {
        tree.problems.push_back(shown + " leads outside the package spec root");
        return;
    }
    if (const auto first = seen.find(real); first != seen.end()) {
        tree.problems.push_back(shown + " is the same directory as " + first->second.generic_string()
                                + "; spec directories must not alias each other");
        return;
    }
    seen.emplace(real, relative);
    std::vector<std::string> names;
    std::filesystem::directory_iterator it(directory, error);
    for (const std::filesystem::directory_iterator end; !error && it != end; it.increment(error))
        names.push_back(it->path().filename().string());
    if (error) {
        tree.problems.push_back(shown + " cannot be read: " + spec_tree_reason(error));
        return;
    }
    std::sort(names.begin(), names.end());
    std::vector<std::string> files;
    std::vector<std::string> directories;
    for (const auto& name : names) {
        std::error_code status_error;
        const auto status = std::filesystem::status(directory / name, status_error);
        if (status_error || status.type() == std::filesystem::file_type::not_found) {
            if (!status_error) status_error = std::make_error_code(std::errc::no_such_file_or_directory);
            tree.problems.push_back((relative / name).generic_string() + " cannot be read: " + spec_tree_reason(status_error));
            continue;
        }
        if (status.type() == std::filesystem::file_type::directory) directories.push_back(name);
        else if (status.type() == std::filesystem::file_type::regular) files.push_back(name);
    }
    // A directory's documents come before its subdirectories (Python order).
    for (const auto& name : files) {
        const std::filesystem::path child = relative / name;
        if (child.extension() != ".yaml") continue;
        if (std::distance(child.begin(), child.end()) < 4) {
            tree.problems.push_back(child.generic_string() + " is not at <tree>/<kind>/<segment>/.../<version>.yaml");
            continue;
        }
        tree.documents.push_back(child);
    }
    for (const auto& name : directories) collect_spec_tree(root, base, relative / name, depth + 1, seen, tree);
}

SpecTree spec_tree(const std::filesystem::path& root) {
    SpecTree tree;
    std::error_code error;
    const auto base = std::filesystem::canonical(root, error);
    if (error) {
        tree.problems.push_back("cannot be read: " + spec_tree_reason(error));
        return tree;
    }
    std::map<std::filesystem::path, std::filesystem::path> seen;
    for (const char* name : kSpecDocumentTrees) {
        const auto top = root / name;
        std::error_code status_error;
        const auto status = std::filesystem::symlink_status(top, status_error);
        if (!status_error && status.type() != std::filesystem::file_type::not_found)
            collect_spec_tree(root, base, name, 0, seen, tree);
    }
    return tree;
}

std::vector<std::filesystem::path> spec_documents(const std::filesystem::path& root) { return spec_tree(root).documents; }

std::optional<std::string> read_spec_bytes(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) return std::nullopt;
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

std::string spec_document_id(const std::filesystem::path& relative) {
    std::string id;
    auto part = std::next(relative.begin());
    for (; part != relative.end(); ++part) {
        const bool last = std::next(part) == relative.end();
        id += (id.empty() ? "" : ".") + (last ? part->stem().string() : part->string());
    }
    return id;
}

std::string spec_document_prefix(const std::filesystem::path& relative) {
    auto part = std::next(relative.begin());
    const std::string kind = part->string();
    return kind + "." + std::next(part)->string();
}

// The path an id names, the way every loader resolves it (Python
// r2kernel.spec_paths.relative_spec_path, the core's spec_relative_path):
// dot-separated parts, empty parts dropped, the last one the version file.
std::optional<std::filesystem::path> spec_id_path(const std::string& tree, const std::string& id) {
    std::vector<std::string> parts;
    std::size_t start = 0;
    while (start <= id.size()) {
        const auto end = std::min(id.find('.', start), id.size());
        if (end > start) parts.push_back(id.substr(start, end - start));
        start = end + 1;
    }
    if (parts.size() < 2) return std::nullopt;
    std::filesystem::path path(tree);
    for (std::size_t index = 0; index + 1 < parts.size(); ++index) path /= parts[index];
    return path / (parts.back() + ".yaml");
}

// Python _document_identity_problem: why a message or API document's id does
// not match its path. The core reads every message and API document of a spec
// root, referenced or not, and refuses one whose id differs from its path or
// whose path is not the one its id names.
std::optional<std::string> spec_document_identity_problem(const std::filesystem::path& root,
                                                          const std::filesystem::path& relative) {
    const std::string tree = relative.begin()->string();
    if (tree != "messages" && tree != "apis") return std::nullopt;
    const std::string shown = relative.generic_string();
    const std::string expected = spec_document_id(relative);
    YAML::Node document;
    try {
        const auto text = read_spec_bytes(root / relative);
        if (!text) return shown + " is not a valid YAML document";
        document = YAML::Load(*text);
    } catch (const YAML::Exception&) {
        return shown + " is not a valid YAML document";
    }
    std::string declared;
    if (document.IsMap() && document["id"] && document["id"].IsScalar()) declared = document["id"].Scalar();
    const auto first = declared.find_first_not_of(" \t\r\n");
    declared = first == std::string::npos ? std::string() : declared.substr(first, declared.find_last_not_of(" \t\r\n") - first + 1);
    if (declared.empty()) return shown + " has no id";
    if (declared != expected) return shown + " declares id " + declared + "; a document at this path must declare " + expected;
    // A dot inside a directory or file name joins to the same id but names another path.
    const auto canonical = spec_id_path(tree, declared);
    if (!canonical) return shown + " declares id " + declared + ", which names no document path";
    if (canonical->generic_string() != shown)
        return shown + " is not at the path of id " + declared + "; a document with this id must be at " + canonical->generic_string();
    return std::nullopt;
}

// Python package_spec_root_violations. dictionary is the installed tree; when
// it is absent the core's shipped standards supply the definitions.
std::vector<std::string> package_spec_root_violations(const std::filesystem::path& root,
                                                      const std::optional<std::filesystem::path>& dictionary) {
    std::set<std::string> prefixes;
    if (dictionary)
        for (const auto& relative : spec_documents(*dictionary)) prefixes.insert(spec_document_prefix(relative));
    std::optional<SchemaContext> shipped;
    std::vector<std::string> violations;
    const auto tree = spec_tree(root);
    for (const auto& problem : tree.problems) violations.push_back("Package spec root " + root.string() + ": " + problem);
    for (const auto& relative : tree.documents) {
        if (const auto problem = spec_document_identity_problem(root, relative))
            violations.push_back("Package spec root " + root.string() + ": " + *problem);
        const std::string id = spec_document_id(relative);
        std::optional<std::string> standard;
        if (dictionary) {
            std::error_code error;
            if (std::filesystem::is_regular_file(*dictionary / relative, error)) standard = read_spec_bytes(*dictionary / relative);
        } else {
            if (!shipped) shipped.emplace(std::vector<std::string> {});
            standard = shipped->spec_text(relative.generic_string());
        }
        if (standard) {
            if (read_spec_bytes(root / relative) != standard) {
                violations.push_back("Package spec root " + root.string() + ": " + id
                                     + " differs from the installed standard definition; a package spec root may contain"
                                       " a standard ID only as an identical copy");
            }
            continue;
        }
        const std::string prefix = spec_document_prefix(relative);
        if (prefixes.count(prefix) != 0) {
            violations.push_back("Package spec root " + root.string() + ": " + id + " uses the standard prefix " + prefix
                                 + "; use a vendor segment such as " + std::next(relative.begin())->string()
                                 + ".<vendor>.*");
        }
    }
    // Python _core_refusal: the core's own schema context decides last, so a
    // root the core refuses never passes (cross-audit CONTROL-112-03).
    if (violations.empty()) {
        try {
            SchemaContext context(std::vector<std::string> {root.string()});
        } catch (const std::exception& error) {
            violations.push_back("Package spec root " + root.string()
                                 + ": the runtime core refuses this spec root when it prepares the codecs ("
                                 + error.what() + ")");
        }
    }
    return violations;
}

// Python validate_package_spec_roots: every violation of every root, joined.
void validate_package_spec_roots(const std::vector<std::filesystem::path>& roots) {
    if (roots.empty()) return;
    const auto dictionary = installed_dictionary_root();
    std::string message;
    for (const auto& root : roots)
        for (const auto& violation : package_spec_root_violations(root, dictionary))
            message += (message.empty() ? "" : "; ") + violation;
    if (!message.empty()) throw ManifestError(message);
}

// Python caller_package_spec_roots: the package spec roots the launcher
// injected for this process.
std::vector<std::filesystem::path> caller_package_spec_roots() {
    std::vector<std::filesystem::path> roots;
    std::stringstream entries(env_string_value("R2KERNEL_CONTRACT_SPEC_ROOTS"));
    std::string entry;
    while (std::getline(entries, entry, ':')) {
        if (entry.empty()) continue;
        const auto root = expand_user(entry);
        std::error_code error;
        if (!std::filesystem::is_directory(root, error)) continue;
        const auto resolved = resolved_path(root);
        if (std::find(roots.begin(), roots.end(), resolved) == roots.end()) roots.push_back(resolved);
    }
    return roots;
}
