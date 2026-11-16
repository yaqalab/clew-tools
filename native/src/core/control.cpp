#include "control.h"

#include <chrono>

#include "files.h"
#include "platform.h"
#include "server.h"
#include "service.h"
#include "settings.h"

namespace clew {

namespace {

// The settings keys that never come in through settings.apply: each has its own command.
const char* const kSealedKeys[] = {"secret", "sttSecret", "analysisSecret", "v", "folder"};

const char* dictation_state(Dictation::State s) {
    switch (s) {
        case Dictation::State::Off: return "off";
        case Dictation::State::Missing: return "missing";
        case Dictation::State::NoModel: return "no-model";
        case Dictation::State::Loading: return "loading";
        case Dictation::State::Ready: return "ready";
        case Dictation::State::Failed: return "failed";
    }
    return "off";
}

const char* analysis_state(Analysis::State s) {
    switch (s) {
        case Analysis::State::Off: return "off";
        case Analysis::State::Missing: return "missing";
        case Analysis::State::Starting: return "starting";
        case Analysis::State::Ready: return "ready";
        case Analysis::State::Failed: return "failed";
    }
    return "off";
}

const char* look_name(Analysis::Look l) {
    switch (l) {
        case Analysis::Look::Unknown: return "unknown";
        case Analysis::Look::Looking: return "looking";
        case Analysis::Look::Found: return "found";
        case Analysis::Look::NotFound: return "not-found";
    }
    return "unknown";
}

const char* model_use(SttModel::Use u) {
    switch (u) {
        case SttModel::Live: return "live";
        case SttModel::Files: return "files";
        case SttModel::Gpu: return "gpu";
    }
    return "live";
}

json::Value strings(const std::vector<std::string>& list) {
    json::Value a = json::Value::array();
    for (const auto& s : list) a.push(s);
    return a;
}

struct Failure {
    std::string why;
};

const std::string& text_arg(const json::Value& req, const char* name) {
    if (!req[name].is_string() || req[name].str().empty()) throw Failure{std::string("missing \"") + name + "\""};
    return req[name].str();
}

}  // namespace

Control::Control(Service& svc, Write write) : svc_(svc), write_(std::move(write)) {
    pump_ = std::thread([this] { pump(); });
}

Control::~Control() {
    {
        std::lock_guard<std::mutex> g(m_);
        quit_ = true;
    }
    cv_.notify_all();
    if (pump_.joinable()) pump_.join();
}

void Control::send(const json::Value& v) {
    std::lock_guard<std::mutex> g(write_lock_);
    write_(v.dump());
}

void Control::event(const std::string& name, json::Value fields) {
    json::Value e = json::Value::object();
    e.set("event", name);
    if (fields.is_object())
        for (const auto& [k, v] : fields.members()) e.set(k, v);
    send(e);
}

void Control::log(const std::string& line) {
    json::Value f = json::Value::object();
    f.set("line", line);
    event("log", f);
}

void Control::changed() {
    {
        std::lock_guard<std::mutex> g(m_);
        dirty_ = true;
    }
    cv_.notify_all();
}

// Many requests make many changes: one status event per short while is enough.
void Control::pump() {
    std::unique_lock<std::mutex> lk(m_);
    for (;;) {
        cv_.wait(lk, [this] { return dirty_ || quit_; });
        if (quit_) return;
        dirty_ = false;
        lk.unlock();
        json::Value f = json::Value::object();
        f.set("status", status());
        event("status", f);
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        lk.lock();
    }
}

json::Value Control::status() const {
    json::Value out = json::Value::object();

    auto st = svc_.status();
    json::Value server = json::Value::object();
    server.set("running", st.running);
    server.set("port", st.port);
    server.set("problem", st.problem);
    server.set("converting", st.converting);
    server.set("requests", static_cast<unsigned long long>(st.requests));
    server.set("folders", strings(st.folders));
    out.set("server", server);

    auto d = svc_.dictation().status();
    json::Value dict = json::Value::object();
    dict.set("state", dictation_state(d.state));
    dict.set("wanted", svc_.dictation().wanted());
    dict.set("model", d.model);
    dict.set("problem", d.problem);
    dict.set("downloading", d.downloading);
    dict.set("percent", d.percent);
    dict.set("downloadProblem", d.download_problem);
    dict.set("gpus", d.gpus);
    out.set("dictation", dict);

    auto a = svc_.analysis().status();
    json::Value ana = json::Value::object();
    ana.set("state", analysis_state(a.state));
    ana.set("wanted", svc_.analysis().wanted());
    ana.set("problem", a.problem);
    json::Value programs = json::Value::object();
    for (const auto& [backend, p] : a.programs) {
        json::Value pv = json::Value::object();
        pv.set("look", look_name(p.look));
        pv.set("version", p.version);
        pv.set("path", p.path);
        pv.set("why", p.why);
        programs.set(backend, pv);
    }
    ana.set("programs", programs);
    out.set("analysis", ana);
    return out;
}

// Each command, as the tray program's window or menu does it.
json::Value Control::run(const std::string& cmd, const json::Value& req) {
    json::Value r = json::Value::object();
    if (cmd == "ping") return r;
    if (cmd == "status") return status();
    if (cmd == "recent") return strings(svc_.recent());

    if (cmd == "settings.get") {
        r = svc_.settings().to_json(false);
        r.set("sttModelsDirDefault", path_to_utf8(platform::default_stt_models_dir()));
        r.set("settingsFile", path_to_utf8(svc_.settings_file()));
        return r;
    }
    if (cmd == "settings.apply") {
        // Only what is given changes; everything is checked as the settings file is.
        const json::Value& patch = req["settings"];
        if (!patch.is_object()) throw Failure{"missing \"settings\""};
        json::Value merged = svc_.settings().to_json(false);
        for (const auto& [k, v] : patch.members()) {
            bool sealed = false;
            for (const char* s : kSealedKeys) sealed = sealed || k == s;
            if (!sealed) merged.set(k, v);
        }
        std::string problem = svc_.apply(Settings::from_json(merged));
        r.set("problem", problem);
        r.set("settings", svc_.settings().to_json(false));
        return r;
    }

    if (cmd == "server.start") {
        // The window's Start: the saved settings, applied again.
        r.set("problem", svc_.apply(svc_.settings(), false));
        return r;
    }
    if (cmd == "server.stop") {
        svc_.stop();
        return r;
    }

    if (cmd == "secret.get" || cmd == "secret.reset") {
        std::string s = cmd == "secret.reset" ? svc_.reset_secret() : svc_.secret();
        if (s.empty()) throw Failure{"this system could not keep a secret safely"};
        r.set("secret", s);
        return r;
    }
    if (cmd == "sttKey.get" || cmd == "sttKey.reset") {
        std::string k = cmd == "sttKey.reset" ? svc_.reset_stt_key() : svc_.stt_key();
        if (k.empty()) throw Failure{"this system could not keep a key safely"};
        r.set("key", k);
        return r;
    }
    if (cmd == "analysisKey.get" || cmd == "analysisKey.reset") {
        std::string k = cmd == "analysisKey.reset" ? svc_.reset_analysis_key() : svc_.analysis_key();
        if (k.empty()) throw Failure{"this system could not keep a key safely"};
        r.set("key", k);
        return r;
    }

    if (cmd == "dictation.start") {
        svc_.dictation().start();
        return r;
    }
    if (cmd == "dictation.stop") {
        svc_.dictation().stop();
        return r;
    }
    if (cmd == "dictation.download") {
        const std::string& model = text_arg(req, "model");
        if (!stt_model_known(model) && model != "speakers") throw Failure{"unknown model: " + model};
        svc_.dictation().download(model);
        return r;
    }
    if (cmd == "dictation.models") {
        auto dir = svc_.stt_models_dir();
        int gpus = svc_.dictation().status().gpus;
        std::uint64_t memory = platform::total_memory();
        json::Value list = json::Value::array();
        for (const auto& m : kSttModels) {
            json::Value mv = json::Value::object();
            mv.set("name", m.name);
            mv.set("bytes", static_cast<unsigned long long>(m.bytes));
            mv.set("use", model_use(m.use));
            mv.set("memoryGb", int(m.memory_gb));
            mv.set("englishOnly", m.english_only);
            mv.set("present", Dictation::model_present(dir, m.name));
            mv.set("blocker", stt_model_blocker(m, memory, gpus));
            list.push(mv);
        }
        r.set("models", list);
        r.set("speakersPresent", Dictation::speaker_model_present(dir));
        r.set("speakersBytes", static_cast<unsigned long long>(kSpeakerModelBytes));
        r.set("modelsDir", path_to_utf8(dir));
        r.set("memoryBytes", static_cast<unsigned long long>(memory));
        return r;
    }
    if (cmd == "dictation.setProgram") {
        // The speech server installed after this program started (Clew Desktop fetches it on demand).
        svc_.set_stt_program(path_from_utf8(text_arg(req, "path")));
        return r;
    }

    if (cmd == "analysis.start") {
        svc_.analysis().start();
        return r;
    }
    if (cmd == "analysis.stop") {
        svc_.analysis().stop();
        return r;
    }
    if (cmd == "analysis.lookAgain") {
        svc_.analysis().look_again();
        return r;
    }
    if (cmd == "analysis.setProgram") {
        svc_.set_analysis_program(path_from_utf8(text_arg(req, "path")));
        return r;
    }

    throw Failure{"unknown command: " + cmd};
}

bool Control::handle(const std::string& line) {
    if (line.find_first_not_of(" \t\r") == std::string::npos) return true;
    json::Value reply = json::Value::object();
    auto req = json::parse(line);
    if (!req || !req->is_object() || !(*req)["cmd"].is_string()) {
        reply.set("id", req && req->is_object() ? (*req)["id"] : json::Value());
        reply.set("ok", false);
        reply.set("error", "not a request: one JSON object with \"cmd\" per line");
        send(reply);
        return true;
    }
    const std::string cmd = (*req)["cmd"].str();
    reply.set("id", (*req)["id"]);
    if (cmd == "quit") {
        reply.set("ok", true);
        reply.set("result", json::Value::object());
        send(reply);
        return false;
    }
    try {
        json::Value result = run(cmd, *req);
        reply.set("ok", true);
        reply.set("result", result);
    } catch (const Failure& f) {
        reply.set("ok", false);
        reply.set("error", f.why);
    } catch (const std::exception& e) {
        reply.set("ok", false);
        reply.set("error", e.what());
    }
    send(reply);
    return true;
}

int run_control(const std::vector<std::string>& args, const Console& console) {
    std::string config, stt_program, analysis_program;
    for (std::size_t i = 0; i < args.size(); ++i) {
        const std::string& a = args[i];
        if (a == "--headless" || a == "--control") continue;
        std::string* into = a == "--config"             ? &config
                            : a == "--stt-program"      ? &stt_program
                            : a == "--analysis-program" ? &analysis_program
                                                        : nullptr;
        if (!into || i + 1 >= args.size()) {
            console.out(R"({"event":"bye","error":"unknown or incomplete option: )" + a + R"("})");
            return 2;
        }
        *into = args[++i];
    }
    if (!console.read_line) return 2;

    // The service logs before Control exists; those lines wait for it.
    std::mutex early_lock;
    Control* control = nullptr;
    auto log = [&](const std::string& line) {
        std::lock_guard<std::mutex> g(early_lock);
        if (control) control->log(line);
        else console.err(line);
    };
    Service svc(config.empty() ? default_settings_file() : path_from_utf8(config), {}, log);
    if (!stt_program.empty()) svc.set_stt_program(std::filesystem::absolute(path_from_utf8(stt_program)));
    if (!analysis_program.empty())
        svc.set_analysis_program(std::filesystem::absolute(path_from_utf8(analysis_program)));

    Control ctl(svc, console.out);
    {
        std::lock_guard<std::mutex> g(early_lock);
        control = &ctl;
    }
    svc.on_change([&ctl] { ctl.changed(); });

    json::Value hello = json::Value::object();
    hello.set("version", kVersion);
    hello.set("settingsFile", path_to_utf8(svc.settings_file()));
#ifdef _WIN32
    hello.set("platform", "windows");
#elif defined(__APPLE__)
    hello.set("platform", "macos");
#else
    hello.set("platform", "linux");
#endif
    ctl.event("hello", hello);

    // As the tray program starts.
    svc.secret();
    svc.apply(svc.settings(), false);
    svc.start_dictation_if_enabled();
    svc.start_analysis_if_enabled();
    ctl.changed();

    std::string line;
    while (console.read_line(&line))
        if (!ctl.handle(line)) break;

    svc.on_change(nullptr);
    svc.analysis().stop();
    svc.dictation().stop();
    svc.stop();
    {
        std::lock_guard<std::mutex> g(early_lock);
        control = nullptr;
    }
    ctl.event("bye", json::Value::object());
    return 0;
}

}  // namespace clew
