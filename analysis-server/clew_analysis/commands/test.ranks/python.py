"""Groups compared by ranks (scipy), the Python twin of r.R: Mann-Whitney with R's continuity
correction for two groups (U of the first group is R's W), Kruskal-Wallis for more."""

from clew_py import label, num, p_said, table


def analyse(d, spec):
    import pandas as pd
    from scipy import stats

    y, g = spec.dependent[0], spec.independent[0]
    v = d[y]
    if isinstance(v.dtype, pd.CategoricalDtype):
        v = v.cat.codes.where(v.notna()).astype(float) + 1
    data = pd.DataFrame({"v": v.astype(float), "g": d[g]}).dropna()
    groups = [c for c in d[g].cat.categories if (data["g"] == c).any()]
    if len(groups) < 2:
        raise ValueError(f"{label(spec, g)} needs two groups or more here")
    ranks = data["v"].rank()
    rows = []
    for c in groups:
        sel = data["g"] == c
        rows.append([str(c), str(int(sel.sum())), num(data.loc[sel, "v"].median(), 2), num(ranks[sel].mean(), 2)])
    samples = [data.loc[data["g"] == c, "v"].to_numpy() for c in groups]
    if len(groups) == 2:
        t = stats.mannwhitneyu(samples[0], samples[1], alternative="two-sided", use_continuity=True,
                               method="asymptotic")
        note = (f"Mann-Whitney (Wilcoxon rank-sum): W = {num(t.statistic, 1)}, p {p_said(t.pvalue)}; "
                f"n = {len(data)}")
        call = "stats.mannwhitneyu(*samples, alternative='two-sided', method='asymptotic')"
    else:
        t = stats.kruskal(*samples)
        note = (f"Kruskal-Wallis: H = {num(t.statistic, 2)}, df = {len(groups) - 1}, p {p_said(t.pvalue)}; "
                f"n = {len(data)}")
        call = "stats.kruskal(*samples)"
    return {
        "tables": [table(f"{label(spec, y)} by {label(spec, g)}",
                         [label(spec, g), "n", "Median", "Mean rank"], rows)],
        "notes": [note],
        "script": [
            "from scipy import stats",
            f"samples = [x[{y!r}].dropna() for _, x in d.groupby({g!r}, observed=True)]",
            f"print({call})",
        ],
    }
