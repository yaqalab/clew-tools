// macOS and Linux side of platform.h. Compatible, NOT built or tested yet (README, "Other systems").
//
// What is here works without extra libraries: system randomness, the folders, and ffmpeg as an
// optional converter. What is left to do:
//   - the secret: macOS Keychain (Security.framework) and Linux Secret Service (libsecret); until
//     then it is kept in a file readable by this user only ("file").
//   - conversion with the system's own codecs: macOS AudioToolbox (decodes, but has no mp3 encoder;
//     would produce m4a); Linux has none (GStreamer is optional). Until then: ffmpeg only.
//   - the sandbox for the converter (macOS sandbox-exec profile; Linux seccomp or bubblewrap).
//
// Child processes (dictation, analysis) mirror the Windows ones: one pipe in (the key, then
// closed), stdout and stderr together on one pipe out, read line by line on a thread of their own.
// A process group stands in for the job object, so stop() ends the child and whatever it started;
// on Linux the child also dies with this program (PR_SET_PDEATHSIG), and its memory is capped with
// RLIMIT_DATA. macOS has neither: a child outlives a killed parent there, and is not capped.
#include <fcntl.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <stdlib.h>  // arc4random_buf
#else
#include <sys/prctl.h>
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

namespace {

class PosixChild final : public Child {
public:
    PosixChild(pid_t pid, int output, std::function<void(const std::string&)> on_line, std::function<void(int)> on_exit)
        : pid_(pid), output_(output) {
        reader_ = std::thread([this, on_line = std::move(on_line), on_exit = std::move(on_exit)] {
            std::string pending;
            char buf[4096];
            for (;;) {
                ssize_t n = ::read(output_, buf, sizeof buf);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) break;
                pending.append(buf, std::size_t(n));
                std::size_t end;
                while ((end = pending.find('\n')) != std::string::npos) {
                    std::string line = pending.substr(0, end);
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    pending.erase(0, end + 1);
                    if (on_line) on_line(line);
                }
                if (pending.size() > 65536) pending.clear();  // a line that never ends is not one
            }
            if (!pending.empty() && on_line) on_line(pending);
            ::close(output_);
            int status = 0;
            pid_t r;
            do r = waitpid(pid_, &status, 0);
            while (r < 0 && errno == EINTR);
            {
                std::lock_guard<std::mutex> g(lock_);
                reaped_ = true;  // from here on the pid may belong to someone else: never signal it
            }
            int code = r == pid_ && WIFEXITED(status) ? WEXITSTATUS(status) : 1;
            if (on_exit) on_exit(code);
        });
    }

    ~PosixChild() override {
        stop();
        if (reader_.joinable()) reader_.join();
    }

    // The whole group, as TerminateJobObject ends the whole job.
    void stop() override {
        std::lock_guard<std::mutex> g(lock_);
        if (!reaped_) ::kill(-pid_, SIGKILL);
    }

private:
    pid_t pid_;
    int output_;
    std::mutex lock_;
    bool reaped_ = false;
    std::thread reader_;
};

bool make_pipe(int fds[2]) {
#if defined(__APPLE__)
    if (::pipe(fds) != 0) return false;
    for (int i = 0; i < 2; ++i) ::fcntl(fds[i], F_SETFD, FD_CLOEXEC);
    return true;
#else
    return ::pipe2(fds, O_CLOEXEC) == 0;
#endif
}

}  // namespace

std::unique_ptr<Child> start_child(const fs::path& exe, const std::vector<std::string>& args,
                                   const std::string& input, std::uint64_t memory_limit,
                                   std::function<void(const std::string&)> on_line, std::function<void(int)> on_exit,
                                   std::string* why) {
    int in[2], out[2], failed[2];  // `failed`: exec's errno, if it never ran (closed by a successful exec)
    if (!make_pipe(in)) {
        if (why) *why = "no pipe";
        return nullptr;
    }
    if (!make_pipe(out)) {
        ::close(in[0]);
        ::close(in[1]);
        if (why) *why = "no pipe";
        return nullptr;
    }
    if (!make_pipe(failed)) {
        for (int fd : {in[0], in[1], out[0], out[1]}) ::close(fd);
        if (why) *why = "no pipe";
        return nullptr;
    }

    // Everything the child needs, prepared before fork: only async-signal-safe calls after it.
    std::string program = exe.string();
    std::string dir = exe.parent_path().string();
    std::vector<std::string> argv_s;
    argv_s.push_back(program);
    for (const auto& a : args) argv_s.push_back(a);
    std::vector<char*> argv;
    for (auto& a : argv_s) argv.push_back(a.data());
    argv.push_back(nullptr);
    // Python writes its lines at once rather than when a buffer fills.
    ::setenv("PYTHONUNBUFFERED", "1", 1);
    ::setenv("PYTHONIOENCODING", "utf-8", 1);

    pid_t pid = fork();
    if (pid < 0) {
        for (int fd : {in[0], in[1], out[0], out[1], failed[0], failed[1]}) ::close(fd);
        if (why) *why = "could not start " + program + " (fork)";
        return nullptr;
    }
    if (pid == 0) {
        ::setpgid(0, 0);
#if !defined(__APPLE__)
        ::prctl(PR_SET_PDEATHSIG, SIGKILL);
        if (memory_limit) {
            struct rlimit lim{rlim_t(memory_limit), rlim_t(memory_limit)};
            ::setrlimit(RLIMIT_DATA, &lim);
        }
#else
        (void)memory_limit;
#endif
        ::signal(SIGPIPE, SIG_DFL);
        ::dup2(in[0], 0);
        ::dup2(out[1], 1);
        ::dup2(out[1], 2);
        if (::chdir(dir.c_str()) != 0) {
            // Not fatal: the program finds its own files from its path.
        }
        ::execv(program.c_str(), argv.data());
        int e = errno;
        ssize_t ignored = ::write(failed[1], &e, sizeof e);
        (void)ignored;
        _exit(127);
    }
    ::setpgid(pid, pid);  // also from here, so a stop() right away reaches the group
    ::close(in[0]);
    ::close(out[1]);
    ::close(failed[1]);
    int exec_errno = 0;
    ssize_t got;
    do got = ::read(failed[0], &exec_errno, sizeof exec_errno);
    while (got < 0 && errno == EINTR);
    ::close(failed[0]);
    if (got == sizeof exec_errno) {
        ::close(in[1]);
        ::close(out[0]);
        int status = 0;
        waitpid(pid, &status, 0);
        if (why) *why = "could not start " + program + " (" + std::strerror(exec_errno) + ")";
        return nullptr;
    }
    if (!input.empty()) {
        std::size_t at = 0;
        while (at < input.size()) {
            ssize_t n = ::write(in[1], input.data() + at, input.size() - at);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) break;
            at += std::size_t(n);
        }
    }
    ::close(in[1]);
    return std::make_unique<PosixChild>(pid, out[0], std::move(on_line), std::move(on_exit));
}

fs::path find_stt_server() {
    if (const char* forced = std::getenv("CLEW_STT_SERVER_EXE")) return fs::path(forced);
    std::error_code ec;
    auto beside = self_path().parent_path() / "clew-stt-server" / "clew-stt-server";
    return fs::is_regular_file(beside, ec) ? beside : fs::path();
}

fs::path find_analysis_server() {
    if (const char* forced = std::getenv("CLEW_ANALYSIS_SERVER_EXE")) return fs::path(forced);
    std::error_code ec;
    auto beside = self_path().parent_path() / "clew-analysis-server" / "clew-analysis-server";
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
