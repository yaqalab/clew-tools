// The server driven by another program over its standard input and output: Clew Desktop starts
// `clew-audio-server --control` hidden and is its only window, tray icon and updater. One JSON
// object per line each way, UTF-8.
//
// Requests: {"id": <any>, "cmd": "<name>", ...arguments}. Each gets one reply with the same id:
// {"id": ..., "ok": true, "result": ...} or {"id": ..., "ok": false, "error": "..."}.
// Events, unasked: {"event": "hello" | "status" | "log" | "bye", ...}. "status" comes after every
// change (at most about five a second), "log" with each activity line.
//
// The commands are what the tray program's window and menu do, nothing more (see kCommands in
// control.cpp). The secret and the two keys travel only on this pipe, to the program that started
// this one. The end of standard input ends the program, so it never outlives its parent.
#pragma once

#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "headless.h"
#include "json.h"

namespace clew {

class Service;

class Control {
public:
    using Write = std::function<void(const std::string& line)>;

    // `write` sends one line (it is called under a lock, from any thread).
    Control(Service& svc, Write write);
    ~Control();
    Control(const Control&) = delete;
    Control& operator=(const Control&) = delete;

    // One request line. False when it asked the program to end ("quit").
    bool handle(const std::string& line);

    // The service changed: a "status" event follows, coalesced with any others close behind it.
    void changed();
    // An activity line, sent as a "log" event.
    void log(const std::string& line);
    // Any event: {"event": name, ...fields}.
    void event(const std::string& name, json::Value fields);

    json::Value status() const;

private:
    json::Value run(const std::string& cmd, const json::Value& req);
    void send(const json::Value& v);
    void pump();

    Service& svc_;
    Write write_;
    std::mutex write_lock_;

    std::mutex m_;
    std::condition_variable cv_;
    bool dirty_ = false;
    bool quit_ = false;
    std::thread pump_;
};

// `--control [--config <file>] [--stt-program <exe>] [--analysis-program <exe>]`: starts as the
// tray program does (the settings' folders, dictation and analysis when they are on), says hello,
// then answers requests until "quit" or the end of standard input. Returns the exit code.
int run_control(const std::vector<std::string>& args, const Console& console);

}  // namespace clew
