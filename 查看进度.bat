@echo off
rem ===================================================================
rem Live progress monitor for the translation workflow
rem ===================================================================
rem
rem Read-only window: it shows what the workflow is doing right now.
rem Closing THIS window does NOT stop the translation.
rem
rem Shows: current step, batch progress, total progress, AI review
rem stats, recently finished items. Refreshes every 3 seconds.
rem
rem NOTE: keep this file ASCII-only. Chinese Windows cmd parses .bat as
rem GBK, so non-ASCII comments turn into garbage and can even be run as
rem commands. There is a test for this:
rem tests/test_scripts.py::test_bat_files_are_ascii_only
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\watch.py
pause
