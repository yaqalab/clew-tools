// The tray icon and the settings window. The icon is Clew's "c" mark, ink on a light taskbar and cream
// on a dark one: as it is when Clew can reach the files, faded when stopped or something needs the
// user, with a badge while converting. One click opens the settings; the right-click
// menu has the everyday actions. Closing the window keeps the server running; "Quit" stops it.
// The window has three tabs: "Audio files", "Dictation" (docs/28: the speech server on this
// computer, a child process) and "Updates" (docs/32). The pages are in one dialog, shown and hidden
// by the tab strip.
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <windowsx.h>

#include <atomic>
#include <ctime>
#include <iterator>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "../../res/resource.h"
#include "../core/files.h"
#include "../core/folders.h"
#include "../core/platform.h"
#include "../core/service.h"
#include "../core/settings.h"
#include "../core/text.h"
#include "../core/update.h"
#include "app.h"
#include "strings.h"
#include "update_win.h"
#include "util.h"
#include "version.h"

namespace clew::win {

namespace {

constexpr UINT WM_APP_TRAY = WM_APP + 1;
constexpr UINT WM_APP_STATUS = WM_APP + 2;
constexpr UINT WM_APP_SHOW = WM_APP + 3;
constexpr UINT WM_APP_TOGGLE_FOLDER = WM_APP + 4;  // wParam: the row whose tick box changed
constexpr UINT WM_APP_UPDATE_CHECKED = WM_APP + 5;     // lParam: a CheckResult to delete
constexpr UINT WM_APP_UPDATE_PROGRESS = WM_APP + 6;    // wParam: percent
constexpr UINT WM_APP_UPDATE_DOWNLOADED = WM_APP + 7;  // lParam: a DownloadResult to delete
constexpr UINT_PTR kRefreshTimer = 1;      // on the settings window
constexpr UINT_PTR kUpdateFirstTimer = 2;  // on the tray window: the first check, a little after start
constexpr UINT_PTR kUpdateTimer = 3;       // on the tray window: hourly, checks when a day has passed
const wchar_t kWindowClass[] = L"ClewAudioServerTray";
const wchar_t kMutex[] = L"Local\\ClewAudioServer.SingleInstance";
const wchar_t kRunKey[] = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const wchar_t kRunValue[] = L"ClewAudioServer";

enum MenuId {
    ID_OPEN = 1,
    ID_COPY_SECRET,
    ID_COPY_ADDRESS,
    ID_OPEN_FOLDER,
    ID_STARTSTOP,
    ID_DICTATION,
    ID_COPY_STT_KEY,
    ID_AUTOSTART,
    ID_UPDATE,
    ID_QUIT,
    ID_FOLDER_TOGGLE = 100,  // + the folder's index
    ID_FOLDER_OPEN = 300,    // + the folder's index
};
constexpr int kMaxMenuFolders = 100;

const int kAudioPage[] = {IDC_STATUS,    IDC_STARTSTOP,   IDC_G_FOLDER,  IDC_FOLDER,    IDC_BROWSE,   IDC_REMOVEFOLDER, IDC_OPENFOLDER,
                          IDC_G_CONN,    IDC_L_PORT,      IDC_PORT,      IDC_L_HOURS,   IDC_HOURS,    IDC_L_ORIGINS,
                          IDC_ORIGINS,   IDC_G_SECRET,    IDC_SECRET,    IDC_SHOW,      IDC_COPY,     IDC_NEWSECRET,
                          IDC_L_SECRET,  IDC_G_CONVERT,   IDC_CONVERT,   IDC_PREPARE,   IDC_L_FFMPEG, IDC_FFMPEG,
                          IDC_FFBROWSE,  IDC_FFFIND,      IDC_L_FFHINT,  IDC_L_CACHE,   IDC_CACHEMB,  IDC_CACHEUSED,
                          IDC_CLEARCACHE};
const int kDictationPage[] = {IDC_STT_STATUS,   IDC_STT_STARTSTOP, IDC_G_STT_MODEL,   IDC_STT_MODELS,  IDC_STT_DOWNLOAD,
                              IDC_STT_MODEL_NOTE, IDC_G_STT_FOLDER, IDC_STT_FOLDER,   IDC_STT_CHANGE,
                              IDC_STT_OPENFOLDER, IDC_STT_FREE,    IDC_G_STT_CONN,    IDC_L_STT_LANG,  IDC_STT_LANG,
                              IDC_L_STT_PORT,   IDC_STT_PORT,      IDC_G_STT_KEY,     IDC_STT_KEY,     IDC_STT_SHOWKEY,
                              IDC_STT_COPYKEY,  IDC_STT_NEWKEY,    IDC_L_STT_KEY,     IDC_STT_AUTOSTART, IDC_L_STT_LOCAL};
const int kUpdatesPage[] = {IDC_UPD_VERSION, IDC_G_UPD,       IDC_UPD_AUTO,   IDC_UPD_STATUS, IDC_UPD_CHECK,
                            IDC_UPD_INSTALL, IDC_UPD_NOTES,   IDC_L_UPD_KEPT, IDC_G_UPD_HOW,  IDC_L_UPD_HOW};
// The language list, in the combo box's order. Whisper knows many more; these are the ones Clew
// offers (docs/28 §3.5).
const char* const kLanguages[] = {"", "en", "ja", "fr", "mg"};

enum class Upd { Idle, Checking, Current, Available, Downloading, Failed };

struct App {
    HINSTANCE inst = nullptr;
    HANDLE mutex = nullptr;  // one server per Windows session; let go just before an update installs
    HWND hwnd = nullptr;
    HWND dlg = nullptr;
    Service* svc = nullptr;
    UINT taskbar_created = 0;
    std::atomic<bool> status_pending{false};
    bool secret_visible = false;
    bool stt_key_visible = false;
    int tab = 0;
    int stt_row = 0;          // the model row chosen in the list (kSttModels' order)
    std::wstring stt_note;    // why a grey row cannot be chosen, after a press on it
    bool stopped_by_user = false;
    bool filling_folders = false;  // the folder list is being drawn: its tick boxes are not the user's
    // A fixed identity for the tray icon: Windows then keeps the user's "show in the taskbar" choice
    // across updates. Windows ties it to the signed program; when it refuses, the icon goes by id.
    bool use_guid = true;
    // New versions (docs/32). Only the window's thread touches these; workers post their results.
    update::State upd;
    Upd upd_state = Upd::Idle;
    std::optional<update::Release> upd_release;  // a newer one than this, once found
    std::string upd_problem;
    int upd_percent = 0;
} g;

// {9C2E6B1A-4F7D-4E3B-8A1C-5D0F2B7E9A43}
const GUID kTrayGuid = {0x9c2e6b1a, 0x4f7d, 0x4e3b, {0x8a, 0x1c, 0x5d, 0x0f, 0x2b, 0x7e, 0x9a, 0x43}};

void identify(NOTIFYICONDATAW& nid) {
    nid.cbSize = sizeof nid;
    nid.hWnd = g.hwnd;
    nid.uID = 1;
    if (g.use_guid) {
        nid.uFlags |= NIF_GUID;
        nid.guidItem = kTrayGuid;
    }
}

// ---------------------------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------------------------

bool autostart_on() {
    wchar_t buf[MAX_PATH * 2];
    DWORD size = sizeof buf;
    return RegGetValueW(HKEY_CURRENT_USER, kRunKey, kRunValue, RRF_RT_REG_SZ, nullptr, buf, &size) == ERROR_SUCCESS;
}

void set_autostart(bool on) {
    if (on) {
        std::wstring cmd = quote_arg(platform::self_path().wstring());
        RegSetKeyValueW(HKEY_CURRENT_USER, kRunKey, kRunValue, REG_SZ, cmd.c_str(),
                        DWORD((cmd.size() + 1) * sizeof(wchar_t)));
    } else {
        RegDeleteKeyValueW(HKEY_CURRENT_USER, kRunKey, kRunValue);
    }
}

// The secret stays out of Windows' clipboard history and cloud clipboard.
void copy_text(HWND owner, const std::wstring& text, bool sensitive) {
    if (!OpenClipboard(owner)) return;
    EmptyClipboard();
    HGLOBAL mem = GlobalAlloc(GMEM_MOVEABLE, (text.size() + 1) * sizeof(wchar_t));
    if (mem) {
        memcpy(GlobalLock(mem), text.c_str(), (text.size() + 1) * sizeof(wchar_t));
        GlobalUnlock(mem);
        if (!SetClipboardData(CF_UNICODETEXT, mem)) GlobalFree(mem);
    }
    if (sensitive) {
        UINT exclude = RegisterClipboardFormatW(L"ExcludeClipboardContentFromMonitorProcessing");
        UINT history = RegisterClipboardFormatW(L"CanIncludeInClipboardHistory");
        UINT cloud = RegisterClipboardFormatW(L"CanUploadToCloudClipboard");
        for (UINT fmt : {history, cloud}) {
            HGLOBAL zero = GlobalAlloc(GMEM_MOVEABLE, sizeof(DWORD));
            if (!zero) continue;
            *static_cast<DWORD*>(GlobalLock(zero)) = 0;
            GlobalUnlock(zero);
            if (!SetClipboardData(fmt, zero)) GlobalFree(zero);
        }
        HGLOBAL empty = GlobalAlloc(GMEM_MOVEABLE, 1);
        if (empty && !SetClipboardData(exclude, empty)) GlobalFree(empty);
    }
    CloseClipboard();
}

std::wstring problem_text(const std::string& p, int port) {
    if (p == "no folder chosen") return tr(S::ProblemNoFolder);
    if (starts_with(p, "port ") && p.find("in use") != std::string::npos) return trf(S::ProblemPortInUse, port);
    const std::string gone = "the folder cannot be opened: ";
    if (starts_with(p, gone)) return trf(S::ProblemFolderGone, widen(p.substr(gone.size())).c_str());
    if (p == "every folder is off") return tr(S::ProblemAllOff);
    return widen(p);
}

std::wstring human_size(std::uint64_t bytes) {
    wchar_t buf[64];
    if (bytes >= 1024ull * 1024 * 1024)
        swprintf_s(buf, L"%.1f GB", double(bytes) / (1024.0 * 1024 * 1024));
    else
        swprintf_s(buf, L"%.0f MB", double(bytes) / (1024.0 * 1024));
    return buf;
}

std::wstring window_text(HWND dlg, int id) {
    HWND h = GetDlgItem(dlg, id);
    int n = GetWindowTextLengthW(h);
    std::wstring s(std::size_t(n) + 1, L'\0');
    GetWindowTextW(h, s.data(), n + 1);
    s.resize(std::size_t(n));
    return s;
}

std::wstring pick(HWND owner, bool folder, const std::wstring& start, const wchar_t* title = nullptr) {
    IFileOpenDialog* d = nullptr;
    if (FAILED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&d)))) return {};
    DWORD opts = 0;
    d->GetOptions(&opts);
    d->SetOptions(opts | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | (folder ? FOS_PICKFOLDERS : FOS_FILEMUSTEXIST));
    d->SetTitle(title ? title : tr(folder ? S::ChooseFolder : S::ChooseFfmpeg));
    if (!folder) {
        COMDLG_FILTERSPEC spec{L"ffmpeg.exe", L"ffmpeg.exe;*.exe"};
        d->SetFileTypes(1, &spec);
    }
    if (!start.empty()) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(SHCreateItemFromParsingName(start.c_str(), nullptr, IID_PPV_ARGS(&item)))) {
            d->SetFolder(item);
            item->Release();
        }
    }
    std::wstring out;
    if (SUCCEEDED(d->Show(owner))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(d->GetResult(&item))) {
            PWSTR p = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &p))) {
                out = p;
                CoTaskMemFree(p);
            }
            item->Release();
        }
    }
    d->Release();
    return out;
}

