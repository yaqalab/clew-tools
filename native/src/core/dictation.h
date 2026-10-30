// Dictation on this computer (docs/28): the speech server tools/stt-server builds, run as a child
// process. It is the stt.alefa.net server with faster-whisper; Clew's Dictation settings talk to it
// directly at http://127.0.0.1:<port>. This side only starts it, stops it, restarts it once if it
// dies, fetches models, and says what state it is in.
//
// One thread of its own does every start and stop, so a child's death (reported from the child's
// thread) never destroys the child from inside itself.
#pragma once

#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "platform.h"

namespace clew {

// A cap on the server's committed memory (what Windows promises it, more than it touches).
// Measured 2026-10-16 (docs/28 §7): small commits 2.9 GB, large-v3-turbo 4.3 GB at its peak while
// loading, with 1.9 GB in RAM. 4 GB stopped turbo at its start.
constexpr std::uint64_t kSttMemoryLimit = std::uint64_t(6) * 1024 * 1024 * 1024;

struct DictationConfig {
    std::filesystem::path program;  // clew-stt-server(.exe); empty: none installed
    std::string model;
    std::filesystem::path models_dir;
    std::string language;  // "" or one code
    int port = 0;
    std::vector<std::string> origins;  // pages besides this computer's that may call it
    std::string key;                   // handed over on stdin
    // The audio server's folders by name (folders.h), for deferred transcription (docs/29); a folder
    // that is off has an empty path.
    std::vector<std::pair<std::string, std::filesystem::path>> audio_folders;
    bool fake_engine = false;  // the tests: no model needed
    bool operator==(const DictationConfig& o) const {
        return program == o.program && model == o.model && models_dir == o.models_dir && language == o.language &&
               port == o.port && origins == o.origins && key == o.key && audio_folders == o.audio_folders &&
               fake_engine == o.fake_engine;
    }
    bool operator!=(const DictationConfig& o) const { return !(*this == o); }
};

class Dictation {
public:
    enum class State {
        Off,       // not wanted
        Missing,   // no server program installed
        NoModel,   // the chosen model is not downloaded
        Loading,   // started; loading the model
        Ready,     // listening
        Failed,    // stopped by itself; `problem` says why
    };
    struct Status {
        State state = State::Off;
        std::string model;
        std::string problem;
        std::string downloading;  // the model being fetched, or ""
        int percent = 0;          // of that download
        std::string download_problem;
        int gpus = -1;  // NVIDIA GPUs CUDA sees; -1 until the server program has said
    };

    using Notify = std::function<void()>;
    using Log = std::function<void(const std::string&)>;

    // `notify` is called from any thread when the status changed; `log` gets one line per event
    // (never the key, never what was said).
    Dictation(Notify notify, Log log);
    ~Dictation();
    Dictation(const Dictation&) = delete;
    Dictation& operator=(const Dictation&) = delete;

    // The settings and key it runs with. A running server restarts when they changed.
    void configure(const DictationConfig& config);
    void start();
    void stop();
    bool wanted() const;
    // Fetches a model into the models folder: queued, one at a time. Done: a waiting server
    // starts. "speakers" is live transcription's speaker model (docs/29).
    void download(const std::string& model);

    Status status() const;

    // Whether `dir/name` holds a whole model (the files tools/stt-server's models.py requires).
    static bool model_present(const std::filesystem::path& dir, const std::string& name);
    static bool speaker_model_present(const std::filesystem::path& dir);
    // A line the server printed, read for its state: "ready", a problem, or nothing.
    struct LineMeaning {
        bool ready = false;
        std::string problem;
        bool vague = false;  // "Application startup failed": keep the line that said why
    };
    static LineMeaning read_server_line(const std::string& line, int port);

private:
    void run();
    void launch(std::unique_lock<std::mutex>& lk);
    void launch_download(std::unique_lock<std::mutex>& lk);
    void launch_probe(std::unique_lock<std::mutex>& lk);
    void kill(std::unique_ptr<platform::Child>& child, std::unique_lock<std::mutex>& lk);
    void changed();

    Notify notify_;
    Log log_;
    mutable std::mutex m_;
    std::condition_variable cv_;
    bool quit_ = false;
    bool wake_ = false;

    DictationConfig config_;
    bool want_ = false;
    bool restart_ = false;
    int restarts_left_ = 1;
    std::uint64_t generation_ = 0;  // which child a callback is about
    std::unique_ptr<platform::Child> server_;
    bool server_exited_ = false;
    int exit_code_ = 0;
    Status status_;

    std::deque<std::string> download_wanted_;
    std::unique_ptr<platform::Child> fetcher_;
    std::uint64_t fetch_generation_ = 0;
    bool fetcher_exited_ = false;
    std::string fetch_result_;  // "done" or the error

    std::filesystem::path probed_;  // the program `gpus` was read from
    std::unique_ptr<platform::Child> prober_;
    std::uint64_t probe_generation_ = 0;
    bool prober_exited_ = false;

    std::thread worker_;
};

}  // namespace clew
