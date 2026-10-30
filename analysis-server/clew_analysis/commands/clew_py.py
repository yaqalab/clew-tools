"""Clew analysis: the part every Python adapter shares (docs/36, section 5.2), the Python twin of
clew.R. It runs in the researcher's own Python environment (a Conda environment, say) with pandas,
numpy, scipy and statsmodels; nothing else of Clew's is imported there.

    python -I clew_py.py <adapter.py>

Standard input: the engine's checked request as JSON (command, dependent, independent, groupby,
options, codebook {name: {level, label, order}}, columns, rows). The adapter defines
`analyse(d, spec)` returning {"tables": [...], "notes": [...], "script": [...]}. This file builds
the data frame (numbers as floats, categories in the form's order, an ordered category ordered),
runs the adapter once per Group by group, collects warnings as notes, and prints one JSON result
between two marker lines. Nothing is written to disk.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import warnings
from types import SimpleNamespace

BEGIN = "<<<CLEW-RESULT>>>"
END = "<<<CLEW-END>>>"


# -- helpers the adapters use (the same look as clew.R's) ------------------------------------------

def num(x, digits=3):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    return "" if math.isnan(x) or math.isinf(x) else f"{x:.{digits}f}"


def pval(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return ""
    if math.isnan(p):
        return ""
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def p_said(p):
    """"= 0.012" or "< 0.001", for a sentence."""
    s = pval(p)
    return s if s.startswith("<") else f"= {s}"


def table(title, head, rows, stub=1):
    return {"title": title, "head": [str(h) for h in head], "body": [[str(c) for c in r] for r in rows], "stub": stub}


def label(spec, name):
    return spec.labels.get(name) or name


def design(d, spec, names):
    """The predictors as numbers: a number as it is, a category as one 0/1 column per value after
    its first (the first present value is the reference, as R's treatment contrasts). Returns the
    matrix's columns and their names as a reader knows them ("Gender: Male")."""
    import pandas as pd

    cols = {}
    shown = []
    for n in names:
        s = d[n]
        if isinstance(s.dtype, pd.CategoricalDtype):
            present = [c for c in s.cat.categories if (s == c).any()]
            for level in present[1:]:
                key = f"{n}\x1f{level}"
                cols[key] = (s == level).astype(float).where(s.notna())
                shown.append(f"{label(spec, n)}: {level}")
        else:
            cols[n] = s.astype(float)
            shown.append(label(spec, n))
    return pd.DataFrame(cols, index=d.index), shown


# -- the run ---------------------------------------------------------------------------------------

def frame(req):
    import pandas as pd

    d = pd.DataFrame(req["rows"], columns=req["columns"]).replace({"": None})
    for name, entry in req["codebook"].items():
        level = entry["level"]
        if level == "scale":
            d[name] = pd.to_numeric(d[name], errors="coerce")
        elif level in ("nominal", "ordinal"):
            order = entry.get("order") or sorted(v for v in d[name].dropna().unique())
            d[name] = pd.Categorical(d[name], categories=order, ordered=level == "ordinal")
    return d


def prelude(req):
    out = [
        "import pandas as pd",
        "# data-labels.csv: from Survey entry's Export for analysis",
        "d = pd.read_csv('data-labels.csv', encoding='utf-8-sig', dtype=str, keep_default_na=False).replace({'': None})",
    ]
    for name, entry in req["codebook"].items():
        if name not in req["columns"]:
            continue
        if entry["level"] == "scale":
            out.append(f"d[{name!r}] = pd.to_numeric(d[{name!r}])")
        elif entry["level"] in ("nominal", "ordinal"):
            out.append(
                f"d[{name!r}] = pd.Categorical(d[{name!r}], categories={entry.get('order')!r}, "
                f"ordered={entry['level'] == 'ordinal'})"
            )
    return out


def main():
    adapter_path = sys.argv[1]
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        spec_obj = importlib.util.spec_from_file_location("clew_adapter", adapter_path)
        adapter = importlib.util.module_from_spec(spec_obj)
        sys.modules["clew_py"] = sys.modules[__name__]
        spec_obj.loader.exec_module(adapter)
        spec = SimpleNamespace(
            command=req["command"],
            dependent=req["dependent"],
            independent=req["independent"],
            groupby=req["groupby"],
            options=req.get("options", {}),
            labels={n: e.get("label", "") for n, e in req["codebook"].items()},
            levels={n: e["level"] for n, e in req["codebook"].items()},
        )
        d = frame(req)
        tables, notes, script = [], [], prelude(req)
        warned = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if not spec.groupby:
                out = adapter.analyse(d, spec)
                tables, notes = out.get("tables", []), list(out.get("notes", []))
                script += out.get("script", [])
            else:
                for key, part in d.groupby(spec.groupby, observed=True, sort=True):
                    key = key if isinstance(key, tuple) else (key,)
                    where = " and ".join(f"{label(spec, g)}={v}" for g, v in zip(spec.groupby, key))
                    try:
                        out = adapter.analyse(part, spec)
                    except Exception as e:  # noqa: BLE001 - one group's failure is said, the rest go on
                        notes.append(f"[{where}] {e}")
                        continue
                    for t in out.get("tables", []):
                        t["title"] = f"{t['title']} [{where}]"
                        tables.append(t)
                    notes += [f"[{where}] {n}" for n in out.get("notes", [])]
                    script += [f"# {where}", *out.get("script", [])]
            warned = [str(w.message) for w in caught]
        seen = []
        for w in warned:
            if w not in seen:
                seen.append(w)
        result = {
            "tables": tables,
            "notes": notes + seen[:5],
            "figures": [],
            "script": "\n".join(script),
            "backend": {"name": "Python", "version": sys.version.split()[0]},
        }
    except Exception as e:  # noqa: BLE001 - every failure comes back in words
        result = {"error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(BEGIN + "\n" + json.dumps(result, ensure_ascii=False) + "\n" + END + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
