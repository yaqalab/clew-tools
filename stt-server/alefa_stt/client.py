"""A test client: stream an audio file to /v1/listen at real-time pace, as dictation would.

Prints each result with its latency: how long after the end of that piece's audio
was sent the words came back. Uses `websockets`, which uvicorn[standard] installs.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote

from . import SAMPLE_RATE
from .audio import decode_file

CHUNK_SECONDS = 0.1


async def stream_file(url: str, key: str, path: str, ffmpeg: str, tmp_dir: str, speed: float = 1.0,
                      connect: str | None = None, decoder: str = "ffmpeg", language: str | None = None,
                      prompt: str | None = None) -> int:
    import websockets

    with open(path, "rb") as fh:
        audio = await decode_file(ffmpeg, fh.read(), tmp_dir, decoder)
    pcm = (audio.clip(-1, 1) * 32767).astype("<i2").tobytes()
    step = int(SAMPLE_RATE * CHUNK_SECONDS) * 2
    base = url.rstrip("/")
    if base.startswith("http"):
        base = "ws" + base[4:]
    target = f"{base}/v1/listen?encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1&vad_events=true"
    if language:
        target += f"&language={quote(language)}"
    if prompt:
        target += f"&prompt={quote(prompt)}"

    sent_at: list[tuple[float, float]] = []  # (audio seconds sent, wall clock)

    def sent_time(audio_seconds: float) -> float:
        for seconds, wall in sent_at:
            if seconds >= audio_seconds:
                return wall
        return sent_at[-1][1] if sent_at else time.monotonic()

    options = {}
    if connect:
        # Reach a given address (e.g. Traefik before DNS exists) but keep the name for TLS and Host.
        from urllib.parse import urlsplit

        parts = urlsplit(target)
        options = {"host": connect, "port": parts.port or (443 if parts.scheme == "wss" else 80)}
        if parts.scheme == "wss":
            options["server_hostname"] = parts.hostname
    async with websockets.connect(target, subprotocols=["alefa-stt", key], max_size=2**20, **options) as ws:
        async def sender():
            started = time.monotonic()
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i : i + step])
                seconds = (i + step) / 2 / SAMPLE_RATE
                sent_at.append((seconds, time.monotonic()))
                delay = started + seconds / speed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "CloseStream"}))

        task = asyncio.create_task(sender())
        texts = []
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "Results":
                end = message["start"] + message["duration"]
                latency = time.monotonic() - sent_time(end)
                text = message["channel"]["alternatives"][0]["transcript"]
                texts.append(text)
                mark = "." if message["speech_final"] else "~"
                print(f"[{message['start']:6.1f}s +{message['duration']:4.1f}s] {mark} {latency * 1000:5.0f} ms  {text}")
            elif kind == "Metadata":
                print(f"\n{message['duration']:.1f} s of audio\n\n" + " ".join(texts))
            elif kind in ("Error", "Warning"):
                print(f"{kind}: {message.get('description')}")
        await task
    return 0
