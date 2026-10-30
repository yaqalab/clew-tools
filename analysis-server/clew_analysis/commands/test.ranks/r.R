# Groups compared by ranks: Mann-Whitney (Wilcoxon rank-sum) for two groups, Kruskal-Wallis for
# more. An ordered category is ranked by its order on the form.

analyse <- function(d, spec) {
  y <- spec$dependent[1]
  g <- spec$independent[1]
  v <- d[[y]]
  if (is.factor(v)) v <- as.integer(v)
  grp <- droplevels(factor(d[[g]]))
  keep <- !is.na(v) & !is.na(grp)
  v <- v[keep]
  grp <- droplevels(grp[keep])
  k <- nlevels(grp)
  if (k < 2) stop(sprintf("%s needs two groups or more here", clew_label(spec, g)))
  r <- rank(v)
  rows <- lapply(levels(grp), function(l) {
    c(l, sum(grp == l), clew_num(stats::median(v[grp == l]), 2), clew_num(mean(r[grp == l]), 2))
  })
  if (k == 2) {
    t <- stats::wilcox.test(v ~ grp, exact = FALSE)
    note <- sprintf("Mann-Whitney (Wilcoxon rank-sum): W = %s, p %s; n = %d",
      clew_num(t$statistic, 1), sub("^([0-9])", "= \\1", clew_p(t$p.value)), length(v))
    call <- sprintf("wilcox.test(%s ~ %s, data = d, exact = FALSE)", y, g)
  } else {
    t <- stats::kruskal.test(v, grp)
    note <- sprintf("Kruskal-Wallis: H = %s, df = %d, p %s; n = %d",
      clew_num(t$statistic, 2), as.integer(t$parameter),
      sub("^([0-9])", "= \\1", clew_p(t$p.value)), length(v))
    call <- sprintf("kruskal.test(%s ~ %s, data = d)", y, g)
  }
  list(
    tables = list(clew_table(
      paste0(clew_label(spec, y), " by ", clew_label(spec, g)),
      c(clew_label(spec, g), "n", "Median", "Mean rank"), rows
    )),
    notes = note,
    script = call
  )
}
