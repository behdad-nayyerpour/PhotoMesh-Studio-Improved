# Build the Windows release EXEs

This release process creates two files for each `APP_VERSION`:

- `PhotoMeshStudio_Setup_<version>.exe` — complete offline installation
  with Python, Python packages, COLMAP, and GLOMAP.
- `PhotoMeshStudio_Update_<version>.exe` — installs the new app into an
  existing installation. By default it contains only app files, so it is
  much smaller than the full installer.

Both files are created from the **new repository's checkout**. The runtime is
read from a tested PhotoMeshStudio installation, and that installation is never
modified. The output contains no project files or source photos.

## Build

Use Inno Setup 7's `ISCC.exe`. Update `APP_VERSION` and `CHANGES.md`, run the
test suite, then execute:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/build_release.ps1 `
  -RuntimeSource "C:\path\to\tested\PhotoMeshStudio" `
  -ISCC "C:\path\to\Inno Setup\ISCC.exe" `
  -OutputDir "C:\path\to\release-output" `
  -StageDir "C:\path\to\fresh-stage" `
  -StateDir "C:\path\to\release-state"
```

`RuntimeSource` must have `photomesh-studio\.venv` and
`tools\colmap`, `tools\glomap`. The script checks that the copied
runtime can import the app's main dependencies before compiling. It writes a
SHA-256 file beside each EXE. Use a fresh stage directory for every build.
Keep `StateDir\runtime.id` between releases. If the runtime fingerprint
changes, the default build stops instead of silently making a full-size
updater. Build against the same tested runtime as the prior release to make
a small update. When the runtime really must change, explicitly pass
`-BundleRuntimeInUpdate`; that update EXE will be similar in size to Setup.
The build log prints `bundled runtime: 0` for the small updater and `1` for
the full runtime update. If `StateDir\runtime.id` is missing, verify the
installed runtime ID before distributing the code-only updater.

The EXEs are not code-signed. Sign them with your own certificate before
public distribution. The full installer and updater use the same Inno Setup
AppId, and the updater checks the selected installation before changing files.
Projects (`*.pmsx`, `*.files`) and original photos belong outside the app
installation and are not included in either EXE.

## Smoke check

1. Install the full EXE to a clean test folder.
2. Start PhotoMesh Studio and confirm COLMAP is found.
3. Close the app and run the update EXE against that folder.
4. Reopen the app and check Help > About.
5. Test a small photo set with COLMAP/GPU on the target computer.

The automated test suite covers source code. A real dense reconstruction needs
a compatible NVIDIA driver and dataset, so verify it on the target hardware
before releasing to other users.
