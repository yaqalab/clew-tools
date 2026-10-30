#include "folders.h"

#include <set>

#include "text.h"

namespace clew {

namespace {

// The last segment of a path, ignoring a trailing separator; a drive's letter for a drive's root.
std::string last_segment(const std::string& utf8) {
    fs::path p = path_from_utf8(utf8);
    std::string name = path_to_utf8(p.filename());
    if (name.empty()) name = path_to_utf8(p.parent_path().filename());
    if (name.empty() || name == "/" || name == "\\") {
        name = path_to_utf8(p.root_name());
        if (!name.empty() && name.back() == ':') name.pop_back();
    }
    return name.empty() ? std::string("folder") : name;
}

}  // namespace

std::vector<std::string> folder_names(const std::vector<std::string>& paths) {
    std::vector<std::string> names;
    std::set<std::string> taken;  // lower case
    for (const auto& p : paths) {
        std::string base = last_segment(p);
        std::string name = base;
        for (int n = 2; taken.count(to_lower(name)); ++n) name = base + " " + std::to_string(n);
        taken.insert(to_lower(name));
        names.push_back(name);
    }
    return names;
}

std::vector<AudioFile> list_served(const std::vector<ServedFolder>& folders, bool* truncated) {
    std::vector<AudioFile> all;
    *truncated = false;
    for (const auto& f : folders) {
        if (!f.enabled || f.root.empty()) continue;
        bool cut = false;
        for (auto& file : list_audio(f.root, &cut)) {
            if (all.size() >= kMaxFiles) {
                *truncated = true;
                return all;
            }
            file.path = f.name + "/" + file.path;
            all.push_back(std::move(file));
        }
        if (cut) *truncated = true;
    }
    return all;
}

std::optional<fs::path> resolve_served(const std::vector<ServedFolder>& folders, const std::string& path) {
    std::size_t slash = path.find('/');
    if (slash != std::string::npos) {
        std::string first = path.substr(0, slash);
        for (const auto& f : folders) {
            if (f.name != first) continue;
            if (!f.enabled || f.root.empty()) return std::nullopt;
            return resolve_audio(f.root, path.substr(slash + 1));
        }
    }
    // A path saved when there was one folder: only where it is not ambiguous.
    std::optional<fs::path> found;
    for (const auto& f : folders) {
        if (!f.enabled || f.root.empty()) continue;
        if (auto hit = resolve_audio(f.root, path)) {
            if (found) return std::nullopt;
            found = hit;
        }
    }
    return found;
}

const ServedFolder* folder_of(const std::vector<ServedFolder>& folders, const fs::path& file) {
    for (const auto& f : folders)
        if (f.enabled && !f.root.empty() && inside(f.root, file)) return &f;
    return nullptr;
}

}  // namespace clew
