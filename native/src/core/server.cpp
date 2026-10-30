#include "server.h"

#include <chrono>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using sock_t = SOCKET;
static const sock_t kBadSocket = INVALID_SOCKET;
static void close_sock(sock_t s) { closesocket(s); }
static void shutdown_sock(sock_t s) { shutdown(s, SD_BOTH); }
static const int kSendFlags = 0;
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
using sock_t = int;
static const sock_t kBadSocket = -1;
static void close_sock(sock_t s) { ::close(s); }
static void shutdown_sock(sock_t s) { shutdown(s, SHUT_RDWR); }
#ifdef MSG_NOSIGNAL
static const int kSendFlags = MSG_NOSIGNAL;
#else
static const int kSendFlags = 0;
#endif
#endif

#include "files.h"
#include "json.h"
#include "platform.h"
#include "sha256.h"
#include "text.h"
#include "version.h"

namespace clew {

const char* const kVersion = CLEW_VERSION;

namespace {

constexpr std::size_t kMaxHead = 16 * 1024;
constexpr std::size_t kMaxBody = 8 * 1024;
constexpr int kMaxConnections = 64;
constexpr std::size_t kChunk = 64 * 1024;

const char* reason(int status) {
    switch (status) {
        case 200: return "OK";
        case 204: return "No Content";
        case 206: return "Partial Content";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 403: return "Forbidden";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 413: return "Payload Too Large";
        case 416: return "Range Not Satisfiable";
        case 431: return "Request Header Fields Too Large";
        case 501: return "Not Implemented";
        case 503: return "Service Unavailable";
        case 505: return "HTTP Version Not Supported";
        default: return "Status";
    }
}

// host, then an optional :port, nothing else.
bool loopback_name(const std::string& s, std::size_t* end) {
    for (const char* name : {"localhost", "127.0.0.1", "[::1]"}) {
        std::size_t n = std::strlen(name);
        if (to_lower(s.substr(0, n)) == name) {
            *end = n;
            return true;
        }
    }
    return false;
}

bool optional_port(const std::string& s, std::size_t at) {
    if (at == s.size()) return true;
    if (s[at] != ':') return false;
    std::string port = s.substr(at + 1);
    return all_digits(port) && port.size() <= 5;
}

void set_timeouts(sock_t s) {
#ifdef _WIN32
    DWORD recv_ms = 30000, send_ms = 60000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&recv_ms), sizeof recv_ms);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&send_ms), sizeof send_ms);
#else
    timeval recv_tv{30, 0}, send_tv{60, 0};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &recv_tv, sizeof recv_tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &send_tv, sizeof send_tv);
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
#endif
}

struct Request {
    std::string method, target, version, path, query;
    std::map<std::string, std::string> headers;
    int hosts = 0;
    bool keep_alive = true;
    std::string body;

    const std::string* header(const std::string& name) const {
        auto it = headers.find(name);
        return it == headers.end() ? nullptr : &it->second;
    }
};

}  // namespace

bool loopback_origin(const std::string& origin) {
    std::string o = to_lower(origin);
    std::size_t scheme = starts_with(o, "http://") ? 7 : starts_with(o, "https://") ? 8 : 0;
    if (scheme == 0) return false;
    std::string rest = o.substr(scheme);
    std::size_t end;
    return loopback_name(rest, &end) && optional_port(rest, end);
}

bool loopback_host(const std::string& host) {
    std::size_t end;
    return loopback_name(host, &end) && optional_port(host, end);
}

// ---------------------------------------------------------------------------------------------
// One connection: requests one after another (keep-alive), until either side closes.
// ---------------------------------------------------------------------------------------------

struct Server::Connection {
    Server& server;
    sock_t sock;
    std::string buf;  // bytes received and not yet used
    std::shared_ptr<const ServerConfig> cfg;
    const Request* req = nullptr;
    bool close = false;   // close after this answer
    bool broken = false;  // the peer went away: send nothing more

    bool send_all(const char* data, std::size_t len) {
        while (len > 0) {
            int n = send(sock, data, int(len > (1 << 20) ? (1 << 20) : len), kSendFlags);
            if (n <= 0) return false;
            data += n;
            len -= std::size_t(n);
        }
        return true;
    }

    // Reads until `buf` holds `want` bytes. False when the peer went away or timed out.
    bool fill(std::size_t want) {
        char tmp[8192];
        while (buf.size() < want) {
            int n = recv(sock, tmp, sizeof tmp, 0);
            if (n <= 0) return false;
            buf.append(tmp, std::size_t(n));
        }
        return true;
    }

