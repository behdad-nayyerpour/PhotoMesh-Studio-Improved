<#
PhotoMesh Studio updater (started by update.bat / rollback.bat; runs from a temp copy so
git may replace this file while it runs).
  update.bat            fetch, show what is new, fast-forward to the latest version
  update.bat --stash    first put locally modified program files aside (git stash)
  rollback.bat          go back to an earlier version (tag), e.g. v0.1.4
Never touches .venv, projects (*.pmsx) or any other file git does not track.
Internet is used only here, while updating; the app itself stays offline.
#>
param([Parameter(Mandatory = $true)][string]$Repo, [switch]$Rollback, [string]$Tag = "",
      [switch]$Stash, [switch]$Setup, [switch]$Yes, [switch]$NoPause)
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$Repo = (Resolve-Path $Repo).Path.TrimEnd('\', '/')
$Root = Split-Path $Repo
$IsWin = ($env:OS -eq "Windows_NT")

function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
function Done([int]$code) {
    if ($PSCommandPath -and $PSCommandPath.StartsWith([IO.Path]::GetTempPath())) { Remove-Item $PSCommandPath -ErrorAction SilentlyContinue }
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $code
}
function Ask($q) { if ($Yes) { return $true }; $a = Read-Host "$q [Y/n]"; return ($a -eq "" -or $a -match "^[yY]") }

# ---------- git present? ----------
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    foreach ($p in "$env:ProgramFiles\Git\cmd\git.exe", "${env:ProgramFiles(x86)}\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe") {
        if ($p -and (Test-Path $p)) { $git = $p; break }
    }
}
if (-not $git) {
    Say "Git is not installed." Red
    Say "Install Git for Windows once (all default options, it includes Git Credential Manager):"
    Say "  https://git-scm.com/download/win   - or in a terminal:  winget install --id Git.Git -e"
    Say "Then close this window and run update.bat again."
    Done 1
}
function G { & $git -C $Repo @args }
function GQ { & $git -C $Repo @args 2>$null }
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Say "$Repo is not a git checkout yet." Red
    Say "Run setup_git_update.bat (in the PhotoMeshStudio folder) once; after that use update.bat."
    Done 1
}
$null = GQ rev-parse --git-dir
if ($LASTEXITCODE) {
    $msg = (& $git -C $Repo rev-parse --git-dir 2>&1 | Out-String)
    if ($msg -match "dubious ownership") {
        Say "Marking $Repo as a safe git folder (it was created by another Windows user)." Yellow
        & $git config --global --add safe.directory ($Repo -replace '\\', '/')
    } else { Say $msg Red; Done 1 }
}

function Version-Of($rev) {
    $t = if ($rev) { (GQ show "${rev}:photomesh/__init__.py") -join "`n" } else { Get-Content -Raw (Join-Path $Repo "photomesh\__init__.py") }
    if ($t -match 'APP_VERSION\s*=\s*"([^"]+)"') { return $Matches[1] } else { return "?" }
}

# ---------- app or COLMAP running? (warn only, never kill) ----------
function Running {
    if (-not $IsWin) { return @() }
    $esc = [regex]::Escape($Repo)
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'main\.py' -and ($_.CommandLine -match $esc -or
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)))) -or
        ($_.Name -match '^(colmap|glomap)\.exe$' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase))
    })
}
while ($true) {
    $r = Running
    if (-not $r) { break }
    Say "PhotoMesh Studio (or a COLMAP job it started) is still running:" Yellow
    $r | ForEach-Object { Say ("  {0} (pid {1})" -f $_.Name, $_.ProcessId) Yellow }
    Say "Save your project and close the app (let a running COLMAP job finish). Nothing will be closed for you."
    $a = Read-Host "Press Enter to check again, or Q to quit"
    if ($a -match "^[qQ]") { Done 1 }
}

# ---------- local changes to program files ----------
$dirty = @(GQ status --porcelain --untracked-files=no)
if ($dirty) {
    if ($Stash) {
        G -c user.name=PhotoMesh -c user.email=photomesh@localhost stash push -m ("before update " + (Get-Date -Format "yyyy-MM-dd HH:mm"))
        if ($LASTEXITCODE) { Say "git stash failed - nothing was changed." Red; Done 1 }
        Say "Your changed files were put aside (git stash). Get them back later with:  git stash pop" Yellow
    } else {
        Say "These program files were changed on this laptop:" Yellow
        $dirty | ForEach-Object { Say "  $_" Yellow }
        Say "The update stops so nothing of yours is overwritten. Either undo the changes, or run" 
        Say "  update.bat --stash      (keeps them aside; restore later with: git stash pop)"
        Done 1
    }
}

