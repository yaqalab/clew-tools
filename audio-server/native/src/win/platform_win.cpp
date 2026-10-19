// The Windows side of platform.h: system randomness, DPAPI for the secret, the user's folders,
// conversion in a sandboxed child process, and the dictation server as a child in a job.
#include <windows.h>
#include <bcrypt.h>
#include <dpapi.h>
#include <sddl.h>
#include <shlobj.h>

#include <atomic>
#include <iterator>
#include <stdexcept>
#include <thread>
#include <vector>

#include "../core/platform.h"
#include "../core/text.h"
#include "mf_convert.h"
#include "util.h"

namespace clew::platform {

namespace {

const char kEntropy[] = "clew-audio-server/secret/v1";
constexpr DWORD kConvertTimeoutMs = 30 * 60 * 1000;
constexpr SIZE_T kChildMemory = SIZE_T(1536) * 1024 * 1024;

std::string path_to_utf8_win(const fs::path& p) { return win::narrow(p.wstring()); }

fs::path known_folder(REFKNOWNFOLDERID id) {
    PWSTR p = nullptr;
    fs::path out;
    if (SUCCEEDED(SHGetKnownFolderPath(id, KF_FLAG_CREATE, nullptr, &p))) out = p;
    CoTaskMemFree(p);
    return out;
}

struct Handle {
    HANDLE h = nullptr;
    Handle() = default;
    explicit Handle(HANDLE v) : h(v == INVALID_HANDLE_VALUE ? nullptr : v) {}
    ~Handle() {
        if (h) CloseHandle(h);
    }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    void reset(HANDLE v = nullptr) {
        if (h) CloseHandle(h);
        h = v;
    }
};

// A copy of this process's token at low integrity: what it starts can read the user's files but
// write almost nowhere (not the user's documents, not the registry), whatever a codec does.
HANDLE low_integrity_token() {
    HANDLE self = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(),
                          TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT, &self))
        return nullptr;
    HANDLE low = nullptr;
    BOOL ok = DuplicateTokenEx(self, 0, nullptr, SecurityImpersonation, TokenPrimary, &low);
    CloseHandle(self);
    if (!ok) return nullptr;
    PSID sid = nullptr;
    if (!ConvertStringSidToSidW(L"S-1-16-4096", &sid)) {
        CloseHandle(low);
        return nullptr;
    }
    TOKEN_MANDATORY_LABEL label{};
    label.Label.Attributes = SE_GROUP_INTEGRITY;
    label.Label.Sid = sid;
    ok = SetTokenInformation(low, TokenIntegrityLevel, &label, DWORD(sizeof label + GetLengthSid(sid)));
    LocalFree(sid);
    if (!ok) {
        CloseHandle(low);
        return nullptr;
    }
    return low;
}

// One process, at most 1.5 GB, no windows, no clipboard, killed with the job.
HANDLE sandbox_job() {
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (!job) return nullptr;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim{};
    lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS |
                                           JOB_OBJECT_LIMIT_PROCESS_MEMORY |
                                           JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
    lim.BasicLimitInformation.ActiveProcessLimit = 1;
    lim.ProcessMemoryLimit = kChildMemory;
    JOBOBJECT_BASIC_UI_RESTRICTIONS ui{};
    ui.UIRestrictionsClass = JOB_OBJECT_UILIMIT_DESKTOP | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS |
                             JOB_OBJECT_UILIMIT_EXITWINDOWS | JOB_OBJECT_UILIMIT_GLOBALATOMS |
                             JOB_OBJECT_UILIMIT_HANDLES | JOB_OBJECT_UILIMIT_READCLIPBOARD |
                             JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS | JOB_OBJECT_UILIMIT_WRITECLIPBOARD;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &lim, sizeof lim) ||
        !SetInformationJobObject(job, JobObjectBasicUIRestrictions, &ui, sizeof ui)) {
        CloseHandle(job);
        return nullptr;
    }
    return job;
}

