# Clew analysis: the part every R adapter shares (docs/36, section 5.2). Base R only.
#
#   Rscript --vanilla clew.R <adapter.R>
#
# Standard input (UTF-8), written by the analysis server from Clew's neutral command:
#
#   command<TAB>regression.linear
#   dependent<TAB>q1                       one line per input, its names after the key
#   independent<TAB>q2<TAB>age_band
#   groupby<TAB>school
#   option<TAB>name<TAB>value
#   level<TAB>q2<TAB>nominal               scale, nominal, ordinal or text
#   label<TAB>q2<TAB>Gender                what the column is called in tables
#   order<TAB>q2<TAB>Female<TAB>Male       a category's values, in the form's order
#   ---data---
#   q1,q2,age_band,school                  CSV, every value as text, empty when missing
#
# The adapter defines `analyse(d, spec)`, returning list(tables = ..., notes = ..., script = ...).
# This file reads the input, builds the data frame, runs the adapter once per group, and prints
# one JSON result between two marker lines on standard output. Nothing is written to disk.

clew_str <- function(s) {
  s <- enc2utf8(as.character(s))
  s[is.na(s)] <- ""
  s <- gsub("\\", "\\\\", s, fixed = TRUE)
  s <- gsub("\"", "\\\"", s, fixed = TRUE)
  s <- gsub("\n", "\\n", s, fixed = TRUE)
  s <- gsub("\r", "\\r", s, fixed = TRUE)
  s <- gsub("\t", "\\t", s, fixed = TRUE)
  s <- gsub("[[:cntrl:]]", " ", s)
  paste0("\"", s, "\"")
}

# A list with names is an object, without names an array; one value alone is a scalar. Arrays of
# one are made with as.list().
clew_json <- function(x) {
  if (is.null(x)) return("null")
  if (is.list(x)) {
    n <- names(x)
    if (!is.null(n) && length(n) > 0 && all(n != "")) {
      parts <- vapply(n, function(k) paste0(clew_str(k), ":", clew_json(x[[k]])), "")
      return(paste0("{", paste(parts, collapse = ","), "}"))
    }
    return(paste0("[", paste(vapply(x, clew_json, ""), collapse = ","), "]"))
  }
  if (is.logical(x) && length(x) == 1) return(if (isTRUE(x)) "true" else "false")
  if (is.numeric(x) && length(x) == 1) return(if (is.finite(x)) format(x, digits = 15) else "null")
  if (length(x) == 1) return(clew_str(x))
  paste0("[", paste(vapply(as.list(x), clew_json, ""), collapse = ","), "]")
}

# Numbers as tables show them: fixed decimals, empty when missing.
clew_num <- function(x, digits = 3) {
  ifelse(is.na(x) | !is.finite(x), "", formatC(x, format = "f", digits = digits))
}

clew_p <- function(p) {
  ifelse(is.na(p), "", ifelse(p < 0.001, "< 0.001", formatC(p, format = "f", digits = 3)))
}

# A table: a title, a header, rows of text. `rows` is a list of character vectors.
clew_table <- function(title, head, rows, stub = 1) {
  list(
    title = title,
    head = as.list(as.character(head)),
    body = lapply(rows, function(r) as.list(as.character(r))),
    stub = stub
  )
}

clew_label <- function(spec, name) {
  l <- spec$labels[[name]]
  if (is.null(l) || l == "") name else l
}

# A model's term as a reader knows it: "q2Male" -> "Gender: Male".
clew_term <- function(spec, term) {
  if (term == "(Intercept)") return("(Intercept)")
  for (v in c(spec$independent, spec$dependent)) {
    if (startsWith(term, v)) {
      rest <- substring(term, nchar(v) + 1)
      return(if (rest == "") clew_label(spec, v) else paste0(clew_label(spec, v), ": ", rest))
    }
  }
  term
}

