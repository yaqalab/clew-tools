# Packs the local dictation server into a folder that needs no Python on the researcher's
# machine (clew docs/28, section 3.3).
#
#   powershell -ExecutionPolicy Bypass -File tools/stt-server/build_exe.ps1
#
# --onedir, not --onefile: CTranslate2's and FFmpeg's DLLs make it a few hundred MB, which
# --onefile would unpack into Temp at every start (and a Temp cleanup can remove while it runs).
# No models inside: `clew-stt-server download small` fetches one later.
#
# Builds in a throwaway virtual environment so PyInstaller is not installed into your Python.
# Result: tools/stt-server/dist/clew-stt-server/clew-stt-server.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $env:TEMP "clew-stt-server-build"
if (-not (Test-Path $venv)) { python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check pyinstaller -e ".[local]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Left out: the Hailo engine's imports (lazy, Pi only), onnxruntime (faster-whisper's own VAD,
# unused: the segmenter finds the speech), and the usual heavy strays.
$exclude = @("torch", "hailo_apps", "hailo_platform", "onnxruntime", "tkinter", "matplotlib",
             "PIL", "scipy", "pandas", "IPython", "webrtcvad", "pytest", "hf_xet", "sqlite3")
$excludeArgs = $exclude | ForEach-Object { "--exclude-module", $_ }

& $py -m PyInstaller --noconfirm --clean --onedir --console --name clew-stt-server `
  --distpath dist --workpath (Join-Path $venv "work") --specpath (Join-Path $venv "spec") `
  --collect-submodules uvicorn --collect-submodules websockets `
  --collect-binaries ctranslate2 --collect-data faster_whisper --collect-binaries sherpa_onnx `
  @excludeArgs `
  clew_stt_server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$dir = Join-Path $PSScriptRoot "dist\clew-stt-server"
# The licences of everything packed in, installed beside the program (third_party_notices.py).
& $py third_party_notices.py (Join-Path $dir "THIRD-PARTY-NOTICES.txt")
if ($LASTEXITCODE -ne 0) { throw "the third-party notices could not be written" }
$size = (Get-ChildItem $dir -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "Built $dir ($([math]::Round($size / 1MB)) MB)"
