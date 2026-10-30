#include "update_win.h"

#include <windows.h>
#include <shlobj.h>
#include <softpub.h>
#include <wincrypt.h>
#include <winhttp.h>
#include <wintrust.h>

#include <vector>

#include "../core/files.h"
#include "../core/platform.h"
#include "../core/sha256.h"
#include "../core/text.h"
#include "util.h"
#include "version.h"

namespace clew::win::updater {

namespace {

constexpr std::size_t kManifestMax = 64 * 1024;

struct Net {
    HINTERNET h = nullptr;
    explicit Net(HINTERNET handle) : h(handle) {}
    Net(const Net&) = delete;
    Net& operator=(const Net&) = delete;
    ~Net() {
        if (h) WinHttpCloseHandle(h);
    }
};

std::string hex(const Digest& d) {
    static const char* digits = "0123456789abcdef";
    std::string s;
    for (auto b : d) {
        s += digits[b >> 4];
        s += digits[b & 15];
    }
    return s;
}

std::string net_error(DWORD e) {
    switch (e) {
        case ERROR_WINHTTP_NAME_NOT_RESOLVED:
        case ERROR_WINHTTP_CANNOT_CONNECT:
        case ERROR_WINHTTP_CONNECTION_ERROR:
            return "no connection to github.com";
        case ERROR_WINHTTP_TIMEOUT: return "github.com did not answer in time";
        case ERROR_WINHTTP_SECURE_FAILURE: return "the secure connection to github.com could not be checked";
        default: return "network error " + std::to_string(e);
    }
}

// CLEW_UPDATE_BASE, for the tests only: a server on this computer stands in for
// https://github.com/yaqalab/clew-tools. Anything else in it is ignored.
std::string rebase(const std::string& url) {
    wchar_t buf[128];
    DWORD n = GetEnvironmentVariableW(L"CLEW_UPDATE_BASE", buf, 128);
    if (n == 0 || n >= 128) return url;
    std::string base = narrow(buf);
    std::string prefix = std::string("https://github.com/") + update::kRepository;
    if (!starts_with(base, "http://127.0.0.1:") || !starts_with(url, prefix)) return url;
    return base + url.substr(prefix.size());
}

// One GET. The body goes to `sink` piece by piece; it returns false to stop. HTTPS only (or the
// tests' loopback server); redirects are followed, never from HTTPS down to HTTP; no cookies and
// no Windows credentials are ever sent.
bool get(const std::string& address, const std::function<bool(const char*, DWORD)>& sink, std::string* why) {
    std::wstring url = widen(rebase(address));
    wchar_t host[256] = {}, path[2048] = {};
    URL_COMPONENTS uc{};
    uc.dwStructSize = sizeof uc;
    uc.lpszHostName = host;
    uc.dwHostNameLength = DWORD(std::size(host));
    uc.lpszUrlPath = path;
    uc.dwUrlPathLength = DWORD(std::size(path));
    if (!WinHttpCrackUrl(url.c_str(), 0, 0, &uc)) {
        *why = "bad address";
        return false;
    }
    bool https = uc.nScheme == INTERNET_SCHEME_HTTPS;
    if (!https && !(uc.nScheme == INTERNET_SCHEME_HTTP && wcscmp(host, L"127.0.0.1") == 0)) {
        *why = "refused: not HTTPS";
        return false;
    }
    std::wstring agent = L"ClewAudioServer/" + widen(CLEW_VERSION);
    Net session(WinHttpOpen(agent.c_str(), WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME,
                            WINHTTP_NO_PROXY_BYPASS, 0));
    if (!session.h) {
        *why = net_error(GetLastError());
        return false;
    }
    DWORD protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 | WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
    if (!WinHttpSetOption(session.h, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof protocols)) {
        protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2;  // Windows 10 before TLS 1.3
        WinHttpSetOption(session.h, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof protocols);
    }
    WinHttpSetTimeouts(session.h, 10000, 15000, 30000, 30000);
    Net connection(WinHttpConnect(session.h, host, uc.nPort, 0));
    Net request(connection.h ? WinHttpOpenRequest(connection.h, L"GET", path, nullptr, WINHTTP_NO_REFERER,
                                                  WINHTTP_DEFAULT_ACCEPT_TYPES, https ? WINHTTP_FLAG_SECURE : 0)
                             : nullptr);
    if (!request.h) {
        *why = net_error(GetLastError());
        return false;
    }
    DWORD off = WINHTTP_DISABLE_COOKIES;
    WinHttpSetOption(request.h, WINHTTP_OPTION_DISABLE_FEATURE, &off, sizeof off);
    DWORD logon = WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH;
    WinHttpSetOption(request.h, WINHTTP_OPTION_AUTOLOGON_POLICY, &logon, sizeof logon);
    if (!WinHttpSendRequest(request.h, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(request.h, nullptr)) {
        *why = net_error(GetLastError());
        return false;
    }
    DWORD status = 0, size = sizeof status;
    WinHttpQueryHeaders(request.h, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX,
                        &status, &size, WINHTTP_NO_HEADER_INDEX);
    if (status == 404) {
        *why = "no release was found";
        return false;
    }
    if (status != 200) {
        *why = "github.com answered " + std::to_string(status);
        return false;
    }
    std::vector<char> buf(64 * 1024);
    for (;;) {
        DWORD n = 0;
        if (!WinHttpReadData(request.h, buf.data(), DWORD(buf.size()), &n)) {
            *why = net_error(GetLastError());
            return false;
        }
        if (n == 0) return true;
        if (!sink(buf.data(), n)) return false;
    }
}

std::filesystem::path updates_dir() {
    PWSTR p = nullptr;
    std::filesystem::path dir;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_CREATE, nullptr, &p))) dir = p;
    CoTaskMemFree(p);
    return dir.empty() ? dir : dir / "clew-audio-server" / "updates";
}