clew_read <- function() {
  con <- file("stdin", encoding = "UTF-8")
  lines <- readLines(con, warn = FALSE)
  close(con)
  cut <- match("---data---", lines)
  if (is.na(cut)) stop("the input has no data")
  spec <- list(
    command = "", dependent = character(), independent = character(), groupby = character(),
    options = list(), levels = list(), labels = list(), order = list()
  )
  for (line in lines[seq_len(cut - 1)]) {
    f <- strsplit(line, "\t", fixed = TRUE)[[1]]
    key <- f[1]
    rest <- f[-1]
    if (key == "command") spec$command <- rest[1]
    else if (key %in% c("dependent", "independent", "groupby")) spec[[key]] <- rest
    else if (key == "option") spec$options[[rest[1]]] <- if (length(rest) > 1) rest[2] else ""
    else if (key == "level") spec$levels[[rest[1]]] <- rest[2]
    else if (key == "label") spec$labels[[rest[1]]] <- if (length(rest) > 1) rest[2] else ""
    else if (key == "order") spec$order[[rest[1]]] <- rest[-1]
  }
  text <- lines[-seq_len(cut)]
  d <- utils::read.csv(
    text = paste(text, collapse = "\n"), colClasses = "character", na.strings = "",
    check.names = FALSE, encoding = "UTF-8"
  )
  for (name in names(d)) {
    level <- spec$levels[[name]]
    if (is.null(level)) next
    if (level == "scale") {
      d[[name]] <- suppressWarnings(as.numeric(d[[name]]))
    } else if (level %in% c("nominal", "ordinal")) {
      values <- spec$order[[name]]
      if (is.null(values)) values <- sort(unique(stats::na.omit(d[[name]])))
      d[[name]] <- factor(d[[name]], levels = values, ordered = level == "ordinal")
    }
  }
  list(spec = spec, data = d)
}

# The lines a reader would run to rebuild `d` from Clew's Export for analysis: data-labels.csv
# holds the choices' labels, which is what the engine was sent.
clew_prelude <- function(spec, d) {
  out <- c(
    "# data-labels.csv: from Survey entry's Export for analysis",
    paste0(
      'd <- read.csv("data-labels.csv", fileEncoding = "UTF-8-BOM", colClasses = "character", ',
      'na.strings = "", check.names = FALSE)'
    )
  )
  for (name in names(d)) {
    level <- spec$levels[[name]]
    if (is.null(level)) next
    if (level == "scale") {
      out <- c(out, sprintf("d$%s <- as.numeric(d$%s)", name, name))
    } else if (level %in% c("nominal", "ordinal")) {
      values <- paste(vapply(levels(d[[name]]), clew_str, ""), collapse = ", ")
      out <- c(out, sprintf(
        "d$%s <- factor(d$%s, levels = c(%s)%s)", name, name, values,
        if (level == "ordinal") ", ordered = TRUE" else ""
      ))
    }
  }
  out
}

local({
  adapter <- commandArgs(trailingOnly = TRUE)[1]
  result <- tryCatch({
    input <- clew_read()
    spec <- input$spec
    d <- input$data
    source(adapter, local = FALSE, encoding = "UTF-8")
    warned <- character()
    run <- function(part) {
      withCallingHandlers(
        analyse(part, spec),
        warning = function(w) {
          warned <<- c(warned, conditionMessage(w))
          invokeRestart("muffleWarning")
        }
      )
    }
    tables <- list()
    notes <- character()
    script <- clew_prelude(spec, d)
    if (length(spec$groupby) == 0) {
      out <- run(d)
      tables <- out$tables
      notes <- out$notes
      script <- c(script, out$script)
    } else {
      keys <- interaction(d[spec$groupby], drop = TRUE, sep = " / ", lex.order = TRUE)
      for (k in levels(keys)) {
        part <- d[!is.na(keys) & keys == k, , drop = FALSE]
        where <- paste(vapply(spec$groupby, function(g) {
          paste0(clew_label(spec, g), "=", as.character(part[[g]][1]))
        }, ""), collapse = " and ")
        out <- tryCatch(run(part), error = function(e) {
          list(tables = list(), notes = paste0("[", where, "] ", conditionMessage(e)))
        })
        for (t in out$tables) {
          t$title <- paste0(t$title, " [", where, "]")
          tables[[length(tables) + 1]] <- t
        }
        if (length(out$notes) > 0) notes <- c(notes, paste0("[", where, "] ", out$notes))
        if (is.null(out$script)) next
        script <- c(script, sprintf("# %s", where), out$script)
      }
    }
    list(
      tables = tables,
      notes = as.list(c(notes, unique(warned))),
      figures = list(),
      script = paste(script, collapse = "\n"),
      backend = list(name = "R", version = paste(R.version$major, R.version$minor, sep = "."))
    )
  }, error = function(e) list(error = conditionMessage(e)))
  cat("<<<CLEW-RESULT>>>\n", clew_json(result), "\n<<<CLEW-END>>>\n", sep = "")
})
