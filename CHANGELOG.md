# Changes

Each section is the text of that version's release on GitHub.

## 1.1.0 — 2026-10-19

The first public release.

- **Updates from the program.** A new **Updates** tab checks once a day for a new version (you can
  turn this off). Nothing is installed until you press **Install**; the installer must match the
  release's size and SHA-256 fingerprint and carry the same YaqaLab signature, or it is deleted.
- **Works with clew.alefa.net from the start.** The hosted Clew is allowed on a first install;
  before, its address had to be added by hand. It can be removed under *Other Clew addresses*.
- **Several audio folders**, each with a tick box to pause it, also from the tray's menu.
- **Dictation on this computer** (optional part of the installer): Whisper models from `tiny` to
  `large-v3`, greyed out when the computer cannot run them; an NVIDIA graphics card is used when
  there is one. Live transcription of recordings tells speakers apart.
- Licences: the code is under the MIT licence; `THIRD-PARTY-NOTICES.txt` lists what the
  dictation part brings with it.
- The Japanese setup-type names in the installer showed stray characters; fixed.
