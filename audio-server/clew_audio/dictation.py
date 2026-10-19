"""Dictation on this computer, from the Python audio server (docs/33): the same speech server the
Windows tray runs (tools/stt-server, docs/28), started here as a child process with this Python.

Installed with the `dictation` extra (`pipx install "clew-tools[dictation] @ git+…"`), which brings
faster-whisper and the server's own libraries; without it, `available()` says so and nothing starts.
The key, `stt.local.<secret>`, goes to the child on its standard input, never on its command line.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_PORT = 47822
DEFAULT_MODEL = "small"
LANGUAGES = ("", "en", "ja", "fr", "mg")  # "": Clew names one per session

# The Dictation tab's list (the native settings.h has the same): name, bytes, use, memory needed.
MODELS = (
    ("tiny", 78_203_619, "live", 2),
    ("base", 147_882_941, "live", 2),
    ("small", 486_212_372, "live", 4),
    ("medium", 1_530_571_735, "gpu", 8),
    ("large-v3-turbo", 1_621_665_983, "files", 8),
    ("distil-large-v3", 1_516_479_628, "files", 8),
    ("large-v3", 3_090_835_702, "gpu", 8),
)


def _stt_source() -> Path | None:
    """tools/stt-server in a checkout, so `python clew_audio_server.py` finds the server too."""
    here = Path(__file__).resolve().parents[2] / "stt-server"
    return here if (here / "alefa_stt").is_dir() else None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    source = _stt_source()
    if source and importlib.util.find_spec("alefa_stt") is None:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(source), env.get("PYTHONPATH", "")) if p)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def available() -> str:
    """'' when dictation can run here, else why not (in words)."""
    if importlib.util.find_spec("alefa_stt") is None and _stt_source() is None:
        return "the dictation server is not installed"
    for module in ("fastapi", "uvicorn", "numpy"):
        if importlib.util.find_spec(module) is None:
            return f"dictation's libraries are not installed ({module} is missing)"
    return ""


def engine_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def default_models_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "clew-stt" / "models"
    base = os.environ.get("XDG_DATA_HOME") or os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "clew-stt" / "models"


def model_present(models_dir: Path, name: str) -> bool:
    folder = models_dir / name
    return all((folder / f).is_file() for f in ("config.json", "model.bin", "tokenizer.json"))


def read_line(line: str, port: int) -> tuple[bool, str]:
    """(ready, problem) from one line of the server's output, as the native tray reads it."""
    if "Uvicorn running on" in line:
        return True, ""
    low = line.lower()
    if "address already in use" in low or "error while attempting to bind" in low or "10048" in low:
        return False, f"port {port} is in use"
    if "memoryerror" in low or "out of memory" in low:
        return False, "not enough memory for this model"
    for marker in ("EngineError: ", "DownloadError: ", "--key-stdin: "):
        if marker in line:
            return False, line.split(marker, 1)[1].strip()
    kind, sep, rest = line.partition(": ")
    if sep and " " not in kind and (kind.endswith("Error") or kind.endswith("Exception")):
        return False, rest.strip()
    return False, ""


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    models_dir: Path = field(default_factory=default_models_dir)
    port: int = DEFAULT_PORT
    language: str = ""
    origins: list[str] = field(default_factory=list)
    folders: list[tuple[str, str]] = field(default_factory=list)  # (name, path) of folders that are on
    key: str = ""
    fake_engine: bool = False  # the tests: no model needed


class Dictation:
    """One child at a time. States: off, loading, ready, failed (with `problem`)."""

    def __init__(self, on_change: Callable[[], None] | None = None, log: Callable[[str], None] | None = None) -> None:
        self.on_change = on_change or (lambda: None)
        self.log = log or (lambda line: None)
        self.state = "off"
        self.problem = ""
        self.child: subprocess.Popen[str] | None = None
        self.downloading = ""
        self.percent = 0
        self.lock = threading.Lock()

    def start(self, cfg: Config) -> None:
        self.stop()
        why = available()
        if not why and not cfg.fake_engine and not engine_installed():
            why = "faster-whisper is not installed (the `dictation` extra)"
        if not why and not cfg.fake_engine and not model_present(cfg.models_dir, cfg.model):
            why = f"the model {cfg.model} is not downloaded yet"
        if why:
            self._set("failed", why)
            return
        args = [
            sys.executable, "-m", "alefa_stt", "serve", "--local",
            "--engine", "fake" if cfg.fake_engine else "whisper",
            "--model", cfg.model, "--models-dir", str(cfg.models_dir),
            "--port", str(cfg.port), "--language", cfg.language,
            "--origins", " ".join(cfg.origins), "--key-stdin",
        ]
        for name, path in cfg.folders:
            args += ["--audio-folder", f"{name}={path}"]
        if cfg.fake_engine:
            args += ["--speakers", "fake"]
        try:
            child = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=_env(),
            )
        except OSError as e:
            self._set("failed", f"the dictation server could not start ({e})")
            return
        assert child.stdin is not None
        child.stdin.write(cfg.key + "\n")
        child.stdin.close()
        with self.lock:
            self.child = child
        self._set("loading", "")
        threading.Thread(target=self._watch, args=(child, cfg.port), daemon=True).start()

    def _watch(self, child: subprocess.Popen[str], port: int) -> None:
        problem = ""
        assert child.stdout is not None
        for line in child.stdout:
            line = line.rstrip()
            self.log(line)
            ready, why = read_line(line, port)
            if ready and child is self.child:
                self._set("ready", "")
            elif why:
                problem = why
        child.wait()
        with self.lock:
            mine = child is self.child
            if mine:
                self.child = None
        if mine:
            self._set("failed", problem or f"the dictation server stopped (exit {child.returncode})")

    def stop(self) -> None:
        with self.lock:
            child, self.child = self.child, None
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
        self._set("off", "")

    def _set(self, state: str, problem: str) -> None:
        self.state, self.problem = state, problem
        self.on_change()

    def download(self, name: str, models_dir: Path, done: Callable[[str], None] | None = None) -> None:
        """Fetches a model (or "speakers") in the background: `percent` moves, `done(problem)` at the end."""
        if self.downloading:
            return
        self.downloading, self.percent = name, 0
        self.on_change()

        def run() -> None:
            problem = ""
            try:
                p = subprocess.Popen(
                    [sys.executable, "-m", "alefa_stt", "download", name, "--models-dir", str(models_dir)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_env(),
                )
                assert p.stdout is not None
                for line in p.stdout:
                    if line.startswith("progress "):
                        self.percent = int(line.split()[1])
                        self.on_change()
                    elif line.startswith("error "):
                        problem = line[6:].strip()
                p.wait()
                if p.returncode and not problem:
                    problem = f"the download stopped (exit {p.returncode})"
            except OSError as e:
                problem = str(e)
            self.downloading = ""
            self.on_change()
            if done:
                done(problem)

        threading.Thread(target=run, daemon=True).start()
