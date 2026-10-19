"""Where the secret and the dictation key are kept, so they can be shown again.

The server itself only checks the secret against its scrypt hash (config.json); this is the copy the
window shows and copies. Strongest available first:

- macOS: the login Keychain, through the system's `security` program. The value is written on its
  standard input (`security -i`), never on a command line other programs could read.
- Linux: the Secret Service (GNOME Keyring, KWallet), through `secret-tool`, value on standard input.
- Elsewhere, or when those are missing or refuse: `secrets.json` beside the settings, readable by
  this user only (0600), as SSH keeps its keys.

CLEW_KEYSTORE=file forces the file (the tests, or a machine whose keyring asks too often).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE = "clew-audio-server"


class KeyStore:
    def __init__(self, config_dir: Path, backend: str | None = None) -> None:
        self.config_dir = config_dir
        self.backend = backend or self._choose()

    @staticmethod
    def _choose() -> str:
        forced = os.environ.get("CLEW_KEYSTORE", "").strip().lower()
        if forced in ("file", "keychain", "secret-service"):
            return forced
        if sys.platform == "darwin" and shutil.which("security"):
            return "keychain"
        if sys.platform.startswith("linux") and shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return "secret-service"
        return "file"

    @property
    def where(self) -> str:
        """In words, for the window and the README."""
        return {
            "keychain": "the macOS Keychain",
            "secret-service": "the desktop's keyring (Secret Service)",
        }.get(self.backend, f"{self._file} (readable by you only)")

    # -- the three backends ------------------------------------------------------------------

    @property
    def _file(self) -> Path:
        return self.config_dir / "secrets.json"

    def _read_file(self) -> dict[str, str]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, data: dict[str, str]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        # Created 0600 from the start, so the value is never readable by others, even briefly.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._file)

    def get(self, name: str) -> str | None:
        if self.backend == "file":
            return self._read_file().get(name)
        argv = (["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"] if self.backend == "keychain"
                else ["secret-tool", "lookup", "service", SERVICE, "account", name])
        try:
            r = subprocess.run(argv, capture_output=True, text=True)
        except OSError:
            return None
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def set(self, name: str, value: str) -> bool:
        """Keeps `value`; False when the system store refused (the caller may fall back)."""
        if any(c in value for c in "\n\r\"\\"):
            raise ValueError("a secret is one line of URL-safe characters")
        if self.backend == "keychain":
            # -U replaces an older one. Through `security -i`: the value is on stdin, not argv.
            command = f'add-generic-password -U -s {SERVICE} -a {name} -l "Clew audio server" -w "{value}"\n'
            r = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True)
            return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()
        if self.backend == "secret-service":
            r = subprocess.run(
                ["secret-tool", "store", f"--label=Clew audio server ({name})", "service", SERVICE, "account", name],
                input=value, capture_output=True, text=True,
            )
            return r.returncode == 0
        data = self._read_file()
        data[name] = value
        self._write_file(data)
        return True

    def delete(self, name: str) -> None:
        try:
            if self.backend == "keychain":
                subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", name], capture_output=True)
                return
            if self.backend == "secret-service":
                subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", name], capture_output=True)
                return
        except OSError:
            return  # the system's tool is gone: nothing of ours is left there to remove
        if self.backend == "file":
            data = self._read_file()
            if data.pop(name, None) is not None:
                self._write_file(data)


def open_store(config_dir: Path) -> KeyStore:
    """The best store that works here: a system one that refuses a test write falls back to the file."""
    store = KeyStore(config_dir)
    if store.backend != "file":
        probe = "probe"
        try:
            ok = store.set(probe, "ok") and store.get(probe) == "ok"
        except (OSError, ValueError):
            ok = False
        store.delete(probe)
        if not ok:
            store = KeyStore(config_dir, "file")
    return store
