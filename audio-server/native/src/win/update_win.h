// New versions on Windows (docs/32): asking GitHub for the latest release, downloading its
// installer, and checking it before it may run. The tray (tray_win.cpp) decides when.
#pragma once

#include <windows.h>

#include <cstdint>
#include <functional>
#include <optional>
#include <string>

#include "../core/update.h"

namespace clew::win::updater {

// The latest release, or nothing with `why` set. One small HTTPS request to github.com.
std::optional<update::Release> fetch_latest(std::string* why);

// A downloaded installer, held open so nothing can change it between the checks and its start.
struct Installer {
    std::wstring path;
    HANDLE file = INVALID_HANDLE_VALUE;  // shared for reading only
};

// Downloads the release's installer into %LOCALAPPDATA%\clew-audio-server\updates and checks it:
// the size and SHA-256 that latest.json gives, an intact Authenticode signature, and a signer that
// is the very certificate this program was signed with. `progress` gets 0..100 from the worker
// thread. Nothing (and the file removed) when any check fails, with `why` set.
std::optional<Installer> download(const update::Release& r, const std::function<void(int)>& progress, std::string* why);

// Whether this program carries a signature an update can be compared with. A build signed by no
// one cannot update itself: the window then offers the release page instead.
bool self_signed_by_known_signer();

// Starts the installer quietly, as an update: it closes nothing of ours (the tray has already
// stopped), keeps the chosen components and options, and starts the new version when done.
bool start(const Installer& in, std::string* why);
void close(Installer& in);

}  // namespace clew::win::updater
