"""Ordinal regression (statsmodels OrderedModel, logit link), the Python twin of r.R's MASS::polr:
the same model, P(Y <= k) = F(threshold_k - x'b), so estimates and thresholds match polr's."""

import math

from clew_py import design, label, num, pval, table


def analyse(d, spec):
    import numpy as np
    import pandas as pd
    from scipy import stats
    from statsmodels.miscmodels.ordinal_model import OrderedModel

    y = spec.dependent[0]
    present = [c for c in d[y].cat.categories if (d[y] == c).any()]
    if len(present) < 3:
        raise ValueError(f"{label(spec, y)} needs three ordered values or more here")
    x, names = design(d, spec, spec.independent)
    yy = pd.Series(pd.Categorical(d[y], categories=present, ordered=True), index=d.index)
    data = x.assign(__y=yy).dropna()
    model = OrderedModel(data["__y"], data.drop(columns="__y"), distr="logit")
    fit = model.fit(method="bfgs", disp=0, maxiter=2000)
    nb = len(names)
    params = np.asarray(fit.params)
    bse = np.asarray(fit.bse)
    terms = []
    for i, name in enumerate(names):
        z = params[i] / bse[i]
        terms.append([name, num(params[i]), num(bse[i]), num(z, 2), pval(2 * stats.norm.sf(abs(z))),
                      num(math.exp(params[i]))])
    # The thresholds as polr reports them; their standard errors through the delta method.
    raw = params[nb:]
    cuts = model.transform_threshold_params(params)[1:-1]
    cov = np.asarray(fit.cov_params())[nb:, nb:]
    jac = np.zeros((len(raw), len(raw)))
    for k in range(len(raw)):
        jac[k, 0] = 1.0
        for j in range(1, k + 1):
            jac[k, j] = math.exp(raw[j])
    cut_se = np.sqrt(np.diag(jac @ cov @ jac.T))
    rows = []
    for k in range(len(cuts)):
        z = cuts[k] / cut_se[k]
        rows.append([f"{present[k]}|{present[k + 1]}", num(cuts[k]), num(cut_se[k]), num(z, 2),
                     pval(2 * stats.norm.sf(abs(z)))])
    k_params = len(params)
    aic = -2 * fit.llf + 2 * k_params
    return {
        "tables": [
            table(f"Ordinal regression of {label(spec, y)}", ["Term", "Estimate", "SE", "z", "p", "Odds ratio"], terms),
            table("Thresholds", ["Between", "Estimate", "SE", "z", "p"], rows),
        ],
        "notes": [f"Order: {' < '.join(str(p) for p in present)}.", f"n = {int(fit.nobs)}; AIC = {num(aic, 1)}"],
        "script": [
            "from statsmodels.miscmodels.ordinal_model import OrderedModel",
            f"print(OrderedModel.from_formula({y + ' ~ 0 + ' + ' + '.join(spec.independent)!r}, data=d, "
            "distr='logit').fit(method='bfgs').summary())",
        ],
    }
