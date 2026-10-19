# Clew audio server (native)

Lets Clew list and play the audio files in your folders, without choosing the file
again on each visit and without uploading anything. A small Windows program (about 0.6 MB) with a
tray icon and a settings window; it needs no Python and no ffmpeg.

## Use it

1. Install it with `clew-audio-server-setup-<version>.exe` (no administrator rights needed).
2. The settings window opens. Press **Add…** and choose **the folder of your audio files**.
   The tray icon, Clew's "c" mark, shows in full: Clew can reach the files.
3. Press **Copy** next to the secret, and paste it into Clew under **"From the audio server…"**.

After that the server starts with Windows (if you left that ticked) and Clew finds it on its own.

Windows 11 first puts a new tray icon under the **^** beside the clock. To keep it in sight, drag it onto
the taskbar, or turn it on in *Settings → Personalization → Taskbar → Other system tray icons*; the
choice is kept when the program is updated.

| Tray icon | Means |
|---|---|
| the "c" mark in full | running; Clew can list and play the folder |
| with a round badge | converting a file (a .wma, .amr or long variable-bit-rate .mp3) |
| faded | stopped, or something needs you (the window says what) |

The mark is ink on a light taskbar and cream on a dark one, and follows a switch at once.

One click on the icon opens the settings; a right click has *Copy the secret*, *Copy the address*,
*Audio folders* (tick or untick one), *Open the audio folder*, *Stop / Start*, *Start with Windows*
and *Quit*. Closing the window keeps the server running.

### Several folders

Each project can keep its recordings in its own folder. Add each one with **Add…**; Clew lists the files
of every ticked folder, each under the folder's name ("study-a/p01.mp3"). **Untick** a folder you are
not working on: it stays in the list with its name, nothing in it is listed or played, and ticking it
again brings everything back, links in Clew included. A tick, Add… and Remove take effect at once, no
Save needed; the tray's *Audio folders* menu does the same without opening the window.

Two folders with the same last name ("…\Study A\audio", "…\Study B\audio") are shown as "audio" and
"audio 2", in the list's order. Removing a folder does not touch it or its recordings; a recording
linked from it plays again once it is added back. Recordings linked before there could be several
folders keep working while only one ticked folder has that file.

### Settings

| Setting | |
|---|---|
| Folders | the folders whose audio files Clew may list and play (subfolders too); ticked ones are served |
| Port | 47821 unless another program uses it |
| Play links last | how long one play link works (default 4 hours); Clew asks for a new one by itself |
| Other Clew addresses | a Clew hosted on the web; `https://clew.alefa.net` is there after a first install and can be removed; a Clew on this computer is always allowed |
| Secret | Show / Copy / New secret. A new secret stops the old one at once |
| Convert … | converts .wma, .amr and long variable-bit-rate .mp3 so every browser plays and seeks them |
| Prepare … in the background | converts .wma and .amr files as soon as Clew lists the folder, so they open at once. The file you open always goes first: preparing stops, waits, and picks up again afterwards |
| ffmpeg (optional) | only used when Windows itself cannot convert a file |
| Cache limit | converted copies are kept up to this size in each folder, the oldest removed first |

The **Updates** tab shows the version, checks for a new one once a day (or on *Check now*), and
installs it on a press; the update keeps every setting and starts the program again by itself.

Settings live in `%APPDATA%\clew-audio-server\settings.json` (the update check's in
`update.json` beside it). **Converted copies stay beside your
recordings**, in a `.clew-converted` folder inside the recording's own audio folder: they are as private as the
interviews, so they stay wherever you chose to keep those (an encrypted drive, a synced folder). Clew
never lists that folder and it cannot be asked for; it holds a README saying what it is, and deleting
it is safe (a copy is made again when needed). Copies are named by a hash, not by the recording's name.
Your original files are never changed. Uninstalling leaves the copies where they are.

### Formats

mp3, m4a, aac, wav, ogg, oga, opus, flac, weba, webm, mp4, 3gp, amr, wma. Browsers cannot play .wma
or .amr, and Firefox seeks badly in a long variable-bit-rate mp3, so those are **converted once with
Windows' own codecs** to a 128 kbit/s mp3 (the first time takes a moment: a few seconds for an hour of
speech). Windows decodes WMA (all versions, also WMA Voice), MP3, AAC, FLAC, Opus and AMR-NB; it has no
AMR-WB decoder. A file Windows cannot read is tried with ffmpeg if you chose one, else sent as it is.

Windows "N" editions lack the codecs until the free *Media Feature Pack* is installed.

## Safety

- **This computer only.** It listens on 127.0.0.1, never on the network, and refuses requests that
  name another host (a web page cannot reach it through a trick address). The port is held
  exclusively.
