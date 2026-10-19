#pragma once

#include <windows.h>

namespace clew::win {

// The tray icon and the settings window; returns when the user quits.
int run_tray(HINSTANCE instance);

// Asks a running tray app to quit, and waits for it (the uninstaller uses this).
int quit_running();

}  // namespace clew::win
