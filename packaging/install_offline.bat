@echo off
REM PhotoMesh Studio - offline installer (no internet needed).
REM Creates photomesh-studio\.venv from the bundled Python and the wheelhouse,
REM then creates a desktop shortcut. Re-run it after moving this folder.
REM   install_offline.bat            install + desktop shortcut
REM   install_offline.bat /noshortcut
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "APP=%ROOT%photomesh-studio"
if not exist "%ROOT%python\python.exe" (echo ERROR: python\python.exe missing - incomplete package & goto :fail)
if not exist "%ROOT%wheelhouse" (echo ERROR: wheelhouse folder missing - incomplete package & goto :fail)
"%ROOT%python\python.exe" -c "import struct,sys; assert struct.calcsize('P')==8; print('Python', sys.version.split()[0], '64-bit')" || goto :fail
REM Windows MAX_PATH (260 characters): refuse folder paths that are too deep
"%ROOT%python\python.exe" "%ROOT%check_install_path.py" "%APP%" "%ROOT%wheelhouse" || goto :fail
if exist "%APP%\.venv" (
  echo Removing previous environment...
  rmdir /s /q "%APP%\.venv"
)
echo Creating virtual environment...
"%ROOT%python\python.exe" -m venv "%APP%\.venv" || goto :fail
echo Installing packages from the wheelhouse (offline)...
"%APP%\.venv\Scripts\python.exe" -m pip install --no-index --find-links "%ROOT%wheelhouse" --disable-pip-version-check -q -r "%ROOT%requirements.lock" || goto :fail
"%APP%\.venv\Scripts\python.exe" -c "import PySide6, pyqtgraph, numpy, scipy, open3d, plyfile, PIL, OpenGL; print('Packages OK')" || goto :fail
if exist "%ROOT%tools\colmap\bin\colmap.exe" (
  set "PATH=%ROOT%tools\colmap\lib;%PATH%"
  "%ROOT%tools\colmap\bin\colmap.exe" help 2>&1 | findstr /c:"COLMAP 3" >nul && (echo COLMAP OK) || (echo WARNING: tools\colmap\bin\colmap.exe does not start - install the VC++ 2015-2022 x64 runtime: redist\vc_redist.x64.exe)
) else (echo WARNING: tools\colmap missing - alignment/dense need COLMAP)
if exist "%ROOT%tools\glomap\bin\glomap.exe" (
  "%ROOT%tools\glomap\bin\glomap.exe" -h 2>&1 | findstr /c:"GLOMAP" >nul && (echo GLOMAP OK) || (echo WARNING: tools\glomap\bin\glomap.exe does not start)
)
if exist "%SystemRoot%\System32\nvcuda.dll" (echo NVIDIA driver found: GPU/CUDA processing available) else (echo No NVIDIA driver found: CPU-only mode - alignment works, dense point cloud / depth maps need an NVIDIA GPU)
if /i not "%~1"=="/noshortcut" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%create_shortcut.ps1"
)
echo.
echo Installation complete. Start with "PhotoMesh Studio.bat" or the desktop shortcut.
if /i not "%~1"=="/noshortcut" pause
exit /b 0
:fail
echo.
echo Installation FAILED.
if /i not "%~1"=="/noshortcut" pause
exit /b 1
