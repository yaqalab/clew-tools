#include "convert_cache.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <vector>

#include "files.h"
#include "sha256.h"
#include "text.h"

namespace clew {

namespace {

constexpr int kConvertKbps = 128;

std::string hex(const Digest& d, std::size_t n) {
    static const char* h = "0123456789abcdef";
    std::string out;
    for (std::size_t i = 0; i < n && i < d.size(); ++i) {
        out += h[d[i] >> 4];
        out += h[d[i] & 15];
    }
    return out;
}

}  // namespace

bool unplayable(const fs::path& path) {
    std::string ext = to_lower(path_to_utf8(path.extension()));
    return ext == ".wma" || ext == ".amr";
}

ConvertCache::ConvertCache(fs::path dir, ConvertFn convert)
    : forced_dir_(std::move(dir)), convert_(std::move(convert)) {
    worker_ = std::thread([this] { worker(); });
}

void ConvertCache::set_roots(std::vector<fs::path> roots) {
    std::lock_guard<std::mutex> g(lock_);
    roots_ = std::move(roots);
}

fs::path ConvertCache::dir_for(const fs::path& file) const {
    std::lock_guard<std::mutex> g(lock_);
    if (!forced_dir_.empty()) return forced_dir_;
    for (const auto& root : roots_)
        if (inside(root, file)) return root / kCacheFolder;
    return {};
}

std::vector<fs::path> ConvertCache::dirs() const {
    std::lock_guard<std::mutex> g(lock_);
    if (!forced_dir_.empty()) return {forced_dir_};
    std::vector<fs::path> out;
    for (const auto& root : roots_) out.push_back(root / kCacheFolder);
    return out;
}

namespace {

// Said once, inside the cache folder, for whoever opens it.
const char kReadme[] =
    "Clew audio server: converted copies\r\n"
    "\r\n"
    "These are copies of recordings in the folder above, converted so that every browser can play\r\n"
    "them (.wma and .amr files, and long variable-bit-rate .mp3 files). The originals are untouched.\r\n"
    "They are as private as the recordings themselves, which is why they are kept here, beside them.\r\n"
    "\r\n"
    "Deleting this folder is safe: a copy is made again when the recording is next opened in Clew.\r\n";

void write_readme(const fs::path& dir) {
    std::error_code ec;
    auto file = dir / "README.txt";
    if (fs::exists(file, ec)) return;
    std::ofstream(file, std::ios::binary) << kReadme;
}

}  // namespace

ConvertCache::~ConvertCache() {
    {
        std::lock_guard<std::mutex> g(queue_lock_);
        stopping_ = true;
        queue_.clear();
    }
    queue_cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

void ConvertCache::set_on_busy(BusyFn fn) {
    std::lock_guard<std::mutex> g(lock_);
    on_busy_ = std::move(fn);
}

void ConvertCache::set_on_note(NoteFn fn) {
    std::lock_guard<std::mutex> g(lock_);
    on_note_ = std::move(fn);
}

void ConvertCache::note(const std::string& line) {
    NoteFn fn;
    {
        std::lock_guard<std::mutex> g(lock_);
        fn = on_note_;
    }
    if (fn) fn(line);
}

void ConvertCache::busy(const std::string& name) {
    BusyFn fn;
    {
        std::lock_guard<std::mutex> g(lock_);
        fn = on_busy_;
    }
    if (fn) fn(name);
}

Mp3Head ConvertCache::head_of(const fs::path& path) {
    std::error_code ec;
    auto size = fs::file_size(path, ec);
    if (ec) return {};
    auto mtime = fs::last_write_time(path, ec).time_since_epoch().count();
    if (ec) return {};
    {
        std::lock_guard<std::mutex> g(lock_);
        auto it = heads_.find(path);
        if (it != heads_.end() && it->second.mtime == mtime && it->second.size == size) return it->second.head;
    }
    Mp3Head head = read_mp3_head(path);
    std::lock_guard<std::mutex> g(lock_);
    heads_[path] = Seen{std::int64_t(mtime), size, head};
    return head;
}

bool ConvertCache::needs(const fs::path& path) {
    if (!enabled()) return false;
    if (unplayable(path)) return true;
    if (to_lower(path_to_utf8(path.extension())) != ".mp3") return false;
    auto kind = head_of(path).kind;
    return kind == Mp3Head::Xing || kind == Mp3Head::Vbri;
}

fs::path ConvertCache::target_of(const fs::path& path, const fs::path& dir) const {
    std::error_code ec;
    auto size = fs::file_size(path, ec);
    auto mtime = fs::last_write_time(path, ec).time_since_epoch().count();
    std::string key = path_to_utf8(path) + "\n" + std::to_string(mtime) + "\n" + std::to_string(size) + "\n" +
                      std::to_string(kConvertKbps) + "\nsystem-codec-1";
    return dir / (hex(sha256(key), 16) + ".mp3");
}

std::optional<fs::path> ConvertCache::converted(const fs::path& path) {
    fs::path d = dir_for(path);
    if (d.empty()) return std::nullopt;
    std::error_code ec;
    auto hit = target_of(path, d);
    // Its time is not touched on use: in a synced folder that would upload the copy again at every
    // play. So the trimming removes the oldest copies first.
    if (!fs::is_regular_file(hit, ec)) return std::nullopt;
    return hit;
}

std::optional<fs::path> ConvertCache::convert(const fs::path& path) {
    if (!needs(path)) return std::nullopt;
    ++opening_;
    {
        // Preparing another file ahead gives way at once, and its converter has ended before this one
        // starts: one conversion at a time. The same file is left to finish: run() below waits for it
        // and takes its copy.
        std::unique_lock<std::mutex> g(lock_);
        if (!preparing_.empty() && preparing_ != path) {
            stop_preparing_ = true;
            preparing_ended_.wait_for(g, std::chrono::seconds(5), [this] { return !preparing_runs_; });
        }
    }
    auto copy = run(path, false);
    {
        // Under the queue's lock, so the worker cannot miss it between its check and its wait.
        std::lock_guard<std::mutex> g(queue_lock_);
        --opening_;
    }
    queue_cv_.notify_all();
    return copy;
}

std::optional<fs::path> ConvertCache::run(const fs::path& path, bool background) {
    fs::path d = dir_for(path);
    if (d.empty()) return std::nullopt;
    auto target = target_of(path, d);
    std::shared_ptr<std::mutex> once;
    {
        std::lock_guard<std::mutex> g(lock_);
        auto& slot = once_[path_to_utf8(target.filename())];
        if (!slot) slot = std::make_shared<std::mutex>();
        once = slot;
    }
    {
        std::lock_guard<std::mutex> g(*once);
        if (auto hit = converted(path)) return hit;
        std::error_code ec;
        fs::create_directories(d, ec);
        write_readme(d);
        auto part = target;
        part.replace_extension(".part");
        {
            std::lock_guard<std::mutex> g2(lock_);
            ++running_;
        }
        busy(path_to_utf8(path.filename()));
        std::string why;
        platform::ConvertControl control;
        control.background = background;
        control.cancel = background ? &stop_preparing_ : nullptr;
        if (background) {
            std::lock_guard<std::mutex> g2(lock_);
            preparing_runs_ = true;
        }
        bool ok = convert_(path, part, control, &why) && fs::file_size(part, ec) > 0 && !ec;
        bool gave_way = !ok && control.cancelled();
        if (background) {
            {
                std::lock_guard<std::mutex> g2(lock_);
                preparing_runs_ = false;
            }
            preparing_ended_.notify_all();
        }
        if (ok) {
            fs::rename(part, target, ec);
            ok = !ec;
        }
        bool idle;
        {
            std::lock_guard<std::mutex> g2(lock_);
            idle = --running_ == 0;
            if (!ok && !gave_way) failed_.insert(path);
        }
        if (idle) busy("");
        if (!ok) {
            fs::remove(part, ec);
            return std::nullopt;
        }
    }
    trim(target);
    return target;
}

void ConvertCache::trim(const fs::path& keep) {
    struct Item {
        fs::file_time_type at;
        std::uint64_t size;
        fs::path path;
    };
    std::vector<Item> items;
    std::uint64_t total = 0;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(keep.parent_path(), ec)) {
        if (e.path().extension() != ".mp3") continue;
        std::error_code ec2;
        Item it{e.last_write_time(ec2), e.file_size(ec2), e.path()};
        if (ec2) continue;
        total += it.size;
        items.push_back(it);
    }
    std::sort(items.begin(), items.end(), [](const Item& a, const Item& b) { return a.at < b.at; });
    for (const auto& it : items) {
        if (total <= limit_) break;
        if (it.path == keep) continue;
        std::error_code ec2;
        if (fs::remove(it.path, ec2)) total -= it.size;
    }
}

void ConvertCache::prepare(const std::vector<fs::path>& paths) {
    if (!enabled() || !prepare_) return;
    {
        std::lock_guard<std::mutex> g(queue_lock_);
        for (const auto& p : paths) {
            if (!unplayable(p) || queued_.count(p)) continue;
            {
                std::lock_guard<std::mutex> g2(lock_);
                if (failed_.count(p)) continue;
            }
            queued_.insert(p);
            queue_.push_back(p);
        }
    }
    queue_cv_.notify_one();
}

void ConvertCache::worker() {
    while (true) {
        fs::path next;
        {
            // Nothing is prepared while a file someone opened is converting.
            std::unique_lock<std::mutex> g(queue_lock_);
            queue_cv_.wait(g, [this] { return stopping_ || (!queue_.empty() && opening_ == 0); });
            if (stopping_) return;
            next = queue_.front();
            queue_.pop_front();
        }
        {
            std::lock_guard<std::mutex> g(lock_);
            preparing_ = next;
            stop_preparing_ = false;
        }
        // Checked after `preparing_` is set: an opening that began before is seen here, one that
        // begins after sees `preparing_` and stops this one (both go through lock_).
        bool put_back = opening_ > 0;
        if (!put_back && enabled() && bytes_on_disk() < limit_ / 2 && !converted(next)) {
            run(next, true);
            if (stop_preparing_) {
                put_back = true;
                note("preparing " + path_to_utf8(next.filename()) + " paused for the file being opened");
            }
        }
        {
            std::lock_guard<std::mutex> g(lock_);
            preparing_.clear();
        }
        std::lock_guard<std::mutex> g(queue_lock_);
        if (put_back && !stopping_)
            queue_.push_front(next);  // resumed when the opened files are ready
        else
            queued_.erase(next);
    }
}

std::uint64_t ConvertCache::bytes_on_disk() const {
    std::uint64_t total = 0;
    for (const auto& d : dirs()) {
        std::error_code ec;
        for (const auto& e : fs::directory_iterator(d, ec)) {
            std::error_code ec2;
            if (e.is_regular_file(ec2) && e.path().extension() == ".mp3") total += e.file_size(ec2);
        }
    }
    return total;
}

void ConvertCache::clear() {
    for (const auto& d : dirs()) {
        std::error_code ec;
        for (const auto& e : fs::directory_iterator(d, ec)) {
            auto ext = e.path().extension();
            if (ext == ".mp3" || ext == ".part") {
                std::error_code ec2;
                fs::remove(e.path(), ec2);
            }
        }
    }
    std::lock_guard<std::mutex> g(lock_);
    failed_.clear();
}

}  // namespace clew
