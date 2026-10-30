# Clew analysis server

Runs analyses Clew does not do in the browser (regressions, exact and rank tests, reliability) in
**your own statistics program** (R, a Conda Python, PSPP or SPSS), on your own computer. It is a translator: Clew sends one neutral command
with the data ("linear regression, Dependent q1, Independent q2"), this server writes and runs the
R code for it, and sends one result back, which Clew shows, saves and exports like its own tables.
Every result carries the exact R script that ran, so the numbers can be checked outside Clew.

Clew never sends code. It can only choose one of the commands below, or one of your own scripts
when you turn a folder of them on.

## Start it

You need [R](https://cran.r-project.org) (4.0 or later). Nothing else: the commands use base R and
`MASS`, which comes with R.

**On Windows**, tick **Analysis** in the Clew audio server's installer: the engine is packed with
Python inside it (`build_exe.ps1`, about 18 MB), and the settings window's **Analysis** tab starts
it, shows which R it found, and keeps its key (encrypted for your Windows account). Elsewhere:

```
clew-analysis-server            # installed with pipx as part of clew-tools
python -m clew_analysis         # or from this folder
```

The first start makes a key and prints it. In Clew, open **Analysis**, then **Start › On this
computer › Connect**, and paste the address (`http://127.0.0.1:47823`) and the key.
`--show-key` prints the key again; it is kept in `key.txt` in the settings folder
(`%APPDATA%\clew-analysis-server` on Windows, `~/.config/clew-analysis-server` elsewhere),
readable by you only.

| Option | Does |
|---|---|
| `--port 47823` | The port (loopback only: nothing outside this computer can reach it) |
| `--backend r` | The program that runs the commands: `r`, `python` (a Conda environment), `pspp` or `spss` |
| `--python PATH` | A Python with pandas, numpy, scipy and statsmodels (default: a Conda environment that has them: one named `clew` first, then `base`) |
| `--pspp PATH` | `pspp` to use, when it is not on `PATH` or in Program Files |
| `--spss PATH` | SPSS Statistics' folder, or its own Python, when it is not in Program Files |
| `--rscript PATH` | Rscript, when it is not found on `PATH`, in the registry or in Program Files |
| `--scripts DIR` | A folder of your own R scripts (below) |
| `--timeout 120` | Seconds a run may take before it is stopped |
| `--allow-origin URL` | Another address Clew is served from (`https://clew.alefa.net` is allowed) |
| `--check` | Lists what it found and the commands, then stops |
| `--key-stdin` | Reads the key from the first line of standard input (how the Windows tray starts it) |

## The programs

| Program | What it needs | What comes back | Checked |
|---|---|---|---|
| **R** | R 4.0 or later; base R and `MASS` (shipped with R) | The tables below | Real R 4.3.3 (Windows), Linux in CI |
| **Python** (Conda) | A Python with pandas, numpy, scipy, statsmodels: `conda create -n clew pandas scipy statsmodels` | The same tables as R, the same numbers (checked against R on the same data) | pandas 3.0, scipy 1.18, statsmodels 0.15 |
| **PSPP** | PSPP 1.4 or later ([gnu.org/software/pspp](https://www.gnu.org/software/pspp)) | PSPP's own tables (Coefficients, Ranks, Chi-Square Tests...). No ordinal regression: PSPP has no PLUM | Real PSPP 2.0.0 (Linux) |
| **SPSS** | SPSS Statistics with its Python plug-in (its own Python, which can `import spss`) | SPSS's own tables, through OMS | **Not yet tried with a real SPSS**: written from IBM's documentation |

R and Python get the data on their standard input. PSPP and SPSS read files only, so their job
(the data, the syntax, the output) goes to a temporary folder that is deleted after the run.
Fisher's exact test in Python takes two-by-two tables only (scipy has no larger one); in PSPP and
SPSS (without the Exact Tests module) it is shown for two-by-two tables, beside the chi-square.

## Commands

| Command | Dependent | Independent |
|---|---|---|
| `regression.linear` | one number | one or more |
| `regression.logistic` | one category with two values | one or more |
| `regression.ordinal` (not in PSPP) | one category, taken in the form's order | one or more |
| `test.fisher` | one category | one category |
| `test.ranks` (Mann–Whitney, Kruskal–Wallis) | a number or ordered category | one category |
| `reliability.alpha` | two or more items | none |

Group by runs any of them once per group. Each command is a folder in `clew_analysis/commands`:
`command.json` (what it needs), `r.R` and `python.py` (the adapters, each defining
`analyse(d, spec)`). `clew.R` and `clew_py.py` are the shared parts: they read the command and the
data from standard input, build the data frame with each column's level and order, run the
adapter, and print the result. PSPP and SPSS share one translator, `clew_analysis/sps.py`, which
writes SPSS syntax and reads their tables back.

## Your own scripts

Put R files (run by R) or Python files (run by the Python backend) in a folder and start with
`--scripts DIR`. Each defines `analyse(d, spec)` and returns
`list(tables = list(clew_table(title, head, rows)), notes = "...")`. `d` is the data, one column per
variable sent, numbers as numbers and categories as factors; `spec$dependent`, `spec$independent`
and `spec$labels` say what was chosen. Optional header lines:

```r
#' title: Mean of each column
#' dependent: 1-5
#' independent: 0
```

## Privacy

Only the columns on Clew's board and the rows its filters keep are sent, to this program on your
computer, which hands them to R on its standard input: nothing is written to disk. Logs carry the
method, path and status of each request, never its content. Same guards as the audio server:
loopback only, the `Host` and `Origin` checked, the key compared in constant time, `no-store`.

## Tests

```
python -m unittest discover -s tests -v
```

They start a real server and, when R is installed, run every command in real R. The numbers are
checked against values worked out in Python and a textbook table, not against R itself.