    // 0: a request was read; otherwise the status to refuse it with (-1: just close).
    int read_request(Request& r) {
        std::size_t end;
        char tmp[8192];
        while ((end = buf.find("\r\n\r\n")) == std::string::npos) {
            if (buf.size() > kMaxHead) return 431;
            int n = recv(sock, tmp, sizeof tmp, 0);
            if (n <= 0) return -1;
            buf.append(tmp, std::size_t(n));
        }
        if (end > kMaxHead) return 431;
        std::string head = buf.substr(0, end);
        buf.erase(0, end + 4);

        std::size_t line_end = head.find("\r\n");
        std::string line = head.substr(0, line_end);
        std::size_t a = line.find(' '), b = line.rfind(' ');
        if (a == std::string::npos || a == b) return 400;
        r.method = line.substr(0, a);
        r.target = line.substr(a + 1, b - a - 1);
        r.version = line.substr(b + 1);
        if (r.version != "HTTP/1.1" && r.version != "HTTP/1.0") return 505;
        if (r.target.empty() || r.target[0] != '/' || r.target.find(' ') != std::string::npos) return 400;
        std::size_t q = r.target.find('?');
        r.path = r.target.substr(0, q);
        r.query = q == std::string::npos ? "" : r.target.substr(q + 1);

        std::size_t at = line_end == std::string::npos ? head.size() : line_end + 2;
        while (at < head.size()) {
            std::size_t next = head.find("\r\n", at);
            if (next == std::string::npos) next = head.size();
            std::string h = head.substr(at, next - at);
            at = next + 2;
            if (h.empty()) continue;
            if (h[0] == ' ' || h[0] == '\t') return 400;  // obsolete line folding
            std::size_t colon = h.find(':');
            if (colon == std::string::npos || colon == 0) return 400;
            std::string name = to_lower(h.substr(0, colon));
            for (char c : name)
                if (c <= ' ' || c == '"' || c == '(' || c == ')' || c == ',' || c == '/' || c == ':' || c == ';' ||
                    c == '<' || c == '=' || c == '>' || c == '?' || c == '@' || c == '[' || c == '\\' || c == ']' ||
                    c == '{' || c == '}' || static_cast<unsigned char>(c) >= 0x7F)
                    return 400;
            std::string value = trim(h.substr(colon + 1));
            if (name == "host") ++r.hosts;
            auto it = r.headers.find(name);
            if (it == r.headers.end())
                r.headers.emplace(name, value);
            else
                it->second += ", " + value;
        }
        std::string conn = to_lower(r.headers.count("connection") ? r.headers["connection"] : "");
        r.keep_alive = r.version == "HTTP/1.1" ? conn.find("close") == std::string::npos
                                                : conn.find("keep-alive") != std::string::npos;
        // Chunked bodies are never needed here; refusing them closes a request-smuggling door.
        if (r.headers.count("transfer-encoding")) return 501;
        if (const std::string* cl = r.header("content-length")) {
            if (!all_digits(*cl) || cl->size() > 9) return 400;
            std::size_t n = std::stoul(*cl);
            if (n > kMaxBody) return 413;
            if (!fill(n)) return -1;
            r.body = buf.substr(0, n);
            buf.erase(0, n);
        }
        return 0;
    }

    void log(int status) {
        // Method and path only; a link's query carries its signature.
        if (server.log_) server.log_(req->method + " " + req->path + " -> " + std::to_string(status));
    }

    std::string cors() {
        const std::string* origin = req->header("origin");
        if (!origin || !origin_ok(*origin)) return "";
        return "Access-Control-Allow-Origin: " + *origin +
               "\r\nVary: Origin\r\nAccess-Control-Expose-Headers: Content-Range, Accept-Ranges, Content-Length\r\n";
    }

    bool origin_ok(const std::string& origin) {
        return loopback_origin(origin) || (cfg && cfg->origins.count(to_lower(origin)) > 0);
    }

    void head(int status, const std::string& extra) {
        std::string h = std::string("HTTP/1.1 ") + std::to_string(status) + " " + reason(status) + "\r\n" +
                        "Server: ClewAudioServer/" + kVersion + "\r\nX-Content-Type-Options: nosniff\r\n" + cors() +
                        extra;
        if (close) h += "Connection: close\r\n";
        h += "\r\n";
        if (!send_all(h.data(), h.size())) broken = close = true;
        log(status);
    }

