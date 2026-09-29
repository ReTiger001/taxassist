@echo off
REM Tailscale installer
REM Usage: right-click this file -> "Run as administrator"
REM
REM Keep this file ASCII-only. Chinese Windows parses .bat as GBK, and
REM UTF-8 Chinese text turns into garbage that cmd may try to run as commands.

msiexec /i "%~dp0tools\tailscale-setup-amd64.msi" /passive /norestart
echo.
echo Exit code: %ERRORLEVEL%   (0 = success, 1603 = fatal, 1625 = blocked by policy)
echo.
pause