// The start attributes both kinds of child use: only `a` and `b` are inherited, and the child is
// born inside `job`. Created outside it and assigned a moment later, a child whose parent died in
// that moment stayed suspended forever, in no job (found by the tests, 2026-10-16); born inside,
// it ends with the job's last handle like the rest.
class StartAttributes {
public:
    bool init(HANDLE a, HANDLE b, HANDLE job) {
        handles_[0] = a;
        handles_[1] = b;
        job_ = job;
        SIZE_T size = 0;
        InitializeProcThreadAttributeList(nullptr, 2, 0, &size);
        buf_.resize(size);
        auto list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(buf_.data());
        if (!InitializeProcThreadAttributeList(list, 2, 0, &size)) return false;
        list_ = list;
        return UpdateProcThreadAttribute(list_, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles_, sizeof handles_,
                                         nullptr, nullptr) &&
               UpdateProcThreadAttribute(list_, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, &job_, sizeof job_, nullptr,
                                         nullptr);
    }
    LPPROC_THREAD_ATTRIBUTE_LIST get() const { return list_; }
    ~StartAttributes() {
        if (list_) DeleteProcThreadAttributeList(list_);
    }

private:
    std::vector<char> buf_;
    LPPROC_THREAD_ATTRIBUTE_LIST list_ = nullptr;
    HANDLE handles_[2] = {};
    HANDLE job_ = nullptr;
};

// Runs `app` sandboxed; what it writes to its standard output becomes `out`. Its standard error is
// kept (a little) to say why it failed.
bool run_sandboxed(const std::wstring& app, std::wstring cmdline, const fs::path& out, const ConvertControl& control,
                   std::string* why) {
    SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
    HANDLE out_r = nullptr, out_w = nullptr, err_r = nullptr, err_w = nullptr;
    if (!CreatePipe(&out_r, &out_w, &sa, 1 << 16) || !CreatePipe(&err_r, &err_w, &sa, 1 << 16)) {
        if (why) *why = "no pipe";
        return false;
    }
    Handle hr(out_r), hw(out_w), er(err_r), ew(err_w);
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);

    Handle job(sandbox_job());
    if (!job.h) {
        if (why) *why = "no job object";
        return false;
    }
    // Only these two handles reach the child, nothing else this process holds; it is born in the job.
    StartAttributes attrs;
    if (!attrs.init(out_w, err_w, job.h)) {
        if (why) *why = "no attribute list";
        return false;
    }
    STARTUPINFOEXW si{};
    si.StartupInfo.cb = sizeof si;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = nullptr;
    si.StartupInfo.hStdOutput = out_w;
    si.StartupInfo.hStdError = err_w;
    si.lpAttributeList = attrs.get();
    // The file being opened at normal priority; preparing ahead only with what is left over.
    DWORD flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW |
                  (control.background ? IDLE_PRIORITY_CLASS : NORMAL_PRIORITY_CLASS);
    PROCESS_INFORMATION pi{};
    Handle token(low_integrity_token());
    BOOL started = token.h && CreateProcessAsUserW(token.h, app.c_str(), cmdline.data(), nullptr, nullptr, TRUE,
                                                   flags, nullptr, nullptr, &si.StartupInfo, &pi);
    if (!started) {
        // Some systems refuse the low-integrity start; the job's limits still hold.
        started = CreateProcessW(app.c_str(), cmdline.data(), nullptr, nullptr, TRUE, flags, nullptr, nullptr,
                                 &si.StartupInfo, &pi);
    }
    if (!started) {
        if (why) *why = "could not start the converter";
        return false;
    }
    Handle process(pi.hProcess), thread(pi.hThread);
    if (!AssignProcessToJobObject(job.h, pi.hProcess)) {
        TerminateProcess(pi.hProcess, 1);
        if (why) *why = "could not confine the converter";
        return false;
    }
    ResumeThread(pi.hThread);
    hw.reset();
    ew.reset();

    std::atomic<bool> timed_out{false}, cancelled{false};
    std::thread watchdog([&] {
        ULONGLONG start = GetTickCount64();
        // A short poll: the file someone is opening waits for this to end.
        while (WaitForSingleObject(pi.hProcess, 20) == WAIT_TIMEOUT) {
            if (control.cancelled()) {
                cancelled = true;
                TerminateJobObject(job.h, 1);
                return;
            }
            if (GetTickCount64() - start > kConvertTimeoutMs) {
                timed_out = true;
                TerminateJobObject(job.h, 1);
                return;
            }
        }
    });
    std::string err;
    std::thread err_reader([&] {
        char buf[1024];
        DWORD n = 0;
        while (ReadFile(er.h, buf, sizeof buf, &n, nullptr) && n > 0)
            if (err.size() < 4096) err.append(buf, n);
    });

    Handle file(CreateFileW(out.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr));
    bool wrote_all = file.h != nullptr;
    std::vector<char> buf(1 << 16);
    DWORD n = 0;
    unsigned long long total = 0;
    while (ReadFile(hr.h, buf.data(), DWORD(buf.size()), &n, nullptr) && n > 0) {
        DWORD done = 0;
        if (wrote_all && (!WriteFile(file.h, buf.data(), n, &done, nullptr) || done != n)) wrote_all = false;
        total += n;
    }
    if (!wrote_all) TerminateJobObject(job.h, 1);
    WaitForSingleObject(pi.hProcess, INFINITE);
    watchdog.join();
    err_reader.join();
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    file.reset();
    if (code == 0 && wrote_all && total > 0 && !timed_out && !cancelled) return true;
    if (why) {
        *why = cancelled   ? "stopped for the file being opened"
               : timed_out ? "took longer than 30 minutes"
               : !wrote_all ? "could not write the cache"
                            : trim(err);
        if (why->empty()) *why = "the converter stopped (" + std::to_string(code) + ")";
    }
    std::error_code ec;
    fs::remove(out, ec);
    return false;
}

}  // namespace