// The SHA-256 of the certificate that signed `file` (its first signer), or nothing when unsigned.
std::optional<Digest> signer_of(const std::wstring& file) {
    HCERTSTORE store = nullptr;
    HCRYPTMSG msg = nullptr;
    DWORD encoding = 0, content = 0, format = 0;
    if (!CryptQueryObject(CERT_QUERY_OBJECT_FILE, file.c_str(), CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
                          CERT_QUERY_FORMAT_FLAG_BINARY, 0, &encoding, &content, &format, &store, &msg, nullptr))
        return std::nullopt;
    std::optional<Digest> out;
    DWORD size = 0;
    if (CryptMsgGetParam(msg, CMSG_SIGNER_INFO_PARAM, 0, nullptr, &size) && size > 0) {
        std::vector<BYTE> buf(size);
        if (CryptMsgGetParam(msg, CMSG_SIGNER_INFO_PARAM, 0, buf.data(), &size)) {
            auto* info = reinterpret_cast<CMSG_SIGNER_INFO*>(buf.data());
            CERT_INFO wanted{};
            wanted.Issuer = info->Issuer;
            wanted.SerialNumber = info->SerialNumber;
            PCCERT_CONTEXT cert = CertFindCertificateInStore(store, X509_ASN_ENCODING | PKCS_7_ASN_ENCODING, 0,
                                                             CERT_FIND_SUBJECT_CERT, &wanted, nullptr);
            if (cert) {
                Sha256 h;
                h.update(cert->pbCertEncoded, cert->cbCertEncoded);
                out = h.finish();
                CertFreeCertificateContext(cert);
            }
        }
    }
    CryptMsgClose(msg);
    CertCloseStore(store, 0);
    return out;
}

// Whether the file's Authenticode signature is whole: the file unchanged since it was signed. The
// YaqaLab certificate is self-signed, so Windows does not trust its root (CERT_E_UNTRUSTEDROOT)
// unless the user chose to; who signed is checked by signer_of() against this program's own
// signer instead. Nothing is fetched from the network (no revocation lists).
bool signature_intact(const std::wstring& file, HANDLE handle) {
    WINTRUST_FILE_INFO fi{};
    fi.cbStruct = sizeof fi;
    fi.pcwszFilePath = file.c_str();
    fi.hFile = handle == INVALID_HANDLE_VALUE ? nullptr : handle;
    WINTRUST_DATA wd{};
    wd.cbStruct = sizeof wd;
    wd.dwUIChoice = WTD_UI_NONE;
    wd.fdwRevocationChecks = WTD_REVOKE_NONE;
    wd.dwUnionChoice = WTD_CHOICE_FILE;
    wd.pFile = &fi;
    wd.dwStateAction = WTD_STATEACTION_VERIFY;
    wd.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE;
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    LONG result = WinVerifyTrust(static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &wd);
    wd.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &wd);
    return result == ERROR_SUCCESS || result == CERT_E_UNTRUSTEDROOT;
}

