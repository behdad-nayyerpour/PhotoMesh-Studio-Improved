<#
Build one Windows setup EXE and one versioned update EXE. Both contain their
payload; recipients never extract a ZIP. Run from PowerShell with -RuntimeSource
pointing to a tested PhotoMeshStudio installation that has .venv and tools.
The build reads that installation and writes only to StageDir/OutputDir/StateDir.
#>
param(
    [Parameter(Mandatory=$true)][string]$RuntimeSource,
    [Parameter(Mandatory=$true)][string]$ISCC,
    [switch]$BundleRuntimeInUpdate,
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\dist"),
    [string]$StageDir = (Join-Path $PSScriptRoot "..\release-stage"),
    [string]$StateDir = (Join-Path $PSScriptRoot "..\release-state")
)
$ErrorActionPreference = "Stop"
$app = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtime = [IO.Path]::GetFullPath($RuntimeSource)
$stage = [IO.Path]::GetFullPath($StageDir)
$out = [IO.Path]::GetFullPath($OutputDir)
$state = [IO.Path]::GetFullPath($StateDir)
$compiler = [IO.Path]::GetFullPath($ISCC)
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw "ISCC.exe not found: $compiler" }
if (Test-Path -LiteralPath $stage) { throw "Stage already exists: $stage. Choose a fresh -StageDir." }
$venv = Join-Path $runtime "photomesh-studio\.venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
$site = Join-Path $venv "Lib\site-packages"
$colmap = Join-Path $runtime "tools\colmap"
$glomap = Join-Path $runtime "tools\glomap"
foreach ($p in @($venvPy, $site, $colmap, $glomap)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "Required runtime path missing: $p" }
}
$cfg = Get-Content -LiteralPath (Join-Path $venv "pyvenv.cfg")
$base = (($cfg | Where-Object { $_ -match "^home\s*=" } | Select-Object -First 1) -replace "^home\s*=\s*", "").Trim()
if (-not (Test-Path -LiteralPath (Join-Path $base "pythonw.exe"))) { throw "Base Python missing: $base" }
$versionText = Get-Content -LiteralPath (Join-Path $app "photomesh\__init__.py") -Raw
if ($versionText -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') { throw "APP_VERSION missing" }
$version = $Matches[1]
foreach ($name in @("PhotoMeshStudio_Setup_$version.exe", "PhotoMeshStudio_Update_$version.exe")) {
    if (Test-Path -LiteralPath (Join-Path $out $name)) { throw "Output already exists: $name" }
}

function Copy-Tree([string]$Source, [string]$Destination, [string[]]$ExcludeDirs = @(), [string[]]$ExcludeFiles = @()) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $args = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($ExcludeDirs.Count) { $args += "/XD"; $args += $ExcludeDirs }
    if ($ExcludeFiles.Count) { $args += "/XF"; $args += $ExcludeFiles }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copy failed ($LASTEXITCODE): $Source" }
    $global:LASTEXITCODE = 0
}

New-Item -ItemType Directory -Force -Path $stage, $out, $state | Out-Null
Write-Host "Staging PhotoMesh Studio $version"
$appStage = Join-Path $stage "app"
Copy-Tree (Join-Path $app "photomesh") (Join-Path $appStage "photomesh") @("__pycache__") @("*.pyc", "*.log")
foreach ($name in @("main.py", "README.md", "CHANGES.md", "UPDATING.md", "INSTALL_OTHER_LAPTOP.md", "requirements.txt", "run.bat", "setup.bat")) {
    Copy-Item -LiteralPath (Join-Path $app $name) -Destination (Join-Path $appStage $name)
}

Write-Host "Copying Python and its tested packages"
$pythonStage = Join-Path $stage "python"
Copy-Tree $base $pythonStage @("site-packages", "__pycache__") @("*.pyc")
Copy-Tree $site (Join-Path $pythonStage "Lib\site-packages") @("__pycache__") @("*.pyc")
Write-Host "Copying COLMAP and GLOMAP"
Copy-Tree $colmap (Join-Path $stage "tools\colmap")
Copy-Tree $glomap (Join-Path $stage "tools\glomap")