$old = (GQ rev-parse HEAD)
$oldVer = Version-Of $null
Say "Installed: PhotoMesh Studio $oldVer ($($old.Substring(0, 8)))" Cyan
Say "Checking GitHub for updates (the first time a browser window may ask you to sign in to GitHub)..."
G fetch origin --tags --prune --force
if ($LASTEXITCODE) {
    Say "Could not reach the repository. Check the internet connection and that you are signed in to the" Red
    Say "GitHub account that has access to behdad-nayyerpour/photomesh-studio-improved, then run update.bat again." Red
    Done 1
}

# ---------- choose target ----------
if ($Rollback) {
    $tags = @(GQ tag --list "v*" --sort=-v:refname)
    if (-not $tags) { Say "No versions (tags) found." Red; Done 1 }
    if (-not $Tag) {
        Say "Available versions:" Cyan
        for ($i = 0; $i -lt $tags.Count; $i++) {
            $d = (GQ log -1 --format=%cs $tags[$i])
            $mark = if ((GQ rev-parse "$($tags[$i])^{commit}") -eq $old) { "  <- installed" } else { "" }
            Say ("  [{0}] {1}  ({2}){3}" -f ($i + 1), $tags[$i], $d, $mark)
        }
        $a = Read-Host "Number or tag of the version to go back to (Enter = cancel)"
        if (-not $a) { Done 0 }
        $Tag = if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $tags.Count) { $tags[[int]$a - 1] } else { $a }
    }
    $target = (GQ rev-parse "$Tag^{commit}")
    if ($LASTEXITCODE -or -not $target) { Say "Unknown version '$Tag'." Red; Done 1 }
    $label = $Tag
} else {
    $target = (GQ rev-parse origin/main)
    $label = "origin/main"
    if ($target -eq $old) {
        Say "Already up to date: PhotoMesh Studio $oldVer." Green
        if (-not $Setup) { Done 0 }
    } else {
        $null = GQ merge-base --is-ancestor $old $target
        if ($LASTEXITCODE) {
            Say "This checkout has commits that are not on GitHub (or was rolled back past a rewrite)." Red
            Say "Nothing was changed. Ask for help, or use rollback.bat to pick a released version." Red
            Done 1
        }
    }
}
$newVer = Version-Of $target
if ($target -ne $old) {
    Say ""
    $what = if ($Rollback) { "Version $Tag" } else { "New version" }
    Say "${what}: PhotoMesh Studio $newVer  (now $oldVer)" Cyan
    if ($Rollback -and -not (GQ log --format=%h "$old..$target")) {
        Say "Changes that will be undone:"
        GQ log --format="  %cs  %s" "$target..$old" | Select-Object -First 30 | ForEach-Object { Say $_ }
    } else {
        Say "Changes:"
        GQ log --format="  %cs  %s" "$old..$target" | Select-Object -First 30 | ForEach-Object { Say $_ }
    }
    $ch = @(GQ show "${target}:CHANGES.md")
    $start = -1
    for ($i = 0; $i -lt $ch.Count; $i++) { if ($ch[$i] -match "^###\s+$([regex]::Escape($newVer))\b") { $start = $i; break } }
    if ($start -ge 0) {
        Say ""
        for ($i = $start; $i -lt [Math]::Min($ch.Count, $start + 40); $i++) {
            if ($i -gt $start -and $ch[$i] -match '^##') { break }
            Say $ch[$i]
        }
    }
    Say ""
    $verb = if ($Rollback) { "Go back to $Tag ($newVer)" } else { "Install $newVer" }
    if (-not (Ask "$verb now?")) { Say "Nothing was changed."; Done 0 }
    if ($Rollback) { G reset --keep $target } else { G merge --ff-only $target }
    if ($LASTEXITCODE) { Say "git could not update the files - nothing of yours was deleted. See the message above." Red; Done 1 }
}

