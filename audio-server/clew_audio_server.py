"""
Clew audio server: lets Clew list and stream the audio files in folders of this computer.

An alternative to picking a file with the browser's file chooser (docs/22-audio-server.md).

    python clew_audio_server.py --folder D:\\interviews
    python clew_audio_server.py --folder D:\\study-b --folder-off D:\\interviews

The folders are remembered, each on or off (docs/22 section 11): a listed file's path starts with
its folder's name, and a folder that is off keeps that name while nothing in it is served.

Standard library only, on purpose: the same file is packed into a single .exe (build_exe.ps1)
and nothing needs installing on the researcher's machine.

What it promises
- Listens on 127.0.0.1 only. It refuses any request whose Host is not loopback (DNS rebinding).
- One secret, generated on the first start and shown once. Only its scrypt hash is stored.
- Listing and minting a stream link need the secret (Authorization: Bearer <secret>).
- A stream link is signed (HMAC) for one file and one expiry, with a key that lives in memory only,
  so a restart revokes every link. A player element cannot send headers; that is why links exist.
- Only files under the folders that are on, only audio extensions, and symlinks that leave a folder
  are not followed.
- Only Clew's own origins get CORS headers. Loopback origins always; a hosted Clew with --allow-origin.
- Nothing from a request beyond method and path is logged, and never the secret or a link's query.
- Firefox seeks badly in a long mp3 that carries a seek table (docs/22 section 8). So a constant-bit-rate
  mp3's table is blanked as it is sent (same length, nothing shifts), and a variable-bit-rate one is
  converted once, with ffmpeg, to a constant-bit-rate mp3 kept in a cache folder. Without ffmpeg it is
  sent as it is.
- No browser plays .wma or .amr: with ffmpeg they are converted the same way, once, to an mp3.

Settings, strongest first: command-line options, then a `.env` file next to this program
(`.env.example` lists every key), then what the last start remembered, then a question on the console.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

API_VERSION = 1
APP_NAME = "clew-audio-server"
DEFAULT_PORT = 47821
# Long enough for a long interview, short enough that a link copied somewhere is soon useless.
LINK_TTL_SEC = 4 * 60 * 60
MAX_FILES = 20_000
CHUNK = 64 * 1024

# Same set as the kit's AUDIO_ACCEPT (packages/kit/src/audio/files.ts).
MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".weba": "audio/webm",
    ".webm": "audio/webm",
    ".mp4": "audio/mp4",
    ".3gp": "audio/3gpp",
    ".amr": "audio/amr",
    ".wma": "audio/x-ms-wma",
}

LOOPBACK_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
LOOPBACK_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------
# The .env file
# ---------------------------------------------------------------------------------------------

ENV_KEYS = {
    "CLEW_AUDIO_FOLDER",
    "CLEW_AUDIO_PORT",
    "CLEW_AUDIO_ALLOW_ORIGINS",
    "CLEW_AUDIO_CONFIG",
    "CLEW_AUDIO_LINK_HOURS",
    "CLEW_AUDIO_FFMPEG",
}


def app_dir() -> Path:
    """Where this program lives: beside the script, or beside the .exe when packed."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_env(path: Path) -> tuple[dict[str, str], list[str]]:
    """KEY=VALUE lines. `#` starts a comment line, a value may be quoted, `export ` is ignored.
    No interpolation, so a Windows path needs no escaping. Returns (values, warnings); a missing
    file is not an error."""
    values: dict[str, str] = {}
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values, warnings
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not key:
            warnings.append(f"{path.name} line {n}: not KEY=VALUE, ignored")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in ENV_KEYS:
            warnings.append(f"{path.name} line {n}: unknown key {key}, ignored")
            continue
        values[key] = value
    return values, warnings


