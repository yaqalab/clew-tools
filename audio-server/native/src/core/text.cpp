#include "text.h"

#include <cctype>

namespace clew {

namespace {

const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int b64_value(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
}

int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

}  // namespace

std::string base64_encode(const std::string& bytes) {
    std::string out;
    out.reserve((bytes.size() + 2) / 3 * 4);
    std::size_t i = 0;
    for (; i + 2 < bytes.size(); i += 3) {
        std::uint32_t v = std::uint32_t(std::uint8_t(bytes[i])) << 16 | std::uint32_t(std::uint8_t(bytes[i + 1])) << 8 |
                          std::uint8_t(bytes[i + 2]);
        out += B64[v >> 18];
        out += B64[(v >> 12) & 63];
        out += B64[(v >> 6) & 63];
        out += B64[v & 63];
    }
    if (i + 1 == bytes.size()) {
        std::uint32_t v = std::uint32_t(std::uint8_t(bytes[i])) << 16;
        out += B64[v >> 18];
        out += B64[(v >> 12) & 63];
        out += "==";
    } else if (i + 2 == bytes.size()) {
        std::uint32_t v = std::uint32_t(std::uint8_t(bytes[i])) << 16 | std::uint32_t(std::uint8_t(bytes[i + 1])) << 8;
        out += B64[v >> 18];
        out += B64[(v >> 12) & 63];
        out += B64[(v >> 6) & 63];
        out += '=';
    }
    return out;
}

std::optional<std::string> base64_decode(const std::string& text) {
    std::string out;
    std::uint32_t acc = 0;
    int bits = 0;
    for (char c : text) {
        if (c == '=') break;
        if (c == '\r' || c == '\n') continue;
        int v = b64_value(c);
        if (v < 0) return std::nullopt;
        acc = (acc << 6) | std::uint32_t(v);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out += char((acc >> bits) & 0xFF);
        }
    }
    return out;
}

std::string base64url_encode(const std::string& bytes) {
    std::string s = base64_encode(bytes);
    while (!s.empty() && s.back() == '=') s.pop_back();
    for (char& c : s) {
        if (c == '+') c = '-';
        if (c == '/') c = '_';
    }
    return s;
}

std::string percent_encode(const std::string& s) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : s) {
        if (std::isalnum(c) || c == '_' || c == '.' || c == '-' || c == '~') {
            out += char(c);
        } else {
            out += '%';
            out += hex[c >> 4];
            out += hex[c & 15];
        }
    }
    return out;
}

std::string percent_decode(const std::string& s) {
    std::string out;
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '+') {
            out += ' ';
        } else if (s[i] == '%' && i + 2 < s.size() && hex_value(s[i + 1]) >= 0 && hex_value(s[i + 2]) >= 0) {
            out += char(hex_value(s[i + 1]) * 16 + hex_value(s[i + 2]));
            i += 2;
        } else {
            out += s[i];
        }
    }
    return out;
}

std::map<std::string, std::string> parse_query(const std::string& query) {
    std::map<std::string, std::string> out;
    std::size_t at = 0;
    while (at <= query.size()) {
        std::size_t end = query.find('&', at);
        if (end == std::string::npos) end = query.size();
        std::string part = query.substr(at, end - at);
        if (!part.empty()) {
            std::size_t eq = part.find('=');
            std::string key = percent_decode(part.substr(0, eq));
            std::string value = eq == std::string::npos ? "" : percent_decode(part.substr(eq + 1));
            // parse_qs drops blank values; the first value of a key wins.
            if (!value.empty() && out.find(key) == out.end()) out.emplace(key, value);
        }
        at = end + 1;
    }
    return out;
}

std::string to_lower(std::string s) {
    for (char& c : s) c = char(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string trim(const std::string& s) {
    std::size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) ++a;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) --b;
    return s.substr(a, b - a);
}

bool starts_with(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool all_digits(const std::string& s) {
    if (s.empty()) return false;
    for (unsigned char c : s)
        if (c < '0' || c > '9') return false;
    return true;
}

bool is_utf8(const std::string& s) {
    std::size_t i = 0;
    while (i < s.size()) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        int n;
        std::uint32_t cp;
        if (c < 0x80) {
            ++i;
            continue;
        } else if ((c & 0xE0) == 0xC0) {
            n = 1;
            cp = c & 0x1F;
        } else if ((c & 0xF0) == 0xE0) {
            n = 2;
            cp = c & 0x0F;
        } else if ((c & 0xF8) == 0xF0) {
            n = 3;
            cp = c & 0x07;
        } else {
            return false;
        }
        if (i + n >= s.size()) return false;
        for (int k = 1; k <= n; ++k) {
            unsigned char cc = static_cast<unsigned char>(s[i + k]);
            if ((cc & 0xC0) != 0x80) return false;
            cp = (cp << 6) | (cc & 0x3F);
        }
        if ((n == 1 && cp < 0x80) || (n == 2 && cp < 0x800) || (n == 3 && cp < 0x10000) || cp > 0x10FFFF ||
            (cp >= 0xD800 && cp <= 0xDFFF))
            return false;
        i += n + 1;
    }
    return true;
}

}  // namespace clew
