# Creates "PhotoMesh Studio" on the desktop (and in the Start menu) pointing at this package.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $root "photomesh-studio"
$target = Join-Path $app ".venv\Scripts\pythonw.exe"
# the app icon inside the program folder (tracked in git, always present);
# never python(w).exe's own icon
$icon = Join-Path $app "photomesh\resources\photomesh.ico"
if (-not (Test-Path $icon)) { $icon = Join-Path $root "photomesh.ico" }
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))) {
    if (-not $dir) { continue }
    $lnk = $sh.CreateShortcut((Join-Path $dir "PhotoMesh Studio.lnk"))
    $lnk.TargetPath = $target
    $lnk.Arguments = '"' + (Join-Path $app "main.py") + '"'
    $lnk.WorkingDirectory = $app
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Description = "PhotoMesh Studio (offline photogrammetry)"
    $lnk.Save()
    Write-Host "Shortcut: $($lnk.FullName)"
}
