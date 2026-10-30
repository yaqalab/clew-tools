# Linear regression: one number explained by one or more columns (lm). Base R.

analyse <- function(d, spec) {
  y <- spec$dependent[1]
  f <- stats::reformulate(spec$independent, response = y)
  m <- stats::lm(f, data = d)
  s <- summary(m)
  co <- s$coefficients
  rows <- lapply(seq_len(nrow(co)), function(i) {
    c(clew_term(spec, rownames(co)[i]), clew_num(co[i, 1]), clew_num(co[i, 2]),
      clew_num(co[i, 3], 2), clew_p(co[i, 4]))
  })
  fs <- s$fstatistic
  note <- if (is.null(fs)) {
    sprintf("n = %d", stats::nobs(m))
  } else {
    p <- stats::pf(fs[1], fs[2], fs[3], lower.tail = FALSE)
    sprintf(
      "R2 = %s, adjusted R2 = %s; F(%d, %d) = %s, p %s; n = %d",
      clew_num(s$r.squared), clew_num(s$adj.r.squared), as.integer(fs[2]), as.integer(fs[3]),
      clew_num(fs[1], 2), sub("^([0-9])", "= \\1", clew_p(p)), stats::nobs(m)
    )
  }
  list(
    tables = list(clew_table(
      paste0("Linear regression of ", clew_label(spec, y)),
      c("Term", "Estimate", "SE", "t", "p"), rows
    )),
    notes = note,
    script = sprintf("summary(lm(%s, data = d))", paste(deparse(f), collapse = " "))
  )
}
