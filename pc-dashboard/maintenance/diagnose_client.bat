@echo off
setlocal
cd /d "%~dp0.."

set "PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"

"%PYTHON%" diagnose_client.py
echo.
pause

