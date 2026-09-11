@echo off
setlocal
set "KEY_FILE=%USERPROFILE%\.ssh\lifelink-aliyun"
set "SSH_EXE=%SystemRoot%\System32\OpenSSH\ssh.exe"
set "SERVER_HOST=YOUR_SERVER_HOST"

if "%SERVER_HOST%"=="YOUR_SERVER_HOST" (
  echo Set SERVER_HOST before using this template.
  pause
  exit /b 1
)

if not exist "%KEY_FILE%" (
  echo SSH key not found: %KEY_FILE%
  pause
  exit /b 1
)
if not exist "%SSH_EXE%" (
  echo OpenSSH client not found: %SSH_EXE%
  pause
  exit /b 1
)

start "" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:18092'"
"%SSH_EXE%" -i "%KEY_FILE%" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -N -L 18092:127.0.0.1:18092 root@%SERVER_HOST%

if errorlevel 1 pause
