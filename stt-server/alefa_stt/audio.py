"""Turning what clients send into 16 kHz mono float32.

Raw 16 kHz mono linear16 is converted in Python. Everything else (other rates,
mu-law/a-law, or a container such as WebM/Opus from the browser's MediaRecorder)
goes through one ffmpeg process per session, fed as the bytes arrive.

With ``decoder="av"`` (the Windows build, clew docs/28) the same work is done in-process by
PyAV, FFmpeg's libraries as a Python package, which faster-whisper already needs: no
ffmpeg program to install. One thread per session reads what has arrived so far.
"""

from __future__ import annotations

import asyncio
import io
import os
import queue
import tempfile
import threading
import time
from typing import Awaitable, Callable

import numpy as np

from . import SAMPLE_RATE

OnAudio = Callable[[np.ndarray], Awaitable[None]]

RAW_ENCODINGS = {"linear16": "s16le", "mulaw": "mulaw", "alaw": "alaw"}
CONTAINERS = {"webm": "matroska", "matroska": "matroska", "ogg": "ogg", "mp4": "mp4", "wav": "wav"}
_OUTPUT = ["-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-flush_packets", "1", "pipe:1"]


class FormatError(ValueError):
    pass


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def ffmpeg_input_args(encoding: str | None, sample_rate: int | None, channels: int, container: str | None) -> list[str]:
    if encoding:
        fmt = RAW_ENCODINGS.get(encoding)
        if fmt is None:
            raise FormatError(f"encoding {encoding!r} is not supported; use one of {sorted(RAW_ENCODINGS)} or none for a container")
        if not sample_rate:
            raise FormatError(f"encoding={encoding} needs sample_rate")
        return ["-f", fmt, "-ar", str(sample_rate), "-ac", str(channels), "-i", "pipe:0"]
    if container:
        demuxer = CONTAINERS.get(container)
        if demuxer is None:
            raise FormatError(f"container {container!r} is not supported; use one of {sorted(CONTAINERS)}")
        return ["-f", demuxer, "-i", "pipe:0"]
    return ["-i", "pipe:0"]


class DirectPcm:
    """16 kHz mono linear16: no process needed."""

    def __init__(self, on_audio: OnAudio):
        self._on_audio = on_audio
        self._carry = b""
        self.bytes_in = 0

    async def write(self, data: bytes) -> None:
        self.bytes_in += len(data)
        data = self._carry + data
        whole = len(data) // 2 * 2
        self._carry = data[whole:]
        if whole:
            await self._on_audio(pcm16_to_float(data[:whole]))

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def abort(self) -> None:
        pass


