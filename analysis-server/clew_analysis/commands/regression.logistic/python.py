"""Logistic regression (statsmodels Logit), the Python twin of r.R."""

import math

from clew_py import design, label, num, pval, table


def analyse(d, spec):
    import statsmodels.api as sm

    y = spec.dependent[0]
    present = [c for c in d[y].cat.categories if (d[y] == c).any()]
    if len(present) != 2:
        raise ValueError(f"{label(spec, y)} needs exactly two values here; it has {len(present)}")
    x, names = design(d, spec, spec.independent)
    data = x.assign(__y=(d[y] == present[1]).astype(float).where(d[y].notna())).dropna()
    model = sm.Logit(data["__y"], sm.add_constant(data.drop(columns="__y"), has_constant="add")).fit(disp=0)
    rows = []
    for i, term in enumerate(["(Intercept)", *names]):
        est = model.params.iloc[i]
        rows.append([term, num(est), num(model.bse.iloc[i]), num(model.tvalues.iloc[i], 2),
                     pval(model.pvalues.iloc[i]), num(math.exp(est))])
    return {
        "tables": [table(f"Logistic regression of {label(spec, y)}",
                         ["Term", "Estimate", "SE", "z", "p", "Odds ratio"], rows)],
        "notes": [
            f'The chance of "{present[1]}" rather than "{present[0]}".',
            f"n = {int(model.nobs)}; AIC = {num(model.aic, 1)}; residual deviance = {num(-2 * model.llf, 2)} "
            f"on {int(model.df_resid)} df",
        ],
        "script": [
            "import statsmodels.formula.api as smf",
            f"d['__y'] = (d[{y!r}] == {present[1]!r}).astype(float)",
            f"print(smf.logit({'__y ~ ' + ' + '.join(spec.independent)!r}, data=d).fit().summary())",
        ],
    }
