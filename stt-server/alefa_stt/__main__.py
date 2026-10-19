"""Command line: serve, manage keys, fetch a model, or transcribe a file locally (the benchmark)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from . import SAMPLE_RATE, __version__
from .auth import add_key_line, new_key, revoke_key, KeyStore
from .config import Settings, split_list


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alefa-stt", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the server (settings from ALEFA_STT_* variables, or these options)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--engine", choices=["hailo", "whisper", "fake"])
    serve.add_argument("--model")
    serve.add_argument("--models-dir")
    serve.add_argument("--cpu-threads", type=int)
    serve.add_argument("--device", choices=["auto", "cpu", "cuda"])
    serve.add_argument("--audio-root", help="the audio server's folder, read for deferred transcription")
    serve.add_argument("--audio-folder", action="append", metavar="NAME=PATH",
                       help="one of the audio server's folders by its name (repeatable; an empty PATH: off)")
    serve.add_argument("--speakers", choices=["auto", "fake"], help=argparse.SUPPRESS)
    serve.add_argument("--language", help="the only language offered; empty: each request names one")
    serve.add_argument("--origins", help="page addresses allowed to call, separated by spaces")
    serve.add_argument("--decoder", choices=["ffmpeg", "av"])
    serve.add_argument("--local", action="store_true",
                       help="this computer only: 127.0.0.1, Host checked, loopback pages allowed, PyAV decoder")
    serve.add_argument("--key-stdin", action="store_true",
                       help="read the one accepted key from the first line of stdin (no keys file)")
    fetch = sub.add_parser("download", help="fetch a faster-whisper model; prints 'progress N' lines")
    fetch.add_argument("model")
    fetch.add_argument("--models-dir")
    sub.add_parser("models", help="the models this program can fetch, and which are here")
    sub.add_parser("system", help="what this computer offers: 'cuda N' (NVIDIA GPUs CUDA sees)")
    add = sub.add_parser("add-key", help="create a key; it is printed once")
    add.add_argument("name")
    revoke = sub.add_parser("revoke-key", help="remove a key")
    revoke.add_argument("name")
    sub.add_parser("list-keys", help="names of the keys in the keys file")
    bench = sub.add_parser("transcribe", help="transcribe audio files here, with timings (no server)")
    bench.add_argument("files", nargs="+")
    stream = sub.add_parser("stream", help="stream a file to a running server at real-time pace, with latencies")
    stream.add_argument("url", help="e.g. https://stt.alefa.net or http://127.0.0.1:8710")
    stream.add_argument("file")
    stream.add_argument("--key", default=os.environ.get("ALEFA_STT_KEY"), help="or set ALEFA_STT_KEY")
    stream.add_argument("--speed", type=float, default=1.0, help="2 sends twice as fast as real time")
    stream.add_argument("--connect", metavar="IP", help="connect to this address, keeping the URL's name for TLS (test before DNS)")
    stream.add_argument("--language")
    stream.add_argument("--prompt")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "serve":
        _apply_serve_options(settings, args)
        if args.key_stdin:
            line = sys.stdin.readline().strip()
            if not line:
                print("--key-stdin: no key on stdin", file=sys.stderr)
                return 2
            settings.single_key = line
        return _serve(settings)
    if args.command == "system":
        from .engine import cuda_devices

        print(f"cuda {cuda_devices()}", flush=True)
        return 0
    if args.command in ("download", "models"):
        from .models import MODELS, DownloadError, download, model_path

        models_dir = getattr(args, "models_dir", None) or settings.models_dir
        if args.command == "models":
            for name in MODELS:
                print(f"{name}\t{'here' if model_path(models_dir, name) else 'not downloaded'}")
            return 0
        try:
            folder = download(args.model, models_dir)
        except (DownloadError, OSError) as exc:
            print(f"error {exc}", flush=True)
            return 1
        print(f"done {folder}", flush=True)
        return 0
    if args.command == "add-key":
        key, line = new_key(args.name)
        add_key_line(settings.keys_file, line)
        print(f"Key '{args.name}' added to {settings.keys_file}. It is shown only now:\n\n  {key}\n")
        return 0
    if args.command == "revoke-key":
        if not revoke_key(settings.keys_file, args.name):
            print(f"No key named '{args.name}'.", file=sys.stderr)
            return 1
        print(f"Key '{args.name}' revoked.")
        return 0
    if args.command == "list-keys":
        print("\n".join(KeyStore(settings.keys_file).names()) or "(no keys)")
        return 0
    if args.command == "stream":
        from .client import stream_file

        if not args.key:
            parser.error("give --key or set ALEFA_STT_KEY")
        return asyncio.run(stream_file(args.url, args.key, args.file, settings.ffmpeg, settings.tmp_dir, args.speed,
                                       args.connect, settings.decoder, args.language, args.prompt))
    return asyncio.run(_transcribe(settings, args.files))


def _apply_serve_options(settings: Settings, args) -> None:
    if args.local:
        settings.host = "127.0.0.1"
        settings.loopback_only = True
        settings.loopback_origins = True
        settings.decoder = "av"
    for option in ("host", "port", "engine", "model", "models_dir", "cpu_threads", "device", "language", "decoder",
                   "audio_root", "speakers"):
        value = getattr(args, option)
        if value is not None:
            setattr(settings, option, value)
    if args.origins is not None:
        settings.allowed_origins = split_list(args.origins)
    for entry in args.audio_folder or []:
        name, sep, path = entry.partition("=")
        if not sep or not name:
            raise SystemExit(f"--audio-folder takes NAME=PATH, not {entry!r}")
        settings.audio_folders[name] = path


class NoWebSocketLines(logging.Filter):
    """uvicorn logs each WebSocket's whole address, query included ('?prompt=' carries the
    spelling hints), and every connection opened and closed. Ours logs the path and status."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ('"WebSocket ' in message or message in ("connection open", "connection closed"))


