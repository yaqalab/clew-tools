"""Live transcription (clew docs/29): turns with voices, from an upload or the audio folder."""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alefa_stt.app import create_app
from alefa_stt.config import Settings
from alefa_stt.engine import FakeEngine
from alefa_stt.speakers import SAME_VOICE, Voices
from alefa_stt.transcribe import PathError, resolve_in_root, resolve_served

from conftest import speechlike, to_wav

pytest.importorskip("av")

KEY = "stt.local.Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4"
WS = "ws://127.0.0.1"


def interview(seed=0):
    """Two voices taking turns: loud (about 0.1 RMS) and soft (about 0.05), the fake speaker
    model's two voices, with pauses between."""
    rng = np.random.default_rng(seed)
    parts = []
    for seconds, level in [(0.5, 0.0), (2.0, 0.1), (0.9, 0.0), (2.5, 0.05), (0.9, 0.0), (1.8, 0.1), (0.9, 0.0)]:
        n = int(seconds * 16000)
        parts.append(rng.normal(0, level or 0.001, n))
    return np.clip(np.concatenate(parts), -1, 1).astype(np.float32)


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "audio"
    (root / "2026").mkdir(parents=True)
    (root / "2026" / "interview.wav").write_bytes(to_wav(interview()))
    (root / "notes.txt").write_text("not audio")
    (tmp_path / "outside.wav").write_bytes(to_wav(interview()))
    return root


@pytest.fixture
def client(tmp_path, folder):
    settings = Settings(engine="fake", single_key=KEY, tmp_dir=str(tmp_path), host="127.0.0.1",
                        loopback_origins=True, loopback_only=True, decoder="av", idle_seconds=5,
                        audio_root=str(folder), speakers="fake")
    with TestClient(create_app(settings, FakeEngine(languages=("en", "ja"))), base_url="http://127.0.0.1") as c:
        yield c


def read_all(ws):
    out = []
    while True:
        try:
            out.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            return out


def of(messages, kind):
    return [m for m in messages if m["type"] == kind]


