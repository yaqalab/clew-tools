# Clew audio server

Lets Clew list and play the audio files in your folders, without asking for the
file with the browser's chooser each visit. Design and security model: `docs/22-audio-server.md`.

**On Windows, use the native server instead:** `native/` (a tray icon, a settings window, an
installer, conversion with Windows' own codecs; no Python, no ffmpeg; `docs/22` §10). This Python
server is the one for macOS and Linux (installed with pipx: the top-level README), and a backup on
Windows. The two keep different secrets.

## Run it (development)

```
python tools/audio-server/clew_audio_server.py --folder D:\interviews
python tools/audio-server/clew_audio_server.py --gui
```

Python 3.10+, standard library only. The first start prints the **secret** — paste it into Clew
("From the audio server…"). Later starts remember the folders and the secret's hash. With the
`clew_audio/` package beside this file (always, when installed), the secret is also kept where it
can be shown again: the macOS Keychain, the desktop's keyring (`secret-tool`), else
`secrets.json` beside the settings, readable by this user only (`CLEW_KEYSTORE=file` forces it).

| Option (with `clew_audio/`) | |
|---|---|
| `--gui` | the settings window (Tkinter): the server runs while it is open; closing it stops the server |
| `--show-secret` | print the secret and the dictation key again |
| `--autostart on` / `gui` / `off` | start at login without / with the window (launchd on macOS; a systemd user service, or a desktop autostart entry with the window, on Linux) |
| `--dictation` | also run dictation (the `dictation` extra: `clew-tools[dictation]`); its settings are the window's Dictation tab, saved in `config.json` |
| `--download-model <name>` | fetch a dictation model (`tiny` … `large-v3`) into the models folder |
| `--check-update` | ask GitHub whether a newer version is out (a daily check also runs at start unless turned off in the window) |
| `--version` | the installed version |

| Option | |
|---|---|
| `--folder <path>` | a folder to serve: added to the remembered list, or turned on; repeatable |
| `--folder-off <path>` | turn a folder off: it keeps its name in Clew, nothing in it is served; repeatable |
| `--remove-folder <path>` | take a folder out of the list; repeatable |
| `--port <n>` | default 47821 |
| `--allow-origin <url>` | a hosted Clew's origin (`https://…`); remembered. `http://localhost:*` and `127.0.0.1` are always allowed |
| `--reset-secret` | a new secret; the old one stops working |
| `--config <file>` | where settings are kept; default `%APPDATA%\clew-audio-server\config.json`, else `$XDG_CONFIG_HOME` or `~/.config/clew-audio-server/config.json` |

It listens on 127.0.0.1 only.

## Settings in a `.env` file

Copy `.env.example` to `.env` **next to `clew_audio_server.py`** (or next to the `.exe`) and edit it,
so nothing has to be typed at each start. `.env` is ignored by git.

| Key | Same as | |
|---|---|---|
| `CLEW_AUDIO_FOLDER` | `--folder` | a folder to serve, several separated by `;` (`:` off Windows); a Windows path needs no quotes |
| `CLEW_AUDIO_PORT` | `--port` | default 47821 |
| `CLEW_AUDIO_ALLOW_ORIGINS` | `--allow-origin` | a hosted Clew's address(es), comma separated |
| `CLEW_AUDIO_LINK_HOURS` | | how long a stream link lasts; default 4 |
| `CLEW_AUDIO_CONFIG` | `--config` | where the secret's hash is kept |
| `CLEW_AUDIO_FFMPEG` | | path to ffmpeg, or `off`; default beside the program, else on `PATH` |

Order, strongest first: command-line option, `.env`, what the last start remembered, a question on the
console. Origins from `.env` are used but not written to the remembered settings, so deleting the line
removes it. The secret is not a `.env` setting on purpose: it is generated, shown once, and only its hash
is stored. `--env <file>` reads another file.

## Run it with Clew

From the repository root, `npm run dev` (or `dev.cmd` / `./dev.sh`) starts this server and the Clew dev
server together, output prefixed `[audio]` and `[clew]`. See `scripts/dev.mjs`.

## Formats

mp3, m4a, aac, wav, ogg, oga, opus, flac, weba, webm, mp4, 3gp, amr, **wma** (any letter case). The server
sends any of them, but **the browser has to play it**. Chrome, Firefox and Safari cannot play `.wma` or
`.amr` (Edge on Windows may play `.wma`, depending on its codecs: from what is known of those browsers).
**With ffmpeg, the server converts a `.wma` or `.amr` file to an mp3 the first time it is opened** (below),
and Clew plays that. Without ffmpeg Clew says the browser cannot play the file, and an mp3 or m4a copy
plays everywhere.

## Conversion with ffmpeg: long mp3 files, .wma and .amr

**.wma and .amr:** no browser plays them, so with ffmpeg the server converts each to a 128 kbit/s
constant-bit-rate mp3, once (checked in Firefox and Chromium on a .wma file made with ffmpeg's `wmav2`). Same
cache, same wait on first open (a short recording is seconds), same `CLEW_AUDIO_FFMPEG` setting, below.

**Long mp3 and Firefox.** Firefox seeks badly in a long mp3 that starts with a seek table (most do): after a click on a timestamp it can
play in silence for seconds. The server sends such a file in a form that seeks exactly (`docs/22-audio-server.md`
§8):

- a **constant-bit-rate** mp3 (tag `Info`): the table is sent as zeros, the same length, so every size and
  every `Range` is unchanged. Nothing to install.
- a **variable-bit-rate** mp3 (tag `Xing` or `VBRI`): converted **once** with **ffmpeg** to a 128 kbit/s
  constant-bit-rate mp3 and kept in a `cache` folder beside the config (at most 4 GB, least recently used
  first). The first time a recording is opened it waits for the conversion (80 s for 89 minutes of sound on the
  development machine). ffmpeg is looked for beside the program, then on `PATH`; `CLEW_AUDIO_FFMPEG` names it, or `off`
  turns the conversion off. Without ffmpeg the file is sent as it is.

The original file is never touched. The first sound of a re-encoded recording is about 25 ms later than in
the original.

## Run it (standalone, no Python needed)

```
powershell -ExecutionPolicy Bypass -File tools/audio-server/build_exe.ps1
```

builds `tools/audio-server/dist/clew-audio-server.exe` (about 8 MB) — copy that one file
anywhere and double-click it. Windows may warn on an unsigned program.

## Test

```
python -m unittest discover -s tools/audio-server -v
```

A real server on a free port and a real folder, no mocks.
