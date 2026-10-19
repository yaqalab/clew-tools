"""HTTP and WebSocket surface. The wire format is in PROTOCOL.md."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile
from starlette.middleware.cors import CORSMiddleware

from . import SAMPLE_RATE, __version__
from .audio import FormatError, decode_file
from .auth import KeyStore, SingleKey
from .config import Settings
from .engine import Serial, continued, load_engine
from .segmenter import Segment, Segmenter, make_vad
from .session import (CLOSE_POLICY, CLOSE_TRY_LATER, ListenParams, ListenSession, clean_prompt,
                      offered_languages, pick_language)
from .speakers import FakeSpeakerModel, SpeakerError, SpeakerModel
from .transcribe import TranscribeParams, TranscribeSession

log = logging.getLogger("alefa_stt")

SUBPROTOCOL = "alefa-stt"
RESPONSE_FORMATS = ("json", "text", "verbose_json")


class ApiError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
LOOPBACK_ORIGIN = r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


def create_app(settings: Settings, engine=None) -> FastAPI:
    keys = SingleKey(settings.single_key) if settings.single_key else KeyStore(settings.keys_file)
    state = {"engine": Serial(engine) if engine else None, "sessions": 0, "speakers": None}

    def speaker_model():
        """Loaded on first use; None when the model is not downloaded."""
        if state["speakers"] is None:
            if settings.speakers == "fake":
                state["speakers"] = FakeSpeakerModel()
            else:
                from .models import speaker_model_path

                path = speaker_model_path(settings.models_dir)
                if path is None:
                    return None
                try:
                    state["speakers"] = SpeakerModel(str(path))
                except SpeakerError as exc:
                    log.warning("speaker model: %s", exc)
                    return None
        return state["speakers"]

    def speakers_available() -> bool:
        if settings.speakers == "fake":
            return True
        from .models import speaker_model_path

        return speaker_model_path(settings.models_dir) is not None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["engine"] is None:
            log.info("loading engine %s (model %s)", settings.engine, settings.model)
            loaded = await asyncio.to_thread(load_engine, settings)
            state["engine"] = Serial(loaded)
            log.info("engine ready: %s, %.0f s window", loaded.model, loaded.window_seconds)
        yield
        await asyncio.to_thread(state["engine"].engine.close)

    app = FastAPI(title="alefa-stt", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_origin_regex=LOOPBACK_ORIGIN if settings.loopback_origins else None,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    app.add_middleware(QuietLogAndNoStore)
    if settings.loopback_only:
        app.add_middleware(LoopbackHostOnly)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse({"error": {"message": exc.message, "type": exc.kind}}, status_code=exc.status)

    async def check_key(key: str | None) -> str | None:
        name = await asyncio.to_thread(keys.verify, key)
        if name is None and keys.should_slow_down:
            await asyncio.sleep(1)
        return name

    async def require_key(request: Request) -> str:
        name = await check_key(_bearer(request.headers.get("authorization")))
        if name is None:
            raise ApiError(401, "missing or wrong API key", "authentication_error")
        return name

    @app.get("/api/ping")
    async def ping():
        return {"server": "alefa-stt", "api": 1, "version": __version__, "ready": state["engine"] is not None}

    @app.get("/api/me")
    async def me(request: Request):
        name = await require_key(request)
        engine: Serial = state["engine"]
        return {"key": name, "model": f"whisper-{engine.model}", "window_seconds": engine.window_seconds,
                "languages": offered_languages(settings, engine.languages),
                "device": getattr(engine.engine, "device", "cpu"),
                "transcribe": True, "speakers": speakers_available(), "folder": bool(settings.audio_root or any(settings.audio_folders.values()))}

    @app.websocket("/v1/listen")
    async def listen(ws: WebSocket):
        origin = ws.headers.get("origin")
        if origin and not origin_allowed(origin, settings):
            await ws.close(code=CLOSE_POLICY)  # before accept: the client sees HTTP 403
            return
        offered = ws.scope.get("subprotocols") or []
        key = _bearer(ws.headers.get("authorization")) or next(
            (p for p in offered if p.startswith("stt.")), None)
        name = await check_key(key)
        if name is None:
            await ws.close(code=CLOSE_POLICY)
            return
        await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        try:
            params = ListenParams.parse(ws.query_params, settings, state["engine"].languages)
        except FormatError as exc:
            await ws.send_json({"type": "Error", "description": str(exc)})
            await ws.close(code=CLOSE_POLICY, reason=str(exc)[:120])
            return
        if state["sessions"] >= settings.max_sessions:
            await ws.send_json({"type": "Error", "description": "too many sessions; try again shortly"})
            await ws.close(code=CLOSE_TRY_LATER)
            return
        state["sessions"] += 1
        try:
            await ListenSession(ws, state["engine"], settings, params, name).run()
        finally:
            state["sessions"] -= 1

    @app.websocket("/v1/transcribe")
    async def transcribe(ws: WebSocket):
        origin = ws.headers.get("origin")
        if origin and not origin_allowed(origin, settings):
            await ws.close(code=CLOSE_POLICY)
            return
        offered = ws.scope.get("subprotocols") or []
        key = _bearer(ws.headers.get("authorization")) or next(
            (p for p in offered if p.startswith("stt.")), None)
        name = await check_key(key)
        if name is None:
            await ws.close(code=CLOSE_POLICY)
            return
        await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        engine: Serial = state["engine"]
        try:
            params = TranscribeParams.parse(ws.query_params, settings, engine.languages)
        except FormatError as exc:
            await ws.send_json({"type": "Error", "description": str(exc)})
            await ws.close(code=CLOSE_POLICY, reason=str(exc)[:120])
            return
        if state["sessions"] >= settings.max_sessions:
            await ws.send_json({"type": "Error", "description": "too many sessions; try again shortly"})
            await ws.close(code=CLOSE_TRY_LATER)
            return
        model = await asyncio.to_thread(speaker_model) if params.speakers else None
        if params.speakers and model is None:
            await ws.send_json({"type": "Warning",
                                "description": "the speaker model is not downloaded: turns come without voices"})
        state["sessions"] += 1
        try:
            await TranscribeSession(ws, engine, model, settings, params, name).run()
        finally:
            state["sessions"] -= 1

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        await require_key(request)
        limit = settings.max_upload_mb * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > limit + 64 * 1024:
            raise ApiError(413, f"the upload is larger than {settings.max_upload_mb} MB")
        form = await request.form(max_files=1, max_fields=16)
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "send the audio as a multipart field named 'file'")
        engine: Serial = state["engine"]
        try:
            language = pick_language(str(form.get("language") or ""), settings, engine.languages)
        except FormatError as exc:
            raise ApiError(400, str(exc)) from exc
        prompt = clean_prompt(str(form.get("prompt") or ""))
        response_format = str(form.get("response_format") or "json")
        if response_format not in RESPONSE_FORMATS:
            raise ApiError(400, f"response_format must be one of {', '.join(RESPONSE_FORMATS)}")
        data = await upload.read(limit + 1)
        await form.close()
        if len(data) > limit:
            raise ApiError(413, f"the upload is larger than {settings.max_upload_mb} MB")
        try:
            audio = await decode_file(settings.ffmpeg, data, settings.tmp_dir, settings.decoder)
        except FormatError as exc:
            raise ApiError(400, f"could not decode the audio: {exc}") from exc
        del data
        duration = len(audio) / SAMPLE_RATE
        if duration > settings.max_file_seconds:
            raise ApiError(413, f"the audio is longer than {settings.max_file_seconds} seconds")
        segmenter = Segmenter(make_vad(settings.vad), engine.window_seconds, settings.endpointing_ms)
        pieces = [e for e in segmenter.feed(audio) + segmenter.finalize() if isinstance(e, Segment)]
        segments = []
        for piece in pieces:
            text = await engine.transcribe(piece.audio, language=language, prompt=prompt)
            if text and not piece.speech_final:
                text = continued(text)
            if text:
                segments.append({"id": len(segments), "start": round(piece.start, 2),
                                 "end": round(piece.start + piece.duration, 2), "text": text})
        text = " ".join(s["text"] for s in segments)
        if response_format == "text":
            return PlainTextResponse(text)
        if response_format == "verbose_json":
            return {"task": "transcribe", "language": LANGUAGE_NAMES.get(language, language),
                    "duration": round(duration, 2),
                    "text": text, "segments": segments}
        return {"text": text}

    return app


LANGUAGE_NAMES = {"en": "english", "ja": "japanese", "fr": "french", "mg": "malagasy"}


def origin_allowed(origin: str, settings: Settings) -> bool:
    origin = origin.rstrip("/")
    if origin in settings.allowed_origins:
        return True
    return settings.loopback_origins and re.match(LOOPBACK_ORIGIN, origin) is not None


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host


class LoopbackHostOnly:
    """Refuses any request whose Host is not this computer, so a web page cannot reach the
    server through a name it controls that resolves to 127.0.0.1 (DNS rebinding)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            if _host_name(host) not in LOOPBACK_HOSTS:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": CLOSE_POLICY})
                    return
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b"this server answers only to 127.0.0.1"})
                return
        return await self.app(scope, receive, send)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() in ("bearer", "token") and value.strip() else None


class QuietLogAndNoStore:
    """Logs method, path and status, never the query or headers; marks every answer no-store."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"cache-control", b"no-store")]
            elif message["type"] == "websocket.accept":
                status["code"] = 101
            elif message["type"] == "websocket.close" and status["code"] is None:
                status["code"] = 403
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            method = scope.get("method", "WS")
            log.info("%s %s %s %.0fms", method, scope["path"], status["code"], (time.monotonic() - started) * 1000)