def _serve(settings: Settings) -> int:
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.error").addFilter(NoWebSocketLines())
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=False,  # uvicorn's would log query strings; ours does not
        ws_max_size=1024 * 1024,
        ws_ping_interval=20,
        log_level="info",
        proxy_headers=False,
    )
    return 0


async def _transcribe(settings: Settings, files: list[str]) -> int:
    from .audio import decode_file
    from .engine import load_engine
    from .segmenter import Segment, Segmenter, make_vad

    started = time.monotonic()
    engine = load_engine(settings)
    print(f"engine {engine.name}, whisper-{engine.model}, {engine.window_seconds:.0f} s window, "
          f"loaded in {time.monotonic() - started:.1f} s\n")
    try:
        for path in files:
            with open(path, "rb") as fh:
                audio = await decode_file(settings.ffmpeg, fh.read(), settings.tmp_dir, settings.decoder)
            duration = len(audio) / SAMPLE_RATE
            segmenter = Segmenter(make_vad(settings.vad), engine.window_seconds, settings.endpointing_ms)
            pieces = [e for e in segmenter.feed(audio) + segmenter.finalize() if isinstance(e, Segment)]
            print(f"== {path}: {duration:.1f} s of audio, {len(pieces)} pieces")
            busy = 0.0
            texts = []
            for piece in pieces:
                t0 = time.monotonic()
                text = engine.transcribe(piece.audio)
                took = time.monotonic() - t0
                busy += took
                texts.append(text)
                print(f"  [{piece.start:6.1f}s +{piece.duration:4.1f}s] {took * 1000:5.0f} ms  {text}")
            ratio = busy / duration if duration else 0
            print(f"  -> {busy:.1f} s of inference for {duration:.1f} s of audio (x{ratio:.2f} real time)\n")
            print("  " + " ".join(t for t in texts if t) + "\n")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
