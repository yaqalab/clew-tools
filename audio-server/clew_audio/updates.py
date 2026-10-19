"""Telling the user a new version exists (docs/33), the way the Windows program does (docs/32 §3):
once a day when they leave it on, one small HTTPS request to the clew-tools releases, nothing sent
about them. Here nothing is installed: the window and the console say how to update
(`pipx upgrade clew-tools`), since a pipx or pip install is the user's own to change.

update.json beside the settings: {"check": true, "lastCheck": <seconds>, "notified": "<version>"}.
CLEW_UPDATE_BASE=http://127.0.0.1:<port> stands in for GitHub (the tests); nothing else is accepted.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import REPOSITORY, version

DAY = 24 * 60 * 60
UPGRADE = "pipx upgrade clew-tools"


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def newer(candidate: str, current: str | None = None) -> bool:
    a, b = parse_version(candidate), parse_version(current or version())
    return bool(a and b and a > b)


def _base() -> str:
    test = os.environ.get("CLEW_UPDATE_BASE", "")
    if test.startswith("http://127.0.0.1:"):
        return test.rstrip("/")
    return f"https://github.com/{REPOSITORY}"


def release_page(v: str | None = None) -> str:
    return f"https://github.com/{REPOSITORY}/releases" + (f"/tag/v{v}" if v else "/latest")


def fetch_latest(timeout: float = 15) -> str:
    """The latest release's version. Raises OSError (no connection) or ValueError (unreadable)."""
    request = urllib.request.Request(
        f"{_base()}/releases/latest/download/latest.json",
        headers={"User-Agent": f"clew-audio-server-python/{version()}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as r:
        body = r.read(64 * 1024 + 1)
    if len(body) > 64 * 1024:
        raise ValueError("the release's description is too large")
    data = json.loads(body)
    v = data.get("version") if isinstance(data, dict) else None
    if not isinstance(v, str) or not parse_version(v):
        raise ValueError("the release's description cannot be read")
    return v


@dataclass
class State:
    check: bool = True
    last_check: int = 0
    notified: str = ""

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(d, dict):
            return cls()
        return cls(
            check=d.get("check") is not False,
            last_check=int(d["lastCheck"]) if isinstance(d.get("lastCheck"), (int, float)) and d["lastCheck"] >= 0 else 0,
            notified=d["notified"] if isinstance(d.get("notified"), str) and parse_version(d["notified"]) else "",
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"check": self.check, "lastCheck": self.last_check, "notified": self.notified}),
                       encoding="utf-8")
        os.replace(tmp, path)

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        # A clock set back makes last_check lie in the future: check rather than wait for it.
        return self.check and (now - self.last_check >= DAY or now < self.last_check)


def daily(path: Path) -> str | None:
    """The daily check, for the console: a newer version once (the first time it is seen), else None.
    Quiet when it is off, not due, or GitHub cannot be reached."""
    state = State.load(path)
    if not state.due():
        return None
    try:
        latest = fetch_latest()
    except (OSError, ValueError):
        return None
    state.last_check = int(time.time())
    found = latest if newer(latest) and state.notified != latest else None
    if found:
        state.notified = latest
    state.save(path)
    return found
