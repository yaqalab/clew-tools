// The HTTP server: API version 1, the same as the Python server (docs/22 section 2), so Clew does not
// know which one it talks to.
//
//   GET  /api/ping                 nothing needed       {server, api, version, convert}
//   GET  /api/files                Bearer <secret>      {folder, folders, files, truncated}
//   POST /api/url {path}           Bearer <secret>      {url, expires, size}
//   GET  /api/stream?p&exp&sig     a signed link        the file, with Range and HEAD
//
// What it promises (docs/22 section 3): 127.0.0.1 only, the port held exclusively; a Host that is
// not loopback is refused (DNS rebinding); CORS only for loopback origins and the configured ones;
// links signed for one file and one expiry with a key that lives in memory only; only files of
// the folders that are on (folders.h); nothing logged beyond method, path and status. Added here: limits on the request
// head (16 KB), the body (8 KB) and the connections (64), idle timeouts, and no chunked bodies.
#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>

#include "convert_cache.h"
#include "folders.h"

namespace clew {

constexpr const char* kAppName = "clew-audio-server";
constexpr int kApiVersion = 1;
extern const char* const kVersion;

struct ServerConfig {
    std::vector<ServedFolder> folders;  // every folder, on or off (folders.h); none until one is chosen
    // Whether any folder is on and can be opened: without one there is nothing to serve.
    bool serving() const {
        for (const auto& f : folders)
            if (f.enabled && !f.root.empty()) return true;
        return false;
    }
    std::set<std::string> origins;
    int link_ttl_sec = 4 * 3600;
    std::string secret;
};

class Server {
public:
    using LogFn = std::function<void(const std::string& line)>;

    Server(ConvertCache* cache, LogFn log);
    ~Server();
    Server(const Server&) = delete;
    Server& operator=(const Server&) = delete;

    // Binds 127.0.0.1:port (0 picks a free one). False with a reason when the port is taken.
    bool start(int port, std::string* error);
    void stop();
    bool running() const { return running_; }
    int port() const { return port_; }

    // Takes effect for the next request; links already given keep working.
    void configure(ServerConfig config);
    std::shared_ptr<const ServerConfig> config() const;

    std::string sign(const std::string& rel, long long exp) const;
    std::uint64_t requests() const { return requests_; }

    struct Connection;

private:
    void accept_loop();
    void serve(std::uintptr_t sock);

    ConvertCache* cache_;
    LogFn log_;
    std::string link_key_;  // in memory only: a restart revokes every link

    mutable std::mutex lock_;
    std::shared_ptr<const ServerConfig> config_;
    std::set<std::uintptr_t> open_;
    int failures_ = 0;

    std::uintptr_t listener_;
    std::atomic<bool> running_{false};
    std::atomic<int> active_{0};
    std::atomic<std::uint64_t> requests_{0};
    int port_ = 0;
    std::thread accept_thread_;

    friend struct Connection;
};

// Whether an Origin header names a loopback page (always allowed).
bool loopback_origin(const std::string& origin);
// Whether a Host header names this machine's loopback interface.
bool loopback_host(const std::string& host);

}  // namespace clew