void open_folder(const std::string& path) {
    std::wstring folder = widen(path);
    std::error_code ec;
    if (!folder.empty() && std::filesystem::is_directory(folder, ec))
        ShellExecuteW(nullptr, L"open", folder.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void fill_folders(HWND dlg);
void message(HWND dlg, const std::wstring& text);
void refresh_status(HWND dlg);
void tray(DWORD message);

// A change to the folders takes effect at once, from the window or the tray's menu (docs/22 section
// 11): switching between projects should not wait for Save. Only the folders change; anything else
// edited in the window and not saved stays as it is.
std::string set_folders(std::vector<FolderSetting> folders) {
    Settings s = g.svc->settings();
    s.folders = std::move(folders);
    g.stopped_by_user = false;
    std::string problem = g.svc->apply(s);
    if (g.dlg) {
        fill_folders(g.dlg);
        message(g.dlg, problem.empty() ? std::wstring(tr(S::Saved)) : problem_text(problem, s.port));
        refresh_status(g.dlg);
    }
    tray(NIM_MODIFY);
    return problem;
}

void toggle_folder(std::size_t i) {
    auto folders = g.svc->settings().folders;
    if (i >= folders.size()) return;
    folders[i].enabled = !folders[i].enabled;
    set_folders(folders);
}

// The space left on the drive of `p`, which need not exist yet. 0 when unknown.
std::uint64_t free_space(std::filesystem::path p) {
    std::error_code ec;
    while (!p.empty() && !std::filesystem::exists(p, ec) && p.has_parent_path() && p.parent_path() != p)
        p = p.parent_path();
    auto space = std::filesystem::space(p, ec);
    return ec ? 0 : space.available;
}

void toggle_dictation() {
    if (g.svc->dictation().wanted())
        g.svc->dictation().stop();
    else
        g.svc->dictation().start();
}

// ---------------------------------------------------------------------------------------------
// Tray icon
// ---------------------------------------------------------------------------------------------

// Whether the taskbar is light (Windows' "default app mode" and "Windows mode" are separate; the
// tray follows the latter).
bool light_taskbar() {
    DWORD v = 0, size = sizeof v;
    return RegGetValueW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                        L"SystemUsesLightTheme", RRF_RT_REG_DWORD, nullptr, &v, &size) == ERROR_SUCCESS &&
           v == 1;
}

void tray(DWORD message) {
    auto st = g.svc->status();
    bool light = light_taskbar();
    int icon = !st.converting.empty()            ? (light ? IDI_TRAY_BUSY : IDI_TRAY_BUSY_DARK)
               : st.running && st.problem.empty() ? (light ? IDI_TRAY_ON : IDI_TRAY_ON_DARK)
                                                  : (light ? IDI_TRAY_OFF : IDI_TRAY_OFF_DARK);
    std::wstring tip = !st.converting.empty()            ? trf(S::TipConverting, widen(st.converting).c_str())
                       : !st.running && g.stopped_by_user ? std::wstring(tr(S::TipStopped))
                       : !st.problem.empty()             ? trf(S::TipProblem, problem_text(st.problem, st.port).c_str())
                                                         : trf(S::TipRunning, st.port);
    NOTIFYICONDATAW nid{};
    nid.uFlags = NIF_ICON | NIF_TIP | NIF_SHOWTIP | NIF_MESSAGE;
    identify(nid);
    nid.uCallbackMessage = WM_APP_TRAY;
    LoadIconMetric(g.inst, MAKEINTRESOURCEW(icon), LIM_SMALL, &nid.hIcon);
    wcsncpy_s(nid.szTip, tip.c_str(), _TRUNCATE);
    if (!Shell_NotifyIconW(message, &nid) && message == NIM_ADD && g.use_guid) {
        // A copy that ended without removing its icon still holds the identity: remove, then retry.
        NOTIFYICONDATAW stale{};
        identify(stale);
        Shell_NotifyIconW(NIM_DELETE, &stale);
        if (!Shell_NotifyIconW(NIM_ADD, &nid)) {
            // Windows ties the identity to another copy of the program: go by id instead.
            g.use_guid = false;
            nid.uFlags &= ~NIF_GUID;
            Shell_NotifyIconW(NIM_ADD, &nid);
        }
    }
    if (message == NIM_ADD) {
        nid.uVersion = NOTIFYICON_VERSION_4;
        Shell_NotifyIconW(NIM_SETVERSION, &nid);
    }
    if (nid.hIcon) DestroyIcon(nid.hIcon);
}

void show_settings();
void show_updates();

void menu(int x, int y) {
    auto st = g.svc->status();
    HMENU m = CreatePopupMenu();
    std::wstring head = st.running ? trf(S::MenuStatusRunning, st.port) : std::wstring(tr(S::MenuStatusStopped));
    AppendMenuW(m, MF_STRING | MF_GRAYED, 0, head.c_str());
    AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(m, MF_STRING, ID_OPEN, tr(S::MenuOpen));
    AppendMenuW(m, MF_STRING, ID_COPY_SECRET, tr(S::MenuCopySecret));
    AppendMenuW(m, MF_STRING | (st.running ? 0 : MF_GRAYED), ID_COPY_ADDRESS, tr(S::MenuCopyAddress));
    // The folders, each ticked when on: the everyday switch between projects.
    auto folders = g.svc->settings().folders;
    std::vector<std::string> paths;
    for (const auto& f : folders) paths.push_back(f.path);
    auto names = folder_names(paths);
    HMENU list = CreatePopupMenu();
    HMENU open = CreatePopupMenu();
    for (std::size_t i = 0; i < folders.size() && i < kMaxMenuFolders; ++i) {
        std::wstring text = widen(names[i]) + L"\t" + widen(folders[i].path);
        AppendMenuW(list, MF_STRING | (folders[i].enabled ? MF_CHECKED : 0), ID_FOLDER_TOGGLE + UINT(i), text.c_str());
        if (folders[i].enabled) AppendMenuW(open, MF_STRING, ID_FOLDER_OPEN + UINT(i), widen(names[i]).c_str());
    }
    AppendMenuW(m, MF_POPUP | (folders.empty() ? MF_GRAYED : 0), UINT_PTR(list), tr(S::MenuFolders));
    if (st.folders.size() == 1) {
        DestroyMenu(open);
        AppendMenuW(m, MF_STRING, ID_OPEN_FOLDER, tr(S::MenuOpenFolder));
    } else {
        AppendMenuW(m, MF_POPUP | (st.folders.empty() ? MF_GRAYED : 0), UINT_PTR(open), tr(S::MenuOpenOneFolder));
    }
    AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(m, MF_STRING, ID_STARTSTOP, tr(st.running ? S::MenuStop : S::MenuStart));
    bool dictating = g.svc->dictation().wanted();
    AppendMenuW(m, MF_STRING, ID_DICTATION, tr(dictating ? S::MenuDictationStop : S::MenuDictationStart));
    AppendMenuW(m, MF_STRING, ID_COPY_STT_KEY, tr(S::MenuCopySttKey));
    AppendMenuW(m, MF_STRING | (autostart_on() ? MF_CHECKED : 0), ID_AUTOSTART, tr(S::MenuAutostart));
    if (g.upd_release && g.upd_state == Upd::Available) {
        AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
        AppendMenuW(m, MF_STRING, ID_UPDATE, trf(S::MenuUpdate, widen(g.upd_release->version).c_str()).c_str());
    }
    AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(m, MF_STRING, ID_QUIT, tr(S::MenuQuit));
    SetMenuDefaultItem(m, ID_OPEN, FALSE);
    SetForegroundWindow(g.hwnd);
    UINT flags = TPM_RETURNCMD | TPM_RIGHTBUTTON | (GetSystemMetrics(SM_MENUDROPALIGNMENT) ? TPM_RIGHTALIGN : 0);
    int cmd = TrackPopupMenuEx(m, flags, x, y, g.hwnd, nullptr);
    PostMessageW(g.hwnd, WM_NULL, 0, 0);
    DestroyMenu(m);
    switch (cmd) {
        case ID_OPEN: show_settings(); break;
        case ID_COPY_SECRET: copy_text(g.hwnd, widen(g.svc->secret()), true); break;
        case ID_COPY_ADDRESS: copy_text(g.hwnd, L"http://127.0.0.1:" + std::to_wstring(st.port), false); break;
        case ID_OPEN_FOLDER:
            if (!st.folders.empty()) open_folder(st.folders.front());
            break;
        case ID_STARTSTOP:
            if (st.running) {
                g.stopped_by_user = true;
                g.svc->stop();
            } else {
                g.stopped_by_user = false;
                g.svc->apply(g.svc->settings(), false);
            }
            break;
        case ID_DICTATION: toggle_dictation(); break;
        case ID_COPY_STT_KEY: copy_text(g.hwnd, widen(g.svc->stt_key()), true); break;
        case ID_AUTOSTART: set_autostart(!autostart_on()); break;
        case ID_UPDATE: show_updates(); break;
        case ID_QUIT: DestroyWindow(g.hwnd); break;
        default:
            if (cmd >= ID_FOLDER_TOGGLE && cmd < ID_FOLDER_TOGGLE + kMaxMenuFolders)
                toggle_folder(std::size_t(cmd - ID_FOLDER_TOGGLE));
            else if (cmd >= ID_FOLDER_OPEN && cmd < ID_FOLDER_OPEN + kMaxMenuFolders &&
                     std::size_t(cmd - ID_FOLDER_OPEN) < folders.size())
                open_folder(folders[std::size_t(cmd - ID_FOLDER_OPEN)].path);
            break;
    }
}

// ---------------------------------------------------------------------------------------------
// Settings window
// ---------------------------------------------------------------------------------------------

void message(HWND dlg, const std::wstring& text) { SetDlgItemTextW(dlg, IDC_MESSAGE, text.c_str()); }

void refresh_cache(HWND dlg) {
    SetDlgItemTextW(dlg, IDC_CACHEUSED, trf(S::CacheUsed, human_size(g.svc->cache().bytes_on_disk()).c_str()).c_str());
}

void refresh_dictation(HWND dlg);

void refresh_status(HWND dlg) {
    auto st = g.svc->status();
    std::wstring text = !st.converting.empty()                ? trf(S::TipConverting, widen(st.converting).c_str())
                        : !st.running && g.stopped_by_user    ? std::wstring(tr(S::StatusStopped))
                        : !st.problem.empty()                 ? trf(S::StatusProblem, problem_text(st.problem, st.port).c_str())
                        : st.running                          ? trf(S::StatusRunning, st.port, st.requests)
                                                              : std::wstring(tr(S::StatusStopped));
    SetDlgItemTextW(dlg, IDC_STATUS, text.c_str());
    SetDlgItemTextW(dlg, IDC_STARTSTOP, tr(st.running ? S::Stop : S::Start));

    HWND list = GetDlgItem(dlg, IDC_ACTIVITY);
    auto lines = g.svc->recent();
    SendMessageW(list, WM_SETREDRAW, FALSE, 0);
    SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const auto& l : lines) SendMessageW(list, LB_ADDSTRING, 0, LPARAM(widen(l).c_str()));
    if (!lines.empty()) SendMessageW(list, LB_SETTOPINDEX, WPARAM(lines.size() - 1), 0);
    SendMessageW(list, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(list, nullptr, TRUE);
    refresh_dictation(dlg);
}

std::wstring stt_problem_text(const std::string& p, int port) {
    if (starts_with(p, "port ") && p.find("in use") != std::string::npos) return trf(S::ProblemPortInUse, port);
    if (starts_with(p, "not enough memory")) return tr(S::SttNoMemory);
    return widen(p);
}

const wchar_t* languages_text(const std::string& code) {
    return code == "en"   ? tr(S::LangEnglish)
           : code == "ja" ? tr(S::LangJapanese)
           : code == "fr" ? tr(S::LangFrench)
           : code == "mg" ? tr(S::LangMalagasy)
                          : tr(S::LangPerSession);
}

// Why this computer cannot run row `i`, in words, or "".
std::wstring model_blocker_text(std::size_t i) {
    const SttModel& m = kSttModels[i];
    std::uint64_t memory = platform::total_memory();
    std::string why = stt_model_blocker(m, memory, g.svc->dictation().status().gpus);
    if (why == "memory") return trf(S::SttNeedsMemory, widen(m.name).c_str(), m.memory_gb, human_size(memory).c_str());
    if (why == "gpu") return trf(S::SttNeedsGpu, widen(m.name).c_str());
    return {};
}

std::wstring model_row_text(std::size_t i) {
    const SttModel& m = kSttModels[i];
    bool here = Dictation::model_present(g.svc->stt_models_dir(), m.name);
    S use = m.use == SttModel::Live ? S::SttUseLive : m.use == SttModel::Files ? S::SttUseFiles : S::SttUseGpu;
    return trf(S::SttModelLine, widen(m.name).c_str(), human_size(m.bytes).c_str(), tr(use),
               tr(here ? S::SttHere : S::SttNotHere)) +
           (m.english_only ? tr(S::SttEnglishOnly) : L"");
}

// The list draws itself: a row this computer cannot run in grey, never highlighted.
void draw_model_row(const DRAWITEMSTRUCT& d) {
    if (d.itemID == UINT(-1)) return;
    bool blocked = !model_blocker_text(d.itemID).empty();
    bool selected = (d.itemState & ODS_SELECTED) && !blocked;
    FillRect(d.hDC, &d.rcItem, GetSysColorBrush(selected ? COLOR_HIGHLIGHT : COLOR_WINDOW));
    SetBkMode(d.hDC, TRANSPARENT);
    SetTextColor(d.hDC, GetSysColor(blocked ? COLOR_GRAYTEXT : selected ? COLOR_HIGHLIGHTTEXT : COLOR_WINDOWTEXT));
    RECT r = d.rcItem;
    r.left += 4;
    std::wstring text = model_row_text(d.itemID);
    DrawTextW(d.hDC, text.c_str(), int(text.size()), &r, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
    if (d.itemState & ODS_FOCUS) DrawFocusRect(d.hDC, &d.rcItem);
}

// The chosen model takes effect at once, like the models folder: it is saved, and a running
// dictation server starts again with it. Waiting for Save made Start run the old model while the
// list showed the new one, and reopening the window put the old one back (found 2026-10-16).
void use_model_row(int row) {
    Settings s = g.svc->settings();
    const char* name = kSttModels[std::size_t(row)].name;
    if (s.stt_model == name) return;
    s.stt_model = name;
    g.svc->apply(s);
}

// The language too takes effect at once, for the same reason as the model.
void use_language(HWND dlg) {
    LRESULT i = SendDlgItemMessageW(dlg, IDC_STT_LANG, CB_GETCURSEL, 0, 0);
    if (i < 0 || i >= LRESULT(std::size(kLanguages))) return;
    Settings s = g.svc->settings();
    if (s.stt_language == kLanguages[i]) return;
    s.stt_language = kLanguages[i];
    g.svc->apply(s);
    refresh_dictation(dlg);
}

// A grey row cannot be chosen. Moving with the arrow keys steps over it to the next one this
// computer can run; a click on it keeps the choice where it was, and the note says why.
void choose_model_row(HWND dlg) {
    HWND list = GetDlgItem(dlg, IDC_STT_MODELS);
    const int count = int(std::size(kSttModels));
    int row = int(SendMessageW(list, LB_GETCURSEL, 0, 0));
    if (row < 0 || row >= count) return;
    std::wstring why = model_blocker_text(std::size_t(row));
    if (why.empty()) {
        g.stt_row = row;
        g.stt_note.clear();
    } else {
        bool clicked = (GetKeyState(VK_LBUTTON) & 0x8000) != 0;
        int step = row > g.stt_row ? 1 : -1;
        int next = row;
        while (!clicked && next >= 0 && next < count && !model_blocker_text(std::size_t(next)).empty()) next += step;
        if (!clicked && next >= 0 && next < count) {
            g.stt_row = next;
            g.stt_note.clear();
        } else {
            g.stt_note = why;
            MessageBeep(MB_ICONWARNING);
        }
        SendMessageW(list, LB_SETCURSEL, WPARAM(g.stt_row), 0);
    }
    use_model_row(g.stt_row);
    refresh_dictation(dlg);
}

void refresh_dictation(HWND dlg) {
    auto st = g.svc->dictation().status();
    Settings s = g.svc->settings();
    std::wstring model = widen(st.model.empty() ? s.stt_model : st.model);
    std::wstring text;
    switch (st.state) {
        case Dictation::State::Off: text = tr(S::SttOff); break;
        case Dictation::State::Missing: text = tr(S::SttMissing); break;
        case Dictation::State::NoModel: text = trf(S::SttNoModel, model.c_str()); break;
        case Dictation::State::Loading: text = trf(S::SttLoading, model.c_str()); break;
        case Dictation::State::Ready: text = trf(S::SttReady, model.c_str(), languages_text(s.stt_language)); break;
        case Dictation::State::Failed:
            text = trf(S::SttFailed, stt_problem_text(st.problem, s.stt_port).c_str());
            break;
    }
    // A download says how far it is on its own button; a failed one says why here, first.
    if (st.downloading.empty() && !st.download_problem.empty())
        text = trf(S::SttDownloadFailed, widen(st.download_problem).c_str()) + L"\n" + text;
    SetDlgItemTextW(dlg, IDC_STT_STATUS, text.c_str());
    bool wanted = g.svc->dictation().wanted();
    SetDlgItemTextW(dlg, IDC_STT_STARTSTOP, tr(wanted ? S::Stop : S::Start));
    EnableWindow(GetDlgItem(dlg, IDC_STT_STARTSTOP), wanted || st.state != Dictation::State::Missing);

    auto dir = g.svc->stt_models_dir();
    bool installed = st.state != Dictation::State::Missing && !platform::find_stt_server().empty();
    InvalidateRect(GetDlgItem(dlg, IDC_STT_MODELS), nullptr, TRUE);
    const SttModel& chosen = kSttModels[std::size_t(g.stt_row)];
    bool here = Dictation::model_present(dir, chosen.name);
    bool speakers_here = Dictation::speaker_model_present(dir);
    EnableWindow(GetDlgItem(dlg, IDC_STT_DOWNLOAD), (!here || !speakers_here) && installed &&
                                                         st.downloading.empty() &&
                                                         model_blocker_text(std::size_t(g.stt_row)).empty());
    // A download says how far it is on the button and in the note.
    SetDlgItemTextW(dlg, IDC_STT_DOWNLOAD,
                    st.downloading.empty() ? tr(S::SttDownload) : (std::to_wstring(st.percent) + L" %").c_str());
    std::wstring note = !st.downloading.empty()
                            ? trf(S::SttDownloading, widen(st.downloading).c_str(), st.percent)
                        : !g.stt_note.empty() ? g.stt_note
                        : !speakers_here      ? trf(S::SttSpeakersMissing, human_size(kSpeakerModelBytes).c_str())
                        : st.gpus > 0         ? std::wstring(tr(S::SttGpuFound))
                                              : std::wstring();
    SetDlgItemTextW(dlg, IDC_STT_MODEL_NOTE, note.c_str());
    std::uint64_t free = free_space(dir);
    SetDlgItemTextW(dlg, IDC_STT_FREE, free ? trf(S::SttFree, human_size(free).c_str()).c_str() : L"");
}

// Fetches what the chosen model needs and is not here: the Whisper model, then the speaker model.
void download_model(HWND dlg, const SttModel& m) {
    auto dir = g.svc->stt_models_dir();
    bool whisper = !Dictation::model_present(dir, m.name);
    bool speakers = !Dictation::speaker_model_present(dir);
    std::uint64_t bytes = (whisper ? m.bytes : 0) + (speakers ? kSpeakerModelBytes : 0);
    if (!bytes) return;
    std::wstring what = whisper && speakers ? widen(m.name) + L" + " + tr(S::SttSpeakerModel)
                        : whisper           ? widen(m.name)
                                            : std::wstring(tr(S::SttSpeakerModel));
    std::uint64_t free = free_space(dir);
    std::uint64_t need = bytes + bytes / 20;
    if (free && free < need) {
        MessageBoxW(dlg, trf(S::SttNoSpace, human_size(need).c_str(), human_size(free).c_str()).c_str(),
                    tr(S::AppName), MB_OK | MB_ICONWARNING);
        return;
    }
    std::wstring ask = trf(S::SttDownloadConfirm, what.c_str(), human_size(bytes).c_str(), dir.wstring().c_str(),
                           free ? human_size(free).c_str() : L"?");
    if (MessageBoxW(dlg, ask.c_str(), tr(S::AppName), MB_OKCANCEL | MB_ICONQUESTION) != IDOK) return;
    if (whisper) g.svc->dictation().download(m.name);
    if (speakers) g.svc->dictation().download("speakers");
    refresh_dictation(dlg);
}

// Whether this copy can install an update itself: only a signed one, whose signer the new installer
// is compared with. Asked once; the answer cannot change while the program runs.
bool can_install() {
    static const bool signed_copy = updater::self_signed_by_known_signer();
    return signed_copy;
}

void refresh_updates(HWND dlg) {
    std::wstring version = g.upd_release ? widen(g.upd_release->version) : std::wstring();
    std::wstring text;
    switch (g.upd_state) {
        case Upd::Idle: text = tr(S::UpdIdle); break;
        case Upd::Checking: text = tr(S::UpdChecking); break;
        case Upd::Current: text = tr(S::UpdCurrent); break;
        case Upd::Available: text = trf(S::UpdAvailable, version.c_str()); break;
        case Upd::Downloading: text = trf(S::UpdDownloading, version.c_str(), g.upd_percent); break;
        case Upd::Failed: text = trf(S::UpdFailed, widen(g.upd_problem).c_str()); break;
    }
    SetDlgItemTextW(dlg, IDC_UPD_STATUS, text.c_str());
    bool busy = g.upd_state == Upd::Checking || g.upd_state == Upd::Downloading;
    EnableWindow(GetDlgItem(dlg, IDC_UPD_CHECK), !busy);
    SetDlgItemTextW(dlg, IDC_UPD_INSTALL,
                    g.upd_release ? trf(S::UpdInstall, version.c_str()).c_str() : tr(S::UpdInstallNone));
    EnableWindow(GetDlgItem(dlg, IDC_UPD_INSTALL), g.upd_release && !busy && can_install());
    SetDlgItemTextW(dlg, IDC_L_UPD_KEPT, tr(can_install() ? S::UpdKept : S::UpdUnsigned));
}

void show_page(HWND dlg, int tab) {
    g.tab = tab;
    for (int id : kAudioPage) ShowWindow(GetDlgItem(dlg, id), tab == 0 ? SW_SHOW : SW_HIDE);
    for (int id : kDictationPage) ShowWindow(GetDlgItem(dlg, id), tab == 1 ? SW_SHOW : SW_HIDE);
    for (int id : kUpdatesPage) ShowWindow(GetDlgItem(dlg, id), tab == 2 ? SW_SHOW : SW_HIDE);
    TabCtrl_SetCurSel(GetDlgItem(dlg, IDC_TABS), tab);
}

void show_stt_key(HWND dlg, bool visible) {
    g.stt_key_visible = visible;
    HWND edit = GetDlgItem(dlg, IDC_STT_KEY);
    SendMessageW(edit, EM_SETPASSWORDCHAR, visible ? 0 : WPARAM(L'●'), 0);
    SetDlgItemTextW(dlg, IDC_STT_SHOWKEY, tr(visible ? S::Hide : S::Show));
    InvalidateRect(edit, nullptr, TRUE);
}

void show_secret(HWND dlg, bool visible) {
    g.secret_visible = visible;
    HWND edit = GetDlgItem(dlg, IDC_SECRET);
    SendMessageW(edit, EM_SETPASSWORDCHAR, visible ? 0 : WPARAM(L'●'), 0);
    SetDlgItemTextW(dlg, IDC_SHOW, tr(visible ? S::Hide : S::Show));
    InvalidateRect(edit, nullptr, TRUE);
}

int selected_folder(HWND dlg) {
    return int(SendDlgItemMessageW(dlg, IDC_FOLDER, LVM_GETNEXTITEM, WPARAM(-1), LVNI_SELECTED));
}

// Remove and Open act on the chosen row.
void enable_folder_buttons(HWND dlg) {
    bool chosen = selected_folder(dlg) >= 0;
    EnableWindow(GetDlgItem(dlg, IDC_REMOVEFOLDER), chosen);
    EnableWindow(GetDlgItem(dlg, IDC_OPENFOLDER), chosen);
}

// The list's two columns and its tick boxes, once.
void init_folder_list(HWND dlg) {
    HWND list = GetDlgItem(dlg, IDC_FOLDER);
    ListView_SetExtendedListViewStyle(list, LVS_EX_CHECKBOXES | LVS_EX_FULLROWSELECT | LVS_EX_DOUBLEBUFFER);
    RECT r{};
    GetClientRect(list, &r);
    int scroll = GetSystemMetrics(SM_CXVSCROLL);
    int name_width = (r.right - scroll) * 2 / 5;
    LVCOLUMNW col{};
    col.mask = LVCF_TEXT | LVCF_WIDTH;
    col.cx = name_width;
    col.pszText = const_cast<LPWSTR>(tr(S::ColFolderName));
    ListView_InsertColumn(list, 0, &col);
    col.cx = r.right - scroll - name_width;
    col.pszText = const_cast<LPWSTR>(tr(S::ColFolderPath));
    ListView_InsertColumn(list, 1, &col);
}

void fill_folders(HWND dlg) {
    HWND list = GetDlgItem(dlg, IDC_FOLDER);
    int keep = selected_folder(dlg);
    auto folders = g.svc->settings().folders;
    std::vector<std::string> paths;
    for (const auto& f : folders) paths.push_back(f.path);
    auto names = folder_names(paths);
    g.filling_folders = true;
    ListView_DeleteAllItems(list);
    for (std::size_t i = 0; i < folders.size(); ++i) {
        std::wstring name = widen(names[i]), path = widen(folders[i].path);
        LVITEMW item{};
        item.mask = LVIF_TEXT;
        item.iItem = int(i);
        item.pszText = name.data();
        ListView_InsertItem(list, &item);
        ListView_SetItemText(list, int(i), 1, path.data());
        ListView_SetCheckState(list, int(i), folders[i].enabled);
    }
    if (keep >= int(folders.size())) keep = int(folders.size()) - 1;
    if (keep >= 0) ListView_SetItemState(list, keep, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED);
    g.filling_folders = false;
    enable_folder_buttons(dlg);
}

void add_folder(HWND dlg) {
    auto folders = g.svc->settings().folders;
    int at = selected_folder(dlg);
    std::wstring start = at >= 0 && at < int(folders.size()) ? widen(folders[std::size_t(at)].path) : L"";
    std::wstring p = pick(dlg, true, start);
    if (p.empty()) return;
    std::error_code ec;
    auto chosen = std::filesystem::weakly_canonical(p, ec);
    for (const auto& f : folders) {
        std::error_code ec2;
        auto known = std::filesystem::weakly_canonical(path_from_utf8(f.path), ec2);
        if (to_lower(path_to_utf8(known)) == to_lower(path_to_utf8(chosen))) {
            message(dlg, tr(S::FolderAlready));
            return;
        }
    }
    folders.push_back({narrow(p), true});
    set_folders(folders);
    HWND list = GetDlgItem(dlg, IDC_FOLDER);
    int last = int(folders.size()) - 1;
    ListView_SetItemState(list, last, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED);
    ListView_EnsureVisible(list, last, FALSE);
}

void remove_folder(HWND dlg) {
    auto folders = g.svc->settings().folders;
    int at = selected_folder(dlg);
    if (at < 0 || at >= int(folders.size())) return;
    std::wstring ask = trf(S::FolderRemoveConfirm, widen(folders[std::size_t(at)].path).c_str());
    if (MessageBoxW(dlg, ask.c_str(), tr(S::AppName), MB_OKCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2) != IDOK) return;
    folders.erase(folders.begin() + at);
    set_folders(folders);
}

void fill(HWND dlg) {
    Settings s = g.svc->settings();
    fill_folders(dlg);
    SetDlgItemInt(dlg, IDC_PORT, UINT(s.port), FALSE);
    SetDlgItemInt(dlg, IDC_HOURS, UINT(s.link_hours), FALSE);
    std::wstring origins;
    for (const auto& o : s.origins) origins += widen(o) + L"\r\n";
    SetDlgItemTextW(dlg, IDC_ORIGINS, origins.c_str());
    SetDlgItemTextW(dlg, IDC_SECRET, widen(g.svc->secret()).c_str());
    CheckDlgButton(dlg, IDC_CONVERT, s.convert ? BST_CHECKED : BST_UNCHECKED);
    CheckDlgButton(dlg, IDC_PREPARE, s.prepare ? BST_CHECKED : BST_UNCHECKED);
    SetDlgItemTextW(dlg, IDC_FFMPEG, widen(s.ffmpeg).c_str());
    SetDlgItemInt(dlg, IDC_CACHEMB, UINT(s.cache_mb), FALSE);
    CheckDlgButton(dlg, IDC_AUTOSTART, autostart_on() ? BST_CHECKED : BST_UNCHECKED);

    for (std::size_t i = 0; i < std::size(kSttModels); ++i)
        if (s.stt_model == kSttModels[i].name) g.stt_row = int(i);
    SendDlgItemMessageW(dlg, IDC_STT_MODELS, LB_SETCURSEL, WPARAM(g.stt_row), 0);
    g.stt_note.clear();
    SetDlgItemTextW(dlg, IDC_STT_FOLDER, g.svc->stt_models_dir().wstring().c_str());
    int lang = 0;
    for (int i = 0; i < int(std::size(kLanguages)); ++i)
        if (s.stt_language == kLanguages[i]) lang = i;
    SendDlgItemMessageW(dlg, IDC_STT_LANG, CB_SETCURSEL, WPARAM(lang), 0);
    SetDlgItemInt(dlg, IDC_STT_PORT, UINT(s.stt_port), FALSE);
    SetDlgItemTextW(dlg, IDC_STT_KEY, widen(g.svc->stt_key()).c_str());
    CheckDlgButton(dlg, IDC_STT_AUTOSTART, s.stt_enabled ? BST_CHECKED : BST_UNCHECKED);
    CheckDlgButton(dlg, IDC_UPD_AUTO, g.upd.check ? BST_CHECKED : BST_UNCHECKED);
}

void label(HWND dlg) {
    SetWindowTextW(dlg, tr(S::AppName));
    struct {
        int id;
        S s;
    } const texts[] = {
        {IDC_G_FOLDER, S::GroupFolder},   {IDC_BROWSE, S::FolderAdd},    {IDC_REMOVEFOLDER, S::FolderRemove},
        {IDC_OPENFOLDER, S::Open},
        {IDC_G_CONN, S::GroupConnection}, {IDC_L_PORT, S::Port},         {IDC_L_HOURS, S::LinkHours},
        {IDC_L_ORIGINS, S::Origins},      {IDC_G_SECRET, S::GroupSecret}, {IDC_L_SECRET, S::SecretHint},
        {IDC_SHOW, S::Show},              {IDC_COPY, S::Copy},           {IDC_NEWSECRET, S::NewSecret},
        {IDC_G_CONVERT, S::GroupConversion}, {IDC_CONVERT, S::Convert},  {IDC_PREPARE, S::Prepare},
        {IDC_L_FFMPEG, S::Ffmpeg},        {IDC_FFBROWSE, S::Browse},     {IDC_FFFIND, S::FfmpegFind},
        {IDC_L_FFHINT, S::FfmpegHint},    {IDC_L_CACHE, S::CacheLimit},  {IDC_CLEARCACHE, S::ClearCache},
        {IDC_AUTOSTART, S::Autostart},    {IDC_L_ACTIVITY, S::Activity}, {IDOK, S::Save},
        {IDCANCEL, S::Close},
        {IDC_G_STT_MODEL, S::GroupSttModel}, {IDC_STT_DOWNLOAD, S::SttDownload},
        {IDC_G_STT_FOLDER, S::GroupSttFolder}, {IDC_STT_CHANGE, S::Change}, {IDC_STT_OPENFOLDER, S::Open},
        {IDC_G_STT_CONN, S::GroupSttConn}, {IDC_L_STT_LANG, S::Language}, {IDC_L_STT_PORT, S::Port},
        {IDC_G_STT_KEY, S::GroupSttKey},  {IDC_STT_SHOWKEY, S::Show},   {IDC_STT_COPYKEY, S::Copy},
        {IDC_STT_NEWKEY, S::NewKey},   {IDC_L_STT_KEY, S::SttKeyHint}, {IDC_STT_AUTOSTART, S::SttAutostart},
        {IDC_L_STT_LOCAL, S::SttLocal},
        {IDC_G_UPD, S::GroupUpd},         {IDC_UPD_AUTO, S::UpdAuto},    {IDC_UPD_CHECK, S::UpdCheck},
        {IDC_UPD_NOTES, S::UpdNotes},     {IDC_G_UPD_HOW, S::GroupUpdHow}, {IDC_L_UPD_HOW, S::UpdHow},
    };
    for (const auto& t : texts) SetDlgItemTextW(dlg, t.id, tr(t.s));
    SetDlgItemTextW(dlg, IDC_UPD_VERSION, trf(S::UpdVersion, widen(CLEW_VERSION).c_str()).c_str());
    SendDlgItemMessageW(dlg, IDC_STT_LANG, CB_RESETCONTENT, 0, 0);
    for (S s : {S::LangChosen, S::LangEnglish, S::LangJapanese, S::LangFrench, S::LangMalagasy})
        SendDlgItemMessageW(dlg, IDC_STT_LANG, CB_ADDSTRING, 0, LPARAM(tr(s)));
    SendDlgItemMessageW(dlg, IDC_STT_PORT, EM_LIMITTEXT, 5, 0);
    // The names are the rows' text for screen readers; what is drawn is model_row_text().
    SendDlgItemMessageW(dlg, IDC_STT_MODELS, LB_RESETCONTENT, 0, 0);
    for (const auto& m : kSttModels) SendDlgItemMessageW(dlg, IDC_STT_MODELS, LB_ADDSTRING, 0, LPARAM(widen(m.name).c_str()));
    HWND tabs = GetDlgItem(dlg, IDC_TABS);
    TabCtrl_DeleteAllItems(tabs);
    S tab_names[] = {S::TabAudio, S::TabDictation, S::TabUpdates};
    for (int i = 0; i < int(std::size(tab_names)); ++i) {
        TCITEMW item{};
        item.mask = TCIF_TEXT;
        item.pszText = const_cast<LPWSTR>(tr(tab_names[i]));
        TabCtrl_InsertItem(tabs, i, &item);
    }
    SendDlgItemMessageW(dlg, IDC_PORT, EM_LIMITTEXT, 5, 0);
    SendDlgItemMessageW(dlg, IDC_HOURS, EM_LIMITTEXT, 3, 0);
    SendDlgItemMessageW(dlg, IDC_CACHEMB, EM_LIMITTEXT, 7, 0);
    SendDlgItemMessageW(dlg, IDC_ORIGINS, EM_LIMITTEXT, 4000, 0);
}

bool read_int(HWND dlg, int id, int lo, int hi, int* out) {
    BOOL ok = FALSE;
    UINT v = GetDlgItemInt(dlg, id, &ok, FALSE);
    if (!ok || int(v) < lo || int(v) > hi) return false;
    *out = int(v);
    return true;
}

void bad(HWND dlg, int id, const std::wstring& text) {
    message(dlg, text);
    MessageBeep(MB_ICONWARNING);
    SetFocus(GetDlgItem(dlg, id));
    SendDlgItemMessageW(dlg, id, EM_SETSEL, 0, -1);
}

void save(HWND dlg) {
    Settings s = g.svc->settings();  // the folders as they are: they changed when they were changed
    std::error_code ec;
    if (!read_int(dlg, IDC_PORT, 1024, 65535, &s.port)) return bad(dlg, IDC_PORT, tr(S::BadPort));
    if (!read_int(dlg, IDC_HOURS, 1, 168, &s.link_hours)) return bad(dlg, IDC_HOURS, tr(S::BadHours));
    if (!read_int(dlg, IDC_CACHEMB, 256, 1024 * 1024, &s.cache_mb)) return bad(dlg, IDC_CACHEMB, tr(S::BadCache));
    std::vector<std::string> origins;
    std::string lines = narrow(window_text(dlg, IDC_ORIGINS));
    std::size_t at = 0;
    while (at <= lines.size()) {
        std::size_t end = lines.find('\n', at);
        if (end == std::string::npos) end = lines.size();
        std::string line = trim(lines.substr(at, end - at));
        at = end + 1;
        if (line.empty()) continue;
        auto o = normalize_origin(line);
        if (!o) return bad(dlg, IDC_ORIGINS, trf(S::BadOrigin, widen(line).c_str()));
        origins.push_back(*o);
    }
    std::wstring ffmpeg = widen(trim(narrow(window_text(dlg, IDC_FFMPEG))));
    if (!ffmpeg.empty() && !std::filesystem::is_regular_file(ffmpeg, ec)) return bad(dlg, IDC_FFMPEG, tr(S::BadFfmpeg));
    if (!read_int(dlg, IDC_STT_PORT, 1024, 65535, &s.stt_port) || s.stt_port == s.port) {
        show_page(dlg, 1);
        return bad(dlg, IDC_STT_PORT, tr(S::BadSttPort));
    }
    s.stt_model = kSttModels[std::size_t(g.stt_row)].name;
    LRESULT lang = SendDlgItemMessageW(dlg, IDC_STT_LANG, CB_GETCURSEL, 0, 0);
    if (lang >= 0 && lang < LRESULT(std::size(kLanguages))) s.stt_language = kLanguages[lang];
    s.stt_enabled = IsDlgButtonChecked(dlg, IDC_STT_AUTOSTART) == BST_CHECKED;

    s.origins = origins;
    s.ffmpeg = narrow(ffmpeg);
    s.convert = IsDlgButtonChecked(dlg, IDC_CONVERT) == BST_CHECKED;
    s.prepare = IsDlgButtonChecked(dlg, IDC_PREPARE) == BST_CHECKED;
    set_autostart(IsDlgButtonChecked(dlg, IDC_AUTOSTART) == BST_CHECKED);
    g.stopped_by_user = false;
    std::string problem = g.svc->apply(s);
    fill(dlg);
    message(dlg, problem.empty() ? std::wstring(tr(S::Saved)) : problem_text(problem, s.port));
    refresh_status(dlg);
    tray(NIM_MODIFY);
}

void maybe_check();
void check_now(bool quiet);
void install_update(HWND dlg);
void open_release_page();

INT_PTR CALLBACK settings_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_INITDIALOG: {
            HICON icon_big = nullptr, icon_small = nullptr;
            LoadIconMetric(g.inst, MAKEINTRESOURCEW(IDI_APP), LIM_LARGE, &icon_big);
            LoadIconMetric(g.inst, MAKEINTRESOURCEW(IDI_APP), LIM_SMALL, &icon_small);
            SendMessageW(dlg, WM_SETICON, ICON_BIG, LPARAM(icon_big));
            SendMessageW(dlg, WM_SETICON, ICON_SMALL, LPARAM(icon_small));
            label(dlg);
            init_folder_list(dlg);
            fill(dlg);
            show_secret(dlg, false);
            show_stt_key(dlg, false);
            show_page(dlg, g.tab);
            refresh_status(dlg);
            refresh_cache(dlg);
            refresh_updates(dlg);
            if (g.svc->settings().folders.empty()) message(dlg, tr(S::FirstRun));
            SetTimer(dlg, kRefreshTimer, 3000, nullptr);
            return TRUE;
        }
        case WM_TIMER:
            refresh_cache(dlg);
            return TRUE;
        case WM_APP_STATUS:
            refresh_status(dlg);
            return TRUE;
        case WM_APP_TOGGLE_FOLDER: {
            // What the tick box shows now, against what is saved: a click that changed nothing does nothing.
            auto folders = g.svc->settings().folders;
            std::size_t i = std::size_t(wp);
            if (i < folders.size()) {
                bool ticked = ListView_GetCheckState(GetDlgItem(dlg, IDC_FOLDER), int(i)) != 0;
                if (ticked != folders[i].enabled) toggle_folder(i);
            }
            return TRUE;
        }
        case WM_MEASUREITEM: {
            auto* m = reinterpret_cast<MEASUREITEMSTRUCT*>(lp);
            if (m->CtlID != IDC_STT_MODELS) break;
            RECT r{0, 0, 0, 11};  // 11 dialog units: a row with room around its text
            MapDialogRect(dlg, &r);
            m->itemHeight = UINT(r.bottom);
            SetWindowLongPtrW(dlg, DWLP_MSGRESULT, TRUE);
            return TRUE;
        }
        case WM_DRAWITEM: {
            auto* d = reinterpret_cast<DRAWITEMSTRUCT*>(lp);
            if (d->CtlID != IDC_STT_MODELS) break;
            draw_model_row(*d);
            return TRUE;
        }
        case WM_NOTIFY: {
            auto* hdr = reinterpret_cast<NMHDR*>(lp);
            if (hdr->idFrom == IDC_TABS && hdr->code == TCN_SELCHANGE) {
                show_page(dlg, TabCtrl_GetCurSel(hdr->hwndFrom));
                return TRUE;
            }
            if (hdr->idFrom == IDC_FOLDER && hdr->code == LVN_ITEMCHANGED && !g.filling_folders) {
                auto* change = reinterpret_cast<NMLISTVIEW*>(lp);
                if ((change->uChanged & LVIF_STATE) &&
                    ((change->uNewState ^ change->uOldState) & LVIS_STATEIMAGEMASK) && change->iItem >= 0) {
                    // A tick box: takes effect at once. Posted, so the list is not redrawn while it
                    // is still telling of the change.
                    PostMessageW(dlg, WM_APP_TOGGLE_FOLDER, WPARAM(change->iItem), 0);
                }
                enable_folder_buttons(dlg);
                return TRUE;
            }
            if (hdr->idFrom == IDC_FOLDER && hdr->code == NM_DBLCLK) {
                auto folders = g.svc->settings().folders;
                int at = selected_folder(dlg);
                if (at >= 0 && at < int(folders.size())) open_folder(folders[std::size_t(at)].path);
                return TRUE;
            }
            break;
        }
        case WM_COMMAND:
            switch (LOWORD(wp)) {
                case IDOK: save(dlg); return TRUE;
                case IDCANCEL: DestroyWindow(dlg); return TRUE;
                case IDC_STARTSTOP:
                    if (g.svc->status().running) {
                        g.stopped_by_user = true;
                        g.svc->stop();
                    } else {
                        g.stopped_by_user = false;
                        g.svc->apply(g.svc->settings(), false);
                    }
                    refresh_status(dlg);
                    return TRUE;
                case IDC_BROWSE: add_folder(dlg); return TRUE;
                case IDC_REMOVEFOLDER: remove_folder(dlg); return TRUE;
                case IDC_OPENFOLDER: {
                    auto folders = g.svc->settings().folders;
                    int at = selected_folder(dlg);
                    if (at >= 0 && at < int(folders.size())) open_folder(folders[std::size_t(at)].path);
                    return TRUE;
                }
                case IDC_SHOW: show_secret(dlg, !g.secret_visible); return TRUE;
                case IDC_COPY:
                    copy_text(dlg, widen(g.svc->secret()), true);
                    message(dlg, tr(S::Copied));
                    return TRUE;
                case IDC_NEWSECRET:
                    if (MessageBoxW(dlg, tr(S::NewSecretConfirm), tr(S::AppName),
                                    MB_OKCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2) == IDOK) {
                        SetDlgItemTextW(dlg, IDC_SECRET, widen(g.svc->reset_secret()).c_str());
                        show_secret(dlg, true);
                    }
                    return TRUE;
                case IDC_FFBROWSE: {
                    std::wstring p = pick(dlg, false, {});
                    if (!p.empty()) SetDlgItemTextW(dlg, IDC_FFMPEG, p.c_str());
                    return TRUE;
                }
                case IDC_FFFIND: {
                    auto found = platform::find_ffmpeg();
                    if (found.empty())
                        message(dlg, tr(S::FfmpegNotFound));
                    else
                        SetDlgItemTextW(dlg, IDC_FFMPEG, found.c_str());
                    return TRUE;
                }
                case IDC_CLEARCACHE:
                    g.svc->cache().clear();
                    refresh_cache(dlg);
                    return TRUE;
                case IDC_STT_STARTSTOP:
                    toggle_dictation();
                    refresh_dictation(dlg);
                    return TRUE;
                case IDC_STT_DOWNLOAD: download_model(dlg, kSttModels[std::size_t(g.stt_row)]); return TRUE;
                case IDC_STT_MODELS:
                    if (HIWORD(wp) == LBN_SELCHANGE) choose_model_row(dlg);
                    return TRUE;
                case IDC_STT_LANG:
                    if (HIWORD(wp) == CBN_SELCHANGE) use_language(dlg);
                    return TRUE;
                case IDC_STT_CHANGE: {
                    // Takes effect at once: a download goes where the window says.
                    std::wstring p = pick(dlg, true, g.svc->stt_models_dir().wstring(), tr(S::ChooseModelsFolder));
                    if (p.empty()) return TRUE;
                    Settings s = g.svc->settings();
                    s.stt_models_dir = narrow(p);
                    g.svc->apply(s);
                    SetDlgItemTextW(dlg, IDC_STT_FOLDER, p.c_str());
                    refresh_dictation(dlg);
                    return TRUE;
                }
                case IDC_STT_OPENFOLDER: {
                    auto dir = g.svc->stt_models_dir();
                    std::error_code ec;
                    std::filesystem::create_directories(dir, ec);
                    ShellExecuteW(nullptr, L"open", dir.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
                    return TRUE;
                }
                case IDC_UPD_AUTO:
                    // Takes effect at once, like the folders.
                    g.upd.check = IsDlgButtonChecked(dlg, IDC_UPD_AUTO) == BST_CHECKED;
                    update::save_state(update::state_file(), g.upd);
                    if (g.upd.check) maybe_check();
                    return TRUE;
                case IDC_UPD_CHECK: check_now(false); return TRUE;
                case IDC_UPD_INSTALL: install_update(dlg); return TRUE;
                case IDC_UPD_NOTES: open_release_page(); return TRUE;
                case IDC_STT_SHOWKEY: show_stt_key(dlg, !g.stt_key_visible); return TRUE;
                case IDC_STT_COPYKEY:
                    copy_text(dlg, widen(g.svc->stt_key()), true);
                    message(dlg, tr(S::Copied));
                    return TRUE;
                case IDC_STT_NEWKEY:
                    if (MessageBoxW(dlg, tr(S::NewSttKeyConfirm), tr(S::AppName),
                                    MB_OKCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2) == IDOK) {
                        SetDlgItemTextW(dlg, IDC_STT_KEY, widen(g.svc->reset_stt_key()).c_str());
                        show_stt_key(dlg, true);
                    }
                    return TRUE;
            }
            break;
        case WM_DESTROY:
            KillTimer(dlg, kRefreshTimer);
            // Nothing of the secret stays in the closed window's memory longer than needed.
            SetDlgItemTextW(dlg, IDC_SECRET, L"");
            SetDlgItemTextW(dlg, IDC_STT_KEY, L"");
            g.dlg = nullptr;
            return TRUE;
    }
    return FALSE;
}

void show_settings() {
    if (!g.dlg) g.dlg = CreateDialogParamW(g.inst, MAKEINTRESOURCEW(IDD_SETTINGS), nullptr, settings_proc, 0);
    if (!g.dlg) return;
    ShowWindow(g.dlg, IsIconic(g.dlg) ? SW_RESTORE : SW_SHOW);
    SetForegroundWindow(g.dlg);
}

void show_updates() {
    g.tab = 2;
    show_settings();
    if (g.dlg) show_page(g.dlg, 2);
}

// ---------------------------------------------------------------------------------------------
// New versions (docs/32): a daily question to GitHub when the user leaves it on; a download, the
// checks and the installer only after a press on Install.
// ---------------------------------------------------------------------------------------------

struct CheckResult {
    std::optional<update::Release> release;
    std::string why;
    bool quiet = false;  // the daily check: it tells through the tray, once per version
};

struct DownloadResult {
    std::optional<updater::Installer> installer;
    std::string why;
};

std::int64_t now_seconds() { return std::int64_t(std::time(nullptr)); }

bool newer_than_this(const update::Release& r) {
    auto theirs = update::parse_version(r.version);
    auto mine = update::parse_version(CLEW_VERSION);
    return theirs && mine && update::compare(*theirs, *mine) > 0;
}

void open_release_page() {
    std::string url = g.upd_release ? update::release_page(g.upd_release->version)
                                    : std::string("https://github.com/") + update::kRepository + "/releases";
    ShellExecuteW(nullptr, L"open", widen(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void notify_update(const std::string& version) {
    NOTIFYICONDATAW nid{};
    nid.uFlags = NIF_INFO;
    identify(nid);
    wcsncpy_s(nid.szInfoTitle, tr(S::UpdBalloonTitle), _TRUNCATE);
    wcsncpy_s(nid.szInfo, trf(S::UpdBalloonText, widen(version).c_str()).c_str(), _TRUNCATE);
    nid.dwInfoFlags = NIIF_INFO | NIIF_RESPECT_QUIET_TIME;
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

void check_now(bool quiet) {
    if (g.upd_state == Upd::Checking || g.upd_state == Upd::Downloading) return;
    g.upd_state = Upd::Checking;
    if (g.dlg) refresh_updates(g.dlg);
    HWND hwnd = g.hwnd;
    std::thread([hwnd, quiet] {
        auto* res = new CheckResult;
        res->quiet = quiet;
        res->release = updater::fetch_latest(&res->why);
        if (!PostMessageW(hwnd, WM_APP_UPDATE_CHECKED, 0, LPARAM(res))) delete res;
    }).detach();
}

void maybe_check() {
    if (update::check_due(g.upd, now_seconds())) check_now(true);
}

void on_checked(std::unique_ptr<CheckResult> res) {
    if (!res->release) {
        // A daily check that could not reach GitHub (offline, a proxy) says nothing and tries again
        // within the hour; one the user asked for says why.
        if (res->quiet) {
            g.upd_state = g.upd_release ? Upd::Available : Upd::Idle;
        } else {
            g.upd_state = Upd::Failed;
            g.upd_problem = res->why;
        }
    } else {
        g.upd.last_check = now_seconds();
        if (newer_than_this(*res->release)) {
            g.upd_release = res->release;
            g.upd_state = Upd::Available;
            if (g.upd.notified != res->release->version) {
                if (res->quiet) notify_update(res->release->version);
                g.upd.notified = res->release->version;
            }
        } else {
            g.upd_release.reset();
            g.upd_state = Upd::Current;
        }
        update::save_state(update::state_file(), g.upd);
    }
    if (g.dlg) refresh_updates(g.dlg);
}

void install_update(HWND dlg) {
    if (!g.upd_release || g.upd_state == Upd::Checking || g.upd_state == Upd::Downloading) return;
    if (!can_install()) return open_release_page();
    std::wstring ask = trf(S::UpdInstallConfirm, widen(g.upd_release->version).c_str());
    if (MessageBoxW(dlg, ask.c_str(), tr(S::AppName), MB_OKCANCEL | MB_ICONQUESTION) != IDOK) return;
    g.upd_state = Upd::Downloading;
    g.upd_percent = 0;
    refresh_updates(dlg);
    HWND hwnd = g.hwnd;
    update::Release r = *g.upd_release;
    std::thread([hwnd, r] {
        auto* res = new DownloadResult;
        res->installer = updater::download(
            r, [hwnd](int percent) { PostMessageW(hwnd, WM_APP_UPDATE_PROGRESS, WPARAM(percent), 0); }, &res->why);
        if (!PostMessageW(hwnd, WM_APP_UPDATE_DOWNLOADED, 0, LPARAM(res))) {
            if (res->installer) updater::close(*res->installer);
            delete res;
        }
    }).detach();
}

void on_downloaded(std::unique_ptr<DownloadResult> res) {
    if (!res->installer) {
        g.upd_state = Upd::Failed;
        g.upd_problem = res->why;
        if (g.dlg) refresh_updates(g.dlg);
        return;
    }
    // Everything of ours stops so the installer can replace the files: the server now, the
    // children (dictation, a conversion) with this process, by their job objects. The single-
    // instance mutex goes first, so the installer does not wait for it.
    g.svc->dictation().stop();
    g.svc->stop();
    if (g.mutex) CloseHandle(g.mutex);
    g.mutex = nullptr;
    std::string why;
    bool started = updater::start(*res->installer, &why);
    updater::close(*res->installer);
    if (started) {
        if (g.dlg) DestroyWindow(g.dlg);
        DestroyWindow(g.hwnd);
        return;
    }
    // The installer did not start: carry on as before.
    g.mutex = CreateMutexW(nullptr, TRUE, kMutex);
    g.svc->apply(g.svc->settings(), false);
    g.svc->start_dictation_if_enabled();
    g.upd_state = Upd::Failed;
    g.upd_problem = why;
    tray(NIM_MODIFY);
    if (g.dlg) {
        refresh_status(g.dlg);
        refresh_updates(g.dlg);
    }
}

// ---------------------------------------------------------------------------------------------
// The hidden window that owns the tray icon
// ---------------------------------------------------------------------------------------------

LRESULT CALLBACK tray_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == g.taskbar_created && msg != 0) {
        tray(NIM_ADD);  // Explorer restarted: the icon must be added again.
        return 0;
    }
    switch (msg) {
        case WM_APP_TRAY:
            switch (LOWORD(lp)) {
                case NIN_SELECT:
                case NIN_KEYSELECT: show_settings(); return 0;
                case NIN_BALLOONUSERCLICK: show_updates(); return 0;
                case WM_CONTEXTMENU: menu(GET_X_LPARAM(wp), GET_Y_LPARAM(wp)); return 0;
            }
            return 0;
        case WM_APP_STATUS:
            g.status_pending = false;
            tray(NIM_MODIFY);
            if (g.dlg) SendMessageW(g.dlg, WM_APP_STATUS, 0, 0);
            return 0;
        case WM_APP_SHOW: show_settings(); return 0;
        case WM_TIMER:
            if (wp == kUpdateFirstTimer) KillTimer(hwnd, kUpdateFirstTimer);
            if (wp == kUpdateFirstTimer || wp == kUpdateTimer) maybe_check();
            return 0;
        case WM_APP_UPDATE_CHECKED:
            on_checked(std::unique_ptr<CheckResult>(reinterpret_cast<CheckResult*>(lp)));
            return 0;
        case WM_APP_UPDATE_PROGRESS:
            g.upd_percent = int(wp);
            if (g.dlg && g.upd_state == Upd::Downloading) refresh_updates(g.dlg);
            return 0;
        case WM_APP_UPDATE_DOWNLOADED:
            on_downloaded(std::unique_ptr<DownloadResult>(reinterpret_cast<DownloadResult*>(lp)));
            return 0;
        case WM_SETTINGCHANGE:
            // The light/dark switch arrives as "ImmersiveColorSet".
            if (lp && wcscmp(reinterpret_cast<const wchar_t*>(lp), L"ImmersiveColorSet") == 0) tray(NIM_MODIFY);
            return 0;
        case WM_ENDSESSION:
            if (wp) DestroyWindow(hwnd);
            return 0;
        case WM_DESTROY: {
            NOTIFYICONDATAW nid{};
            identify(nid);
            Shell_NotifyIconW(NIM_DELETE, &nid);
            PostQuitMessage(0);
            return 0;
        }
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

}  // namespace

int quit_running() {
    HWND other = FindWindowW(kWindowClass, nullptr);
    if (!other) return 0;
    DWORD pid = 0;
    GetWindowThreadProcessId(other, &pid);
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    PostMessageW(other, WM_CLOSE, 0, 0);
    DWORD waited = process ? WaitForSingleObject(process, 10000) : WAIT_FAILED;
    if (process) CloseHandle(process);
    return waited == WAIT_OBJECT_0 ? 0 : 1;
}

int run_tray(HINSTANCE instance) {
    // One server per Windows session: a second start opens the first one's window instead.
    g.mutex = CreateMutexW(nullptr, TRUE, kMutex);
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND other = FindWindowW(kWindowClass, nullptr);
        if (other) {
            DWORD pid = 0;
            GetWindowThreadProcessId(other, &pid);
            AllowSetForegroundWindow(pid);
            PostMessageW(other, WM_APP_SHOW, 0, 0);
        } else {
            MessageBoxW(nullptr, tr(S::AlreadyRunning), tr(S::AppName), MB_OK | MB_ICONINFORMATION);
        }
        return 0;
    }
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    INITCOMMONCONTROLSEX icc{sizeof icc, ICC_STANDARD_CLASSES | ICC_TAB_CLASSES};
    InitCommonControlsEx(&icc);

    g.inst = instance;
    static Service svc(default_settings_file(), {}, nullptr);
    g.svc = &svc;
    g.upd = update::load_state(update::state_file());

    WNDCLASSEXW wc{sizeof wc};
    wc.lpfnWndProc = tray_proc;
    wc.hInstance = instance;
    wc.lpszClassName = kWindowClass;
    wc.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(IDI_APP));
    RegisterClassExW(&wc);
    // A real (hidden) top-level window, not message-only: it must receive TaskbarCreated.
    g.hwnd = CreateWindowExW(0, kWindowClass, tr(S::AppName), WS_OVERLAPPED, 0, 0, 0, 0, nullptr, nullptr, instance,
                             nullptr);
    g.taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");
    ChangeWindowMessageFilterEx(g.hwnd, g.taskbar_created, MSGFLT_ALLOW, nullptr);

    svc.on_change([] {
        // Many requests make many changes; one repaint is enough.
        if (!g.status_pending.exchange(true)) PostMessageW(g.hwnd, WM_APP_STATUS, 0, 0);
    });
    svc.secret();
    svc.apply(svc.settings(), false);
    svc.start_dictation_if_enabled();
    tray(NIM_ADD);
    if (svc.settings().folders.empty()) show_settings();
    // The first daily check two minutes after start (not during Windows' busy sign-in), then hourly.
    SetTimer(g.hwnd, kUpdateFirstTimer, 2 * 60 * 1000, nullptr);
    SetTimer(g.hwnd, kUpdateTimer, 60 * 60 * 1000, nullptr);

    MSG m;
    while (GetMessageW(&m, nullptr, 0, 0) > 0) {
        if (g.dlg && IsDialogMessageW(g.dlg, &m)) continue;
        TranslateMessage(&m);
        DispatchMessageW(&m);
    }
    svc.stop();
    if (g.mutex) CloseHandle(g.mutex);
    // Connection threads and a running conversion are not waited for: the process ends, and the
    // job object ends the converter with it.
    ExitProcess(0);
}

}  // namespace clew::win
