// New versions (docs/32): what a release says about itself, and what this copy remembers about
// checking. Portable; the download and the signature check are the system's (src/win/update_win.h).
//
// Each release on github.com/yaqalab/clew-tools carries `latest.json`:
//   {"version": "1.2.0", "installer": "clew-audio-server-setup-1.2.0.exe",
//    "sha256": "<64 hex>", "size": 63081992}
// The program only ever asks that one repository: the addresses below are built here from the
// version, never taken from the file, so a changed file cannot send it anywhere else.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace clew::update {

constexpr const char* kRepository = "yaqalab/clew-tools";

struct Version {
    int major = 0, minor = 0, patch = 0;
};
// "1.2.3" only: three numbers below 10000, nothing before or after.
std::optional<Version> parse_version(const std::string& text);
// <0, 0 or >0, like strcmp.
int compare(const Version& a, const Version& b);

struct Release {
    std::string version;    // "1.2.0"
    std::string installer;  // "clew-audio-server-setup-1.2.0.exe"
    std::string sha256;     // lowercase hex
    std::uint64_t size = 0;
};
// The release's `latest.json`; nothing when any part is missing or not what it should be (the
// installer must be named after the version, the size at most 1 GB).
std::optional<Release> parse_manifest(const std::string& text);

std::string installer_name(const std::string& version);
std::string manifest_url();
std::string installer_url(const Release& r);
std::string release_page(const std::string& version);

// What this copy remembers, in update.json beside the settings.
struct State {
    bool check = true;             // look for a new version once a day
    std::int64_t last_check = 0;   // seconds since 1970, when it last asked
    std::string notified;          // the version the tray last told of, so it says so once
};
std::filesystem::path state_file();
State load_state(const std::filesystem::path& file);
bool save_state(const std::filesystem::path& file, const State& s);

// Whether a daily check is due now.
bool check_due(const State& s, std::int64_t now);

}  // namespace clew::update