# ---------- requirements (only when requirements.txt changed) ----------
$py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not $IsWin) { $py = Join-Path $Repo ".venv/bin/python" }
if (-not (Test-Path $py)) {
    Say "Python environment not found: $py" Red
    Say "Run install_offline.bat (portable install) or setup.bat (developer install) once, then update.bat again." Red
    Done 1
}
$reqChanged = $Setup -or [bool](GQ diff --name-only $old $target -- requirements.txt)
if ($reqChanged) {
    Say "Checking Python packages (requirements.txt)..."
    $req = Join-Path $Repo "requirements.txt"
    & $py -m pip install --no-index -q -r $req 2>$null
    if ($LASTEXITCODE) {
        $wh = Join-Path $Root "wheelhouse"
        $ok = $false
        if (Test-Path $wh) { & $py -m pip install --no-index --find-links $wh -r $req; $ok = ($LASTEXITCODE -eq 0) }
        if (-not $ok) {
            Say "Downloading the new packages from the internet (only now, during the update)..."
            & $py -m pip install -r $req; $ok = ($LASTEXITCODE -eq 0)
        }
        if (-not $ok) { Say "Package install failed. Use rollback.bat to go back to $oldVer." Red; Done 1 }
    } else { Say "  packages OK" }
}

# ---------- refresh the scripts in the PhotoMeshStudio folder ----------
foreach ($f in "update.bat", "rollback.bat", "setup_git_update.bat") {
    $src = Join-Path $Repo $f; $dst = Join-Path $Root $f
    if ((Test-Path $src) -and (-not (Test-Path $dst) -or (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash)) { Copy-Item $src $dst -Force; Say "  updated $dst" }
}
foreach ($f in "check_update.bat", "PhotoMesh Studio.bat", "create_shortcut.ps1", "check_install_path.py", "install_offline.bat", "run_tests_offline.bat", "UPDATE_README.txt") {
    $src = Join-Path $Repo "packaging\$f"; $dst = Join-Path $Root $f
    if ((Test-Path $src) -and (Test-Path $dst) -and (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash) { Copy-Item $src $dst -Force; Say "  updated $dst" }
}
# app icon for the desktop / Start menu shortcut (create_shortcut.ps1 uses $Root\photomesh.ico)
$src = Join-Path $Repo "photomesh\resources\photomesh.ico"; $dst = Join-Path $Root "photomesh.ico"
if ((Test-Path $src) -and (-not (Test-Path $dst) -or (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash)) { Copy-Item $src $dst -Force; Say "  updated $dst" }
# ---------- shortcut icon repair (only IconLocation; see fix_shortcut_icon.ps1) ----------
$fix = Join-Path $Repo "packaging\fix_shortcut_icon.ps1"
if ($IsWin -and (Test-Path $fix)) { & powershell -NoProfile -ExecutionPolicy Bypass -File $fix -Repo $Repo }
foreach ($f in "CHANGES.md", "INSTALL_OTHER_LAPTOP.md", "UPDATING.md") {
    $src = Join-Path $Repo $f; $dst = Join-Path $Root $f
    if ((Test-Path $src) -and (Test-Path $dst) -and (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash) { Copy-Item $src $dst -Force }
}

# ---------- import check (same as check_update.bat) ----------
Get-ChildItem (Join-Path $Repo "photomesh\__pycache__") -Filter *.pyc -ErrorAction SilentlyContinue | Remove-Item -ErrorAction SilentlyContinue
Push-Location $Repo
& $py -c "import importlib, pkgutil, photomesh, open3d, scipy; mods = [m.name for m in pkgutil.iter_modules(photomesh.__path__)]; [importlib.import_module('photomesh.' + m) for m in mods]; from photomesh import diag, APP_VERSION; print('Update OK, version', APP_VERSION, '-', len(mods), 'modules - log folder:', diag.log_dir())"
$rc = $LASTEXITCODE
Pop-Location
if ($rc) { Say "The import check failed. Use rollback.bat to go back to $oldVer, and send the message above." Red; Done 1 }
$now = (GQ describe --tags --always)
Say "PhotoMesh Studio $newVer is installed ($now). Start it from the desktop shortcut as usual." Green
Done 0
