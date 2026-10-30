#include "json.h"

#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

#include "text.h"

namespace clew::json {

namespace {

const Value kNull;

void write_string(std::string& out, const std::string& s) {
    static const char* hex = "0123456789abcdef";
    auto u = [&](unsigned v) {
        out += "\\u";
        out += hex[(v >> 12) & 15];
        out += hex[(v >> 8) & 15];
        out += hex[(v >> 4) & 15];
        out += hex[v & 15];
    };
    out += '"';
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c < 0x80) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                case '\b': out += "\\b"; break;
                case '\f': out += "\\f"; break;
                default:
                    if (c < 0x20)
                        u(c);
                    else
                        out += char(c);
            }
            ++i;
            continue;
        }
        // A UTF-8 sequence, written as \u escapes (a pair above U+FFFF). Bytes that are not UTF-8
        // become U+FFFD rather than invalid JSON.
        int n = (c & 0xE0) == 0xC0 ? 1 : (c & 0xF0) == 0xE0 ? 2 : (c & 0xF8) == 0xF0 ? 3 : -1;
        std::uint32_t cp = n == 1 ? c & 0x1F : n == 2 ? c & 0x0F : c & 0x07;
        bool ok = n > 0;
        for (int k = 1; ok && k <= n; ++k) {
            if (i + k >= s.size()) {
                ok = false;
                break;
            }
            unsigned char cc = static_cast<unsigned char>(s[i + k]);
            if ((cc & 0xC0) != 0x80) ok = false;
            cp = (cp << 6) | (cc & 0x3F);
        }
        if (!ok) {
            u(0xFFFD);
            ++i;
            continue;
        }
        if (cp >= 0x10000) {
            cp -= 0x10000;
            u(0xD800 + (cp >> 10));
            u(0xDC00 + (cp & 0x3FF));
        } else {
            u(cp);
        }
        i += n + 1;
    }
    out += '"';
}

void newline(std::string& out, int indent, int depth) {
    if (indent < 0) return;
    out += '\n';
    out.append(std::size_t(indent * depth), ' ');
}

class Parser {
public:
    explicit Parser(const std::string& s) : s_(s) {}

    std::optional<Value> run() {
        auto v = value(0);
        ws();
        if (!v || at_ != s_.size()) return std::nullopt;
        return v;
    }

private:
    void ws() {
        while (at_ < s_.size() && (s_[at_] == ' ' || s_[at_] == '\t' || s_[at_] == '\n' || s_[at_] == '\r')) ++at_;
    }

    bool lit(const char* word) {
        std::size_t n = std::char_traits<char>::length(word);
        if (s_.compare(at_, n, word) != 0) return false;
        at_ += n;
        return true;
    }

    std::optional<Value> value(int depth) {
        if (depth > 32) return std::nullopt;
        ws();
        if (at_ >= s_.size()) return std::nullopt;
        char c = s_[at_];
        if (c == '{') return object(depth);
        if (c == '[') return array(depth);
        if (c == '"') {
            auto s = string();
            if (!s) return std::nullopt;
            return Value(*s);
        }
        if (lit("true")) return Value(true);
        if (lit("false")) return Value(false);
        if (lit("null")) return Value(nullptr);
        return number();
    }

