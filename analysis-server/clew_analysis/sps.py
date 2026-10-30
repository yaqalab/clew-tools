"""The translator for PSPP and SPSS (docs/36 section 5.2): Clew's neutral command written as
SPSS syntax, which both programs read, with the few places they differ ("dialect").

- The data go in a job folder as data.csv, each category as its position in the form's order
  (1, 2, 3...), with VALUE LABELS, numbers as numbers. The job folder is deleted after the run.
- A category used as a predictor becomes one 0/1 variable per value after its first, labelled
  "Gender: Male", as R's and Python's adapters name their terms.
- Group by is SPLIT FILE LAYERED.
- The output is the program's own tables: PSPP writes them as CSV (`pspp -o out.csv`), SPSS
  through OMS as tab-separated text. Both are read back into the neutral table shape.

PSPP has no ordinal regression (no PLUM): that command is SPSS only here.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

SUPPORT = {
    "pspp": {"regression.linear", "regression.logistic", "test.fisher", "test.ranks", "reliability.alpha"},
    "spss": {"regression.linear", "regression.logistic", "regression.ordinal", "test.fisher", "test.ranks",
             "reliability.alpha"},
}


def _q(text: str) -> str:
    """A quoted string in syntax: single quotes, doubled inside, on one line."""
    return "'" + re.sub(r"[\r\n\t]+", " ", str(text)).replace("'", "''") + "'"


def _label(req: dict[str, Any], name: str) -> str:
    return req["codebook"][name].get("label") or name


def _order(req: dict[str, Any], name: str) -> list[str]:
    entry = req["codebook"][name]
    if entry.get("order"):
        return list(entry["order"])
    return sorted({str(r.get(name)) for r in req["rows"] if r.get(name) not in (None, "")})


def _present(req: dict[str, Any], name: str) -> list[int]:
    """The 1-based codes of the values the rows hold, in the form's order."""
    order = _order(req, name)
    held = {str(r.get(name)) for r in req["rows"] if r.get(name) not in (None, "")}
    return [i + 1 for i, v in enumerate(order) if v in held]


def data_csv(req: dict[str, Any]) -> str:
    """The rows, categories as their codes, numbers as they are, missing as empty."""
    codes = {}
    for name, entry in req["codebook"].items():
        if entry["level"] in ("nominal", "ordinal"):
            codes[name] = {v: str(i + 1) for i, v in enumerate(_order(req, name))}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(req["columns"])
    for r in req["rows"]:
        out = []
        for c in req["columns"]:
            v = r.get(c)
            v = "" if v is None else str(v)
            out.append(codes[c].get(v, "") if c in codes and v else v)
        w.writerow(out)
    return buf.getvalue()


def _head(req: dict[str, Any], dialect: str, data_path: str, out_path: str) -> list[str]:
    lines = []
    if dialect == "spss":
        lines += [
            "SET UNICODE=ON.",
            f"OMS /SELECT TABLES /DESTINATION FORMAT=TABTEXT OUTFILE={_q(out_path)} VIEWER=NO.",
        ]
    variables = []
    for c in req["columns"]:
        level = req["codebook"][c]["level"]
        variables.append(f"  {c} {'A255' if level == 'text' else 'F12.4' if level == 'scale' else 'F8.0'}")
    lines += [
        "GET DATA /TYPE=TXT",
        f"  /FILE={_q(data_path)}",
        "  /ENCODING='UTF8'" if dialect == "spss" else "  /ENCODING='UTF-8'",
        "  /ARRANGEMENT=DELIMITED /DELCASE=LINE /FIRSTCASE=2",
        "  /DELIMITERS=\",\" /QUALIFIER='\"'",
        "  /VARIABLES=",
        *variables,
        ".",
    ]
    labels = [f"  {c} {_q(_label(req, c))}" for c in req["columns"]]
    lines += ["VARIABLE LABELS", " /\n".join(labels) + "."]
    cats = [c for c in req["columns"] if req["codebook"][c]["level"] in ("nominal", "ordinal")]
    if cats:
        parts = []
        for c in cats:
            parts.append(f"  {c} " + " ".join(f"{i + 1} {_q(v)}" for i, v in enumerate(_order(req, c))))
        lines += ["VALUE LABELS", " /\n".join(parts) + "."]
    return lines


