#include "headless.h"

#include <chrono>
#include <condition_variable>
#include <mutex>

#include "files.h"
#include "platform.h"
#include "service.h"
#include "settings.h"
#include "text.h"

namespace clew {

const char* const kHeadlessHelp =
    "Clew audio server: serves the audio files of your folders to Clew, on 127.0.0.1 only.\n"
    "\n"
    "  clew-audio-server                 the tray icon and the settings window (Windows)\n"
    "  clew-audio-server --headless      no window; output on the console\n"
    "\n"
    "With --headless (each option for this run only; the settings window's values are the defaults):\n"
    "  --folder <path>        a folder to serve (repeatable; replaces the window's list for this run)\n"
    "  --folder-off <path>    a folder in the list, but off: named, not served (repeatable)\n"
    "  --port <n>             default 47821\n"
    "  --allow-origin <url>   a hosted Clew's address (repeatable); loopback is always allowed\n"
    "  --ffmpeg <path>        an ffmpeg to try when the system's codecs cannot convert a file\n"
    "  --no-convert           send every file as it is\n"
    "  --no-system-codecs     convert with --ffmpeg only\n"
    "  --show-secret          print the secret\n"
    "  --reset-secret         make a new secret (saved); the old one stops working\n"
    "  --config <file>        the settings file (default: the settings window's)\n"
    "  --cache-dir <dir>      where converted copies go (default: .clew-converted in the folder)\n"
    "\n"
    "Dictation (the speech server on this computer, docs/28):\n"
    "  --dictation            start it too\n"
    "  --stt-port <n>         default 47822\n"
    "  --stt-model <name>     small or large-v3-turbo\n"
    "  --stt-models-dir <dir> where the models are\n"
    "  --stt-language <code>  en, ja, fr or mg; default: Clew names one per session\n"
    "  --stt-download <name>  fetch a model (or \"speakers\", the speaker model), then quit\n"
    "  --stt-program <exe>    the server program (default: clew-stt-server beside this one)\n"
    "  --show-stt-key         print its key\n"
    "  --version\n";

namespace {

// --stt-download: fetches one model with the server program, saying how far it got.
int download_model(Service& svc, Settings s, const std::string& model, const Console& console) {
    svc.apply(s, /*persist=*/false);
    auto dir = svc.stt_models_dir();
    auto here = [&] {
        return model == "speakers" ? Dictation::speaker_model_present(dir) : Dictation::model_present(dir, model);
    };
    if (here()) {
        console.out("Already here: " + path_to_utf8(dir / path_from_utf8(model)));
        return 0;
    }
    std::mutex m;
    std::condition_variable cv;
    svc.dictation().download(model);
    int last = -1;
    for (;;) {
        auto st = svc.dictation().status();
        if (st.downloading.empty() && (here() || !st.download_problem.empty())) {
            if (!st.download_problem.empty()) {
                console.out("Download failed: " + st.download_problem);
                return 1;
            }
            console.out("Downloaded: " + path_to_utf8(dir / path_from_utf8(model)));
            return 0;
        }
        if (st.percent != last && !st.downloading.empty()) {
            last = st.percent;
            console.out(model + ": " + std::to_string(st.percent) + " %");
        }
        std::unique_lock<std::mutex> lk(m);
        cv.wait_for(lk, std::chrono::milliseconds(200));
    }
}

bool take(const std::vector<std::string>& args, std::size_t& i, std::string* value) {
    if (i + 1 >= args.size()) return false;
    *value = args[++i];
    return true;
}

}  // namespace

int run_headless(const std::vector<std::string>& args, const Console& console) {
    std::string config, cache_dir, ffmpeg, value;
    std::vector<FolderSetting> folders;
    std::string stt_model, stt_models_dir, stt_language, stt_download, stt_program;
    std::vector<std::string> origins;
    int port = -1, link_seconds = 0;  // -1: the settings' port; 0: any free port
    int stt_port = -1;
    bool reset = false, show = false, no_convert = false, no_system = false;
    bool dictation = false, show_stt_key = false, stt_fake = false;
    for (std::size_t i = 0; i < args.size(); ++i) {
        const std::string& a = args[i];
        if (a == "--headless") continue;
        if (a == "--help" || a == "-h") {
            console.out(kHeadlessHelp);
            return 0;
        }
        if (a == "--version") {
            console.out(std::string(kAppName) + " " + kVersion);
            return 0;
        }
        bool ok = true;
        if (a == "--folder" || a == "--folder-off") {
            ok = take(args, i, &value);
            if (ok) folders.push_back({path_to_utf8(std::filesystem::absolute(path_from_utf8(value))), a == "--folder"});
        }
        else if (a == "--config") ok = take(args, i, &config);
        else if (a == "--cache-dir") ok = take(args, i, &cache_dir);
        else if (a == "--ffmpeg") ok = take(args, i, &ffmpeg);
        else if (a == "--dictation") dictation = true;
        else if (a == "--show-stt-key") show_stt_key = true;
        else if (a == "--stt-fake-engine") stt_fake = true;  // the tests: no model needed
        else if (a == "--stt-model") ok = take(args, i, &stt_model) && stt_model_known(stt_model);
        else if (a == "--stt-models-dir") ok = take(args, i, &stt_models_dir);
        else if (a == "--stt-language") ok = take(args, i, &stt_language) && stt_language_known(stt_language);
        else if (a == "--stt-download")
            ok = take(args, i, &stt_download) && (stt_model_known(stt_download) || stt_download == "speakers");
        else if (a == "--stt-program") ok = take(args, i, &stt_program);
        else if (a == "--stt-port") {
            ok = take(args, i, &value) && all_digits(value) && value.size() <= 5 && std::stoi(value) >= 1024 &&
                 std::stoi(value) <= 65535;
            if (ok) stt_port = std::stoi(value);
        }
        else if (a == "--allow-origin") {
            ok = take(args, i, &value);
            auto o = ok ? normalize_origin(value) : std::nullopt;
            if (ok && !o) {
                console.out("Not an origin (https://host[:port]): " + value);
                return 2;
            }
            if (o) origins.push_back(*o);
        } else if (a == "--port" || a == "--link-seconds") {
            ok = take(args, i, &value) && all_digits(value) && value.size() <= 6;
            int n = ok ? std::stoi(value) : 0;
            if (a == "--port") {
                ok = ok && n <= 65535;
                port = n;
            } else {
                // For the tests: links that expire within seconds.
                link_seconds = n;
            }
        } else if (a == "--reset-secret") reset = true;
        else if (a == "--show-secret") show = true;
        else if (a == "--no-convert") no_convert = true;
        else if (a == "--no-system-codecs") no_system = true;
        else {
            console.out("Unknown option: " + a + "\n\n" + kHeadlessHelp);
            return 2;
        }
        if (!ok) {
            console.out("Missing or bad value for " + a);
            return 2;
        }
    }

    Service svc(config.empty() ? default_settings_file() : path_from_utf8(config),
                cache_dir.empty() ? std::filesystem::path() : path_from_utf8(cache_dir), console.err);
    Settings s = svc.settings();
    const bool had_stt_key = s.stt_secret.has_value();
    if (!stt_model.empty()) s.stt_model = stt_model;
    if (!stt_models_dir.empty())
        s.stt_models_dir = path_to_utf8(std::filesystem::absolute(path_from_utf8(stt_models_dir)));
    if (!stt_language.empty()) s.stt_language = stt_language;
    if (stt_port >= 0) s.stt_port = stt_port;
    if (!stt_program.empty()) svc.set_stt_program(std::filesystem::absolute(path_from_utf8(stt_program)));
    if (stt_fake) svc.set_stt_fake_engine(true);

    if (!stt_download.empty()) return download_model(svc, s, stt_download, console);

    if (!folders.empty()) s.folders = folders;
    if (port >= 0) s.port = port;
    for (const auto& o : origins) s.origins.push_back(o);
    if (!ffmpeg.empty()) s.ffmpeg = ffmpeg;
    if (no_convert) s.convert = false;
    if (no_system) s.system_codecs = false;
    bool any_on = false;
    for (const auto& f : s.folders) {
        if (!f.enabled) continue;
        any_on = true;
        std::error_code ec;
        if (!std::filesystem::is_directory(path_from_utf8(f.path), ec)) {
            console.out("Not a folder: " + f.path);
            return 2;
        }
    }
    if (!any_on) {
        console.out("No folder chosen. Start with --folder <path>, or choose one in the settings window.");
        return 2;
    }

    bool made = false;
    std::string secret = reset ? svc.reset_secret() : svc.secret(&made);
    made = made || reset;
    if (secret.empty()) {
        console.out("This system could not keep a secret safely, so the server does not start.");
        return 3;
    }
    std::string problem = svc.apply(s, /*persist=*/false);
    if (!svc.server().running()) {
        console.out("Port " + std::to_string(s.port) + " is not available (" + problem + "). Try --port <number>.");
        return 2;
    }
    if (link_seconds > 0) {
        ServerConfig c = *svc.server().config();
        c.link_ttl_sec = link_seconds;
        svc.server().configure(c);
    }

    console.out("Clew audio server  ->  http://127.0.0.1:" + std::to_string(svc.server().port()));
    if (!s.origins.empty()) {
        std::string list;
        for (const auto& o : s.origins) list += (list.empty() ? "" : ", ") + o;
        console.out("Also allowed to call it: " + list);
    }
    auto served = svc.server().config()->folders;
    for (std::size_t i = 0; i < served.size() && i < s.folders.size(); ++i) {
        const auto& f = served[i];
        if (!f.enabled) {
            console.out("Folder \"" + f.name + "\": " + s.folders[i].path + "  (off)");
            continue;
        }
        bool truncated = false;
        auto files = list_audio(f.root, &truncated);
        console.out("Folder \"" + f.name + "\": " + s.folders[i].path + "  (" + std::to_string(files.size()) +
                    " audio files" + (truncated ? ", more than shown" : "") + ")");
    }
    if (!s.convert)
        console.out("Conversion: off");
    else
        console.out(std::string("Conversion: ") +
                    (s.system_codecs && platform::can_convert() ? "the system's own codecs" : "no system codecs") +
                    (s.ffmpeg.empty() ? "" : ", then " + s.ffmpeg) +
                    " (.wma, .amr and variable-bit-rate mp3 are converted so that browsers play them)");
    if (made || show) {
        console.out("");
        console.out(made ? "Secret (shown once here; the settings window shows it again) - paste it into Clew:"
                         : "Secret - paste it into Clew:");
        console.out("    " + secret);
        console.out("");
    } else {
        console.out("Secret: in the settings window, or start with --show-secret.");
    }
    if (dictation) {
        bool made_key = false;
        std::string key = svc.stt_key(&made_key);
        made_key = made_key || !had_stt_key;
        svc.dictation().start();
        console.out("Dictation  ->  http://127.0.0.1:" + std::to_string(s.stt_port) + "  (model " + s.stt_model +
                    ", starting)");
        if (made_key || show_stt_key) console.out("Dictation key - paste it into Clew's Dictation settings:\n    " + key);
    }
    console.out("Press Ctrl+C to stop.");
    console.wait_for_stop();
    svc.dictation().stop();
    svc.stop();
    return 0;
}

}  // namespace clew
