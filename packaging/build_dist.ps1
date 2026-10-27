<#
Builds BOTH packages of the current code into <Root>\dist, without deleting anything that exists:
  powershell -ExecutionPolicy Bypass -File packaging\build_dist.ps1 [-Root C:\Users\Behdad\PhotoMeshStudio]

  dist\PhotoMeshStudio_portable_<stamp>.zip   full offline package (python, wheelhouse, COLMAP/GLOMAP, app)
  dist\PhotoMeshStudio_update_<stamp>.zip     code-only update for an existing portable install
  dist\*.sha256                               SHA256 of each zip
  -UpdateOnly                                 build only the update zip (no full package)

Python, wheelhouse, tools and redist are REUSED from the last full build
(<Root>\PhotoMeshStudio_portable\PhotoMeshStudio, made by build_portable.ps1), so no internet is needed.
It stops if the dev venv's packages differ from that build's requirements.lock: then run
build_portable.ps1 first (it needs internet for pip download).
#>
param([string]$Root = "C:\Users\Behdad\PhotoMeshStudio", [string]$Stamp = (Get-Date -Format "yyyyMMdd_HHmm"), [switch]$UpdateOnly)
$ErrorActionPreference = "Stop"
try { (Get-Process -Id $PID).PriorityClass = "BelowNormal" } catch {}
$app = Join-Path $Root "photomesh-studio"
$venvPy = Join-Path $app ".venv\Scripts\python.exe"
$reuse = Join-Path $Root "PhotoMeshStudio_portable\PhotoMeshStudio"
$dist = Join-Path $Root "dist"
$work = Join-Path $dist "stage_$Stamp"
$full = Join-Path $dist "PhotoMeshStudio_portable_$Stamp.zip"
$upd = Join-Path $dist "PhotoMeshStudio_update_$Stamp.zip"
foreach ($p in $work, $full, $upd) { if (Test-Path $p) { throw "$p exists - use another -Stamp" } }
foreach ($d in "python\python.exe", "wheelhouse", "tools\colmap\bin\colmap.exe", "tools\glomap", "requirements.lock") {
    if (-not (Test-Path (Join-Path $reuse $d))) { throw "last full build incomplete: $reuse\$d missing - run build_portable.ps1" }
}
$lock = @(Get-Content (Join-Path $reuse "requirements.lock"))
$now = @(& $venvPy -m pip freeze --exclude-editable | Where-Object { $_ -and $_ -notmatch "^(pip|wheel)==" })
$diff = Compare-Object $lock $now
if ($diff) { $diff | Format-Table -AutoSize | Out-String | Write-Host; throw "packages changed since the last full build - run build_portable.ps1" }
Write-Host "packages unchanged ($($lock.Count) pinned)"

function Copy-Tree($src, $dst, [string[]]$xd = @(), [string[]]$xf = @()) {
    $ra = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($xd) { $ra += "/XD"; $ra += $xd }
    if ($xf) { $ra += "/XF"; $ra += $xf }
    & robocopy @ra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $src failed ($LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}
$codeXD = @(".git", ".venv", "__pycache__", ".pytest_cache", "packaging"); $codeXF = @("*.pyc", "screenshot*.png")
$ver = ((Get-Content (Join-Path $app "photomesh\__init__.py") | Select-String 'APP_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value)

function Write-Icon($dir) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $env:QT_QPA_PLATFORM = "offscreen"
    & $venvPy -c @"
import sys
sys.path.insert(0, r'$app')
from PySide6.QtWidgets import QApplication
app = QApplication([])
from photomesh import appicon
appicon.write_ico(r'$dir\photomesh.ico')
"@
    if (-not (Test-Path "$dir\photomesh.ico")) { throw "icon not written" }
}
$zips = @()
$pkg = Join-Path $work "full\PhotoMeshStudio"
if (-not $UpdateOnly) {
# ---- full portable ----
Write-Host "1/4 full: reuse python, wheelhouse, tools, redist"
foreach ($d in "python", "wheelhouse", "tools", "redist") {
    if (Test-Path (Join-Path $reuse $d)) { Copy-Tree (Join-Path $reuse $d) (Join-Path $pkg $d) }
}
Copy-Item (Join-Path $reuse "requirements.lock") $pkg
Write-Host "2/4 full: app code, scripts, docs, icon"
Copy-Tree $app (Join-Path $pkg "photomesh-studio") $codeXD $codeXF
foreach ($f in "install_offline.bat", "PhotoMesh Studio.bat", "create_shortcut.ps1", "run_tests_offline.bat", "check_install_path.py", "check_update.bat") {
    Copy-Item (Join-Path $app "packaging\$f") (Join-Path $pkg $f)
}
foreach ($f in "INSTALL_OTHER_LAPTOP.md", "CHANGES.md", "UPDATING.md", "update.bat", "rollback.bat", "setup_git_update.bat") { Copy-Item (Join-Path $app $f) $pkg }
Copy-Item (Join-Path $app "packaging\README_FIRST.txt") $pkg
Write-Icon $pkg
$pyver = (& "$pkg\python\python.exe" -c "import sys;print(sys.version.split()[0])")
@"
PhotoMesh Studio $ver - full portable package
built:   $(Get-Date -Format "yyyy-MM-dd HH:mm") (UTC+4) on $env:COMPUTERNAME
python:  $pyver (x64), $($lock.Count) pinned packages in wheelhouse\ (offline install)
colmap:  tools\colmap (3.8, CUDA 11 build: cudart64_110.dll)
glomap:  tools\glomap
"@ | Set-Content -Encoding utf8 (Join-Path $pkg "PACKAGE_INFO.txt")
$zips += $full
}

# ---- update-only ----
$u = Join-Path $work "update"
Write-Host "3/4 update: app code + check_update.bat + docs"
Copy-Tree $app (Join-Path $u "photomesh-studio") $codeXD $codeXF
foreach ($f in "check_update.bat", "create_shortcut.ps1", "check_install_path.py", "install_offline.bat", "PhotoMesh Studio.bat", "run_tests_offline.bat", "UPDATE_README.txt") {
    Copy-Item (Join-Path $app "packaging\$f") (Join-Path $u $f)
}
foreach ($f in "INSTALL_OTHER_LAPTOP.md", "CHANGES.md", "UPDATING.md", "update.bat", "rollback.bat", "setup_git_update.bat") { Copy-Item (Join-Path $app $f) $u }
Write-Icon $u

Write-Host "4/4 zip + SHA256"
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (-not $UpdateOnly) { [IO.Compression.ZipFile]::CreateFromDirectory($pkg, $full, [IO.Compression.CompressionLevel]::Optimal, $true) }
[IO.Compression.ZipFile]::CreateFromDirectory($u, $upd, [IO.Compression.CompressionLevel]::Optimal, $false)
$zips += $upd
foreach ($z in $zips) {
    $h = (Get-FileHash $z -Algorithm SHA256).Hash.ToLower()
    "$h *$(Split-Path -Leaf $z)" | Set-Content -Encoding ascii "$z.sha256"
    "{0}  {1:N0} bytes  SHA256 {2}" -f $z, (Get-Item $z).Length, $h
}
Remove-Item -Recurse -Force $work     # only the staging folder this run created
