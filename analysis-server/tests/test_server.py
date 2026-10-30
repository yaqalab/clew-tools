"""
The analysis server against a real server on a free port and, when R is installed, real R:

    python -m unittest discover -s tools/analysis-server/tests -v

Numbers are checked against values worked out here independently (means, rank sums, Cronbach's
alpha) and a textbook table (Fisher's tea tasting, p = 0.4857), not against R itself.
"""

from __future__ import annotations

import http.client
import json
import statistics
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clew_analysis.backends import find_rscript  # noqa: E402
from clew_analysis.server import State, create_server  # noqa: E402

KEY = "test-key-not-a-real-one-0123456789"
HAS_R = find_rscript() is not None

HOURS = [2, 3, 3, 4, 5, 6, 4, 6, 7, 8, 7, 9]
GENDER = ["Female"] * 6 + ["Male"] * 6
YEAR = ["1", "2", "3"] * 4
PASSED = ["no", "no", "yes", "no", "yes", "yes", "no", "yes", "yes", "yes", "no", "yes"]
I1 = [1, 2, 3, 3, 4, 5, 2, 3, 4, 4, 5, 5]
I2 = [2, 2, 3, 4, 4, 5, 1, 3, 3, 5, 4, 5]
I3 = [1, 3, 2, 3, 5, 4, 2, 2, 4, 4, 5, 4]
ROWS = [
    {"hours": str(h), "gender": g, "year": y, "passed": p, "i1": str(a), "i2": str(b), "i3": str(c)}
    for h, g, y, p, a, b, c in zip(HOURS, GENDER, YEAR, PASSED, I1, I2, I3)
]
CODEBOOK = [
    {"name": "hours", "level": "scale", "label": "Hours of study"},
    {"name": "gender", "level": "nominal", "label": "Gender", "order": ["Female", "Male"]},
    {"name": "year", "level": "ordinal", "label": "Year", "order": ["1", "2", "3"]},
    {"name": "passed", "level": "nominal", "label": "Passed", "order": ["no", "yes"]},
    {"name": "i1", "level": "scale", "label": "Item 1"},
    {"name": "i2", "level": "scale", "label": "Item 2"},
    {"name": "i3", "level": "scale", "label": "Item 3"},
]


