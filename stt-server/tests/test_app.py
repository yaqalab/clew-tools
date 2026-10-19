import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alefa_stt.app import create_app
from alefa_stt.engine import FakeEngine

from conftest import needs_ffmpeg, speechlike, to_pcm16, to_wav

DICTATION = [(0.5, False), (1.2, True), (0.8, False), (2.0, True), (0.8, False)]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeEngine())) as c:
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


def test_ping_needs_no_key(client):
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json()["server"] == "alefa-stt" and r.json()["ready"] is True
    assert r.headers["cache-control"] == "no-store"


def test_me_checks_the_key(client, key):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer stt.tester.wrong"}).status_code == 401
    r = client.get("/api/me", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["key"] == "tester"


def test_cors_allows_only_listed_origins(client):
    ok = client.options("/api/me", headers={"Origin": "https://clew.alefa.net",
                                            "Access-Control-Request-Method": "GET",
                                            "Access-Control-Request-Headers": "authorization"})
    assert ok.headers.get("access-control-allow-origin") == "https://clew.alefa.net"
    bad = client.options("/api/me", headers={"Origin": "https://evil.example",
                                             "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


def test_listen_linear16_with_subprotocol_key(client, key):
    audio = to_pcm16(speechlike(DICTATION))
    url = "/v1/listen?encoding=linear16&sample_rate=16000&channels=1&vad_events=true"
    with client.websocket_connect(url, subprotocols=["alefa-stt", key],
                                  headers={"Origin": "https://clew.alefa.net"}) as ws:
        assert ws.accepted_subprotocol == "alefa-stt"
        for i in range(0, len(audio), 3200):  # 100 ms per message
            ws.send_bytes(audio[i : i + 3200])
        ws.send_text(json.dumps({"type": "KeepAlive"}))
        ws.send_text(json.dumps({"type": "CloseStream"}))
        messages = read_until_closed(ws)
    finals = results(messages)
    assert [r["channel"]["alternatives"][0]["transcript"] for r in finals] == [
        "speech of 1.6 seconds", "speech of 2.4 seconds"]
    assert all(r["is_final"] and r["speech_final"] for r in finals)
    assert sum(m["type"] == "SpeechStarted" for m in messages) == 2
    assert messages[-1]["type"] == "Metadata" and messages[-1]["duration"] == pytest.approx(5.3, abs=0.01)


def test_finalize_returns_the_words_so_far(client, key):
    audio = to_pcm16(speechlike([(0.5, False), (1.5, True)]))
    with client.websocket_connect("/v1/listen?encoding=linear16&sample_rate=16000",
                                  headers={"Authorization": f"Bearer {key}"}) as ws:
        ws.send_bytes(audio)
        ws.send_text(json.dumps({"type": "Finalize"}))
        first = json.loads(ws.receive_text())
        assert first["type"] == "Results" and first["from_finalize"] is True
        ws.send_text(json.dumps({"type": "CloseStream"}))
        rest = read_until_closed(ws)
    assert [m["type"] for m in rest] == ["Metadata"]


def test_listen_refuses_wrong_key_and_foreign_origin(client, key):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/listen", subprotocols=["alefa-stt", "stt.tester.nope"]):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/listen", subprotocols=["alefa-stt", key],
                                      headers={"Origin": "https://evil.example"}):
            pass


def test_listen_refuses_other_languages(client, key):
    with client.websocket_connect("/v1/listen?language=fr", subprotocols=["alefa-stt", key]) as ws:
        assert json.loads(ws.receive_text())["type"] == "Error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


@needs_ffmpeg
def test_listen_resamples_raw_audio_at_another_rate(client, key):
    import numpy as np
    audio16 = speechlike(DICTATION)
    audio48 = np.repeat(audio16, 3)  # crude 48 kHz; the cut points are what matter
    with client.websocket_connect("/v1/listen?encoding=linear16&sample_rate=48000",
                                  subprotocols=["alefa-stt", key]) as ws:
        data = to_pcm16(audio48)
        for i in range(0, len(data), 9600):
            ws.send_bytes(data[i : i + 9600])
        ws.send_text(json.dumps({"type": "CloseStream"}))
        assert len(results(read_until_closed(ws))) == 2


@needs_ffmpeg
def test_listen_webm_opus_as_the_browser_sends_it(client, key, tmp_path):
    wav = tmp_path / "in.wav"
    wav.write_bytes(to_wav(speechlike(DICTATION)))
    webm = tmp_path / "out.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libopus",
                    "-b:a", "24k", "-ar", "48000", "-f", "webm", str(webm)], check=True)
    data = webm.read_bytes()
    with client.websocket_connect("/v1/listen?container=webm", subprotocols=["alefa-stt", key]) as ws:
        for i in range(0, len(data), 750):  # about 250 ms of 24 kbit/s Opus per message
            ws.send_bytes(data[i : i + 750])
        ws.send_text(json.dumps({"type": "CloseStream"}))
        messages = read_until_closed(ws)
    assert len(results(messages)) == 2
    assert messages[-1]["duration"] == pytest.approx(5.3, abs=0.1)


@needs_ffmpeg
def test_file_transcription_openai_style(client, key):
    wav = to_wav(speechlike(DICTATION))
    headers = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/audio/transcriptions", headers=headers,
                    files={"file": ("a.wav", wav, "audio/wav")}, data={"model": "whisper-1"})
    assert r.status_code == 200
    assert r.json() == {"text": "speech of 1.6 seconds speech of 2.4 seconds"}
    r = client.post("/v1/audio/transcriptions", headers=headers,
                    files={"file": ("a.wav", wav, "audio/wav")}, data={"response_format": "verbose_json"})
    assert [s["text"] for s in r.json()["segments"]] == ["speech of 1.6 seconds", "speech of 2.4 seconds"]
    r = client.post("/v1/audio/transcriptions", headers=headers,
                    files={"file": ("a.wav", wav, "audio/wav")}, data={"language": "fr"})
    assert r.status_code == 400 and "English" in r.json()["error"]["message"]
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")})
    assert r.status_code == 401


@needs_ffmpeg
def test_file_that_is_not_audio(client, key):
    r = client.post("/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("a.wav", b"not audio at all", "audio/wav")})
    assert r.status_code == 400
