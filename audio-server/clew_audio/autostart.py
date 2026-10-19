"""Starting the server when the user logs in (docs/33).

- macOS: a launch agent, ~/Library/LaunchAgents/net.alefa.clew-audio-server.plist.
- Linux with the window: a desktop autostart entry, ~/.config/autostart/clew-audio-server.desktop
  (the window needs the graphical session, which systemd's user services may start before).
- Linux without it: a systemd user service, ~/.config/systemd/user/clew-audio-server.service.

What is started is this same program with this same Python: the command line is built from
`sys.executable` and the installed `clew-audio-server` script (or the checkout's file), so a pipx
install, a virtual environment or a plain checkout each start what they have.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "net.alefa.clew-audio-server"
UNIT = "clew-audio-server.service"
DESKTOP = "clew-audio-server.desktop"


def command(gui: bool) -> list[str]:
    """The command line that starts the server, as an argument list."""
    script = shutil.which("clew-audio-server")
    # The launcher beside this Python (a venv's or pipx's bin), not one of another install. Only the
    # launcher is resolved: pipx links it from ~/.local/bin, while a venv's python links elsewhere.
    if script and Path(script).resolve().parent == Path(sys.executable).parent:
        argv = [script]  # pipx or a venv: its own launcher, which knows its Python
    else:
        argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "clew_audio_server.py")]
    return argv + (["--gui", "--minimized"] if gui else [])


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def files(gui: bool) -> list[tuple[Path, str]]:
    """Where the start-at-login entry goes on this system, and what it says. Nothing is written."""
    argv = command(gui)
    if sys.platform == "darwin":
        args = "".join(f"\n      <string>{escape(a)}</string>" for a in argv)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
  </dict>
</plist>
"""
        return [(_home() / "Library" / "LaunchAgents" / f"{LABEL}.plist", plist)]
    config = Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")
    line = shlex.join(argv)
    if gui:
        desktop = f"""[Desktop Entry]
Type=Application
Name=Clew audio server
Comment=Lets Clew play the recordings in your folders
Exec={line}
Terminal=false
X-GNOME-Autostart-enabled=true
"""
        return [(config / "autostart" / DESKTOP, desktop)]
    unit = f"""[Unit]
Description=Clew audio server (lets Clew play the recordings in your folders)

[Service]
ExecStart={line}
Restart=on-failure

[Install]
WantedBy=default.target
"""
    return [(config / "systemd" / "user" / UNIT, unit)]


def _all_places() -> list[Path]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")
    return [
        _home() / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        config / "autostart" / DESKTOP,
        config / "systemd" / "user" / UNIT,
    ]


def is_on() -> bool:
    return any(p.is_file() for p in _all_places())


def enable(gui: bool, activate: bool = True) -> list[Path]:
    """Writes the entry (replacing any other kind) and, unless `activate` is False, tells the system."""
    disable(activate)
    written = []
    for path, text in files(gui):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    if activate:
        if sys.platform == "darwin":
            # Loaded for next login; not started now (this process is already serving).
            pass
        elif not gui and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", UNIT], capture_output=True)
    return written


def disable(activate: bool = True) -> None:
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / "systemd" / "user" / UNIT
    if activate and unit.is_file() and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", UNIT], capture_output=True)
    for p in _all_places():
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if activate and unit.parent.is_dir() and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
