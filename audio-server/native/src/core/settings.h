// What the settings window edits and the next start remembers. The secret is kept sealed by the
// system's key store (platform.h), so the window can show it again; it is never written in clear.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "platform.h"

namespace clew {

constexpr int kDefaultPort = 47821;
constexpr int kDefaultLinkHours = 4;
constexpr int kDefaultCacheMb = 4096;
constexpr int kDefaultSttPort = 47822;
constexpr const char* kDefaultSttModel = "small";
// The hosted Clew (docs/32): allowed on a first start, so the website works without a step more;
// the window can remove it. The secret is still needed for everything.
constexpr const char* kHostedClew = "https://clew.alefa.net";

// One folder of recordings (docs/22 section 11). One that is off keeps its place and its name in
// Clew, but nothing in it is listed or served until it is on again.
struct FolderSetting {
    std::string path;  // UTF-8
    bool enabled = true;
    bool operator==(const FolderSetting& o) const { return path == o.path && enabled == o.enabled; }
};

struct Settings {
    std::vector<FolderSetting> folders;  // in the order the window lists them; empty until chosen
    int port = kDefaultPort;
    std::vector<std::string> origins = {kHostedClew};  // hosted Clew addresses; loopback is always allowed
    int link_hours = kDefaultLinkHours;
    bool convert = true;  // convert what browsers cannot play
    bool prepare = true;  // convert those ahead, in the background, after a listing
    int cache_mb = kDefaultCacheMb;
    // Optional: an ffmpeg program, tried when the system's codecs cannot convert a file. Empty: none.
    std::string ffmpeg;
    bool system_codecs = true;  // off: ffmpeg only (for testing it, or when Windows' codecs misbehave)
    std::optional<platform::Sealed> secret;

    // Dictation (docs/28): the speech server on this computer, a child process (dictation.h).
    bool stt_enabled = false;              // start it with the server
    std::string stt_model = kDefaultSttModel;
    std::string stt_models_dir;            // UTF-8; empty: platform::default_stt_models_dir()
    std::string stt_language;              // "", "en", "ja", "fr" or "mg"; "": Clew names one per session
    int stt_port = kDefaultSttPort;
    std::optional<platform::Sealed> stt_secret;  // its key, sealed like `secret`

    static Settings load(const std::filesystem::path& file);
    // Atomic: written beside, then renamed over.
    bool save(const std::filesystem::path& file) const;
};

std::filesystem::path default_settings_file();

// "https://you.github.io/" -> "https://you.github.io"; nothing for what is not an origin.
std::optional<std::string> normalize_origin(const std::string& text);

// A new secret: 24 random bytes, URL-safe base64 (as the Python server makes).
std::string new_secret();

// A new dictation key, in the speech server's shape: "stt.local.<secret>".
std::string new_stt_key();

// The models the Dictation tab lists (tools/stt-server/alefa_stt/models.py has their sources),
// smallest first. Sizes as listed on Hugging Face, 2026-10-16. What each needs (docs/28 §3.6):
// small and large-v3-turbo were measured on an i7-1360P with 16 GB; the rest are estimates.
struct SttModel {
    enum Use { Live, Files, Gpu };
    const char* name;
    std::uint64_t bytes;
    Use use;               // Live: dictate on the CPU; Files: seconds a sentence; Gpu: an NVIDIA GPU only
    unsigned memory_gb;    // the computer's memory it needs
    bool english_only;
};
constexpr SttModel kSttModels[] = {
    {"tiny", 78203619ull, SttModel::Live, 2, false},
    {"base", 147882941ull, SttModel::Live, 2, false},
    {"small", 486212372ull, SttModel::Live, 4, false},
    {"medium", 1530571735ull, SttModel::Gpu, 8, false},
    {"large-v3-turbo", 1621665983ull, SttModel::Files, 8, false},
    {"distil-large-v3", 1516479628ull, SttModel::Files, 8, true},
    {"large-v3", 3090835702ull, SttModel::Gpu, 8, false},
};
// Live transcription's speaker model (docs/29), fetched with the Whisper model.
constexpr std::uint64_t kSpeakerModelBytes = 28281164ull;

bool stt_model_known(const std::string& name);
const SttModel* find_stt_model(const std::string& name);

// Why this computer cannot run a model: "" when it can, "memory" or "gpu". `gpus` is how many
// NVIDIA GPUs CUDA sees (the server's `system` command), or -1 while that is not known yet.
std::string stt_model_blocker(const SttModel& model, std::uint64_t memory_bytes, int gpus);
bool stt_language_known(const std::string& code);

}  // namespace clew
