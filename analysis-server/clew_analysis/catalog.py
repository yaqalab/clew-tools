"""The commands this server can run: the shipped ones (commands/<id>/command.json plus one
adapter per backend) and, when the researcher turns a folder on, their own scripts.

A command says what it needs from Clew's three inputs (how many columns, of which levels), so a
request is checked here before any backend sees it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sps import SUPPORT as SPS_SUPPORT

SPS_FILE = Path(__file__).resolve().parent / "sps.py"

COMMANDS_DIR = Path(__file__).resolve().parent / "commands"
LEVELS = ("scale", "nominal", "ordinal", "text")
ROLES = ("dependent", "independent", "groupby")
# Names a backend may put in a formula or a script: Clew sends the export's safe names.
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
COMMAND_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
# Adapter file per backend; PSPP and SPSS share one generator (sps.py), which says what it covers.
ADAPTER_FILE = {"r": "r.R", "python": "python.py"}


@dataclass
class Need:
    min: int = 0
    max: int = 0
    levels: tuple[str, ...] = ()


@dataclass
class Command:
    id: str
    version: int
    group: str
    title: dict[str, str]
    needs: dict[str, Need]
    folder: Path
    user: bool = False
    adapters: dict[str, Path] = field(default_factory=dict)

    def describe(self, backend: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "group": self.group,
            "title": self.title,
            "needs": {
                role: {"min": n.min, "max": n.max, "levels": list(n.levels)}
                for role, n in self.needs.items()
            },
            "available": backend in self.adapters,
            "user": self.user,
        }


def _need(raw: Any) -> Need:
    if not isinstance(raw, dict):
        return Need()
    levels = tuple(x for x in raw.get("levels", []) if x in LEVELS)
    return Need(int(raw.get("min", 0)), int(raw.get("max", 0)), levels)


def load_shipped(root: Path = COMMANDS_DIR) -> dict[str, Command]:
    out: dict[str, Command] = {}
    for meta in sorted(root.glob("*/command.json")):
        raw = json.loads(meta.read_text(encoding="utf-8"))
        cid = str(raw.get("id", ""))
        if not COMMAND_ID.match(cid) or cid != meta.parent.name:
            continue
        needs = {role: _need(raw.get("needs", {}).get(role)) for role in ("dependent", "independent")}
        adapters = {
            b: meta.parent / f for b, f in ADAPTER_FILE.items() if (meta.parent / f).is_file()
        }
        for dialect, ids in SPS_SUPPORT.items():
            if cid in ids:
                adapters[dialect] = SPS_FILE
        out[cid] = Command(
            id=cid,
            version=int(raw.get("version", 1)),
            group=str(raw.get("group", "other")),
            title={k: str(v) for k, v in raw.get("title", {}).items()},
            needs=needs,
            folder=meta.parent,
            adapters=adapters,
        )
    return out


_HEADER = re.compile(r"^#'?\s*(title|dependent|independent)\s*:\s*(.*)$")


def load_user(folder: Path | None) -> dict[str, Command]:
    """The researcher's own scripts: `<name>.R` (run by R) or `<name>.py` (run by Python), each
    defining `analyse(d, spec)`, with optional header lines `#' title: ...` (R) or `# title: ...`
    (Python), `dependent: 1-3`, `independent: 0-5`. Clew can choose one by name; it can never send one."""
    out: dict[str, Command] = {}
    if folder is None or not folder.is_dir():
        return out
    for script in sorted([*folder.glob("*.R"), *folder.glob("*.py")]):
        stem = re.sub(r"[^a-z0-9_-]+", "-", script.stem.lower()).strip("-")
        if not stem:
            continue
        title = script.stem
        needs = {"dependent": Need(0, 100, LEVELS), "independent": Need(0, 100, LEVELS)}
        try:
            head = script.read_text(encoding="utf-8").splitlines()[:20]
        except (OSError, UnicodeDecodeError):
            continue
        for line in head:
            m = _HEADER.match(line.strip())
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip()
            if key == "title" and value:
                title = value[:120]
            elif key in needs:
                r = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", value)
                if r:
                    lo = int(r.group(1))
                    needs[key] = Need(lo, int(r.group(2) or lo), LEVELS)
        cid = f"user.{stem}"
        out[cid] = Command(
            id=cid,
            version=1,
            group="user",
            title={"en": title, "ja": title, "fr": title},
            needs=needs,
            folder=folder,
            user=True,
            adapters={"r" if script.suffix == ".R" else "python": script},
        )
    return out


class RequestError(ValueError):
    """A request this server will not run, with the reason in words Clew can show."""


def check_request(command: Command, body: dict[str, Any]) -> dict[str, Any]:
    """Validates Clew's neutral command against what the command needs; returns it normalised."""
    codebook_raw = body.get("codebook")
    rows = body.get("rows")
    if not isinstance(codebook_raw, list) or not isinstance(rows, list):
        raise RequestError("codebook and rows are required")
    codebook: dict[str, dict[str, Any]] = {}
    for entry in codebook_raw:
        if not isinstance(entry, dict):
            raise RequestError("a codebook entry is not an object")
        name = entry.get("name")
        level = entry.get("level")
        if not isinstance(name, str) or not NAME.match(name):
            raise RequestError(f"a column name is not allowed: {name!r}")
        if level not in LEVELS:
            raise RequestError(f"{name}: unknown level {level!r}")
        order = entry.get("order")
        if order is not None and not (isinstance(order, list) and all(isinstance(x, str) for x in order)):
            raise RequestError(f"{name}: order must be a list of texts")
        label = entry.get("label", "")
        codebook[name] = {
            "level": level,
            "label": label if isinstance(label, str) else "",
            "order": order,
        }
    roles: dict[str, list[str]] = {}
    for role in ROLES:
        names = body.get(role, [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise RequestError(f"{role} must be a list of column names")
        for n in names:
            if n not in codebook:
                raise RequestError(f"{n} is not in the codebook")
        roles[role] = names
    for role, need in command.needs.items():
        names = roles[role]
        if not (need.min <= len(names) <= need.max):
            raise RequestError(
                f"{role}: {command.id} takes {need.min}-{need.max} columns, got {len(names)}"
            )
        for n in names:
            if need.levels and codebook[n]["level"] not in need.levels:
                raise RequestError(
                    f"{n}: {command.id} takes {', '.join(need.levels)} columns here, not {codebook[n]['level']}"
                )
    for n in roles["groupby"]:
        if codebook[n]["level"] not in ("nominal", "ordinal"):
            raise RequestError(f"{n}: group by takes categories")
    options = body.get("options", {})
    if not isinstance(options, dict):
        raise RequestError("options must be an object")
    clean_options = {}
    for k, v in options.items():
        if not isinstance(k, str) or not NAME.match(k) or not isinstance(v, (str, int, float, bool)):
            raise RequestError(f"option {k!r} is not allowed")
        clean_options[k] = str(v).lower() if isinstance(v, bool) else str(v)
    names = list(codebook)
    for r in rows:
        if not isinstance(r, dict):
            raise RequestError("a row is not an object")
        for k, v in r.items():
            if k not in codebook or not (v is None or isinstance(v, (str, int, float))):
                raise RequestError("a row holds an unknown column or a value that is not text")
    return {
        "command": command.id,
        **roles,
        "options": clean_options,
        "codebook": codebook,
        "columns": names,
        "rows": rows,
    }
