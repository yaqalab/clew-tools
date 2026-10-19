"""
Black-box tests for the native Clew audio server: the real program, started headless on a free port,
a real folder, real HTTP. No mocks. Mirrors tools/audio-server/test_server.py where the behaviour is
the same, and adds what is new (request limits, conversion with Windows' own codecs, the sandbox).

    python -m unittest discover -s tools/audio-server/native/test -v

The program: CLEW_AUDIO_SERVER_EXE, else tools/audio-server/native/build/clew-audio-server(.exe).
Fixtures are made with ffmpeg when it is on PATH (only to make them; the server never uses it
unless a test says so).
"""

import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = Path(
    os.environ.get("CLEW_AUDIO_SERVER_EXE")
    or HERE.parent / "build" / ("clew-audio-server.exe" if os.name == "nt" else "clew-audio-server")
)
FFMPEG = shutil.which("ffmpeg")
BYTES = bytes(range(256)) * 40  # 10,240 bytes, every value, so a wrong slice is visible


def mp3_frame(fill, tag=None):
    """MPEG1 layer III, 128 kbit/s, 44.1 kHz, stereo: 417 bytes; a Xing/Info tag sits at 4 + 32."""
    f = bytearray([fill]) * 417
    f[0:4] = b"\xff\xfb\x90\x00"
    if tag:
        f[36:40] = tag
    return bytes(f)


