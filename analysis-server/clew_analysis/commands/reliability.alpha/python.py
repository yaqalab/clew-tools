"""Cronbach's alpha, the Python twin of r.R: the same items table and the same note."""

from clew_py import label, num, table


def alpha(m):
    k = m.shape[1]
    return k / (k - 1) * (1 - m.var(axis=0, ddof=1).sum() / m.sum(axis=1).var(ddof=1))


def analyse(d, spec):
    import pandas as pd

    items = spec.dependent
    cols = {}
    for i in items:
        v = d[i]
        cols[i] = (v.cat.codes.where(v.notna()).astype(float) + 1) if isinstance(v.dtype, pd.CategoricalDtype) \
            else v.astype(float)
    m = pd.DataFrame(cols).dropna()
    k = m.shape[1]
    if k < 2:
        raise ValueError("alpha needs two items or more")
    if len(m) < 2:
        raise ValueError("too few forms answered every item")
    rows = []
    for j in items:
        rest = m.drop(columns=j).sum(axis=1)
        rows.append([label(spec, j), num(m[j].mean(), 2), num(m[j].std(ddof=1), 2), num(m[j].corr(rest)),
                     num(alpha(m.drop(columns=j))) if k > 2 else ""])
    return {
        "tables": [table("Items", ["Item", "Mean", "SD", "Correlation with the rest", "Alpha without it"], rows)],
        "notes": [f"Cronbach's alpha = {num(alpha(m))}; {k} items; n = {len(m)}"],
        "script": [
            f"m = d[{items!r}].apply(pd.to_numeric).dropna()",
            "print(m.shape[1] / (m.shape[1] - 1) * (1 - m.var().sum() / m.sum(axis=1).var()))",
        ],
    }
