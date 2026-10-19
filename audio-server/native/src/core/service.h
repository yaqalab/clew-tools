// The running service: settings, secret, conversion cache and HTTP server together, and the
// dictation server beside them (dictation.h). The tray app and the headless mode both drive one of
// these; neither talks to the server directly.
#pragma once

#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "convert_cache.h"
#include "dictation.h"
#include "server.h"
#include "settings.h"

namespace clew {

class Service {
public:
    using LogFn = std::function<void(const std::string& line)>;

    // `cache_dir` empty: converted copies go to kCacheFolder inside the served folder that holds the
    // recording (the normal case); otherwise there (the tests).
    Service(std::filesystem::path settings_file, std::filesystem::path cache_dir, LogFn log);
    ~Service();

    const std::filesystem::path& settings_file() const { return settings_file_; }
    Settings settings() const;

    // Opens the kept secret, or makes, seals and saves a new one. `made` tells which.
    // Empty when the system cannot seal one (then the server refuses every listing).
    std::string secret(bool* made = nullptr);
    std::string reset_secret();

    // Takes these settings: saves them (unless `persist` is false: headless overrides), updates the
    // server, and restarts it when the port changed. Returns a problem to show, or "".
    std::string apply(Settings s, bool persist = true);
    std::string start();
    void stop();

    struct Status {
        bool running = false;
        int port = 0;
        std::vector<std::string> folders;  // full paths of the folders that are on, for this machine's own window only
        std::string problem;     // why it is not running, or ""
        std::string converting;  // the file being converted, or ""
        std::uint64_t requests = 0;
    };
    Status status() const;

    // The last lines of activity (method, path, status; never a query), newest last.
    std::vector<std::string> recent() const;

    ConvertCache& cache() { return *cache_; }
    Server& server() { return *server_; }

    // Dictation. Its key is kept like the secret: sealed in the settings, made on first use.
    Dictation& dictation() { return *dictation_; }
    std::string stt_key(bool* made = nullptr);
    std::string reset_stt_key();  // the old key stops working at once (a running server restarts)
    std::filesystem::path stt_models_dir() const;
    // Starts dictation when the settings say it starts with the server.
    void start_dictation_if_enabled();
    // Headless and the tests: another server program, or the fake engine (no model needed).
    void set_stt_program(std::filesystem::path program);
    void set_stt_fake_engine(bool fake);

    // Called from any thread when the status changed; keep it short (post a message).
    void on_change(std::function<void()> fn);

private:
    void notify();
    void record(const std::string& line);
    bool convert(const std::filesystem::path& in, const std::filesystem::path& out,
                 const platform::ConvertControl& control, std::string* why);
    ServerConfig server_config(const Settings& s, std::string* problem);
    DictationConfig dictation_config(const Settings& s);

    std::filesystem::path settings_file_;
    LogFn log_;
    mutable std::mutex lock_;
    Settings settings_;
    std::string secret_;
    std::string problem_;
    std::string converting_;
    std::deque<std::string> recent_;
    std::function<void()> on_change_;
    std::unique_ptr<ConvertCache> cache_;
    std::unique_ptr<Server> server_;
    std::string stt_key_;
    std::filesystem::path stt_program_;
    bool stt_fake_engine_ = false;
    std::unique_ptr<Dictation> dictation_;
};

}  // namespace clew
