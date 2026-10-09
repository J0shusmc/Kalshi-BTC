@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-paper.ps1"
exit /b %ERRORLEVEL%