def env_int(values: dict[str, str], key: str, warnings: list[str]) -> int | None:
    v = values.get(key, "")
    if not v:
        return None
    try:
        n = int(v)
        if n > 0:
            return n
    except ValueError:
        pass
    warnings.append(f"{key}={v} is not a positive whole number, ignored")
    return None


# ---------------------------------------------------------------------------------------------
# Config and secret
# ---------------------------------------------------------------------------------------------


def default_config_path() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME / "config.json"


def hash_secret(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def new_secret() -> str:
    return secrets.token_urlsafe(24)


class Config:
    """What survives a restart: the folder, the secret's hash, the extra allowed origins."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("v") == 1:
                return cls(path, raw)
        except (OSError, ValueError):
            pass
        return cls(path, {"v": 1})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @property
    def folders(self) -> list[dict[str, Any]]:
        """The remembered folders, each {path, enabled}, in order (docs/22 §11)."""
        v = self.data.get("folders")
        if isinstance(v, list):
            return [
                {"path": f["path"], "enabled": f.get("enabled") is not False}
                for f in v
                if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]
            ]
        # Written before there could be several (2026-10-17).
        old = self.data.get("folder")
        return [{"path": old, "enabled": True}] if isinstance(old, str) and old else []

    @folders.setter
    def folders(self, value: list[dict[str, Any]]) -> None:
        self.data["folders"] = value
        self.data.pop("folder", None)

    @property
    def origins(self) -> list[str]:
        v = self.data.get("origins")
        return [o for o in v if isinstance(o, str)] if isinstance(v, list) else []

    def has_secret(self) -> bool:
        return isinstance(self.data.get("salt"), str) and isinstance(self.data.get("hash"), str)

    def set_secret(self, secret: str) -> None:
        salt = secrets.token_bytes(16)
        self.data["salt"] = base64.b64encode(salt).decode("ascii")
        self.data["hash"] = base64.b64encode(hash_secret(secret, salt)).decode("ascii")

    def check_secret(self, candidate: str) -> bool:
        try:
            salt = base64.b64decode(self.data["salt"])
            expected = base64.b64decode(self.data["hash"])
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(hash_secret(candidate, salt), expected)


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------


def mime_of(path: Path) -> str | None:
    return MIME_BY_EXT.get(path.suffix.lower())


def inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def list_audio(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """Every audio file under `root`, as relative paths with forward slashes. Hidden folders and
    links that point outside the folder are skipped."""
    found: list[dict[str, Any]] = []
    truncated = False
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            path = Path(e.path)
            try:
                if e.is_dir():
                    if inside(root, path.resolve()):
                        stack.append(path)
                    continue
                mime = mime_of(path)
                if mime is None or not e.is_file():
                    continue
                if not inside(root, path.resolve()):
                    continue
                st = e.stat()
            except OSError:
                continue
            if len(found) >= MAX_FILES:
                truncated = True
                break
            rel = path.relative_to(root).as_posix()
            found.append(
                {
                    "path": rel,
                    "name": path.name,
                    "size": st.st_size,
                    "modified": int(st.st_mtime * 1000),
                    "mime": mime,
                }
            )
        if truncated:
            break
    found.sort(key=lambda f: f["path"].lower())
    return found, truncated


def resolve_audio(root: Path, rel: str) -> Path | None:
    """The file `rel` names, or None when it is not an audio file inside `root`."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    path = (root / Path(*parts)).resolve()
    if not inside(root, path) or mime_of(path) is None or not path.is_file():
        return None
    return path


# Several folders served as one (docs/22 §11; the native server's folders.h has the same rules, and
# so does the dictation server's transcribe.py): each folder is named after its last segment, made
# unique ignoring case with " 2", " 3"... over every folder, on or off; a listed path is
# "<name>/<path inside>"; a path whose first segment is a folder's name is looked for there only
# (refused while it is off); any other path was saved when there was one folder, and is used when
# exactly one folder that is on has it.


@dataclass
class Folder:
    name: str
    root: Path | None  # resolved; None when it cannot be opened
    enabled: bool = True


def folder_names(paths: list[str]) -> list[str]:
    names: list[str] = []
    taken: set[str] = set()
    for p in paths:
        trimmed = p.rstrip("/\\")
        base = re.split(r"[/\\]", trimmed)[-1] if trimmed else ""
        if base.endswith(":"):
            base = base[:-1]
        base = base or "folder"
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base} {n}", n + 1
        taken.add(name.lower())
        names.append(name)
    return names


