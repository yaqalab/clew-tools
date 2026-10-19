#include "files.h"

#include <algorithm>
#include <chrono>
#include <cstring>

#include "text.h"

namespace clew {

namespace {

struct MimeEntry {
    const char* ext;
    const char* mime;
};

const MimeEntry kMimes[] = {
    {".mp3", "audio/mpeg"}, {".m4a", "audio/mp4"},  {".aac", "audio/aac"},   {".wav", "audio/wav"},
    {".ogg", "audio/ogg"},  {".oga", "audio/ogg"},  {".opus", "audio/ogg"},  {".flac", "audio/flac"},
    {".weba", "audio/webm"}, {".webm", "audio/webm"}, {".mp4", "audio/mp4"}, {".3gp", "audio/3gpp"},
    {".amr", "audio/amr"},  {".wma", "audio/x-ms-wma"},
};

bool device_name(const std::string& segment) {
    // CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an extension.
    std::string stem = to_lower(segment.substr(0, segment.find('.')));
    if (stem == "con" || stem == "prn" || stem == "aux" || stem == "nul") return true;
    return stem.size() == 4 && (starts_with(stem, "com") || starts_with(stem, "lpt")) && stem[3] >= '1' &&
           stem[3] <= '9';
}

std::int64_t modified_ms(const fs::directory_entry& e, std::error_code& ec) {
    auto t = e.last_write_time(ec);
    if (ec) return 0;
    // file_time_type's epoch differs by platform; go through the system clock.
    auto sys = std::chrono::time_point_cast<std::chrono::milliseconds>(
        t - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
    return sys.time_since_epoch().count();
}

}  // namespace

fs::path path_from_utf8(const std::string& s) { return fs::u8path(s); }

std::string path_to_utf8(const fs::path& p) {
    auto u = p.u8string();
    return std::string(u.begin(), u.end());
}

const char* mime_of(const fs::path& p) {
    std::string ext = to_lower(path_to_utf8(p.extension()));
    for (const auto& m : kMimes)
        if (ext == m.ext) return m.mime;
    return nullptr;
}

bool inside(const fs::path& root, const fs::path& candidate) {
    auto r = root.begin();
    auto c = candidate.begin();
    for (; r != root.end(); ++r, ++c) {
        if (r->empty()) continue;  // a trailing separator
        if (c == candidate.end() || *r != *c) return false;
    }
    return true;
}

std::vector<AudioFile> list_audio(const fs::path& root, bool* truncated) {
    std::vector<AudioFile> found;
    bool cut = false;
    std::vector<fs::path> stack{root};
    while (!stack.empty() && !cut) {
        fs::path here = stack.back();
        stack.pop_back();
        std::error_code ec;
        fs::directory_iterator it(here, fs::directory_options::skip_permission_denied, ec);
        if (ec) continue;
        for (const auto& e : it) {
            std::string name = path_to_utf8(e.path().filename());
            if (name.empty() || name[0] == '.') continue;
            std::error_code ec2;
            auto link = e.symlink_status(ec2);
            if (ec2) continue;
            bool plain_dir = link.type() == fs::file_type::directory;
            bool plain_file = link.type() == fs::file_type::regular;
            if (!plain_dir && !plain_file) {
                // A symlink or a junction: followed only when it stays inside the folder.
                auto real = fs::canonical(e.path(), ec2);
                if (ec2 || !inside(root, real)) continue;
                auto st = fs::status(real, ec2);
                if (ec2) continue;
                plain_dir = st.type() == fs::file_type::directory;
                plain_file = st.type() == fs::file_type::regular;
            }
            if (plain_dir) {
                stack.push_back(e.path());
                continue;
            }
            const char* mime = mime_of(e.path());
            if (!plain_file || mime == nullptr) continue;
            if (found.size() >= kMaxFiles) {
                cut = true;
                break;
            }
            AudioFile f;
            auto rel = e.path().lexically_relative(root).generic_u8string();
            f.path.assign(rel.begin(), rel.end());
            f.name = name;
            f.size = e.file_size(ec2);
            if (ec2) continue;
            f.modified_ms = modified_ms(e, ec2);
            f.mime = mime;
            found.push_back(std::move(f));
        }
    }
    std::sort(found.begin(), found.end(),
              [](const AudioFile& a, const AudioFile& b) { return to_lower(a.path) < to_lower(b.path); });
    if (truncated) *truncated = cut;
    return found;
}

std::optional<fs::path> resolve_audio(const fs::path& root, const std::string& rel) {
    if (rel.empty() || rel.find('\0') != std::string::npos || rel[0] == '/' || rel[0] == '\\' || !is_utf8(rel))
        return std::nullopt;
    if (rel.size() >= 2 && std::isalpha(static_cast<unsigned char>(rel[0])) && rel[1] == ':') return std::nullopt;
    std::string norm = rel;
    std::replace(norm.begin(), norm.end(), '\\', '/');
    fs::path path = root;
    std::size_t at = 0;
    while (at <= norm.size()) {
        std::size_t end = norm.find('/', at);
        if (end == std::string::npos) end = norm.size();
        std::string seg = norm.substr(at, end - at);
        if (seg.empty() || seg == "." || seg == ".." || seg[0] == '.' || seg.find(':') != std::string::npos ||
            seg.back() == '.' || seg.back() == ' ' || device_name(seg))
            return std::nullopt;
        for (unsigned char c : seg)
            if (c < 0x20 || std::strchr("<>\"|?*", c)) return std::nullopt;
        path /= path_from_utf8(seg);
        at = end + 1;
    }
    std::error_code ec;
    auto real = fs::canonical(path, ec);
    if (ec || !inside(root, real) || mime_of(real) == nullptr || !fs::is_regular_file(real, ec)) return std::nullopt;
    return real;
}

ByteRange parse_range(const std::string* header, std::uint64_t size) {
    ByteRange r;
    if (header == nullptr || header->empty()) return r;
    std::string h = trim(*header);
    r.kind = ByteRange::Bad;
    if (!starts_with(h, "bytes=")) return r;
    std::string spec = h.substr(6);
    auto dash = spec.find('-');
    if (dash == std::string::npos) return r;
    std::string a = spec.substr(0, dash), b = spec.substr(dash + 1);
    if ((!a.empty() && !all_digits(a)) || (!b.empty() && !all_digits(b)) || (a.empty() && b.empty())) return r;
    if (a.size() > 18 || b.size() > 18) return r;
    if (a.empty()) {
        std::uint64_t n = std::stoull(b);
        if (n == 0 || size == 0) return r;
        r.kind = ByteRange::Ok;
        r.first = n >= size ? 0 : size - n;
        r.last = size - 1;
        return r;
    }
    std::uint64_t first = std::stoull(a);
    std::uint64_t last = b.empty() ? (size ? size - 1 : 0) : std::stoull(b);
    if (first >= size || first > last) return r;
    r.kind = ByteRange::Ok;
    r.first = first;
    r.last = std::min(last, size - 1);
    return r;
}

}  // namespace clew
