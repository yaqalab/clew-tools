# Packs the server into one Windows .exe that needs no Python on the researcher's machine.
#
#   powershell -ExecutionPolicy Bypass -File tools/audio-server/build_exe.ps1
#
# Builds in a throwaway virtual environment so PyInstaller is not installed into your Python.
# Result: tools/audio-server/dist/clew-audio-server.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $env:TEMP "clew-audio-server-build"
if (-not (Test-Path $venv)) { python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

& $py -m PyInstaller --noconfirm --onefile --console --name clew-audio-server `
  --distpath dist --workpath (Join-Path $venv "work") --specpath (Join-Path $venv "spec") `
  clew_audio_server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $PSScriptRoot "dist\clew-audio-server.exe"
Write-Host "Built $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
