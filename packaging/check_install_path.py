"""Installer pre-check: Windows MAX_PATH (260 chars) vs. the deepest file the
wheelhouse will create below <app>\\.venv\\Lib\\site-packages (unless long
paths are enabled in Windows). Usage: python check_install_path.py <app dir> <wheelhouse>"""
import glob
import os
import sys
import zipfile


def long_paths_enabled():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem")
        return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except Exception:
        return False


def main(app, wheelhouse):
    deepest = max((len(n) for w in glob.glob(os.path.join(wheelhouse, "*.whl"))
                   for n in zipfile.ZipFile(w).namelist()), default=0)
    sp = os.path.join(os.path.abspath(app), ".venv", "Lib", "site-packages") + os.sep
    need = len(sp) + deepest
    max_app = 259 - deepest - len(os.path.join(".venv", "Lib", "site-packages") + os.sep) - 1
    if need < 260 or long_paths_enabled():
        print(f"Install path OK ({need} of 259 characters)")
        return 0
    print(f"ERROR: this folder path is too long for Windows ({len(os.path.abspath(app))} characters,"
          f" at most {max_app} allowed):\n  {os.path.abspath(app)}\n"
          f"Move the PhotoMeshStudio folder to a short path such as C:\\PhotoMeshStudio and run"
          f" install_offline.bat again.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