def test_an_upload_comes_back_as_turns_with_two_voices(client):
    data = to_wav(interview())
    with client.websocket_connect(WS + "/v1/transcribe?source=upload", subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start", "voices": []}))
        for i in range(0, len(data), 64 * 1024):
            ws.send_bytes(data[i : i + 64 * 1024])
        ws.send_text(json.dumps({"type": "EndOfFile"}))
        messages = read_all(ws)
    turns = of(messages, "Turn")
    assert [t["voice"] for t in turns] == ["v1", "v2", "v1"]
    # A turn starts a little before its first sound (the segmenter keeps a moment of lead-in).
    assert 0.2 <= turns[0]["start"] <= 0.5 and 3.1 <= turns[1]["start"] <= 3.4
    assert [v["id"] for v in of(messages, "Voice")] == ["v1", "v2"]
    kept = of(messages, "Voices")[0]["voices"]
    assert [v["id"] for v in kept] == ["v1", "v2"] and kept[0]["count"] == 2
    assert messages[-1]["type"] == "Done" and messages[-1]["duration"] == pytest.approx(9.5, abs=0.1)


def test_progress_knows_the_whole_duration_from_the_first_message(client, monkeypatch):
    # Whole-file transcription draws a bar from it; the duration used to arrive only once the
    # decoder had read to the end, so every Progress said 0.
    import alefa_stt.transcribe as transcribe

    monkeypatch.setattr(transcribe, "PROGRESS_EVERY", 0.0)
    with client.websocket_connect(WS + "/v1/transcribe?source=folder&path=2026/interview.wav",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        messages = read_all(ws)
    progress = of(messages, "Progress")
    assert progress and progress[0]["duration"] == pytest.approx(9.5, abs=0.1)


def test_voices_kept_from_an_earlier_session_keep_their_ids(client):
    earlier = [{"id": "v7", "vector": [0.0, 1.0], "count": 3}]
    with client.websocket_connect(WS + "/v1/transcribe?source=folder&path=2026/interview.wav",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start", "voices": earlier}))
        messages = read_all(ws)
    turns = of(messages, "Turn")
    # The soft voice is the one this transcript knew as v7; the loud one is new.
    assert [t["voice"] for t in turns] == ["v1", "v7", "v1"]


def test_the_folder_source_reads_from_a_point_in_the_recording(client):
    with client.websocket_connect(WS + "/v1/transcribe?source=folder&path=2026%2Finterview.wav&start=3",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        messages = read_all(ws)
    turns = of(messages, "Turn")
    assert len(turns) == 2 and turns[0]["start"] >= 3.0


def test_a_link_to_the_audio_server_is_read_by_ranges(client, folder):
    import http.server
    import threading

    data = (folder / "2026" / "interview.wav").read_bytes()

    class Audio(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            first, last = 0, len(data) - 1
            r = self.headers.get("Range")
            if r:
                a, b = r.split("=")[1].split("-")
                first, last = int(a), int(b) if b else len(data) - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(data)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(last - first + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data[first : last + 1])

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Audio)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    link = f"http://127.0.0.1:{server.server_address[1]}/api/stream?p=x&sig=y"
    try:
        from urllib.parse import quote

        with client.websocket_connect(WS + f"/v1/transcribe?source=url&url={quote(link, safe='')}",
                                      subprotocols=["alefa-stt", KEY]) as ws:
            ws.send_text(json.dumps({"type": "Start"}))
            messages = read_all(ws)
    finally:
        server.shutdown()
        server.server_close()
    assert [t["voice"] for t in of(messages, "Turn")] == ["v1", "v2", "v1"]
    # Only links to this computer.
    with client.websocket_connect(WS + "/v1/transcribe?source=url&url=http%3A%2F%2Fexample.org%2Fa.mp3",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        assert read_all(ws)[0]["type"] == "Error"


def test_only_audio_inside_the_folder(client, folder):
    for path in ("../outside.wav", "notes.txt", "/etc/passwd", "C:/Windows/win.ini", "2026/../../outside.wav",
                 ".hidden.wav", "2026/missing.wav"):
        with client.websocket_connect(WS + f"/v1/transcribe?source=folder&path={path}",
                                      subprotocols=["alefa-stt", KEY]) as ws:
            ws.send_text(json.dumps({"type": "Start"}))
            messages = read_all(ws)
        assert messages[0]["type"] == "Error", path
        assert not of(messages, "Turn")


def test_merging_two_voices(tmp_path, folder):
    settings = Settings(engine="fake", single_key=KEY, tmp_dir=str(tmp_path), decoder="av", idle_seconds=5,
                        audio_root=str(folder), speakers="fake")
    slow = FakeEngine(delay=0.3)  # so the merge arrives while the recording is still being worked on
    data = to_wav(interview())
    with TestClient(create_app(settings, slow), base_url="http://127.0.0.1") as client, client.websocket_connect(WS + "/v1/transcribe?source=upload", subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        ws.send_bytes(data)
        ws.send_text(json.dumps({"type": "EndOfFile"}))
        first = [json.loads(ws.receive_text()) for _ in range(3)]  # Voice v1, Turn, Voice v2
        ws.send_text(json.dumps({"type": "Merge", "from": "v2", "into": "v1"}))
        rest = read_all(ws)
    assert [m["type"] for m in first] == ["Voice", "Turn", "Voice"]
    kept = of(rest, "Voices")[-1]["voices"]
    assert [v["id"] for v in kept] == ["v1"]


def test_a_key_is_needed_and_a_bad_upload_is_said(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS + "/v1/transcribe", subprotocols=["alefa-stt", "stt.local.wrong"]):
            pass
    with client.websocket_connect(WS + "/v1/transcribe?source=upload", subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        ws.send_bytes(b"not audio at all" * 100)
        ws.send_text(json.dumps({"type": "EndOfFile"}))
        messages = read_all(ws)
    assert of(messages, "Error") and not of(messages, "Turn")


def test_auto_decides_the_recordings_language_once(tmp_path, folder):
    settings = Settings(engine="fake", single_key=KEY, tmp_dir=str(tmp_path), decoder="av", idle_seconds=5,
                        audio_root=str(folder), speakers="fake")
    engine = FakeEngine(languages=("en", "ja"))
    with TestClient(create_app(settings, engine), base_url="http://127.0.0.1") as c:
        with c.websocket_connect(WS + "/v1/transcribe?source=folder&path=2026/interview.wav&language=auto",
                                 subprotocols=["alefa-stt", KEY]) as ws:
            ws.send_text(json.dumps({"type": "Start"}))
            messages = read_all(ws)
    assert of(messages, "Language") == [{"type": "Language", "language": "ja", "probability": 0.9}]
    # Decided from all the speech there was (under 20 s here), then every piece in that language.
    assert engine.detected_seconds == pytest.approx(7.6, abs=0.5)
    assert {call["language"] for call in engine.calls} == {"ja"}
    assert len(of(messages, "Turn")) == 3


def test_the_language_can_be_told_per_piece(client, folder):
    with client.websocket_connect(WS + "/v1/transcribe?source=folder&path=2026/interview.wav&language=auto",
                                  subprotocols=["alefa-stt", KEY]) as ws:
        ws.send_text(json.dumps({"type": "Start"}))
        assert of(read_all(ws), "Turn")
    # Live dictation keeps one language: "auto" is for recordings.
    with client.websocket_connect(WS + "/v1/listen?language=auto", subprotocols=["alefa-stt", KEY]) as ws:
        assert json.loads(ws.receive_text())["type"] == "Error"


def test_me_says_it_can_transcribe(client):
    me = client.get("/api/me", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert me["transcribe"] is True and me["speakers"] is True and me["folder"] is True


def test_voices_match_by_similarity_and_short_pieces_follow():
    v = Voices()
    a, new = v.assign(np.array([1.0, 0.0]), 3.0)
    assert new and a.id == "v1"
    b, new = v.assign(np.array([0.0, 1.0]), 3.0)
    assert new and b.id == "v2"
    # Close to v1, long: v1, and its vector moves a little.
    c, new = v.assign(np.array([0.9, 0.1]), 3.0)
    assert c is a and not new and a.count == 2
    # Short and unclear: the voice before it.
    d, new = v.assign(np.array([0.7, 0.7]), 0.8)
    assert d is a and not new
    assert 0.5 < SAME_VOICE < 0.65


def test_paths_are_checked_like_the_audio_servers(folder):
    assert resolve_in_root(str(folder), "2026/interview.wav").name == "interview.wav"
    assert resolve_in_root(str(folder), "2026\\interview.wav").name == "interview.wav"
    for bad in ("", "..", "../x.wav", "2026/./interview.wav", "a:b.wav", "2026/interview.wav "):
        with pytest.raises(PathError):
            resolve_in_root(str(folder), bad)
    with pytest.raises(PathError):
        resolve_in_root("", "2026/interview.wav")


def test_several_folders_follow_the_audio_servers_rules(folder, tmp_path):
    other = tmp_path / "pilot"
    (other / "2026").mkdir(parents=True)
    (other / "2026" / "interview.wav").write_bytes(b"RIFF")
    (other / "only-pilot.wav").write_bytes(b"RIFF")
    off = tmp_path / "old"
    off.mkdir()
    (off / "only-old.wav").write_bytes(b"RIFF")
    folders = {"audio": str(folder), "pilot": str(other), "old": ""}
    assert resolve_served("", folders, "audio/2026/interview.wav").parent.parent == folder.resolve()
    assert resolve_served("", folders, "pilot/2026/interview.wav").parent.parent == other.resolve()
    assert resolve_served("", folders, "only-pilot.wav").parent == other.resolve()  # saved before names
    for bad in ("2026/interview.wav",  # in both: ambiguous
                "old/only-old.wav",  # off
                "audio/only-pilot.wav",  # the named folder only
                "audio/../pilot/only-pilot.wav"):
        with pytest.raises(PathError):
            resolve_served("", folders, bad)
    # Without names, the one root as before.
    assert resolve_served(str(folder), {}, "2026/interview.wav").name == "interview.wav"
