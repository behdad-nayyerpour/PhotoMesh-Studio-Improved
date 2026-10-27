@echo off
REM Usage: run_tests.bat [folder_with_photos]  (folder enables the real COLMAP end-to-end tests)
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat call setup.bat
call .venv\Scripts\activate.bat
pip install -q -r requirements-dev.txt
if not "%~1"=="" set PM_TESTDATA=%~1
set QT_QPA_PLATFORM=offscreen
python -m pytest -q -p no:warnings
set RC=%ERRORLEVEL%
pause
exit /b %RC%
