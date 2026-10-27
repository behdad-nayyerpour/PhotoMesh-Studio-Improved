@echo off
REM Self-test of an installed package (offline). Optional argument: folder with photos for the COLMAP tests.
cd /d "%~dp0photomesh-studio"
if not exist .venv\Scripts\python.exe (echo Run install_offline.bat first & exit /b 1)
if not "%~1"=="" set PM_TESTDATA=%~1
set QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -p no:warnings
exit /b %ERRORLEVEL%