class Server:
    """The program, running headless. Its log lines (stderr) are kept in `log`."""

    def __init__(self, folder, home, *extra):
        self.log = []
        self.proc = subprocess.Popen(
            [
                str(EXE), "--headless", "--folder", str(folder), "--port", "0",
                "--config", str(Path(home, "settings.json")), *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        out = []
        deadline = time.time() + 15
        while time.time() < deadline:
            line = self.proc.stdout.readline().decode("utf-8", "replace")
            if not line:
                break
            out.append(line)
            if "Press Ctrl+C" in line:
                break
        self.banner = "".join(out)
        m = re.search(r"127\.0\.0\.1:(\d+)", self.banner)
        if not m:
            self.proc.kill()
            raise RuntimeError("the server did not start: " + self.banner + self.proc.stderr.read().decode())
        self.port = int(m.group(1))
        s = re.search(r"Secret[^\n]*\n\s+(\S+)", self.banner)
        self.secret = s.group(1) if s else None
        threading.Thread(target=self._read_log, daemon=True).start()

    def _read_log(self):
        for raw in self.proc.stderr:
            self.log.append(raw.decode("utf-8", "replace").rstrip())

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def call(self, method, path, headers=None, body=None, raw_body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            h = dict(headers or {})
            data = raw_body
            if body is not None:
                data = json.dumps(body)
                h["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def raw(self, request, read=65536):
        """Sends bytes as they are and returns what comes back before the server closes or 2 s pass."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as s:
            s.sendall(request)
            got = b""
            try:
                while len(got) < read:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    got += chunk
            except socket.timeout:
                pass
            return got

    def auth(self, secret=None):
        return {"Authorization": f"Bearer {secret or self.secret}"}

    def link(self, rel):
        status, _, raw = self.call("POST", "/api/url", self.auth(), {"path": rel})
        assert status == 200, (status, raw)
        return json.loads(raw)


@unittest.skipUnless(EXE.is_file(), f"build the program first: {EXE}")
class Case(unittest.TestCase):
    EXTRA: tuple = ("--allow-origin", "https://clew.example")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name, "home")
        cls.root = Path(cls.tmp.name, "interviews").resolve()
        (cls.root / "2026" / "spring").mkdir(parents=True)
        (cls.root / ".hidden").mkdir()
        (cls.root / "a.wav").write_bytes(BYTES)
        (cls.root / "old-dictaphone.WMA").write_bytes(BYTES[:500])
        (cls.root / "2026" / "spring" / "b.m4a").write_bytes(BYTES[:1000])
        (cls.root / "日本語の面接.mp3").write_bytes(mp3_frame(0x11) * 3)
        (cls.root / "notes.txt").write_text("not audio")
        (cls.root / ".hidden" / "c.mp3").write_bytes(b"x")
        (cls.root / ".secret.mp3").write_bytes(b"x")
        Path(cls.tmp.name, "outside.mp3").write_bytes(b"private")
        cls.prepare_folder()
        cls.server = Server(cls.root, cls.home, *cls.EXTRA)

    @classmethod
    def prepare_folder(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        for _ in range(20):
            try:
                cls.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.25)  # the killed process lets go of its files a moment later

    def call(self, *a, **k):
        return self.server.call(*a, **k)


class Discovery(Case):
    def test_ping_needs_no_secret(self):
        status, h, raw = self.call("GET", "/api/ping")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual((body["server"], body["api"]), ("clew-audio-server", 1))
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["x-content-type-options"], "nosniff")

    def test_lists_audio_recursively_and_nothing_else(self):
        status, _, raw = self.call("GET", "/api/files", self.server.auth())
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(
            [f["path"] for f in body["files"]],
            ["interviews/2026/spring/b.m4a", "interviews/a.wav", "interviews/old-dictaphone.WMA",
             "interviews/日本語の面接.mp3"],
        )
        self.assertEqual((body["folder"], body["folders"], body["truncated"]), ("interviews", ["interviews"], False))
        a = next(f for f in body["files"] if f["path"] == "interviews/a.wav")
        self.assertEqual((a["size"], a["mime"], a["name"]), (len(BYTES), "audio/wav", "a.wav"))
        self.assertAlmostEqual(a["modified"] / 1000, (self.root / "a.wav").stat().st_mtime, delta=2)

    def test_the_full_path_of_the_folder_is_not_sent(self):
        _, _, raw = self.call("GET", "/api/files", self.server.auth())
        self.assertNotIn(str(self.root.parent).encode(), raw)
        self.assertNotIn(json.dumps(str(self.root.parent))[1:-1].encode(), raw)

    def test_a_link_out_of_the_folder_is_not_followed(self):
        outside_dir = Path(self.tmp.name, "elsewhere")
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "x.mp3").write_bytes(b"private")
        dir_link = self.root / "escape-dir"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dir_link), str(outside_dir)], check=True, capture_output=True)
        else:
            os.symlink(outside_dir, dir_link, target_is_directory=True)
        try:
            _, _, raw = self.call("GET", "/api/files", self.server.auth())
            paths = [f["path"] for f in json.loads(raw)["files"]]
            self.assertFalse([p for p in paths if p.startswith("interviews/escape")], paths)
            self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": "escape-dir/x.mp3"})[0], 404)
        finally:
            os.rmdir(dir_link) if os.name == "nt" else dir_link.unlink()

    def test_a_link_inside_the_folder_is_followed(self):
        inner = self.root / "2026"
        link = self.root / "shortcut"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(inner)], check=True, capture_output=True)
        else:
            os.symlink(inner, link, target_is_directory=True)
        try:
            _, _, raw = self.call("GET", "/api/files", self.server.auth())
            self.assertIn("interviews/shortcut/spring/b.m4a", [f["path"] for f in json.loads(raw)["files"]])
        finally:
            os.rmdir(link) if os.name == "nt" else link.unlink()


@unittest.skipUnless(EXE.is_file(), f"build the program first: {EXE}")
class SeveralFolders(unittest.TestCase):
    """Two projects' folders served as one, a third that is off (docs/22 section 11). The same cases as
    tools/audio-server/test_server.py's SeveralFolders."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        for name, files in (
            ("study-a", {"p01.mp3": b"A1", "pilot/p02.mp3": b"A2"}),
            ("study-b", {"p01.mp3": b"B1", "only-b.wav": b"B2"}),
            ("study-c", {"only-c.mp3": b"C1"}),
            ("x/audio", {"x.mp3": b"X"}),
            ("y/Audio", {"y.mp3": b"Y"}),
        ):
            for rel, data in files.items():
                (base / name / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / name / rel).write_bytes(data)
        cls.base = base
        cls.server = Server(base / "study-a", Path(base, "home"), "--folder", str(base / "study-b"),
                            "--folder-off", str(base / "study-c"), "--folder", str(base / "x" / "audio"),
                            "--folder", str(base / "y" / "Audio"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        for _ in range(20):
            try:
                cls.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.25)

    def play(self, rel):
        status, _, raw = self.server.call("POST", "/api/url", self.server.auth(), {"path": rel})
        if status != 200:
            return status
        return self.server.call("GET", json.loads(raw)["url"])[2]

    def test_the_banner_names_each_folder(self):
        self.assertIn('Folder "study-a"', self.server.banner)
        self.assertIn('Folder "study-c": ' + str(self.base / "study-c") + "  (off)", self.server.banner)
        self.assertIn('Folder "Audio 2"', self.server.banner)

    def test_the_folders_that_are_on_are_listed_under_their_names(self):
        _, _, raw = self.server.call("GET", "/api/files", self.server.auth())
        body = json.loads(raw)
        self.assertEqual(
            [f["path"] for f in body["files"]],
            ["study-a/p01.mp3", "study-a/pilot/p02.mp3", "study-b/only-b.wav", "study-b/p01.mp3", "audio/x.mp3",
             "Audio 2/y.mp3"],
        )
        self.assertEqual(body["folders"], ["study-a", "study-b", "audio", "Audio 2"])
        self.assertEqual(body["folder"], "study-a, study-b, audio, Audio 2")

    def test_a_name_picks_its_folder(self):
        self.assertEqual(self.play("study-a/p01.mp3"), b"A1")
        self.assertEqual(self.play("study-b/p01.mp3"), b"B1")
        self.assertEqual(self.play("Audio 2/y.mp3"), b"Y")
        self.assertEqual(self.play("study-a/only-b.wav"), 404)
        self.assertEqual(self.play("study-a/../study-b/p01.mp3"), 404)

    def test_a_folder_that_is_off_serves_nothing(self):
        self.assertEqual(self.play("study-c/only-c.mp3"), 404)
        self.assertEqual(self.play("only-c.mp3"), 404)

    def test_a_path_saved_before_names_is_found_only_where_it_is_not_ambiguous(self):
        self.assertEqual(self.play("only-b.wav"), b"B2")
        self.assertEqual(self.play("pilot/p02.mp3"), b"A2")
        self.assertEqual(self.play("p01.mp3"), 404)  # in both folders

    def test_a_link_names_its_folder(self):
        status, _, raw = self.server.call("POST", "/api/url", self.server.auth(), {"path": "study-b/p01.mp3"})
        url = json.loads(raw)["url"]
        self.assertIn("p=study-b%2Fp01.mp3", url)
        # Signed for that folder's file: the same name in the other folder is another link.
        self.assertEqual(self.server.call("GET", url.replace("study-b", "study-a"))[0], 401)


@unittest.skipUnless(EXE.is_file() and FFMPEG, "build the program first, and ffmpeg makes the fixtures")
class SeveralFoldersConversion(unittest.TestCase):
    """A converted copy stays in its own recording's folder, not in another project's."""

    def test_each_copy_goes_to_its_recordings_folder(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            (base / "study-a").mkdir()
            (base / "study-b").mkdir()
            tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "wmav2"]
            assert make(tone, base / "study-b" / "dictaphone.wma")
            server = Server(base / "study-a", base / "home", "--folder", str(base / "study-b"))
            try:
                self.assertEqual(server.link("study-b/dictaphone.wma")["size"] > 0, True)
                self.assertTrue(list((base / "study-b" / ".clew-converted").glob("*.mp3")))
                self.assertFalse((base / "study-a" / ".clew-converted").exists())
            finally:
                server.stop()
                time.sleep(0.5)


class Credentials(Case):
    def test_no_secret_no_list(self):
        self.assertEqual(self.call("GET", "/api/files")[0], 401)

    def test_wrong_secret_no_list(self):
        self.assertEqual(self.call("GET", "/api/files", self.server.auth("nope"))[0], 401)

    def test_no_secret_no_link(self):
        self.assertEqual(self.call("POST", "/api/url", None, {"path": "a.wav"})[0], 401)

    def test_the_secret_is_not_in_the_settings_file_in_clear(self):
        raw = Path(self.home, "settings.json").read_text(encoding="utf-8")
        self.assertNotIn(self.server.secret, raw)
        self.assertIn('"kind": "dpapi"' if os.name == "nt" else '"kind"', raw)

    def test_a_restart_shows_no_secret_unless_asked_and_keeps_it(self):
        again = Server(self.root, self.home)
        try:
            self.assertIsNone(again.secret)
            self.assertEqual(again.call("GET", "/api/files", again.auth(self.server.secret))[0], 200)
        finally:
            again.stop()
        shown = Server(self.root, self.home, "--show-secret")
        try:
            self.assertEqual(shown.secret, self.server.secret)
        finally:
            shown.stop()

    def test_a_link_without_a_valid_signature_is_refused(self):
        url = self.server.link("a.wav")["url"]
        self.assertEqual(self.call("GET", url)[0], 200)
        self.assertEqual(self.call("GET", url[:-2] + "xx")[0], 401)
        self.assertEqual(self.call("GET", "/api/stream?p=a.wav&exp=9999999999&sig=x")[0], 401)
        self.assertEqual(self.call("GET", "/api/stream?p=a.wav")[0], 401)

    def test_a_link_is_for_one_file(self):
        url = self.server.link("a.wav")["url"].replace("p=a.wav", "p=2026%2Fspring%2Fb.m4a")
        self.assertEqual(self.call("GET", url)[0], 401)

    def test_a_link_from_another_run_is_refused(self):
        url = self.server.link("a.wav")["url"]
        other = Server(self.root, self.home)
        try:
            self.assertEqual(other.call("GET", url)[0], 401)
        finally:
            other.stop()

    def test_the_log_never_carries_the_query(self):
        url = self.server.link("a.wav")["url"]
        self.call("GET", url)
        time.sleep(0.2)
        text = "\n".join(self.server.log)
        self.assertIn("GET /api/stream -> 200", text)
        self.assertNotIn("sig=", text)
        self.assertNotIn(self.server.secret, text)

    def test_repeated_wrong_secrets_slow_down(self):
        for _ in range(5):
            self.call("GET", "/api/files", self.server.auth("wrong"))
        t = time.time()
        self.assertEqual(self.call("GET", "/api/files", self.server.auth("wrong"))[0], 401)
        self.assertGreaterEqual(time.time() - t, 0.9)
        self.assertEqual(self.call("GET", "/api/files", self.server.auth())[0], 200)  # and it resets


class Expiry(Case):
    EXTRA = ("--link-seconds", "1")

    def test_an_expired_link_is_refused(self):
        url = self.server.link("a.wav")["url"]
        self.assertEqual(self.call("GET", url)[0], 200)
        time.sleep(2.2)
        self.assertEqual(self.call("GET", url)[0], 401)


class Paths(Case):
    def test_cannot_ask_for_a_file_outside_the_folder(self):
        for rel in ["../outside.mp3", "2026/../../outside.mp3", "/outside.mp3", "C:/Windows/win.ini", "..\\outside.mp3"]:
            self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": rel})[0], 404, rel)

    def test_windows_tricks_are_refused(self):
        for rel in ["a.wav:secret", "a.wav::$DATA", "a.wav.", "a.wav ", "CON.mp3", "nul.wav", "A.WAV/.."]:
            self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": rel})[0], 404, rel)

    def test_a_forged_link_for_an_outside_file_is_still_refused(self):
        self.assertEqual(self.call("GET", "/api/stream?p=..%2Foutside.mp3&exp=9999999999&sig=x")[0], 401)

    def test_only_audio_is_served(self):
        self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": "notes.txt"})[0], 404)
        self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": ".secret.mp3"})[0], 404)

    def test_a_bad_body_is_refused(self):
        for body in [b"not json", b'{"path": 5}', b"[]", b'{"path": "\\ud800"}']:
            status, _, _ = self.call(
                "POST", "/api/url", {**self.server.auth(), "Content-Type": "application/json"}, raw_body=body
            )
            self.assertEqual(status, 404, body)

    def test_a_japanese_name(self):
        info = self.server.link("日本語の面接.mp3")
        status, _, body = self.call("GET", info["url"])
        self.assertEqual((status, body), (200, mp3_frame(0x11) * 3))


class Streaming(Case):
    def get(self, rel, headers=None, method="GET"):
        return self.call(method, self.server.link(rel)["url"], headers)

    def test_whole_file(self):
        status, h, body = self.get("a.wav")
        self.assertEqual((status, body, h["content-type"]), (200, BYTES, "audio/wav"))
        self.assertEqual((h["accept-ranges"], h["cache-control"]), ("bytes", "private, no-store"))

    def test_a_range(self):
        status, h, body = self.get("a.wav", {"Range": "bytes=100-199"})
        self.assertEqual((status, body, h["content-range"]), (206, BYTES[100:200], f"bytes 100-199/{len(BYTES)}"))

    def test_an_open_ended_range(self):
        status, _, body = self.get("a.wav", {"Range": "bytes=10000-"})
        self.assertEqual((status, body), (206, BYTES[10000:]))

    def test_a_suffix_range(self):
        status, _, body = self.get("a.wav", {"Range": "bytes=-40"})
        self.assertEqual((status, body), (206, BYTES[-40:]))

    def test_a_range_past_the_end_is_clamped(self):
        status, h, body = self.get("a.wav", {"Range": "bytes=10200-99999"})
        self.assertEqual((status, body, h["content-range"]), (206, BYTES[10200:], f"bytes 10200-10239/{len(BYTES)}"))

    def test_an_unsatisfiable_range(self):
        status, h, _ = self.get("a.wav", {"Range": "bytes=999999-"})
        self.assertEqual((status, h["content-range"]), (416, f"bytes */{len(BYTES)}"))

    def test_head_has_the_size_and_no_body(self):
        status, h, body = self.get("a.wav", method="HEAD")
        self.assertEqual((status, h["content-length"], body), (200, str(len(BYTES)), b""))

    def test_a_file_in_a_subfolder(self):
        status, _, body = self.get("2026/spring/b.m4a")
        self.assertEqual((status, body), (200, BYTES[:1000]))

    def test_keep_alive_serves_several_requests_on_one_connection(self):
        url = self.server.link("a.wav")["url"]
        c = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            for r in ["bytes=0-9", "bytes=10-19", "bytes=-5"]:
                c.request("GET", url, headers={"Range": r})
                resp = c.getresponse()
                self.assertEqual(resp.status, 206)
                resp.read()
        finally:
            c.close()

    def test_a_big_file_arrives_whole(self):
        big = os.urandom(3 * 1024 * 1024 + 7)
        (self.root / "big.wav").write_bytes(big)
        try:
            _, _, body = self.get("big.wav")
            self.assertEqual(body, big)
        finally:
            (self.root / "big.wav").unlink()


class Mp3Blanking(Case):
    INFO = mp3_frame(0, b"Info") + mp3_frame(0x55) * 40
    XING = mp3_frame(0, b"Xing") + mp3_frame(0x55) * 40
    ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10

    @classmethod
    def prepare_folder(cls):
        (cls.root / "cbr.mp3").write_bytes(cls.INFO)
        (cls.root / "vbr.mp3").write_bytes(cls.XING)
        (cls.root / "id3.mp3").write_bytes(cls.ID3 + cls.INFO)

    EXTRA = ("--no-convert",)

    def expected(self, data, at):
        b = bytearray(data)
        b[at + 36 : at + 417] = bytes(417 - 36)
        return bytes(b)

    def test_the_tag_of_a_constant_bit_rate_file_is_sent_as_zeros_at_the_same_length(self):
        info = self.server.link("cbr.mp3")
        _, _, body = self.call("GET", info["url"])
        self.assertEqual((len(body), info["size"]), (len(self.INFO), len(self.INFO)))
        self.assertEqual(body, self.expected(self.INFO, 0))

    def test_ranges_inside_across_and_after_the_tag(self):
        url = self.server.link("cbr.mp3")["url"]
        want = self.expected(self.INFO, 0)
        for first, last in [(40, 99), (0, 1000), (30, 40), (416, 420), (500, 9000)]:
            _, _, part = self.call("GET", url, {"Range": f"bytes={first}-{last}"})
            self.assertEqual(part, want[first : last + 1], (first, last))

    def test_the_tag_behind_an_id3_tag(self):
        _, _, body = self.call("GET", self.server.link("id3.mp3")["url"])
        self.assertEqual(body, self.expected(self.ID3 + self.INFO, 20))

    def test_without_conversion_a_variable_bit_rate_file_is_sent_as_it_is(self):
        info = self.server.link("vbr.mp3")
        _, _, body = self.call("GET", info["url"])
        self.assertEqual((body, info["size"]), (self.XING, len(self.XING)))

    def test_without_conversion_a_wma_file_is_sent_as_it_is(self):
        _, h, body = self.call("GET", self.server.link("old-dictaphone.WMA")["url"])
        self.assertEqual((h["content-type"], body), ("audio/x-ms-wma", BYTES[:500]))


def make(args, out):
    done = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(out)], capture_output=True)
    return done.returncode == 0


def click_wav(path, seconds=6, at=2.0, rate=44100):
    """Silence with one loud click at `at` seconds: where it lands after conversion shows any shift."""
    frames = bytearray(int(seconds * rate) * 2)
    start = int(at * rate)
    for i in range(start, start + 40):
        struct.pack_into("<h", frames, i * 2, 30000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def decode(path):
    """Mono 16-bit samples at 44.1 kHz, decoded by ffmpeg (a second opinion on what was made)."""
    pcm = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "44100", "-"], capture_output=True
    ).stdout
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def first_loud(samples, level=8000):
    return next((i for i, v in enumerate(samples) if abs(v) > level), None)


@unittest.skipUnless(os.name == "nt", "conversion with the system's codecs is Windows only for now")
@unittest.skipUnless(FFMPEG, "ffmpeg is needed to make the fixtures")
class Conversion(Case):
    EXTRA = ()

    @classmethod
    def prepare_folder(cls):
        tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=12"]
        assert make(tone + ["-c:a", "libmp3lame", "-q:a", "4", "-write_xing", "1"], cls.root / "real-vbr.mp3")
        assert make(tone + ["-c:a", "libmp3lame", "-b:a", "128k"], cls.root / "real-cbr.mp3")
        assert make(tone + ["-c:a", "wmav2"], cls.root / "dictaphone.wma")
        assert make(tone + ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb", "-b:a", "12.2k"], cls.root / "phone.amr")
        click_wav(Path(cls.tmp.name, "click.wav"))
        assert make(["-i", str(Path(cls.tmp.name, "click.wav")), "-c:a", "wmav2"], cls.root / "click.wma")
        (cls.root / "damaged.wma").write_bytes(b"0&\xb2u" + os.urandom(5000))

    def converted(self, rel):
        info = self.server.link(rel)
        status, h, body = self.call("GET", info["url"])
        self.assertEqual((status, h["content-type"], len(body)), (200, "audio/mpeg", info["size"]))
        return info, h, body

    def test_a_variable_bit_rate_file_is_converted_once_and_sent_without_a_table(self):
        original = (self.root / "real-vbr.mp3").read_bytes()
        info, _, body = self.converted("real-vbr.mp3")
        self.assertNotEqual(body, original)
        self.assertEqual(body[:2], b"\xff\xfb")  # a frame first: no ID3 tag
        self.assertNotIn(b"Xing", body[:2048])
        self.assertNotIn(b"Info", body[:2048])
        _, h, part = self.call("GET", info["url"], {"Range": "bytes=100-199"})
        self.assertEqual((part, h["content-range"]), (body[100:200], f"bytes 100-199/{len(body)}"))
        # The second time nothing is converted again.
        t = time.time()
        again = self.server.link("real-vbr.mp3")
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(again["size"], info["size"])
        self.assertEqual(sum(1 for line in self.server.log if line == "converting real-vbr.mp3"), 1)

    def test_copies_stay_beside_the_recordings_and_are_never_listed_or_served(self):
        self.converted("dictaphone.wma")
        cache = self.root / ".clew-converted"
        copies = list(cache.glob("*.mp3"))
        self.assertTrue(copies)
        self.assertTrue((cache / "README.txt").is_file())
        # Nothing about the recording's name in the copy's name.
        self.assertFalse([c for c in copies if "dictaphone" in c.name])
        _, _, listing = self.call("GET", "/api/files", self.server.auth())
        self.assertFalse([f for f in json.loads(listing)["files"] if ".clew-converted" in f["path"]])
        for rel in [".clew-converted/" + copies[0].name, "interviews/.clew-converted/" + copies[0].name]:
            self.assertEqual(self.call("POST", "/api/url", self.server.auth(), {"path": rel})[0], 404)

    def test_a_constant_bit_rate_file_is_not_converted(self):
        info = self.server.link("real-cbr.mp3")
        self.assertEqual(info["size"], (self.root / "real-cbr.mp3").stat().st_size)

    def test_wma_and_amr_become_an_mp3_of_the_same_length(self):
        for rel in ["dictaphone.wma", "phone.amr"]:
            _, _, body = self.converted(rel)
            out = Path(self.tmp.name, rel + ".mp3")
            out.write_bytes(body)
            seconds = len(decode(out)) / 44100
            self.assertGreaterEqual(seconds, 11.95, rel)  # nothing lost at the end
            self.assertLess(seconds, 12.4, rel)
        _, _, listing = self.call("GET", "/api/files", self.server.auth())
        entry = next(f for f in json.loads(listing)["files"] if f["path"] == "interviews/dictaphone.wma")
        self.assertEqual(entry["mime"], "audio/x-ms-wma")  # the listing still tells what the file is

    def test_a_moment_stays_where_it_was(self):
        _, _, body = self.converted("click.wma")
        out = Path(self.tmp.name, "click.mp3")
        out.write_bytes(body)
        original = first_loud(decode(self.root / "click.wma"))
        after = first_loud(decode(out))
        self.assertIsNotNone(after)
        # A transcript's timestamps keep pointing at the same words: within 60 ms.
        self.assertLess(abs(after - original) / 44100, 0.060, (original, after))

    def test_a_damaged_file_is_sent_as_it_is_and_the_server_keeps_going(self):
        info = self.server.link("damaged.wma")
        self.assertEqual(info["size"], (self.root / "damaged.wma").stat().st_size)
        self.assertTrue(any(line.startswith("could not convert damaged.wma") for line in self.server.log))
        self.assertEqual(self.call("GET", "/api/ping")[0], 200)

    def test_the_listing_prepares_wma_and_amr_in_the_background(self):
        fresh = Path(self.tmp.name, "fresh")
        fresh.mkdir()
        shutil.copy(self.root / "phone.amr", fresh / "later.amr")
        other = Server(fresh, Path(self.tmp.name, "home2"))
        try:
            other.call("GET", "/api/files", other.auth())
            deadline = time.time() + 30
            while time.time() < deadline and not list((fresh / ".clew-converted").glob("*.mp3")):
                time.sleep(0.2)
            self.assertEqual(len(list((fresh / ".clew-converted").glob("*.mp3"))), 1)
            t = time.time()
            other.link("later.amr")
            self.assertLess(time.time() - t, 1.0)
        finally:
            other.stop()

    def test_the_file_being_opened_goes_before_preparing_ahead(self):
        folder = Path(self.tmp.name, "priority")
        folder.mkdir()
        assert make(["-f", "lavfi", "-i", "sine=frequency=300:duration=2400", "-c:a", "wmav2"], folder / "a-long.wma")
        assert make(["-f", "lavfi", "-i", "sine=frequency=500:duration=600", "-c:a", "wmav2"], folder / "b-open.wma")
        cache = folder / ".clew-converted"
        other = Server(folder, Path(self.tmp.name, "home-priority"))
        try:
            other.call("GET", "/api/files", other.auth())  # starts preparing a-long.wma
            deadline = time.time() + 20
            while time.time() < deadline and "converting a-long.wma" not in other.log:
                time.sleep(0.05)
            self.assertIn("converting a-long.wma", other.log)
            counts = []
            done = threading.Event()

            def watch():
                while not done.is_set():
                    counts.append(converter_count())

            watcher = threading.Thread(target=watch)
            watcher.start()
            try:
                info = other.link("b-open.wma")
            finally:
                done.set()
                watcher.join()
            # Only the opened file was converting: preparing ahead stopped and waited.
            self.assertTrue(counts and max(counts) <= 1, counts)
            self.assertIn("preparing a-long.wma paused for the file being opened", other.log)
            self.assertEqual(len(list(cache.glob("*.mp3"))), 1)  # the opened one only
            self.assertGreater(info["size"], 0)
            # Then preparing resumes and finishes.
            deadline = time.time() + 120
            while time.time() < deadline and len(list(cache.glob("*.mp3"))) < 2:
                time.sleep(0.25)
            self.assertEqual(len(list(cache.glob("*.mp3"))), 2)
            self.assertFalse([line for line in other.log if line.startswith("could not convert")])
        finally:
            other.stop()

    def test_the_converter_runs_sandboxed_at_low_integrity(self):
        long = Path(self.tmp.name, "long")
        long.mkdir()
        assert make(["-f", "lavfi", "-i", "sine=frequency=300:duration=1500", "-c:a", "wmav2"], long / "long.wma")
        other = Server(long, Path(self.tmp.name, "home3"))
        try:
            t = threading.Thread(target=lambda: other.call("POST", "/api/url", other.auth(), {"path": "long.wma"}))
            t.start()
            pid = None
            for _ in range(60):
                time.sleep(0.25)
                pid = converter_pid()
                if pid:
                    break
            self.assertTrue(pid, "no converter process seen")
            self.assertEqual(integrity_of(pid), 0x1000)  # low integrity
            t.join(timeout=120)
        finally:
            other.stop()


def converter_pid():
    """The process id of a running converter child, found with Windows' own process list."""
    out = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='clew-audio-server.exe'\" | "
            "Where-Object { $_.CommandLine -like '*--convert-child*' } | ForEach-Object { $_.ProcessId }",
        ],
        capture_output=True, text=True,
    ).stdout.split()
    return int(out[0]) if out else None


