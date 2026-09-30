@echo off & cd /d "%~dp0" & ".venv\Scripts\python.exe" -m taxassist serve --host 127.0.0.1 --port 8765 --expose & pause
