// What each operating system supplies. Windows: src/win/. macOS and Linux: src/posix/
// (compatible, not built or tested yet; see the README).
#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace clew::platform {

namespace fs = std::filesystem;

// Cryptographically strong random bytes from the system.
std::string random_bytes(std::size_t n);

// A secret sealed by the system's own key store, for this user only.
// Windows: DPAPI (CurrentUser). macOS: the Keychain. Linux: the Secret Service, or a 0600 file.
struct Sealed {
    std::string kind;  // "dpapi", "keychain", "file"...
    std::string data;  // base64, or a key-store reference
};
std::optional<Sealed> seal_secret(const std::string& plain);
std::optional<std::string> open_secret(const Sealed& sealed);

// Where the settings are kept (roaming). Converted copies stay in the served folder (convert_cache.h).
fs::path config_dir();

// Whether this system can convert audio without any extra program.
bool can_convert();

// How one conversion runs: the file someone is opening goes first; preparing ahead runs at idle
// priority and stops at once when `cancel` is set (it is put back in the queue, not marked failed).
struct ConvertControl {
    bool background = false;
    const std::atomic<bool>* cancel = nullptr;
    bool cancelled() const { return cancel && cancel->load(); }
};

// Writes `in` as a 128 kbit/s constant-bit-rate mp3 with no seek table to `out`, with the system's
// own codecs. Runs isolated from the server where the system allows (Windows: a low-integrity child
// process in a job object).
bool convert_to_mp3(const fs::path& in, const fs::path& out, const ConvertControl& control, std::string* why);

// The same with an ffmpeg program the user chose (optional, a fallback; nothing is linked).
bool ffmpeg_to_mp3(const fs::path& ffmpeg, const fs::path& in, const fs::path& out, const ConvertControl& control,
                   std::string* why);

// An ffmpeg beside this program, else on PATH; empty when none.
fs::path find_ffmpeg();

// This program's own file.
fs::path self_path();

// A helper program that runs as long as it is wanted (the dictation server, docs/28 §3.4).
// Windows: in a job object, so it ends with this process, with a memory cap and no windows. Not
// at low integrity like the converter: it writes its models folder.
class Child {
public:
    virtual ~Child() = default;  // ends it, and waits for its output to be read
    virtual void stop() = 0;     // ends it; `on_exit` still follows
};

// Starts `exe args`. `input` is written to its standard input, which is then closed: how a key
// reaches it without being on a command line or in a file. Each line it prints (its standard
// output and error together) goes to `on_line`, then `on_exit` gets its exit code; both are called
// from a thread of the child's own, never after the Child is destroyed. Nothing on failure.
std::unique_ptr<Child> start_child(const fs::path& exe, const std::vector<std::string>& args,
                                   const std::string& input, std::uint64_t memory_limit,
                                   std::function<void(const std::string&)> on_line,
                                   std::function<void(int)> on_exit, std::string* why);

// The dictation server: CLEW_STT_SERVER_EXE, else clew-stt-server/clew-stt-server(.exe) beside
// this program (where the installer puts it). Empty when there is none.
fs::path find_stt_server();

// Where models go unless the user chose a folder: %LOCALAPPDATA%\clew-stt\models on Windows.
fs::path default_stt_models_dir();

// The computer's memory, in bytes (0 when unknown).
std::uint64_t total_memory();

}  // namespace clew::platform
