"""What the Python audio server adds when this package is beside it (docs/33): a secret that can be
shown again, dictation, starting at login, the update check and an optional settings window.

`clew_audio_server.py` alone still runs without it: everything here is loaded only when asked for.
Standard library only, like the server; dictation's own libraries are an optional extra.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY = "yaqalab/clew-tools"


def version() -> str:
    """The installed package's version, or tools/VERSION in a checkout."""
    try:
        from importlib.metadata import PackageNotFoundError, version as installed

        try:
            return installed("clew-tools")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover
        pass
    for candidate in (Path(__file__).resolve().parents[2] / "VERSION",):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"
