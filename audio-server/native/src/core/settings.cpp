#include "settings.h"

#include <fstream>
#include <sstream>

#include "files.h"
#include "json.h"
#include "text.h"

namespace clew {

namespace {

int clamp_int(const json::Value& v, int lo, int hi, int fallback) {
    if (!v.is_number()) return fallback;
    double n = v.num();
    if (n < lo || n > hi) return fallback;
    return int(n);
}

}  // namespace

std::filesystem::path default_settings_file() { return platform::config_dir() / "settings.json"; }

Settings Settings::load(const std::filesystem::path& file) {
    Settings s;
    std::ifstream in(file, std::ios::binary);
    if (!in) return s;
    std::stringstream buf;
    buf << in.rdbuf();
    std::string text = buf.str();
    if (starts_with(text, "\xEF\xBB\xBF")) text = text.substr(3);
    auto v = json::parse(text);
    if (!v || !v->is_object() || !(*v)["v"].is_number() || (*v)["v"].num() != 1) return s;
    const auto& d = *v;
    if (d["folders"].is_array()) {
        for (const auto& f : d["folders"].items()) {
            if (!f.is_object() || !f["path"].is_string() || f["path"].str().empty()) continue;
            s.folders.push_back({f["path"].str(), !f["enabled"].is_bool() || f["enabled"].boolean()});
        }
    } else if (d["folder"].is_string() && !d["folder"].str().empty()) {
        // Written before there could be several (2026-10-17).
        s.folders.push_back({d["folder"].str(), true});
    }
    s.port = clamp_int(d["port"], 1024, 65535, kDefaultPort);
    if (d["origins"].is_array()) {
        s.origins.clear();  // what was saved, even an empty list, replaces the first-start default
        for (const auto& o : d["origins"].items()) {
            if (!o.is_string()) continue;
            if (auto n = normalize_origin(o.str())) s.origins.push_back(*n);
        }
    }
    s.link_hours = clamp_int(d["linkHours"], 1, 24 * 7, kDefaultLinkHours);
    if (d["convert"].is_bool()) s.convert = d["convert"].boolean();
    if (d["prepare"].is_bool()) s.prepare = d["prepare"].boolean();
    s.cache_mb = clamp_int(d["cacheMb"], 256, 1024 * 1024, kDefaultCacheMb);
    if (d["ffmpeg"].is_string()) s.ffmpeg = d["ffmpeg"].str();
    if (d["systemCodecs"].is_bool()) s.system_codecs = d["systemCodecs"].boolean();
    const auto& secret = d["secret"];
    if (secret["kind"].is_string() && secret["data"].is_string())
        s.secret = platform::Sealed{secret["kind"].str(), secret["data"].str()};
    if (d["sttEnabled"].is_bool()) s.stt_enabled = d["sttEnabled"].boolean();
    if (d["sttModel"].is_string() && stt_model_known(d["sttModel"].str())) s.stt_model = d["sttModel"].str();
    if (d["sttModelsDir"].is_string()) s.stt_models_dir = d["sttModelsDir"].str();
    if (d["sttLanguage"].is_string() && stt_language_known(d["sttLanguage"].str()))
        s.stt_language = d["sttLanguage"].str();
    s.stt_port = clamp_int(d["sttPort"], 1024, 65535, kDefaultSttPort);
    const auto& stt_secret = d["sttSecret"];
    if (stt_secret["kind"].is_string() && stt_secret["data"].is_string())
        s.stt_secret = platform::Sealed{stt_secret["kind"].str(), stt_secret["data"].str()};
    return s;
}

bool Settings::save(const std::filesystem::path& file) const {
    json::Value d = json::Value::object();
    d.set("v", 1);
    json::Value list = json::Value::array();
    for (const auto& f : folders) {
        json::Value item = json::Value::object();
        item.set("path", f.path);
        item.set("enabled", f.enabled);
        list.push(item);
    }
    d.set("folders", list);
    // The first one also as before, so an older copy of the program still finds a folder.
    d.set("folder", folders.empty() ? std::string() : folders.front().path);
    d.set("port", port);
    json::Value o = json::Value::array();
    for (const auto& origin : origins) o.push(origin);
    d.set("origins", o);
    d.set("linkHours", link_hours);
    d.set("convert", convert);
    d.set("prepare", prepare);
    d.set("cacheMb", cache_mb);
    d.set("ffmpeg", ffmpeg);
    d.set("systemCodecs", system_codecs);
    if (secret) {
        json::Value sv = json::Value::object();
        sv.set("kind", secret->kind);
        sv.set("data", secret->data);
        d.set("secret", sv);
    }
    d.set("sttEnabled", stt_enabled);
    d.set("sttModel", stt_model);
    d.set("sttModelsDir", stt_models_dir);
    d.set("sttLanguage", stt_language);
    d.set("sttPort", stt_port);
    if (stt_secret) {
        json::Value sv = json::Value::object();
        sv.set("kind", stt_secret->kind);
        sv.set("data", stt_secret->data);
        d.set("sttSecret", sv);
    }
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
    if (ec) {
        std::filesystem::remove(tmp, ec);
        return false;
    }
    return true;
}

std::optional<std::string> normalize_origin(const std::string& text) {
    std::string s = trim(text);
    while (!s.empty() && s.back() == '/') s.pop_back();
    std::string lower = to_lower(s);
    std::size_t scheme = starts_with(lower, "https://") ? 8 : starts_with(lower, "http://") ? 7 : 0;
    if (scheme == 0 || s.size() == scheme) return std::nullopt;
    // host[:port], no path, no user info, no wildcard.
    std::string rest = lower.substr(scheme);
    for (char c : rest) {
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '-' || c == ':' || c == '[' ||
                  c == ']';
        if (!ok) return std::nullopt;
    }
    if (rest.front() == '.' || rest.front() == ':' || rest.front() == '-') return std::nullopt;
    auto colon = rest.rfind(':');
    if (colon != std::string::npos && rest.find(']') == std::string::npos) {
        std::string port = rest.substr(colon + 1);
        if (!all_digits(port) || port.size() > 5 || std::stoi(port) == 0 || std::stoi(port) > 65535)
            return std::nullopt;
    }
    return lower;
}

std::string new_secret() { return base64url_encode(platform::random_bytes(24)); }

std::string new_stt_key() { return "stt.local." + new_secret(); }

const SttModel* find_stt_model(const std::string& name) {
    for (const auto& m : kSttModels)
        if (name == m.name) return &m;
    return nullptr;
}

bool stt_model_known(const std::string& name) { return find_stt_model(name) != nullptr; }

std::string stt_model_blocker(const SttModel& model, std::uint64_t memory_bytes, int gpus) {
    // Windows reports a little less than the memory installed ("16 GB" reads as 15.7).
    std::uint64_t needed = std::uint64_t(model.memory_gb) * 1024 * 1024 * 1024 / 100 * 95;
    if (memory_bytes && memory_bytes < needed) return "memory";
    if (model.use == SttModel::Gpu && gpus == 0) return "gpu";
    return "";
}

bool stt_language_known(const std::string& code) {
    return code.empty() || code == "en" || code == "ja" || code == "fr" || code == "mg";
}

}  // namespace clew
