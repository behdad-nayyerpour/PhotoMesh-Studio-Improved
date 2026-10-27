@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Running first-time setup...
  call setup.bat || exit /b 1
)
if not exist .venv\Scripts\pythonw.exe (
  echo Setup did not create the Python environment.
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "main.py" %*
