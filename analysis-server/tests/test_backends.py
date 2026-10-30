"""
The Python, PSPP and SPSS translators (docs/36 section 5).

- Python runs for real when a Python with pandas, numpy, scipy and statsmodels is given in
  CLEW_TEST_PYTHON (or found in a Conda environment): every command, and each result checked
  against R's on the same data when R is installed too, so the two backends say the same.
- Conda environments are found through a stand-in `conda` that answers like the real one.
- PSPP runs for real when it is installed (CLEW_TEST_PSPP or on PATH).
- SPSS is not available here: its syntax is checked as text, and the reader of its output on a
  sample written to IBM's documented TABTEXT layout. Not run against SPSS.

    python -m unittest discover -s tools/analysis-server/tests -v
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clew_analysis import backends, sps  # noqa: E402
from clew_analysis.catalog import check_request, load_shipped  # noqa: E402

from test_server import CODEBOOK, ROWS, run_body  # noqa: E402

PYTHON = os.environ.get("CLEW_TEST_PYTHON") or (backends.detect_python().path if backends.detect_python().ready else None)
PY_OK = bool(PYTHON) and backends.detect_python(PYTHON).ready
RSCRIPT = backends.find_rscript()
PSPP = os.environ.get("CLEW_TEST_PSPP") or None
PSPP_FOUND = backends.detect_pspp(PSPP)
COMMANDS = load_shipped()


def request(command, dependent, independent, groupby=None, codebook=CODEBOOK, rows=ROWS):
    return check_request(COMMANDS[command], run_body(command, dependent, independent, groupby,
                                                     codebook=codebook, rows=rows))


def column(result, n=0, col=1):
    return {row[0]: row[col] for row in result["tables"][n]["body"]}


@unittest.skipUnless(PY_OK, "no Python with pandas, numpy, scipy and statsmodels (set CLEW_TEST_PYTHON)")
class PythonBackend(unittest.TestCase):
    def py(self, *args, **kwargs):
        req = request(*args, **kwargs)
        return backends.run_python(PYTHON, COMMANDS[req["command"]], req, 120)

    def r(self, *args, **kwargs):
        req = request(*args, **kwargs)
        return backends.run_r(RSCRIPT, COMMANDS[req["command"]], req, 120)

    def test_every_command_has_a_python_version(self):
        for cid, c in COMMANDS.items():
            self.assertIn("python", c.adapters, cid)

    def test_linear_regression(self):
        p = self.py("regression.linear", ["hours"], ["gender", "i1"])
        self.assertEqual(p["backend"]["name"], "Python")
        self.assertEqual(p["tables"][0]["head"], ["Term", "Estimate", "SE", "t", "p"])
        self.assertEqual(list(column(p)), ["(Intercept)", "Gender: Male", "Item 1"])
        self.assertIn("statsmodels", p["script"])
        if RSCRIPT:
            r = self.r("regression.linear", ["hours"], ["gender", "i1"])
            self.assertEqual(p["tables"][0]["body"], r["tables"][0]["body"])
            self.assertEqual(p["notes"][0], r["notes"][0])

    def test_logistic_regression(self):
        p = self.py("regression.logistic", ["passed"], ["hours"])
        self.assertIn('"yes" rather than "no"', p["notes"][0])
        if RSCRIPT:
            r = self.r("regression.logistic", ["passed"], ["hours"])
            self.assertEqual(p["tables"][0]["body"], r["tables"][0]["body"])
            self.assertEqual(p["notes"], r["notes"])

    def test_ordinal_regression_matches_polr(self):
        p = self.py("regression.ordinal", ["year"], ["hours"])
        self.assertEqual(p["tables"][1]["title"], "Thresholds")
        self.assertEqual(list(column(p, 1)), ["1|2", "2|3"])
        if RSCRIPT:
            r = self.r("regression.ordinal", ["year"], ["hours"])
            for n in (0, 1):
                for prow, rrow in zip(p["tables"][n]["body"], r["tables"][n]["body"]):
                    self.assertEqual(prow[0], rrow[0])
                    for a, b in zip(prow[1:4], rrow[1:4]):
                        # Two optimisers: the same to two decimals.
                        self.assertAlmostEqual(float(a), float(b), delta=0.01)

    def test_fisher_on_the_tea_tasting_table(self):
        rows = [{"guess": g, "truth": t} for g, t, n in
                [("milk", "milk", 3), ("milk", "tea", 1), ("tea", "milk", 1), ("tea", "tea", 3)]
                for _ in range(n)]
        codebook = [{"name": "guess", "level": "nominal"}, {"name": "truth", "level": "nominal"}]
        p = self.py("test.fisher", ["guess"], ["truth"], codebook=codebook, rows=rows)
        self.assertIn("p = 0.486", p["notes"][0])
        self.assertEqual(p["tables"][0]["body"][-1], ["All", "4", "4", "8"])

    def test_fisher_refuses_a_larger_table_in_words(self):
        req = request("test.fisher", ["year"], ["gender"])
        with self.assertRaises(backends.BackendError) as e:
            backends.run_python(PYTHON, COMMANDS["test.fisher"], req, 120)
        self.assertIn("two-by-two", str(e.exception))

    def test_ranks_and_alpha_say_what_r_says(self):
        for args in (("test.ranks", ["hours"], ["gender"]), ("test.ranks", ["hours"], ["year"]),
                     ("reliability.alpha", ["i1", "i2", "i3"], [])):
            p = self.py(*args)
            if RSCRIPT:
                r = self.r(*args)
                self.assertEqual(p["tables"][0]["body"], r["tables"][0]["body"], args)
                self.assertEqual(p["notes"], r["notes"], args)

    def test_group_by_runs_once_per_group(self):
        p = self.py("test.ranks", ["hours"], ["passed"], ["gender"])
        self.assertEqual([t["title"].split(" [")[-1] for t in p["tables"]], ["Gender=Female]", "Gender=Male]"])

    def test_the_server_runs_python_when_it_is_chosen(self):
        from clew_analysis.server import State

        state = State("k" * 20, "python", [], None, None, 120, python=PYTHON)
        self.assertTrue(state.found()["python"].ready)


class CondaEnvironments(unittest.TestCase):
    def test_found_through_conda_and_checked_for_the_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(sys.base_prefix)  # a Python laid out as a Conda environment: python at its root
            if not backends._python_of(env).is_file():
                self.skipTest("this Python is not laid out like a Conda environment")
            if sys.platform == "win32":
                fake = Path(tmp, "conda.bat")
                fake.write_text(f'@echo {{"envs": ["{str(env).replace(chr(92), chr(92) * 2)}"]}}\n')
            else:
                fake = Path(tmp, "conda")
                fake.write_text(f'#!/bin/sh\necho \'{{"envs": ["{env}"]}}\'\n')
                fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old = os.environ.get("CONDA_EXE")
            os.environ["CONDA_EXE"] = str(fake)
            try:
                envs = backends.conda_environments()
                found = backends.detect_python()
            finally:
                if old is None:
                    del os.environ["CONDA_EXE"]
                else:
                    os.environ["CONDA_EXE"] = old
            self.assertEqual(envs, [("base", env)])
            if found.ready:
                self.assertEqual(Path(found.path), backends._python_of(env))
            else:
                self.assertRegex(found.why, r"base: \w+ is not installed in it")

    def test_a_chosen_python_without_the_libraries_says_which_is_missing(self):
        found = backends.detect_python(sys.executable)
        if not found.ready:
            self.assertIn("is not installed in it", found.why)


class Syntax(unittest.TestCase):
    """What PSPP and SPSS are given, checked as text."""

    def job(self, dialect, *args, groupby=None):
        req = request(*args, groupby=groupby)
        return req, sps.syntax(req, dialect, "C:/job/data.csv", "C:/job/out.txt")

    def test_categories_are_codes_with_labels(self):
        req = request("regression.linear", ["hours"], ["gender"])
        data = sps.data_csv(req).splitlines()
        self.assertEqual(data[0], "hours,gender,year,passed,i1,i2,i3")
        self.assertEqual(data[1], "2,1,1,1,1,2,1")  # Female is 1, the first year 1, "no" 1
        _, text = self.job("pspp", "regression.linear", ["hours"], ["gender"])
        self.assertIn("  gender 1 'Female' 2 'Male'", text)
        self.assertIn("  hours 'Hours of study'", text)
        self.assertIn("  hours F12.4", text)
        self.assertIn("  gender F8.0", text)

    def test_a_category_predictor_becomes_labelled_dummies(self):
        _, text = self.job("pspp", "regression.linear", ["hours"], ["gender", "i1"])
        self.assertIn("COMPUTE clew_d0_2 = (gender = 2).", text)
        self.assertIn("VARIABLE LABELS clew_d0_2 'Gender: Male'.", text)
        self.assertIn("REGRESSION /DEPENDENT=hours /METHOD=ENTER clew_d0_2 i1.", text)

    def test_each_command(self):
        cases = {
            ("pspp", "regression.logistic", ("passed",), ("hours",)): "LOGISTIC REGRESSION clew_y WITH hours.",
            ("spss", "regression.ordinal", ("year",), ("hours",)): "PLUM year WITH hours /LINK=LOGIT",
            ("pspp", "test.fisher", ("gender",), ("passed",)): "CROSSTABS /TABLES=gender BY passed /STATISTICS=CHISQ.",
            ("pspp", "test.ranks", ("hours",), ("gender",)): "NPAR TESTS /M-W=hours BY gender(1 2).",
            ("pspp", "test.ranks", ("hours",), ("year",)): "NPAR TESTS /K-W=hours BY year(1 3).",
            ("pspp", "reliability.alpha", ("i1", "i2"), ()): "RELIABILITY /VARIABLES=i1 i2 /SCALE('Items') ALL /MODEL=ALPHA",
        }
        for (dialect, cid, dep, ind), expected in cases.items():
            _, text = self.job(dialect, cid, list(dep), list(ind))
            self.assertIn(expected, text, cid)

    def test_spss_writes_its_tables_through_oms_and_pspp_has_no_plum(self):
        _, text = self.job("spss", "regression.linear", ["hours"], ["gender"])
        self.assertTrue(text.startswith("SET UNICODE=ON.\nOMS /SELECT TABLES /DESTINATION FORMAT=TABTEXT"))
        self.assertTrue(text.rstrip().endswith("OMSEND."))
        self.assertNotIn("regression.ordinal", sps.SUPPORT["pspp"])
        self.assertNotIn("pspp", COMMANDS["regression.ordinal"].adapters)
        self.assertIn("spss", COMMANDS["regression.ordinal"].adapters)

    def test_group_by_splits_the_file(self):
        _, text = self.job("pspp", "test.ranks", ["hours"], ["passed"], groupby=["gender"])
        self.assertIn("SORT CASES BY gender.\nSPLIT FILE LAYERED BY gender.", text)

    def test_quotes_in_labels_are_doubled(self):
        codebook = [dict(CODEBOOK[0], label="Hours 'studied'"), *CODEBOOK[1:]]
        req = request("regression.linear", ["hours"], ["gender"], codebook=codebook)
        self.assertIn("hours 'Hours ''studied'''", sps.syntax(req, "pspp", "d.csv", "o.csv"))


class Readers(unittest.TestCase):
    def test_pspp_csv(self):
        text = (
            "Table: Coefficients\n"
            ",,B,Std. Error,t,Sig.\n"
            ",(Constant),4.000,.71,5.66,.000\n"
            ",Gender: Male,2.000,1.00,2.00,.073\n"
            "\n"
            "Note: something PSPP said\n"
        )
        tables, notes = sps.read_pspp_csv(text)
        self.assertEqual(tables[0]["title"], "Coefficients")
        self.assertEqual(tables[0]["head"], ["", "", "B", "Std. Error", "t", "Sig."])
        self.assertEqual(tables[0]["body"][1][1], "Gender: Male")
        self.assertEqual(notes, ["Note: something PSPP said"])

    def test_spss_tabtext(self):
        text = "Coefficients\n\tB\tStd. Error\tSig.\n(Constant)\t4.000\t.707\t.000\n\nWarnings\n"
        tables, notes = sps.read_spss_tabtext(text)
        self.assertEqual(tables[0]["title"], "Coefficients")
        self.assertEqual(tables[0]["body"], [["(Constant)", "4.000", ".707", ".000"]])
        self.assertEqual(notes, ["Warnings"])


@unittest.skipUnless(PSPP_FOUND.ready, "PSPP is not installed (set CLEW_TEST_PSPP)")
class PsppForReal(unittest.TestCase):
    def run_pspp(self, *args, **kwargs):
        req = request(*args, **kwargs)
        return backends.run_sps("pspp", PSPP_FOUND.path, COMMANDS[req["command"]], req, 120)

    def test_each_command_gives_tables(self):
        for args in (("regression.linear", ["hours"], ["gender"]), ("regression.logistic", ["passed"], ["hours"]),
                     ("test.fisher", ["gender"], ["passed"]), ("test.ranks", ["hours"], ["gender"]),
                     ("reliability.alpha", ["i1", "i2", "i3"], [])):
            r = self.run_pspp(*args)
            self.assertTrue(r["tables"], args)
            self.assertEqual(r["backend"]["name"], "PSPP")

    def test_the_regression_says_what_r_says(self):
        r = self.run_pspp("regression.linear", ["hours"], ["gender"])
        coefficients = next(t for t in r["tables"] if "oefficient" in t["title"])
        flat = " ".join(" ".join(row) for row in coefficients["body"])
        self.assertIn("Gender: Male", flat)
        # Male mean 41/6 minus female mean 23/6: exactly 3.
        self.assertRegex(flat, r"(^|\s)3\.0+(\s|$)")