class FfmpegStream:
    def __init__(self, ffmpeg: str, input_args: list[str], on_audio: OnAudio):
        self._cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-fflags", "+nobuffer", "-probesize", "4096", "-analyzeduration", "0",
                     *input_args, *_OUTPUT]
        self._on_audio = on_audio
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._last_output = time.monotonic()
        self.bytes_in = 0
        self.error = b""

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._read_stderr())

    async def _read(self) -> None:
        carry = b""
        assert self._proc and self._proc.stdout
        while True:
            chunk = await self._proc.stdout.read(8192)
            if not chunk:
                break
            self._last_output = time.monotonic()
            data = carry + chunk
            whole = len(data) // 2 * 2
            carry = data[whole:]
            if whole:
                await self._on_audio(pcm16_to_float(data[:whole]))

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            chunk = await self._proc.stderr.read(1024)
            if not chunk:
                break
            self.error = (self.error + chunk)[-2048:]

    async def write(self, data: bytes) -> None:
        assert self._proc and self._proc.stdin
        self.bytes_in += len(data)
        if self._reader and self._reader.done():
            raise FormatError(self.describe_error())
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def flush(self, quiet: float = 0.15, limit: float = 1.0) -> None:
        """Wait until ffmpeg has caught up: no new output for `quiet` seconds."""
        started = time.monotonic()
        while time.monotonic() - started < limit:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - self._last_output >= quiet and now - started >= quiet:
                return

    async def close(self) -> None:
        """End of input: let ffmpeg drain everything it holds."""
        if not self._proc:
            return
        if self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._wait(), timeout=10)
        except asyncio.TimeoutError:
            await self.abort()

    async def _wait(self) -> None:
        if self._reader:
            await self._reader
        if self._stderr:
            await self._stderr
        await self._proc.wait()

    async def abort(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        for task in (self._reader, self._stderr):
            if task and not task.done():
                task.cancel()

    def describe_error(self) -> str:
        text = self.error.decode(errors="replace").strip().splitlines()
        return text[-1] if text else "the audio could not be decoded"


class _Arrived(io.RawIOBase):
    """A file the demuxer reads from: blocks until bytes arrive, empty at the end."""

    def __init__(self):
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._buffer = b""
        self._ended = False

    def readable(self) -> bool:
        return True

    def put(self, data: bytes | None) -> None:
        self._chunks.put(data)

    def read(self, size: int = -1) -> bytes:
        while not self._buffer and not self._ended:
            chunk = self._chunks.get()
            if chunk is None:
                self._ended = True
            else:
                self._buffer = chunk
        if size < 0:
            size = len(self._buffer)
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def readinto(self, target) -> int:
        data = self.read(len(target))
        target[: len(data)] = data
        return len(data)


def av_input(encoding: str | None, sample_rate: int | None, channels: int,
             container: str | None) -> tuple[str | None, dict[str, str]]:
    """PyAV's (format, options) for what the client said it sends; the checks of
    ``ffmpeg_input_args``."""
    args = ffmpeg_input_args(encoding, sample_rate, channels, container)
    if encoding:
        return RAW_ENCODINGS[encoding], {"sample_rate": str(sample_rate), "ch_layout": "mono" if channels == 1
                                         else f"{channels}c"}
    return (args[1] if container else None), {}


class AvStream:
    """The ffmpeg program's job (``FfmpegStream``), done by PyAV in a thread."""

    def __init__(self, fmt: str | None, options: dict[str, str], on_audio: OnAudio):
        self._fmt = fmt
        self._options = {"probesize": "4096", "analyzeduration": "0", "fflags": "nobuffer", **options}
        self._on_audio = on_audio
        self._input = _Arrived()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._done = asyncio.Event()
        self._last_output = time.monotonic()
        self._stop = False
        self.bytes_in = 0
        self.error = ""

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._decode, name="av-decode", daemon=True)
        self._thread.start()

    def _decode(self) -> None:
        import av

        container = None
        try:
            container = av.open(self._input, mode="r", format=self._fmt, options=self._options)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                if self._stop:
                    return
                for out in resampler.resample(frame):
                    self._deliver(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                self._deliver(out.to_ndarray().reshape(-1))
        except Exception as exc:  # av.FFmpegError and friends: the client's bytes
            if not self._stop:
                self.error = str(exc) or type(exc).__name__
        finally:
            if container is not None:
                container.close()
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._done.set)

    def _deliver(self, pcm: np.ndarray) -> None:
        if not len(pcm) or self._stop:
            return
        self._last_output = time.monotonic()
        assert self._loop is not None
        samples = pcm.astype(np.float32) / 32768.0
        asyncio.run_coroutine_threadsafe(self._on_audio(samples), self._loop).result()

    async def write(self, data: bytes) -> None:
        self.bytes_in += len(data)
        if self._done.is_set():
            raise FormatError(self.describe_error())
        self._input.put(bytes(data))

    async def flush(self, quiet: float = 0.15, limit: float = 1.0) -> None:
        started = time.monotonic()
        while time.monotonic() - started < limit:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - self._last_output >= quiet and now - started >= quiet:
                return

    async def close(self) -> None:
        self._input.put(None)
        try:
            await asyncio.wait_for(self._done.wait(), timeout=10)
        except asyncio.TimeoutError:
            await self.abort()

    async def abort(self) -> None:
        self._stop = True
        self._input.put(None)

    def describe_error(self) -> str:
        return self.error or "the audio could not be decoded"


async def open_stream(ffmpeg: str, encoding: str | None, sample_rate: int | None,
                      channels: int, container: str | None, on_audio: OnAudio, decoder: str = "ffmpeg"):
    if encoding == "linear16" and sample_rate == SAMPLE_RATE and channels == 1:
        return DirectPcm(on_audio)
    if decoder == "av":
        fmt, options = av_input(encoding, sample_rate, channels, container)
        stream = AvStream(fmt, options, on_audio)
    else:
        stream = FfmpegStream(ffmpeg, ffmpeg_input_args(encoding, sample_rate, channels, container), on_audio)
    await stream.start()
    return stream


def _decode_bytes_av(data: bytes) -> np.ndarray:
    import av

    parts = []
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise FormatError("there is no audio in the file")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
            parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    except FormatError:
        raise
    except Exception as exc:
        raise FormatError(str(exc) or "the audio could not be decoded") from exc
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


async def decode_file(ffmpeg: str, data: bytes, tmp_dir: str, decoder: str = "ffmpeg") -> np.ndarray:
    """Whole upload to 16 kHz mono. Via a temporary file (on tmpfs under systemd),
    because some containers (mp4 with its index at the end) cannot be read from a pipe.
    PyAV reads from memory, which can seek, so it needs no file."""
    if decoder == "av":
        return await asyncio.to_thread(_decode_bytes_av, data)
    fd, path = tempfile.mkstemp(prefix="upload-", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
            "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            lines = err.decode(errors="replace").strip().splitlines()
            raise FormatError(lines[-1] if lines else "the audio could not be decoded")
        return pcm16_to_float(out[: len(out) // 2 * 2])
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
