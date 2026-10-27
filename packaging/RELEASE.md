# Build the Windows installer

Each release produces one offline file: `PhotoMeshStudio_Setup_<version>.exe`.
It contains the app, Python, Python packages, COLMAP, and GLOMAP. Users run
the new Setup on the existing installation; it replaces the managed app,
Python, and tools folders. Projects, source photos, and processing results
should be kept outside the installation folder.

Build from the new repository checkout with Inno Setup 7:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/build_release.ps1 `
  -RuntimeSource "C:\path\to\installed\PhotoMesh Studio" `
  -ISCC "C:\path\to\Inno Setup\ISCC.exe" `
  -OutputDir "C:\path\to\release-output" `
  -StageDir "C:\path\to\fresh-stage"
```

`RuntimeSource` may point to a tested development folder containing
`photomesh-studio\.venv`, `tools\colmap`, and `tools\glomap`; or to an
existing installer-based PhotoMesh Studio installation containing
`python\python.exe`, `app\runtime.id`, and the two tools folders.
The source is read but not changed. The builder validates copied Python
packages, creates the Setup EXE, and writes a matching `.sha256` file.
Choose a fresh stage directory for each build.

Before publishing, install the new Setup over a test installation, confirm
Help > About shows the new version, and run a small real photo project on
the target hardware. The automated test suite does not exercise a full
COLMAP/GPU reconstruction. The EXE is not code signed.
