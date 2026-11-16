// The server without a window: for `npm run dev`, the tests, and systems without a tray (yet).
// Command-line options apply to that run only; only a newly made secret is written to the settings.
#pragma once

#include <functional>
#include <string>
#include <vector>

namespace clew {

struct Console {
    std::function<void(const std::string&)> out;   // a line for the user
    std::function<void(const std::string&)> err;   // a log line
    std::function<void()> wait_for_stop;           // returns on Ctrl+C or a stop signal
    // One line of standard input, without its line ending; false at its end (--control only).
    std::function<bool(std::string*)> read_line;
};

// `args` without the program's name. Returns the exit code.
int run_headless(const std::vector<std::string>& args, const Console& console);

extern const char* const kHeadlessHelp;

}  // namespace clew