std::string random_bytes(std::size_t n) {
    std::string out(n, '\0');
    if (n && !BCRYPT_SUCCESS(BCryptGenRandom(nullptr, reinterpret_cast<PUCHAR>(out.data()), ULONG(n),
                                             BCRYPT_USE_SYSTEM_PREFERRED_RNG)))
        throw std::runtime_error("no system randomness");
    return out;
}

std::optional<Sealed> seal_secret(const std::string& plain) {
    DATA_BLOB in{DWORD(plain.size()), reinterpret_cast<BYTE*>(const_cast<char*>(plain.data()))};
    DATA_BLOB entropy{DWORD(sizeof kEntropy - 1), reinterpret_cast<BYTE*>(const_cast<char*>(kEntropy))};
    DATA_BLOB out{};
    if (!CryptProtectData(&in, L"Clew audio server secret", &entropy, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN,
                          &out))
        return std::nullopt;
    std::string blob(reinterpret_cast<char*>(out.pbData), out.cbData);
    LocalFree(out.pbData);
    return Sealed{"dpapi", base64_encode(blob)};
}

std::optional<std::string> open_secret(const Sealed& sealed) {
    if (sealed.kind != "dpapi") return std::nullopt;
    auto blob = base64_decode(sealed.data);
    if (!blob || blob->empty()) return std::nullopt;
    DATA_BLOB in{DWORD(blob->size()), reinterpret_cast<BYTE*>(blob->data())};
    DATA_BLOB entropy{DWORD(sizeof kEntropy - 1), reinterpret_cast<BYTE*>(const_cast<char*>(kEntropy))};
    DATA_BLOB out{};
    if (!CryptUnprotectData(&in, nullptr, &entropy, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out))
        return std::nullopt;
    std::string plain(reinterpret_cast<char*>(out.pbData), out.cbData);
    SecureZeroMemory(out.pbData, out.cbData);
    LocalFree(out.pbData);
    return plain;
}

fs::path config_dir() { return known_folder(FOLDERID_RoamingAppData) / "clew-audio-server"; }


