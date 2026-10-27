<#
Builds a small code-only update for an existing portable install:
  powershell -ExecutionPolicy Bypass -File packaging\build_update.ps1 [-Root C:\Users\Behdad\PhotoMeshStudio]
Output: <Root>\PhotoMeshStudio_update_<yyyyMMdd_HHmm>.zip with
  photomesh-studio\ (app code, no .venv), check_update.bat, UPDATE_README.txt, INSTALL_OTHER_LAPTOP.md
Extract it INTO the installed PhotoMeshStudio folder (the one with install_offline.bat) and overwrite.
The Python environment (.venv), tools and wheelhouse are untouched: no reinstall needed.
#>
param([string]$Root = "C:\Users\Behdad\PhotoMeshStudio")
$ErrorActionPreference = "Continue"
$app = Join-Path $Root "photomesh-studio"
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$stage = Join-Path $env:TEMP "pm_update_$stamp"
$zip = Join-Path $Root "PhotoMeshStudio_update_$stamp.zip"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
$ra = @($app, (Join-Path $stage "photomesh-studio"), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1",
        "/XD", ".git", ".venv", "__pycache__", ".pytest_cache", "packaging", "/XF", "*.pyc", "screenshot*.png")
& robocopy @ra | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
Copy-Item (Join-Path $app "packaging\check_update.bat") $stage
Copy-Item (Join-Path $app "INSTALL_OTHER_LAPTOP.md") $stage
$rev = (Get-Content (Join-Path $app "photomesh\__init__.py") | Select-String "APP_VERSION").ToString()
@"
PhotoMesh Studio code update ($stamp)  $rev

1. Close PhotoMesh Studio.
2. Extract this zip INTO your installed PhotoMeshStudio folder - the folder that contains
   install_offline.bat and the photomesh-studio folder (e.g. C:\PhotoMeshStudio\PhotoMeshStudio).
   When Windows asks, choose "Replace the files in the destination".
3. Double-click check_update.bat - it must print "Update OK".
4. Start PhotoMesh Studio from the desktop shortcut.

No reinstall is needed: the Python environment (.venv), COLMAP/GLOMAP and the desktop shortcut are kept.
Logs: Help > Open Log Folder  (%LOCALAPPDATA%\PhotoMeshStudio\logs). Send the newest photomesh_*.log if
anything goes wrong.
"@ | Set-Content -Encoding UTF8 (Join-Path $stage "UPDATE_README.txt")
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path $zip) { Remove-Item $zip }
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip)
Remove-Item -Recurse -Force $stage
$h = (Get-FileHash $zip -Algorithm SHA256).Hash
"{0}  {1:N0} bytes  SHA256 {2}" -f $zip, (Get-Item $zip).Length, $h
