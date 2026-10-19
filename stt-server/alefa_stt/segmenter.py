"""Speech detection and cutting: 16 kHz mono float32 in, utterances out.

The server owns segmentation, so clients just stream. An utterance ends after
``endpointing_ms`` of silence. One that runs past the model's window (5 s for
Whisper base on a Hailo-8) is cut at its quietest point in the window's second
half, and the rest carries on as the next piece.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from . import SAMPLE_RATE

FRAME_MS = 20
FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples


class EnergyVad:
    """Loudness above an adaptive noise floor. Enough for a close headset microphone."""

    def __init__(self, threshold_db: float = 10.0, min_db: float = -50.0):
        self.threshold_db = threshold_db
        self.min_db = min_db
        self.floor_db = -60.0

    def __call__(self, frame: np.ndarray) -> bool:
        db = level_db(frame)
        if db < self.floor_db:
            self.floor_db += 0.3 * (db - self.floor_db)  # quiet: follow down quickly
        else:
            self.floor_db += 0.05  # loud: creep up slowly (2.5 dB/s), speech has gaps
        self.floor_db = min(max(self.floor_db, -90.0), -20.0)
        return db > max(self.floor_db + self.threshold_db, self.min_db)


class WebrtcVad:
    def __init__(self, aggressiveness: int = 2):
        import webrtcvad  # optional dependency

        self._vad = webrtcvad.Vad(aggressiveness)

    def __call__(self, frame: np.ndarray) -> bool:
        pcm = (np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes()
        return self._vad.is_speech(pcm, SAMPLE_RATE)


def make_vad(kind: str):
    return WebrtcVad() if kind == "webrtc" else EnergyVad()


def level_db(frame: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) if len(frame) else 0.0
    return 20 * math.log10(rms + 1e-9)


@dataclass
class SpeechStarted:
    timestamp: float


@dataclass
class Segment:
    audio: np.ndarray
    start: float
    speech_final: bool
    from_finalize: bool = False

    @property
    def duration(self) -> float:
        return len(self.audio) / SAMPLE_RATE


class Segmenter:
    def __init__(
        self,
        vad,
        max_seconds: float,
        endpointing_ms: int = 600,
        preroll_ms: int = 300,
        tail_ms: int = 200,
        min_speech_ms: int = 250,
        onset_frames: int = 3,
    ):
        self.vad = vad
        self.max_frames = max(int(max_seconds * 1000) // FRAME_MS, 10)
        self.end_frames = max(endpointing_ms // FRAME_MS, 1)
        self.tail_frames = tail_ms // FRAME_MS
        self.min_speech_frames = max(min_speech_ms // FRAME_MS, 1)
        self.onset_frames = onset_frames
        self._pending = np.zeros(0, dtype=np.float32)
        self._frame_index = 0  # frames consumed so far
        self._preroll: deque[tuple[np.ndarray, bool, float]] = deque(
            maxlen=max(preroll_ms // FRAME_MS, onset_frames)
        )
        self._onset = 0
        # The current utterance: (samples, is_speech, level_db) per frame.
        self._frames: list[tuple[np.ndarray, bool, float]] | None = None
        self._start_frame = 0
        self._silence = 0

    @property
    def speaking(self) -> bool:
        return self._frames is not None

    def feed(self, samples: np.ndarray) -> list:
        events: list = []
        data = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        whole = len(data) // FRAME * FRAME
        for i in range(0, whole, FRAME):
            self._step(data[i : i + FRAME], events)
        self._pending = data[whole:]
        return events

    def finalize(self) -> list:
        """Flush: the utterance in progress becomes final now."""
        events: list = []
        if len(self._pending):
            frame = np.zeros(FRAME, dtype=np.float32)
            frame[: len(self._pending)] = self._pending
            self._pending = np.zeros(0, dtype=np.float32)
            self._step(frame, events)
        if self._frames is not None:
            self._close(events, from_finalize=True)
        self._preroll.clear()
        self._onset = 0
        return events

    def _step(self, frame: np.ndarray, events: list) -> None:
        speech = bool(self.vad(frame))
        item = (frame, speech, level_db(frame))
        self._frame_index += 1
        if self._frames is None:
            self._preroll.append(item)
            self._onset = self._onset + 1 if speech else 0
            if self._onset >= self.onset_frames:
                self._frames = list(self._preroll)
                self._start_frame = self._frame_index - len(self._frames)
                self._silence = 0
                self._preroll.clear()
                self._onset = 0
                events.append(SpeechStarted(self._start_frame * FRAME_MS / 1000))
            return
        self._frames.append(item)
        self._silence = 0 if speech else self._silence + 1
        if self._silence >= self.end_frames:
            self._close(events)
        elif len(self._frames) >= self.max_frames:
            self._cut(events)

    def _close(self, events: list, from_finalize: bool = False) -> None:
        frames = self._frames or []
        drop = max(self._silence - self.tail_frames, 0)
        if drop:
            frames = frames[: len(frames) - drop]
        self._emit(frames, self._start_frame, True, from_finalize, events)
        self._frames = None
        self._silence = 0

    def _cut(self, events: list) -> None:
        frames = self._frames
        half = self.max_frames // 2
        # Quietest frame in the second half; ties go to the latest one.
        levels = [level for _, _, level in frames[half:]]
        quiet = half + max(range(len(levels)), key=lambda i: (-levels[i], i))
        head, rest = frames[:quiet], frames[quiet:]
        self._emit(head, self._start_frame, False, False, events)
        self._start_frame += len(head)
        self._frames = rest
        self._silence = 0
        for _, speech, _ in reversed(rest):
            if speech:
                break
            self._silence += 1

    def _emit(self, frames, start_frame, speech_final, from_finalize, events) -> None:
        if sum(1 for _, speech, _ in frames if speech) < self.min_speech_frames:
            return
        audio = np.concatenate([f for f, _, _ in frames])
        events.append(Segment(audio, start_frame * FRAME_MS / 1000, speech_final, from_finalize))
