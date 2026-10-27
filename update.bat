@echo off
rem PhotoMesh Studio - update to the latest version from GitHub.
rem Works from the PhotoMeshStudio folder or from photomesh-studio. Keeps .venv, projects and all data.
rem   update.bat           update
rem   update.bat --stash   first put locally changed program files aside (git stash)
setlocal
set "HERE=%~dp0"
set "REPO="
if exist "%HERE%photomesh-studio\main.py" set "REPO=%HERE%photomesh-studio"
if not defined REPO if exist "%HERE%main.py" set "REPO=%HERE:~0,-1%"
if not defined REPO goto :where
if not exist "%REPO%\packaging\update.ps1" goto :nosetup
set "ARGS="
if /i "%~1"=="--stash" set "ARGS=-Stash"
set "T=%TEMP%\pms_update_%RANDOM%%RANDOM%.ps1"
rem one line on purpose: git may replace this file while the update runs
copy /y "%REPO%\packaging\update.ps1" "%T%" >nul && powershell -NoProfile -ExecutionPolicy Bypass -File "%T%" -Repo "%REPO%" %ARGS% && exit /b 0 || exit /b 1
:where
echo ERROR: put update.bat in the PhotoMeshStudio folder that contains the photomesh-studio folder.
pause
exit /b 1
:nosetup
echo ERROR: this install is not set up for git updates yet - run setup_git_update.bat once.
pause
exit /b 1
