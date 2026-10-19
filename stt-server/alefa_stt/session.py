"""One live dictation over a WebSocket (the /v1/listen protocol, see PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import SAMPLE_RATE
from .audio import FormatError, open_stream
from .config import Settings
from .engine import AUTO, Serial, continued, normalize_language
from .segmenter import Segment, Segmenter, SpeechStarted, make_vad

log = logging.getLogger("alefa_stt.session")

CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_INTERNAL = 1011
CLOSE_TRY_LATER = 1013


@dataclass
class ListenParams:
    encoding: str | None
    sample_rate: int | None
    channels: int
    container: str | None
    endpointing_ms: int
    vad_events: bool
    language: str = "en"
    prompt: str | None = None

    @classmethod
    def parse(cls, query, settings: Settings, languages: list[str] | None = None) -> "ListenParams":
        def flag(name: str) -> bool:
            return query.get(name, "false").lower() in ("1", "true", "yes")

        language = pick_language(query.get("language"), settings, languages or ["en"])
        try:
            sample_rate = int(query["sample_rate"]) if query.get("sample_rate") else None
            channels = int(query.get("channels", "1"))
            endpointing = int(query.get("endpointing", settings.endpointing_ms))
        except ValueError as exc:
            raise FormatError("sample_rate, channels and endpointing are numbers") from exc
        if sample_rate is not None and not 8000 <= sample_rate <= 192000:
            raise FormatError("sample_rate must be between 8000 and 192000")
        if not 1 <= channels <= 8:
            raise FormatError("channels must be between 1 and 8")
        return cls(
            encoding=query.get("encoding") or None,
            sample_rate=sample_rate,
            channels=channels,
            container=query.get("container") or None,
            endpointing_ms=min(max(endpointing, 200), 5000),
            vad_events=flag("vad_events"),
            language=language,
            prompt=clean_prompt(query.get("prompt")),
        )


PROMPT_LIMIT = 400


def offered_languages(settings: Settings, languages: list[str]) -> list[str]:
    """What /api/me lists: the tray's one language when it set one, else all the model knows."""
    fixed = normalize_language(settings.language)
    if fixed:
        return [fixed] if fixed in languages else []
    return list(languages)


def pick_language(asked: str | None, settings: Settings, languages: list[str], allow_auto: bool = False) -> str:
    offered = offered_languages(settings, languages)
    if allow_auto and (asked or "").strip().lower() == AUTO and len(offered) > 1:
        return AUTO
    language = normalize_language(asked) or (offered[0] if len(offered) == 1 else "en")
    if language not in offered:
        if offered == ["en"]:
            raise FormatError("only English (language=en) is available")
        raise FormatError(f"language {language!r} is not available here; this server offers "
                          f"{', '.join(offered[:12])}")
    return language


def clean_prompt(text: str | None) -> str | None:
    """Spelling hints for Whisper (`initial_prompt`), at most PROMPT_LIMIT characters."""
    text = " ".join((text or "").split())
    return text[:PROMPT_LIMIT] or None


class ListenSession:
    def __init__(self, ws: WebSocket, engine: Serial, settings: Settings, params: ListenParams, key_name: str):
        self.ws = ws
        self.engine = engine
        self.settings = settings
        self.params = params
        self.key_name = key_name
        self.request_id = str(uuid.uuid4())
        self.segmenter = Segmenter(make_vad(settings.vad), engine.window_seconds, params.endpointing_ms)
        self.samples_in = 0
        self._queue: asyncio.Queue[Segment | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._stream = None
        self._closed = False

    async def send(self, message: dict) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(message))

    async def _on_audio(self, samples: np.ndarray) -> None:
        self.samples_in += len(samples)
        await self._dispatch(self.segmenter.feed(samples))

    async def _dispatch(self, events: list) -> None:
        for event in events:
            if isinstance(event, SpeechStarted):
                if self.params.vad_events:
                    await self.send({"type": "SpeechStarted", "channel": [0, 1], "timestamp": round(event.timestamp, 3)})
            else:
                await self._queue.put(event)

    async def _transcriber(self) -> None:
        while (segment := await self._queue.get()) is not None:
            text = await self.engine.transcribe(segment.audio, language=self.params.language,
                                                prompt=self.params.prompt)
            if text and not segment.speech_final:
                text = continued(text)
            if text:
                await self.send(self._results(segment, text))

    def _results(self, segment: Segment, text: str) -> dict:
        return {
            "type": "Results",
            "channel_index": [0, 1],
            "start": round(segment.start, 3),
            "duration": round(segment.duration, 3),
            "is_final": True,
            "speech_final": segment.speech_final,
            "from_finalize": segment.from_finalize,
            "channel": {"alternatives": [{"transcript": text, "words": []}]},
            "metadata": {"request_id": self.request_id, "model_info": {"name": f"whisper-{self.engine.model}"}},
        }

    async def run(self) -> None:
        started = time.monotonic()
        try:
            self._stream = await open_stream(
                self.settings.ffmpeg, self.params.encoding, self.params.sample_rate,
                self.params.channels, self.params.container, self._on_audio, self.settings.decoder,
            )
        except (FormatError, OSError) as exc:
            await self._fail(CLOSE_UNSUPPORTED, str(exc))
            return
        worker = asyncio.create_task(self._transcriber())
        reason = "client closed"
        try:
            while True:
                left = self.settings.max_session_seconds - (time.monotonic() - started)
                if left <= 0:
                    reason = "session time limit"
                    break
                try:
                    message = await asyncio.wait_for(self.ws.receive(), timeout=min(self.settings.idle_seconds, left))
                except asyncio.TimeoutError:
                    reason = "idle"
                    break
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))
                if message.get("bytes") is not None:
                    await self._stream.write(message["bytes"])
                elif message.get("text") is not None:
                    kind = _control_type(message["text"])
                    if kind == "Finalize":
                        await self._stream.flush()
                        await self._dispatch(self.segmenter.finalize())
                    elif kind == "CloseStream":
                        break
                    elif kind != "KeepAlive":
                        await self.send({"type": "Warning", "description": f"unknown message type {kind!r} ignored"})
            # Normal end: decode what is left, transcribe it, then say goodbye.
            await self._stream.close()
            await self._dispatch(self.segmenter.finalize())
            await self._queue.put(None)
            await worker
            await self.send(self._metadata())
            await self._close(CLOSE_NORMAL, reason)
        except WebSocketDisconnect:
            self._closed = True
            worker.cancel()
            await self._stream.abort()
        except FormatError as exc:
            worker.cancel()
            await self._stream.abort()
            await self._fail(CLOSE_UNSUPPORTED, str(exc))
        except Exception:
            log.exception("session %s failed", self.request_id)
            worker.cancel()
            await self._stream.abort()
            await self._fail(CLOSE_INTERNAL, "internal error")
        finally:
            log.info("session %s key=%s audio=%.1fs (%s)", self.request_id, self.key_name,
                     self.samples_in / SAMPLE_RATE, reason)

    def _metadata(self) -> dict:
        return {
            "type": "Metadata",
            "request_id": self.request_id,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration": round(self.samples_in / SAMPLE_RATE, 3),
            "channels": 1,
            "models": [f"whisper-{self.engine.model}"],
        }

    async def _fail(self, code: int, description: str) -> None:
        await self.send({"type": "Error", "description": description})
        await self._close(code, description)

    async def _close(self, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(code=code, reason=reason[:120])
        except RuntimeError:
            pass


def _control_type(text: str) -> str:
    try:
        message = json.loads(text)
    except ValueError:
        return "?"
    return message.get("type", "?") if isinstance(message, dict) else "?"