    void json_reply(int status, const json::Value& body) {
        std::string raw = body.dump();
        head(status, "Content-Type: application/json; charset=utf-8\r\nContent-Length: " + std::to_string(raw.size()) +
                         "\r\nCache-Control: no-store\r\n");
        if (req->method != "HEAD" && !broken) {
            if (!send_all(raw.data(), raw.size())) broken = close = true;
        }
    }

    void refuse(int status, const char* why) {
        json::Value v = json::Value::object();
        v.set("error", why);
        json_reply(status, v);
    }

    bool guard() {
        const std::string* host = req->header("host");
        if (req->hosts != 1 || !host || !loopback_host(*host)) {
            refuse(403, "host");
            return false;
        }
        const std::string* origin = req->header("origin");
        if (origin && !origin_ok(*origin)) {
            refuse(403, "origin");
            return false;
        }
        return true;
    }

    bool auth() {
        const std::string* h = req->header("authorization");
        bool ok = false;
        if (h && starts_with(*h, "Bearer ") && cfg && !cfg->secret.empty()) {
            std::string given = trim(h->substr(7));
            auto a = sha256(given), b = sha256(cfg->secret);
            ok = equal_constant_time(std::string(a.begin(), a.end()), std::string(b.begin(), b.end()));
        }
        bool slow;
        {
            std::lock_guard<std::mutex> g(server.lock_);
            server.failures_ = ok ? 0 : server.failures_ + 1;
            slow = server.failures_ >= 5;
        }
        if (!ok) {
            // Five wrong tries in a row slow every further answer down.
            if (slow) std::this_thread::sleep_for(std::chrono::seconds(1));
            refuse(401, "secret");
        }
        return ok;
    }

    void handle() {
        const Request& r = *req;
        if (r.method != "GET" && r.method != "HEAD" && r.method != "POST" && r.method != "OPTIONS") {
            close = true;
            refuse(501, "method");
            return;
        }
        if (!guard()) return;
        if (r.method == "OPTIONS") {
            std::string extra =
                "Access-Control-Allow-Methods: GET, HEAD, POST, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Authorization, Content-Type, Range\r\n"
                "Access-Control-Max-Age: 600\r\nContent-Length: 0\r\n";
            const std::string* pna = r.header("access-control-request-private-network");
            if (pna && *pna == "true") extra += "Access-Control-Allow-Private-Network: true\r\n";
            head(204, extra);
            return;
        }
        if (r.method == "POST") {
            if (r.path == "/api/url")
                url();
            else
                refuse(404, "not found");
            return;
        }
        if (r.path == "/api/ping") {
            // No secret: lets Clew tell "no server here" from "wrong secret".
            json::Value v = json::Value::object();
            v.set("server", kAppName);
            v.set("api", kApiVersion);
            v.set("version", kVersion);
            v.set("convert", server.cache_ && server.cache_->enabled());
            json_reply(200, v);
        } else if (r.path == "/api/files") {
            files();
        } else if (r.path == "/api/stream") {
            stream();
        } else {
            refuse(404, "not found");
        }
    }

    void files() {
        if (!auth()) return;
        if (!cfg->serving()) {
            refuse(503, "folder");
            return;
        }
        bool truncated = false;
        auto found = list_served(cfg->folders, &truncated);
        json::Value list = json::Value::array();
        std::vector<std::filesystem::path> to_prepare;
        for (const auto& f : found) {
            json::Value item = json::Value::object();
            item.set("path", f.path);
            item.set("name", f.name);
            item.set("size", static_cast<unsigned long long>(f.size));
            item.set("modified", static_cast<long long>(f.modified_ms));
            item.set("mime", f.mime);
            list.push(item);
            if (unplayable(path_from_utf8(f.name)))
                if (auto full = resolve_served(cfg->folders, f.path)) to_prepare.push_back(*full);
        }
        // The folders' names, not their full paths: Clew has no use for the rest.
        json::Value names = json::Value::array();
        std::string joined;
        for (const auto& folder : cfg->folders) {
            if (!folder.enabled || folder.root.empty()) continue;
            names.push(folder.name);
            joined += (joined.empty() ? "" : ", ") + folder.name;
        }
        json::Value v = json::Value::object();
        v.set("folder", joined);
        v.set("folders", names);
        v.set("files", list);
        v.set("truncated", truncated);
        json_reply(200, v);
        if (server.cache_ && !to_prepare.empty()) server.cache_->prepare(to_prepare);
    }

