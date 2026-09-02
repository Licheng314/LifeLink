@echo off
setlocal EnableExtensions
title Life Link Tailscale Setup
set "MODULE_DIR=%~dp0.."
pushd "%MODULE_DIR%" >nul 2>&1
if errorlevel 1 (
  echo Unable to open the Life Link central service directory:
  echo   %MODULE_DIR%
  pause
  exit /b 1
)

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" configure_tailscale_endpoint.py
set "RESULT=%errorlevel%"
popd
echo.
if not "%RESULT%"=="0" echo Tailscale setup did not complete. Review the message above.
pause
exit /b %RESULT%
