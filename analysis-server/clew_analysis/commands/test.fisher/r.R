# Fisher's exact test: two categories against each other, when cells are too small for a
# chi-square. Larger tables than R can enumerate fall back to a Monte Carlo p (100,000 draws).

analyse <- function(d, spec) {
  y <- spec$dependent[1]
  x <- spec$independent[1]
  tab <- table(droplevels(factor(d[[y]])), droplevels(factor(d[[x]])))
  if (nrow(tab) < 2 || ncol(tab) < 2) stop("each column needs two values or more here")
  simulated <- FALSE
  ft <- tryCatch(
    stats::fisher.test(tab, workspace = 2e7),
    error = function(e) {
      simulated <<- TRUE
      stats::fisher.test(tab, simulate.p.value = TRUE, B = 1e5)
    }
  )
  rows <- lapply(seq_len(nrow(tab)), function(i) c(rownames(tab)[i], as.character(tab[i, ]), sum(tab[i, ])))
  rows[[length(rows) + 1]] <- c("All", as.character(colSums(tab)), sum(tab))
  notes <- sprintf("Fisher's exact test: p %s; n = %d%s",
    sub("^([0-9])", "= \\1", clew_p(ft$p.value)), sum(tab),
    if (simulated) " (Monte Carlo, 100,000 draws)" else "")
  if (!is.null(ft$estimate)) {
    notes <- c(notes, sprintf("Odds ratio %s (95%% CI %s to %s)",
      clew_num(ft$estimate, 2), clew_num(ft$conf.int[1], 2), clew_num(ft$conf.int[2], 2)))
  }
  list(
    tables = list(clew_table(
      paste0(clew_label(spec, y), " by ", clew_label(spec, x)),
      c(clew_label(spec, y), colnames(tab), "All"), rows
    )),
    notes = notes,
    script = sprintf("fisher.test(table(d$%s, d$%s), workspace = 2e7)", y, x)
  )
}