$freeze = @(& $venvPy -m pip freeze --exclude-editable | Sort-Object)
if ($LASTEXITCODE -ne 0) { throw "pip freeze failed" }
$pyver = (& $venvPy -c "import sys; print(sys.version.split()[0])")
$colhash = (Get-FileHash -LiteralPath (Join-Path $colmap "bin\colmap.exe") -Algorithm SHA256).Hash
$glhash = (Get-FileHash -LiteralPath (Join-Path $glomap "bin\glomap.exe") -Algorithm SHA256).Hash
$runtimeDescription = @("python=$pyver", "colmap=$colhash", "glomap=$glhash") + $freeze
$sha = [Security.Cryptography.SHA256]::Create()
$runtimeId = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($runtimeDescription -join "`n")))) -replace "-", "").ToLowerInvariant()
$sha.Dispose()
Set-Content -LiteralPath (Join-Path $appStage "runtime.id") -Value $runtimeId -Encoding ascii
Set-Content -LiteralPath (Join-Path $stage "runtime.lock") -Value $runtimeDescription -Encoding utf8
Set-Content -LiteralPath (Join-Path $appStage "release_manifest.txt") -Value @("PhotoMesh Studio $version", "Runtime ID: $runtimeId") -Encoding utf8

Write-Host "Validating copied runtime"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONPATH = $appStage
& (Join-Path $pythonStage "python.exe") -c "import PySide6, numpy, scipy, open3d, pyproj, photomesh; print('Runtime OK:', photomesh.APP_VERSION)"
if ($LASTEXITCODE -ne 0) { throw "Copied Python runtime failed validation" }

$priorIdPath = Join-Path $state "runtime.id"
$includeRuntime = [int]$BundleRuntimeInUpdate.IsPresent
if (Test-Path -LiteralPath $priorIdPath) {
    $prior = (Get-Content -LiteralPath $priorIdPath -Raw).Trim()
    if ($prior -ne $runtimeId -and -not $BundleRuntimeInUpdate) {
        throw "Runtime differs from the previous build. A code-only updater would not work on that installation. Rebuild with the same tested runtime, or explicitly pass -BundleRuntimeInUpdate to include the full runtime."
    }
} elseif (-not $BundleRuntimeInUpdate) {
    Write-Warning "No previous runtime.id in StateDir. This code-only updater will work only if the installed runtime ID matches $runtimeId."
}
Write-Host "Building full installer"
$iss = Join-Path $PSScriptRoot "release.iss"
& $compiler "/Q" "/DAppVersion=$version" "/DStageDir=$stage" "/DOutputDir=$out" "/DRuntimeId=$runtimeId" "/DUpdateOnly=0" "/DIncludeRuntimeInUpdate=0" $iss
if ($LASTEXITCODE -ne 0) { throw "Full installer compilation failed" }
Write-Host "Building update installer (bundled runtime: $includeRuntime)"
& $compiler "/Q" "/DAppVersion=$version" "/DStageDir=$stage" "/DOutputDir=$out" "/DRuntimeId=$runtimeId" "/DUpdateOnly=1" "/DIncludeRuntimeInUpdate=$includeRuntime" $iss
if ($LASTEXITCODE -ne 0) { throw "Update installer compilation failed" }
foreach ($name in @("PhotoMeshStudio_Setup_$version.exe", "PhotoMeshStudio_Update_$version.exe")) {
    $file = Join-Path $out $name
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash *$name" | Set-Content -LiteralPath "$file.sha256" -Encoding ascii
    Write-Host "$name  $((Get-Item -LiteralPath $file).Length) bytes  SHA256 $hash"
}
Set-Content -LiteralPath $priorIdPath -Value $runtimeId -Encoding ascii
Write-Host "Release complete. Staging retained at $stage for verification."
