#include "dictation.h"

#include <utility>

#include "files.h"
#include "text.h"

namespace clew {

namespace {

// What models.py requires before it calls a folder a model.
const char* const kModelFiles[] = {"config.json", "model.bin", "tokenizer.json"};
// And where it keeps the speaker model.
const char* const kSpeakerModel = "speakers/campplus.onnx";

bool ends_with(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string after(const std::string& line, const std::string& marker) {
    auto at = line.find(marker);
    return at == std::string::npos ? "" : trim(line.substr(at + marker.size()));
}

}  // namespace

bool Dictation::model_present(const std::filesystem::path& dir, const std::string& name) {
    if (name.empty() || name.find_first_of("/\\") != std::string::npos || name == "." || name == "..") return false;
    std::error_code ec;
    for (const char* f : kModelFiles)
        if (!std::filesystem::is_regular_file(dir / path_from_utf8(name) / f, ec)) return false;
    return true;
}

bool Dictation::speaker_model_present(const std::filesystem::path& dir) {
    std::error_code ec;
    return std::filesystem::is_regular_file(dir / kSpeakerModel, ec);
}

Dictation::LineMeaning Dictation::read_server_line(const std::string& line, int port) {
    LineMeaning m;
    if (line.find("Uvicorn running on") != std::string::npos) {
        m.ready = true;
        return m;
    }
    std::string lower = to_lower(line);
    if (lower.find("10048") != std::string::npos || lower.find("address already in use") != std::string::npos ||
        lower.find("error while attempting to bind") != std::string::npos) {
        m.problem = "port " + std::to_string(port) + " is in use";
        return m;
    }
    if (lower.find("memoryerror") != std::string::npos || lower.find("bad_alloc") != std::string::npos ||
        lower.find("out of memory") != std::string::npos) {
        m.problem = "not enough memory for this model";
        return m;
    }
    for (const char* marker : {"EngineError: ", "DownloadError: ", "--key-stdin: "}) {
        std::string rest = after(line, marker);
        if (!rest.empty()) {
            m.problem = rest;
            return m;
        }
    }
    // The last line of a Python traceback: "SomeError: what happened".
    auto colon = line.find(": ");
    if (colon != std::string::npos && colon > 0 && line.find(' ') > colon - 1) {
        std::string kind = line.substr(0, colon);
        if (ends_with(kind, "Error") || ends_with(kind, "Exception")) m.problem = trim(line.substr(colon + 2));
    }
    if (m.problem.empty() && starts_with(line, "ERROR:")) {
        m.problem = trim(line.substr(6));
        m.vague = true;
    }
    return m;
}

Dictation::Dictation(Notify notify, Log log) : notify_(std::move(notify)), log_(std::move(log)) {
    worker_ = std::thread([this] { run(); });
}

Dictation::~Dictation() {
    {
        std::lock_guard<std::mutex> g(m_);
        quit_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

void Dictation::configure(const DictationConfig& config) {
    {
        std::lock_guard<std::mutex> g(m_);
        if (config == config_) return;
        config_ = config;
        if (want_) restart_ = true;
        wake_ = true;
    }
    cv_.notify_all();
}

void Dictation::start() {
    {
        std::lock_guard<std::mutex> g(m_);
        want_ = true;
        restart_ = true;
        restarts_left_ = 1;
        wake_ = true;
    }
    cv_.notify_all();
}

void Dictation::stop() {
    {
        std::lock_guard<std::mutex> g(m_);
        want_ = false;
        restart_ = false;
        wake_ = true;
    }
    cv_.notify_all();
}

bool Dictation::wanted() const {
    std::lock_guard<std::mutex> g(m_);
    return want_;
}

void Dictation::download(const std::string& model) {
    {
        std::lock_guard<std::mutex> g(m_);
        if (status_.downloading == model) return;
        for (const auto& queued : download_wanted_)
            if (queued == model) return;
        download_wanted_.push_back(model);
        wake_ = true;
    }
    cv_.notify_all();
}

Dictation::Status Dictation::status() const {
    std::lock_guard<std::mutex> g(m_);
    return status_;
}

void Dictation::changed() {
    if (notify_) notify_();
}

// Ends a child with the lock released: its reader thread may be waiting for the lock to report a
// line, and the child's destructor waits for that thread.
void Dictation::kill(std::unique_ptr<platform::Child>& child, std::unique_lock<std::mutex>& lk) {
    std::unique_ptr<platform::Child> doomed = std::move(child);
    if (!doomed) return;
    lk.unlock();
    doomed.reset();
    lk.lock();
}

void Dictation::launch(std::unique_lock<std::mutex>& lk) {
    status_.model = config_.model;
    status_.problem.clear();
    std::error_code ec;
    if (config_.program.empty() || !std::filesystem::is_regular_file(config_.program, ec)) {
        status_.state = State::Missing;
        return;
    }
    if (!config_.fake_engine && !model_present(config_.models_dir, config_.model)) {
        status_.state = State::NoModel;
        return;
    }
    std::string origins;
    for (const auto& o : config_.origins) origins += (origins.empty() ? "" : " ") + o;
    // Deferred transcription reads recordings from here (docs/29); the server only reads.
    std::vector<std::string> args = {"serve",
                                     "--local",
                                     "--engine",
                                     config_.fake_engine ? "fake" : "whisper",
                                     "--model",
                                     config_.model,
                                     "--models-dir",
                                     path_to_utf8(config_.models_dir),
                                     "--port",
                                     std::to_string(config_.port),
                                     "--language",
                                     config_.language,
                                     "--origins",
                                     origins,
                                     "--key-stdin"};
    for (const auto& [name, root] : config_.audio_folders) {
        args.push_back("--audio-folder");
        args.push_back(name + "=" + path_to_utf8(root));
    }
    std::uint64_t gen = ++generation_;
    int port = config_.port;
    server_exited_ = false;
    status_.state = State::Loading;
    std::string why;
    // The callbacks wait for the lock; the start below holds it only briefly.
    server_ = platform::start_child(
        config_.program, args, config_.key + "\n", kSttMemoryLimit,
        [this, gen, port](const std::string& line) {
            auto meaning = read_server_line(line, port);
            if (!meaning.ready && meaning.problem.empty()) return;
            bool became_ready = false;
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != generation_) return;
                if (meaning.ready && status_.state == State::Loading) {
                    status_.state = State::Ready;
                    became_ready = true;
                }
                if (!meaning.problem.empty() && !(meaning.vague && !status_.problem.empty()))
                    status_.problem = meaning.problem;
            }
            if (became_ready && log_) log_("dictation ready on http://127.0.0.1:" + std::to_string(port));
            changed();
        },
        [this, gen](int code) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != generation_) return;
                server_exited_ = true;
                exit_code_ = code;
                wake_ = true;
            }
            cv_.notify_all();
        },
        &why);
    (void)lk;
    if (!server_) {
        status_.state = State::Failed;
        status_.problem = why;
    }
}

