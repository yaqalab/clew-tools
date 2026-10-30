// What the server does so that a file plays and seeks in every browser: reads each mp3's first frame
// once, and converts, once, into a cache, a variable-bit-rate mp3 to a constant-bit-rate one and a
// format no browser plays (.wma, .amr) to an mp3. The conversion itself is the system's
// (platform::convert_to_mp3: Media Foundation on Windows); no ffmpeg.
//
// The cache lives inside the served folder (kCacheFolder), not in the user's profile: the copies are
// as sensitive as the interviews, so they stay wherever the researcher chose to keep those (an
// encrypted drive, a synced folder...). Its name starts with a dot, so the folder rules never list
// it or serve from it directly.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>

#include <vector>

#include "mp3.h"
#include "platform.h"

namespace clew {

namespace fs = std::filesystem;

class ConvertCache {
public:
    using ConvertFn = std::function<bool(const fs::path& in, const fs::path& out,
                                         const platform::ConvertControl& control, std::string* why)>;
    // Called with the file's name when a conversion starts, and with "" when none is running.
    using BusyFn = std::function<void(const std::string& name)>;
    // A line for the activity list.
    using NoteFn = std::function<void(const std::string& line)>;

    // `dir` not empty: every copy goes there (the tests). Otherwise each copy goes to kCacheFolder
    // inside the served folder that holds its recording (set_roots).
    ConvertCache(fs::path dir, ConvertFn convert);

    // The served folders (canonical) from now on; a file in none of them is not converted.
    void set_roots(std::vector<fs::path> roots);
    // Where the copy of `file` goes; empty: nowhere.
    fs::path dir_for(const fs::path& file) const;
    // Every place copies go.
    std::vector<fs::path> dirs() const;
    ~ConvertCache();

    void set_enabled(bool on) { enabled_ = on; }
    bool enabled() const { return enabled_ && bool(convert_); }
    void set_limit_bytes(std::uint64_t n) { limit_ = n; }
    void set_prepare(bool on) { prepare_ = on; }
    void set_on_busy(BusyFn fn);
    void set_on_note(NoteFn fn);

    Mp3Head head_of(const fs::path& path);
    // Whether `path` is converted before it is sent (a variable-bit-rate mp3, .wma or .amr).
    bool needs(const fs::path& path);
    // The converted copy, if one was made already.
    std::optional<fs::path> converted(const fs::path& path);
    // The converted copy, made first if needed (a long file takes a while); nothing when the file
    // needs none, conversion is off, or it failed. Concurrent calls for one file share one run.
    // This is the file someone is opening: it goes first. Preparing another file ahead is stopped
    // and put back in the queue, and nothing is prepared until every such call has finished.
    std::optional<fs::path> convert(const fs::path& path);

    // Converts these ahead, one at a time, in the background, while the cache is under half its
    // limit. Only files no browser plays at all are queued (a variable-bit-rate mp3 still plays).
    void prepare(const std::vector<fs::path>& paths);

    std::uint64_t bytes_on_disk() const;
    void clear();

private:
    fs::path target_of(const fs::path& path, const fs::path& dir) const;
    std::optional<fs::path> run(const fs::path& path, bool background);
    void note(const std::string& line);
    void trim(const fs::path& keep);
    void worker();
    void busy(const std::string& name);

    fs::path forced_dir_;
    std::vector<fs::path> roots_;
    ConvertFn convert_;
    std::atomic<bool> enabled_{true};
    std::atomic<bool> prepare_{true};
    std::atomic<std::uint64_t> limit_{4ull * 1024 * 1024 * 1024};

    mutable std::mutex lock_;
    std::map<std::string, std::shared_ptr<std::mutex>> once_;
    struct Seen {
        std::int64_t mtime;
        std::uint64_t size;
        Mp3Head head;
    };
    std::map<fs::path, Seen> heads_;
    std::set<fs::path> failed_;  // not retried by the background queue in this run
    BusyFn on_busy_;
    NoteFn on_note_;
    int running_ = 0;
    std::atomic<int> opening_{0};          // conversions of files someone is opening
    std::atomic<bool> stop_preparing_{false};
    fs::path preparing_;                   // the file being prepared ahead, under lock_
    bool preparing_runs_ = false;          // its converter process is running, under lock_
    std::condition_variable preparing_ended_;

    std::mutex queue_lock_;
    std::condition_variable queue_cv_;
    std::deque<fs::path> queue_;
    std::set<fs::path> queued_;
    bool stopping_ = false;
    std::thread worker_;
};

// The extensions no browser plays.
bool unplayable(const fs::path& path);

// The cache's name inside the served folder.
constexpr const char* kCacheFolder = ".clew-converted";

}  // namespace clew
