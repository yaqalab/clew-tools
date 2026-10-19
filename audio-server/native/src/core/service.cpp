#include "service.h"

#include <ctime>

#include "files.h"
#include "folders.h"
#include "platform.h"

namespace clew {

namespace {

std::string clock_now() {
    std::time_t t = std::time(nullptr);
    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char buf[16];
    std::strftime(buf, sizeof buf, "%H:%M:%S", &tm);
    return buf;
}

// Every folder of the settings with its name (folders.h); a folder that cannot be opened has no root.
std::vector<ServedFolder> served_folders(const Settings& s) {
    std::vector<std::string> paths;
    for (const auto& f : s.folders) paths.push_back(f.path);
    auto names = folder_names(paths);
    std::vector<ServedFolder> out;
    for (std::size_t i = 0; i < s.folders.size(); ++i) {
        ServedFolder f{names[i], {}, s.folders[i].enabled};
        std::error_code ec;
        auto root = std::filesystem::canonical(path_from_utf8(s.folders[i].path), ec);
        if (!ec && std::filesystem::is_directory(root, ec)) f.root = root;
        out.push_back(std::move(f));
    }
    return out;
}

}  // namespace

Service::Service(std::filesystem::path settings_file, std::filesystem::path cache_dir, LogFn log)
    : settings_file_(std::move(settings_file)), log_(std::move(log)) {
    settings_ = Settings::load(settings_file_);
    cache_ = std::make_unique<ConvertCache>(
        std::move(cache_dir), [this](const auto& in, const auto& out, const platform::ConvertControl& control,
                                     std::string* why) { return convert(in, out, control, why); });
    cache_->set_on_busy([this](const std::string& name) {
        {
            std::lock_guard<std::mutex> g(lock_);
            converting_ = name;
        }
        record(name.empty() ? "conversion finished" : "converting " + name);
        notify();
    });
    cache_->set_on_note([this](const std::string& line) {
        record(line);
        notify();
    });
    server_ = std::make_unique<Server>(cache_.get(), [this](const std::string& line) {
        record(line);
        notify();
    });
    dictation_ = std::make_unique<Dictation>([this] { notify(); }, [this](const std::string& line) { record(line); });
}

Service::~Service() {
    dictation_.reset();
    server_->stop();
    server_.reset();
    cache_.reset();
}

Settings Service::settings() const {
    std::lock_guard<std::mutex> g(lock_);
    return settings_;
}

void Service::on_change(std::function<void()> fn) {
    std::lock_guard<std::mutex> g(lock_);
    on_change_ = std::move(fn);
}

void Service::notify() {
    std::function<void()> fn;
    {
        std::lock_guard<std::mutex> g(lock_);
        fn = on_change_;
    }
    if (fn) fn();
}

void Service::record(const std::string& line) {
    {
        std::lock_guard<std::mutex> g(lock_);
        recent_.push_back(clock_now() + "  " + line);
        while (recent_.size() > 200) recent_.pop_front();
    }
    if (log_) log_(line);
}

std::vector<std::string> Service::recent() const {
    std::lock_guard<std::mutex> g(lock_);
    return {recent_.begin(), recent_.end()};
}

bool Service::convert(const std::filesystem::path& in, const std::filesystem::path& out,
                      const platform::ConvertControl& control, std::string* why) {
    // The system's codecs first; then the user's own ffmpeg, if they chose one.
    Settings s = settings();
    std::string system_why;
    if (s.system_codecs && platform::can_convert() && platform::convert_to_mp3(in, out, control, &system_why))
        return true;
    if (control.cancelled()) return false;  // put back in the queue: not a failure
    const std::string& ffmpeg = s.ffmpeg;
    if (!ffmpeg.empty()) {
        std::string ffmpeg_why;
        if (platform::ffmpeg_to_mp3(path_from_utf8(ffmpeg), in, out, control, &ffmpeg_why)) return true;
        if (control.cancelled()) return false;
        system_why += system_why.empty() ? ffmpeg_why : "; ffmpeg: " + ffmpeg_why;
    }
    record("could not convert " + path_to_utf8(in.filename()) + (system_why.empty() ? "" : " (" + system_why + ")"));
    if (why) *why = system_why;
    return false;
}

std::string Service::secret(bool* made) {
    if (made) *made = false;
    std::lock_guard<std::mutex> g(lock_);
    if (!secret_.empty()) return secret_;
    if (settings_.secret) {
        if (auto opened = platform::open_secret(*settings_.secret)) {
            secret_ = *opened;
            return secret_;
        }
        // Sealed for another user or another machine: a new one is the only way on.
    }
    std::string fresh = new_secret();
    auto sealed = platform::seal_secret(fresh);
    if (!sealed) return "";
    settings_.secret = *sealed;
    settings_.save(settings_file_);
    secret_ = fresh;
    if (made) *made = true;
    return secret_;
}

std::string Service::stt_key(bool* made) {
    if (made) *made = false;
    std::lock_guard<std::mutex> g(lock_);
    if (!stt_key_.empty()) return stt_key_;
    if (settings_.stt_secret) {
        if (auto opened = platform::open_secret(*settings_.stt_secret)) {
            stt_key_ = *opened;
            return stt_key_;
        }
    }
    std::string fresh = new_stt_key();
    auto sealed = platform::seal_secret(fresh);
    if (!sealed) return "";
    settings_.stt_secret = *sealed;
    settings_.save(settings_file_);
    stt_key_ = fresh;
    if (made) *made = true;
    return stt_key_;
}

std::string Service::reset_stt_key() {
    {
        std::lock_guard<std::mutex> g(lock_);
        stt_key_.clear();
        settings_.stt_secret.reset();
    }
    std::string key = stt_key();
    dictation_->configure(dictation_config(settings()));
    record("a new dictation key was made; the old one no longer works");
    notify();
    return key;
}

std::filesystem::path Service::stt_models_dir() const {
    Settings s = settings();
    return s.stt_models_dir.empty() ? platform::default_stt_models_dir() : path_from_utf8(s.stt_models_dir);
}

void Service::set_stt_program(std::filesystem::path program) {
    {
        std::lock_guard<std::mutex> g(lock_);
        stt_program_ = std::move(program);
    }
    dictation_->configure(dictation_config(settings()));
}

void Service::set_stt_fake_engine(bool fake) {
    {
        std::lock_guard<std::mutex> g(lock_);
        stt_fake_engine_ = fake;
    }
    dictation_->configure(dictation_config(settings()));
}

void Service::start_dictation_if_enabled() {
    if (settings().stt_enabled) dictation_->start();
}

DictationConfig Service::dictation_config(const Settings& s) {
    DictationConfig c;
    c.key = stt_key();
    {
        std::lock_guard<std::mutex> g(lock_);
        c.program = stt_program_.empty() ? platform::find_stt_server() : stt_program_;
        c.fake_engine = stt_fake_engine_;
    }
    c.model = s.stt_model;
    c.models_dir = s.stt_models_dir.empty() ? platform::default_stt_models_dir() : path_from_utf8(s.stt_models_dir);
    c.language = s.stt_language;
    c.port = s.stt_port;
    c.origins = s.origins;
    // Every folder by its name, so a path means the same file to both servers; one that is off has no
    // root, so the speech server refuses it rather than looking for it elsewhere.
    for (const auto& f : served_folders(s)) c.audio_folders.push_back({f.name, f.enabled ? f.root : std::filesystem::path()});
    return c;
}

std::string Service::reset_secret() {
    {
        std::lock_guard<std::mutex> g(lock_);
        secret_.clear();
        settings_.secret.reset();
    }
    std::string s = secret();
    Settings current = settings();
    std::string problem;
    server_->configure(server_config(current, &problem));
    record("a new secret was made; the old one no longer works");
    notify();
    return s;
}

ServerConfig Service::server_config(const Settings& s, std::string* problem) {
    ServerConfig c;
    for (const auto& o : s.origins) c.origins.insert(o);
    c.link_ttl_sec = s.link_hours * 3600;
    {
        std::lock_guard<std::mutex> g(lock_);
        c.secret = secret_;
    }
    c.folders = served_folders(s);
    if (s.folders.empty()) {
        *problem = "no folder chosen";
        return c;
    }
    for (std::size_t i = 0; i < s.folders.size(); ++i) {
        if (s.folders[i].enabled && c.folders[i].root.empty()) {
            *problem = "the folder cannot be opened: " + s.folders[i].path;
            return c;
        }
    }
    if (!c.serving()) *problem = "every folder is off";
    return c;
}

std::string Service::apply(Settings s, bool persist) {
    int old_port;
    bool stt_was_enabled;
    {
        std::lock_guard<std::mutex> g(lock_);
        old_port = settings_.port;
        stt_was_enabled = settings_.stt_enabled;
        s.secret = settings_.secret;  // the secret is changed only by reset_secret()
        s.stt_secret = settings_.stt_secret;  // and the dictation key only by reset_stt_key()
        settings_ = s;
    }
    if (persist) settings_.save(settings_file_);
    secret();
    dictation_->configure(dictation_config(s));
    // Ticking "start with the server" means wanting it now as well.
    if (s.stt_enabled && !stt_was_enabled) dictation_->start();
    cache_->set_enabled(s.convert);
    cache_->set_prepare(s.prepare);
    cache_->set_limit_bytes(std::uint64_t(s.cache_mb) * 1024 * 1024);
    std::string problem;
    ServerConfig config = server_config(s, &problem);
    std::vector<std::filesystem::path> roots;
    for (const auto& f : config.folders)
        if (f.enabled && !f.root.empty()) roots.push_back(f.root);
    cache_->set_roots(roots);
    server_->configure(config);
    // Without a folder there is nothing to serve: not listening says so more plainly than a 503.
    if (server_->running() && (old_port != s.port || !config.serving())) server_->stop();
    std::string start_problem = server_->running() || !config.serving() ? "" : start();
    {
        std::lock_guard<std::mutex> g(lock_);
        problem_ = !start_problem.empty() ? start_problem : problem;
    }
    notify();
    return problem_;
}

std::string Service::start() {
    std::string error;
    int port = settings().port;
    if (!server_->start(port, &error)) {
        std::lock_guard<std::mutex> g(lock_);
        problem_ = error;
        return error;
    }
    record("listening on http://127.0.0.1:" + std::to_string(server_->port()));
    return "";
}

void Service::stop() {
    server_->stop();
    record("stopped");
    notify();
}

Service::Status Service::status() const {
    Status st;
    st.running = server_->running();
    st.port = server_->running() ? server_->port() : settings().port;
    st.requests = server_->requests();
    std::lock_guard<std::mutex> g(lock_);
    for (const auto& f : settings_.folders)
        if (f.enabled) st.folders.push_back(f.path);
    st.problem = problem_;
    st.converting = converting_;
    return st;
}

}  // namespace clew
