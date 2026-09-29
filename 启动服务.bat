@echo off & cd /d "%~dp0" & ".venv\Scripts\python.exe" -m taxassist serve --host 0.0.0.0 --port 8765 & pause
