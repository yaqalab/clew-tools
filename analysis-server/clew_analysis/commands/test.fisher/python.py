"""Fisher's exact test (scipy), the Python twin of r.R, for two-by-two tables: scipy has no exact
test for larger ones, and an approximation would not be the test the researcher asked for."""

from clew_py import label, num, p_said, table


def analyse(d, spec):
    import pandas as pd
    from scipy import stats

    y, x = spec.dependent[0], spec.independent[0]
    rows_of = [c for c in d[y].cat.categories if (d[y] == c).any()]
    cols_of = [c for c in d[x].cat.categories if (d[x] == c).any()]
    if len(rows_of) < 2 or len(cols_of) < 2:
        raise ValueError("each column needs two values or more here")
    tab = pd.crosstab(d[y], d[x]).reindex(index=rows_of, columns=cols_of, fill_value=0)
    if tab.shape != (2, 2):
        raise ValueError(
            "Fisher's exact test in Python takes a two-by-two table; this one is "
            f"{tab.shape[0]} by {tab.shape[1]} (R runs larger ones)"
        )
    odds, p = stats.fisher_exact(tab.to_numpy())
    rows = [[str(r), *(str(int(v)) for v in tab.loc[r]), str(int(tab.loc[r].sum()))] for r in rows_of]
    rows.append(["All", *(str(int(v)) for v in tab.sum(axis=0)), str(int(tab.to_numpy().sum()))])
    return {
        "tables": [table(f"{label(spec, y)} by {label(spec, x)}",
                         [label(spec, y), *(str(c) for c in cols_of), "All"], rows)],
        "notes": [
            f"Fisher's exact test: p {p_said(p)}; n = {int(tab.to_numpy().sum())}",
            f"Odds ratio (sample) {num(odds, 2)}",
        ],
        "script": [
            "from scipy import stats",
            f"print(stats.fisher_exact(pd.crosstab(d[{y!r}], d[{x!r}]).to_numpy()))",
        ],
    }
