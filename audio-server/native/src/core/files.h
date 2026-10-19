// The audio files of one folder: listing, finding one from a relative path, and byte ranges.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace clew {

namespace fs = std::filesystem;

constexpr std::size_t kMaxFiles = 20000;

struct AudioFile {
    std::string path;  // relative, with "/", UTF-8
    std::string name;
    std::uint64_t size = 0;
    std::int64_t modified_ms = 0;
    std::string mime;
};

fs::path path_from_utf8(const std::string& s);
std::string path_to_utf8(const fs::path& p);

// The MIME type for an audio extension (any letter case), or nullptr. Same set as the kit's
// AUDIO_ACCEPT (packages/kit/src/audio/files.ts).
const char* mime_of(const fs::path& p);

// `candidate` is `root` or below it, both already canonical.
bool inside(const fs::path& root, const fs::path& candidate);

// Every audio file under `root` (canonical), sorted by path ignoring case. Hidden names (".x")
// and links or junctions that lead out of the folder are skipped. `truncated` past kMaxFiles.
std::vector<AudioFile> list_audio(const fs::path& root, bool* truncated);

// The file `rel` names, or nothing when it is not an audio file inside `root`. Refuses "..",
// absolute paths, drive letters, hidden segments, ":" (alternate data streams), names Windows
// would silently change (a trailing dot or space) and device names (CON, NUL, COM1...).
std::optional<fs::path> resolve_audio(const fs::path& root, const std::string& rel);

struct ByteRange {
    enum Kind { None, Bad, Ok } kind = None;
    std::uint64_t first = 0, last = 0;  // inclusive
};

// One range only: a player never asks for more.
ByteRange parse_range(const std::string* header, std::uint64_t size);

}  // namespace clew
