<#
Builds the offline portable package of PhotoMesh Studio on the development PC.

  powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 [-Root C:\Users\Behdad\PhotoMeshStudio] [-NoZip]

Layout of <Root>\PhotoMeshStudio_portable\PhotoMeshStudio:
  photomesh-studio\   app code (no .venv)          tools\colmap, tools\glomap (+ app-local VC++ runtime DLLs)
  python\             copy of the base CPython 3.13 x64 used by the dev venv
  wheelhouse\         every wheel of the dev venv (pip download, win_amd64 / cp313)
  requirements.lock   exact versions (pip freeze of the dev venv)
  redist\             vc_redist.x64.exe (if it could be downloaded)
  install_offline.bat, PhotoMesh Studio.bat, create_shortcut.ps1, run_tests_offline.bat, INSTALL_OTHER_LAPTOP.md
Only needs internet (for pip download / vc_redist) while BUILDING; installing and running are offline.
#>
param([string]$Root = "C:\Users\Behdad\PhotoMeshStudio", [switch]$NoZip)
$ErrorActionPreference = "Continue"   # native tools write progress to stderr; exit codes are checked explicitly
$app = Join-Path $Root "photomesh-studio"
$venvPy = Join-Path $app ".venv\Scripts\python.exe"
$outDir = Join-Path $Root "PhotoMeshStudio_portable"
$pkg = Join-Path $outDir "PhotoMeshStudio"
$zip = Join-Path $Root "PhotoMeshStudio_portable.zip"
$basePy = (((Get-Content (Join-Path $app ".venv\pyvenv.cfg")) -match "^home\s*=") -replace "^home\s*=\s*", "").Trim()
Write-Host "Base Python: $basePy"
if (Test-Path $pkg) { Remove-Item -Recurse -Force $pkg }
New-Item -ItemType Directory -Force $pkg | Out-Null

function Copy-Tree($src, $dst, [string[]]$xd = @(), [string[]]$xf = @()) {
    $ra = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($xd) { $ra += "/XD"; $ra += $xd }
    if ($xf) { $ra += "/XF"; $ra += $xf }
    & robocopy @ra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $src failed ($LASTEXITCODE)" }
}

Write-Host "1/6 app code"
Copy-Tree $app (Join-Path $pkg "photomesh-studio") @(".git", ".venv", "__pycache__", ".pytest_cache", "packaging") @("*.pyc", "screenshot*.png")
Write-Host "2/6 tools (COLMAP, GLOMAP)"
Copy-Tree (Join-Path $Root "tools\colmap") (Join-Path $pkg "tools\colmap")
Copy-Tree (Join-Path $Root "tools\glomap") (Join-Path $pkg "tools\glomap")
# app-local Visual C++ runtime so colmap.exe / glomap.exe start without installing vc_redist
$vc = "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
      "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll", "vcomp140.dll"
foreach ($bin in @("tools\colmap\bin", "tools\glomap\bin")) {
    foreach ($d in $vc) {
        $s = Join-Path "$env:SystemRoot\System32" $d; $t = Join-Path (Join-Path $pkg $bin) $d
        if ((Test-Path $s) -and -not (Test-Path $t)) { Copy-Item $s $t }
    }
}
New-Item -ItemType Directory -Force (Join-Path $pkg "redist") | Out-Null
try {
    Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile (Join-Path $pkg "redist\vc_redist.x64.exe") -UseBasicParsing -TimeoutSec 120
} catch { Write-Warning "vc_redist.x64.exe not downloaded ($_) - app-local DLLs are included instead" }

Write-Host "3/6 Python $basePy"
Copy-Tree $basePy (Join-Path $pkg "python") @("__pycache__", "site-packages") @("*.pyc")
New-Item -ItemType Directory -Force (Join-Path $pkg "python\Lib\site-packages") | Out-Null

Write-Host "4/6 wheelhouse"
$lock = Join-Path $pkg "requirements.lock"
& $venvPy -m pip freeze --exclude-editable | Where-Object { $_ -and $_ -notmatch "^(pip|wheel)==" } |
    Set-Content -Encoding ascii $lock
$wh = Join-Path $pkg "wheelhouse"
& $venvPy -m pip download --disable-pip-version-check -q --only-binary=:all: --no-deps -r $lock -d $wh
if ($LASTEXITCODE) { throw "pip download failed" }
# dependencies resolvable offline?
& $venvPy -m pip install --dry-run --no-index --find-links $wh --ignore-installed -q -r $lock --report (Join-Path $env:TEMP "pm_resolve.json")
if ($LASTEXITCODE) { throw "wheelhouse incomplete (offline resolve failed)" }

Write-Host "5/6 scripts + docs"
foreach ($f in "install_offline.bat", "PhotoMesh Studio.bat", "create_shortcut.ps1", "run_tests_offline.bat", "check_install_path.py", "check_update.bat") {
    Copy-Item (Join-Path $app "packaging\$f") (Join-Path $pkg $f)
}
Copy-Item (Join-Path $app "INSTALL_OTHER_LAPTOP.md") $pkg
& $venvPy -c @"
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, r'$app')
from PySide6.QtWidgets import QApplication
app = QApplication([])
from photomesh import appicon
appicon.write_ico(r'$pkg\photomesh.ico')
"@
$ver = (& $venvPy -c "import sys;print(sys.version.split()[0])")
@"
PhotoMesh Studio portable package
built:   $(Get-Date -Format "yyyy-MM-dd HH:mm") on $env:COMPUTERNAME
python:  $ver (x64)
colmap:  tools\colmap (3.8, CUDA 11 build: cudart64_110.dll)
glomap:  tools\glomap
"@ | Set-Content -Encoding utf8 (Join-Path $pkg "PACKAGE_INFO.txt")

if (-not $NoZip) {
    Write-Host "6/6 zip -> $zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($pkg, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
    "{0:N0} MB  $zip" -f ((Get-Item $zip).Length / 1MB)
}
"{0:N0} MB  $pkg" -f ((Get-ChildItem $pkg -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
