"""Whisper engines. One call at a time: the Hailo-8 serves one process, one request, and
faster-whisper already uses every core it is given.

``HailoEngine`` drives hailo-apps' own Whisper pipeline (encoder and decoder HEFs on
the chip, token embedding and decoding loop on the CPU). It is tied to hailo-apps'
layout (``hailo_apps.python.standalone_apps.speech_recognition``); install.sh pins
the commit it was checked against.

``FasterWhisperEngine`` runs faster-whisper (CTranslate2) on the CPU of a PC, for the
local server the audio server's tray starts (clew docs/28). Its window is Whisper's own
30 s, so a sentence is never cut, and it takes a language and a prompt per call.

Every engine has ``transcribe(audio, language=None, prompt=None)`` and ``languages``;
an engine that cannot use a prompt ignores it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import SAMPLE_RATE
from .config import Settings

log = logging.getLogger("alefa_stt.engine")


class EngineError(RuntimeError):
    pass


def tidy(text: str) -> str:
    """Collapse spaces and drop a sentence the decoder repeated (a Whisper habit)."""
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.?!])\s+", text)
    kept: list[str] = []
    for sentence in sentences:
        norm = sentence.lower().strip(" .?!")
        if norm and any(norm == k.lower().strip(" .?!") for k in kept):
            break
        kept.append(sentence)
    text = _drop_loop(" ".join(kept).strip())
    return "" if not re.search(r"\w", text) else text


def _drop_loop(text: str, min_words: int = 4) -> str:
    """Cut a decoder loop: from the first point where the words start repeating, word
    for word, the run that came just before (at least `min_words` of them), e.g.
    "...was limited, although the effect on X was limited, although the effect on"."""
    tokens = text.split()
    norm = [re.sub(r"\W", "", t.lower()) for t in tokens]
    n = len(norm)
    for j in range(min_words, n - min_words + 1):
        for i in range(j - min_words, -1, -1):
            m = min(n - j, j - i)
            if m >= min_words and norm[i : i + m] == norm[j : j + m]:
                kept = " ".join(tokens[:j]).rstrip(" ,;:")
                return kept if kept.endswith((".", "?", "!")) else kept + "."
    return text


def continued(text: str) -> str:
    """For a piece cut because it filled the window: Whisper still ends it with a period,
    though the sentence goes on in the next piece. Drop that period (not ? ! or ...)."""
    if text.endswith(".") and not text.endswith(".."):
        return text[:-1]
    if text.endswith("\u3002"):  # the Japanese full stop
        return text[:-1]
    return text


AUTO = "auto"  # Whisper tells the language of each piece itself

_LANGUAGE_NAMES = {"english": "en", "japanese": "ja", "french": "fr", "malagasy": "mg"}


def normalize_language(value: str | None) -> str:
    """'en-US', 'English', 'ja' -> 'en', 'en', 'ja'. Empty stays empty."""
    text = (value or "").strip().lower()
    text = _LANGUAGE_NAMES.get(text, text)
    return text.split("-", 1)[0].split("_", 1)[0]


class FakeEngine:
    """No chip: says how long each piece was. For tests and protocol trials."""

    name = "fake"

    def __init__(self, model: str = "fake", window_seconds: float = 5.0, delay: float = 0.0,
                 languages: tuple[str, ...] = ("en",)):
        self.model = model
        self.window_seconds = window_seconds
        self.delay = delay
        self.languages = list(languages)
        self.calls: list[dict] = []

    def transcribe(self, audio: np.ndarray, language: str | None = None, prompt: str | None = None) -> str:
        self.calls.append({"seconds": len(audio) / SAMPLE_RATE, "language": language, "prompt": prompt})
        if self.delay:
            time.sleep(self.delay)
        return f"speech of {len(audio) / SAMPLE_RATE:.1f} seconds"

    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        """The tests: the last language the engine knows, so it differs from the default."""
        self.detected_seconds = len(audio) / SAMPLE_RATE
        return self.languages[-1], 0.9

    def close(self) -> None:
        pass


class HailoEngine:
    name = "hailo"
    languages = ["en"]

    def __init__(self, settings: Settings):
        try:
            from hailo_apps.python.standalone_apps.speech_recognition import audio_utils
            from hailo_apps.python.standalone_apps.speech_recognition.whisper_pipeline import WhisperPipeline
        except ImportError as exc:  # pragma: no cover - only on the Pi
            raise EngineError(f"hailo-apps is not importable ({exc}); see README, '2. Install the service on worker01'") from exc

        self.model = settings.model
        self._preprocess = audio_utils.preprocess_audio
        encoder, decoder, npy_dir, arch = _resolve_models(settings)
        self.arch = arch
        self._pipeline = WhisperPipeline(
            encoder, decoder, variant=settings.model, npy_dir=npy_dir,
            add_embed=arch in ("hailo8", "hailo8l"),
        )
        self.window_seconds = float(self._pipeline.get_chunk_length())
        # The pipeline's thread opens the device; surface a failure now, not on first use.
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))

    def transcribe(self, audio: np.ndarray, language: str | None = None, prompt: str | None = None) -> str:
        # The Hailo pipeline takes no prompt, and its models are English only.
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 1e-6:
            audio = audio * (0.9 / peak)  # Whisper reads quiet input as silence
        texts = []
        for mel in self._preprocess(audio, chunk_length=int(self.window_seconds)):
            self._pipeline.send_data(mel)
            texts.append(self._result())
        return tidy(" ".join(texts))

    def _result(self, timeout: float = 60.0) -> str:
        results = getattr(self._pipeline, "_results_q", None)
        if results is None:  # pragma: no cover - older or newer hailo-apps
            return self._pipeline.get_transcription()
        try:
            return results.get(timeout=timeout)
        except Exception as exc:
            raise EngineError("the Hailo pipeline did not answer") from exc

    def close(self) -> None:
        self._pipeline.stop()


def _resolve_models(settings: Settings) -> tuple[str, str, str, str]:  # pragma: no cover - only on the Pi
    from hailo_apps.python.core.common.core import resolve_hef_paths
    from hailo_apps.python.core.common.defines import (
        RESOURCES_NPY_DIR_NAME, RESOURCES_ROOT_PATH_DEFAULT, WHISPER_H8_APP,
    )
    from hailo_apps.python.core.common.toolbox import resolve_arch
    from hailo_apps.python.standalone_apps.speech_recognition.speech_recognition import (
        VARIANT_MODELS, _ensure_npy_assets,
    )

    arch = resolve_arch(settings.arch)
    npy_dir = settings.npy_dir or str(Path(RESOURCES_ROOT_PATH_DEFAULT) / RESOURCES_NPY_DIR_NAME)
    if not settings.npy_dir:
        # The decoder's token embedding runs on the CPU from .npy files that hailo-apps
        # fetches separately from the HEFs. Its helper exits the process on failure.
        try:
            _ensure_npy_assets(settings.model, npy_dir, WHISPER_H8_APP, arch, RESOURCES_ROOT_PATH_DEFAULT)
        except SystemExit as exc:
            raise EngineError(f"the Whisper decoder assets could not be fetched into {npy_dir}") from exc
    if settings.hef_encoder and settings.hef_decoder:
        return settings.hef_encoder, settings.hef_decoder, npy_dir, arch
    try:
        encoder_name, decoder_name = VARIANT_MODELS[settings.model][arch]
    except KeyError as exc:
        raise EngineError(f"no Whisper {settings.model!r} model for {arch!r}") from exc
    resolved = resolve_hef_paths(hef_paths=[encoder_name, decoder_name], app_name=WHISPER_H8_APP, arch=arch)
    return str(resolved[0].path), str(resolved[1].path), npy_dir, arch


_FULL_STOPS = (".", "?", "!", "\u3002", "\uff1f", "\uff01")


def whisper_prompt(hints: str | None, language: str | None) -> str | None:
    """Spelling hints as Whisper's ``initial_prompt``. Whisper copies the prompt's style, so
    a bare list of terms makes it drop its full stops; ended like a sentence, it keeps them
    and still spells the terms (measured 2026-10-16, clew docs/28 §7)."""
    text = (hints or "").strip()
    if not text:
        return None
    cjk = language in ("ja", "zh")
    if cjk:
        text = re.sub(r"\s*[,\uff0c]\s*", "\u3001", text)
    if not text.endswith(_FULL_STOPS):
        text += "\u3002" if cjk else "."
    return text


_ITEM_SPLIT = re.compile(r"[,.?!;:\u3001\u3002\uff0c\uff1f\uff01]+")


def _items(text: str) -> list[str]:
    return [" ".join(i.split()).lower() for i in _ITEM_SPLIT.split(text) if i.strip()]


_LEAD_END = re.compile(r"[.?!\u3002\uff1f\uff01](?=\s)")


def split_prompt(prompt: str | None) -> tuple[str, list[str]]:
    """Clew's prompt is a sentence in the language, then the spelling hints, comma-separated
    (clew `sttPrompt`): "Manao ahoana tompoko. \u2026 Riana, Rojo". Returns the sentence part (up to
    its last full stop followed by more) and the hints."""
    text = (prompt or "").strip()
    if text.endswith(_FULL_STOPS):  # the sentence alone: the hints never end with a stop
        return text, []
    ends = list(_LEAD_END.finditer(text))
    cut = ends[-1].end() if ends else 0
    return text[:cut].strip(), _items(text[cut:])


def echoes_prompt(text: str, prompt: str | None) -> bool:
    """Whether Whisper wrote its spelling hints instead of the speech. Given audio it cannot
    follow (a language it knows little, a hesitation), it writes the hints back: on a Malagasy
    interview with three names as hints, 28 of 53 pieces came back with the names, none without
    them (measured 2026-10-16, clew docs/29 \u00a713). Echo: the text is hints with at most two other
    words, or it has two hints one after the other ("Riana, Rojo"). The sentence ahead of the
    hints is not counted: a one-word answer can rightly be one of its words."""
    if not text:
        return False
    terms = set(split_prompt(prompt)[1])
    if not terms:
        return False
    chunks = _items(text)
    hits = [c in terms for c in chunks]
    if not any(hits):
        return False
    if any(a and b for a, b in zip(hits, hits[1:])):
        return True
    rest = [w for c, hit in zip(chunks, hits) if not hit for w in c.split()]
    return len(rest) <= 2


def cuda_devices() -> int:
    """NVIDIA GPUs CUDA can see (0 without a driver). Its libraries may still be missing."""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # pragma: no cover - a build without CTranslate2
        return 0


class FasterWhisperEngine:
    """faster-whisper, int8 on the CPU, int8 with float16 on an NVIDIA GPU. The model is a
    folder made by `download`."""

    name = "whisper"
    window_seconds = 30.0

    def __init__(self, settings: Settings):
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:  # pragma: no cover - a broken build
            raise EngineError(f"faster-whisper is not importable ({exc})") from exc
        from .models import model_path

        path = model_path(settings.models_dir, settings.model)
        if path is None:
            raise EngineError(f"the model {settings.model!r} is not downloaded into {settings.models_dir}")
        self.model = settings.model
        device = settings.device
        if device == "auto":
            device = "cuda" if cuda_devices() > 0 else "cpu"
        try:
            self._load(str(path), device, settings)
        except Exception as exc:
            # A GPU without CUDA's libraries (cuBLAS, cuDNN) fails here or on the first call.
            if device != "cuda" or settings.device != "auto":
                raise
            log.warning("the GPU could not be used (%s); using the CPU", exc)
            self._load(str(path), "cpu", settings)

    def _load(self, path: str, device: str, settings: Settings) -> None:
        from faster_whisper import WhisperModel

        threads = settings.cpu_threads or min(8, os.cpu_count() or 4)
        compute = "int8_float16" if device == "cuda" else "int8"
        self._model = WhisperModel(path, device=device, compute_type=compute, cpu_threads=threads)
        self.device = device
        if self._model.model.is_multilingual:
            self.languages = list(self._model.supported_languages)
        else:
            self.languages = ["en"]
        # Load everything now, not on the first sentence.
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language=self.languages[0])

    def transcribe(self, audio: np.ndarray, language: str | None = None, prompt: str | None = None) -> str:
        """`language="auto"`: Whisper tells each piece's language itself (an interview that moves
        between languages). Otherwise one language; none means the model's first."""
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 1e-6:
            audio = audio * (0.9 / peak)
        detect = language == AUTO
        segments, info = self._model.transcribe(
            audio,
            language=None if detect else (language or self.languages[0]),
            initial_prompt=whisper_prompt(prompt, None if detect else language),
            beam_size=1,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,  # the segmenter has already found the speech
        )
        segments = list(segments)
        heard = info.language if detect else language
        joiner = "" if heard in ("ja", "zh") else " "
        return tidy(joiner.join(segment.text.strip() for segment in segments))

    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        """The recording's language, from a stretch of its speech (`Serial.detect`)."""
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 1e-6:
            audio = audio * (0.9 / peak)
        language, probability, _ = self._model.detect_language(audio[: SAMPLE_RATE * 30])
        return language, float(probability)

    def close(self) -> None:
        self._model = None


