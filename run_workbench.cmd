@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=D:\data\codex\短视频\OpenMontage\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [ERROR] Python runtime was not found:
  echo %PYTHON%
  pause
  exit /b 1
)

"%PYTHON%" "%~dp0server.py"
pause
