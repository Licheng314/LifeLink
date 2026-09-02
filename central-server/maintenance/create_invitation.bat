@echo off
setlocal

set "PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"

"%PYTHON%" "%~dp0..\central_invitation.py"
if errorlevel 1 (
    echo.
    echo Failed to create the device invitation.
    pause
    exit /b 1
)

echo.
echo The one-line invitation was copied to the clipboard when available.
echo It can be claimed once by either a PC or a phone and expires after 24 hours.
pause

