"""The local (Windows) server of clew docs/28: one key, loopback only, PyAV, languages, prompt."""

import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alefa_stt import SAMPLE_RATE, models
from alefa_stt.__main__ import _apply_serve_options
from alefa_stt.app import create_app
from alefa_stt.config import Settings
from alefa_stt.engine import (FakeEngine, Serial, continued, echoes_prompt, normalize_language, split_prompt,
                              whisper_prompt)
from alefa_stt.session import PROMPT_LIMIT

from conftest import speechlike, to_pcm16, to_wav

av = pytest.importorskip("av")

DICTATION = [(0.5, False), (1.2, True), (0.8, False), (2.0, True), (0.8, False)]
KEY = "stt.local.Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4"
LOCAL = "http://127.0.0.1"
WS = "ws://127.0.0.1"  # the test client sends Host: testserver for a relative WebSocket URL


@pytest.fixture
def local_settings(tmp_path):
    return Settings(engine="fake", single_key=KEY, tmp_dir=str(tmp_path), host="127.0.0.1",
                    allowed_origins=["https://clew.alefa.net"], loopback_origins=True,
                    loopback_only=True, decoder="av", idle_seconds=5)


@pytest.fixture
def engine():
    return FakeEngine(languages=("en", "ja", "fr"))


@pytest.fixture
def client(local_settings, engine):
    with TestClient(create_app(local_settings, engine), base_url=LOCAL) as c:
        yield c


def read_until_closed(ws):
    messages = []
    while True:
        try:
            messages.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            return messages


def results(messages):
    return [m for m in messages if m["type"] == "Results"]


