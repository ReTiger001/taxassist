@echo off
REM ===================================================================
REM Taxassist launcher -- starts ollama, the web service, and the worker.
REM ===================================================================
REM
REM Run this whenever you want the system up. NOTHING here starts on boot:
REM that is deliberate -- you decide when it runs.
REM
REM Each component gets its own window, so you can watch its log and close
REM it individually to stop just that part.
REM
REM Already-running components are skipped (checked by port / by the write
REM lock), so double-clicking twice does no harm.
REM
REM Keep this file ASCII-only: cmd.exe parses .bat in the system codepage
REM (GBK on Chinese Windows) and non-ASCII bytes become mojibake.
REM tests/test_scripts.py::test_bat_files_are_ascii_only enforces this.
setlocal

set "PY=D:\EY-project\.venv\Scripts\python.exe"
set "OLLAMA=D:\longvideocrater\Ollama\ollama.exe"
cd /d "D:\EY-project"

echo ============================================
echo   Taxassist - starting
echo ============================================
echo.

REM --- 1) ollama --------------------------------------------------------
REM The models live in D:\models, which is NOT ollama's default directory.
REM Without OLLAMA_MODELS it scans only its own default dir and the model
REM list comes back nearly empty -- the assistant then reports "model not
REM found" even though the model is on disk. This was hit for real.
netstat -ano | findstr ":11434" >nul 2>&1
if %errorlevel%==0 (
  echo [skip]  ollama already listening on 11434
) else (
  echo [start] ollama  ^(models: D:\models^)
  start "ollama" cmd /k "set OLLAMA_MODELS=D:\models && set OLLAMA_KEEP_ALIVE=30m && "%OLLAMA%" serve"
)

REM --- 2) web service ---------------------------------------------------
REM --expose turns on the login wall (required whenever it is reachable
REM from outside this machine).
netstat -ano | findstr ":8765" >nul 2>&1
if %errorlevel%==0 (
  echo [skip]  web service already listening on 8765
) else (
  echo [start] web service  ^(http://127.0.0.1:8765^)
  start "taxassist-web" cmd /k ""%PY%" -m taxassist serve --host 127.0.0.1 --port 8765 --expose"
)

REM --- 3) worker --------------------------------------------------------
REM fetch/publish/verify only. translate is deliberately EXCLUDED: it has
REM an 11:00-19:00 window (scripts/translate_all.py) and the worker's
REM translate stage does not honour it, so including it would violate the
REM rule that translation only runs in the user's off hours.
REM
REM Skip if one is already running: a second worker would just fight for
REM the write lock and sit idle, which reads as a fault to whoever is
REM watching the windows. (The lock would keep the data safe, but a
REM silently idle window is a bad thing to hand someone.)
REM
REM **Do NOT use wmic here.** Modern Windows ships without it (removed in
REM Windows 11), and with its error swallowed by 2^>nul the whole script
REM just dies silently -- which is exactly what happened the first time
REM this launcher was tested: output stopped right before this line and
REM the exit code was 255. PowerShell takes ~1-2s to start but is there.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -match 'taxassist worker' }) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [skip]  worker already running
) else (
  echo [start] worker  ^(fetch,publish,verify; translate excluded^)
  start "taxassist-worker" cmd /k ""%PY%" -m taxassist worker --stage fetch,publish,verify"
)

echo.
echo Three windows should be opening above ^(or reported as skipped^).
echo.
echo   Web UI  :  http://127.0.0.1:8765/
echo   Health  :  http://127.0.0.1:8765/health     ^<-- check here if
echo                                                 something seems off
echo.
echo Close any window to stop that part.
echo Nothing here runs at boot -- start this file when you need it.
echo.
pause
