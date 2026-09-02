@echo off
setlocal
cd /d "%~dp0.."

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo ========================================
echo   Life Link Central - Public Endpoint
echo ========================================
echo.
echo The PeanutHull mapping must point to:
echo   the host and port in central config.json
echo   default: 127.0.0.1:8091
echo.
set /p "PUBLIC_URL=Enter the public HTTPS URL: "
if not defined PUBLIC_URL exit /b 2

"%PYTHON_EXE%" central_endpoint.py configure --provider peanuthull --url "%PUBLIC_URL%"
if errorlevel 1 (
  echo.
  echo Configuration failed. Check that Central mode is running and the mapping uses its configured port.
  pause
  exit /b 1
)

echo.
echo Public central endpoint verified and saved.
pause