def webm_opus(audio: np.ndarray) -> bytes:
    """What a browser's MediaRecorder sends: Opus at 48 kHz in WebM, made with PyAV."""
    out = io.BytesIO()
    with av.open(out, mode="w", format="webm") as container:
        stream = container.add_stream("libopus", rate=48000)
        stream.bit_rate = 24000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        frame = av.AudioFrame.from_ndarray(to_pcm16_array(audio), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        for piece in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(piece):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


def to_pcm16_array(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").reshape(1, -1)


# --- one key, held in memory ---

def test_the_one_key_from_the_tray(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer stt.local.wrong"}).status_code == 401
    r = client.get("/api/me", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    assert r.json()["key"] == "local" and r.json()["languages"] == ["en", "ja", "fr"]


def test_a_key_that_is_not_one_is_refused_at_start(local_settings):
    local_settings.single_key = "hunter2"
    with pytest.raises(ValueError):
        create_app(local_settings, FakeEngine())


# --- this computer only ---

def test_another_host_name_is_refused(local_settings, engine):
    with TestClient(create_app(local_settings, engine), base_url="http://rebound.example") as c:
        r = c.get("/api/ping")
        assert r.status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("ws://rebound.example/v1/listen", subprotocols=["alefa-stt", KEY]):
                pass
    with TestClient(create_app(local_settings, engine), base_url="http://localhost:47822") as c:
        assert c.get("/api/ping").status_code == 200


def test_pages_on_this_computer_and_listed_ones_may_call(client):
    for origin in ("http://localhost:5173", "http://127.0.0.1:4173", "https://clew.alefa.net"):
        ok = client.options("/api/me", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                                "Access-Control-Request-Headers": "authorization"})
        assert ok.headers.get("access-control-allow-origin") == origin
    for origin in ("https://evil.example", "http://localhost.evil.example", "http://127.0.0.1.evil.example"):
        bad = client.options("/api/me", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS + "/v1/listen", subprotocols=["alefa-stt", KEY], headers={"Origin": origin}):
                pass


# --- PyAV instead of the ffmpeg program ---

def test_webm_opus_as_the_browser_sends_it(client):
    data = webm_opus(speechlike(DICTATION))
    with client.websocket_connect(WS + "/v1/listen?container=webm", subprotocols=["alefa-stt", KEY],
                                  headers={"Origin": "http://localhost:5173"}) as ws:
        for i in range(0, len(data), 750):
            ws.send_bytes(data[i : i + 750])
        ws.send_text(json.dumps({"type": "CloseStream"}))
        messages = read_until_closed(ws)
    assert len(results(messages)) == 2
    assert messages[-1]["duration"] == pytest.approx(5.3, abs=0.1)


def test_finalize_with_webm_returns_the_words_so_far(client):
    data = webm_opus(speechlike([(0.5, False), (1.5, True), (0.3, False)]))
    with client.websocket_connect(WS + "/v1/listen?container=webm", subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_bytes(data)
        ws.send_text(json.dumps({"type": "Finalize"}))
        first = json.loads(ws.receive_text())
        assert first["type"] == "Results"
        ws.send_text(json.dumps({"type": "CloseStream"}))
        read_until_closed(ws)


def test_raw_audio_at_another_rate(client):
    audio48 = np.repeat(speechlike(DICTATION), 3)
    with client.websocket_connect(WS + "/v1/listen?encoding=linear16&sample_rate=48000",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        data = to_pcm16(audio48)
        for i in range(0, len(data), 9600):
            ws.send_bytes(data[i : i + 9600])
        ws.send_text(json.dumps({"type": "CloseStream"}))
        assert len(results(read_until_closed(ws))) == 2


def test_bytes_that_are_not_webm_close_the_session(client):
    with client.websocket_connect(WS + "/v1/listen?container=webm", subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_bytes(b"\x00not a webm at all" * 400)
        ws.send_text(json.dumps({"type": "CloseStream"}))
        messages = read_until_closed(ws)
    assert not results(messages)


def test_file_transcription_without_ffmpeg(client):
    headers = {"Authorization": f"Bearer {KEY}"}
    for name, data in (("a.wav", to_wav(speechlike(DICTATION))), ("a.webm", webm_opus(speechlike(DICTATION)))):
        r = client.post("/v1/audio/transcriptions", headers=headers, files={"file": (name, data, "audio/x")},
                        data={"response_format": "verbose_json"})
        assert r.status_code == 200, r.text
        assert [s["end"] - s["start"] for s in r.json()["segments"]] == [
            pytest.approx(1.6, abs=0.1), pytest.approx(2.4, abs=0.1)]
    r = client.post("/v1/audio/transcriptions", headers=headers,
                    files={"file": ("a.wav", b"not audio at all", "audio/wav")})
    assert r.status_code == 400


# --- languages and spelling hints ---

def test_language_and_prompt_reach_the_engine(client, engine):
    audio = to_pcm16(speechlike([(0.3, False), (1.5, True), (0.8, False)]))
    with client.websocket_connect(WS + "/v1/listen?encoding=linear16&sample_rate=16000&language=ja-JP"
                                  "&prompt=Ze-Notes%2C%20yaqatw%2C%20%E5%8D%94%E5%83%8D",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_bytes(audio)
        ws.send_text(json.dumps({"type": "CloseStream"}))
        assert len(results(read_until_closed(ws))) == 1
    assert engine.calls[-1]["language"] == "ja"
    assert engine.calls[-1]["prompt"] == "Ze-Notes, yaqatw, \u5354\u50cd"


def test_no_language_means_english(client, engine):
    r = client.post("/v1/audio/transcriptions", headers={"Authorization": f"Bearer {KEY}"},
                    files={"file": ("a.wav", to_wav(speechlike(DICTATION)), "audio/wav")},
                    data={"response_format": "verbose_json", "prompt": "x" * 1000})
    assert r.json()["language"] == "english"
    assert engine.calls[-1]["language"] == "en"
    assert len(engine.calls[-1]["prompt"]) == PROMPT_LIMIT


def test_a_language_the_model_lacks_is_refused(client):
    with client.websocket_connect(WS + "/v1/listen?language=de", subprotocols=["alefa-stt", KEY]) as ws:
        error = json.loads(ws.receive_text())
        assert error["type"] == "Error" and "en, ja, fr" in error["description"]
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


def test_the_tray_can_fix_one_language(local_settings, engine):
    local_settings.language = "Japanese"
    with TestClient(create_app(local_settings, engine), base_url=LOCAL) as c:
        assert c.get("/api/me", headers={"Authorization": f"Bearer {KEY}"}).json()["languages"] == ["ja"]
        audio = to_pcm16(speechlike([(0.3, False), (1.5, True), (0.8, False)]))
        with c.websocket_connect(WS + "/v1/listen?encoding=linear16&sample_rate=16000",
                                 subprotocols=["alefa-stt", KEY]) as ws:
            ws.send_bytes(audio)
            ws.send_text(json.dumps({"type": "CloseStream"}))
            read_until_closed(ws)
        assert engine.calls[-1]["language"] == "ja"
        with c.websocket_connect(WS + "/v1/listen?language=en", subprotocols=["alefa-stt", KEY]) as ws:
            assert json.loads(ws.receive_text())["type"] == "Error"


def test_language_names():
    assert [normalize_language(v) for v in ("en-US", "English", "ja", "ja_JP", "fr-FR", "Malagasy", "", None)] == [
        "en", "en", "ja", "ja", "fr", "mg", "", ""]
    assert continued("\u305d\u3057\u3066\u3002") == "\u305d\u3057\u3066"


def test_hints_are_ended_like_a_sentence():
    assert whisper_prompt("open coding, Ze-Notes", "en") == "open coding, Ze-Notes."
    assert whisper_prompt("Ze-Notes!", "en") == "Ze-Notes!"
    assert whisper_prompt("\u5354\u50cd\u5b66\u7fd2, \u7b2c\u4e8c\u8a00\u8a9e", "ja") == (
        "\u5354\u50cd\u5b66\u7fd2\u3001\u7b2c\u4e8c\u8a00\u8a9e\u3002")
    assert whisper_prompt("  ", "en") is None and whisper_prompt(None, "ja") is None


SENTENCE = "Manao ahoana tompoko. Azonao lazaina amiko ve? Eny, misaotra betsaka."


def test_the_prompt_is_a_sentence_then_hints():
    assert split_prompt(f"{SENTENCE} Riana, Rojo, Anjara") == (SENTENCE, ["riana", "rojo", "anjara"])
    assert split_prompt("Riana, Rojo, Anjara") == ("", ["riana", "rojo", "anjara"])
    assert split_prompt(SENTENCE) == (SENTENCE, [])
    assert split_prompt(None) == ("", [])


def test_hints_written_back_instead_of_the_speech_are_an_echo():
    prompt = f"{SENTENCE} Riana, Rojo, Anjara"
    # What the user saw (clew docs/29 §13): names, alone, with a filler, or after other words.
    assert echoes_prompt("Riana, Anjara, Anjara. Eeeeeee", prompt)
    assert echoes_prompt("Riana, Rojo.", prompt)
    assert echoes_prompt("Riana, Rojo. Ehm...", prompt)
    assert echoes_prompt("Tzna nukar dem, sa vi jo eno fra innen de. Riana, Rojo.", prompt)
    assert echoes_prompt("Anjara.", "Riana, Rojo, Anjara")
    # A name said in a sentence is the speech; the sentence's own words are not hints.
    assert not echoes_prompt("Niresaka tamin'i Rojo aho omaly momba ny asa.", prompt)
    assert not echoes_prompt("Eny.", prompt)
    assert not echoes_prompt("Misaotra betsaka.", prompt)
    assert not echoes_prompt("Riana, Rojo.", SENTENCE)
    assert not echoes_prompt("Riana, Rojo.", None)
    assert not echoes_prompt("", prompt)


class _Echoing:
    """Writes its prompt's hints back when it has them, as Whisper did on Malagasy."""

    model = "echo"
    window_seconds = 30.0

    def __init__(self):
        self.prompts = []

    def transcribe(self, audio, language=None, prompt=None):
        self.prompts.append(prompt)
        hints = split_prompt(prompt)[1]
        return ", ".join(h.capitalize() for h in hints) + "." if hints else "Tsy fantatro."


def test_an_echo_is_transcribed_again_with_only_the_sentence():
    import asyncio

    engine = _Echoing()
    serial = Serial(engine)
    text = asyncio.run(serial.transcribe(np.zeros(16000, dtype=np.float32), "mg", f"{SENTENCE} Riana, Rojo"))
    assert text == "Tsy fantatro."
    assert engine.prompts == [f"{SENTENCE} Riana, Rojo", SENTENCE]
    assert serial.echoes == 1 and serial.calls == 1
    # Hints alone: the second try has no prompt at all.
    engine.prompts.clear()
    assert asyncio.run(serial.transcribe(np.zeros(16000, dtype=np.float32), "mg", "Riana, Rojo")) == "Tsy fantatro."
    assert engine.prompts == ["Riana, Rojo", None]


def test_websocket_addresses_are_not_logged():
    import logging

    from alefa_stt.__main__ import NoWebSocketLines

    def record(msg, *args):
        return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)

    f = NoWebSocketLines()
    assert not f.filter(record('%s - "WebSocket %s" [accepted]', "127.0.0.1:5", "/v1/listen?prompt=secret"))
    assert not f.filter(record("connection open"))
    assert f.filter(record("Uvicorn running on %s", "http://127.0.0.1:47822"))


# --- the command line the tray uses ---

def test_serve_local_options():
    settings = Settings()
    args = SimpleNamespace(local=True, host=None, port=47822, engine="whisper", model="small",
                           models_dir="D:/models", cpu_threads=None, device="cpu", language="", decoder=None,
                           audio_root=None, audio_folder=["study=D:/study", "pilot="], speakers=None,
                           origins="https://clew.alefa.net https://clew.example.org/")
    _apply_serve_options(settings, args)
    assert settings.audio_folders == {"study": "D:/study", "pilot": ""}
    assert settings.host == "127.0.0.1" and settings.loopback_only and settings.loopback_origins
    assert settings.decoder == "av" and settings.port == 47822 and settings.models_dir == "D:/models"
    assert settings.allowed_origins == ["https://clew.alefa.net", "https://clew.example.org"]
    assert settings.device == "cpu"


def test_system_says_how_many_gpus(capsys):
    from alefa_stt.__main__ import main

    assert main(["system"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("cuda ") and out.split()[1].isdigit()


def test_me_says_where_it_runs(client):
    me = client.get("/api/me", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert me["device"] == "cpu"


# --- models on disk ---

class FakeHub:
    def __init__(self, files):
        self.files = files
        self.fetched = []

    def __call__(self, url):
        self.fetched.append(url)
        if "/api/models/" in url:
            body = json.dumps({"siblings": [{"rfilename": n, "size": len(d)} for n, d in self.files.items()]
                              + [{"rfilename": "README.md", "size": 9}]}).encode()
        else:
            body = self.files[url.rsplit("/", 1)[1]]
        return _Response(body)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_download_writes_a_whole_folder_and_says_how_far(tmp_path, monkeypatch):
    files = {"config.json": b"{}", "model.bin": b"m" * (3 << 20), "tokenizer.json": b"{}",
             "vocabulary.txt": b"a\nb"}
    hub = FakeHub(files)
    monkeypatch.setattr(models, "_open", hub)
    lines = []
    assert models.model_path(str(tmp_path), "small") is None
    folder = models.download("small", str(tmp_path), lines.append)
    assert folder == tmp_path / "small" and models.model_path(str(tmp_path), "small") == folder
    assert not (tmp_path / "small.part").exists()
    assert (folder / "model.bin").read_bytes() == files["model.bin"]
    assert lines[0] == f"total {sum(len(d) for d in files.values())}" and lines[-1] == "progress 100"
    percents = [int(l.split()[1]) for l in lines[1:]]
    assert percents == sorted(percents) and len(set(percents)) == len(percents)
    assert not any("README" in u for u in hub.fetched)
    # Already here: nothing fetched.
    hub.fetched.clear()
    models.download("small", str(tmp_path), lines.append)
    assert hub.fetched == []


def test_download_resumes_and_refuses_unknown_models(tmp_path, monkeypatch):
    files = {"config.json": b"{}", "model.bin": b"m" * 1000, "tokenizer.json": b"{}"}
    hub = FakeHub(files)
    monkeypatch.setattr(models, "_open", hub)
    part = tmp_path / "large-v3-turbo.part"
    part.mkdir()
    (part / "model.bin").write_bytes(files["model.bin"])
    models.download("large-v3-turbo", str(tmp_path), lambda _l: None)
    assert not any(u.endswith("/model.bin") for u in hub.fetched)
    assert "mobiuslabsgmbh/faster-whisper-large-v3-turbo" in hub.fetched[0]
    with pytest.raises(models.DownloadError):
        models.download("enormous", str(tmp_path), lambda _l: None)
