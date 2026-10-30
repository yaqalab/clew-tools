# Logistic regression: a two-value column explained by one or more columns (glm, binomial).

analyse <- function(d, spec) {
  y <- spec$dependent[1]
  d[[y]] <- droplevels(factor(d[[y]]))
  if (nlevels(d[[y]]) != 2) {
    stop(sprintf("%s needs exactly two values here; it has %d", clew_label(spec, y), nlevels(d[[y]])))
  }
  f <- stats::reformulate(spec$independent, response = y)
  m <- stats::glm(f, data = d, family = stats::binomial())
  co <- summary(m)$coefficients
  rows <- lapply(seq_len(nrow(co)), function(i) {
    c(clew_term(spec, rownames(co)[i]), clew_num(co[i, 1]), clew_num(co[i, 2]),
      clew_num(co[i, 3], 2), clew_p(co[i, 4]), clew_num(exp(co[i, 1])))
  })
  lv <- levels(d[[y]])
  list(
    tables = list(clew_table(
      paste0("Logistic regression of ", clew_label(spec, y)),
      c("Term", "Estimate", "SE", "z", "p", "Odds ratio"), rows
    )),
    notes = c(
      sprintf("The chance of \"%s\" rather than \"%s\".", lv[2], lv[1]),
      sprintf("n = %d; AIC = %s; residual deviance = %s on %d df",
        stats::nobs(m), clew_num(m$aic, 1), clew_num(m$deviance, 2), m$df.residual)
    ),
    script = sprintf(
      "summary(glm(%s, data = d, family = binomial()))", paste(deparse(f), collapse = " ")
    )
  )
}
