"""Linear regression (statsmodels OLS), the Python twin of r.R: the same table, the same note."""

from clew_py import design, label, num, p_said, pval, table


def analyse(d, spec):
    import statsmodels.api as sm

    y = spec.dependent[0]
    x, names = design(d, spec, spec.independent)
    data = x.assign(__y=d[y].astype(float)).dropna()
    model = sm.OLS(data["__y"], sm.add_constant(data.drop(columns="__y"), has_constant="add")).fit()
    rows = []
    for i, term in enumerate(["(Intercept)", *names]):
        rows.append([term, num(model.params.iloc[i]), num(model.bse.iloc[i]), num(model.tvalues.iloc[i], 2),
                     pval(model.pvalues.iloc[i])])
    note = (
        f"R2 = {num(model.rsquared)}, adjusted R2 = {num(model.rsquared_adj)}; "
        f"F({int(model.df_model)}, {int(model.df_resid)}) = {num(model.fvalue, 2)}, p {p_said(model.f_pvalue)}; "
        f"n = {int(model.nobs)}"
    )
    return {
        "tables": [table(f"Linear regression of {label(spec, y)}", ["Term", "Estimate", "SE", "t", "p"], rows)],
        "notes": [note],
        "script": [
            "import statsmodels.formula.api as smf",
            f"print(smf.ols({y + ' ~ ' + ' + '.join(spec.independent)!r}, data=d).fit().summary())",
        ],
    }