    void url() {
        if (!auth()) return;
        auto body = json::parse(req->body.empty() ? "{}" : req->body);
        const json::Value* rel = body && body->is_object() && (*body)["path"].is_string() ? &(*body)["path"] : nullptr;
        std::optional<std::filesystem::path> path;
        if (rel) path = resolve_served(cfg->folders, rel->str());
        if (!path) {
            refuse(404, "no such file");
            return;
        }
        // A file that is converted is converted here, before the link, so that what the link plays
        // is settled and its size can be told: the player reads the waveform by ranges.
        std::filesystem::path sent = *path;
        if (server.cache_) {
            if (auto c = server.cache_->convert(*path)) sent = *c;
        }
        std::error_code ec;
        auto size = std::filesystem::file_size(sent, ec);
        if (ec) {
            refuse(404, "no such file");
            return;
        }
        long long exp = static_cast<long long>(std::time(nullptr)) + cfg->link_ttl_sec;
        json::Value v = json::Value::object();
        v.set("url", "/api/stream?p=" + percent_encode(rel->str()) + "&exp=" + std::to_string(exp) +
                         "&sig=" + server.sign(rel->str(), exp));
        v.set("expires", exp * 1000);
        v.set("size", static_cast<unsigned long long>(size));
        json_reply(200, v);
    }

    bool link_ok(const std::string& rel, const std::string& exp, const std::string& sig) {
        if (!all_digits(exp) || exp.size() > 12) return false;
        long long e = std::stoll(exp);
        if (e < static_cast<long long>(std::time(nullptr))) return false;
        return equal_constant_time(server.sign(rel, e), sig);
    }

    void stream() {
        auto q = parse_query(req->query);
        const std::string &rel = q["p"], &exp = q["exp"], &sig = q["sig"];
        if (!link_ok(rel, exp, sig)) {
            refuse(401, "link");
            return;
        }
        auto found = resolve_served(cfg->folders, rel);
        if (!found) {
            refuse(404, "no such file");
            return;
        }
        std::filesystem::path path = *found;
        std::string mime = mime_of(path) ? mime_of(path) : "application/octet-stream";
        std::uint64_t blank_first = 0, blank_end = 0;
        bool blank = false;
        if (server.cache_) {
            if (auto c = server.cache_->converted(path)) {
                path = *c;
                mime = "audio/mpeg";
            } else if (mime == "audio/mpeg") {
                blank = server.cache_->head_of(path).blank(&blank_first, &blank_end);
            }
        }
        std::ifstream f(path, std::ios::binary);
        std::error_code ec;
        std::uint64_t size = std::filesystem::file_size(path, ec);
        if (!f || ec) {
            refuse(404, "no such file");
            return;
        }
        ByteRange range = parse_range(req->header("range"), size);
        if (range.kind == ByteRange::Bad) {
            head(416, "Content-Range: bytes */" + std::to_string(size) + "\r\nContent-Length: 0\r\n");
            return;
        }
        std::uint64_t first = range.kind == ByteRange::Ok ? range.first : 0;
        std::uint64_t last = range.kind == ByteRange::Ok ? range.last : (size ? size - 1 : 0);
        std::uint64_t length = size ? last - first + 1 : 0;
        std::string extra = "Content-Type: " + mime + "\r\nAccept-Ranges: bytes\r\nContent-Length: " +
                            std::to_string(length) + "\r\n";
        if (range.kind == ByteRange::Ok)
            extra += "Content-Range: bytes " + std::to_string(first) + "-" + std::to_string(last) + "/" +
                     std::to_string(size) + "\r\n";
        extra += "Cache-Control: private, no-store\r\n";
        head(range.kind == ByteRange::Ok ? 206 : 200, extra);
        if (req->method == "HEAD" || length == 0 || broken) return;
        f.seekg(std::streamoff(first));
        std::vector<char> chunk(kChunk);
        std::uint64_t left = length, at = first;
        while (left > 0) {
            std::size_t want = std::size_t(left < kChunk ? left : kChunk);
            f.read(chunk.data(), std::streamsize(want));
            std::size_t got = std::size_t(f.gcount());
            if (got == 0) break;
            if (blank && at < blank_end && at + got > blank_first) {
                // A constant-bit-rate file's seek table, sent as zeros of the same length.
                std::uint64_t lo = (blank_first > at ? blank_first : at) - at;
                std::uint64_t hi = (blank_end < at + got ? blank_end : at + got) - at;
                std::memset(chunk.data() + lo, 0, std::size_t(hi - lo));
            }
            if (!send_all(chunk.data(), got)) {
                // The player moved on (a seek) and closed this request.
                broken = close = true;
                return;
            }
            at += got;
            left -= got;
        }
        if (left > 0) close = true;  // the file shrank while it was sent
    }

