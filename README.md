# Clew tools

Small programs that let [Clew](https://clew.alefa.net), a research workspace that runs in your
browser, use the recordings and computing power of **your own computer**. Nothing is
uploaded: Clew in the browser talks to them at `127.0.0.1`, which means "this computer".

| Program | What it does for Clew | Systems |
|---|---|---|
| **Clew audio server** | Plays and lists the interview recordings in folders you choose, so you don't have to pick the file again each visit | Windows (tray app); macOS and Linux (Python script) |
| **Dictation** (optional) | Speech to text with Whisper, on your computer: dictation while writing, transcription of recordings | Windows, installed together with the audio server |

**[Download the latest version](https://github.com/yaqalab/clew-tools/releases/latest)**
(`clew-audio-server-setup-<version>.exe`).

## Install on Windows

1. Download `clew-audio-server-setup-<version>.exe` from the
   [latest release](https://github.com/yaqalab/clew-tools/releases/latest).
2. Run it. You don't need administrator rights. Choose **Audio server and dictation**, or
   **Audio server only** if you won't use dictation (the dictation part is about 200 MB).
3. Windows may show **"Windows protected your PC"** the first time you run it. The installer is
   signed by YaqaLab, but the certificate is one YaqaLab made itself, not one bought from a
   certificate authority, so Windows doesn't recognise the name yet. Press **More info**, check
   that the publisher says **YaqaLab**, then **Run anyway**.
   [Checking a download](#checking-a-download) shows how to confirm the file is the published one.
4. The settings window opens. Under **Folders**, press **Add…** and choose the folder that holds
   your recordings.
5. Press **Copy** next to the secret. In Clew, press **From the audio server…** (in the audio
   bar, or in *Recordings*) and paste the secret.

For dictation, open the **Dictation** tab, download a model (`small` suits most computers), press
**Copy** next to the key, and paste it in Clew under **Settings › Dictation › This computer**.

The audio server then starts with Windows (you can untick that), and Clew finds it by itself.
Its icon sits by the clock; one click opens the settings.

## Install on macOS and Linux

The audio server is a Python program there, with the same features as on Windows and an optional
settings window. It needs Python 3.10 or newer, installed with [pipx](https://pipx.pypa.io)
(`brew install pipx` on macOS, `sudo apt install pipx` on Ubuntu and Debian):

```
pipx install "clew-tools @ git+https://github.com/yaqalab/clew-tools"
clew-audio-server --gui
```

1. In the window, press **Add…** under *Folders* and choose the folder of your recordings.
2. Press **Copy** next to the secret, and paste it in Clew under **From the audio server…**.
3. Tick **Start at login** if you want it there each time.

The window needs Tkinter, which comes with Python from python.org. With Homebrew's Python, add
`brew install python-tk`; on Ubuntu and Debian, `sudo apt install python3-tk`. Without a window,
the server runs in a terminal: `clew-audio-server --folder ~/interviews` prints the secret, and
`clew-audio-server --autostart on` starts it at login.

**Dictation** needs more libraries (about 1 GB, from PyPI) and then a model:

```
pipx install --force "clew-tools[dictation] @ git+https://github.com/yaqalab/clew-tools"
```

Then download a model in the window's **Dictation** tab (`small` suits most computers), copy its
key into Clew under **Settings › Dictation › This computer**, and press **Start**.

**Updating:** `pipx upgrade clew-tools`. The window's **Updates** tab tells you when there is a new
version; it never installs anything by itself.

On macOS nothing here needs Apple's approval: pipx installs the source, and Python runs it. All
options: [audio-server/README.md](audio-server/README.md).

## How clew.alefa.net uses these programs

Clew is a web page: everything you write stays in your browser, encrypted, and syncs only to
your own Google Drive or Dropbox. A web page cannot read the folders on your disk by itself, so
for recordings and speech recognition it asks these programs, on your computer:

- **Audio:** Clew asks the audio server (`http://127.0.0.1:47821`) for the list of recordings and
  a link to play one. The audio goes from the program to your browser directly, inside your
  computer. Your recordings are never uploaded.
- **Dictation and transcription:** Clew sends your voice, or a recording, to the dictation server
  (`http://127.0.0.1:47822`) and gets the text back. Speech recognition runs on your processor
  (or your NVIDIA graphics card, if you have one). Nothing is sent to a speech service on the
  internet.

Each request from Clew carries the secret (audio) or the key (dictation) that you pasted. Your
browser may ask once whether clew.alefa.net may connect to apps on this device; allow it for
Clew.

## What keeps it safe

These programs are built to do one narrow job and refuse everything else.

**Only this computer.** Both servers listen on `127.0.0.1` only, so other computers on your
network or the internet can't reach them. A request that names another host is refused, which
stops a web page from reaching them through a disguised address.

**Only Clew, and only with your secret.** Web pages other than Clew (clew.alefa.net, or a Clew
running on this computer) are refused by the browser's own rules. Listing your recordings needs
the secret; dictation needs its key. Both are made from Windows' secure random generator and
stored **encrypted for your Windows account** (DPAPI), so another account, or a copy of the
settings file on another computer, can't read them. Wrong guesses slow the server down. When
you copy the secret, it's kept out of Windows' clipboard history and cloud clipboard. **New
secret** makes the old one stop working at once. On macOS and Linux the server checks the secret
against a scrypt hash, and keeps the secret itself in the **macOS Keychain** or the **desktop's
keyring** (GNOME Keyring, KWallet), or, where there is none, in a file only your account can read.

**Only your folders.** The audio server sends files from the folders you added (and their
subfolders), and nothing else, however a request is written: no `..`, no other drives, no
shortcuts or links that lead outside, no hidden files. You can untick a folder to pause it.

**Play links expire.** A link to play a recording works for one file, for a few hours (4 by
default). It is signed with a key that exists only while the program runs: quitting the program
cancels every link.

**Your recordings are never changed.** Some formats browsers can't play (.wma, .amr) are
converted once to an mp3 copy, using Windows' own codecs. The copy is kept beside the recording,
in a `.clew-converted` folder, so it stays wherever you chose to keep your data (an encrypted
drive, for example). Deleting that folder is safe.

**Conversion runs in a sandbox.** A damaged recording could make a codec misbehave, so conversion
runs in a separate, locked-down process: low integrity (it can write almost nowhere), one process
at most, limited memory and time, no clipboard or desktop, and Windows' exploit protections on.
On macOS and Linux, conversion uses ffmpeg (if you have it) as a separate process at the lowest
priority, with your account's rights, as any media player on your computer does.

**Little to find.** The activity list in the window keeps the method, path and status of recent
requests, in memory only, never a secret or a link's signature. Nothing about your work is
written to a log file.

**Network connections it makes.** The programs connect to the internet only for these:

| When | To | What is sent |
|---|---|---|
| You press **Download** for a dictation model | huggingface.co (where the Whisper models are published) | The model's name; nothing about you |
| Once a day, if **Check for a new version** is on (Updates tab) | github.com/yaqalab/clew-tools | A request for the latest version number |
| You press **Install** for a new version | github.com/yaqalab/clew-tools | A request for the installer file |

Like any website, these services see your computer's internet address. No recording, text,
folder name or secret is ever sent, and there is no analytics or telemetry.

## Updates

The **Updates** tab checks once a day whether a new version exists (you can turn this off, or
press **Check now**). When there is one, the tray tells you once. Nothing is downloaded or
installed until you press **Install**.

Before the new installer runs, the program checks it three ways:

1. its size and SHA-256 fingerprint match what the release lists;
2. its digital signature is intact (any change to the file breaks it);
3. it is signed by **the same YaqaLab certificate** as the program already on your computer.

If any check fails, the file is deleted and nothing changes. When all pass, the server stops for
a moment, the installer runs, and the new version starts by itself. Your folders, secret,
dictation key and downloaded models are kept, and Clew reconnects on its own.

## Using it safely

- **Download only from this page** ([releases](https://github.com/yaqalab/clew-tools/releases)),
  or let the program update itself.
- **Treat the secret and the dictation key like a password.** Paste them only into Clew. If you
  think one was seen by someone else, press **New secret** or **New key**, then paste the new one
  into Clew.
- **Add only the folders Clew needs**, and untick the ones you're not working on.
- **Keep the address list short.** Under *Other Clew addresses*, keep only the Clew you use
  (`https://clew.alefa.net` is there by default). Remove any address you don't recognise.
- **Quit the program** (right-click the tray icon → *Quit*) when you don't need it, for example
  on a shared computer. Quitting also cancels every play link.
- **Keep your recordings where you want them protected.** The converted copies follow them, so an
  encrypted drive protects both.

## Checking a download

Each release lists the SHA-256 fingerprint of the installer in `SHA256SUMS.txt`. In PowerShell:

```powershell
Get-FileHash .\clew-audio-server-setup-1.1.0.exe -Algorithm SHA256
```

The value must match the line in `SHA256SUMS.txt`. You can also right-click the installer →
**Properties** → **Digital Signatures**: the signer is **YaqaLab**.

`yaqalab-codesign.cer` in each release is the public part of the signing certificate. If you
want Windows to recognise YaqaLab on your computer, you can import it into *Trusted People*.
That's your choice; neither the program nor the installer does it for you.

## Reporting a problem

For a bug, open an [issue](https://github.com/yaqalab/clew-tools/issues). For a security
problem, please write to alefa-support@googlegroups.com instead of opening a public issue.

## For developers

| Folder | |
|---|---|
| [audio-server/native/](audio-server/native/) | The Windows program: C++17, Windows APIs only, no third-party code. Tray, settings window, installer (Inno Setup) |
| [audio-server/](audio-server/) | The Python audio server (standard library only): macOS, Linux, or a backup on Windows. `clew_audio_server.py` runs alone; the `clew_audio/` package beside it adds the window (Tkinter), the keystore, start at login, dictation and the update check |
| [pyproject.toml](pyproject.toml) | The `clew-tools` package pipx installs (the audio server, and with `[dictation]` the dictation server and faster-whisper) |
| [stt-server/](stt-server/) | The dictation server: Python, faster-whisper, packed with PyInstaller for Windows |

Build and test instructions are in each folder's README. The version is in [VERSION](VERSION).
Code comments and READMEs refer to design notes as `docs/NN-…`: those notes belong to Clew's own
repository, which is not public; what they decided is described in the code beside them.
Changes from version to version: [CHANGELOG.md](CHANGELOG.md).

## Licence

The code in this repository is under the [MIT licence](LICENSE). The Windows installer's
dictation part also contains third-party software under its own licences, among them FFmpeg
libraries and the x264 and x265 encoders (GPL-2.0-or-later) that come with PyAV. They are listed
in `THIRD-PARTY-NOTICES.txt`, installed beside the dictation server and attached to each
release, together with where to get their source code. The audio server alone contains no
third-party code.
