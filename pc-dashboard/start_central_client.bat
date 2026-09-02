@echo off
setlocal EnableExtensions
title Life Link PC Client

for %%I in ("%~dp0..\development\tools\bootstrap_windows.ps1") do set "BOOTSTRAP=%%~fI"
if not exist "%BOOTSTRAP%" (
    echo.
    echo Life Link bootstrap script was not found:
    echo   %BOOTSTRAP%
    echo.
    pause
    exit /b 2
)

set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%POWERSHELL_EXE%" set "POWERSHELL_EXE=powershell.exe"

"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%" -Role pc
set "LIFELINK_EXIT=%ERRORLEVEL%"
if not "%LIFELINK_EXIT%"=="0" (
    echo.
    echo Life Link setup or startup failed with exit code %LIFELINK_EXIT%.
    echo Review the error above, then press any key to close this window.
    pause >nul
)
exit /b %LIFELINK_EXIT%