void Dictation::launch_download(std::unique_lock<std::mutex>& lk) {
    std::string model = download_wanted_.front();
    download_wanted_.pop_front();
    status_.download_problem.clear();
    std::error_code ec;
    if (config_.program.empty() || !std::filesystem::is_regular_file(config_.program, ec)) {
        status_.download_problem = "the dictation server is not installed";
        return;
    }
    std::uint64_t gen = ++fetch_generation_;
    fetcher_exited_ = false;
    fetch_result_.clear();
    status_.downloading = model;
    status_.percent = 0;
    std::string why;
    fetcher_ = platform::start_child(
        config_.program, {"download", model, "--models-dir", path_to_utf8(config_.models_dir)}, "", 0,
        [this, gen](const std::string& line) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != fetch_generation_) return;
                if (starts_with(line, "progress ")) {
                    std::string n = trim(line.substr(9));
                    if (!all_digits(n) || n.size() > 3) return;
                    status_.percent = std::stoi(n);
                } else if (starts_with(line, "done ")) {
                    fetch_result_ = "done";
                } else if (starts_with(line, "error ")) {
                    fetch_result_ = trim(line.substr(6));
                } else {
                    return;
                }
            }
            changed();
        },
        [this, gen](int code) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != fetch_generation_) return;
                fetcher_exited_ = true;
                if (fetch_result_.empty() && code != 0) fetch_result_ = "the download stopped (" + std::to_string(code) + ")";
                if (fetch_result_.empty()) fetch_result_ = "done";
                wake_ = true;
            }
            cv_.notify_all();
        },
        &why);
    (void)lk;
    if (!fetcher_) {
        status_.downloading.clear();
        status_.download_problem = why;
    }
}

