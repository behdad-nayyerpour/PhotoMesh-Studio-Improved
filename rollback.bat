@echo off
rem PhotoMesh Studio - go back to an earlier version (git tag), e.g.  rollback.bat v0.1.4
rem Without an argument it lists the versions and asks. update.bat later brings you to the newest again.
setlocal
set "HERE=%~dp0"
set "REPO="
if exist "%HERE%photomesh-studio\main.py" set "REPO=%HERE%photomesh-studio"
if not defined REPO if exist "%HERE%main.py" set "REPO=%HERE:~0,-1%"
if not defined REPO goto :where
if not exist "%REPO%\packaging\update.ps1" goto :nosetup
set "ARGS=-Rollback"
if not "%~1"=="" set "ARGS=-Rollback -Tag %~1"
set "T=%TEMP%\pms_rollback_%RANDOM%%RANDOM%.ps1"
rem one line on purpose: git may replace this file while it runs
copy /y "%REPO%\packaging\update.ps1" "%T%" >nul && powershell -NoProfile -ExecutionPolicy Bypass -File "%T%" -Repo "%REPO%" %ARGS% && exit /b 0 || exit /b 1
:where
echo ERROR: put rollback.bat in the PhotoMeshStudio folder that contains the photomesh-studio folder.
pause
exit /b 1
:nosetup
echo ERROR: this install is not set up for git updates yet - run setup_git_update.bat once.
pause
exit /b 1