fs::path self_path() {
    std::wstring buf(MAX_PATH, L'\0');
    while (true) {
        DWORD n = GetModuleFileNameW(nullptr, buf.data(), DWORD(buf.size()));
        if (n < buf.size()) {
            buf.resize(n);
            return buf;
        }
        buf.resize(buf.size() * 2);
    }
}

bool can_convert() { return win::media_foundation_present(); }

bool convert_to_mp3(const fs::path& in, const fs::path& out, const ConvertControl& control, std::string* why) {
    std::wstring app = self_path().wstring();
    std::wstring cmd = win::quote_arg(app) + L" --convert-child " + win::quote_arg(in.wstring());
    return run_sandboxed(app, cmd, out, control, why);
}

bool ffmpeg_to_mp3(const fs::path& ffmpeg, const fs::path& in, const fs::path& out, const ConvertControl& control,
                   std::string* why) {
    std::error_code ec;
    if (!fs::is_regular_file(ffmpeg, ec)) {
        if (why) *why = "not a file: " + ffmpeg.u8string();
        return false;
    }
    std::wstring app = ffmpeg.wstring();
    std::wstring cmd = win::quote_arg(app) +
                       L" -nostdin -hide_banner -loglevel error -i " + win::quote_arg(in.wstring()) +
                       L" -map 0:a:0 -map_metadata -1 -c:a libmp3lame -b:a 128k -write_xing 0 -id3v2_version 0"
                       L" -f mp3 pipe:1";
    return run_sandboxed(app, cmd, out, control, why);
}

namespace {

// The dictation server's job: ends with this process (the job's handle closes with it), a memory
// cap, no windows or clipboard. Several processes are allowed: a Python program may start helpers.
HANDLE helper_job(std::uint64_t memory_limit) {
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (!job) return nullptr;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim{};
    lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                           JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |
                                           (memory_limit ? JOB_OBJECT_LIMIT_PROCESS_MEMORY : 0);
    lim.ProcessMemoryLimit = SIZE_T(memory_limit);
    JOBOBJECT_BASIC_UI_RESTRICTIONS ui{};
    ui.UIRestrictionsClass = JOB_OBJECT_UILIMIT_DESKTOP | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS |
                             JOB_OBJECT_UILIMIT_EXITWINDOWS | JOB_OBJECT_UILIMIT_GLOBALATOMS |
                             JOB_OBJECT_UILIMIT_HANDLES | JOB_OBJECT_UILIMIT_READCLIPBOARD |
                             JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS | JOB_OBJECT_UILIMIT_WRITECLIPBOARD;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &lim, sizeof lim) ||
        !SetInformationJobObject(job, JobObjectBasicUIRestrictions, &ui, sizeof ui)) {
        CloseHandle(job);
        return nullptr;
    }
    return job;
}

class WinChild final : public Child {
public:
    WinChild(HANDLE job, HANDLE process, HANDLE output, std::function<void(const std::string&)> on_line,
             std::function<void(int)> on_exit)
        : job_(job), process_(process), output_(output) {
        reader_ = std::thread([this, on_line = std::move(on_line), on_exit = std::move(on_exit)] {
            std::string pending;
            char buf[4096];
            DWORD n = 0;
            while (ReadFile(output_.h, buf, sizeof buf, &n, nullptr) && n > 0) {
                pending.append(buf, n);
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
            WaitForSingleObject(process_.h, INFINITE);
            DWORD code = 1;
            GetExitCodeProcess(process_.h, &code);
            if (on_exit) on_exit(int(code));
        });
    }

    ~WinChild() override {
        stop();
        if (reader_.joinable()) reader_.join();
    }

    void stop() override { TerminateJobObject(job_.h, 1); }

private:
    Handle job_, process_, output_;
    std::thread reader_;
};

}  // namespace

