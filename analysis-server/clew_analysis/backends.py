"""The translators: one per backend. Each takes a checked neutral request (catalog.check_request),
turns it into what its program reads, runs the command's adapter, and turns the output back into
the neutral result:

    {"tables": [{"title", "head", "body", "stub"}], "notes": [...], "figures": [...],
     "script": "...", "backend": {"name", "version"}}

R and Python get the data on their standard input, never through a file, and give the result
back between two marker lines on their standard output. PSPP and SPSS read files only: their job
goes to a temporary folder that is deleted after the run (run_sps).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import COMMANDS_DIR, Command

BEGIN = "<<<CLEW-RESULT>>>"
END = "<<<CLEW-END>>>"


class BackendError(RuntimeError):
    """The program could not run the command; the message is fit to show."""


@dataclass
class Found:
    id: str
    name: str
    path: str | None
    version: str
    ready: bool
    why: str = ""

    def describe(self) -> dict[str, Any]:
        # The program's path stays here: Clew has no use for it.
        out = {"id": self.id, "name": self.name, "version": self.version, "ready": self.ready}
        if self.why:
            out["why"] = self.why
        return out


def _windows_rscript() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\R-core\R") as key:
                    home = winreg.QueryValueEx(key, "InstallPath")[0]
                exe = Path(home, "bin", "Rscript.exe")
                if exe.is_file():
                    return str(exe)
            except OSError:
                continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        root = Path(base, "R") if base else None
        if root and root.is_dir():
            versions = sorted(p for p in root.glob("R-*") if (p / "bin" / "Rscript.exe").is_file())
            if versions:
                return str(versions[-1] / "bin" / "Rscript.exe")
    return None


def find_rscript(setting: str | None = None) -> str | None:
    """The setting if it is a program, then PATH, then (Windows) the registry and Program Files."""
    if setting and Path(setting).is_file():
        return setting
    return shutil.which("Rscript") or _windows_rscript()


def _quiet() -> dict[str, Any]:
    # No console window for the child on Windows.
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def detect_r(setting: str | None = None) -> Found:
    exe = find_rscript(setting)
    if not exe:
        return Found("r", "R", None, "", False, "R was not found on this computer")
    try:
        out = subprocess.run(
            [exe, "--vanilla", "-e", "cat(R.version$major, R.version$minor, sep='.')"],
            capture_output=True, text=True, timeout=30, **_quiet(),
        )
        version = out.stdout.strip()
        if out.returncode != 0 or not re.match(r"^\d+\.\d+", version):
            return Found("r", "R", exe, "", False, "R did not start")
        return Found("r", "R", exe, version, True)
    except (OSError, subprocess.TimeoutExpired):
        return Found("r", "R", exe, "", False, "R did not start")


def r_input(req: dict[str, Any]) -> str:
    """What clew.R reads on its standard input (see the head of clew.R)."""
    lines = [f"command\t{req['command']}"]
    for role in ("dependent", "independent", "groupby"):
        lines.append("\t".join([role, *req[role]]))
    for k, v in req["options"].items():
        lines.append(f"option\t{k}\t{_one_line(v)}")
    for name, entry in req["codebook"].items():
        lines.append(f"level\t{name}\t{entry['level']}")
        if entry["label"]:
            lines.append(f"label\t{name}\t{_one_line(entry['label'])}")
        if entry["order"]:
            lines.append("\t".join(["order", name, *(_one_line(x) for x in entry["order"])]))
    lines.append("---data---")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(req["columns"])
    for row in req["rows"]:
        w.writerow(["" if row.get(c) is None else _one_line(str(row.get(c))) for c in req["columns"]])
    return "\n".join(lines) + "\n" + buf.getvalue()


def _one_line(s: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", s)


def parse_output(stdout: str) -> dict[str, Any]:
    start = stdout.rfind(BEGIN)
    end = stdout.rfind(END)
    if start < 0 or end < start:
        raise BackendError("the program gave no result")
    try:
        result = json.loads(stdout[start + len(BEGIN):end])
    except ValueError as e:
        raise BackendError("the program's result could not be read") from e
    if not isinstance(result, dict):
        raise BackendError("the program's result could not be read")
    return result


def run_r(exe: str, command: Command, req: dict[str, Any], timeout: float) -> dict[str, Any]:
    adapter = command.adapters.get("r")
    if adapter is None:
        raise BackendError(f"{command.id} has no R version")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        out = subprocess.run(
            [exe, "--vanilla", str(COMMANDS_DIR / "clew.R"), str(adapter)],
            input=r_input(req).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=env,
            **_quiet(),
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"R took longer than {int(timeout)} seconds and was stopped") from e
    except OSError as e:
        raise BackendError("R could not be started") from e
    result = parse_output(out.stdout.decode("utf-8", errors="replace"))
    if "error" in result:
        raise BackendError(f"R: {result['error']}")
    result["command"] = {"id": command.id, "version": command.version}
    return result


# -- Python (a Conda environment, or any Python with pandas, numpy, scipy, statsmodels) ----------

PY_LIBS = ("pandas", "numpy", "scipy", "statsmodels")
PY_CHECK = "import sys, pandas, numpy, scipy, statsmodels; print(sys.version.split()[0])"
CONDA_ROOTS = ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "Miniconda3", "Anaconda3")


def _python_of(env: Path) -> Path:
    return env / "python.exe" if sys.platform == "win32" else env / "bin" / "python"


def _env_path(exe: str) -> dict[str, str]:
    """What `conda activate` would put first on PATH, so a Conda environment's own DLLs (MKL,
    OpenBLAS) load without activating it. Harmless for any other Python."""
    env = dict(os.environ)
    root = Path(exe).parent if sys.platform == "win32" else Path(exe).parent.parent
    extra = [root, root / "Library" / "mingw-w64" / "bin", root / "Library" / "usr" / "bin",
             root / "Library" / "bin", root / "Scripts", root / "bin"]
    env["PATH"] = os.pathsep.join([str(p) for p in extra if p.is_dir()] + [env.get("PATH", "")])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _python_version(exe: str) -> tuple[str, str]:
    """(version, "") when this Python has the libraries, else ("", why)."""
    try:
        out = subprocess.run([exe, "-I", "-c", PY_CHECK], capture_output=True, text=True, timeout=60,
                             env=_env_path(exe), **_quiet())
    except (OSError, subprocess.TimeoutExpired):
        return "", "this Python did not start"
    if out.returncode == 0 and re.match(r"^\d+\.\d+", out.stdout.strip()):
        return out.stdout.strip(), ""
    missing = re.search(r"No module named '([^']+)'", out.stderr or "")
    if missing:
        return "", f"{missing.group(1)} is not installed in it (needs {', '.join(PY_LIBS)})"
    return "", "this Python could not load its libraries"


def conda_environments() -> list[tuple[str, Path]]:
    """Every Conda environment found, as (name, folder): from `conda info --envs --json` when a conda
    program is found, else the usual install folders and their envs/."""
    envs: list[Path] = []
    exe = os.environ.get("CONDA_EXE") or shutil.which("conda") or shutil.which("mamba")
    if exe:
        try:
            out = subprocess.run([exe, "info", "--envs", "--json"], capture_output=True, text=True, timeout=60,
                                 **_quiet())
            envs = [Path(p) for p in json.loads(out.stdout or "{}").get("envs", [])]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            envs = []
    if not envs:
        bases = [Path.home()]
        for var in ("LOCALAPPDATA", "ProgramData", "USERPROFILE"):
            if os.environ.get(var):
                bases.append(Path(os.environ[var]))
        bases.append(Path("/opt"))
        for base in bases:
            for name in CONDA_ROOTS + ("conda",):
                root = base / name
                if _python_of(root).is_file():
                    envs.append(root)
                    if (root / "envs").is_dir():
                        envs += sorted(p for p in (root / "envs").iterdir() if p.is_dir())
    seen: list[tuple[str, Path]] = []
    for env in envs:
        name = env.name if env.parent.name == "envs" else "base"
        if _python_of(env).is_file() and all(env != e for _, e in seen):
            seen.append((name, env))
    return seen


def detect_python(setting: str | None = None) -> Found:
    if setting:
        version, why = _python_version(setting) if Path(setting).is_file() else ("", "this Python was not found")
        return Found("python", "Python", setting, version, bool(version), why)
    envs = conda_environments()
    if not envs:
        return Found("python", "Python", None, "", False, "no Conda environment was found")
    # An environment named clew first, then base, then the others.
    envs.sort(key=lambda e: (e[0] != "clew", e[0] != "base", e[0]))
    whys = []
    for name, env in envs:
        exe = str(_python_of(env))
        version, why = _python_version(exe)
        if version:
            return Found("python", "Python", exe, version, True)
        whys.append(f"{name}: {why}")
    return Found("python", "Python", None, "", False,
                 f"no Conda environment has {', '.join(PY_LIBS)} ({'; '.join(whys[:3])})")


def run_python(exe: str, command: Command, req: dict[str, Any], timeout: float) -> dict[str, Any]:
    adapter = command.adapters.get("python")
    if adapter is None:
        raise BackendError(f"{command.id} has no Python version")
    try:
        out = subprocess.run(
            [exe, "-I", str(COMMANDS_DIR / "clew_py.py"), str(adapter)],
            input=json.dumps(req, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=_env_path(exe),
            **_quiet(),
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"Python took longer than {int(timeout)} seconds and was stopped") from e
    except OSError as e:
        raise BackendError("Python could not be started") from e
    result = parse_output(out.stdout.decode("utf-8", errors="replace"))
    if "error" in result:
        raise BackendError(f"Python: {result['error']}")
    result["command"] = {"id": command.id, "version": command.version}
    return result


# -- PSPP and SPSS: SPSS syntax in a job folder (sps.py) -----------------------------------------

def detect_pspp(setting: str | None = None) -> Found:
    exe = setting if setting and Path(setting).is_file() else None
    if not exe and not setting:
        exe = shutil.which("pspp")
        if not exe and sys.platform == "win32":
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
                cand = Path(base, "PSPP", "bin", "pspp.exe") if base else None
                if cand and cand.is_file():
                    exe = str(cand)
                    break
    if not exe:
        return Found("pspp", "PSPP", None, "", False,
                     "this PSPP was not found" if setting else "PSPP was not found on this computer")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, **_quiet())
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        m = None
    if not m:
        return Found("pspp", "PSPP", exe, "", False, "PSPP did not start")
    return Found("pspp", "PSPP", exe, m.group(1), True)


def _spss_python(folder: Path) -> Path | None:
    for sub in ("Python3", "Python"):
        exe = folder / sub / ("python.exe" if sys.platform == "win32" else "bin/python3")
        if exe.is_file():
            return exe
    return None


def detect_spss(setting: str | None = None) -> Found:
    """SPSS Statistics through its Python plug-in's "external mode": SPSS's own Python, which can
    `import spss` and submit syntax. `setting` is that python.exe or SPSS's install folder."""
    candidates: list[Path] = []
    if setting:
        p = Path(setting)
        candidates = [p] if p.is_file() else ([e] if (e := _spss_python(p)) else [])
    elif sys.platform == "win32":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "IBM")
        roots = list(base.glob("SPSS Statistics*")) + list(base.glob("SPSS/Statistics/*"))
        for root in sorted(roots, reverse=True):
            if (e := _spss_python(root)) is not None:
                candidates.append(e)
    if not candidates:
        return Found("spss", "SPSS", None, "", False,
                     "this SPSS was not found" if setting else "SPSS Statistics was not found on this computer")
    exe = candidates[0]
    site = exe.parent / "Lib" / "site-packages" / "spss"
    if not site.is_dir():
        return Found("spss", "SPSS", str(exe), "", False, "SPSS's Python plug-in (the spss module) is not installed")
    version = next((part for part in reversed(exe.parts) if re.fullmatch(r"\d+(\.\d+)*", part)), "")
    return Found("spss", "SPSS", str(exe), version or "found", True)


