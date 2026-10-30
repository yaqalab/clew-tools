#include "update.h"

#include <fstream>
#include <sstream>

#include "json.h"
#include "platform.h"
#include "text.h"

namespace clew::update {

namespace {

bool lower_hex(const std::string& s, std::size_t n) {
    if (s.size() != n) return false;
    for (char c : s)
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    return true;
}

}  // namespace

std::optional<Version> parse_version(const std::string& text) {
    Version v;
    int* parts[] = {&v.major, &v.minor, &v.patch};
    std::size_t at = 0;
    for (int i = 0; i < 3; ++i) {
        std::size_t start = at;
        int n = 0;
        while (at < text.size() && text[at] >= '0' && text[at] <= '9' && at - start < 4) n = n * 10 + (text[at++] - '0');
        if (at == start) return std::nullopt;
        *parts[i] = n;
        if (i < 2) {
            if (at >= text.size() || text[at] != '.') return std::nullopt;
            ++at;
        }
    }
    if (at != text.size()) return std::nullopt;
    return v;
}

int compare(const Version& a, const Version& b) {
    if (a.major != b.major) return a.major < b.major ? -1 : 1;
    if (a.minor != b.minor) return a.minor < b.minor ? -1 : 1;
    if (a.patch != b.patch) return a.patch < b.patch ? -1 : 1;
    return 0;
}

std::optional<Release> parse_manifest(const std::string& text) {
    auto v = json::parse(text);
    if (!v || !v->is_object()) return std::nullopt;
    const auto& d = *v;
    if (!d["version"].is_string() || !d["installer"].is_string() || !d["sha256"].is_string() || !d["size"].is_number())
        return std::nullopt;
    Release r;
    r.version = d["version"].str();
    r.installer = d["installer"].str();
    r.sha256 = d["sha256"].str();
    double size = d["size"].num();
    if (!parse_version(r.version) || r.installer != installer_name(r.version) || !lower_hex(r.sha256, 64)) return std::nullopt;
    if (!(size >= 1 && size <= 1024.0 * 1024 * 1024) || size != double(std::uint64_t(size))) return std::nullopt;
    r.size = std::uint64_t(size);
    return r;
}

std::string installer_name(const std::string& version) { return "clew-audio-server-setup-" + version + ".exe"; }

std::string manifest_url() {
    return std::string("https://github.com/") + kRepository + "/releases/latest/download/latest.json";
}

std::string installer_url(const Release& r) {
    return std::string("https://github.com/") + kRepository + "/releases/download/v" + r.version + "/" +
           installer_name(r.version);
}

std::string release_page(const std::string& version) {
    return std::string("https://github.com/") + kRepository + "/releases/tag/v" + version;
}

std::filesystem::path state_file() { return platform::config_dir() / "update.json"; }

State load_state(const std::filesystem::path& file) {
    State s;
    std::ifstream in(file, std::ios::binary);
    if (!in) return s;
    std::stringstream buf;
    buf << in.rdbuf();
    auto v = json::parse(buf.str());
    if (!v || !v->is_object()) return s;
    const auto& d = *v;
    if (d["check"].is_bool()) s.check = d["check"].boolean();
    if (d["lastCheck"].is_number() && d["lastCheck"].num() >= 0) s.last_check = std::int64_t(d["lastCheck"].num());
    if (d["notified"].is_string() && parse_version(d["notified"].str())) s.notified = d["notified"].str();
    return s;
}

bool save_state(const std::filesystem::path& file, const State& s) {
    json::Value d = json::Value::object();
    d.set("check", s.check);
    d.set("lastCheck", static_cast<long long>(s.last_check));
    d.set("notified", s.notified);
    std::error_code ec;
    std::filesystem::create_directories(file.parent_path(), ec);
    auto tmp = file;
    tmp += ".tmp";
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return false;
        out << d.dump(2) << "\n";
        if (!out) return false;
    }
    std::filesystem::rename(tmp, file, ec);
    return !ec;
}

bool check_due(const State& s, std::int64_t now) {
    constexpr std::int64_t kDay = 24 * 60 * 60;
    // A clock set back makes last_check lie in the future: check rather than wait for it.
    return s.check && (now - s.last_check >= kDay || now < s.last_check);
}

}  // namespace clew::update
