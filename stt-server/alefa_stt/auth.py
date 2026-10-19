"""API keys.

A key looks like ``stt.<name>.<secret>``. The keys file holds one line per key,
``<name>:s1:<salt>:<hash>`` (scrypt, base64url), never the key itself. The file is
re-read when it changes, so adding or revoking a key needs no restart.

Like clew's audio server: keys are compared in constant time, and five wrong tries
in a row slow every answer by a second until a right one arrives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
from dataclasses import dataclass

PREFIX = "stt"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
SLOW_AFTER = 5


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(key.encode(), salt=salt, **_SCRYPT)


def new_key(name: str) -> tuple[str, str]:
    """Return (key, keys-file line). The key is shown once and stored nowhere."""
    if not NAME_RE.match(name):
        raise ValueError("name: lowercase letters, digits and '-', at most 32")
    key = f"{PREFIX}.{name}.{secrets.token_urlsafe(24)}"
    salt = os.urandom(16)
    return key, f"{name}:s1:{_b64(salt)}:{_b64(_scrypt(key, salt))}"


def key_name(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != PREFIX or not NAME_RE.match(parts[1]):
        return None
    return parts[1]


@dataclass
class _Entry:
    salt: bytes
    digest: bytes


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._entries: dict[str, _Entry] = {}
        # sha256(key) -> name, for keys already verified; memory only.
        self._verified: dict[bytes, str] = {}
        self.failures = 0

    def _reload(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return
        entries: dict[str, _Entry] = {}
        if mtime is not None:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        name, scheme, salt, digest = line.split(":")
                        if scheme != "s1" or not NAME_RE.match(name):
                            continue
                        entries[name] = _Entry(_unb64(salt), _unb64(digest))
                    except ValueError:
                        continue
        self._entries = entries
        self._verified.clear()
        self._mtime = mtime

    def names(self) -> list[str]:
        with self._lock:
            self._reload()
            return sorted(self._entries)

    def verify(self, key: str | None) -> str | None:
        """Return the key's name, or None. Counts consecutive failures."""
        with self._lock:
            self._reload()
            name = self._check(key or "")
            self.failures = 0 if name else self.failures + 1
            return name

    @property
    def should_slow_down(self) -> bool:
        return self.failures >= SLOW_AFTER

    def _check(self, key: str) -> str | None:
        fingerprint = hashlib.sha256(key.encode()).digest()
        known = self._verified.get(fingerprint)
        if known and known in self._entries:
            return known
        name = key_name(key)
        entry = self._entries.get(name) if name else None
        if entry is None:
            # Same work as a real check, so timing does not tell which names exist.
            _scrypt(key, b"\0" * 16)
            return None
        if not hmac.compare_digest(_scrypt(key, entry.salt), entry.digest):
            return None
        self._verified[fingerprint] = name
        return name


def add_key_line(path: str, line: str) -> None:
    name = line.split(":", 1)[0]
    lines = _read_lines(path)
    if any(existing.split(":", 1)[0] == name for existing in lines):
        raise ValueError(f"a key named {name!r} exists; revoke it first")
    _write_lines(path, lines + [line])


def revoke_key(path: str, name: str) -> bool:
    lines = _read_lines(path)
    kept = [line for line in lines if line.split(":", 1)[0] != name]
    if len(kept) == len(lines):
        return False
    _write_lines(path, kept)
    return True


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def _write_lines(path: str, lines: list[str]) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    if os.path.exists(path):
        try:
            st = os.stat(path)
            os.chown(tmp, st.st_uid, st.st_gid)
        except (AttributeError, PermissionError):
            pass
    os.replace(tmp, path)



class SingleKey:
    """The local server's one key (clew docs/28 §3.1): made and kept by the audio server's
    tray, handed over on stdin, held here in memory only. Same answers as ``KeyStore``."""

    def __init__(self, key: str):
        if key_name(key) is None:
            raise ValueError("the key must look like stt.<name>.<secret>")
        self._digest = hashlib.sha256(key.encode()).digest()
        self._name = key_name(key)
        self._lock = threading.Lock()
        self.failures = 0

    def names(self) -> list[str]:
        return [self._name]

    def verify(self, key: str | None) -> str | None:
        given = hashlib.sha256((key or "").encode()).digest()
        ok = hmac.compare_digest(given, self._digest)
        with self._lock:
            self.failures = 0 if ok else self.failures + 1
        return self._name if ok else None

    @property
    def should_slow_down(self) -> bool:
        return self.failures >= SLOW_AFTER
