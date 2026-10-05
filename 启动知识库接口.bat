@echo off
rem Local read-only JSON API for AI tools. Default http://127.0.0.1:8766/
rem Binds to localhost only and never writes to the database.
rem NOTE: keep this file ASCII-only. Chinese Windows cmd parses .bat as GBK,
rem so non-ASCII comments turn into garbage and can even be run as commands.
rem There is a test for this: tests/test_scripts.py::test_bat_files_are_ascii_only
cd /d "%~dp0"
".venv\Scripts\python.exe" -m taxassist kb
pause
