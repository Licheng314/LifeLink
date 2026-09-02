@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-apk.ps1" %*
exit /b %ERRORLEVEL%
