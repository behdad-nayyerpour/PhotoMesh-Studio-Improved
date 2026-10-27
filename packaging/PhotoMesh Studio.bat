@echo off
cd /d "%~dp0photomesh-studio"
if not exist .venv\Scripts\pythonw.exe (
  echo First start: running the offline installer...
  call "%~dp0install_offline.bat" || exit /b 1
)
start "" .venv\Scripts\pythonw.exe main.py %*
