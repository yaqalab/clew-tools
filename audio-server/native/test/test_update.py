"""
The updater's Windows half (src/win/update_win.cpp) against a stand-in for GitHub: the release
description, the download, and every check before an installer may run (docs/32 section 3). The
real code, in build/update_check.exe, signed here with the YaqaLab certificate as a release is.

    python -m unittest discover -s tools/audio-server/native/test -p test_update.py -v

Skipped without that certificate in this user's store (make-cert.ps1) or without signtool. The
"installers" served are copies of update_check.exe, signed or not: nothing is installed or run.
"""

import glob
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE.parent / "build" / "update_check.exe"
UPDATES = Path(os.environ.get("LOCALAPPDATA", "")) / "clew-audio-server" / "updates"


def signtool():
    found = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    return found[-1] if found else None


def has_certificate():
    script = (
        "if (Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert | Where-Object { $_.Subject -eq 'CN=YaqaLab' "
        "-and $_.HasPrivateKey }) { exit 0 } else { exit 1 }"
    )
    return subprocess.run(["powershell", "-NoProfile", "-Command", script]).returncode == 0


SIGNTOOL = signtool() if os.name == "nt" else None


def sign(path):
    subprocess.run([SIGNTOOL, "sign", "/fd", "SHA256", "/n", "YaqaLab", "/s", "My", "/q", str(path)], check=True)


class StandIn(http.server.BaseHTTPRequestHandler):
    """Answers like github.com/yaqalab/clew-tools: latest.json through a redirect, as GitHub does."""

    files = {}

    def do_GET(self):
        if self.path == "/releases/latest/download/latest.json":
            self.send_response(302)
            self.send_header("Location", "/assets/latest.json")
            self.end_headers()
            return
        body = self.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.name == "nt" and CHECK.exists() and SIGNTOOL and has_certificate(),
                     "needs Windows, build/update_check.exe, signtool and the YaqaLab certificate")
class UpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="clew-update-"))
        cls.exe = cls.tmp / "update_check.exe"
        shutil.copy(CHECK, cls.exe)
        sign(cls.exe)
        cls.signed = cls.exe.read_bytes()
        cls.unsigned = CHECK.read_bytes()
        tampered = bytearray(cls.signed)
        tampered[len(tampered) // 3] ^= 0xFF  # inside the code, well before the signature
        cls.tampered = bytes(tampered)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for f in UPDATES.glob("clew-audio-server-setup-9.9.*"):
            f.unlink()

    def publish(self, body, version="9.9.0", **override):
        name = f"clew-audio-server-setup-{version}.exe"
        manifest = {"version": version, "installer": name, "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body)}
        manifest.update(override)
        StandIn.files = {
            "/assets/latest.json": json.dumps(manifest).encode(),
            f"/releases/download/v{version}/{name}": body,
        }

    def run_check(self, *args, base=None):
        env = dict(os.environ, CLEW_UPDATE_BASE=base or self.base)
        out = subprocess.run([str(self.exe), *args], env=env, capture_output=True, text=True, timeout=60)
        return out.stdout.strip()

    def test_reads_the_latest_version_through_a_redirect(self):
        self.publish(self.signed)
        self.assertEqual(self.run_check(), "latest 9.9.0")

    def test_a_signed_installer_passes_every_check(self):
        self.publish(self.signed)
        line = self.run_check("--download")
        self.assertTrue(line.startswith("ok "), line)
        self.assertEqual(Path(line[3:]).read_bytes(), self.signed)

    def test_a_changed_file_is_refused_even_with_its_own_fingerprint(self):
        self.publish(self.tampered)
        self.assertEqual(self.run_check("--download"), "refused the installer's signature is missing or broken")
        self.assertFalse(list(UPDATES.glob("clew-audio-server-setup-9.9.0.exe*")))

    def test_an_unsigned_file_is_refused(self):
        self.publish(self.unsigned)
        self.assertEqual(self.run_check("--download"), "refused the installer's signature is missing or broken")

    def test_a_fingerprint_that_differs_is_refused(self):
        self.publish(self.signed, sha256="0" * 64)
        self.assertEqual(self.run_check("--download"),
                         "refused the installer's fingerprint does not match the release")

    def test_a_file_larger_than_announced_is_stopped(self):
        self.publish(self.signed, size=1000)
        self.assertEqual(self.run_check("--download"), "refused the installer is larger than the release says")

    def test_an_installer_not_named_after_the_version_is_ignored(self):
        self.publish(self.signed, installer="other.exe")
        self.assertEqual(self.run_check(), "refused the release's description cannot be read")

    def test_nothing_answering_is_said_calmly(self):
        self.assertEqual(self.run_check(base="http://127.0.0.1:9"), "refused no connection to github.com")


if __name__ == "__main__":
    unittest.main()
