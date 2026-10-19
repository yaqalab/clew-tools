"""
Tests for what the clew_audio package adds to the Python audio server (docs/33): the keystore,
start at login, the update check, the service the window drives, dictation, and the window itself.
Real files, real HTTP, a real child process; no mocks.

    python -m unittest discover -s tools/audio-server -p "test_*.py" -v

Dictation's tests need fastapi, uvicorn and numpy (not a model: they use the fake engine); the
window's need Tkinter and a display (xvfb-run on a server). Each skips when its part is missing.
"""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import clew_audio_server as srv
from clew_audio import autostart, dictation, updates
from clew_audio.keystore import KeyStore, open_store
from clew_audio.service import Service

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="clew-extras-"))
        patcher = mock.patch.dict(os.environ, {"CLEW_KEYSTORE": "file", "HOME": str(self.tmp / "home"),
                                               "XDG_CONFIG_HOME": str(self.tmp / "home" / ".config")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def folder(self, name: str, files: dict[str, bytes]) -> Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        for rel, data in files.items():
            (root / rel).write_bytes(data)
        return root


class KeyStoreTest(Temp):
    def test_the_file_store_keeps_values_for_this_user_only(self):
        store = KeyStore(self.tmp / "cfg", "file")
        self.assertIsNone(store.get("secret"))
        self.assertTrue(store.set("secret", "abc_DEF-123"))
        self.assertEqual(KeyStore(self.tmp / "cfg", "file").get("secret"), "abc_DEF-123")
        if os.name == "posix":
            mode = stat.S_IMODE((self.tmp / "cfg" / "secrets.json").stat().st_mode)
            self.assertEqual(mode, 0o600)
        store.delete("secret")
        self.assertIsNone(store.get("secret"))

    def test_a_value_that_could_break_out_of_its_quotes_is_refused(self):
        with self.assertRaises(ValueError):
            KeyStore(self.tmp / "cfg", "file").set("secret", 'a" -w "b')

    @unittest.skipUnless(sys.platform.startswith("linux"), "secret-tool is Linux's")
    def test_the_keyring_gets_the_value_on_stdin_never_on_the_command_line(self):
        # A stand-in secret-tool that records its arguments and keeps values in a file.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        log, store_file = self.tmp / "argv.log", self.tmp / "ring.json"
        tool = bin_dir / "secret-tool"
        tool.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
open({str(log)!r}, "a").write(" ".join(args) + "\\n")
try: ring = json.load(open({str(store_file)!r}))
except Exception: ring = {{}}
account = args[args.index("account") + 1]
if args[0] == "store": ring[account] = sys.stdin.read()
elif args[0] == "lookup":
    if account not in ring: sys.exit(1)
    print(ring[account], end="")
elif args[0] == "clear": ring.pop(account, None)
json.dump(ring, open({str(store_file)!r}, "w"))
""")
        tool.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                                          "CLEW_KEYSTORE": "", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}):
            store = open_store(self.tmp / "cfg")
            self.assertEqual(store.backend, "secret-service")
            store.set("secret", "TOPsecret_value")
            self.assertEqual(store.get("secret"), "TOPsecret_value")
        self.assertNotIn("TOPsecret_value", log.read_text())
        self.assertFalse((self.tmp / "cfg" / "secrets.json").exists())

    @unittest.skipUnless(sys.platform == "darwin", "the Keychain is macOS's")
    def test_the_keychain_keeps_a_value_and_forgets_it(self):
        import pwd  # the real home: setUp points HOME at a temporary one, and the Keychain lives there

        with mock.patch.dict(os.environ, {"CLEW_KEYSTORE": "", "HOME": pwd.getpwuid(os.getuid()).pw_dir}):
            store = KeyStore(self.tmp / "cfg", "keychain")
            name = f"test-{os.getpid()}"
            self.addCleanup(store.delete, name)
            self.assertTrue(store.set(name, "Keychain_value-1"))
            self.assertEqual(store.get(name), "Keychain_value-1")
            self.assertTrue(store.set(name, "Keychain_value-2"))  # replaced, not added twice
            self.assertEqual(store.get(name), "Keychain_value-2")
            store.delete(name)
            self.assertIsNone(store.get(name))
        self.assertFalse((self.tmp / "cfg" / "secrets.json").exists())

    def test_a_keyring_that_refuses_falls_back_to_the_file(self):
        with mock.patch.dict(os.environ, {"CLEW_KEYSTORE": "secret-service", "PATH": str(self.tmp)}):
            store = open_store(self.tmp / "cfg")
        self.assertEqual(store.backend, "file")


class AutostartTest(Temp):
    def test_each_system_gets_its_own_entry_running_this_program(self):
        for platform, gui, expect in (
            ("darwin", False, "Library/LaunchAgents/net.alefa.clew-audio-server.plist"),
            ("linux", False, ".config/systemd/user/clew-audio-server.service"),
            ("linux", True, ".config/autostart/clew-audio-server.desktop"),
        ):
            with self.subTest(platform=platform, gui=gui), mock.patch.object(sys, "platform", platform):
                (path, text), = autostart.files(gui)
                self.assertTrue(path.as_posix().endswith(expect), path)
                self.assertTrue("clew_audio_server.py" in text or "/clew-audio-server" in text, text)
                self.assertEqual("--gui" in text, gui)

    def test_turning_it_on_and_off_writes_and_removes_the_entry(self):
        with mock.patch.object(sys, "platform", "linux"):
            written = autostart.enable(gui=True, activate=False)
            self.assertTrue(autostart.is_on())
            self.assertTrue(written[0].is_file())
            # Switching to no window replaces the entry, never leaves two.
            autostart.enable(gui=False, activate=False)
            self.assertFalse(written[0].exists())
            autostart.disable(activate=False)
            self.assertFalse(autostart.is_on())


class StandIn(http.server.BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self):
        if self.path == "/releases/latest/download/latest.json":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class UpdatesTest(Temp):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"CLEW_UPDATE_BASE": f"http://127.0.0.1:{self.server.server_address[1]}"})
        p.start()
        self.addCleanup(p.stop)

    def test_versions_compare_as_numbers(self):
        self.assertTrue(updates.newer("1.10.0", "1.9.9"))
        self.assertFalse(updates.newer("1.1.0", "1.1.0"))
        self.assertFalse(updates.newer("v2.0.0", "1.0.0"))

    def test_the_daily_check_tells_of_a_new_version_once(self):
        StandIn.body = json.dumps({"version": "99.0.0"}).encode()
        state = self.tmp / "update.json"
        self.assertEqual(updates.daily(state), "99.0.0")
        self.assertIsNone(updates.daily(state))  # not due again for a day
        s = updates.State.load(state)
        s.last_check = 0
        s.save(state)
        self.assertIsNone(updates.daily(state))  # due, but already told

    def test_turned_off_it_asks_nothing(self):
        state = self.tmp / "update.json"
        updates.State(check=False).save(state)
        StandIn.body = json.dumps({"version": "99.0.0"}).encode()
        self.assertIsNone(updates.daily(state))
        self.assertEqual(updates.State.load(state).last_check, 0)

    def test_an_unreadable_answer_is_an_error_not_a_version(self):
        StandIn.body = b'{"version": "latest"}'
        with self.assertRaises(ValueError):
            updates.fetch_latest()

    def test_only_a_loopback_stand_in_replaces_github(self):
        with mock.patch.dict(os.environ, {"CLEW_UPDATE_BASE": "https://evil.example"}):
            self.assertEqual(updates._base(), "https://github.com/yaqalab/clew-tools")


class ServiceTest(Temp):
    def make(self) -> Service:
        svc = Service(self.tmp / "cfg" / "config.json")
        self.addCleanup(svc.shutdown)
        return svc

    def test_a_first_start_makes_a_secret_that_can_be_shown_again_and_works(self):
        svc = self.make()
        secret = svc.secret()
        self.assertTrue(secret and secret == svc.made_secret)
        a = self.folder("study-a", {"p01.mp3": b"\xff\xfb" + bytes(500)})
        svc.config.data["port"] = free_port()
        self.assertEqual(svc.set_folders([{"path": str(a), "enabled": True}]), "")
        self.assertTrue(svc.running())
        status, body = get(svc.port, "/api/files", {"Authorization": f"Bearer {secret}"})
        self.assertEqual(status, 200)
        self.assertEqual([f["path"] for f in json.loads(body)["files"]], ["study-a/p01.mp3"])
        self.assertEqual(get(svc.port, "/api/files", {"Authorization": "Bearer wrong"})[0], 401)

    def test_clew_alefa_net_is_allowed_on_a_first_start_and_can_be_removed(self):
        svc = self.make()
        a = self.folder("a", {"x.mp3": bytes(10)})
        port = free_port()
        svc.config.folders = [{"path": str(a), "enabled": True}]
        svc.apply(port, 4, ["https://clew.alefa.net"], "")
        status, _ = get(port, "/api/ping", {"Origin": "https://clew.alefa.net"})
        self.assertEqual(status, 200)
        svc.apply(port, 4, [], "")
        self.assertEqual(get(port, "/api/ping", {"Origin": "https://clew.alefa.net"})[0], 403)

    def test_a_folder_turned_off_stops_being_listed_at_once(self):
        svc = self.make()
        a, b = self.folder("a", {"1.mp3": bytes(10)}), self.folder("b", {"2.mp3": bytes(10)})
        svc.config.data["port"] = free_port()
        svc.set_folders([{"path": str(a), "enabled": True}, {"path": str(b), "enabled": True}])
        auth = {"Authorization": f"Bearer {svc.secret()}"}
        self.assertEqual(len(json.loads(get(svc.port, "/api/files", auth)[1])["files"]), 2)
        svc.set_folders([{"path": str(a), "enabled": True}, {"path": str(b), "enabled": False}])
        self.assertEqual([f["path"] for f in json.loads(get(svc.port, "/api/files", auth)[1])["files"]], ["a/1.mp3"])

    def test_a_new_secret_replaces_the_old_one_at_once(self):
        svc = self.make()
        a = self.folder("a", {"1.mp3": bytes(10)})
        svc.config.data["port"] = free_port()
        svc.set_folders([{"path": str(a), "enabled": True}])
        old = svc.secret()
        new = svc.reset_secret()
        self.assertEqual(get(svc.port, "/api/files", {"Authorization": f"Bearer {old}"})[0], 401)
        self.assertEqual(get(svc.port, "/api/files", {"Authorization": f"Bearer {new}"})[0], 200)

    def test_a_port_in_use_is_said_in_words(self):
        svc = self.make()
        a = self.folder("a", {"1.mp3": bytes(10)})
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            svc.config.folders = [{"path": str(a), "enabled": True}]
            problem = svc.apply(port, 4, [], "")
        self.assertIn(f"port {port}", problem)
        self.assertFalse(svc.running())

    def test_the_console_server_shows_the_kept_secret_again(self):
        cfg = self.tmp / "cfg" / "config.json"
        svc = Service(cfg)
        svc.shutdown()
        out = self._run_cli(["--show-secret", "--config", str(cfg)])
        self.assertIn(f"Secret: {svc.secret()}", out)

    def _run_cli(self, args: list[str]) -> str:
        import subprocess

        r = subprocess.run([sys.executable, str(HERE / "clew_audio_server.py"), *args],
                           capture_output=True, text=True, env=os.environ, timeout=30)
        return r.stdout + r.stderr


@unittest.skipIf(dictation.available(), f"dictation: {dictation.available()}")
class DictationTest(Temp):
    def test_the_fake_engine_starts_answers_with_the_key_and_stops(self):
        svc = Service(self.tmp / "cfg" / "config.json")
        self.addCleanup(svc.shutdown)
        port = free_port()
        svc.set_stt(port=port)
        svc.start_dictation(fake=True)
        deadline = time.time() + 60
        while svc.dictation.state == "loading" and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(svc.dictation.state, "ready", svc.dictation.problem)
        status, body = get(port, "/api/me", {"Authorization": f"Bearer {svc.stt_key()}"})
        self.assertEqual(status, 200, body)
        self.assertEqual(get(port, "/api/me", {"Authorization": "Bearer stt.local.wrong"})[0], 401)
        svc.dictation.stop()
        self.assertEqual(svc.dictation.state, "off")

    def test_a_model_not_downloaded_is_said_before_anything_starts(self):
        d = dictation.Dictation()
        with mock.patch.object(dictation, "engine_installed", return_value=True):
            d.start(dictation.Config(model="small", models_dir=self.tmp / "none", key="stt.local.x"))
        self.assertEqual((d.state, d.problem), ("failed", "the model small is not downloaded yet"))

    def test_the_server_lines_mean_what_the_tray_reads(self):
        self.assertEqual(dictation.read_line("INFO:     Uvicorn running on http://127.0.0.1:47822", 47822), (True, ""))
        self.assertEqual(dictation.read_line("ERROR: [Errno 98] error while attempting to bind", 47822),
                         (False, "port 47822 is in use"))
        self.assertEqual(dictation.read_line("alefa_stt.engine.EngineError: no model", 1), (False, "no model"))


def tk_available() -> bool:
    if importlib.util.find_spec("tkinter") is None:
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        import tkinter

        tkinter.Tk().destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(tk_available(), "needs Tkinter and a display")
class WindowTest(Temp):
    def test_the_window_opens_on_every_tab_and_saves(self):
        import tkinter

        from clew_audio import gui

        a = self.folder("study-a", {"p01.mp3": bytes(10)})
        cfg = self.tmp / "cfg" / "config.json"
        config = srv.Config.load(cfg)
        config.folders = [{"path": str(a), "enabled": True}]
        config.data["port"] = free_port()
        config.save()
        root = tkinter.Tk()
        w = gui.open_window(cfg, root)
        self.addCleanup(lambda: w.svc.shutdown())
        for tab in (w.audio, w.stt, w.upd):
            w.tabs.select(tab)
            root.update()
        self.assertTrue(w.svc.running())
        self.assertIn("Running", w.status.cget("text"))
        self.assertEqual(w.folders.item("0")["values"][1], "study-a")
        # The secret shows, masked until asked.
        self.assertEqual(w.secret.get(), w.svc.secret())
        self.assertEqual(w.secret.cget("show"), gui.MASK)
        w.toggle_secret()
        self.assertEqual(w.secret.cget("show"), "")
        # A folder turned off from the window.
        w.folders.selection_set("0")
        w.toggle_folder()
        self.assertFalse(w.svc.config.folders[0]["enabled"])
        # Saving a new port restarts the server there.
        new_port = free_port()
        w.port.delete(0, "end")
        w.port.insert(0, str(new_port))
        w.toggle_folder()
        w.save()
        root.update()
        self.assertEqual(w.svc.port, new_port)
        self.assertEqual(get(new_port, "/api/ping")[0], 200)
        # A bad origin is refused in words, nothing saved.
        w.origins.delete("1.0", "end")
        w.origins.insert("1.0", "not an address")
        w.save()
        self.assertIn("not an address", w.message.cget("text"))
        w.close()


if __name__ == "__main__":
    unittest.main()
