import io
import shutil
import wave

import numpy as np
import pytest

from alefa_stt import SAMPLE_RATE
from alefa_stt.auth import add_key_line, new_key
from alefa_stt.config import Settings

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def speechlike(pattern, seed=0):
    """[(seconds, loud?)] -> float32 audio: noise bursts at about -20 dBFS over -60 dBFS hiss."""
    rng = np.random.default_rng(seed)
    parts = []
    for seconds, loud in pattern:
        n = int(seconds * SAMPLE_RATE)
        parts.append(rng.normal(0, 0.1 if loud else 0.001, n))
    return np.clip(np.concatenate(parts), -1, 1).astype(np.float32)


def to_pcm16(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def to_wav(audio, rate=SAMPLE_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(to_pcm16(audio))
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(engine="fake", keys_file=str(tmp_path / "keys"), tmp_dir=str(tmp_path),
                    allowed_origins=["https://clew.alefa.net"], idle_seconds=5)


@pytest.fixture
def key(settings):
    key, line = new_key("tester")
    add_key_line(settings.keys_file, line)
    return key