void remove_old(const std::filesystem::path& dir) {
    std::error_code ec;
    for (const auto& e : std::filesystem::directory_iterator(dir, ec)) {
        std::string name = path_to_utf8(e.path().filename());
        if (starts_with(name, "clew-audio-server-setup-")) std::filesystem::remove(e.path(), ec);
    }
}

}  // namespace

std::optional<update::Release> fetch_latest(std::string* why) {
    std::string body;
    bool ok = get(update::manifest_url(), [&](const char* p, DWORD n) {
        if (body.size() + n > kManifestMax) return false;
        body.append(p, n);
        return true;
    }, why);
    if (!ok) {
        if (why->empty()) *why = "the release's description is too large";
        return std::nullopt;
    }
    auto r = update::parse_manifest(body);
    if (!r) *why = "the release's description cannot be read";
    return r;
}

std::optional<Installer> download(const update::Release& r, const std::function<void(int)>& progress, std::string* why) {
    auto dir = updates_dir();
    std::error_code ec;
    if (dir.empty() || (std::filesystem::create_directories(dir, ec), ec)) {
        *why = "no folder to download into";
        return std::nullopt;
    }
    remove_old(dir);
    auto final_path = dir / widen(r.installer);
    auto part = final_path;
    part += ".part";
    HANDLE out = CreateFileW(part.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (out == INVALID_HANDLE_VALUE) {
        *why = "the download cannot be written";
        return std::nullopt;
    }
    Sha256 hash;
    std::uint64_t got = 0;
    int shown = -1;
    bool written = true, too_large = false;
    bool ok = get(update::installer_url(r), [&](const char* p, DWORD n) {
        if (got + n > r.size) {
            too_large = true;  // larger than the release says: stop at once
            return false;
        }
        DWORD done = 0;
        if (!WriteFile(out, p, n, &done, nullptr) || done != n) {
            written = false;
            return false;
        }
        hash.update(p, n);
        got += n;
        int percent = int(got * 100 / r.size);
        if (percent != shown) progress(shown = percent);
        return true;
    }, why);
    CloseHandle(out);
    auto fail = [&](const std::string& reason) -> std::optional<Installer> {
        if (!reason.empty()) *why = reason;
        std::filesystem::remove(part, ec);
        std::filesystem::remove(final_path, ec);
        return std::nullopt;
    };
    if (!written) return fail("the download cannot be written (is the disk full?)");
    if (too_large) return fail("the installer is larger than the release says");
    if (!ok) return fail("");
    if (got != r.size) return fail("the download is incomplete");
    if (hex(hash.finish()) != r.sha256) return fail("the installer's fingerprint does not match the release");
    if (!MoveFileExW(part.c_str(), final_path.c_str(), MOVEFILE_REPLACE_EXISTING)) return fail("the download cannot be kept");

    // Held open, shared for reading only, from the checks until the installer has started.
    Installer in;
    in.path = final_path.wstring();
    in.file = CreateFileW(in.path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                          nullptr);
    if (in.file == INVALID_HANDLE_VALUE) return fail("the download cannot be opened");
    auto mine = signer_of(platform::self_path().wstring());
    auto theirs = signer_of(in.path);
    if (!signature_intact(in.path, in.file) || !theirs) {
        close(in);
        return fail("the installer's signature is missing or broken");
    }
    if (!mine || *mine != *theirs) {
        close(in);
        return fail("the installer is not signed by the same publisher as this program");
    }
    return in;
}

bool self_signed_by_known_signer() {
    auto self = platform::self_path().wstring();
    return signer_of(self).has_value() && signature_intact(self, INVALID_HANDLE_VALUE);
}

bool start(const Installer& in, std::string* why) {
    // /update=1 tells the installer to start the new version when it is done (clew-audio-server.iss).
    auto log = std::filesystem::path(in.path).parent_path() / "update-log.txt";
    std::wstring cmd = quote_arg(in.path) + L" /SILENT /SUPPRESSMSGBOXES /NORESTART /update=1 /LOG=" +
                       quote_arg(log.wstring());
    STARTUPINFOW si{};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(in.path.c_str(), cmd.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi)) {
        *why = "the installer could not start (error " + std::to_string(GetLastError()) + ")";
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

void close(Installer& in) {
    if (in.file != INVALID_HANDLE_VALUE) CloseHandle(in.file);
    in.file = INVALID_HANDLE_VALUE;
}

}  // namespace clew::win::updater
