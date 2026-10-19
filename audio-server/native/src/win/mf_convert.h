#pragma once

#include <windows.h>

#include <string>

namespace clew::win {

// Converts `in` to a constant-bit-rate mp3 written to `out`. 0 on success; otherwise `why` says
// what failed. Meant for the sandboxed child process only.
int convert_with_media_foundation(const wchar_t* in, HANDLE out, std::string* why);

// Whether Media Foundation is installed (Windows "N" editions lack it without the Media Feature Pack).
bool media_foundation_present();

}  // namespace clew::win
