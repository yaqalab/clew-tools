// macOS and Linux entry point: headless only for now (no tray). NOT built or tested yet.
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <string>
#include <vector>

#include "../core/headless.h"
#include "../core/platform.h"

namespace {

volatile sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

}  // namespace

int main(int argc, char** argv) {
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    umask(077);  // the settings file holds the secret until a key store is wired in
    std::vector<std::string> args(argv + 1, argv + argc);
    clew::Console c;
    c.out = [](const std::string& s) {
        std::fputs((s + "\n").c_str(), stdout);
        std::fflush(stdout);
    };
    c.err = [](const std::string& s) {
        std::fputs((s + "\n").c_str(), stderr);
        std::fflush(stderr);
    };
    c.wait_for_stop = [] {
        while (!g_stop) pause();
    };
    return clew::run_headless(args, c);
}
