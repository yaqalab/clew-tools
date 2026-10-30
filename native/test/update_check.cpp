// The updater's Windows half against a stand-in for GitHub (test_update.py runs it): asks for the
// latest release, downloads the installer and runs every check, then prints one line:
//   latest <version>        fetch only (no argument)
//   ok <path>               downloaded and every check passed (the file is not run)
//   refused <why>           a check failed, or the fetch did
// CLEW_UPDATE_BASE must point at the stand-in. Signed with the YaqaLab certificate by the test, so
// "the same signer as this program" means the same as a release.
#include <windows.h>

#include <cstdio>
#include <string>

#include "../src/win/update_win.h"

int wmain(int argc, wchar_t** argv) {
    using namespace clew::win;
    std::string why;
    auto r = updater::fetch_latest(&why);
    if (!r) {
        std::printf("refused %s\n", why.c_str());
        return 0;
    }
    if (argc < 2 || std::wstring(argv[1]) != L"--download") {
        std::printf("latest %s\n", r->version.c_str());
        return 0;
    }
    auto in = updater::download(*r, [](int) {}, &why);
    if (!in) {
        std::printf("refused %s\n", why.c_str());
        return 0;
    }
    std::printf("ok %ls\n", in->path.c_str());
    updater::close(*in);
    return 0;
}
