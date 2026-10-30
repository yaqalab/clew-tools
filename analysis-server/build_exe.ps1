# Packs the analysis engine into a folder that needs no Python on the researcher's machine, for the
# Windows tray's Analysis tab (clew docs/36, section 5). R itself is not packed: the engine uses the
# researcher's own.
#
#   powershell -ExecutionPolicy Bypass -File tools/analysis-server/build_exe.ps1
#
# --onedir, as the dictation server: nothing is unpacked into Temp at each start. The command
# adapters (clew_analysis/commands) travel as data. Standard library only, so the folder is small.
#
# Builds in a throwaway virtual environment so PyInstaller is not installed into your Python.
# Result: tools/analysis-server/dist/clew-analysis-server/clew-analysis-server.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $env:TEMP "clew-analysis-server-build"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
  if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
  python -m venv $venv
}
& $py -m pip install --quiet --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$dist = Join-Path $PSScriptRoot "dist\clew-analysis-server"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }

# Not `email`: http.server needs it.
$exclude = @("tkinter", "sqlite3", "unittest", "pydoc", "xmlrpc", "multiprocessing", "asyncio")
$excludeArgs = $exclude | ForEach-Object { "--exclude-module", $_ }

& $py -m PyInstaller --noconfirm --clean --onedir --console --name clew-analysis-server `
  --distpath dist --workpath (Join-Path $venv "work") --specpath (Join-Path $venv "spec") `
  --paths $PSScriptRoot `
  --add-data "$(Join-Path $PSScriptRoot 'clew_analysis\commands');clew_analysis\commands" `
  @excludeArgs `
  clew_analysis_server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# What is packed: Python's own library (its licence) and PyInstaller's bootloader (GPL with an
# exception that allows this use). Both texts go beside the program.
$notices = Join-Path $dist "THIRD-PARTY-NOTICES.txt"
$pythonLicence = & $py -c "import sys, pathlib; p = pathlib.Path(sys.base_prefix, 'LICENSE.txt'); print(p if p.exists() else '')"
$pyinstallerLicence = & $py -c "import importlib.metadata as m; d = m.distribution('pyinstaller'); print(next((str(d.locate_file(f)) for f in (d.files or []) if 'COPYING' in f.name), ''))"
$text = @("Clew analysis server: third-party software packed in this folder.", "")
if ($pythonLicence) { $text += @("== Python (PSF License) ==", (Get-Content -Raw $pythonLicence), "") }
if ($pyinstallerLicence) { $text += @("== PyInstaller bootloader (GPL 2.0 with the PyInstaller exception) ==", (Get-Content -Raw $pyinstallerLicence)) }
Set-Content -Path $notices -Value ($text -join "`r`n") -Encoding utf8

$size = (Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "Built $dist ($([math]::Round($size / 1MB)) MB)"
