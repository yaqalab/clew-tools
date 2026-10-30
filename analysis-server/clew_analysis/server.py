"""The analysis server's HTTP side: loopback only, one key, the same guards as the audio server
(docs/22 section 3): a loopback Host, an allowed Origin, CORS for those origins only, no-store,
logs with method, path and status and nothing else (never a body: it holds answers).

    GET  /api/ping       no key      {server, api, version, backend}
    GET  /api/backends   key         {selected, backends: [{id, name, version, ready, why?}]}
    GET  /api/commands   key         {backend, commands: [{id, version, group, title, needs, available, user}]}
    POST /api/run        key         Clew's neutral command with the data -> the neutral result
"""

from __future__ import annotations

import hmac
import json
import re
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import API_VERSION, APP_NAME, __version__
from .backends import (
    BackendError,
    Found,
    detect_pspp,
    detect_python,
    detect_r,
    detect_spss,
    run_python,
    run_r,
    run_sps,
)
from .catalog import Command, RequestError, check_request, load_shipped, load_user

LOOPBACK_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
LOOPBACK_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE)
MAX_BODY = 50 * 1024 * 1024
BACKENDS = ("r", "python", "pspp", "spss")


class State:
    def __init__(
        self,
        key: str,
        backend: str = "r",
        origins: list[str] | None = None,
        rscript: str | None = None,
        scripts: Path | None = None,
        timeout: float = 120.0,
        python: str | None = None,
        pspp: str | None = None,
        spss: str | None = None,
    ) -> None:
        self.key = key
        self.backend = backend
        self.origins = set(origins or [])
        self.rscript = rscript
        self.python = python
        self.pspp = pspp
        self.spss = spss
        self.scripts = scripts
        self.timeout = timeout
        self.failures = 0
        self.lock = threading.Lock()
        self.busy = threading.Lock()
        self._found: dict[str, Found] = {}
        self.log: Callable[[str], None] | None = None

    def origin_ok(self, origin: str) -> bool:
        return bool(LOOPBACK_ORIGIN.match(origin)) or origin in self.origins

    def key_ok(self, header: str | None) -> bool:
        ok = bool(header and header.startswith("Bearer ")) and hmac.compare_digest(
            header[7:].strip().encode("utf-8"), self.key.encode("utf-8")
        )
        with self.lock:
            self.failures = 0 if ok else self.failures + 1
            slow = self.failures >= 5
        if not ok and slow:
            time.sleep(1.0)
        return ok

    def found(self) -> dict[str, Found]:
        """What each backend is on this computer; looked for once, then kept."""
        with self.lock:
            if not self._found:
                self._found = {
                    "r": detect_r(self.rscript),
                    "python": detect_python(self.python),
                    "pspp": detect_pspp(self.pspp),
                    "spss": detect_spss(self.spss),
                }
            return self._found

    def commands(self) -> dict[str, Command]:
        cmds = load_shipped()
        cmds.update(load_user(self.scripts))
        return cmds


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClewAnalysisServer/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            line = f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}"
            if state.log is not None:
                state.log(line)
            else:
                sys.stderr.write(line + "\n")

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin and state.origin_ok(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _json(self, status: int, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _refuse(self, status: int, why: str) -> None:
            self._json(status, {"error": why})

        def _guard(self) -> bool:
            if not LOOPBACK_HOST.match(self.headers.get("Host", "")):
                self._refuse(HTTPStatus.FORBIDDEN, "host")
                return False
            origin = self.headers.get("Origin")
            if origin and not state.origin_ok(origin):
                self._refuse(HTTPStatus.FORBIDDEN, "origin")
                return False
            return True

        def _auth(self) -> bool:
            if state.key_ok(self.headers.get("Authorization")):
                return True
            self._refuse(HTTPStatus.UNAUTHORIZED, "key")
            return False

        def _body(self) -> dict[str, Any] | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                return None
            try:
                v = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return None
            return v if isinstance(v, dict) else None

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._guard():
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path == "/api/ping":
                self._json(HTTPStatus.OK, {
                    "server": APP_NAME, "api": API_VERSION, "version": __version__,
                    "backend": state.backend,
                })
            elif path == "/api/backends":
                if self._auth():
                    found = state.found()
                    self._json(HTTPStatus.OK, {
                        "selected": state.backend,
                        "backends": [found[b].describe() for b in BACKENDS],
                    })
            elif path == "/api/commands":
                if self._auth():
                    self._json(HTTPStatus.OK, {
                        "backend": state.backend,
                        "commands": [c.describe(state.backend) for c in state.commands().values()],
                    })
            else:
                self._refuse(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            body = self._body()
            if urlsplit(self.path).path != "/api/run":
                self._refuse(HTTPStatus.NOT_FOUND, "not found")
                return
            if not self._auth():
                return
            if body is None:
                self._refuse(HTTPStatus.BAD_REQUEST, "the request could not be read")
                return
            self._run(body)

        def _run(self, body: dict[str, Any]) -> None:
            command = state.commands().get(str(body.get("command", "")))
            if command is None:
                self._refuse(HTTPStatus.UNPROCESSABLE_ENTITY, "unknown command")
                return
            try:
                req = check_request(command, body)
            except RequestError as e:
                self._refuse(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
                return
            found = state.found().get(state.backend)
            if found is None or not found.ready or found.path is None:
                why = found.why if found else "unknown backend"
                self._refuse(HTTPStatus.SERVICE_UNAVAILABLE, f"{state.backend}: {why}")
                return
            # One run at a time: a second is told so, not queued behind a long model.
            if not state.busy.acquire(blocking=False):
                self._refuse(HTTPStatus.CONFLICT, "busy")
                return
            try:
                if state.backend == "r":
                    result = run_r(found.path, command, req, state.timeout)
                elif state.backend == "python":
                    result = run_python(found.path, command, req, state.timeout)
                else:
                    result = run_sps(state.backend, found.path, command, req, state.timeout)
                    result["backend"]["version"] = found.version
            except BackendError as e:
                self._refuse(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
                return
            finally:
                state.busy.release()
            self._json(HTTPStatus.OK, result)

    return Handler


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(state: State, port: int) -> LoopbackServer:
    return LoopbackServer(("127.0.0.1", port), make_handler(state))
