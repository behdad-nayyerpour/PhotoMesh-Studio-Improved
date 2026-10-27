@echo off
rem Run after extracting PhotoMeshStudio_update_*.zip into the folder that contains install_offline.bat.
setlocal
cd /d "%~dp0"
set "PY=%~dp0photomesh-studio\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: "%PY%" not found.
  echo Extract this update into the PhotoMeshStudio folder that contains install_offline.bat
  echo and the photomesh-studio folder, then run this file again.
  pause
  exit /b 1
)
del /s /q "%~dp0photomesh-studio\photomesh\__pycache__\*.pyc" >nul 2>&1
cd /d "%~dp0photomesh-studio"
"%PY%" -c "import importlib, pkgutil, photomesh, open3d, scipy; mods = [m.name for m in pkgutil.iter_modules(photomesh.__path__)]; [importlib.import_module('photomesh.' + m) for m in mods]; from photomesh import diag, APP_VERSION; print('Update OK, version', APP_VERSION, '-', len(mods), 'modules - log folder:', diag.log_dir())"
if errorlevel 1 (
  echo ERROR: update check failed.
  pause
  exit /b 1
)
echo Start PhotoMesh Studio from the desktop shortcut as usual.
pause
