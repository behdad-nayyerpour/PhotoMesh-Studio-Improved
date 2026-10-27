"""One-time repair of the desktop / Start-menu shortcut icon (Windows).

Older setups created "PhotoMesh Studio.lnk" with python(w).exe's icon. The
updater that installs a new version is still the previous version's script,
so the app itself runs packaging/fix_shortcut_icon.ps1 once per version
(hidden, in the background, offline). The script changes only IconLocation
of shortcuts that start this install."""
from __future__ import annotations

import os
import subprocess
import sys

KEY = "startup/shortcut_icon_checked"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "packaging", "fix_shortcut_icon.ps1")


def command(repo=ROOT):
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", SCRIPT, "-Repo", repo]


def maybe_fix(settings, version, popen=subprocess.Popen, platform=None):
    """Start the repair once per app version on Windows. Returns True if started."""
    if (platform or sys.platform) != "win32" or not os.path.exists(SCRIPT):
        return False
    if settings.value(KEY, "") == version:
        return False
    settings.setValue(KEY, version)
    try:
        popen(command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=0x08000000)        # CREATE_NO_WINDOW
    except Exception:  # noqa: BLE001 - cosmetic, never stop the app
        return False
    return True