SPSS_RUN = """import sys
import spss
try:
    spss.Submit(open(sys.argv[1], encoding="utf-8").read())
finally:
    spss.StopSPSS()
"""


def run_sps(dialect: str, exe: str, command: Command, req: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Writes the job, runs PSPP or SPSS on it, reads the tables back. The job folder (the data
    among it) is deleted whatever happens."""
    import tempfile

    from . import sps

    if command.id not in sps.SUPPORT[dialect]:
        raise BackendError(f"{command.id} is not available in {dialect.upper()}")
    name = "PSPP" if dialect == "pspp" else "SPSS"
    with tempfile.TemporaryDirectory(prefix="clew-analysis-") as job:
        data = Path(job, "data.csv")
        out_file = Path(job, "out.csv" if dialect == "pspp" else "out.txt")
        syntax_file = Path(job, "job.sps")
        data.write_text(sps.data_csv(req), encoding="utf-8")
        try:
            text = sps.syntax(req, dialect, str(data), str(out_file))
        except ValueError as e:
            raise BackendError(f"{name}: {e}") from e
        syntax_file.write_text(text, encoding="utf-8")
        if dialect == "pspp":
            args = [exe, "-o", str(out_file), str(syntax_file)]
            env = None
        else:
            runner = Path(job, "run.py")
            runner.write_text(SPSS_RUN, encoding="utf-8")
            args = [exe, str(runner), str(syntax_file)]
            env = _env_path(exe)
        try:
            proc = subprocess.run(args, capture_output=True, timeout=timeout, cwd=job, env=env, **_quiet())
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"{name} took longer than {int(timeout)} seconds and was stopped") from e
        except OSError as e:
            raise BackendError(f"{name} could not be started") from e
        said = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        output = out_file.read_text(encoding="utf-8", errors="replace") if out_file.is_file() else ""
    tables, notes = sps.read_pspp_csv(output) if dialect == "pspp" else sps.read_spss_tabtext(output)
    errors = [line.strip() for line in said.splitlines() if re.search(r"\berror\b", line, re.I)]
    errors += [n for n in notes if re.search(r"\berror\b", n, re.I)]
    if not tables:
        raise BackendError(f"{name}: " + (errors[0] if errors else "no table came back"))
    return {
        "tables": tables,
        "notes": [n for n in notes if n not in errors][:10] + errors[:5],
        "figures": [],
        "script": "* data.csv: the data as the engine wrote it, each category as 1, 2, 3... in the form's order.\n"
        + text.replace(str(data), "data.csv").replace(str(out_file), out_file.name),
        "backend": {"name": name, "version": ""},
        "command": {"id": command.id, "version": command.version},
    }
