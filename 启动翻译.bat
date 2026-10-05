@echo off
rem Start content translation. The script has a built-in time window
rem (default 11-19): outside the window it WAITS, inside it starts.
rem   --anytime        start now, ignore the window
rem   --only titles    translate titles only
rem
rem NOTE: keep this file ASCII-only. Chinese Windows cmd parses .bat as GBK,
rem so non-ASCII comments turn into garbage and can even be run as commands.
rem There is a test for this: tests/test_scripts.py::test_bat_files_are_ascii_only
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\translate_all.py --skip-titles
pause
