"""faster-whisper models on disk, and fetching one from Hugging Face (clew docs/28 §3.3).

A model is the folder ``<models_dir>/<name>/`` holding CTranslate2's files. ``download``
writes into ``<name>.part`` and renames it when every file is complete, so a folder with
the model's name is always whole, and a download that stopped resumes file by file.
Progress goes to stdout, one line per percent (``progress 42``), for the tray to read.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Callable

# Every model the tray lists; it greys out what this computer cannot run (docs/28 §3.6).
# The repositories are the ones faster-whisper 1.2.1 itself downloads by these names.
MODELS: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json",
         "preprocessor_config.json")
REQUIRED = ("config.json", "model.bin", "tokenizer.json")
# ALEFA_STT_HUB: another address that answers like Hugging Face (the tray's tests).
HUB = os.environ.get("ALEFA_STT_HUB", "https://huggingface.co").rstrip("/")


# The speaker-embedding model live transcription uses to tell voices apart (clew docs/29 §3):
# 3D-Speaker's CAM++ (Apache-2.0), as sherpa-onnx publishes it. One file, kept in
# <models_dir>/speakers/. ALEFA_STT_SPEAKER_URL points elsewhere (the tests).
SPEAKERS = "speakers"
SPEAKER_FILE = "campplus.onnx"
SPEAKER_URL = os.environ.get(
    "ALEFA_STT_SPEAKER_URL",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
)
SPEAKER_BYTES = 28281164


class DownloadError(RuntimeError):
    pass


def speaker_model_path(models_dir: str) -> Path | None:
    """The speaker model's file when it is here, else None."""
    path = Path(models_dir) / SPEAKERS / SPEAKER_FILE
    return path if path.is_file() else None


def model_path(models_dir: str, name: str) -> Path | None:
    """The model's folder when it is complete, else None."""
    if name == SPEAKERS:
        found = speaker_model_path(models_dir)
        return found.parent if found else None
    if name not in MODELS:
        return None
    folder = Path(models_dir) / name
    return folder if all((folder / f).is_file() for f in REQUIRED) else None


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "clew-stt-server"})
    return urllib.request.urlopen(request, timeout=60)


def remote_files(repo: str) -> dict[str, int]:
    """File name -> size, for the files a model needs."""
    with _open(f"{HUB}/api/models/{repo}?blobs=true") as response:
        info = json.load(response)
    sizes = {}
    for sibling in info.get("siblings", []):
        name = sibling.get("rfilename")
        if name in FILES:
            sizes[name] = int(sibling.get("size") or 0)
    missing = [f for f in REQUIRED if f not in sizes]
    if missing:
        raise DownloadError(f"{repo} has no {', '.join(missing)}")
    return sizes


def download(name: str, models_dir: str, say: Callable[[str], None] | None = None) -> Path:
    """Fetches the model into ``models_dir``; returns its folder."""
    say = say or _print
    if name == SPEAKERS:
        return _download_speakers(models_dir, say)
    repo = MODELS.get(name)
    if repo is None:
        raise DownloadError(f"unknown model {name!r}; one of {', '.join(MODELS)}")
    done = model_path(models_dir, name)
    if done is not None:
        say("progress 100")
        return done
    sizes = remote_files(repo)
    total = sum(sizes.values()) or 1
    say(f"total {total}")
    part = Path(models_dir) / f"{name}.part"
    part.mkdir(parents=True, exist_ok=True)
    have = 0
    last = -1

    def report(count: int) -> None:
        nonlocal last
        percent = min(99, count * 100 // total)
        if percent != last:
            last = percent
            say(f"progress {percent}")

    for filename, size in sizes.items():
        target = part / filename
        if target.is_file() and target.stat().st_size == size:
            have += size
            report(have)
            continue
        tmp = part / f"{filename}.tmp"
        with _open(f"{HUB}/{repo}/resolve/main/{filename}") as response, open(tmp, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                have += len(chunk)
                report(have)
        if size and tmp.stat().st_size != size:
            raise DownloadError(f"{filename}: got {tmp.stat().st_size} bytes of {size}")
        os.replace(tmp, target)
    folder = Path(models_dir) / name
    if folder.exists():
        shutil.rmtree(folder)
    os.replace(part, folder)
    say("progress 100")
    return folder


def _download_speakers(models_dir: str, say: Callable[[str], None]) -> Path:
    folder = Path(models_dir) / SPEAKERS
    if speaker_model_path(models_dir):
        say("progress 100")
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{SPEAKER_FILE}.tmp"
    have, last = 0, -1
    with _open(SPEAKER_URL) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("Content-Length") or SPEAKER_BYTES) or 1
        say(f"total {total}")
        while chunk := response.read(1 << 20):
            out.write(chunk)
            have += len(chunk)
            percent = min(99, have * 100 // total)
            if percent != last:
                last = percent
                say(f"progress {percent}")
    if have < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"the speaker model is too small ({have} bytes)")
    os.replace(tmp, folder / SPEAKER_FILE)
    say("progress 100")
    return folder


def _print(line: str) -> None:
    print(line, flush=True)
    sys.stdout.flush()
