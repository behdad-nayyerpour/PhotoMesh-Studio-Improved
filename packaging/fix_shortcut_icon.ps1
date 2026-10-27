<#
Repairs the icon of existing "PhotoMesh Studio.lnk" shortcuts (desktop and Start menu,
per-user and all-users) that start THIS install but show python(w).exe's icon.
Only IconLocation is changed - target, arguments, working folder and name stay as they are.
Run by update.ps1 after an update, and once by the app itself (photomesh/shortcut_icon.py).
Offline; prints one line per fixed shortcut.
#>
param([Parameter(Mandatory = $true)][string]$Repo)
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path $Repo).Path.TrimEnd('\', '/')
$Root = Split-Path $Repo
$ico = Join-Path $Repo "photomesh\resources\photomesh.ico"
if (-not (Test-Path $ico)) { exit 0 }
try { $sh = New-Object -ComObject WScript.Shell } catch { exit 0 }
$dirs = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"),
          [Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("CommonPrograms"))
foreach ($d in $dirs) {
    if (-not $d) { continue }
    $p = Join-Path $d "PhotoMesh Studio.lnk"
    if (-not (Test-Path $p)) { continue }
    try {
        $lnk = $sh.CreateShortcut($p)
        $all = ($lnk.TargetPath + " " + $lnk.Arguments + " " + $lnk.WorkingDirectory).ToLower()
        $mine = $all.Contains($Repo.ToLower()) -or $lnk.TargetPath.ToLower().StartsWith($Root.ToLower() + "\")
        if (-not $mine) { continue }
        $cur = ($lnk.IconLocation -replace ',\s*-?\d+$', '').Trim('"')
        if ($cur -eq $ico) { continue }             # -eq is case-insensitive
        $lnk.IconLocation = "$ico,0"
        $lnk.Save()
        Write-Host "  shortcut icon fixed: $p"
    } catch {
        Write-Host "  could not update the icon of $p ($($_.Exception.Message))"
    }
}
exit 0
