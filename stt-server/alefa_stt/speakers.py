"""Telling voices apart, for live transcription (clew docs/29 §3).

Each piece of speech gets a voice vector from a small speaker-embedding model (3D-Speaker CAM++,
run by sherpa-onnx), and is matched to the voices heard so far by cosine similarity. Measured on
2026-10-16 (docs/29 §7): on whole sentences, the same speaker scored 0.65 or more and two
speakers 0.54 or less, so 0.6 is the line. A second of speech is too little to tell (the same
speaker fell to 0.21), so a short piece joins the voice before it unless it clearly matches
another.

Voices are numbered v1, v2... A transcript keeps its voices (sealed, in Clew) and hands them back
at the next session, so the same interview's voices keep their names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from . import SAMPLE_RATE

SAME_VOICE = 0.6
SHORT_SECONDS = 1.5


class SpeakerError(RuntimeError):
    pass


@dataclass
class Voice:
    id: str
    vector: np.ndarray  # unit length
    count: int = 1

    def as_message(self) -> dict:
        return {"id": self.id, "vector": [round(float(x), 5) for x in self.vector], "count": self.count}


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


@dataclass
class Voices:
    """The voices of one transcript, matched and updated one piece at a time."""

    voices: list[Voice] = field(default_factory=list)
    last: Voice | None = None

    @classmethod
    def from_messages(cls, items: list) -> "Voices":
        out = cls()
        for item in items or []:
            try:
                vid = str(item["id"])
                vec = _unit(np.asarray(item["vector"], dtype=np.float32))
                count = max(1, int(item.get("count", 1)))
            except (KeyError, TypeError, ValueError):
                continue
            if vid and vec.ndim == 1 and len(vec) > 0 and not any(v.id == vid for v in out.voices):
                out.voices.append(Voice(vid, vec, count))
        return out

    def _next_id(self) -> str:
        used = {v.id for v in self.voices}
        n = 1
        while f"v{n}" in used:
            n += 1
        return f"v{n}"

    def assign(self, vector: np.ndarray, seconds: float) -> tuple[Voice, bool]:
        """The voice this piece belongs to, and whether it is new."""
        vector = _unit(np.asarray(vector, dtype=np.float32))
        best, score = None, -1.0
        for voice in self.voices:
            if len(voice.vector) != len(vector):
                continue
            s = float(voice.vector @ vector)
            if s > score:
                best, score = voice, s
        if seconds < SHORT_SECONDS:
            # Too short to tell: the voice before it, unless it clearly matches another.
            chosen = best if best is not None and score >= SAME_VOICE else self.last
            if chosen is not None:
                self.last = chosen
                return chosen, False
        elif best is not None and score >= SAME_VOICE:
            best.vector = _unit(best.vector * best.count + vector)
            best.count += 1
            self.last = best
            return best, False
        voice = Voice(self._next_id(), vector)
        self.voices.append(voice)
        self.last = voice
        return voice, True

    def merge(self, source: str, target: str) -> Voice | None:
        """The person says two voices are one: `source` goes into `target`."""
        a = next((v for v in self.voices if v.id == source), None)
        b = next((v for v in self.voices if v.id == target), None)
        if a is None or b is None or a is b or len(a.vector) != len(b.vector):
            return None
        b.vector = _unit(b.vector * b.count + a.vector * a.count)
        b.count += a.count
        self.voices.remove(a)
        if self.last is a:
            self.last = b
        return b


def _load_own_onnxruntime() -> None:
    """Windows ships its own, older onnxruntime.dll in System32, and a process that prefers
    System32's images (the audio server's tray hardens itself so, and its children inherit it)
    loaded that one for sherpa-onnx, which then crashed (found 2026-10-16). Loading the one
    sherpa-onnx brings by its full path first makes every later load by name find it."""
    if os.name != "nt":
        return
    import ctypes
    import importlib.util

    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or not spec.origin:
        return
    dll = os.path.join(os.path.dirname(spec.origin), "lib", "onnxruntime.dll")
    if os.path.isfile(dll):
        ctypes.WinDLL(dll)


class SpeakerModel:
    """The embedding model. One call at a time is enough: a piece takes about 30 ms."""

    def __init__(self, path: str, threads: int = 2):
        _load_own_onnxruntime()
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - a build without it
            raise SpeakerError(f"sherpa-onnx is not importable ({exc})") from exc
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=path, num_threads=threads)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(self, audio: np.ndarray) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(audio, dtype=np.float32))
        stream.input_finished()
        return _unit(np.asarray(self._extractor.compute(stream), dtype=np.float32))


class FakeSpeakerModel:
    """For tests: the loudness of the piece stands for the voice (two levels, two voices)."""

    def embed(self, audio: np.ndarray) -> np.ndarray:
        level = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
        return _unit(np.array([1.0, 0.0]) if level > 0.07 else np.array([0.0, 1.0]))