    std::optional<Value> number() {
        std::size_t start = at_;
        if (at_ < s_.size() && s_[at_] == '-') ++at_;
        if (at_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[at_]))) return std::nullopt;
        if (s_[at_] == '0') {
            ++at_;
        } else {
            while (at_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[at_]))) ++at_;
        }
        if (at_ < s_.size() && s_[at_] == '.') {
            ++at_;
            if (at_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[at_]))) return std::nullopt;
            while (at_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[at_]))) ++at_;
        }
        if (at_ < s_.size() && (s_[at_] == 'e' || s_[at_] == 'E')) {
            ++at_;
            if (at_ < s_.size() && (s_[at_] == '+' || s_[at_] == '-')) ++at_;
            if (at_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[at_]))) return std::nullopt;
            while (at_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[at_]))) ++at_;
        }
        double v = std::strtod(s_.substr(start, at_ - start).c_str(), nullptr);
        if (!std::isfinite(v)) return std::nullopt;
        return Value(v);
    }

    static void put_utf8(std::string& out, std::uint32_t cp) {
        if (cp < 0x80) {
            out += char(cp);
        } else if (cp < 0x800) {
            out += char(0xC0 | (cp >> 6));
            out += char(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += char(0xE0 | (cp >> 12));
            out += char(0x80 | ((cp >> 6) & 0x3F));
            out += char(0x80 | (cp & 0x3F));
        } else {
            out += char(0xF0 | (cp >> 18));
            out += char(0x80 | ((cp >> 12) & 0x3F));
            out += char(0x80 | ((cp >> 6) & 0x3F));
            out += char(0x80 | (cp & 0x3F));
        }
    }

    std::optional<std::uint32_t> hex4() {
        if (at_ + 4 > s_.size()) return std::nullopt;
        std::uint32_t v = 0;
        for (int k = 0; k < 4; ++k) {
            char c = s_[at_ + k];
            int d = c >= '0' && c <= '9'   ? c - '0'
                    : c >= 'a' && c <= 'f' ? c - 'a' + 10
                    : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                           : -1;
            if (d < 0) return std::nullopt;
            v = v * 16 + std::uint32_t(d);
        }
        at_ += 4;
        return v;
    }

    std::optional<std::string> string() {
        ++at_;  // the opening quote
        std::string out;
        while (at_ < s_.size()) {
            char c = s_[at_++];
            if (c == '"') {
                if (!is_utf8(out)) return std::nullopt;
                return out;
            }
            if (static_cast<unsigned char>(c) < 0x20) return std::nullopt;
            if (c != '\\') {
                out += c;
                continue;
            }
            if (at_ >= s_.size()) return std::nullopt;
            char e = s_[at_++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    auto hi = hex4();
                    if (!hi) return std::nullopt;
                    std::uint32_t cp = *hi;
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (!lit("\\u")) return std::nullopt;
                        auto lo = hex4();
                        if (!lo || *lo < 0xDC00 || *lo > 0xDFFF) return std::nullopt;
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (*lo - 0xDC00);
                    } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                        return std::nullopt;
                    }
                    put_utf8(out, cp);
                    break;
                }
                default: return std::nullopt;
            }
        }
        return std::nullopt;
    }

    std::optional<Value> array(int depth) {
        ++at_;
        Value v = Value::array();
        ws();
        if (at_ < s_.size() && s_[at_] == ']') {
            ++at_;
            return v;
        }
        while (true) {
            auto item = value(depth + 1);
            if (!item) return std::nullopt;
            v.push(std::move(*item));
            ws();
            if (at_ >= s_.size()) return std::nullopt;
            if (s_[at_] == ',') {
                ++at_;
                continue;
            }
            if (s_[at_] == ']') {
                ++at_;
                return v;
            }
            return std::nullopt;
        }
    }

    std::optional<Value> object(int depth) {
        ++at_;
        Value v = Value::object();
        ws();
        if (at_ < s_.size() && s_[at_] == '}') {
            ++at_;
            return v;
        }
        while (true) {
            ws();
            if (at_ >= s_.size() || s_[at_] != '"') return std::nullopt;
            auto key = string();
            if (!key) return std::nullopt;
            ws();
            if (at_ >= s_.size() || s_[at_] != ':') return std::nullopt;
            ++at_;
            auto item = value(depth + 1);
            if (!item) return std::nullopt;
            v.set(*key, std::move(*item));
            ws();
            if (at_ >= s_.size()) return std::nullopt;
            if (s_[at_] == ',') {
                ++at_;
                continue;
            }
            if (s_[at_] == '}') {
                ++at_;
                return v;
            }
            return std::nullopt;
        }
    }

    const std::string& s_;
    std::size_t at_ = 0;
};

}  // namespace

const Value& Value::operator[](const std::string& key) const {
    if (kind_ != Kind::Object) return kNull;
    for (const auto& m : members_)
        if (m.first == key) return m.second;
    return kNull;
}

Value& Value::set(const std::string& key, Value v) {
    kind_ = Kind::Object;
    for (auto& m : members_) {
        if (m.first == key) {
            m.second = std::move(v);
            return *this;
        }
    }
    members_.emplace_back(key, std::move(v));
    return *this;
}

Value& Value::push(Value v) {
    kind_ = Kind::Array;
    items_.push_back(std::move(v));
    return *this;
}

void Value::write(std::string& out, int indent, int depth) const {
    switch (kind_) {
        case Kind::Null: out += "null"; break;
        case Kind::Bool: out += bool_ ? "true" : "false"; break;
        case Kind::Number: {
            char buf[32];
            if (std::floor(number_) == number_ && std::fabs(number_) < 9.007199254740992e15)
                std::snprintf(buf, sizeof buf, "%lld", static_cast<long long>(number_));
            else
                std::snprintf(buf, sizeof buf, "%.17g", number_);
            out += buf;
            break;
        }
        case Kind::String: write_string(out, string_); break;
        case Kind::Array: {
            out += '[';
            for (std::size_t i = 0; i < items_.size(); ++i) {
                if (i) out += indent < 0 ? ", " : ",";
                newline(out, indent, depth + 1);
                items_[i].write(out, indent, depth + 1);
            }
            if (!items_.empty()) newline(out, indent, depth);
            out += ']';
            break;
        }
        case Kind::Object: {
            out += '{';
            for (std::size_t i = 0; i < members_.size(); ++i) {
                if (i) out += indent < 0 ? ", " : ",";
                newline(out, indent, depth + 1);
                write_string(out, members_[i].first);
                out += ": ";
                members_[i].second.write(out, indent, depth + 1);
            }
            if (!members_.empty()) newline(out, indent, depth);
            out += '}';
            break;
        }
    }
}

std::string Value::dump(int indent) const {
    std::string out;
    write(out, indent, 0);
    return out;
}

std::optional<Value> parse(const std::string& text) { return Parser(text).run(); }

}  // namespace clew::json
