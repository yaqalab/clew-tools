// The first frame of an mp3: whether it carries a seek table, and where (docs/22 section 8).
//
// Firefox seeks by an mp3's Xing/Info table, so in a long recording it lands far from where it was
// asked and plays silence on the way. Without a table it works the position out from the size,
// which is exact for a constant bit rate. So an "Info" tag (constant bit rate) is sent as zeros of
// the same length, and a "Xing"/"VBRI" file (variable bit rate) is converted once.
#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace clew {

struct Mp3Head {
    enum Kind { None, Info, Xing, Vbri } kind = None;
    std::uint64_t tag_start = 0;
    std::uint64_t frame_end = 0;  // the first byte after the frame

    // The bytes sent as zeros, [first, end), for a constant-bit-rate file only.
    bool blank(std::uint64_t* first, std::uint64_t* end) const {
        if (kind != Info) return false;
        *first = tag_start;
        *end = frame_end;
        return true;
    }
};

struct Mp3Frame {
    std::size_t length;
    std::size_t tag_at;  // where a Xing/Info tag would sit in the frame
};

// A layer III frame header at `at`, or nothing.
std::optional<Mp3Frame> mp3_frame_at(const std::uint8_t* buf, std::size_t len, std::size_t at);

// Looks at the start of the file only; anything unexpected reads as None (sent as it is).
Mp3Head read_mp3_head(const std::filesystem::path& path);

}  // namespace clew
