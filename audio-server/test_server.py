"""
Tests for the Clew audio server. A real server on a free port and a real folder, no mocks:

    python -m unittest discover -s tools/audio-server -v
"""

import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import clew_audio_server as srv

SECRET = "test-secret-not-a-real-one"
BYTES = bytes(range(256)) * 40  # 10,240 bytes, every value, so a wrong slice is visible


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name, "interviews").resolve()
        (cls.root / "2026" / "spring").mkdir(parents=True)
        (cls.root / ".hidden").mkdir()
        (cls.root / "a.wav").write_bytes(BYTES)
        (cls.root / "old-dictaphone.WMA").write_bytes(BYTES[:500])
        (cls.root / "2026" / "spring" / "b.m4a").write_bytes(BYTES[:1000])
        (cls.root / "notes.txt").write_text("not audio")
        (cls.root / ".hidden" / "c.mp3").write_bytes(b"x")
        (cls.root / ".secret.mp3").write_bytes(b"x")
        cls.outside = Path(cls.tmp.name, "outside.mp3")
        cls.outside.write_bytes(b"private")

        config = srv.Config(Path(cls.tmp.name, "config.json"), {"v": 1})
        config.set_secret(SECRET)
        cls.state = srv.State(cls.root, config, ["https://clew.example"])
        cls.server = srv.create_server(cls.state, 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            h = dict(headers or {})
            data = None
            if body is not None:
                data = json.dumps(body)
                h["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def auth(self, secret=SECRET):
        return {"Authorization": f"Bearer {secret}"}

    def link(self, rel):
        status, _, raw = self.call("POST", "/api/url", self.auth(), {"path": rel})
        self.assertEqual(status, 200, raw)
        return json.loads(raw)["url"]


class Discovery(ServerCase):
    def test_ping_needs_no_secret(self):
        status, _, raw = self.call("GET", "/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["server"], "clew-audio-server")

    def test_lists_audio_recursively_and_nothing_else(self):
        status, _, raw = self.call("GET", "/api/files", self.auth())
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(
            [f["path"] for f in body["files"]],
            ["interviews/2026/spring/b.m4a", "interviews/a.wav", "interviews/old-dictaphone.WMA"],
        )
        self.assertEqual((body["folder"], body["folders"]), ("interviews", ["interviews"]))
        a = next(f for f in body["files"] if f["path"] == "interviews/a.wav")
        self.assertEqual((a["size"], a["mime"], a["name"]), (len(BYTES), "audio/wav", "a.wav"))
        self.assertIsInstance(a["modified"], int)

    def test_the_full_path_of_the_folder_is_not_sent(self):
        _, _, raw = self.call("GET", "/api/files", self.auth())
        self.assertNotIn(str(self.root.parent).encode(), raw)

    def test_a_link_out_of_the_folder_is_not_followed(self):
        outside_dir = Path(self.tmp.name, "elsewhere")
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "x.mp3").write_bytes(b"private")
        file_link, dir_link = self.root / "escape.mp3", self.root / "escape-dir"
        try:
            try:
                os.symlink(self.outside, file_link)
                os.symlink(outside_dir, dir_link, target_is_directory=True)
            except (OSError, NotImplementedError):
                # Windows needs a privilege for symlinks; a directory junction is the same escape.
                if os.name != "nt":
                    raise
                subprocess.run(["cmd", "/c", "mklink", "/J", str(dir_link), str(outside_dir)], check=True, capture_output=True)
            _, _, raw = self.call("GET", "/api/files", self.auth())
            paths = [f["path"] for f in json.loads(raw)["files"]]
            self.assertFalse([p for p in paths if p.startswith("interviews/escape")], paths)
            status, _, _ = self.call("POST", "/api/url", self.auth(), {"path": "escape-dir/x.mp3"})
            self.assertEqual(status, 404)
        finally:
            for link in (file_link, dir_link):
                if link.is_symlink() or link.is_file():
                    link.unlink()
                elif link.exists():
                    os.rmdir(link)  # a junction: removes the link, not the folder it points at


class Credentials(ServerCase):
    def test_no_secret_no_list(self):
        self.assertEqual(self.call("GET", "/api/files")[0], 401)

    def test_wrong_secret_no_list(self):
        self.assertEqual(self.call("GET", "/api/files", self.auth("nope"))[0], 401)

    def test_no_secret_no_link(self):
        self.assertEqual(self.call("POST", "/api/url", None, {"path": "a.wav"})[0], 401)

    def test_the_secret_is_not_kept_in_the_config(self):
        self.assertNotIn(SECRET, json.dumps(self.state.config.data))

    def test_a_link_without_a_valid_signature_is_refused(self):
        url = self.link("a.wav")
        self.assertEqual(self.call("GET", url)[0], 200)
        self.assertEqual(self.call("GET", url[:-2] + "xx")[0], 401)
        self.assertEqual(self.call("GET", "/api/stream?p=a.wav&exp=9999999999&sig=x")[0], 401)

    def test_a_link_is_for_one_file(self):
        url = self.link("a.wav").replace("p=a.wav", "p=2026%2Fspring%2Fb.m4a")
        self.assertEqual(self.call("GET", url)[0], 401)

    def test_an_expired_link_is_refused(self):
        exp = int(time.time()) - 5
        url = f"/api/stream?p=a.wav&exp={exp}&sig={self.state.sign('a.wav', exp)}"
        self.assertEqual(self.call("GET", url)[0], 401)

    def test_a_link_from_another_run_is_refused(self):
        url = self.link("a.wav")
        old = self.state.link_key
        self.state.link_key = b"\x01" * 32
        try:
            self.assertEqual(self.call("GET", url)[0], 401)
        finally:
            self.state.link_key = old

    def test_the_log_never_carries_the_query(self):
        import io
        import contextlib

        url = self.link("a.wav")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.call("GET", url)
        self.assertIn("/api/stream", buf.getvalue())
        self.assertNotIn("sig=", buf.getvalue())


class Paths(ServerCase):
    def test_cannot_ask_for_a_file_outside_the_folder(self):
        for rel in ["../outside.mp3", "..\\outside.mp3", "/outside.mp3", "C:/x.mp3", "a/../../outside.mp3", ".secret.mp3"]:
            status, _, _ = self.call("POST", "/api/url", self.auth(), {"path": rel})
            self.assertEqual(status, 404, rel)

    def test_a_forged_link_for_an_outside_file_is_still_refused(self):
        exp = int(time.time()) + 100
        rel = "../outside.mp3"
        url = f"/api/stream?p=..%2Foutside.mp3&exp={exp}&sig={self.state.sign(rel, exp)}"
        self.assertEqual(self.call("GET", url)[0], 404)

    def test_only_audio_is_served(self):
        self.assertEqual(self.call("POST", "/api/url", self.auth(), {"path": "notes.txt"})[0], 404)


class Streaming(ServerCase):
    def test_whole_file(self):
        status, h, raw = self.call("GET", self.link("a.wav"))
        self.assertEqual((status, raw), (200, BYTES))
        self.assertEqual(h["content-type"], "audio/wav")
        self.assertEqual(h["accept-ranges"], "bytes")

    def test_a_range(self):
        status, h, raw = self.call("GET", self.link("a.wav"), {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, BYTES[100:200])
        self.assertEqual(h["content-range"], f"bytes 100-199/{len(BYTES)}")

    def test_an_open_ended_range(self):
        status, _, raw = self.call("GET", self.link("a.wav"), {"Range": "bytes=10000-"})
        self.assertEqual((status, raw), (206, BYTES[10000:]))

    def test_a_suffix_range(self):
        status, _, raw = self.call("GET", self.link("a.wav"), {"Range": "bytes=-50"})
        self.assertEqual((status, raw), (206, BYTES[-50:]))

    def test_a_range_past_the_end_is_clamped(self):
        status, _, raw = self.call("GET", self.link("a.wav"), {"Range": "bytes=10000-99999"})
        self.assertEqual((status, raw), (206, BYTES[10000:]))

    def test_an_unsatisfiable_range(self):
        status, h, _ = self.call("GET", self.link("a.wav"), {"Range": "bytes=99999-"})
        self.assertEqual(status, 416)
        self.assertEqual(h["content-range"], f"bytes */{len(BYTES)}")

    def test_head_has_the_size_and_no_body(self):
        status, h, raw = self.call("HEAD", self.link("a.wav"))
        self.assertEqual((status, raw), (200, b""))
        self.assertEqual(h["content-length"], str(len(BYTES)))

    def test_wma_is_listed_and_served_in_any_letter_case(self):
        status, h, raw = self.call("GET", self.link("old-dictaphone.WMA"))
        self.assertEqual((status, raw), (200, BYTES[:500]))
        self.assertEqual(h["content-type"], "audio/x-ms-wma")

    def test_a_file_in_a_subfolder(self):
        status, _, raw = self.call("GET", self.link("2026/spring/b.m4a"))
        self.assertEqual((status, raw), (200, BYTES[:1000]))


def mp3_frame(fill: int, kbps_index: int = 9, padding: int = 0) -> bytes:
    """One MPEG1 layer III stereo frame at 44.1 kHz (128 kbit/s by default: 417 bytes), body all `fill`."""
    length = 144 * [32, 40, 48, 56, 64, 80, 96, 112, 128][kbps_index - 1] * 1000 // 44100 + padding
    return bytes([0xFF, 0xFB, kbps_index << 4 | padding << 1, 0x00]) + bytes([fill]) * (length - 4)


ID3_OF_20_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + bytes(10)


def mp3_with_tag(tag: bytes, lead: bytes = b"") -> bytes:
    """A file that starts with a frame holding `tag` and a nonsense seek table, then 40 frames of sound."""
    first = bytearray(mp3_frame(0))
    first[36:40] = tag
    first[40:140] = bytes([0xAA]) * 100
    return lead + bytes(first) + b"".join(mp3_frame(0x55) for _ in range(40))


def ffmpeg_can_make_mp3() -> str | None:
    exe = srv.find_ffmpeg(None)
    if exe is None:
        return None
    try:
        made = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if b"libmp3lame" in made.stdout else None


class Mp3Case(ServerCase):
    """mp3 files shaped like a long recording's: a first frame that is only a tag."""

    FFMPEG: str | None = None
    INFO = mp3_with_tag(b"Info")
    XING = mp3_with_tag(b"Xing")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        (cls.root / "cbr.mp3").write_bytes(cls.INFO)
        (cls.root / "vbr.mp3").write_bytes(cls.XING)
        (cls.root / "tagged.mp3").write_bytes(mp3_with_tag(b"Info", lead=ID3_OF_20_BYTES))
        (cls.root / "plain.mp3").write_bytes(mp3_frame(0x55) * 40)
        cls.state.seek = srv.Mp3Seek(cls.FFMPEG, Path(cls.tmp.name, "cache"))


class Mp3Heads(unittest.TestCase):
    def head(self, data: bytes) -> srv.Mp3Head:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "x.mp3")
            p.write_bytes(data)
            return srv.read_mp3_head(p)

    def test_a_constant_bit_rate_tag(self):
        h = self.head(Mp3Case.INFO)
        self.assertEqual((h.kind, h.tag_start, h.frame_end, h.blank), ("info", 36, 417, (36, 417)))

    def test_a_variable_bit_rate_tag_is_not_blanked(self):
        h = self.head(Mp3Case.XING)
        self.assertEqual((h.kind, h.blank), ("xing", None))

    def test_a_tag_after_an_id3_tag(self):
        h = self.head(mp3_with_tag(b"Info", lead=ID3_OF_20_BYTES))
        self.assertEqual((h.kind, h.tag_start, h.frame_end), ("info", 20 + 36, 20 + 417))

    def test_a_file_without_a_tag_and_a_file_that_is_not_an_mp3(self):
        self.assertEqual(self.head(mp3_frame(0x55) * 40).kind, "none")
        self.assertEqual(self.head(BYTES).kind, "none")
        self.assertEqual(self.head(b"").kind, "none")


class Mp3Blanking(Mp3Case):
    def test_the_tag_of_a_constant_bit_rate_file_is_sent_as_zeros_at_the_same_length(self):
        status, h, raw = self.call("GET", self.link("cbr.mp3"))
        self.assertEqual(status, 200)
        self.assertEqual(len(raw), len(self.INFO))
        self.assertEqual(raw[:36], self.INFO[:36])
        self.assertEqual(raw[36:417], bytes(417 - 36))
        self.assertEqual(raw[417:], self.INFO[417:])
        self.assertEqual(h["content-length"], str(len(self.INFO)))

    def test_a_range_inside_the_tag_and_one_across_its_edges(self):
        _, _, inside = self.call("GET", self.link("cbr.mp3"), {"Range": "bytes=100-199"})
        self.assertEqual(inside, bytes(100))
        _, h, across = self.call("GET", self.link("cbr.mp3"), {"Range": "bytes=30-420"})
        self.assertEqual(across, self.INFO[30:36] + bytes(417 - 36) + self.INFO[417:421])
        self.assertEqual(h["content-range"], f"bytes 30-420/{len(self.INFO)}")

    def test_a_range_after_the_tag_is_untouched(self):
        _, _, after = self.call("GET", self.link("cbr.mp3"), {"Range": "bytes=417-"})
        self.assertEqual(after, self.INFO[417:])

    def test_blanking_survives_chunk_boundaries(self):
        old = srv.CHUNK
        srv.CHUNK = 50
        try:
            _, _, raw = self.call("GET", self.link("cbr.mp3"))
        finally:
            srv.CHUNK = old
        self.assertEqual(raw, self.INFO[:36] + bytes(417 - 36) + self.INFO[417:])

    def test_the_tag_behind_an_id3_tag(self):
        _, _, raw = self.call("GET", self.link("tagged.mp3"))
        self.assertEqual(raw[20 + 36 : 20 + 417], bytes(417 - 36))

    def test_other_files_are_sent_as_they_are(self):
        _, _, vbr = self.call("GET", self.link("vbr.mp3"))
        self.assertEqual(vbr, self.XING)
        _, _, plain = self.call("GET", self.link("plain.mp3"))
        self.assertEqual(plain, mp3_frame(0x55) * 40)
        _, _, wav = self.call("GET", self.link("a.wav"))
        self.assertEqual(wav, BYTES)

    def test_without_ffmpeg_the_link_tells_the_files_own_size(self):
        _, _, raw = self.call("POST", "/api/url", self.auth(), {"path": "vbr.mp3"})
        self.assertEqual(json.loads(raw)["size"], len(self.XING))


class Mp3Conversion(Mp3Case):
    FFMPEG = ffmpeg_can_make_mp3()

    @classmethod
    def setUpClass(cls) -> None:
        if cls.FFMPEG is None:
            raise unittest.SkipTest("no ffmpeg with libmp3lame")
        super().setUpClass()
        # A real variable-bit-rate mp3 (with a Xing tag): 20 seconds of a tone.
        made = subprocess.run(
            [
                cls.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                "-c:a", "libmp3lame", "-q:a", "4", "-write_xing", "1", str(cls.root / "real-vbr.mp3"),
            ],
            capture_output=True,
        )
        assert made.returncode == 0, made.stderr

    def test_a_variable_bit_rate_file_is_converted_once_and_sent_without_a_table(self):
        original = (self.root / "real-vbr.mp3").read_bytes()
        self.assertEqual(srv.read_mp3_head(self.root / "real-vbr.mp3").kind, "xing")
        _, _, raw = self.call("POST", "/api/url", self.auth(), {"path": "real-vbr.mp3"})
        told = json.loads(raw)
        _, h, body = self.call("GET", told["url"])
        self.assertEqual(h["content-type"], "audio/mpeg")
        self.assertEqual(len(body), told["size"])
        self.assertNotEqual(body, original)
        with tempfile.TemporaryDirectory() as d:
            sent = Path(d, "sent.mp3")
            sent.write_bytes(body)
            self.assertEqual(srv.read_mp3_head(sent).kind, "none")
        # Ranges are those of the converted file.
        _, h, part = self.call("GET", told["url"], {"Range": "bytes=100-199"})
        self.assertEqual((part, h["content-range"]), (body[100:200], f"bytes 100-199/{len(body)}"))
        # The second time nothing is converted again.
        runs = []
        real_run = srv.subprocess.run
        srv.subprocess.run = lambda *a, **k: runs.append(a) or real_run(*a, **k)
        try:
            self.call("POST", "/api/url", self.auth(), {"path": "real-vbr.mp3"})
        finally:
            srv.subprocess.run = real_run
        self.assertEqual(runs, [])
        self.assertEqual(len(list(Path(self.tmp.name, "cache").glob("*.mp3"))), 1)

    def test_a_wma_file_is_sent_as_an_mp3(self):
        made = subprocess.run(
            [
                self.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
                "-c:a", "wmav2", str(self.root / "dictaphone.wma"),
            ],
            capture_output=True,
        )
        if made.returncode != 0:
            self.skipTest("this ffmpeg cannot write wma")
        _, _, raw = self.call("POST", "/api/url", self.auth(), {"path": "dictaphone.wma"})
        told = json.loads(raw)
        status, h, body = self.call("GET", told["url"])
        self.assertEqual((status, h["content-type"]), (200, "audio/mpeg"))
        self.assertEqual(len(body), told["size"])
        self.assertTrue(body[0] == 0xFF or body.startswith(b"ID3"), body[:4])
        # What the listing says about the file is still the file's own.
        _, _, listing = self.call("GET", "/api/files", self.auth())
        entry = next(f for f in json.loads(listing)["files"] if f["path"].endswith("/dictaphone.wma"))
        self.assertEqual(entry["mime"], "audio/x-ms-wma")

    def test_without_ffmpeg_a_wma_file_is_sent_as_it_is(self):
        srv_seek = self.state.seek
        self.state.seek = srv.Mp3Seek(None, Path(self.tmp.name, "cache2"))
        try:
            _, _, raw = self.call("POST", "/api/url", self.auth(), {"path": "old-dictaphone.WMA"})
            told = json.loads(raw)
            _, h, body = self.call("GET", told["url"])
        finally:
            self.state.seek = srv_seek
        self.assertEqual((h["content-type"], body), ("audio/x-ms-wma", BYTES[:500]))

    def test_a_constant_bit_rate_file_is_not_converted(self):
        _, _, raw = self.call("POST", "/api/url", self.auth(), {"path": "cbr.mp3"})
        self.assertEqual(json.loads(raw)["size"], len(self.INFO))


class Ffmpeg(unittest.TestCase):
    def test_off_and_a_missing_path_mean_none(self):
        self.assertIsNone(srv.find_ffmpeg("off"))
        self.assertIsNone(srv.find_ffmpeg(str(Path(tempfile.gettempdir(), "no-such-dir", "ffmpeg"))))

    def test_a_given_path_wins(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d, "ffmpeg")
            fake.write_text("")
            self.assertEqual(srv.find_ffmpeg(str(fake)), str(fake))


class Browser(ServerCase):
    def test_the_hosts_other_than_loopback_are_refused(self):
        # A page on another name pointing at 127.0.0.1 (DNS rebinding) arrives with its own Host.
        status, _, _ = self.call("GET", "/api/ping", {"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_a_foreign_origin_is_refused(self):
        status, _, _ = self.call("GET", "/api/ping", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_loopback_and_configured_origins_get_cors(self):
        for origin in ["http://localhost:5173", "http://127.0.0.1:4173", "https://clew.example"]:
            status, h, _ = self.call("GET", "/api/ping", {"Origin": origin})
            self.assertEqual(status, 200, origin)
            self.assertEqual(h["access-control-allow-origin"], origin)

    def test_preflight(self):
        status, h, _ = self.call(
            "OPTIONS",
            "/api/files",
            {
                "Origin": "https://clew.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        self.assertEqual(status, 204)
        self.assertIn("Authorization", h["access-control-allow-headers"])
        self.assertEqual(h["access-control-allow-private-network"], "true")

    def test_a_player_request_without_an_origin_is_fine(self):
        self.assertEqual(self.call("GET", self.link("a.wav"))[0], 200)


class SeveralFolders(unittest.TestCase):
    """Two projects' folders served as one, and a third that is off (docs/22 §11)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        for name, files in (
            ("study-a", {"p01.mp3": b"A1", "pilot/p02.mp3": b"A2"}),
            ("study-b", {"p01.mp3": b"B1", "only-b.wav": b"B2"}),
            ("study-c", {"only-c.mp3": b"C1"}),
        ):
            for rel, data in files.items():
                (base / name / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / name / rel).write_bytes(data)
        config = srv.Config(Path(cls.tmp.name, "config.json"), {"v": 1})
        config.set_secret(SECRET)
        folders = srv.make_folders(
            [
                {"path": str(base / "study-a"), "enabled": True},
                {"path": str(base / "study-b"), "enabled": True},
                {"path": str(base / "study-c"), "enabled": False},
            ]
        )
        cls.server = srv.create_server(srv.State(folders, config, []), 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    call = ServerCase.call
    auth = ServerCase.auth

    def play(self, rel):
        status, _, raw = self.call("POST", "/api/url", self.auth(), {"path": rel})
        if status != 200:
            return status
        return self.call("GET", json.loads(raw)["url"])[2]

    def test_the_folders_that_are_on_are_listed_under_their_names(self):
        _, _, raw = self.call("GET", "/api/files", self.auth())
        body = json.loads(raw)
        self.assertEqual(
            [f["path"] for f in body["files"]],
            ["study-a/p01.mp3", "study-a/pilot/p02.mp3", "study-b/only-b.wav", "study-b/p01.mp3"],
        )
        self.assertEqual((body["folder"], body["folders"]), ("study-a, study-b", ["study-a", "study-b"]))

    def test_a_name_picks_its_folder(self):
        self.assertEqual(self.play("study-a/p01.mp3"), b"A1")
        self.assertEqual(self.play("study-b/p01.mp3"), b"B1")
        self.assertEqual(self.play("study-a/only-b.wav"), 404)
        self.assertEqual(self.play("study-a/../study-b/p01.mp3"), 404)

    def test_a_folder_that_is_off_serves_nothing(self):
        self.assertEqual(self.play("study-c/only-c.mp3"), 404)
        self.assertEqual(self.play("only-c.mp3"), 404)

    def test_a_path_saved_before_names_is_found_only_where_it_is_not_ambiguous(self):
        self.assertEqual(self.play("only-b.wav"), b"B2")
        self.assertEqual(self.play("pilot/p02.mp3"), b"A2")
        self.assertEqual(self.play("p01.mp3"), 404)  # in both folders


class Units(unittest.TestCase):
    def test_parse_range(self):
        self.assertIsNone(srv.parse_range(None, 100))
        self.assertEqual(srv.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(srv.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(srv.parse_range("bytes=-10", 100), (90, 99))
        for bad in ["bytes=-", "bytes=5-2", "bytes=100-", "bytes=-0", "items=0-1", "bytes=a-b"]:
            self.assertEqual(srv.parse_range(bad, 100), "bad", bad)

    def test_config_round_trip_and_secret(self):
        with tempfile.TemporaryDirectory() as d:
            c = srv.Config(Path(d, "x", "config.json"), {"v": 1})
            c.set_secret("abc")
            c.folders = [{"path": "F", "enabled": True}, {"path": "G", "enabled": False}]
            c.save()
            again = srv.Config.load(Path(d, "x", "config.json"))
            self.assertEqual(again.folders, [{"path": "F", "enabled": True}, {"path": "G", "enabled": False}])
            self.assertTrue(again.check_secret("abc"))
            self.assertFalse(again.check_secret("abd"))

    def test_a_config_from_before_several_folders_keeps_its_folder(self):
        c = srv.Config(Path("unused.json"), {"v": 1, "folder": "D:/old"})
        self.assertEqual(c.folders, [{"path": "D:/old", "enabled": True}])

    def test_folder_names(self):
        self.assertEqual(
            srv.folder_names(["D:\\Study A\\audio", "E:\\Study B\\Audio", "F:\\x\\audio\\", "G:\\", "/srv/pilot"]),
            ["audio", "Audio 2", "audio 3", "G", "pilot"],
        )

    def test_a_broken_config_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "config.json")
            p.write_text("{not json")
            c = srv.Config.load(p)
            self.assertFalse(c.has_secret())


class EnvFile(unittest.TestCase):
    def write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name, ".env")
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_keys_comments_quotes_and_windows_paths(self):
        nl, bs, bom = chr(10), chr(92), chr(0xFEFF)
        path = self.write(
            nl.join(
                [
                    bom + "# a comment",
                    "CLEW_AUDIO_FOLDER=D:" + bs + "interviews" + bs + "2026",
                    'CLEW_AUDIO_ALLOW_ORIGINS="https://a.example, https://b.example"',
                    "export CLEW_AUDIO_PORT=5000 # a trailing comment",
                    "",
                ]
            )
        )
        values, warnings = srv.load_env(path)
        self.assertEqual(values["CLEW_AUDIO_FOLDER"], "D:" + bs + "interviews" + bs + "2026")
        self.assertEqual(values["CLEW_AUDIO_ALLOW_ORIGINS"], "https://a.example, https://b.example")
        self.assertEqual(values["CLEW_AUDIO_PORT"], "5000")
        self.assertEqual(warnings, [])

    def test_warns_about_what_it_cannot_use_and_keeps_going(self):
        nl = chr(10)
        values, warnings = srv.load_env(self.write(nl.join(["nonsense", "CLEW_AUDIO_TYPO=1", "CLEW_AUDIO_PORT=80"])))
        self.assertEqual(values, {"CLEW_AUDIO_PORT": "80"})
        self.assertEqual(len(warnings), 2)

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(srv.load_env(Path("/no/such/.env")), ({}, []))

    def test_a_bad_number_is_ignored_with_a_warning(self):
        warnings = []
        self.assertIsNone(srv.env_int({"CLEW_AUDIO_PORT": "abc"}, "CLEW_AUDIO_PORT", warnings))
        self.assertIsNone(srv.env_int({"CLEW_AUDIO_PORT": "-3"}, "CLEW_AUDIO_PORT", warnings))
        self.assertEqual(srv.env_int({"CLEW_AUDIO_PORT": "12"}, "CLEW_AUDIO_PORT", warnings), 12)
        self.assertEqual(len(warnings), 2)


class StartFromEnv(unittest.TestCase):
    """The program itself, started the way a researcher starts it, from nothing but a .env."""

    def start(self, lines, *extra):
        import socket
        import subprocess
        import sys
        from types import SimpleNamespace

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        base = Path(d.name)
        for name, audio in (("from-env", "a.mp3"), ("from-cli", "b.mp3")):
            (base / name).mkdir()
            (base / name / audio).write_bytes(b"x")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        fill = dict(env=base / "from-env", cli=base / "from-cli", port=port)
        (base / ".env").write_text(chr(10).join(lines).format(**fill), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", srv.__file__, "--env", str(base / ".env"),
             "--config", str(base / "c.json"), *[x.format(**fill) for x in extra]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        out = ""
        for line in proc.stdout:
            out += line
            if "Press Ctrl+C" in line:
                break
        secret = out.split("paste it into Clew):")[1].split()[0]
        return SimpleNamespace(port=port, secret=secret, out=out, dir=base)

    def get(self, port, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def paths(self, run):
        _, _, raw = self.get(run.port, "/api/files", {"Authorization": f"Bearer {run.secret}"})
        return [f["path"] for f in json.loads(raw)["files"]]

    def test_folder_port_and_origin_come_from_the_env_file(self):
        run = self.start(
            [
                "CLEW_AUDIO_FOLDER={env}",
                "CLEW_AUDIO_PORT={port}",
                "CLEW_AUDIO_ALLOW_ORIGINS=https://clew.example",
            ]
        )
        self.assertIn(f"127.0.0.1:{run.port}", run.out)
        status, headers, _ = self.get(run.port, "/api/ping", {"Origin": "https://clew.example"})
        self.assertEqual((status, headers["access-control-allow-origin"]), (200, "https://clew.example"))
        self.assertEqual(self.paths(run), ["from-env/a.mp3"])

    def test_the_command_line_wins_over_the_env_file(self):
        run = self.start(["CLEW_AUDIO_FOLDER={env}", "CLEW_AUDIO_PORT={port}"], "--folder", "{cli}")
        self.assertEqual(self.paths(run), ["from-cli/b.mp3"])

    def test_an_origin_from_the_env_file_is_not_written_to_the_remembered_config(self):
        run = self.start(
            [
                "CLEW_AUDIO_FOLDER={env}",
                "CLEW_AUDIO_PORT={port}",
                "CLEW_AUDIO_ALLOW_ORIGINS=https://temp.example",
            ]
        )
        self.assertIn("https://temp.example", run.out)
        saved = json.loads((run.dir / "c.json").read_text(encoding="utf-8"))
        # Only the first start's default is remembered, never the .env's.
        self.assertEqual(saved.get("origins"), ["https://clew.alefa.net"])

    def test_a_bad_line_is_warned_about_and_the_rest_still_applies(self):
        run = self.start(["CLEW_AUDIO_FOLDER={env}", "CLEW_AUDIO_PORT={port}", "CLEW_AUDIO_TYPO=1"])
        self.assertIn("unknown key CLEW_AUDIO_TYPO", run.out)
        self.assertEqual(self.paths(run), ["from-env/a.mp3"])


if __name__ == "__main__":
    unittest.main()
