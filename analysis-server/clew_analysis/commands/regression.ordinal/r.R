# Ordinal regression: ordered categories explained by one or more columns (MASS::polr, which
# comes with every R). A one-choice column is ordered as its choices are listed on the form.

analyse <- function(d, spec) {
  y <- spec$dependent[1]
  d[[y]] <- droplevels(factor(d[[y]], levels = levels(d[[y]]), ordered = TRUE))
  if (nlevels(d[[y]]) < 3) {
    stop(sprintf("%s needs three ordered values or more here", clew_label(spec, y)))
  }
  f <- stats::reformulate(spec$independent, response = y)
  m <- MASS::polr(f, data = d, Hess = TRUE)
  co <- summary(m)$coefficients
  z <- co[, 1] / co[, 2]
  p <- 2 * stats::pnorm(-abs(z))
  nb <- length(m$coefficients)
  row <- function(i, name) {
    c(name, clew_num(co[i, 1]), clew_num(co[i, 2]), clew_num(z[i], 2), clew_p(p[i]))
  }
  terms <- lapply(seq_len(nb), function(i) {
    c(row(i, clew_term(spec, rownames(co)[i])), clew_num(exp(co[i, 1])))
  })
  cuts <- lapply(seq_len(nrow(co) - nb) + nb, function(i) row(i, rownames(co)[i]))
  list(
    tables = list(
      clew_table(
        paste0("Ordinal regression of ", clew_label(spec, y)),
        c("Term", "Estimate", "SE", "z", "p", "Odds ratio"), terms
      ),
      clew_table("Thresholds", c("Between", "Estimate", "SE", "z", "p"), cuts)
    ),
    notes = c(
      sprintf("Order: %s.", paste(levels(d[[y]]), collapse = " < ")),
      sprintf("n = %d; AIC = %s", stats::nobs(m), clew_num(stats::AIC(m), 1))
    ),
    script = c(
      sprintf("d$%s <- factor(d$%s, ordered = TRUE)", y, y),
      sprintf("summary(MASS::polr(%s, data = d, Hess = TRUE))", paste(deparse(f), collapse = " "))
    )
  )
}
