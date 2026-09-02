@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" bilibili_audio.py %*
) else (
  py -3 bilibili_audio.py %*
)
endlocal
