@echo off
REM ===================================================================
REM Ollama launcher -- MUST set OLLAMA_MODELS before starting.
REM ===================================================================
REM
REM Why this file exists: the models live in D:\models, which is NOT
REM ollama's default location. Starting `ollama serve` without
REM OLLAMA_MODELS makes it scan only its own default dir, and the model
REM list comes back with a single entry -- the assistant then reports
REM "model not found" even though qwen2.5:14b-instruct is on disk.
REM This was hit for real: the memory of where models live said
REM D:\ollama-models, but that dir only holds hunyuan-mt; the real one
REM with qwen2.5 is D:\models. Hard-coding it here removes the guesswork.
REM
REM Usage: run this only when ollama is NOT already running -- it binds
REM 127.0.0.1:11434, so a second instance fails to bind. Verify with:
REM     "D:\longvideocrater\Ollama\ollama.exe" list
REM which should show qwen2.5:14b-instruct.
REM
REM Keep this file ASCII-only: cmd.exe parses .bat in the system
REM codepage (GBK on Chinese Windows) and non-ASCII bytes become
REM mojibake. tests/test_scripts.py::test_bat_files_are_ascii_only
REM enforces this.
setlocal

REM Where the models actually are (not the default dir).
set "OLLAMA_MODELS=D:\models"

REM Keep the model resident while the user is working. ollama's default
REM is 5 minutes; reloading a 9 GB model on every question is slow.
set "OLLAMA_KEEP_ALIVE=30m"

REM This one stays in the foreground, on purpose: if you double-clicked it you
REM almost certainly want to watch the log. The silent path (no window at all,
REM log to data\logs\) is the main launcher (the other .bat next to this file),
REM which starts ollama hidden and also brings up the web service and worker.
"D:\longvideocrater\Ollama\ollama.exe" serve

endlocal