def _dummies(req: dict[str, Any], names: list[str]) -> tuple[list[str], list[str]]:
    """The predictors as the procedures take them: numbers as they are, categories as 0/1 variables
    for each present value after the first. Returns the syntax that makes them and their names."""
    lines, out = [], []
    for i, n in enumerate(names):
        level = req["codebook"][n]["level"]
        if level == "scale":
            out.append(n)
            continue
        order = _order(req, n)
        for code in _present(req, n)[1:]:
            name = f"clew_d{i}_{code}"
            lines += [f"COMPUTE {name} = ({n} = {code}).", f"VARIABLE LABELS {name} {_q(_label(req, n) + ': ' + order[code - 1])}."]
            out.append(name)
    return lines, out


def _command(req: dict[str, Any], dialect: str) -> list[str]:
    cid = req["command"]
    dep, ind = req["dependent"], req["independent"]
    if cid == "regression.linear":
        make, xs = _dummies(req, ind)
        return make + [f"REGRESSION /DEPENDENT={dep[0]} /METHOD=ENTER {' '.join(xs)}."]
    if cid == "regression.logistic":
        present = _present(req, dep[0])
        if len(present) != 2:
            raise ValueError(f"{_label(req, dep[0])} needs exactly two values here; it has {len(present)}")
        make, xs = _dummies(req, ind)
        order = _order(req, dep[0])
        return make + [
            f"COMPUTE clew_y = ({dep[0]} = {present[1]}).",
            f"VARIABLE LABELS clew_y {_q(_label(req, dep[0]) + ': ' + order[present[1] - 1])}.",
            f"LOGISTIC REGRESSION clew_y WITH {' '.join(xs)}.",
        ]
    if cid == "regression.ordinal":
        if dialect != "spss":
            raise ValueError("ordinal regression is not in PSPP")
        make, xs = _dummies(req, ind)
        return make + [f"PLUM {dep[0]} WITH {' '.join(xs)} /LINK=LOGIT /PRINT=FIT PARAMETER SUMMARY."]
    if cid == "test.fisher":
        return [f"CROSSTABS /TABLES={dep[0]} BY {ind[0]} /STATISTICS=CHISQ."]
    if cid == "test.ranks":
        groups = _present(req, ind[0])
        if len(groups) < 2:
            raise ValueError(f"{_label(req, ind[0])} needs two groups or more here")
        if len(groups) == 2:
            return [f"NPAR TESTS /M-W={dep[0]} BY {ind[0]}({groups[0]} {groups[1]})."]
        return [f"NPAR TESTS /K-W={dep[0]} BY {ind[0]}({groups[0]} {groups[-1]})."]
    if cid == "reliability.alpha":
        return [f"RELIABILITY /VARIABLES={' '.join(dep)} /SCALE('Items') ALL /MODEL=ALPHA /SUMMARY=TOTAL."]
    raise ValueError(f"{cid} has no SPSS syntax")


def syntax(req: dict[str, Any], dialect: str, data_path: str, out_path: str) -> str:
    """The whole job: read the data, label it, split it, run the command."""
    lines = _head(req, dialect, data_path, out_path)
    if req["groupby"]:
        lines += [f"SORT CASES BY {' '.join(req['groupby'])}.", f"SPLIT FILE LAYERED BY {' '.join(req['groupby'])}."]
    lines += _command(req, dialect)
    if dialect == "spss":
        lines.append("OMSEND.")
    return "\n".join(lines) + "\n"


# -- reading the output back ----------------------------------------------------------------------

def _blocks(rows: list[list[str]]) -> list[list[list[str]]]:
    blocks, current = [], []
    for r in rows:
        if not any(c.strip() for c in r):
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(r)
    if current:
        blocks.append(current)
    return blocks


def _table(title: str, rows: list[list[str]]) -> dict[str, Any] | None:
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return {"title": title, "head": rows[0], "body": rows[1:], "stub": 1}


def read_pspp_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """PSPP's CSV output: each table starts with a "Table: <title>" line; other items (notes,
    errors, text) are lines of their own. Returns the tables and the text items."""
    tables, notes = [], []
    for block in _blocks(list(csv.reader(io.StringIO(text)))):
        first = block[0][0] if block[0] else ""
        if first.startswith("Table:"):
            t = _table(first[len("Table:"):].strip(), block[1:])
            if t:
                tables.append(t)
        else:
            notes += [" ".join(c for c in r if c.strip()) for r in block]
    return tables, [n for n in notes if n]


def read_spss_tabtext(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """SPSS's OMS TABTEXT output: each table is its title on a line, then its rows separated by
    tabs, with a blank line after it."""
    tables, notes = [], []
    rows = [line.split("\t") for line in text.splitlines()]
    for block in _blocks(rows):
        if len(block) == 1:
            notes.append(" ".join(block[0]).strip())
            continue
        t = _table(" ".join(block[0]).strip(), block[1:])
        if t:
            tables.append(t)
    return tables, [n for n in notes if n]
