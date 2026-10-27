<#
Build one complete Windows setup EXE. Each release carries the app, Python,
COLMAP, and GLOMAP; recipients never extract a ZIP. Run from PowerShell with -RuntimeSource
pointing to a tested development runtime (.venv and tools) or an existing
installer-based PhotoMesh Studio installation (python, tools, app/runtime.id).
The build reads that source and writes only to StageDir/OutputDir.
#>
param(
    [Parameter(Mandatory=$true)][string]$RuntimeSource,
    [Parameter(Mandatory=$true)][string]$ISCC,
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\dist"),
    [string]$StageDir = (Join-Path $PSScriptRoot "..\release-stage")
)
$ErrorActionPreference = "Stop"
$app = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtime = [IO.Path]::GetFullPath($RuntimeSource)
$stage = [IO.Path]::GetFullPath($StageDir)
$out = [IO.Path]::GetFullPath($OutputDir)
$compiler = [IO.Path]::GetFullPath($ISCC)
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw "ISCC.exe not found: $compiler" }
if (Test-Path -LiteralPath $stage) { throw "Stage already exists: $stage. Choose a fresh -StageDir." }
$venv = Join-Path $runtime "photomesh-studio\.venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
$site = Join-Path $venv "Lib\site-packages"
$installedPython = Join-Path $runtime "python\python.exe"
$installedIdPath = Join-Path $runtime "app\runtime.id"
$colmap = Join-Path $runtime "tools\colmap"
$glomap = Join-Path $runtime "tools\glomap"
foreach ($p in @($colmap, $glomap)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "Required runtime path missing: $p" }
}
if ((Test-Path -LiteralPath $venvPy -PathType Leaf) -and (Test-Path -LiteralPath $site -PathType Container)) {
    $sourceMode = "venv"
    $cfg = Get-Content -LiteralPath (Join-Path $venv "pyvenv.cfg")
    $base = (($cfg | Where-Object { $_ -match "^home\s*=" } | Select-Object -First 1) -replace "^home\s*=\s*", "").Trim()
    if (-not (Test-Path -LiteralPath (Join-Path $base "pythonw.exe"))) { throw "Base Python missing: $base" }
} elseif ((Test-Path -LiteralPath $installedPython -PathType Leaf) -and (Test-Path -LiteralPath $installedIdPath -PathType Leaf)) {
    $sourceMode = "installed"
    $runtimeId = (Get-Content -LiteralPath $installedIdPath -Raw).Trim()
    if ($runtimeId -notmatch '^[0-9a-fA-F]{64}$') { throw "Invalid installed runtime ID: $installedIdPath" }
    Write-Host "Reusing installed runtime from $runtime"
} else {
    throw "RuntimeSource must contain photomesh-studio\.venv or an installed app with python\python.exe and app\runtime.id: $runtime"
}
$versionText = Get-Content -LiteralPath (Join-Path $app "photomesh\__init__.py") -Raw
if ($versionText -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') { throw "APP_VERSION missing" }
$version = $Matches[1]
$name = "PhotoMeshStudio_Setup_$version.exe"
if (Test-Path -LiteralPath (Join-Path $out $name)) { throw "Output already exists: $name" }

function Copy-Tree([string]$Source, [string]$Destination, [string[]]$ExcludeDirs = @(), [string[]]$ExcludeFiles = @()) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $args = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($ExcludeDirs.Count) { $args += "/XD"; $args += $ExcludeDirs }
    if ($ExcludeFiles.Count) { $args += "/XF"; $args += $ExcludeFiles }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copy failed ($LASTEXITCODE): $Source" }
    $global:LASTEXITCODE = 0
}

New-Item -ItemType Directory -Force -Path $stage, $out | Out-Null
Write-Host "Staging PhotoMesh Studio $version"
$appStage = Join-Path $stage "app"
Copy-Tree (Join-Path $app "photomesh") (Join-Path $appStage "photomesh") @("__pycache__") @("*.pyc", "*.log")
foreach ($name in @("main.py", "README.md", "CHANGES.md", "UPDATING.md", "INSTALL_OTHER_LAPTOP.md", "requirements.txt", "run.bat", "setup.bat")) {
    Copy-Item -LiteralPath (Join-Path $app $name) -Destination (Join-Path $appStage $name)
}

Write-Host "Copying Python and its tested packages"
$pythonStage = Join-Path $stage "python"
if ($sourceMode -eq "venv") {
    Copy-Tree $base $pythonStage @("site-packages", "__pycache__") @("*.pyc")
    Copy-Tree $site (Join-Path $pythonStage "Lib\site-packages") @("__pycache__") @("*.pyc")
} else {
    Copy-Tree (Join-Path $runtime "python") $pythonStage @("__pycache__") @("*.pyc")
}
Write-Host "Copying COLMAP and GLOMAP"
Copy-Tree $colmap (Join-Path $stage "tools\colmap")
Copy-Tree $glomap (Join-Path $stage "tools\glomap")

if ($sourceMode -eq "venv") {
    $freeze = @(& $venvPy -m pip freeze --exclude-editable | Sort-Object)
    if ($LASTEXITCODE -ne 0) { throw "pip freeze failed" }
    $pyver = (& $venvPy -c "import sys; print(sys.version.split()[0])")
    $colhash = (Get-FileHash -LiteralPath (Join-Path $colmap "bin\colmap.exe") -Algorithm SHA256).Hash
    $glhash = (Get-FileHash -LiteralPath (Join-Path $glomap "bin\glomap.exe") -Algorithm SHA256).Hash
    $runtimeDescription = @("python=$pyver", "colmap=$colhash", "glomap=$glhash") + $freeze
    $sha = [Security.Cryptography.SHA256]::Create()
    $runtimeId = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($runtimeDescription -join "`n")))) -replace "-", "").ToLowerInvariant()
    $sha.Dispose()
} else {
    $runtimeDescription = @("Reused installed runtime ID: $runtimeId")
}
Set-Content -LiteralPath (Join-Path $appStage "runtime.id") -Value $runtimeId -Encoding ascii
Set-Content -LiteralPath (Join-Path $stage "runtime.lock") -Value $runtimeDescription -Encoding utf8
Set-Content -LiteralPath (Join-Path $appStage "release_manifest.txt") -Value @("PhotoMesh Studio $version", "Runtime ID: $runtimeId") -Encoding utf8

Write-Host "Validating copied runtime"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONPATH = $appStage
& (Join-Path $pythonStage "python.exe") -c "import PySide6, numpy, scipy, open3d, pyproj, photomesh; print('Runtime OK:', photomesh.APP_VERSION)"
if ($LASTEXITCODE -ne 0) { throw "Copied Python runtime failed validation" }

Write-Host "Building full installer"
$iss = Join-Path $PSScriptRoot "release.iss"
& $compiler "/Q" "/DAppVersion=$version" "/DStageDir=$stage" "/DOutputDir=$out" $iss
if ($LASTEXITCODE -ne 0) { throw "Full installer compilation failed" }
$file = Join-Path $out $name
$hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash *$name" | Set-Content -LiteralPath "$file.sha256" -Encoding ascii
Write-Host "$name  $((Get-Item -LiteralPath $file).Length) bytes  SHA256 $hash"
Write-Host "Release complete. Staging retained at $stage for verification."
