"""The running server, as the settings window drives it (docs/33): start, stop, the folders, the
settings, the secret and the dictation key, and dictation itself. The window's thread calls these;
the server and dictation run on threads of their own and only set state the window reads.

Settings live in the same config.json as the console server's, so both see the same folders.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import clew_audio_server as srv

from . import dictation as dict_mod
from .keystore import open_store

SECRET = "secret"
STT_KEY = "stt-key"


class Service:
    def __init__(self, config_path: Path, env_origins: list[str] | None = None) -> None:
        self.config = srv.Config.load(config_path)
        self.keys = open_store(config_path.parent)
        self.env_origins = env_origins or []
        self.server: srv.LoopbackServer | None = None
        self.state: srv.State | None = None
        self.problem = ""
        self.stopped_by_user = False
        self.recent: deque[str] = deque(maxlen=100)
        self.listeners: list[Callable[[], None]] = []
        self.dictation = dict_mod.Dictation(on_change=self._changed, log=self._stt_log)
        self.made_secret = ""
        if not self.config.has_secret():
            self.made_secret = self.reset_secret()

    # -- settings ---------------------------------------------------------------------------

    def _get(self, key: str, default: Any) -> Any:
        """A saved value of the default's type (a positive number for numbers), else the default."""
        v = self.config.data.get(key)
        if isinstance(default, int):
            return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else default
        return v if isinstance(v, type(default)) else default

    @property
    def port(self) -> int:
        return self._get("port", srv.DEFAULT_PORT)

    @property
    def link_hours(self) -> int:
        return self._get("linkHours", srv.LINK_TTL_SEC // 3600)

    @property
    def ffmpeg_setting(self) -> str:
        return self._get("ffmpeg", "")

    @property
    def stt(self) -> dict[str, Any]:
        d = self.config.data.get("dictation")
        d = d if isinstance(d, dict) else {}
        return {
            "enabled": d.get("enabled") is True,
            "model": d["model"] if isinstance(d.get("model"), str) else dict_mod.DEFAULT_MODEL,
            "modelsDir": d["modelsDir"] if isinstance(d.get("modelsDir"), str) and d["modelsDir"] else str(dict_mod.default_models_dir()),
            "language": d["language"] if d.get("language") in dict_mod.LANGUAGES else "",
            "port": d["port"] if isinstance(d.get("port"), int) else dict_mod.DEFAULT_PORT,
        }

    def set_stt(self, **changes: Any) -> None:
        d = self.stt
        d.update(changes)
        self.config.data["dictation"] = d
        self.config.save()

    def origins(self) -> list[str]:
        return sorted({*self.config.origins, *self.env_origins})

    # -- the server -------------------------------------------------------------------------

    def running(self) -> bool:
        return self.server is not None

    def start(self) -> str:
        """Starts with the saved settings; returns a problem to show, or ''."""
        self.stop(user=False)
        entries = self.config.folders
        if not entries:
            self.problem = "add the folder of your audio files"
            self._changed()
            return self.problem
        if not any(e["enabled"] for e in entries):
            self.problem = "every folder is off: tick the ones Clew may use"
            self._changed()
            return self.problem
        ffmpeg = srv.find_ffmpeg(self.ffmpeg_setting or None)
        state = srv.State(
            srv.make_folders(entries), self.config, self.origins(), self.link_hours * 3600,
            srv.Mp3Seek(ffmpeg, self.config.path.parent / "cache"),
        )
        state.log = self._log
        try:
            server = srv.create_server(state, self.port)
        except OSError:
            self.problem = f"port {self.port} is used by another program; choose another port"
            self._changed()
            return self.problem
        self.server, self.state, self.problem, self.stopped_by_user = server, state, "", False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._log(f"listening on http://127.0.0.1:{self.port}")
        self._changed()
        return ""

    def stop(self, user: bool = True) -> None:
        server, self.server, self.state = self.server, None, None
        if server is not None:
            server.shutdown()
            server.server_close()
            self._log("stopped")
        if user:
            self.stopped_by_user = True
        self._changed()

    def set_folders(self, entries: list[dict[str, Any]]) -> str:
        """Takes effect at once, like the Windows window (docs/22 §11)."""
        self.config.folders = entries
        self.config.save()
        if self.state is not None and any(e["enabled"] for e in entries):
            self.state.folders = srv.make_folders(entries)
            problem = ""
        else:
            problem = self.start()
        if self.dictation.state in ("loading", "ready"):
            self.start_dictation()  # it reads the folders for deferred transcription
        self._changed()
        return problem

    def apply(self, port: int, link_hours: int, origins: list[str], ffmpeg: str) -> str:
        self.config.data.update({"port": port, "linkHours": link_hours, "origins": origins, "ffmpeg": ffmpeg})
        self.config.save()
        return self.start()

    # -- the secret and the key -------------------------------------------------------------

    def secret(self) -> str | None:
        """The secret, when it was kept; None when only its hash is (made before it was kept)."""
        return self.keys.get(SECRET)

    def reset_secret(self) -> str:
        made = srv.new_secret()
        self.config.set_secret(made)
        self.config.save()
        self.keys.set(SECRET, made)
        return made

    def stt_key(self) -> str:
        key = self.keys.get(STT_KEY)
        if not key:
            key = self.reset_stt_key(restart=False)
        return key

    def reset_stt_key(self, restart: bool = True) -> str:
        key = f"stt.local.{secrets.token_urlsafe(24)}"
        self.keys.set(STT_KEY, key)
        if restart and self.dictation.state in ("loading", "ready"):
            self.start_dictation(key)
        return key

    # -- dictation --------------------------------------------------------------------------

    def start_dictation(self, key: str | None = None, fake: bool = False) -> None:
        s = self.stt
        names = srv.folder_names([e["path"] for e in self.config.folders])
        folders = [(n, e["path"]) for n, e in zip(names, self.config.folders) if e["enabled"]]
        self.dictation.start(dict_mod.Config(
            model=s["model"], models_dir=Path(s["modelsDir"]), port=s["port"], language=s["language"],
            origins=self.origins(), folders=folders, key=key or self.stt_key(), fake_engine=fake,
        ))

    def start_dictation_if_enabled(self) -> None:
        if self.stt["enabled"] and not dict_mod.available():
            self.start_dictation()

    # -- notices ----------------------------------------------------------------------------

    def _log(self, line: str) -> None:
        self.recent.append(f"{time.strftime('%H:%M:%S')}  {line}")
        self._changed()

    def _stt_log(self, line: str) -> None:
        # Only what says where it is: the dictation server logs no text, but keep this short anyway.
        if line.startswith(("ERROR", "Traceback")) or "Uvicorn running" in line:
            self._log(f"dictation: {line[:160]}")

    def _changed(self) -> None:
        for fn in list(self.listeners):
            fn()

    def shutdown(self) -> None:
        self.dictation.stop()
        self.stop(user=False)


def default_config_path() -> Path:
    env = os.environ.get("CLEW_AUDIO_CONFIG")
    return Path(env).expanduser() if env else srv.default_config_path()
