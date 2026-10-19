// macOS and Linux side of platform.h. Compatible, NOT built or tested yet (README, "Other systems").
//
// What is here works without extra libraries: system randomness, the folders, and ffmpeg as an
// optional converter. What is left to do:
//   - the secret: macOS Keychain (Security.framework) and Linux Secret Service (libsecret); until
//     then it is kept in a file readable by this user only ("file").
//   - conversion with the system's own codecs: macOS AudioToolbox (decodes, but has no mp3 encoder;
//     would produce m4a); Linux has none (GStreamer is optional). Until then: ffmpeg only.
//   - the sandbox for the converter (macOS sandbox-exec profile; Linux seccomp or bubblewrap).
//   - the dictation server as a child (fork, a pipe each way, kill on exit); start_child says
//     "not on this system yet" until then.
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <vector>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <stdlib.h>  // arc4random_buf
#else
#include <sys/random.h>
#endif

#include "../core/platform.h"
#include "../core/text.h"

namespace clew::platform {

std::string random_bytes(std::size_t n) {
    std::string out(n, '\0');
#if defined(__APPLE__)
    arc4random_buf(out.data(), n);
#else
    std::size_t got = 0;
    while (got < n) {
        ssize_t r = getrandom(out.data() + got, n - got, 0);
        if (r <= 0) throw std::runtime_error("no system randomness");
        got += std::size_t(r);
    }
#endif
    return out;
}

// Until the key stores are wired in: the settings file itself is made readable by this user only.
std::optional<Sealed> seal_secret(const std::string& plain) { return Sealed{"file", base64_encode(plain)}; }

std::optional<std::string> open_secret(const Sealed& sealed) {
    if (sealed.kind != "file") return std::nullopt;
    return base64_decode(sealed.data);
}

fs::path config_dir() {
    const char* home = std::getenv("HOME");
#if defined(__APPLE__)
    return fs::path(home ? home : ".") / "Library" / "Application Support" / "clew-audio-server";
#else
    const char* xdg = std::getenv("XDG_CONFIG_HOME");
    return (xdg && *xdg ? fs::path(xdg) : fs::path(home ? home : ".") / ".config") / "clew-audio-server";
#endif
}

fs::path self_path() {
#if defined(__APPLE__)
    char buf[4096];
    uint32_t size = sizeof buf;
    if (_NSGetExecutablePath(buf, &size) == 0) return fs::path(buf);
    return {};
#else
    std::error_code ec;
    return fs::read_symlink("/proc/self/exe", ec);
#endif
}

bool can_convert() { return false; }

bool convert_to_mp3(const fs::path&, const fs::path&, const ConvertControl&, std::string* why) {
    if (why) *why = "no system codecs on this system yet";
    return false;
}

bool ffmpeg_to_mp3(const fs::path& ffmpeg, const fs::path& in, const fs::path& out, const ConvertControl& control,
                   std::string* why) {
    int fd = ::open(out.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) {
        if (why) *why = "could not write the cache";
        return false;
    }
    pid_t pid = fork();
    if (pid == 0) {
        dup2(fd, 1);
        int devnull = ::open("/dev/null", O_RDWR);
        dup2(devnull, 0);
        if (control.background) (void)nice(19);
        std::vector<std::string> args = {ffmpeg.string(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i",
                                         in.string(), "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame",
                                         "-b:a", "128k", "-write_xing", "0", "-id3v2_version", "0", "-f", "mp3",
                                         "pipe:1"};
        std::vector<char*> argv;
        for (auto& a : args) argv.push_back(a.data());
        argv.push_back(nullptr);
        execv(argv[0], argv.data());
        _exit(127);
    }
    ::close(fd);
    int status = 0;
    bool stopped = false;
    while (pid > 0) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid || r < 0) break;
        if (control.cancelled() && !stopped) {
            kill(pid, SIGKILL);
            stopped = true;
        }
        usleep(100 * 1000);
    }
    if (pid < 0 || stopped || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        if (why) *why = stopped ? "stopped for the file being opened" : "ffmpeg failed";
        std::error_code ec;
        fs::remove(out, ec);
        return false;
    }
    return true;
}

fs::path find_ffmpeg() {
    std::error_code ec;
    auto beside = self_path().parent_path() / "ffmpeg";
    if (fs::is_regular_file(beside, ec)) return beside;
    const char* path = std::getenv("PATH");
    std::string p = path ? path : "";
    std::size_t at = 0;
    while (at <= p.size()) {
        std::size_t end = p.find(':', at);
        if (end == std::string::npos) end = p.size();
        fs::path candidate = fs::path(p.substr(at, end - at)) / "ffmpeg";
        if (!p.substr(at, end - at).empty() && fs::is_regular_file(candidate, ec)) return candidate;
        at = end + 1;
    }
    return {};
}

std::unique_ptr<Child> start_child(const fs::path&, const std::vector<std::string>&, const std::string&,
                                   std::uint64_t, std::function<void(const std::string&)>, std::function<void(int)>,
                                   std::string* why) {
    if (why) *why = "not on this system yet";
    return nullptr;
}

fs::path find_stt_server() {
    if (const char* forced = std::getenv("CLEW_STT_SERVER_EXE")) return fs::path(forced);
    std::error_code ec;
    auto beside = self_path().parent_path() / "clew-stt-server" / "clew-stt-server";
    return fs::is_regular_file(beside, ec) ? beside : fs::path();
}

std::uint64_t total_memory() {
    long pages = sysconf(_SC_PHYS_PAGES), size = sysconf(_SC_PAGE_SIZE);
    return pages > 0 && size > 0 ? std::uint64_t(pages) * std::uint64_t(size) : 0;
}

fs::path default_stt_models_dir() {
    const char* home = std::getenv("HOME");
#if defined(__APPLE__)
    return fs::path(home ? home : ".") / "Library" / "Application Support" / "clew-stt" / "models";
#else
    const char* xdg = std::getenv("XDG_DATA_HOME");
    return (xdg && *xdg ? fs::path(xdg) : fs::path(home ? home : ".") / ".local" / "share") / "clew-stt" / "models";
#endif
}

}  // namespace clew::platform