def converter_count():
    """How many converter children run now (Windows' process list, via the Tool Help API)."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    by_pid, ok = {}, k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        by_pid[entry.th32ProcessID] = (entry.szExeFile.lower(), entry.th32ParentProcessID)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    # A converter is the program started by the program.
    return sum(
        1 for name, parent in by_pid.values()
        if name == "clew-audio-server.exe" and by_pid.get(parent, ("",))[0] == "clew-audio-server.exe"
    )


def integrity_of(pid):
    """The mandatory integrity level of a process (0x1000 low, 0x2000 medium), via the Windows API."""
    import ctypes
    from ctypes import wintypes

    k32, adv = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("advapi32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    token = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 25, buf, size, ctypes.byref(size)):
            return None
        sid = ctypes.c_void_p.from_buffer(buf).value
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        count = adv.GetSidSubAuthorityCount(ctypes.c_void_p(sid)).contents.value
        return adv.GetSidSubAuthority(ctypes.c_void_p(sid), count - 1).contents.value
    finally:
        if token:
            k32.CloseHandle(token)
        k32.CloseHandle(h)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class FfmpegFallback(Case):
    EXTRA = ("--no-system-codecs", "--ffmpeg", FFMPEG or "")

    @classmethod
    def prepare_folder(cls):
        assert make(["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "wmav2"], cls.root / "via-ffmpeg.wma")

    def test_a_chosen_ffmpeg_converts_when_the_system_codecs_do_not(self):
        info = self.server.link("via-ffmpeg.wma")
        _, h, body = self.call("GET", info["url"])
        self.assertEqual((h["content-type"], len(body)), ("audio/mpeg", info["size"]))
        self.assertIn(body[:2], (b"\xff\xfb", b"\xff\xf3"))


class Browser(Case):
    def test_hosts_other_than_loopback_are_refused(self):
        # A page on another name pointing at 127.0.0.1 (DNS rebinding) arrives with its own Host.
        self.assertEqual(self.call("GET", "/api/ping", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/ping", {"Host": "127.0.0.1.evil.example"})[0], 403)

    def test_two_host_headers_are_refused(self):
        got = self.server.raw(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: evil.example\r\n\r\n")
        self.assertTrue(got.startswith(b"HTTP/1.1 403"), got[:40])

    def test_a_foreign_origin_is_refused(self):
        self.assertEqual(self.call("GET", "/api/ping", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/ping", {"Origin": "http://localhost.evil.example"})[0], 403)

    def test_loopback_and_configured_origins_get_cors(self):
        for origin in ["http://localhost:5173", "http://127.0.0.1:4173", "https://clew.example"]:
            status, h, _ = self.call("GET", "/api/ping", {"Origin": origin})
            self.assertEqual(status, 200, origin)
            self.assertEqual(h["access-control-allow-origin"], origin)
            self.assertIn("Content-Range", h["access-control-expose-headers"])

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
        self.assertEqual(self.call("GET", self.server.link("a.wav")["url"])[0], 200)


class Limits(Case):
    def test_a_huge_request_head_is_refused(self):
        got = self.server.raw(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n")
        self.assertTrue(got.startswith(b"HTTP/1.1 431"), got[:40])

    def test_a_chunked_body_is_refused(self):
        got = self.server.raw(
            b"POST /api/url HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
        )
        self.assertTrue(got.startswith(b"HTTP/1.1 501"), got[:40])

    def test_a_big_body_is_refused(self):
        status, _, _ = self.call("POST", "/api/url", self.server.auth(), raw_body=b"x" * 9000)
        self.assertEqual(status, 413)

    def test_garbage_is_refused(self):
        self.assertTrue(self.server.raw(b"HELLO\r\n\r\n").startswith(b"HTTP/1.1 400"))
        self.assertTrue(self.server.raw(b"GET /api/ping HTTP/2.0\r\n\r\n").startswith(b"HTTP/1.1 505"))
        self.assertTrue(self.server.raw(b"BREW /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").startswith(b"HTTP/1.1 501"))

    def test_http_1_0_works_and_closes(self):
        got = self.server.raw(b"GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:40])
        self.assertIn(b"Connection: close", got)

    def test_the_server_is_on_loopback_only(self):
        addresses = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        for a in addresses - {"127.0.0.1"}:
            with self.assertRaises(OSError):
                socket.create_connection((a, self.server.port), timeout=1).close()

    def test_the_port_cannot_be_shared(self):
        if os.name != "nt":
            self.skipTest("Windows behaviour")
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with self.assertRaises(OSError):
                s.bind(("127.0.0.1", self.server.port))
        finally:
            s.close()


@unittest.skipUnless(EXE.is_file(), "build the program first")
class CommandLine(unittest.TestCase):
    def run_exe(self, *args):
        return subprocess.run([str(EXE), *args], capture_output=True, text=True, timeout=20)

    def test_version_and_help(self):
        self.assertIn("clew-audio-server", self.run_exe("--version").stdout)
        self.assertIn("--headless", self.run_exe("--help").stdout)

    def test_a_missing_folder_is_said_plainly(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_exe("--headless", "--folder", str(Path(d, "nope")), "--config", str(Path(d, "s.json")))
            self.assertEqual(r.returncode, 2)
            self.assertIn("Not a folder", r.stdout)

    def test_a_taken_port_is_said_plainly(self):
        with tempfile.TemporaryDirectory() as d:
            holder = socket.socket()
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            try:
                port = holder.getsockname()[1]
                r = self.run_exe("--headless", "--folder", d, "--port", str(port), "--config", str(Path(d, "s.json")))
                self.assertEqual(r.returncode, 2)
                self.assertIn(f"Port {port} is not available", r.stdout)
            finally:
                holder.close()

    def test_a_bad_origin_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_exe("--headless", "--folder", d, "--allow-origin", "https://*.example")
            self.assertEqual(r.returncode, 2)


# --- Dictation (docs/28): the speech server as a child of this one -------------------------------

STT_EXE = Path(
    os.environ.get("CLEW_STT_SERVER_EXE")
    or HERE.parents[2] / "stt-server" / "dist" / "clew-stt-server" / "clew-stt-server.exe"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def stt_call(port, path, key=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if host:
            headers["Host"] = host
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@unittest.skipUnless(EXE.is_file() and STT_EXE.is_file(), f"build both programs first: {STT_EXE}")
class Dictation(unittest.TestCase):
    """The real speech server with its fake engine (no model), started by the real tray program."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name, "home")
        self.folder = Path(self.tmp.name, "audio")
        self.folder.mkdir()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        for _ in range(20):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.25)

    def start(self, *extra):
        port = free_port()
        s = Server(self.folder, self.home, "--dictation", "--stt-fake-engine", "--stt-port", str(port),
                   "--stt-program", str(STT_EXE), *extra)
        self.servers.append(s)
        k = re.search(r"Dictation key[^\n]*\n\s+(\S+)", s.banner)
        return s, port, (k.group(1) if k else None)

    def test_it_starts_with_its_key_and_answers_only_to_it(self):
        server, port, key = self.start()
        self.assertIn(f"Dictation  ->  http://127.0.0.1:{port}", server.banner)
        self.assertRegex(key or "", r"^stt\.local\.[A-Za-z0-9_-]{32}$")
        self.assertTrue(wait_for(lambda: any("dictation ready" in l for l in server.log)), server.log)
        status, raw = stt_call(port, "/api/me", key)
        self.assertEqual(status, 200, raw)
        me = json.loads(raw)
        self.assertEqual(me["key"], "local")
        # The audio server's folder is handed over, for deferred transcription (docs/29).
        self.assertTrue(me["transcribe"] and me["folder"])
        self.assertEqual(stt_call(port, "/api/me", "stt.local.wrong")[0], 401)
        self.assertEqual(stt_call(port, "/api/me")[0], 401)
        # A page reaching it through a name that resolves to 127.0.0.1 is refused.
        self.assertEqual(stt_call(port, "/api/ping", host="rebound.example")[0], 403)
        # The key is kept sealed, not in clear, and never on the child's command line.
        settings = Path(self.home, "settings.json").read_text(encoding="utf-8")
        self.assertIn("sttSecret", settings)
        self.assertNotIn(key, settings)
        self.assertFalse(any(key in line for line in server.log))

    def test_the_key_is_kept_across_runs(self):
        first, _, key = self.start()
        first.stop()
        self.servers.remove(first)
        again, port, shown = self.start()
        self.assertIsNone(shown)  # not made now, so not shown unasked
        self.assertTrue(wait_for(lambda: any("dictation ready" in l for l in again.log)), again.log)
        self.assertEqual(stt_call(port, "/api/me", key)[0], 200)

    def test_it_ends_with_the_server(self):
        server, port, _ = self.start()
        self.assertTrue(wait_for(lambda: listening(port)))
        server.stop()
        self.servers.remove(server)
        self.assertTrue(wait_for(lambda: not listening(port), 10), "the speech server outlived its parent")

    @unittest.skipUnless(os.name == "nt", "Windows' process list")
    def test_killed_while_starting_it_leaves_nothing_behind(self):
        # A child created outside its job and put in a moment later stayed suspended forever when
        # the server died in that moment. Now it is born in the job: kill the server at once,
        # many times, at different moments, and look for any child left.
        port = free_port()
        for i in range(16):
            proc = subprocess.Popen(
                [str(EXE), "--headless", "--folder", str(self.folder), "--port", "0",
                 "--config", str(Path(self.home, "settings.json")), "--dictation", "--stt-fake-engine",
                 "--stt-port", str(port), "--stt-program", str(STT_EXE)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            # The banner's dictation line comes as the child is being started: kill around then.
            for raw in proc.stdout:
                if b"Dictation  ->" in raw:
                    break
            time.sleep(0.002 * (i % 8))
            proc.kill()
            proc.wait()
            proc.stdout.close()
        time.sleep(2)
        left = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='clew-stt-server.exe'\" | "
             "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"],
            capture_output=True, text=True, timeout=60).stdout
        mine = [line for line in left.splitlines() if f"--port {port}" in line]
        for line in mine:  # do not leave them for the next run either
            subprocess.run(["taskkill", "/F", "/PID", line.split()[0]], capture_output=True)
        self.assertEqual(mine, [])

    def test_a_taken_port_is_said(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            taken = holder.getsockname()[1]
            server = Server(self.folder, self.home, "--dictation", "--stt-fake-engine", "--stt-port", str(taken),
                            "--stt-program", str(STT_EXE))
            self.servers.append(server)
            self.assertTrue(
                wait_for(lambda: any(f"port {taken} is in use" in l for l in server.log)), server.log)
        finally:
            holder.close()

    def test_a_missing_model_is_said_before_anything_starts(self):
        port = free_port()
        server = Server(self.folder, self.home, "--dictation", "--stt-port", str(port), "--stt-program",
                        str(STT_EXE), "--stt-models-dir", str(Path(self.tmp.name, "no-models")))
        self.servers.append(server)
        time.sleep(2)
        self.assertFalse(listening(port))
        self.assertFalse(any("dictation starting" in l for l in server.log), server.log)

    def test_a_model_is_downloaded_whole(self):
        import http.server

        files = {"config.json": b"{}", "model.bin": os.urandom(300_000), "tokenizer.json": b"{}",
                 "vocabulary.txt": b"a"}
        repo = "Systran/faster-whisper-small"

        class Hub(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith(f"/api/models/{repo}"):
                    body = json.dumps({"siblings": [{"rfilename": n, "size": len(d)} for n, d in files.items()]})
                    body = body.encode()
                elif self.path.startswith(f"/{repo}/resolve/main/"):
                    body = files.get(self.path.rsplit("/", 1)[1])
                    if body is None:
                        self.send_error(404)
                        return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hub)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        models = Path(self.tmp.name, "models")
        env = dict(os.environ, ALEFA_STT_HUB=f"http://127.0.0.1:{hub.server_address[1]}")
        try:
            r = subprocess.run([str(EXE), "--headless", "--config", str(Path(self.home, "s.json")),
                                "--stt-program", str(STT_EXE), "--stt-models-dir", str(models),
                                "--stt-download", "small"], capture_output=True, text=True, timeout=60, env=env)
        finally:
            hub.shutdown()
            hub.server_close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Downloaded:", r.stdout)
        self.assertEqual(Path(models, "small", "model.bin").read_bytes(), files["model.bin"])
        self.assertFalse(Path(models, "small.part").exists())


if __name__ == "__main__":
    unittest.main()