    void run() {
        while (!close) {
            Request r;
            req = &r;
            int refused = read_request(r);
            if (refused == -1) return;
            if (refused != 0) {
                close = true;
                if (r.method.empty()) r.method = "?";
                refuse(refused, "request");
                return;
            }
            cfg = server.config();
            ++server.requests_;
            if (!r.keep_alive) close = true;
            handle();
        }
    }
};

// ---------------------------------------------------------------------------------------------

Server::Server(ConvertCache* cache, LogFn log)
    : cache_(cache), log_(std::move(log)), link_key_(platform::random_bytes(32)),
      config_(std::make_shared<ServerConfig>()), listener_(std::uintptr_t(kBadSocket)) {}

Server::~Server() {
    stop();
    while (active_ > 0) std::this_thread::sleep_for(std::chrono::milliseconds(20));
}

void Server::configure(ServerConfig config) {
    std::lock_guard<std::mutex> g(lock_);
    config_ = std::make_shared<const ServerConfig>(std::move(config));
}

std::shared_ptr<const ServerConfig> Server::config() const {
    std::lock_guard<std::mutex> g(lock_);
    return config_;
}

std::string Server::sign(const std::string& rel, long long exp) const {
    auto mac = hmac_sha256(link_key_, rel + "\n" + std::to_string(exp));
    return base64url_encode(std::string(mac.begin(), mac.end()));
}

bool Server::start(int port, std::string* error) {
    if (running_) return true;
#ifdef _WIN32
    static bool wsa = [] {
        WSADATA d;
        return WSAStartup(MAKEWORD(2, 2), &d) == 0;
    }();
    if (!wsa) {
        if (error) *error = "network start-up failed";
        return false;
    }
#endif
    sock_t s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == kBadSocket) {
        if (error) *error = "no socket";
        return false;
    }
#ifdef _WIN32
    // Without this, another program could bind the same port and take some of the requests.
    BOOL one = TRUE;
    setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&one), sizeof one);
#else
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
#endif
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<unsigned short>(port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // this machine only
    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 || listen(s, 64) != 0) {
        close_sock(s);
        if (error) *error = "port " + std::to_string(port) + " is in use";
        return false;
    }
    socklen_t len = sizeof addr;
    getsockname(s, reinterpret_cast<sockaddr*>(&addr), &len);
    port_ = ntohs(addr.sin_port);
    listener_ = std::uintptr_t(s);
    running_ = true;
    accept_thread_ = std::thread([this] { accept_loop(); });
    return true;
}

void Server::stop() {
    if (!running_) return;
    running_ = false;
    close_sock(sock_t(listener_));
    if (accept_thread_.joinable()) accept_thread_.join();
    {
        std::lock_guard<std::mutex> g(lock_);
        for (auto s : open_) shutdown_sock(sock_t(s));
    }
    for (int i = 0; i < 150 && active_ > 0; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(20));
}

void Server::accept_loop() {
    while (running_) {
        sock_t c = accept(sock_t(listener_), nullptr, nullptr);
        if (c == kBadSocket) {
            if (!running_) return;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        if (active_ >= kMaxConnections) {
            static const char busy[] =
                "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            send(c, busy, sizeof busy - 1, kSendFlags);
            close_sock(c);
            continue;
        }
        set_timeouts(c);
        ++active_;
        {
            std::lock_guard<std::mutex> g(lock_);
            open_.insert(std::uintptr_t(c));
        }
        std::thread([this, c] { serve(std::uintptr_t(c)); }).detach();
    }
}

void Server::serve(std::uintptr_t sock) {
    {
        Connection conn{*this, sock_t(sock)};
        conn.run();
    }
    {
        std::lock_guard<std::mutex> g(lock_);
        open_.erase(sock);
    }
    // A graceful close: closing while the client still sends (a refused body) would reset the
    // connection, and the client would lose the answer. Stop sending, read what is left (briefly).
    shutdown(sock_t(sock), 1 /* SD_SEND / SHUT_WR */);
#ifdef _WIN32
    DWORD linger_ms = 1000;
    setsockopt(sock_t(sock), SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&linger_ms), sizeof linger_ms);
#else
    timeval linger_tv{1, 0};
    setsockopt(sock_t(sock), SOL_SOCKET, SO_RCVTIMEO, &linger_tv, sizeof linger_tv);
#endif
    char drain[4096];
    for (int total = 0; total < (1 << 20);) {
        int n = recv(sock_t(sock), drain, sizeof drain, 0);
        if (n <= 0) break;
        total += n;
    }
    close_sock(sock_t(sock));
    --active_;
}

}  // namespace clew
