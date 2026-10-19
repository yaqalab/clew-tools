// Windows entry point. Three ways to start the same program:
//   (no option)                 the tray icon and the settings window (tray_win.cpp)
//   --headless [options]        no window, output on the console (core/headless.cpp)
//   --convert-child <file>      internal: the sandboxed converter (platform_win.cpp starts it)
#include <windows.h>
#include <shellapi.h>

#include <string>
#include <vector>

#include "../core/headless.h"
#include "app.h"
#include "mf_convert.h"
#include "util.h"

namespace {

// Windows' own exploit mitigations, set before anything else is loaded.
void harden(bool converter) {
    HeapSetInformation(nullptr, HeapEnableTerminationOnCorruption, nullptr, 0);
    // DLLs only from the system and this program's folder: never the current folder or PATH.
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    SetSearchPathMode(BASE_SEARCH_PATH_ENABLE_SAFE_SEARCHMODE | BASE_SEARCH_PATH_PERMANENT);

    PROCESS_MITIGATION_IMAGE_LOAD_POLICY image{};
    image.NoRemoteImages = 1;
    image.NoLowMandatoryLabelImages = 1;
    image.PreferSystem32Images = 1;
    SetProcessMitigationPolicy(ProcessImageLoadPolicy, &image, sizeof image);

    PROCESS_MITIGATION_STRICT_HANDLE_CHECK_POLICY handles{};
    handles.RaiseExceptionOnInvalidHandleReference = 1;
    handles.HandleExceptionsPermanentlyEnabled = 1;
    SetProcessMitigationPolicy(ProcessStrictHandleCheckPolicy, &handles, sizeof handles);

    if (converter) {
        // The converter reads untrusted bytes with codecs: no injected DLLs (AppInit, hooks), no
        // code generated at run time. Not for the window, where they could break an input method.
        PROCESS_MITIGATION_EXTENSION_POINT_DISABLE_POLICY ext{};
        ext.DisableExtensionPoints = 1;
        SetProcessMitigationPolicy(ProcessExtensionPointDisablePolicy, &ext, sizeof ext);
        PROCESS_MITIGATION_DYNAMIC_CODE_POLICY code{};
        code.ProhibitDynamicCode = 1;
        SetProcessMitigationPolicy(ProcessDynamicCodePolicy, &code, sizeof code);
        SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    }
}

int converter_child(const std::wstring& in) {
    std::string why;
    int code = clew::win::convert_with_media_foundation(in.c_str(), GetStdHandle(STD_OUTPUT_HANDLE), &why);
    if (code != 0) {
        DWORD n = 0;
        WriteFile(GetStdHandle(STD_ERROR_HANDLE), why.data(), DWORD(why.size()), &n, nullptr);
    }
    return code;
}

// A window program has no console of its own: use the one it was started from, or the pipes it
// was given (npm run dev, the tests).
struct ConsoleOut {
    HANDLE out = nullptr, err = nullptr;
    HANDLE stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);

    ConsoleOut() {
        out = GetStdHandle(STD_OUTPUT_HANDLE);
        err = GetStdHandle(STD_ERROR_HANDLE);
        if ((!out || out == INVALID_HANDLE_VALUE) && AttachConsole(ATTACH_PARENT_PROCESS)) {
            out = err = CreateFileW(L"CONOUT$", GENERIC_WRITE, FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        }
    }

    static void write(HANDLE h, const std::string& line) {
        if (!h || h == INVALID_HANDLE_VALUE) return;
        std::string s = line + "\r\n";
        DWORD mode = 0, n = 0;
        if (GetConsoleMode(h, &mode)) {
            // A real console: wide characters, so a Japanese code page cannot garble or crash it.
            std::wstring w = clew::win::widen(s);
            WriteConsoleW(h, w.data(), DWORD(w.size()), &n, nullptr);
        } else {
            WriteFile(h, s.data(), DWORD(s.size()), &n, nullptr);
        }
    }
};

ConsoleOut* g_console = nullptr;

BOOL WINAPI on_ctrl(DWORD) {
    if (g_console) SetEvent(g_console->stop);
    return TRUE;
}

int headless(const std::vector<std::string>& args) {
    ConsoleOut console;
    g_console = &console;
    SetConsoleCtrlHandler(on_ctrl, TRUE);
    clew::Console c;
    c.out = [&](const std::string& s) { ConsoleOut::write(console.out, s); };
    c.err = [&](const std::string& s) { ConsoleOut::write(console.err, s); };
    c.wait_for_stop = [&] { WaitForSingleObject(console.stop, INFINITE); };
    return clew::run_headless(args, c);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::vector<std::wstring> wargs(argv + 1, argv + argc);
    LocalFree(argv);

    if (wargs.size() == 2 && wargs[0] == L"--convert-child") {
        harden(true);
        return converter_child(wargs[1]);
    }
    harden(false);
    std::vector<std::string> args;
    bool console_mode = false;
    for (const auto& a : wargs) {
        args.push_back(clew::win::narrow(a));
        if (a == L"--headless" || a == L"--help" || a == L"-h" || a == L"--version") console_mode = true;
    }
    if (console_mode) return headless(args);
    if (wargs.size() == 1 && wargs[0] == L"--quit") return clew::win::quit_running();
    return clew::win::run_tray(instance);
}
