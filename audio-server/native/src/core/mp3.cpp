#include "mp3.h"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <vector>

namespace clew {

namespace {

// Layer III bit rates in kbit/s by the header's index 1..14: MPEG1, then MPEG2 and 2.5.
const int kKbpsV1[14] = {32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320};
const int kKbpsV2[14] = {8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160};
const int kHzV1[3] = {44100, 48000, 32000};
const int kHzV2[3] = {22050, 24000, 16000};
const int kHzV25[3] = {11025, 12000, 8000};

}  // namespace

std::optional<Mp3Frame> mp3_frame_at(const std::uint8_t* buf, std::size_t len, std::size_t at) {
    if (at + 4 > len || buf[at] != 0xFF || (buf[at + 1] & 0xE0) != 0xE0) return std::nullopt;
    int version = (buf[at + 1] >> 3) & 3;  // 3 is MPEG1, 2 is MPEG2, 0 is MPEG2.5, 1 is reserved
    if (version == 1 || ((buf[at + 1] >> 1) & 3) != 1) return std::nullopt;  // reserved, or not layer III
    int bitrate_index = buf[at + 2] >> 4, rate_index = (buf[at + 2] >> 2) & 3;
    if (bitrate_index == 0 || bitrate_index == 15 || rate_index == 3) return std::nullopt;
    int padding = (buf[at + 2] >> 1) & 1;
    bool mono = (buf[at + 3] >> 6) == 3;
    int kbps = version == 3 ? kKbpsV1[bitrate_index - 1] : kKbpsV2[bitrate_index - 1];
    int hz = version == 3 ? kHzV1[rate_index] : version == 2 ? kHzV2[rate_index] : kHzV25[rate_index];
    std::size_t length = std::size_t((version == 3 ? 144 : 72) * kbps * 1000 / hz + padding);
    std::size_t side = version == 3 ? (mono ? 17 : 32) : (mono ? 9 : 17);
    std::size_t checksum = (buf[at + 1] & 1) ? 0 : 2;  // the protection bit is 0 when a checksum follows
    return Mp3Frame{length, 4 + checksum + side};
}

Mp3Head read_mp3_head(const std::filesystem::path& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::uint8_t lead[10] = {};
    f.read(reinterpret_cast<char*>(lead), 10);
    std::uint64_t start = 0;
    if (f.gcount() == 10 && std::memcmp(lead, "ID3", 3) == 0) {
        std::uint64_t size = std::uint64_t(lead[6] & 0x7F) << 21 | std::uint64_t(lead[7] & 0x7F) << 14 |
                             std::uint64_t(lead[8] & 0x7F) << 7 | std::uint64_t(lead[9] & 0x7F);
        start = 10 + size + ((lead[5] & 0x10) ? 10 : 0);
    }
    f.clear();
    f.seekg(std::streamoff(start));
    std::vector<std::uint8_t> buf(8192);
    f.read(reinterpret_cast<char*>(buf.data()), std::streamsize(buf.size()));
    std::size_t len = std::size_t(f.gcount());
    if (len == 0) return {};
    // The first frame follows the ID3 tag directly; a little junk before it is tolerated.
    for (std::size_t i = 0; i < std::min<std::size_t>(len, 2048); ++i) {
        auto frame = mp3_frame_at(buf.data(), len, i);
        if (!frame) continue;
        // A real frame is followed by another one, unless the file ends there.
        if (i + frame->length + 4 <= len && !mp3_frame_at(buf.data(), len, i + frame->length)) continue;
        struct Tag {
            Mp3Head::Kind kind;
            const char* text;
            std::size_t at;
        };
        const Tag tags[] = {{Mp3Head::Info, "Info", frame->tag_at},
                            {Mp3Head::Xing, "Xing", frame->tag_at},
                            {Mp3Head::Vbri, "VBRI", 36}};
        for (const auto& t : tags) {
            if (i + t.at + 4 <= len && std::memcmp(buf.data() + i + t.at, t.text, 4) == 0) {
                Mp3Head h;
                h.kind = t.kind;
                h.tag_start = start + i + t.at;
                h.frame_end = start + i + frame->length;
                return h;
            }
        }
        return {};
    }
    return {};
}

}  // namespace clew