// `clew-stt-server system` prints "cuda N". Asked once per program, in the background.
void Dictation::launch_probe(std::unique_lock<std::mutex>& lk) {
    probed_ = config_.program;
    std::uint64_t gen = ++probe_generation_;
    prober_exited_ = false;
    std::string why;
    prober_ = platform::start_child(
        config_.program, {"system"}, "", 0,
        [this, gen](const std::string& line) {
            if (!starts_with(line, "cuda ")) return;
            std::string n = trim(line.substr(5));
            if (!all_digits(n) || n.empty() || n.size() > 3) return;
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != probe_generation_) return;
                status_.gpus = std::stoi(n);
            }
            changed();
        },
        [this, gen](int) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != probe_generation_) return;
                prober_exited_ = true;
                wake_ = true;
            }
            cv_.notify_all();
        },
        &why);
    (void)lk;
}

void Dictation::run() {
    std::unique_lock<std::mutex> lk(m_);
    for (;;) {
        cv_.wait(lk, [&] { return quit_ || wake_; });
        if (quit_) break;
        wake_ = false;
        std::vector<std::string> lines;

        if (server_ && server_exited_) {
            bool was_ready = status_.state == State::Ready;
            std::string why = !status_.problem.empty() ? status_.problem
                                                       : "it stopped (" + std::to_string(exit_code_) + ")";
            kill(server_, lk);
            server_exited_ = false;
            if (!want_) {
                status_.state = State::Off;
            } else if (!restart_ && was_ready && restarts_left_ > 0) {
                --restarts_left_;
                lines.push_back("dictation stopped (" + why + "); starting it again");
                restart_ = true;
            } else if (!restart_) {
                status_.state = State::Failed;
                status_.problem = why;
                lines.push_back("dictation stopped: " + why);
            }
        }
        if (!want_ && server_) {
            kill(server_, lk);
            lines.push_back("dictation stopped");
        }
        if (!want_) {
            restart_ = false;
            if (status_.state != State::Off) {
                status_.state = State::Off;
                status_.problem.clear();
            }
        }
        if (want_ && restart_) {
            restart_ = false;
            kill(server_, lk);
            launch(lk);
            if (status_.state == State::Loading) lines.push_back("dictation starting: " + config_.model);
        }

        std::error_code ec;
        if (!prober_ && !config_.program.empty() && config_.program != probed_ &&
            std::filesystem::is_regular_file(config_.program, ec))
            launch_probe(lk);
        if (prober_ && prober_exited_) {
            kill(prober_, lk);
            prober_exited_ = false;
        }

        if (!download_wanted_.empty() && !fetcher_) launch_download(lk);
        if (fetcher_ && fetcher_exited_) {
            kill(fetcher_, lk);
            fetcher_exited_ = false;
            std::string model = std::exchange(status_.downloading, std::string());
            if (fetch_result_ == "done") {
                lines.push_back("model downloaded: " + model);
                if (want_ && model == config_.model &&
                    (status_.state == State::NoModel || status_.state == State::Failed))
                    restart_ = wake_ = true;
            } else {
                status_.download_problem = fetch_result_;
                lines.push_back("download of " + model + " failed: " + fetch_result_);
                download_wanted_.clear();
            }
            if (!download_wanted_.empty()) wake_ = true;
        }

        lk.unlock();
        if (log_)
            for (const auto& l : lines) log_(l);
        changed();
        lk.lock();
    }
    kill(server_, lk);
    kill(fetcher_, lk);
    kill(prober_, lk);
}

}  // namespace clew
