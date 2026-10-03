@echo off
rem 启动正文翻译。脚本自带时间窗（默认 11-19 点）：
rem   不在窗口内会**等待**，进入窗口自动开工；
rem   想立刻跑就加 --anytime。
rem 想只译标题用 --only titles；想忽略窗口用 --anytime。
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\translate_all.py --skip-titles
pause
