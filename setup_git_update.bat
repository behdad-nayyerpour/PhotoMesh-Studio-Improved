@echo off
rem PhotoMesh Studio - ONE-TIME switch of an existing install to git updates.
rem Keeps photomesh-studio\.venv, projects (*.pmsx) and every file git does not track.
rem Put it in the PhotoMeshStudio folder (the one that contains photomesh-studio) and double-click it.
rem Needs Git for Windows (default options) and, the first time, a browser sign-in to GitHub.
setlocal EnableExtensions
if /i "%~1"=="--from-temp" goto :main
rem run from a temp copy: the checkout below rewrites program files, maybe this one too
copy /y "%~f0" "%TEMP%\pms_setup_git_update.bat" >nul && "%TEMP%\pms_setup_git_update.bat" --from-temp "%~dp0."
exit /b

:main
set "HERE=%~f2\"
set "SETUP="
set "FIX="
set "URL=https://github.com/behdad-nayyerpour/photomesh-studio-improved.git"
set "APP="
if exist "%HERE%photomesh-studio\main.py" set "APP=%HERE%photomesh-studio"
if not defined APP if exist "%HERE%main.py" set "APP=%HERE:~0,-1%"
if not defined APP goto :where
echo PhotoMesh Studio program folder: %APP%

where git >nul 2>nul && goto :havegit
if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
if exist "%ProgramFiles%\Git\cmd\git.exe" goto :havegit
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "PATH=%LOCALAPPDATA%\Programs\Git\cmd;%PATH%"
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" goto :havegit
echo.
echo Git is not installed. Install Git for Windows once, with all default options:
echo   https://git-scm.com/download/win    or in a terminal:  winget install --id Git.Git -e
echo Then close this window and double-click setup_git_update.bat again.
goto :fail
:havegit
git --version

cd /d "%APP%" || goto :fail
if exist "%APP%\.git" goto :already
if not exist "%APP%\.venv\Scripts\python.exe" echo WARNING: %APP%\.venv not found - after this run install_offline.bat once.
call :checkrun
git init -q -b main || goto :fail
git rev-parse --git-dir >nul 2>nul || git config --global --add safe.directory "%APP:\=/%"
git remote add origin "%URL%" || goto :undo
call :download || goto :nofetch
call :adopt || goto :undo
goto :ready

:already
echo %APP% is already a git checkout - checking it.
git rev-parse --git-dir >nul 2>nul || git config --global --add safe.directory "%APP:\=/%"
git remote get-url origin >nul 2>nul || git remote add origin "%URL%" || goto :fail
set "FIX="
if not exist "%APP%\packaging\update.ps1" set "FIX=1"
git rev-parse -q --verify HEAD >nul 2>nul || set "FIX=1"
if not defined FIX goto :runupdate
rem older or unfinished checkout (e.g. the 0.1.4 portable zip has no packaging folder):
rem bring the program files to the GitHub version; untracked .venv, projects and data stay
echo This checkout is incomplete or older than the updater (packaging\update.ps1 is missing).
echo It is now set to the GitHub version. .venv, projects and other data are kept.
call :checkrun
call :download || goto :nofetch_keep
for /f "delims=" %%h in ('git rev-parse -q --verify HEAD 2^>nul') do echo Previous version %%h is kept in git reflog.
call :adopt || goto :fail

:ready
echo.
echo Git checkout ready. Untracked files such as .venv were not touched:
git status --short
echo Running the update check...
set "SETUP=-Setup"

:runupdate
set "T=%TEMP%\pms_update_setup_%RANDOM%%RANDOM%.ps1"
if exist "%APP%\packaging\update.ps1" copy /y "%APP%\packaging\update.ps1" "%T%" >nul
if not exist "%T%" git show origin/main:packaging/update.ps1 > "%T%" 2>nul
if not exist "%T%" goto :noscript
for %%s in ("%T%") do if %%~zs LSS 100 goto :noscript
powershell -NoProfile -ExecutionPolicy Bypass -File "%T%" -Repo "%APP%" %SETUP%
exit /b %errorlevel%

rem ---------------------------------------------------------------- subroutines
:checkrun
powershell -NoProfile -Command "if (@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'main\.py' }).Count) { exit 3 } else { exit 0 }"
if not errorlevel 3 exit /b 0
echo.
echo PhotoMesh Studio seems to be running. Save your project and close it - nothing is closed for you.
set "A="
set /p "A=Press Enter to check again, or type S and Enter to continue anyway: "
if /i "%A%"=="S" exit /b 0
goto :checkrun

:download
echo.
echo Downloading PhotoMesh Studio from GitHub.
echo The first time a browser window opens: sign in to GitHub and allow Git Credential Manager.
git fetch origin --tags || exit /b 1
git rev-parse -q --verify origin/main >nul || exit /b 1
exit /b 0

:adopt
rem index = GitHub version, files on disk untouched so far
git symbolic-ref HEAD refs/heads/main || exit /b 1
git reset -q --mixed origin/main || exit /b 1
git branch -q --set-upstream-to=origin/main main
rem files missing on disk are simply added; old copies of files that differ are kept in git stash
for /f "delims=" %%f in ('git ls-files -d') do git checkout -q -- "%%f"
git diff --quiet
if not errorlevel 1 goto :adopt_clean
git -c user.name=PhotoMesh -c user.email=photomesh@localhost stash push -q -m "program files before git setup" || exit /b 1
echo Older copies of changed program files were kept in git stash - git stash list.
:adopt_clean
git rm -r -q --cached . >nul || exit /b 1
git reset -q --hard HEAD || exit /b 1
exit /b 0

rem ---------------------------------------------------------------- errors
:noscript
echo.
echo ERROR: packaging\update.ps1 was not found in %APP% nor on GitHub (origin/main).
echo Check the internet connection and run setup_git_update.bat again.
goto :fail
:nofetch_keep
echo.
echo Could not download from GitHub. Check the internet connection and sign in with the GitHub
echo account that has access to behdad-nayyerpour/photomesh-studio-improved, then run this file again.
echo Your existing checkout was not changed.
goto :fail
:nofetch
echo.
echo Could not download from GitHub. Check the internet connection and sign in with the GitHub
echo account that has access to behdad-nayyerpour/photomesh-studio-improved, then run this file again.
:undo
rem remove only the .git folder this run created; program files and data are unchanged
cd /d "%HERE%"
if exist "%APP%\.git" rmdir /s /q "%APP%\.git"
goto :fail
:where
echo ERROR: put setup_git_update.bat in the PhotoMeshStudio folder that contains the photomesh-studio folder.
:fail
echo.
echo Setup did not finish - nothing of yours was deleted.
pause
exit /b 1
