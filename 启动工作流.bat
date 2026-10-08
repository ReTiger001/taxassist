@echo off
rem ===================================================================
rem Daily unattended workflow: translate -> self-check -> self-fix
rem ===================================================================
rem
rem Runs around the clock (0-24) and only stops when you close the window.
rem Rounds take about 65 minutes each; results go to data/logs/auto_workflow.json
rem Live view: the progress-monitor .bat in the project root.
rem
rem NOTE: keep this file ASCII-only. Chinese Windows cmd parses .bat as
rem GBK, so non-ASCII comments turn into garbage and can even be run as
rem commands. There is a test for this:
rem tests/test_scripts.py::test_bat_files_are_ascii_only
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\auto_workflow.py --hours 0-24 --ai-review-limit 400 --retranslate-limit 50
pause
