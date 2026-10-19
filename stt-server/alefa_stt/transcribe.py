"""Live transcription of a recording, with speakers (clew docs/29).

``/v1/transcribe`` is a WebSocket, like ``/v1/listen``, but for a recording rather than a
microphone: the server works through it as fast as it can and sends one **turn** per piece of
speech, with its times and its voice. The client decides when each turn is shown (in step with
playback, or ahead of it); the server just works ahead.

Two sources:

- ``source=upload`` (real time, D-l): the client sends the file's bytes, then ``EndOfFile``. They
  are held in memory, never written to disk: they are an interview.
- ``source=folder&path=...``: the server reads the file from the audio server's folder
  (``--audio-root``), which the tray hands it. Only audio files inside that folder.
- ``source=url&url=...`` (deferred): the server reads what the audio server serves at that link,
  by ranges: the same bytes the player plays, a converted copy included (a variable-bit-rate mp3
  is served converted, so its times are the player's). Links on this computer only.

``language=auto`` decides the recording's language once, from its first ``DETECT_SECONDS`` of
speech, and keeps it (a ``Language`` message says which): a second or two of speech is too little
to tell, and deciding per piece gave six languages in a minute of Malagasy (docs/29 §10).

Messages from the client, as JSON text:

- ``{"type": "Start", "voices": [...]}`` first, with the voices this transcript kept from earlier
  sessions (``Voice`` messages, as below), so the same people keep their names.
- ``{"type": "EndOfFile"}`` after the bytes, for an upload.
- ``{"type": "Merge", "from": "v3", "into": "v1"}`` when the person says two voices are one.
- ``{"type": "CloseStream"}`` to stop.

From the server: ``Language`` (the one decided, with ``auto``), ``Voice`` (a new voice: id, vector,
count), ``Turn`` (start, end, text, voice, speech_final), ``Progress`` (seconds done, duration), and
at the end ``Voices`` (all of them, for the client to keep) and ``Done``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import SAMPLE_RATE
from .audio import FormatError
from .config import Settings
from .engine import AUTO, Serial, continued
from .segmenter import Segment, Segmenter, make_vad
from .session import CLOSE_INTERNAL, CLOSE_NORMAL, CLOSE_POLICY, clean_prompt, pick_language
from .speakers import Voices

log = logging.getLogger("alefa_stt.transcribe")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".weba", ".webm",
                    ".mp4", ".3gp", ".amr", ".wma"}
PROGRESS_EVERY = 10.0  # seconds of audio
DETECT_SECONDS = 20.0  # of speech, to decide the recording's language with `auto`
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def check_link(url: str) -> str:
    """A link the server may read: plain http to this computer (the audio server), no user info."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise PathError("not a link") from exc
    if parts.scheme != "http" or parts.hostname not in LOOPBACK or parts.username or parts.password:
        raise PathError("only a link to the audio server on this computer")
    return url
CHUNK = SAMPLE_RATE  # one second of audio per step through the segmenter


class PathError(ValueError):
    pass


def resolve_served(root: str, folders: dict[str, str], path: str) -> Path:
    """The audio file `path` names, under the audio server's rules for several folders (clew docs/22
    §11): `folders` maps each folder's name to its path, "" for one that is off. A path whose first
    segment is a folder's name is looked for there only (refused while it is off); any other was
    saved when there was one folder, and is used when exactly one folder that is on has it.
    Without `folders`, `root` alone (a server started with --audio-root)."""
    if not folders:
        return resolve_in_root(root, path)
    first, _, rest = path.replace("\\", "/").partition("/")
    if rest and first in folders:
        if not folders[first]:
            raise PathError("that audio folder is off")
        return resolve_in_root(folders[first], rest)
    found = []
    for folder in folders.values():
        if not folder:
            continue
        try:
            found.append(resolve_in_root(folder, path))
        except PathError:
            pass
    if len(found) != 1:
        raise PathError("no such audio file" if not found else "more than one folder has that file")
    return found[0]


def resolve_in_root(root: str, rel: str) -> Path:
    """The audio file `rel` names inside `root`, or PathError. The audio server's rules: no
    absolute path, no "..", no hidden segment, no ":" (Windows streams and drives), an audio
    extension, and the real path (links followed) still inside the folder."""
    if not root:
        raise PathError("this server has no audio folder")
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p]
    if not parts or rel.startswith("/") or ":" in rel:
        raise PathError("not a path inside the audio folder")
    if any(p in (".", "..") or p.startswith(".") or p != p.rstrip(" .") for p in parts):
        raise PathError("not a path inside the audio folder")
    base = Path(root).resolve()
    target = base.joinpath(*parts).resolve()
    if base != target and base not in target.parents:
        raise PathError("not a path inside the audio folder")
    if target.suffix.lower() not in AUDIO_EXTENSIONS or not target.is_file():
        raise PathError("no such audio file")
    return target