def make_folders(entries: list[dict[str, Any]]) -> list[Folder]:
    out = []
    for name, entry in zip(folder_names([e["path"] for e in entries]), entries):
        root = Path(entry["path"]).expanduser()
        out.append(Folder(name, root.resolve() if root.is_dir() else None, bool(entry["enabled"])))
    return out


def list_served(folders: list[Folder]) -> tuple[list[dict[str, Any]], bool]:
    found: list[dict[str, Any]] = []
    truncated = False
    for f in folders:
        if not f.enabled or f.root is None:
            continue
        files, cut = list_audio(f.root)
        for item in files:
            if len(found) >= MAX_FILES:
                return found, True
            found.append({**item, "path": f"{f.name}/{item['path']}"})
        truncated = truncated or cut
    return found, truncated


def resolve_served(folders: list[Folder], rel: str) -> Path | None:
    first, sep, rest = rel.partition("/")
    if sep:
        for f in folders:
            if f.name == first:
                return resolve_audio(f.root, rest) if f.enabled and f.root is not None else None
    hits = [p for f in folders if f.enabled and f.root is not None for p in [resolve_audio(f.root, rel)] if p]
    return hits[0] if len(hits) == 1 else None


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """(first, last) inclusive; None for no Range header; "bad" for one that cannot be met.
    Only one range is served — a player never asks for more."""
    if not header:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return "bad"
    if m.group(1) == "":
        n = int(m.group(2))
        if n == 0:
            return "bad"
        return (max(0, size - n), size - 1) if size else "bad"
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) != "" else size - 1
    if first >= size or first > last:
        return "bad"
    return (first, min(last, size - 1))


# ---------------------------------------------------------------------------------------------
# Long mp3 files and Firefox's seeking
# ---------------------------------------------------------------------------------------------
#
# An mp3 usually starts with one frame that holds no sound, only a "Xing" (variable bit rate) or
# "Info" (constant bit rate) tag with a table of 100 seek points. Firefox seeks by that table, so in a
# long recording it lands up to a minute from where it was asked and stalls in silence on the way
# (docs/22 section 8). With no table it works the length out from the file's size, which is exact for a
# constant bit rate, and seeks exactly. So:
#   - "Info": the tag is blanked in what is sent. The frame keeps its length, so every size and every
#     Range request stays what the listing says.
#   - "Xing" or "VBRI": the file is converted once to a constant-bit-rate mp3 with no table.

