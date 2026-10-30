# Cronbach's alpha for a set of items (numbers, or ordered choices taken by their order), with
# each item's correlation with the rest and the alpha without it. Forms missing an item are left out.

analyse <- function(d, spec) {
  items <- spec$dependent
  m <- sapply(items, function(i) {
    v <- d[[i]]
    if (is.factor(v)) as.integer(v) else as.numeric(v)
  })
  m <- m[stats::complete.cases(m), , drop = FALSE]
  k <- ncol(m)
  if (k < 2) stop("alpha needs two items or more")
  if (nrow(m) < 2) stop("too few forms answered every item")
  alpha <- function(x) {
    kk <- ncol(x)
    kk / (kk - 1) * (1 - sum(apply(x, 2, stats::var)) / stats::var(rowSums(x)))
  }
  a <- alpha(m)
  rows <- lapply(seq_len(k), function(j) {
    rest <- rowSums(m[, -j, drop = FALSE])
    c(clew_label(spec, items[j]), clew_num(mean(m[, j]), 2), clew_num(stats::sd(m[, j]), 2),
      clew_num(stats::cor(m[, j], rest), 3), if (k > 2) clew_num(alpha(m[, -j, drop = FALSE])) else "")
  })
  list(
    tables = list(clew_table(
      "Items", c("Item", "Mean", "SD", "Correlation with the rest", "Alpha without it"), rows
    )),
    notes = sprintf("Cronbach's alpha = %s; %d items; n = %d", clew_num(a), k, nrow(m)),
    script = c(
      sprintf("m <- na.omit(sapply(d[c(%s)], as.numeric))",
        paste(vapply(items, clew_str, ""), collapse = ", ")),
      "ncol(m) / (ncol(m) - 1) * (1 - sum(apply(m, 2, var)) / var(rowSums(m)))"
    )
  )
}
