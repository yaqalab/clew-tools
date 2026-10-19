"""Settings, read once from the environment (systemd's EnvironmentFile on the Pi), or for the
Windows build from `serve`'s options, which the audio server's tray passes (docs/28 §3.4)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return split_list(raw)


def split_list(raw: str) -> list[str]:
    return [item.strip().rstrip("/") for item in raw.replace(",", " ").split() if item.strip()]


def default_models_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "clew-stt", "models")


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8710
    # "hailo" on the Pi; "whisper" (faster-whisper on the CPU) on a PC; "fake" for tests and
    # for trying the protocol without a model.
    engine: str = "hailo"
    model: str = "base"
    arch: str | None = None
    hef_encoder: str | None = None
    hef_decoder: str | None = None
    npy_dir: str | None = None
    # faster-whisper: models live in <models_dir>/<model>/ (`download` puts them there).
    models_dir: str = field(default_factory=default_models_dir)
    cpu_threads: int = 0
    # "auto": an NVIDIA GPU when CUDA finds one and its libraries load, else the CPU.
    device: str = "auto"
    # Live transcription (clew docs/29): the audio server's folder, read-only, for the deferred
    # mode; empty: uploads only. "fake" speakers: the tests' stand-in for the speaker model.
    audio_root: str = ""
    # The audio server's folders by name (clew docs/22 §11), "" for one that is off; when given,
    # they replace audio_root.
    audio_folders: dict[str, str] = field(default_factory=dict)
    speakers: str = "auto"
    max_transcribe_mb: int = 1024
    # Empty: each request names its language (English when it does not). Otherwise the only one.
    language: str = ""
    keys_file: str = "/etc/alefa-stt/keys"
    # One key, handed on stdin by the tray and held in memory only (docs/28 §3.1).
    single_key: str | None = None
    allowed_origins: list[str] = field(default_factory=list)
    # Also accept any http://localhost:<port> or http://127.0.0.1:<port> page (a Clew run here).
    loopback_origins: bool = False
    # Refuse a request whose Host is not this computer (DNS rebinding), like the audio server.
    loopback_only: bool = False
    # "ffmpeg" runs the ffmpeg program; "av" decodes in-process with PyAV (no ffmpeg needed).
    decoder: str = "ffmpeg"
    vad: str = "energy"
    endpointing_ms: int = 600
    max_sessions: int = 2
    idle_seconds: int = 30
    max_session_seconds: int = 3600
    max_upload_mb: int = 25
    max_file_seconds: int = 240
    ffmpeg: str = "ffmpeg"
    tmp_dir: str = field(default_factory=tempfile.gettempdir)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            host=env("ALEFA_STT_HOST", "0.0.0.0"),
            port=_int("ALEFA_STT_PORT", 8710),
            engine=env("ALEFA_STT_ENGINE", "hailo"),
            model=env("ALEFA_STT_MODEL", "base"),
            arch=env("ALEFA_STT_ARCH") or None,
            hef_encoder=env("ALEFA_STT_HEF_ENCODER") or None,
            hef_decoder=env("ALEFA_STT_HEF_DECODER") or None,
            npy_dir=env("ALEFA_STT_NPY_DIR") or None,
            models_dir=env("ALEFA_STT_MODELS_DIR") or default_models_dir(),
            cpu_threads=_int("ALEFA_STT_CPU_THREADS", 0),
            device=env("ALEFA_STT_DEVICE", "auto"),
            audio_root=env("ALEFA_STT_AUDIO_ROOT", ""),
            max_transcribe_mb=_int("ALEFA_STT_MAX_TRANSCRIBE_MB", 1024),
            language=env("ALEFA_STT_LANGUAGE", ""),
            keys_file=env("ALEFA_STT_KEYS_FILE", "/etc/alefa-stt/keys"),
            allowed_origins=_list("ALEFA_STT_ALLOWED_ORIGINS", "https://clew.alefa.net"),
            vad=env("ALEFA_STT_VAD", "energy"),
            decoder=env("ALEFA_STT_DECODER", "ffmpeg"),
            endpointing_ms=_int("ALEFA_STT_ENDPOINTING_MS", 600),
            max_sessions=_int("ALEFA_STT_MAX_SESSIONS", 2),
            idle_seconds=_int("ALEFA_STT_IDLE_SECONDS", 30),
            max_session_seconds=_int("ALEFA_STT_MAX_SESSION_SECONDS", 3600),
            max_upload_mb=_int("ALEFA_STT_MAX_UPLOAD_MB", 25),
            max_file_seconds=_int("ALEFA_STT_MAX_FILE_SECONDS", 240),
            ffmpeg=env("ALEFA_STT_FFMPEG", "ffmpeg"),
            # systemd's RuntimeDirectory is a tmpfs, so uploads never touch the disk.
            tmp_dir=env("ALEFA_STT_TMP_DIR") or env("RUNTIME_DIRECTORY") or tempfile.gettempdir(),
        )