def run_body(command: str, dependent: list[str], independent: list[str], groupby=None, **extra):
    return {
        "command": command,
        "dependent": dependent,
        "independent": independent,
        "groupby": groupby or [],
        "codebook": extra.get("codebook", CODEBOOK),
        "rows": extra.get("rows", ROWS),
    }


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        scripts = Path(cls.tmp.name)
        (scripts / "Mean hours.R").write_text(
            "#' title: Mean of each column\n#' dependent: 1-5\n#' independent: 0\n"
            "analyse <- function(d, spec) {\n"
            "  rows <- lapply(spec$dependent, function(v) c(clew_label(spec, v), clew_num(mean(d[[v]], na.rm = TRUE), 2)))\n"
            "  list(tables = list(clew_table('Means', c('Column', 'Mean'), rows)), notes = 'mine')\n"
            "}\n",
            encoding="utf-8",
        )
        cls.state = State(KEY, "r", ["https://clew.alefa.net"], None, scripts, 120.0)
        cls.state.log = lambda line: None
        cls.server = create_server(cls.state, 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method: str, path: str, body=None, key: str | None = KEY, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if key:
            h["Authorization"] = f"Bearer {key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        raw = res.read()
        out = (res.status, json.loads(raw) if raw else None, dict(res.getheaders()))
        conn.close()
        return out

    # -- guards -----------------------------------------------------------------------------

    def test_ping_needs_no_key_and_says_what_it_is(self) -> None:
        status, body, _ = self.request("GET", "/api/ping", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["server"], "clew-analysis-server")
        self.assertEqual(body["backend"], "r")

    def test_everything_else_needs_the_key(self) -> None:
        self.assertEqual(self.request("GET", "/api/commands", key=None)[0], 401)
        self.assertEqual(self.request("GET", "/api/commands", key="wrong-key-0000000000")[0], 401)
        self.assertEqual(self.request("POST", "/api/run", run_body("test.fisher", [], []), key=None)[0], 401)

    def test_refuses_another_host_and_another_origin(self) -> None:
        self.assertEqual(self.request("GET", "/api/ping", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/ping", headers={"Origin": "https://evil.example"})[0], 403)
        status, _, headers = self.request("GET", "/api/ping", headers={"Origin": "https://clew.alefa.net"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://clew.alefa.net")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    # -- catalogue and checks ------------------------------------------------------------------

    def test_lists_the_shipped_commands_and_the_researchers_own(self) -> None:
        status, body, _ = self.request("GET", "/api/commands")
        self.assertEqual(status, 200)
        ids = {c["id"]: c for c in body["commands"]}
        for cid in ("regression.linear", "regression.logistic", "regression.ordinal",
                    "test.fisher", "test.ranks", "reliability.alpha", "user.mean-hours"):
            self.assertIn(cid, ids)
        self.assertTrue(ids["regression.linear"]["available"])
        self.assertEqual(ids["regression.linear"]["needs"]["dependent"]["levels"], ["scale"])
        self.assertEqual(ids["user.mean-hours"]["title"]["en"], "Mean of each column")
        self.assertTrue(ids["user.mean-hours"]["user"])

    def test_checks_a_request_before_any_program_sees_it(self) -> None:
        status, body, _ = self.request("POST", "/api/run", run_body("regression.linear", ["gender"], ["hours"]))
        self.assertEqual(status, 422)
        self.assertIn("scale", body["error"])
        bad = [dict(CODEBOOK[0], name="hours); system('x')")]
        status, body, _ = self.request("POST", "/api/run", run_body("regression.linear", ["hours"], ["hours"], codebook=bad))
        self.assertEqual(status, 422)
        self.assertIn("not allowed", body["error"])
        status, body, _ = self.request("POST", "/api/run", dict(run_body("test.fisher", ["gender"], ["passed"]), command="system"))
        self.assertEqual(status, 422)

    def test_lists_backends_without_paths(self) -> None:
        status, body, _ = self.request("GET", "/api/backends")
        self.assertEqual(status, 200)
        self.assertEqual(body["selected"], "r")
        self.assertEqual([b["id"] for b in body["backends"]], ["r", "python", "pspp", "spss"])
        self.assertNotIn("path", body["backends"][0])

    # -- R ------------------------------------------------------------------------------------

    def run_ok(self, body):
        status, result, _ = self.request("POST", "/api/run", body)
        self.assertEqual(status, 200, result)
        return result

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_linear_regression_gives_the_group_means(self) -> None:
        r = self.run_ok(run_body("regression.linear", ["hours"], ["gender"]))
        table = r["tables"][0]
        self.assertEqual(table["head"], ["Term", "Estimate", "SE", "t", "p"])
        rows = {row[0]: row for row in table["body"]}
        female = statistics.mean(HOURS[:6])
        male = statistics.mean(HOURS[6:])
        self.assertEqual(rows["(Intercept)"][1], f"{female:.3f}")
        self.assertEqual(rows["Gender: Male"][1], f"{male - female:.3f}")
        self.assertIn("n = 12", r["notes"][0])
        self.assertIn("lm(hours ~ gender", r["script"])
        self.assertEqual(r["backend"]["name"], "R")
        self.assertEqual(r["command"], {"id": "regression.linear", "version": 1})

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_fisher_matches_the_tea_tasting_table(self) -> None:
        rows = [{"guess": g, "truth": t} for g, t, n in
                [("milk", "milk", 3), ("milk", "tea", 1), ("tea", "milk", 1), ("tea", "tea", 3)]
                for _ in range(n)]
        codebook = [{"name": "guess", "level": "nominal"}, {"name": "truth", "level": "nominal"}]
        r = self.run_ok(run_body("test.fisher", ["guess"], ["truth"], codebook=codebook, rows=rows))
        self.assertIn("p = 0.486", r["notes"][0])
        self.assertEqual(r["tables"][0]["body"][-1], ["All", "4", "4", "8"])

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_ranks_give_the_rank_sum_statistic(self) -> None:
        r = self.run_ok(run_body("test.ranks", ["hours"], ["gender"]))
        ranked = sorted(HOURS)
        rank = {v: (ranked.index(v) + 1 + len(ranked) - ranked[::-1].index(v)) / 2 for v in HOURS}
        w = sum(rank[h] for h in HOURS[:6]) - 6 * 7 / 2
        self.assertIn(f"W = {w:.1f}", r["notes"][0])
        self.assertEqual(r["tables"][0]["head"], ["Gender", "n", "Median", "Mean rank"])
        three = self.run_ok(run_body("test.ranks", ["hours"], ["year"]))
        self.assertIn("Kruskal-Wallis", three["notes"][0])

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_alpha_matches_the_formula(self) -> None:
        r = self.run_ok(run_body("reliability.alpha", ["i1", "i2", "i3"], []))
        items = [I1, I2, I3]
        total = [sum(x) for x in zip(*items)]
        k = 3
        alpha = k / (k - 1) * (1 - sum(statistics.variance(i) for i in items) / statistics.variance(total))
        self.assertIn(f"alpha = {alpha:.3f}", r["notes"][0])
        self.assertEqual(len(r["tables"][0]["body"]), 3)

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_logistic_and_ordinal_models_run_and_name_their_terms(self) -> None:
        lg = self.run_ok(run_body("regression.logistic", ["passed"], ["hours"]))
        self.assertEqual(lg["tables"][0]["head"][-1], "Odds ratio")
        self.assertIn('"yes" rather than "no"', lg["notes"][0])
        od = self.run_ok(run_body("regression.ordinal", ["year"], ["hours", "gender"]))
        terms = [row[0] for row in od["tables"][0]["body"]]
        self.assertEqual(terms, ["Hours of study", "Gender: Male"])
        self.assertEqual(od["tables"][1]["title"], "Thresholds")
        self.assertIn("1 < 2 < 3", od["notes"][0])

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_group_by_runs_once_per_group(self) -> None:
        r = self.run_ok(run_body("test.ranks", ["hours"], ["passed"], ["gender"]))
        titles = [t["title"] for t in r["tables"]]
        self.assertEqual(len(titles), 2)
        self.assertTrue(titles[0].endswith(" [Gender=Female]"), titles)
        self.assertTrue(titles[1].endswith(" [Gender=Male]"), titles)

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_an_r_error_comes_back_in_words(self) -> None:
        status, body, _ = self.request("POST", "/api/run", run_body("regression.logistic", ["year"], ["hours"]))
        self.assertEqual(status, 422)
        self.assertIn("exactly two values", body["error"])

    @unittest.skipUnless(HAS_R, "R is not installed")
    def test_runs_the_researchers_own_script_and_keeps_non_ascii(self) -> None:
        codebook = [dict(CODEBOOK[0], label="勉強時間")]
        rows = [{"hours": str(h)} for h in HOURS]
        r = self.run_ok(run_body("user.mean-hours", ["hours"], [], codebook=codebook, rows=rows))
        self.assertEqual(r["tables"][0]["body"], [["勉強時間", f"{statistics.mean(HOURS):.2f}"]])
        self.assertEqual(r["notes"], ["mine"])


if __name__ == "__main__":
    unittest.main()
