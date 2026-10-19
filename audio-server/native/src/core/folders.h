// Several folders of recordings, served as one (docs/22 section 11). The Python server
// (clew_audio_server.py) and the dictation server (tools/stt-server, transcribe.py) follow the
// same rules, so a path means the same file to all three.
//
// - Each folder is named after its last segment ("D:\Study A\audio" -> "audio"); a name already
//   taken, ignoring case, gets " 2", " 3"... in the list's order. Names are given over every
//   folder, on or off, so turning one off never renames another.
// - A listed file's path is "<name>/<path inside the folder>".
// - A path whose first segment is a folder's name is looked for in that folder only, and refused
//   while that folder is off. Any other path was saved when there was one folder: it is looked for
//   in every folder that is on, and used only when exactly one of them has it.
#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "files.h"

namespace clew {

struct ServedFolder {
    std::string name;
    std::filesystem::path root;  // canonical; empty when the folder cannot be opened
    bool enabled = true;
};

// The names of these folders (UTF-8 paths), in the same order.
std::vector<std::string> folder_names(const std::vector<std::string>& paths);

// Every audio file of the folders that are on, each path prefixed with its folder's name, folder
// by folder in the list's order. `truncated` past kMaxFiles in all.
std::vector<AudioFile> list_served(const std::vector<ServedFolder>& folders, bool* truncated);

// The file `path` names under the rules above, or nothing.
std::optional<std::filesystem::path> resolve_served(const std::vector<ServedFolder>& folders,
                                                    const std::string& path);

// The folder (on) that holds this canonical file, or nullptr.
const ServedFolder* folder_of(const std::vector<ServedFolder>& folders, const std::filesystem::path& file);

}  // namespace clew
