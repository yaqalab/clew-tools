#include "analysis.h"

#include <utility>

#include "files.h"
#include "text.h"

namespace clew {

namespace {

bool ends_with(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

}  // namespace

Analysis::LineMeaning Analysis::read_server_line(const std::string& line, int port) {
    LineMeaning m;
    // "Clew analysis server 0.1.0 on http://127.0.0.1:47823 (backend: r)" (clew_analysis/__main__.py).
    if (starts_with(line, "Clew analysis server ") && line.find("http://127.0.0.1:") != std::string::npos) {
        m.ready = true;
        return m;
    }
    std::string lower = to_lower(line);
    if ((starts_with(lower, "port ") && lower.find("is not available") != std::string::npos) ||
        lower.find("10048") != std::string::npos || lower.find("address already in use") != std::string::npos) {
        m.problem = "port " + std::to_string(port) + " is in use";
        return m;
    }
    if (starts_with(line, "The key on standard input")) {
        m.problem = "the key was not accepted";
        return m;
    }
    // The last line of a Python traceback: "SomeError: what happened".
    auto colon = line.find(": ");
    if (colon != std::string::npos && colon > 0 && line.find(' ') > colon - 1) {
        std::string kind = line.substr(0, colon);
        if (ends_with(kind, "Error") || ends_with(kind, "Exception")) m.problem = trim(line.substr(colon + 2));
    }
    return m;
}

const std::vector<std::pair<std::string, std::string>>& Analysis::backends() {
    static const std::vector<std::pair<std::string, std::string>> list = {
        {"r", "R"}, {"python", "Python"}, {"pspp", "PSPP"}, {"spss", "SPSS"}};
    return list;
}

bool Analysis::read_check_line(const std::string& line, Status* into) {
    for (const auto& [id, name] : backends()) {
        if (!starts_with(line, name + ": ")) continue;
        std::string rest = trim(line.substr(name.size() + 2));
        Program p;
        if (starts_with(rest, "not ready")) {
            p.look = Look::NotFound;
            auto open = rest.find('(');
            auto close = rest.rfind(')');
            if (open != std::string::npos && close != std::string::npos && close > open)
                p.why = rest.substr(open + 1, close - open - 1);
        } else {
            // "4.3.3 at <path>"; SPSS may say "found" where its folder carries no number.
            auto at = rest.find(" at ");
            if (at == std::string::npos || at == 0) return false;
            p.look = Look::Found;
            p.version = rest.substr(0, at);
            p.path = trim(rest.substr(at + 4));
        }
        into->programs[id] = p;
        return true;
    }
    return false;
}

namespace {

// The engine's options for the programs chosen in the tab.
void program_args(const AnalysisConfig& c, std::vector<std::string>* args) {
    static const std::map<std::string, std::string> option = {
        {"r", "--rscript"}, {"python", "--python"}, {"pspp", "--pspp"}, {"spss", "--spss"}};
    for (const auto& [id, path] : c.programs) {
        auto o = option.find(id);
        if (o == option.end() || path.empty()) continue;
        args->push_back(o->second);
        args->push_back(path_to_utf8(path));
    }
}

}  // namespace

Analysis::Analysis(Notify notify, Log log) : notify_(std::move(notify)), log_(std::move(log)) {
    worker_ = std::thread([this] { run(); });
}

Analysis::~Analysis() {
    {
        std::lock_guard<std::mutex> g(m_);
        quit_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

void Analysis::configure(const AnalysisConfig& config) {
    {
        std::lock_guard<std::mutex> g(m_);
        if (config == config_) return;
        bool programs_changed = config.program != config_.program || config.programs != config_.programs;
        config_ = config;
        if (want_) restart_ = true;
        if (programs_changed) check_wanted_ = true;
        wake_ = true;
    }
    cv_.notify_all();
}

void Analysis::start() {
    {
        std::lock_guard<std::mutex> g(m_);
        want_ = true;
        restart_ = true;
        restarts_left_ = 1;
        wake_ = true;
    }
    cv_.notify_all();
}

void Analysis::stop() {
    {
        std::lock_guard<std::mutex> g(m_);
        want_ = false;
        restart_ = false;
        wake_ = true;
    }
    cv_.notify_all();
}

bool Analysis::wanted() const {
    std::lock_guard<std::mutex> g(m_);
    return want_;
}

void Analysis::look_again() {
    {
        std::lock_guard<std::mutex> g(m_);
        check_wanted_ = true;
        wake_ = true;
    }
    cv_.notify_all();
}

Analysis::Status Analysis::status() const {
    std::lock_guard<std::mutex> g(m_);
    return status_;
}

void Analysis::changed() {
    if (notify_) notify_();
}

// Ends a child with the lock released: its reader thread may be waiting for the lock to report a
// line, and the child's destructor waits for that thread.
void Analysis::kill(std::unique_ptr<platform::Child>& child, std::unique_lock<std::mutex>& lk) {
    std::unique_ptr<platform::Child> doomed = std::move(child);
    if (!doomed) return;
    lk.unlock();
    doomed.reset();
    lk.lock();
}

void Analysis::launch(std::unique_lock<std::mutex>& lk) {
    status_.problem.clear();
    std::error_code ec;
    if (config_.program.empty() || !std::filesystem::is_regular_file(config_.program, ec)) {
        status_.state = State::Missing;
        return;
    }
    std::vector<std::string> args = {"--port",    std::to_string(config_.port),
                                     "--backend", config_.backend,
                                     "--timeout", std::to_string(config_.timeout),
                                     "--key-stdin"};
    program_args(config_, &args);
    if (!config_.scripts.empty()) {
        args.push_back("--scripts");
        args.push_back(path_to_utf8(config_.scripts));
    }
    for (const auto& o : config_.origins) {
        args.push_back("--allow-origin");
        args.push_back(o);
    }
    std::uint64_t gen = ++generation_;
    int port = config_.port;
    server_exited_ = false;
    status_.state = State::Starting;
    std::string why;
    server_ = platform::start_child(
        config_.program, args, config_.key + "\n", kAnalysisMemoryLimit,
        [this, gen, port](const std::string& line) {
            auto meaning = read_server_line(line, port);
            if (!meaning.ready && meaning.problem.empty()) return;
            bool became_ready = false;
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != generation_) return;
                if (meaning.ready && status_.state == State::Starting) {
                    status_.state = State::Ready;
                    became_ready = true;
                }
                if (!meaning.problem.empty()) status_.problem = meaning.problem;
            }
            if (became_ready && log_) log_("analysis engine ready on http://127.0.0.1:" + std::to_string(port));
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

// `clew-analysis-server --check` prints one line per program: "R: 4.3.3 at <path>", "PSPP: not
// ready (why)"...
void Analysis::launch_check(std::unique_lock<std::mutex>& lk) {
    check_wanted_ = false;
    std::error_code ec;
    if (config_.program.empty() || !std::filesystem::is_regular_file(config_.program, ec)) {
        status_.programs.clear();
        return;
    }
    std::vector<std::string> args = {"--check"};
    program_args(config_, &args);
    std::uint64_t gen = ++check_generation_;
    checker_exited_ = false;
    for (const auto& b : backends()) status_.programs[b.first] = Program{Look::Looking, "", "", ""};
    std::string why;
    checker_ = platform::start_child(
        config_.program, args, "", 0,
        [this, gen](const std::string& line) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != check_generation_) return;
                if (!read_check_line(line, &status_)) return;
            }
            changed();
        },
        [this, gen](int) {
            {
                std::lock_guard<std::mutex> g(m_);
                if (gen != check_generation_) return;
                checker_exited_ = true;
                wake_ = true;
            }
            cv_.notify_all();
        },
        &why);
    (void)lk;
    if (!checker_) {
        for (auto& [id, p] : status_.programs) p = Program{Look::NotFound, "", "", why};
    }
}

void Analysis::run() {
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
                lines.push_back("analysis engine stopped (" + why + "); starting it again");
                restart_ = true;
            } else if (!restart_) {
                status_.state = State::Failed;
                status_.problem = why;
                lines.push_back("analysis engine stopped: " + why);
            }
        }
        if (!want_ && server_) {
            kill(server_, lk);
            lines.push_back("analysis engine stopped");
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
            if (status_.state == State::Starting) lines.push_back("analysis engine starting");
        }

        if (check_wanted_ && !checker_) launch_check(lk);
        if (checker_ && checker_exited_) {
            kill(checker_, lk);
            checker_exited_ = false;
            for (auto& [id, p] : status_.programs)
                if (p.look == Look::Looking) p = Program{Look::NotFound, "", "", "the engine could not look for it"};
            if (check_wanted_) wake_ = true;
        }

        lk.unlock();
        if (log_)
            for (const auto& l : lines) log_(l);
        changed();
        lk.lock();
    }
    kill(server_, lk);
    kill(checker_, lk);
}

}  // namespace clew