@dataclass
class TranscribeParams:
    source: str
    path: str
    url: str
    start: float
    language: str
    prompt: str | None
    endpointing_ms: int
    speakers: bool

    @classmethod
    def parse(cls, query, settings: Settings, languages: list[str]) -> "TranscribeParams":
        source = query.get("source", "upload")
        if source not in ("upload", "folder", "url"):
            raise FormatError("source is upload, folder or url")
        try:
            start = max(0.0, float(query.get("start", "0")))
            endpointing = int(query.get("endpointing", settings.endpointing_ms))
        except ValueError as exc:
            raise FormatError("start and endpointing are numbers") from exc
        return cls(
            source=source,
            path=query.get("path", ""),
            url=query.get("url", ""),
            start=start,
            language=pick_language(query.get("language"), settings, languages, allow_auto=True),
            prompt=clean_prompt(query.get("prompt")),
            endpointing_ms=min(max(endpointing, 200), 5000),
            speakers=query.get("speakers", "true").lower() not in ("0", "false", "no"),
        )


def _decode_into(source, start: float, put, stop: threading.Event, on_duration=None) -> float:
    """Decodes `source` (a path or a file-like) from `start` seconds into 16 kHz mono float32,
    one second at a time, through `put(chunk, offset)`. Returns the recording's duration, and
    tells `on_duration` it as soon as the file is open, so progress has a total from the start."""
    import av

    options = {"rw_timeout": "20000000"} if isinstance(source, str) and source.startswith("http") else {}
    with av.open(source, mode="r", options=options) as container:
        if not container.streams.audio:
            raise FormatError("there is no audio in the file")
        stream = container.streams.audio[0]
        duration = float(container.duration / 1_000_000) if container.duration else 0.0
        if on_duration is not None:
            on_duration(duration)
        if start > 0:
            container.seek(int(start * 1_000_000), any_frame=False, backward=True)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        pending: list[np.ndarray] = []
        offset: float | None = None
        size = 0

        def flush(final: bool = False):
            nonlocal pending, size, offset
            if not pending:
                return
            data = np.concatenate(pending).astype(np.float32) / 32768.0
            pending, size = [], 0
            put(data, offset)
            offset = None

        for frame in container.decode(stream):
            if stop.is_set():
                return duration
            frame_time = float(frame.time) if frame.time is not None else 0.0
            for out in resampler.resample(frame):
                samples = out.to_ndarray().reshape(-1)
                t = frame_time
                if t < start:  # the seek lands on a frame before `start`
                    skip = int((start - t) * SAMPLE_RATE)
                    samples = samples[skip:]
                    t = start
                if not len(samples):
                    continue
                if offset is None:
                    offset = t
                pending.append(samples)
                size += len(samples)
                if size >= CHUNK:
                    flush()
        for out in resampler.resample(None):
            pending.append(out.to_ndarray().reshape(-1))
        flush(final=True)
        return duration


