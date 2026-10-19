# clew-stt-server — dictation on this computer

> **TL;DR** The stt.alefa.net speech server (`docs/27`), run on the researcher's Windows PC
> with faster-whisper on the CPU instead of a Hailo-8 on the Pi (`docs/28`). Same protocol, so
> Clew talks to it unchanged. It listens on 127.0.0.1 only: the audio never leaves the computer.
> The audio server's tray starts and stops it (its **Dictation** tab); you rarely run it by hand.
> Adapted from the author's stt.alefa.net server (`docs/provenance.md`), which runs on a
> Raspberry Pi with a Hailo-8 and keeps its own copy.

## What it adds to the Pi's server

- **Engine `whisper`:** faster-whisper, int8, Whisper's own 30 s window (a sentence is never cut).
- **Languages:** `?language=` on `/v1/listen` and `language` on `/v1/audio/transcriptions`;
  `/api/me` lists what the model knows (or only the one the tray fixed with `--language`).
- **Spelling hints:** `?prompt=` (and `prompt` for files), given to Whisper as its
  `initial_prompt`, ended like a sentence so Whisper keeps its punctuation. Capped at 400
  characters. The Pi's server ignores it.
- **`serve --local`:** binds 127.0.0.1, refuses a request whose `Host` is not this computer,
  accepts pages served from `http://localhost:*` and `http://127.0.0.1:*` besides `--origins`,
  and decodes with PyAV (FFmpeg's libraries, already a faster-whisper dependency), so no ffmpeg
  program is needed.
- **`serve --key-stdin`:** one key, `stt.<name>.<secret>`, read from the first line of stdin and
  held in memory. The tray makes it, seals it with DPAPI and hands it over at each start.
- **`download <model>` / `models`:** models live in `<models-dir>/<name>/`; the default folder
  is `%LOCALAPPDATA%\clew-stt\models`. A download writes `<name>.part` and renames it when
  complete, resumes file by file, and prints `total <bytes>`, then `progress N` once per percent,
  then `done <folder>` or `error <reason>`. `ALEFA_STT_HUB` points it at another address that
  answers like Hugging Face (the tray's tests).
- **`--device auto|cpu|cuda`:** `auto` uses an NVIDIA GPU when CUDA sees one (int8 with
  float16), else the processor (int8). A GPU whose libraries (cuBLAS, cuDNN) do not load falls
  back to the processor with a log line. `/api/me` says which (`device`). `system` prints
  `cuda N`, which the tray reads to grey out models that need a GPU.

| Model | Download | Use | Source |
|---|---|---|---|
| `tiny`, `base` | 78, 148 MB | live, least accurate | `Systran/faster-whisper-tiny`, `-base` |
| `small` | 486 MB | live dictation; measured word-perfect in English and French, Japanese with hints | `Systran/faster-whisper-small` |
| `medium` | 1.53 GB | an NVIDIA GPU | `Systran/faster-whisper-medium` |
| `large-v3-turbo` | 1.62 GB | files; about 8–10 s a sentence on a laptop processor | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` |
| `distil-large-v3` | 1.52 GB | files, English only | `Systran/faster-distil-whisper-large-v3` |
| `large-v3` | 3.09 GB | an NVIDIA GPU | `Systran/faster-whisper-large-v3` |
| `small.en` | 486 MB | English only; no better than `small` here (`28` §2); not in the tray's list | `Systran/faster-whisper-small.en` |

## Live transcription (clew docs/29)

`/v1/transcribe` (WebSocket): a recording's turns with their voices, worked through as fast as
the machine allows. `source=upload` (the client sends the file, held in memory) or
`source=folder&path=...` (read from `--audio-root`, the audio server's folder). `language=auto`
lets Whisper tell each piece's language. Speakers: 3D-Speaker CAM++ (Apache-2.0) through
`sherpa-onnx` (added for this; it replaces nothing and brings its own ONNX runtime), fetched
with `download speakers` into `<models-dir>/speakers/`. Messages: `transcribe.py`'s header.

## Run it by hand

```powershell
# once: a virtual environment with the server, faster-whisper and PyAV
python -m venv $env:TEMP\clew-stt-server-build
& $env:TEMP\clew-stt-server-build\Scripts\python.exe -m pip install -e "tools/stt-server[local,test]"

clew-stt-server download small
"stt.local.$(New-Guid)" | clew-stt-server serve --local --engine whisper --model small --port 47822 --key-stdin
clew-stt-server stream http://127.0.0.1:47822 talk.wav --key stt.local.… --language ja --prompt "協働学習, 第二言語"
```

## Build the Windows program

```powershell
powershell -ExecutionPolicy Bypass -File tools/stt-server/build_exe.ps1
```

Result: `tools/stt-server/dist/clew-stt-server/` (about 200 MB, `--onedir`). The native audio
server's installer ships it as an optional component. No model is inside.

**Dependencies** (`[local]`): `faster-whisper` (the engine; brings CTranslate2, tokenizers,
huggingface_hub) and `av` (decoding without ffmpeg). Left out of the build: `onnxruntime`
(faster-whisper's own VAD; the segmenter finds the speech), and everything the Hailo engine
imports.

## Tests

```powershell
& $env:TEMP\clew-stt-server-build\Scripts\python.exe -m pytest -q tools/stt-server
```

`tests/test_local.py` covers the local additions with the fake engine; the Pi's tests run
unchanged. Real models were checked by hand (`28` §8). Spelling hints are ended like a
sentence before Whisper sees them, because a bare list made it drop its full stops.