# Layer III bit rates in kbit/s by the header's index 1..14: MPEG1, then MPEG2 and 2.5.
MP3_KBPS = {
    3: [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    0: [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
MP3_HZ = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
CONVERT_KBPS = 128
# The cache of converted files is cut back to this when a new one is made.
CACHE_LIMIT_BYTES = 4 * 1024**3


class Mp3Head:
    """What the first frame of an mp3 carries: the kind of tag, and where the tag sits in the file."""

    def __init__(self, kind: str, tag_start: int = 0, frame_end: int = 0) -> None:
        self.kind = kind  # "info", "xing", "vbri" or "none"
        self.tag_start = tag_start
        self.frame_end = frame_end  # the first byte after the frame

    @property
    def blank(self) -> tuple[int, int] | None:
        """The bytes of a constant-bit-rate file's tag that are sent as zeros, as (first, end)."""
        return (self.tag_start, self.frame_end) if self.kind == "info" else None


def mp3_frame_at(buf: bytes, at: int) -> tuple[int, int] | None:
    """(length of the frame, offset of a Xing/Info tag in it) for a layer III header at `at`, else None."""
    if at + 4 > len(buf) or buf[at] != 0xFF or buf[at + 1] & 0xE0 != 0xE0:
        return None
    version = (buf[at + 1] >> 3) & 3  # 3 is MPEG1, 2 is MPEG2, 0 is MPEG2.5, 1 is reserved
    if version == 1 or (buf[at + 1] >> 1) & 3 != 1:  # reserved, or not layer III
        return None
    bitrate_index, rate_index = buf[at + 2] >> 4, (buf[at + 2] >> 2) & 3
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    padding = (buf[at + 2] >> 1) & 1
    mono = buf[at + 3] >> 6 == 3
    length = (144 if version == 3 else 72) * MP3_KBPS[version][bitrate_index - 1] * 1000 // MP3_HZ[version][rate_index] + padding
    side = (17 if mono else 32) if version == 3 else (9 if mono else 17)
    checksum = 0 if buf[at + 1] & 1 else 2  # the protection bit is 0 when a checksum follows
    return length, 4 + checksum + side


def read_mp3_head(path: Path) -> Mp3Head:
    """Looks at the start of the file only. Anything unexpected reads as "none": it is sent as it is."""
    try:
        with open(path, "rb") as f:
            start = 0
            lead = f.read(10)
            if lead[:3] == b"ID3" and len(lead) == 10:
                size = (lead[6] & 0x7F) << 21 | (lead[7] & 0x7F) << 14 | (lead[8] & 0x7F) << 7 | (lead[9] & 0x7F)
                start = 10 + size + (10 if lead[5] & 0x10 else 0)
            f.seek(start)
            buf = f.read(8192)
    except OSError:
        return Mp3Head("none")
    # The first frame follows the ID3 tag directly; a little junk before it is tolerated.
    for i in range(min(len(buf), 2048)):
        found = mp3_frame_at(buf, i)
        if found is None:
            continue
        length, tag_at = found
        # A real frame is followed by another one, unless the file ends there.
        if i + length + 4 <= len(buf) and mp3_frame_at(buf, i + length) is None:
            continue
        for kind, tag, at in (("info", b"Info", tag_at), ("xing", b"Xing", tag_at), ("vbri", b"VBRI", 36)):
            if buf[i + at : i + at + 4] == tag:
                return Mp3Head(kind, start + i + at, start + i + length)
        return Mp3Head("none")
    return Mp3Head("none")


def find_ffmpeg(setting: str | None) -> str | None:
    """The ffmpeg to use: `setting` (a path), else beside this program, else on PATH. "off" means none."""
    if setting is not None and setting.strip().lower() in ("off", "no", "none", "0"):
        return None
    if setting:
        given = Path(setting).expanduser()
        return str(given) if given.is_file() else None
    beside = app_dir() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return str(beside) if beside.is_file() else shutil.which("ffmpeg")


# What no browser plays (the player's own message is "the browser cannot play this file"): these are
# converted to an mp3 whenever ffmpeg is there.
UNPLAYABLE = {".wma", ".amr"}


class Mp3Seek:
    """What the server does to make a file play and seek: reads each mp3's first frame once, and converts
    with ffmpeg, once, into a cache, a variable-bit-rate mp3 to a constant-bit-rate one and a format no
    browser plays (`UNPLAYABLE`, such as .wma) to an mp3."""

    def __init__(self, ffmpeg: str | None, cache_dir: Path) -> None:
        self.ffmpeg = ffmpeg
        self.cache_dir = cache_dir
        self.lock = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.heads: dict[str, tuple[int, int, Mp3Head]] = {}

    def head_of(self, path: Path) -> Mp3Head:
        """The first frame's tag, remembered until the file changes."""
        st = path.stat()
        with self.lock:
            seen = self.heads.get(str(path))
        if seen and seen[0] == st.st_mtime_ns and seen[1] == st.st_size:
            return seen[2]
        head = read_mp3_head(path)
        with self.lock:
            self.heads[str(path)] = (st.st_mtime_ns, st.st_size, head)
        return head

    def _target(self, path: Path) -> Path:
        st = path.stat()
        key = hashlib.sha256(f"{path}\n{st.st_mtime_ns}\n{st.st_size}\n{CONVERT_KBPS}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key[:32]}.mp3"

    def converted(self, path: Path) -> Path | None:
        """The converted copy of `path`, if one was made."""
        try:
            hit = self._target(path)
            if hit.is_file():
                os.utime(hit)  # the most recently used survives the trimming
                return hit
        except OSError:
            pass
        return None

    def convert(self, path: Path) -> Path | None:
        """The converted copy, making it first (a long file takes a while); None when the file needs none,
        there is no ffmpeg, or ffmpeg failed. Concurrent calls for one file wait for the one conversion."""
        suffix = path.suffix.lower()
        if self.ffmpeg is None or (suffix != ".mp3" and suffix not in UNPLAYABLE):
            return None
        try:
            if suffix == ".mp3" and self.head_of(path).kind not in ("xing", "vbri"):
                return None
            target = self._target(path)
        except OSError:
            return None
        with self.lock:
            once = self.locks.setdefault(target.name, threading.Lock())
        with once:
            hit = self.converted(path)
            if hit is not None:
                return hit
            part = target.with_suffix(".part")
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                done = subprocess.run(
                    [
                        self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                        "-map", "0:a:0", "-map_metadata", "-1",
                        "-c:a", "libmp3lame", "-b:a", f"{CONVERT_KBPS}k", "-write_xing", "0", "-id3v2_version", "0",
                        "-f", "mp3", str(part),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if done.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                    part.unlink(missing_ok=True)
                    return None
                os.replace(part, target)
            except OSError:
                return None
        self._trim(keep=target)
        return target

    def _trim(self, keep: Path) -> None:
        """Deletes the least recently used copies until the cache is under its limit."""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.cache_dir.glob("*.mp3")]
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files, key=lambda f: f[0]):
            if total <= CACHE_LIMIT_BYTES:
                break
            if p == keep:
                continue
            try:
                p.unlink()
                total -= size
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------------------


class State:
    def __init__(
        self,
        folders: list[Folder] | Path,
        config: Config,
        extra_origins: list[str],
        link_ttl_sec: int = LINK_TTL_SEC,
        seek: Mp3Seek | None = None,
    ) -> None:
        # One folder may be given as its path (the tests).
        self.folders = make_folders([{"path": str(folders), "enabled": True}]) if isinstance(folders, Path) else folders
        self.link_ttl_sec = link_ttl_sec
        self.config = config
        self.seek = seek or Mp3Seek(None, config.path.parent / "cache")
        self.origins = set(extra_origins) | set(config.origins)
        # In memory only: a restart revokes every stream link.
        self.link_key = secrets.token_bytes(32)
        self.failures = 0
        self.lock = threading.Lock()

    def origin_ok(self, origin: str) -> bool:
        return bool(LOOPBACK_ORIGIN.match(origin)) or origin in self.origins

    def sign(self, rel: str, exp: int) -> str:
        mac = hmac.new(self.link_key, f"{rel}\n{exp}".encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def link_for(self, rel: str, now: float | None = None) -> tuple[str, int]:
        exp = int((now if now is not None else time.time()) + self.link_ttl_sec)
        return f"/api/stream?p={quote(rel, safe='')}&exp={exp}&sig={self.sign(rel, exp)}", exp * 1000

    def link_ok(self, rel: str, exp: str, sig: str) -> bool:
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(self.sign(rel, int(exp)), sig)

    def secret_ok(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return self._failed()
        ok = self.config.check_secret(header[7:].strip())
        with self.lock:
            self.failures = 0 if ok else self.failures + 1
            slow = self.failures >= 5
        if not ok and slow:
            time.sleep(1.0)
        return ok

    def _failed(self) -> bool:
        with self.lock:
            self.failures += 1
        return False


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClewAudioServer/1"
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Method and path only; a link's query carries its signature.
            path = urlsplit(self.path).path
            sys.stderr.write(f"{self.command} {path} -> {args[1] if len(args) > 1 else ''}\n")

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin and state.origin_ok(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")

        def _json(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _refuse(self, status: int, why: str) -> None:
            self._json(status, {"error": why})

        def _guard(self) -> bool:
            """Loopback Host and an allowed Origin, for every request. False once it answered."""
            host = self.headers.get("Host", "")
            if not LOOPBACK_HOST.match(host):
                self._refuse(HTTPStatus.FORBIDDEN, "host")
                return False
            origin = self.headers.get("Origin")
            if origin and not state.origin_ok(origin):
                self._refuse(HTTPStatus.FORBIDDEN, "origin")
                return False
            return True

        def _body(self) -> dict[str, Any] | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if n < 0 or n > 8192:
                self.close_connection = True
                return None
            try:
                v = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None
            return v if isinstance(v, dict) else None

        # -- verbs ------------------------------------------------------------------------

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._guard():
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Range")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            url = urlsplit(self.path)
            if url.path == "/api/ping":
                # No secret: lets Clew tell "no server here" from "wrong secret".
                self._json(HTTPStatus.OK, {"server": APP_NAME, "api": API_VERSION})
            elif url.path == "/api/files":
                self._files()
            elif url.path == "/api/stream":
                self._stream(parse_qs(url.query))
            else:
                self._refuse(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            # Read the body first: a refusal that leaves it unread would corrupt the next request
            # on this kept-alive connection.
            body = self._body()
            if urlsplit(self.path).path == "/api/url":
                self._url(body)
            else:
                self._refuse(HTTPStatus.NOT_FOUND, "not found")

        # -- endpoints --------------------------------------------------------------------

        def _auth(self) -> bool:
            if state.secret_ok(self.headers.get("Authorization")):
                return True
            self._refuse(HTTPStatus.UNAUTHORIZED, "secret")
            return False

        def _files(self) -> None:
            if not self._auth():
                return
            files, truncated = list_served(state.folders)
            # The folders' names, not their full paths: Clew has no use for the rest.
            names = [f.name for f in state.folders if f.enabled and f.root is not None]
            self._json(
                HTTPStatus.OK,
                {"folder": ", ".join(names), "folders": names, "files": files, "truncated": truncated},
            )

        def _url(self, body: dict[str, Any] | None) -> None:
            if not self._auth():
                return
            rel = body.get("path") if body else None
            path = resolve_served(state.folders, rel) if isinstance(rel, str) else None
            if path is None or not isinstance(rel, str):
                self._refuse(HTTPStatus.NOT_FOUND, "no such file")
                return
            # A variable-bit-rate mp3 is converted here, before the link, so that what the link
            # plays is settled and its size can be told: the player reads the waveform by ranges.
            sent = state.seek.convert(path) or path
            try:
                size = sent.stat().st_size
            except OSError:
                self._refuse(HTTPStatus.NOT_FOUND, "no such file")
                return
            url, expires = state.link_for(rel)
            self._json(HTTPStatus.OK, {"url": url, "expires": expires, "size": size})

        def _stream(self, query: dict[str, list[str]]) -> None:
            rel = (query.get("p") or [""])[0]
            exp = (query.get("exp") or [""])[0]
            sig = (query.get("sig") or [""])[0]
            if not state.link_ok(rel, exp, sig):
                self._refuse(HTTPStatus.UNAUTHORIZED, "link")
                return
            path = resolve_served(state.folders, rel)
            if path is None:
                self._refuse(HTTPStatus.NOT_FOUND, "no such file")
                return
            mime = mime_of(path) or "application/octet-stream"
            blank: tuple[int, int] | None = None
            try:
                converted = state.seek.converted(path)
                if converted is not None:
                    path, mime = converted, "audio/mpeg"
                elif mime == "audio/mpeg":
                    blank = state.seek.head_of(path).blank
                size = path.stat().st_size
                span = parse_range(self.headers.get("Range"), size)
                f = open(path, "rb")
            except OSError:
                self._refuse(HTTPStatus.NOT_FOUND, "no such file")
                return
            with f:
                if span == "bad":
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first, last = span if isinstance(span, tuple) else (0, size - 1)
                length = max(0, last - first + 1) if size else 0
                self.send_response(HTTPStatus.PARTIAL_CONTENT if isinstance(span, tuple) else HTTPStatus.OK)
                self._cors()
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if isinstance(span, tuple):
                    self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                if self.command == "HEAD" or length == 0:
                    return
                f.seek(first)
                left = length
                at = first
                try:
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        if blank is not None and at < blank[1] and at + len(chunk) > blank[0]:
                            # A constant-bit-rate file's seek table, sent as silence (see above).
                            data = bytearray(chunk)
                            lo, hi = max(blank[0], at) - at, min(blank[1], at + len(chunk)) - at
                            data[lo:hi] = bytes(hi - lo)
                            chunk = bytes(data)
                        self.wfile.write(chunk)
                        at += len(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # The player moved on (a seek) and closed this request.
                    self.close_connection = True

    return Handler


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process share the port; there it is refused instead.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(state: State, port: int) -> LoopbackServer:
    return LoopbackServer(("127.0.0.1", port), make_handler(state))


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def ask_folder() -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("No folder chosen. Start with --folder <path>.")
    return input("Folder with your audio files: ").strip().strip('"')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Serve the audio files of your folders to Clew.")
    p.add_argument(
        "--folder",
        action="append",
        default=[],
        help="a folder to serve: added to the remembered list, or turned on (repeatable)",
    )
    p.add_argument(
        "--folder-off",
        action="append",
        default=[],
        metavar="FOLDER",
        help="turn a remembered folder off: it keeps its name in Clew, nothing in it is served (repeatable)",
    )
    p.add_argument(
        "--remove-folder",
        action="append",
        default=[],
        metavar="FOLDER",
        help="take a folder out of the remembered list (repeatable)",
    )
    p.add_argument("--port", type=int, default=None, help=f"default {DEFAULT_PORT}")
    p.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="URL",
        help="a hosted Clew's origin, e.g. https://you.github.io (remembered); loopback is always allowed",
    )
    p.add_argument("--reset-secret", action="store_true", help="make a new secret; the old one stops working")
    p.add_argument("--config", type=Path, default=None, help="where the settings are kept")
    p.add_argument("--env", type=Path, default=None, help="a .env file; default: .env beside this program")
    args = p.parse_args(argv)

    # A console in a legacy code page (cp932, cp1252...) must never crash the start-up: the secret is
    # shown once, and a failed print after it was saved would lose it.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", line_buffering=True)

    env_path = args.env or app_dir() / ".env"
    env, warnings = load_env(env_path)
    if args.env is not None and not env_path.is_file():
        raise SystemExit(f"No such .env file: {env_path}")
    port = args.port or env_int(env, "CLEW_AUDIO_PORT", warnings) or DEFAULT_PORT
    link_hours = env_int(env, "CLEW_AUDIO_LINK_HOURS", warnings)
    ffmpeg = find_ffmpeg(env.get("CLEW_AUDIO_FFMPEG"))
    if env.get("CLEW_AUDIO_FFMPEG", "").strip().lower() not in ("", "off", "no", "none", "0") and ffmpeg is None:
        warnings.append(f"CLEW_AUDIO_FFMPEG is not a file: {env['CLEW_AUDIO_FFMPEG']}")
    env_config = env.get("CLEW_AUDIO_CONFIG")
    config = Config.load(args.config or (Path(env_config).expanduser() if env_config else default_config_path()))
    shown_secret: str | None = None
    if args.reset_secret or not config.has_secret():
        shown_secret = new_secret()
        config.set_secret(shown_secret)

    # The command line beats the .env (whose CLEW_AUDIO_FOLDER may hold several, separated by
    # os.pathsep); both add to the remembered list, or turn a folder on.
    entries = config.folders
    same = lambda a, b: os.path.normcase(a) == os.path.normcase(b)  # noqa: E731
    env_folders = [f for f in env.get("CLEW_AUDIO_FOLDER", "").split(os.pathsep) if f.strip()]
    wanted = [(f, True) for f in (args.folder or env_folders)] + [(f, False) for f in args.folder_off]
    for raw, on in wanted:
        root = Path(raw.strip()).expanduser()
        if not root.is_dir():
            raise SystemExit(f"Not a folder: {root}")
        path = str(root.resolve())
        known = next((e for e in entries if same(e["path"], path)), None)
        if known:
            known["enabled"] = on
        else:
            entries.append({"path": path, "enabled": on})
    for raw in args.remove_folder:
        path = str(Path(raw.strip()).expanduser().resolve())
        entries = [e for e in entries if not same(e["path"], path)]
    if not any(e["enabled"] for e in entries):
        root = Path(ask_folder()).expanduser()
        if not root.is_dir():
            raise SystemExit(f"Not a folder: {root}")
        entries.append({"path": str(root.resolve()), "enabled": True})
    config.folders = entries
    folders = make_folders(entries)
    # Origins from the command line and the remembered ones are kept; the .env's are used
    # without being written to the remembered config, so removing a line from .env removes it.
    env_origins = [o.rstrip("/") for o in re.split(r"[,\s]+", env.get("CLEW_AUDIO_ALLOW_ORIGINS", "")) if o]
    remembered = sorted({*config.origins, *(o.rstrip("/") for o in args.allow_origin)})
    if remembered:
        config.data["origins"] = remembered
    config.save()

    state = State(
        folders,
        config,
        sorted({*remembered, *env_origins}),
        link_hours * 3600 if link_hours else LINK_TTL_SEC,
        Mp3Seek(ffmpeg, config.path.parent / "cache"),
    )
    try:
        server = create_server(state, port)
    except OSError as e:
        raise SystemExit(f"Port {port} is not available ({e}). Try --port <number>.") from e

    for w in warnings:
        print(f"Warning: {w}")
    if env_path.is_file():
        print(f"Settings from {env_path}")
    print(f"Clew audio server  ->  http://127.0.0.1:{port}")
    if state.origins:
        print(f"Also allowed to call it: {', '.join(sorted(state.origins))}")
    for entry, f in zip(entries, folders):
        if not f.enabled:
            print(f'Folder "{f.name}": {entry["path"]}  (off)')
        elif f.root is None:
            print(f'Folder "{f.name}": {entry["path"]}  (cannot be opened: a disconnected drive?)')
        else:
            files, truncated = list_audio(f.root)
            more = ", more than shown" if truncated else ""
            print(f'Folder "{f.name}": {entry["path"]}  ({len(files)} audio files{more})')
    print(
        f"ffmpeg: {ffmpeg} (variable-bit-rate mp3, .wma and .amr files are converted so that browsers play them)"
        if ffmpeg
        else "ffmpeg: not found (.wma and .amr cannot be played, and long variable-bit-rate mp3 files seek badly in Firefox; docs/22 section 8)"
    )
    if shown_secret:
        print()
        print("Secret (shown once - paste it into Clew):")
        print(f"    {shown_secret}")
        print()
    else:
        print("Secret: the one shown at the first start (--reset-secret makes a new one).")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
