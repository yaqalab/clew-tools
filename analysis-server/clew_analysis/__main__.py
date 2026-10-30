"""clew-analysis-server: the analysis engine on this computer (docs/36 section 5).

    python -m clew_analysis                      start; makes a key the first time and shows it
    python -m clew_analysis --show-key           show the key again
    python -m clew_analysis --key-stdin          the key is the first line of standard input
                                                 (how the tray program starts it, as dictation)
    python -m clew_analysis --check              list the backends found and the commands

Options: --port (default 47823), --backend r, --rscript PATH, --scripts DIR (the researcher's own
R scripts), --timeout SECONDS (default 120), --allow-origin URL (repeatable).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .server import BACKENDS, State, create_server

DEFAULT_PORT = 47823
HOSTED_CLEW = "https://clew.alefa.net"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def key_file() -> Path:
    return config_dir() / "key.txt"


def stored_key(make: bool) -> tuple[str | None, bool]:
    """The key kept for the standalone server, readable by this user only; made when asked."""
    path = key_file()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip(), False
    if not make:
        return None, False
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "ana." + secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key, True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Clew's analysis engine on this computer.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--backend", choices=BACKENDS, default="r")
    p.add_argument("--rscript", default=None, help="Rscript to use, when it is not found by itself")
    p.add_argument("--python", default=None, help="a Python with pandas, numpy, scipy, statsmodels "
                   "(default: a Conda environment that has them)")
    p.add_argument("--pspp", default=None, help="pspp(.exe) to use, when it is not found by itself")
    p.add_argument("--spss", default=None, help="SPSS Statistics' folder or its Python, when not found by itself")
    p.add_argument("--scripts", default=None, help="a folder of your own R scripts")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--allow-origin", action="append", default=[])
    p.add_argument("--key-stdin", action="store_true")
    p.add_argument("--show-key", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    if args.show_key:
        key, _ = stored_key(make=False)
        print(key or "No key yet: start the server once to make one.")
        return 0 if key else 1

    origins = [HOSTED_CLEW, *args.allow_origin]
    scripts = Path(args.scripts).expanduser() if args.scripts else None

    if args.check:
        state = State("", args.backend, origins, args.rscript, scripts, args.timeout,
                      args.python, args.pspp, args.spss)
        for f in state.found().values():
            # "R: 4.3.3 at C:\...\Rscript.exe": the tray's Analysis tab reads these lines.
            if f.ready:
                print(f"{f.name}: {f.version} at {f.path}", flush=True)
            else:
                print(f"{f.name}: not ready ({f.why})", flush=True)
        for c in state.commands().values():
            print(f"  {c.id}{'' if args.backend in c.adapters else ' (not for ' + args.backend + ')'}")
        return 0

    if args.key_stdin:
        key = sys.stdin.readline().strip()
        if len(key) < 16:
            print("The key on standard input is too short.", file=sys.stderr)
            return 2
    else:
        key, made = stored_key(make=True)
        assert key is not None
        if made:
            print(f"Key (paste it into Clew; --show-key shows it again): {key}", flush=True)

    state = State(key, args.backend, origins, args.rscript, scripts, args.timeout,
                  args.python, args.pspp, args.spss)
    try:
        server = create_server(state, args.port)
    except OSError as e:
        print(f"Port {args.port} is not available ({e}). Try --port <number>.", file=sys.stderr)
        return 1
    print(f"Clew analysis server {__version__} on http://127.0.0.1:{args.port} (backend: {args.backend})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