def load_engine(settings: Settings):
    if settings.engine == "fake":
        return FakeEngine()
    if settings.engine == "hailo":
        return HailoEngine(settings)
    if settings.engine == "whisper":
        return FasterWhisperEngine(settings)
    raise EngineError(f"unknown engine {settings.engine!r}")


@dataclass
class Serial:
    """Async front: one transcription at a time across all sessions and uploads."""

    engine: object
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _thread_lock: threading.Lock = field(default_factory=threading.Lock)
    calls: int = 0
    busy_seconds: float = 0.0
    # Pieces transcribed again without the prompt because the first try echoed it.
    echoes: int = 0

    @property
    def model(self) -> str:
        return self.engine.model

    @property
    def window_seconds(self) -> float:
        return self.engine.window_seconds

    @property
    def languages(self) -> list[str]:
        return list(getattr(self.engine, "languages", ["en"]))

    async def transcribe(self, audio: np.ndarray, language: str | None = None, prompt: str | None = None) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._run, audio, language, prompt)

    async def detect(self, audio: np.ndarray) -> tuple[str, float] | None:
        """The language of `audio`, or None for an engine that cannot tell (the Hailo one)."""
        detect = getattr(self.engine, "detect_language", None)
        if detect is None:
            return None
        async with self._lock:
            return await asyncio.to_thread(detect, audio)

    def _run(self, audio: np.ndarray, language: str | None, prompt: str | None) -> str:
        with self._thread_lock:
            started = time.monotonic()
            try:
                text = self.engine.transcribe(audio, language=language, prompt=prompt)
                if echoes_prompt(text, prompt):
                    # The hints, not the speech: once more with only the sentence ahead of
                    # them, which keeps the language (`echoes_prompt`).
                    self.echoes += 1
                    lead = split_prompt(prompt)[0] or None
                    text = self.engine.transcribe(audio, language=language, prompt=lead)
                return text
            finally:
                self.calls += 1
                self.busy_seconds += time.monotonic() - started
