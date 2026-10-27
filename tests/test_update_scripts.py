"""Static checks of the git update tooling (update.bat, rollback.bat, setup_git_update.bat, update.ps1)."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://github.com/behdad-nayyerpour/photomesh-studio-improved.git"
SCRIPTS = ["update.bat", "rollback.bat", "setup_git_update.bat", "packaging/update.ps1"]


def _read(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


@pytest.mark.parametrize("rel", SCRIPTS)
def test_scripts_are_ascii(rel):
    # Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI and cmd uses the OEM code page.
    data = _read(rel)
    assert all(b < 128 for b in data), f"{rel} has non-ASCII bytes"


def test_bat_wrappers_run_temp_copy_in_one_line():
    for rel in ("update.bat", "rollback.bat"):
        lines = [l for l in _read(rel).decode().splitlines() if "powershell" in l]
        assert len(lines) == 1
        assert lines[0].startswith("copy /y") and lines[0].rstrip().endswith("&& exit /b 0 || exit /b 1"), rel
        assert "packaging\\update.ps1" in lines[0]


def test_setup_uses_repo_url_and_keeps_data():
    s = _read("setup_git_update.bat").decode()
    assert URL in s
    assert "--from-temp" in s                     # re-launches from %TEMP% before rewriting files
    assert "reset -q --mixed origin/main" in s     # index first, working tree untouched
    assert "stash push" in s                       # differing old files are kept
    assert "clean -" not in s and "del /s" not in s  # never removes untracked files (.venv, projects)
    assert 'rmdir /s /q "%APP%\\.git"' in s        # only its own new .git on failure


def test_update_ps1_never_kills_or_cleans():
    s = _read("packaging/update.ps1").decode().lower()
    for bad in ("stop-process", "taskkill", "git clean", "reset --hard", "remove-item -recurse"):
        assert bad not in s, bad
    assert "merge --ff-only" in s and "reset --keep" in s


def test_run_stops_if_first_time_setup_fails():
    s = _read("run.bat").decode()
    assert "call setup.bat || exit /b 1" in s
    assert 'if not exist .venv\\Scripts\\pythonw.exe (' in s


def test_gitattributes_line_endings():
    s = _read(".gitattributes").decode()
    assert "*.bat  text eol=crlf" in s and "*.ps1  text eol=crlf" in s and "*.py   text eol=lf" in s


@pytest.mark.skipif(not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")),
                    reason="needs a git checkout")
def test_gitignore_keeps_user_data_out():
    paths = [".venv/Scripts/python.exe", "proj/site.pmsx", "proj/site.files/x.bin", "photomesh/__pycache__/a.pyc",
             "dist/x.zip", "bench_drone5/a.ply", "tools/colmap/bin/colmap.exe", "memtest/x", "sync_a.zip",
             ".pytest_cache/v", "photos/DJI_0001.JPG", "run.log", ".env", ".vscode/settings.json"]
    r = subprocess.run(["git", "-C", ROOT, "check-ignore", "--no-index", "-v", *paths], capture_output=True, text=True)
    ignored = {l.split("\t")[-1] for l in r.stdout.splitlines()}
    assert ignored == set(paths), set(paths) - ignored
    keep = ["photomesh/dense.py", "tests/data/shapes/fi.shp", "packaging/update.ps1", "tools/e2e_test.py"]
    r = subprocess.run(["git", "-C", ROOT, "check-ignore", "--no-index", *keep], capture_output=True, text=True)
    assert r.stdout.strip() == ""


def _labels(s):
    """label -> its lines, for the batch file."""
    out, cur = {}, None
    for l in s.splitlines():
        if l.startswith(":") and not l.startswith("::"):
            cur = l[1:].strip().lower(); out[cur] = []
        elif cur:
            out[cur].append(l.strip())
    return out


def test_setup_existing_checkout_without_updater():
    """An existing .git without packaging\\update.ps1 (older / unfinished
    checkout, 0.1.4 portable zip) is repaired instead of failing with
    'pms_update_setup.ps1 does not exist'; that path never deletes .git."""
    s = _read("setup_git_update.bat").decode()
    L = _labels(s)
    al = L["already"]
    assert any("remote get-url origin" in l and "remote add origin" in l for l in al)
    assert any('not exist "%APP%\\packaging\\update.ps1"' in l and "FIX=1" in l for l in al)
    assert any("call :download || goto :nofetch_keep" in l for l in al)
    assert any("call :adopt" in l for l in al)
    assert not any(l.endswith(("goto :undo", "goto :nofetch")) for l in al)
    assert not any("rmdir" in l for l in L["nofetch_keep"])
    ru = L["runupdate"]
    assert any("git show origin/main:packaging/update.ps1" in l for l in ru)
    assert any("goto :noscript" in l for l in ru)
    assert "copy /y \"%APP%\\packaging\\update.ps1\" \"%TEMP%\\pms_update_setup.ps1\" >nul\r\npowershell" not in s
    for lab in ("checkrun", "download", "adopt"):                    # subroutines return
        assert any("exit /b" in l for l in L[lab] + L.get(lab + "_clean", [])), lab
    assert s.count("call :checkrun") == 2


def _git(cwd, *a, **k):
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, **k)
    assert r.returncode == 0 or k.get("check") is False, (a, r.stderr)
    return r


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_adopt_steps_on_old_checkout(tmp_path):
    """Run the git commands of setup_git_update.bat's :adopt block on a copy of
    an old checkout (no packaging/, local edit, untracked .venv + project)."""
    import shlex
    if os.name == "nt":
        git_root = os.path.dirname(os.path.dirname(shutil.which("git")))
        shell = os.path.join(git_root, "bin", "sh.exe")
        probe = subprocess.run([shell, "-c", "true"], capture_output=True, text=True)
        if "NtCreateDirectoryObject" in probe.stderr:
            pytest.skip("Git Bash cannot create its IPC objects in this sandbox")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp_path / "gc"), GIT_CONFIG_NOSYSTEM="1")
    up = tmp_path / "origin"; up.mkdir()
    _git(up, "init", "-q", "-b", "main", env=env)
    (up / "main.py").write_text("v1\n"); (up / "a.py").write_text("a1\n")
    _git(up, "add", "."); _git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "old", env=env)
    old = _git(up, "rev-parse", "HEAD").stdout.strip()
    (up / "packaging").mkdir(); (up / "packaging" / "update.ps1").write_text("# updater\n" * 20)
    (up / "a.py").write_text("a2\n")
    _git(up, "add", "."); _git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "new", env=env)
    app = tmp_path / "photomesh-studio"; app.mkdir()
    _git(app, "init", "-q", "-b", "master", env=env)                 # some older layout
    _git(app, "remote", "add", "origin", str(up), env=env)
    _git(app, "fetch", "-q", "origin", old, env=env)
    _git(app, "reset", "-q", "--hard", "FETCH_HEAD", env=env)
    (app / "a.py").write_text("local edit\n")
    (app / ".venv").mkdir(); (app / ".venv" / "python.exe").write_text("x")
    (app / "site.pmsx").write_text("project")
    _git(app, "fetch", "-q", "origin", "--tags", env=env)
    block = _labels(_read("setup_git_update.bat").decode())
    for l in block["adopt"] + block["adopt_clean"]:
        if not l.startswith("git "):
            if l.startswith("for /f") and "ls-files -d" in l:
                for f in _git(app, "ls-files", "-d", env=env).stdout.split():
                    _git(app, "checkout", "-q", "--", f, env=env)
            continue
        cmd = shlex.split(l.split("||")[0].replace(">nul", ""))
        r = _git(app, *cmd[1:], env=env, check=False)
        if cmd[1:3] != ["diff", "--quiet"]:
            assert r.returncode == 0, (l, r.stderr)
    assert (app / "packaging" / "update.ps1").exists()
    assert (app / "a.py").read_text() == "a2\n"
    assert (app / ".venv" / "python.exe").exists() and (app / "site.pmsx").read_text() == "project"
    assert _git(app, "rev-parse", "--abbrev-ref", "HEAD", env=env).stdout.strip() == "main"
    assert _git(app, "rev-parse", "HEAD", env=env).stdout == _git(up, "rev-parse", "HEAD").stdout
    assert "before git setup" in _git(app, "stash", "list", env=env).stdout      # local edit kept
    assert _git(app, "status", "--porcelain", "--untracked-files=no", env=env).stdout == ""