std::unique_ptr<Child> start_child(const fs::path& exe, const std::vector<std::string>& args,
                                   const std::string& input, std::uint64_t memory_limit,
                                   std::function<void(const std::string&)> on_line, std::function<void(int)> on_exit,
                                   std::string* why) {
    SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
    HANDLE in_r = nullptr, in_w = nullptr, out_r = nullptr, out_w = nullptr;
    if (!CreatePipe(&in_r, &in_w, &sa, 0) || !CreatePipe(&out_r, &out_w, &sa, 1 << 16)) {
        if (in_r) CloseHandle(in_r);
        if (in_w) CloseHandle(in_w);
        if (why) *why = "no pipe";
        return nullptr;
    }
    Handle ir(in_r), iw(in_w), orr(out_r), ow(out_w);
    SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);

    Handle job(helper_job(memory_limit));
    if (!job.h) {
        if (why) *why = "no job object";
        return nullptr;
    }
    StartAttributes attrs;
    if (!attrs.init(in_r, out_w, job.h)) {
        if (why) *why = "no attribute list";
        return nullptr;
    }
    STARTUPINFOEXW si{};
    si.StartupInfo.cb = sizeof si;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = in_r;
    si.StartupInfo.hStdOutput = out_w;
    si.StartupInfo.hStdError = out_w;
    si.lpAttributeList = attrs.get();
    std::wstring app = exe.wstring();
    std::wstring cmd = win::quote_arg(app);
    for (const auto& a : args) cmd += L" " + win::quote_arg(win::widen(a));
    // Python writes its lines at once rather than when a buffer fills.
    SetEnvironmentVariableW(L"PYTHONUNBUFFERED", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    PROCESS_INFORMATION pi{};
    BOOL started = CreateProcessW(app.c_str(), cmd.data(), nullptr, nullptr, TRUE,
                                  EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW, nullptr,
                                  exe.parent_path().c_str(), &si.StartupInfo, &pi);
    if (!started) {
        if (why) *why = "could not start " + path_to_utf8_win(exe) + " (" + std::to_string(GetLastError()) + ")";
        return nullptr;
    }
    Handle thread(pi.hThread);
    if (!AssignProcessToJobObject(job.h, pi.hProcess)) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hProcess);
        if (why) *why = "could not put it in a job";
        return nullptr;
    }
    ResumeThread(pi.hThread);
    ir.reset();
    ow.reset();
    DWORD written = 0;
    if (!input.empty()) WriteFile(iw.h, input.data(), DWORD(input.size()), &written, nullptr);
    iw.reset();
    HANDLE job_h = job.h, out_h = orr.h;
    job.h = nullptr;
    orr.h = nullptr;
    return std::make_unique<WinChild>(job_h, pi.hProcess, out_h, std::move(on_line), std::move(on_exit));
}

fs::path find_stt_server() {
    wchar_t forced[MAX_PATH * 2];
    DWORD n = GetEnvironmentVariableW(L"CLEW_STT_SERVER_EXE", forced, DWORD(std::size(forced)));
    if (n > 0 && n < std::size(forced)) return fs::path(forced);
    std::error_code ec;
    auto beside = self_path().parent_path() / "clew-stt-server" / "clew-stt-server.exe";
    return fs::is_regular_file(beside, ec) ? beside : fs::path();
}

fs::path default_stt_models_dir() { return known_folder(FOLDERID_LocalAppData) / "clew-stt" / "models"; }

std::uint64_t total_memory() {
    MEMORYSTATUSEX m{};
    m.dwLength = sizeof m;
    return GlobalMemoryStatusEx(&m) ? m.ullTotalPhys : 0;
}

fs::path find_ffmpeg() {
    auto beside = self_path().parent_path() / "ffmpeg.exe";
    std::error_code ec;
    if (fs::is_regular_file(beside, ec)) return beside;
    wchar_t buf[MAX_PATH];
    DWORD n = SearchPathW(nullptr, L"ffmpeg.exe", nullptr, MAX_PATH, buf, nullptr);
    if (n > 0 && n < MAX_PATH) return fs::path(buf);
    return {};
}

}  // namespace clew::platform