- **A secret.** Listing the folder and asking for a play link need the secret. It is generated by
  Windows' random generator and kept **encrypted for your Windows account** (DPAPI): another account,
  or the settings file copied elsewhere, cannot read it. Wrong guesses slow the server down. Copying
  it keeps it out of Windows' clipboard history.
- **Play links** are signed for one file and a few hours, with a key that exists only while the
  program runs: quitting it revokes every link.
- **Only the folder.** Nothing outside it is listed or sent, however the request is written (`..`,
  other drives, links and junctions that lead out, hidden files, Windows device names, alternate data
  streams).
- **Only Clew.** Web pages other than a Clew on this computer and the addresses you add are refused.
- **Conversion runs in a sandbox:** a separate process at *low integrity* (it can read your files but
  write almost nowhere), inside a job object (one process, 1.5 GB of memory at most, 30 minutes at
  most, no clipboard or desktop access), with exploit mitigations on (no injected or generated code,
  no DLLs from untrusted places). A damaged file that makes a codec misbehave cannot reach the server
  or your documents.
- **Little to steal.** The activity list keeps method, path and status in memory only; never a link's
  signature, never the secret.
- **Outgoing connections, only these:** a dictation model download from huggingface.co when you
  press Download; once a day, if *Check for a new version* is on (Updates tab), a request to
  github.com/yaqalab/clew-tools for the latest version number; and the new installer's download
  when you press Install. None carries anything about you or your files.
- **Updates install only on your press,** and only an installer that matches the release's size and
  SHA-256 and is signed by the same certificate as the program already installed; otherwise it is
  deleted and nothing changes (`docs/32` §3).
- The program, the installer and the uninstaller are **signed by YaqaLab** with a self-signed
  certificate: Properties → Digital Signatures shows it, and any change to a file after signing breaks
  the signature. Windows does not trust a self-signed signer, so SmartScreen still warns on first run
  ("More info" → "Run anyway"). Only a certificate from a public authority removes that warning.

## For developers

Design and API: `docs/22-audio-server.md` (§10 for this program). The API is the same as the Python
server's (`tools/audio-server/`), which stays as a backup and for macOS and Linux.

```
tools\audio-server\native\build.cmd                   build + unit tests (Visual Studio 2022 C++ tools)
python -m unittest discover -s tools\audio-server\native\test -v     the real program over HTTP
tools\audio-server\native\installer\build-installer.cmd             build + installer (Inno Setup 6)
set CLEW_AUDIO_SERVER_EXE=...\clew-audio-server.exe && npm run smoke:audio-server   real Clew in Chromium
```

`clew-audio-server.exe --headless --folder <dir> [--folder <dir>…] [--folder-off <dir>] [--port n] …` runs
without a window (`--help` lists the options; they apply to that run only, and `--folder` replaces the
window's list for that run). `CLEW_AUDIO_LANG=ja` or `en` forces the window's language.

| Folder | |
|---|---|
| `src/core/` | portable C++17: HTTP server, links, folder rules, mp3 tags, settings, conversion cache, headless mode |
| `src/win/` | Windows: DPAPI, Media Foundation conversion, the sandbox, tray icon and window, en/ja strings |
| `src/posix/` | macOS and Linux: headless only, ffmpeg-only conversion, secret in a 0600 file. **Not built or tested yet** |
| `res/` | dialog, manifest, icons (`make_icons.py` draws them) |
| `test/` | `unit_tests.cpp` (SHA-256/HMAC vectors, paths, ranges, JSON, settings) and `test_native.py` |

No third-party code and no libraries beyond what Windows ships. The version is written once, in
`tools/VERSION`: CMake makes `version.h` from it (the program, `res/app.rc`), and
`build-installer.cmd` passes it to the installer.

### Signing

```
powershell -ExecutionPolicy Bypass -File tools\audio-server\native\installer\make-cert.ps1
```

makes, once, a self-signed code-signing certificate `CN=YaqaLab` (RSA 3072, 10 years) in this Windows
user's store, with a **non-exportable** private key: it never leaves the store and is never in the
repository. `build-installer.cmd` then signs the program, the installer and the uninstaller with it;
without it, it builds unsigned. The public part is written to `dist\yaqalab-codesign.cer`: importing it
into *Trusted People* / *Trusted Root* on a machine makes that machine trust the signature (a choice for
each machine's owner, not done by the installer). `CLEW_SIGN_TIMESTAMP=<RFC 3161 URL>` adds a timestamp,
which sends the files' hashes to that server; it is off unless set. Another build machine needs its own
certificate (the key cannot be copied), so its signatures carry a different thumbprint.

### Other systems

The core compiles with standard C++17 and BSD sockets. To finish macOS or Linux: the secret in the
Keychain / Secret Service (`seal_secret`), conversion with the system's codecs where there are some
(macOS AudioToolbox decodes, but has no mp3 encoder: it would produce m4a), a sandbox for the
converter, and a tray (NSStatusItem / AppIndicator). Until then the Python server is the one to use there.