class TranscribeSession:
    def __init__(self, ws: WebSocket, engine: Serial, speaker_model, settings: Settings,
                 params: TranscribeParams, key_name: str):
        self.ws = ws
        self.engine = engine
        self.speaker_model = speaker_model if params.speakers else None
        self.settings = settings
        self.params = params
        self.key_name = key_name
        self.request_id = str(uuid.uuid4())
        self.voices = Voices()
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._stop = threading.Event()
        self._cancelled = False  # the client said CloseStream, or left

    async def send(self, message: dict) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(message, ensure_ascii=False))

    async def _receive_json(self) -> dict | None:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("text") is None:
            return None
        try:
            value = json.loads(message["text"])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def _upload(self) -> io.BytesIO:
        limit = self.settings.max_transcribe_mb * 1024 * 1024
        buf = io.BytesIO()
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                buf.write(message["bytes"])
                if buf.tell() > limit:
                    raise FormatError(f"the recording is larger than {self.settings.max_transcribe_mb} MB")
            elif message.get("text") is not None:
                kind = _type_of(message["text"])
                if kind == "EndOfFile":
                    buf.seek(0)
                    return buf
                if kind == "CloseStream":
                    raise WebSocketDisconnect(CLOSE_NORMAL)

    async def _listen_for_controls(self) -> None:
        """While working: Merge and CloseStream. A client gone stops the work."""
        try:
            await self._controls()
        except (WebSocketDisconnect, RuntimeError):
            self._closed = True
            self._cancelled = True
            self._stop.set()

    async def _controls(self) -> None:
        while True:
            message = await self._receive_json()
            if message is None:
                continue
            kind = message.get("type")
            if kind == "CloseStream":
                self._cancelled = True
                self._stop.set()
                return
            if kind == "Merge":
                merged = self.voices.merge(str(message.get("from", "")), str(message.get("into", "")))
                if merged is not None:
                    await self.send({"type": "Voice", **merged.as_message()})

    async def run(self) -> None:
        try:
            start = await self._receive_json()
            if start and start.get("type") == "Start":
                self.voices = Voices.from_messages(start.get("voices") or [])
            if self.params.source == "folder":
                source = resolve_served(self.settings.audio_root or "", self.settings.audio_folders,
                                        self.params.path)
                source = str(source)
            elif self.params.source == "url":
                source = check_link(self.params.url)
            else:
                source = await self._upload()
        except PathError as exc:
            await self._fail(CLOSE_POLICY, str(exc))
            return
        except FormatError as exc:
            await self._fail(CLOSE_POLICY, str(exc))
            return
        except WebSocketDisconnect:
            self._closed = True
            return
        controls = asyncio.create_task(self._listen_for_controls())
        try:
            duration = await self._work(source)
            if not self._cancelled:
                await self.send({"type": "Voices", "voices": [v.as_message() for v in self.voices.voices]})
                await self.send({"type": "Done", "duration": round(duration, 3)})
            await self._close(CLOSE_NORMAL, "done")
        except WebSocketDisconnect:
            self._closed = True
        except FormatError as exc:
            await self._fail(CLOSE_POLICY, str(exc))
        except Exception:
            log.exception("transcription %s failed", self.request_id)
            await self._fail(CLOSE_INTERNAL, "internal error")
        finally:
            self._stop.set()
            controls.cancel()

    async def _work(self, source) -> float:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        segmenter = Segmenter(make_vad(self.settings.vad), self.engine.window_seconds, self.params.endpointing_ms)
        state = {"base": None, "duration": 0.0}

        def put(chunk: np.ndarray, offset: float | None) -> None:
            if state["base"] is None:
                state["base"] = offset if offset is not None else self.params.start
            events = segmenter.feed(chunk)
            for e in events:
                if isinstance(e, Segment):
                    asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()

        def decode() -> None:
            try:
                state["duration"] = _decode_into(source, self.params.start, put, self._stop,
                                                 lambda d: state.__setitem__("duration", d))
                for e in segmenter.finalize():
                    if isinstance(e, Segment):
                        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()
            except Exception as exc:  # the client's bytes
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        decoder = threading.Thread(target=decode, name="transcribe-decode", daemon=True)
        decoder.start()
        last_progress = 0.0
        previous_final = True
        previous_voice = None
        language = self.params.language
        held: list = []  # pieces gathered to decide the language, then worked through first

        async def next_item():
            return held.pop(0) if held else await queue.get()

        try:
            if language == AUTO:
                gathered = 0.0
                while gathered < DETECT_SECONDS:
                    item = await queue.get()
                    held.append(item)
                    if item is None or isinstance(item, Exception):
                        break
                    gathered += item.duration
                speech = [s.audio for s in held if isinstance(s, Segment)]
                found = await self.engine.detect(np.concatenate(speech)) if speech else None
                language = found[0] if found else None
                if found:
                    await self.send({"type": "Language", "language": found[0], "probability": round(found[1], 2)})
            while (item := await next_item()) is not None:
                if isinstance(item, Exception):
                    raise FormatError(str(item) or "the audio could not be decoded") from item
                if self._stop.is_set():
                    continue
                segment: Segment = item
                base = state["base"] or 0.0
                start = base + segment.start
                end = start + segment.duration
                text = await self.engine.transcribe(segment.audio, language=language or AUTO,
                                                    prompt=self.params.prompt)
                if text and not segment.speech_final:
                    text = continued(text)
                voice_id = None
                if self.speaker_model is not None:
                    if not previous_final and previous_voice is not None:
                        voice_id = previous_voice  # the same sentence, cut by the window
                    else:
                        vector = await asyncio.to_thread(self.speaker_model.embed, segment.audio)
                        voice, new = self.voices.assign(vector, segment.duration)
                        voice_id = voice.id
                        if new:
                            await self.send({"type": "Voice", **voice.as_message()})
                previous_final, previous_voice = segment.speech_final, voice_id
                if text:
                    await self.send({"type": "Turn", "start": round(start, 3), "end": round(end, 3),
                                     "text": text, "voice": voice_id, "speech_final": segment.speech_final})
                if end - last_progress >= PROGRESS_EVERY:
                    last_progress = end
                    await self.send({"type": "Progress", "seconds": round(end, 1),
                                     "duration": round(state["duration"], 1)})
        finally:
            self._stop.set()
            # Let the decoder finish its put() calls, so its thread ends.
            while decoder.is_alive():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)
        return state["duration"]

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


def _type_of(text: str) -> str:
    try:
        value = json.loads(text)
    except ValueError:
        return "?"
    return value.get("type", "?") if isinstance(value, dict) else "?"
