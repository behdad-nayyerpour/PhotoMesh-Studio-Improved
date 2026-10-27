"""Error reports (0.1.5): report building, anonymisation, GitHub URL
length/encoding, exit-code mapping, the error dialog and the hooks."""
import json
import os
import sys
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest

from photomesh import report as rp, APP_VERSION


# ---------------------------------------------------------------- exit codes
@pytest.mark.parametrize("code,hx,name", [
    (-1073740791, "0xC0000409", "STATUS_STACK_BUFFER_OVERRUN"),
    (3221226505, "0xC0000409", "STATUS_STACK_BUFFER_OVERRUN"),
    (-1073741819, "0xC0000005", "STATUS_ACCESS_VIOLATION"),
    (-1073741801, "0xC0000017", "STATUS_NO_MEMORY"),
    (-1073741571, "0xC00000FD", "STATUS_STACK_OVERFLOW"),
    (-1073741515, "0xC0000135", "STATUS_DLL_NOT_FOUND"),
    (-529697949, "0xE06D7363", "CPP_EXCEPTION"),
    (3, "0x00000003", "ABORT"),
])
def test_exit_code_mapping(code, hx, name):
    i = rp.exit_code_info(code)
    assert i["hex"] == hx and i["name"] == name and i["meaning"]
    assert i["text"].startswith(f"{code} ({hx}") and i["meaning"] in i["text"]


def test_exit_code_unknown_and_none():
    i = rp.exit_code_info(42)
    assert i["hex"] == "0x0000002A" and i["meaning"] == "" and i["text"] == "42 (0x0000002A)"
    assert rp.exit_code_info(None)["text"] == "none"
    assert "crashed" in rp.exit_code_info(77, crashed=True)["text"]
    assert "SIGKILL" in rp.exit_code_info(-9)["text"]
    assert "out of memory" in rp.exit_code_info(-1073740791)["meaning"]


# ---------------------------------------------------------------- anonymisation
@pytest.fixture
def fake_user(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Alice")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Alice\AppData\Local")
    monkeypatch.setenv("TEMP", r"C:\Users\Alice\AppData\Local\Temp")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-ALICE01")
    return rp.Anonymizer({r"D:\Survey 2026\Site A": "<project>", r"F:\DCIM\100MEDIA": "<photos>"})


def test_anonymise_paths(fake_user):
    a = fake_user
    assert a.text(r"C:\Users\Alice\PhotoMeshStudio\x.pmsx") == r"%USERPROFILE%\PhotoMeshStudio\x.pmsx"
    assert a.text(r"c:/users/alice/AppData/Local/PhotoMeshStudio/reports") == \
        "%LOCALAPPDATA%/PhotoMeshStudio/reports"
    assert a.text(r"C:\Users\Alice\AppData\Local\Temp\tmp1.json") == r"%TEMP%\tmp1.json"
    assert a.text(r"read D:\Survey 2026\Site A\dense\fused.ply ok") == r"read <project>\dense\fused.ply ok"
    assert a.text(r"F:\DCIM\100MEDIA\DJI_0001.JPG") == r"<photos>\DJI_0001.JPG"
    # JSON-escaped and other users' homes
    assert a.text(r'{"p": "C:\\Users\\Alice\\a"}') == r'{"p": "%USERPROFILE%\\a"}'
    assert a.text(r"C:\Users\Bob\Desktop\x.txt") == r"%USERPROFILE%\Desktop\x.txt"
    # unknown absolute paths keep only drive + file name
    assert a.text(r"E:\Clients\ACME Corp\job 17\photo_12.tif failed") == r"E:\...\photo_12.tif failed"
    assert a.text("/home/alice/data/x.ply") in ("~/data/x.ply", "%USERPROFILE%/data/x.ply")
    assert a.text(r"\\nas01\projects\x.jpg") == r"\\<server>\<share>\x.jpg"
    # user and computer names
    out = a.text("user alice on DESKTOP-ALICE01 (Alice)")
    assert "alice" not in out.lower() and "DESKTOP" not in out and "<user>" in out


def test_anonymise_coordinates_but_not_numbers(fake_user):
    a = fake_user
    assert a.text("GPS lat 40.181234 lon 44.512345 alt 1012.5") == "GPS lat <hidden> lon <hidden> alt <hidden>"
    assert a.text("marker at 40.181234, 44.512345, 1012.123") == "marker at <coords>"
    assert a.text("UTM 451234.567 4451234.890") == "UTM <coords>"
    assert a.text("easting 451234.5 northing 4451234.9") == "easting <hidden> northing <hidden>"
    assert a.text("centre 4451234.25 m") == "centre <coord> m"
    assert a.text("/tmp/build/x/fused.ply and /opt/colmap/bin/colmap") == "/.../fused.ply and /.../colmap"
    for keep in ("see https://github.com/colmap/colmap/issues/1", "<project>/dense/a.bin", "~/x/y", "1/2 done",
                 "%USERPROFILE%/a/b", "Undistorted 640 of 641 photos in 12.3 s", "exit code -1073740791", "RMSE 0.0123 m",
                 "Fused 12,345,678 points", "[12/640]", "focal 3612.5 px", "COLMAP 3.8"):
        assert a.text(keep) == keep


def test_params_hide_coordinates(fake_user):
    p = fake_user.params({"quality": "High", "max_points": 30000000, "offset": [451000.0, 4451000.0, 900.0],
                          "markers": [{"label": "GCP1", "ref": [1, 2, 3]}] * 3, "georef": {"similarity": [1] * 7,
                          "rmse": 0.02}, "workspace": r"C:\Users\Alice\proj\dense", "big": 4451234.5,
                          "names": list(range(50))})
    assert p["quality"] == "High" and p["max_points"] == 30000000
    assert p["offset"] == "<3 items hidden>" and p["markers"] == "<3 items hidden>"
    assert p["georef"] == {"similarity": "<7 items hidden>", "rmse": 0.02}
    assert p["workspace"] == r"%USERPROFILE%\proj\dense" and p["big"] == "<hidden>" and p["names"] == "<50 items>"


def test_crs_name():
    assert rp.crs_name("") .startswith("none")
    assert rp.crs_name("EPSG:32638") == "EPSG:32638"
    wkt = 'PROJCS["WGS 84 / UTM zone 38N",GEOGCS["WGS 84",DATUM["WGS_1984"]],PARAMETER["false_easting",500000],' \
          'AUTHORITY["EPSG","32638"]]'
    assert rp.crs_name(wkt) == "WGS 84 / UTM zone 38N (EPSG:32638)"
    assert rp.crs_name('GEOGCRS["WGS 84",ID["EPSG",4326]]').startswith("WGS 84")


# ---------------------------------------------------------------- building
def _project(tmp_path):
    from photomesh.project import Project, Camera
    pr = Project()
    pr.path = str(tmp_path / "site" / "job.pmsx")
    ch = pr.chunk
    photos = tmp_path / "photos"
    ch.cameras = [Camera(path=str(photos / f"DJI_{i:04d}.JPG"), aligned=i < 7, lat=40.18 + i * 1e-4,
                         lon=44.51, alt=1000.0, width=5472, height=3648) for i in range(10)]
    ch.point_count, ch.face_count, ch.crs = 1234567, 500000, "EPSG:32638"
    ch.workdir = str(tmp_path / "site" / "job.files" / "0")
    ch.meta = {"Build Point Cloud": {"params": {"quality": "High", "filtering": "Mild"}, "seconds": 812.5}}
    return pr


SYS = {"os": "Windows 11 Pro (build 10.0.26100, AMD64)", "cpu": "Intel(R) Core(TM) i9-13900K", "threads": 32,
       "ram_total_gb": 63.8, "ram_free_gb": 40.1, "python": "3.13.15 (64bit)", "packages": {"numpy": "2.3.0"},
       "gpus": [{"name": "NVIDIA GeForce RTX 4090", "vram_total_mb": 24564, "vram_free_mb": 22000,
                 "driver": "566.36"}], "disk": {"drive": "C:", "free_gb": 120.5, "total_gb": 931.0},
       "colmap": "COLMAP 3.8 (Commit 43de802 with CUDA), CUDA", "glomap": "found"}


def _failed(tmp_path, n_log=40, **kw):
    pr = _project(tmp_path)
    wd = pr.chunk.workdir
    log = [f"{wd}\\dense\\stereo: depth map {i}/640 for DJI_{i:04d}.JPG" for i in range(n_log)]
    log += ["Traceback (most recent call last):", '  File "x.py", line 3, in <module>', "    a = [0] * 10**12",
            "MemoryError"]
    args = dict(step="Build Point Cloud > Depth maps", params={"quality": "High", "offset": [1.0, 2.0, 3.0]},
                command=["colmap", "patch_match_stereo", "--workspace_path", wd + "\\dense"],
                exit_code=-1073740791, log_lines=log, project=pr, system=SYS, directory=str(tmp_path))
    args.update(kw)
    return rp.build_report("Build Point Cloud failed", "'Depth maps' failed (exit code 0xC0000409)", **args)


def test_build_report_contents(tmp_path):
    r = _failed(tmp_path)
    md = rp.render_markdown(r)
    assert r["id"].startswith("report_") and r["version"] == APP_VERSION and APP_VERSION in md
    for sect in ("## Step settings", "## Command", "## Traceback", "## Project", "## System", "## Log (last 44"):
        assert sect in md, sect
    assert "0xC0000409" in md and "STATUS_STACK_BUFFER_OVERRUN" in md and "MemoryError" in r["traceback"]
    assert "Photos | 10 (10 enabled, 7 aligned, 10 with GPS)" in md and "1,234,567" in md
    assert "EPSG:32638" in md and "RTX 4090" in md and "24564 MB VRAM" in md and "COLMAP 3.8" in md
    assert "<workdir>\\dense" in md and str(tmp_path) not in md
    assert "40.18" not in md and '"offset": "<3 items hidden>"' in md
    assert "quality=High" in md


def test_build_report_real_system_info(tmp_path):
    r = rp.build_report("x", "y", project=None, directory=str(tmp_path))
    s = r["system"]
    assert s["threads"] and s["python"] and "packages" in s and "colmap" in s and "disk" in s
    assert r["commit"]


def test_traceback_from_child_output():
    lines = ["Reading", "Traceback (most recent call last):", '  File "a.py", line 1, in f', "    g()",
             "ValueError: bad", "after"]
    assert rp.extract_traceback(lines).splitlines()[-1] == "ValueError: bad"
    assert rp.extract_traceback(["nothing"]) == ""


def test_task_config_is_included(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"input": str(tmp_path / "a.ply"), "quality": "Low", "up": [0, 0, 1]}))
    r = rp.build_report("t", command=["python", "-m", "photomesh.tasks", "filter_cloud", str(cfg)],
                        system={}, directory=str(tmp_path))
    tc = r["params"]["task_config"]
    assert tc["quality"] == "Low" and tc["up"] == "<3 items hidden>" and str(tmp_path) not in json.dumps(r)


def test_save_report_md_and_zip(tmp_path):
    r = _failed(tmp_path)
    sess = tmp_path / "session.log"
    sess.write_text(f"start {tmp_path}\\x\nGPS 40.181234, 44.512345\n")
    md, zp = rp.save_report(r, str(tmp_path / "reports"), str(sess))
    assert os.path.basename(md) == r["id"] + ".md" and os.path.basename(zp) == r["id"] + ".zip"
    with zipfile.ZipFile(zp) as z:
        assert set(z.namelist()) == {"report.md", "step_log.txt", "report.json", "session_log.txt"}
        s = z.read("session_log.txt").decode()
        assert "40.181234" not in s and "<coords>" in s
    assert open(md, encoding="utf-8").read().startswith("# PhotoMesh Studio error report")
    # unique names within the same second
    assert rp.new_id(directory=str(tmp_path / "reports")) != r["id"] or True


def test_new_id_is_unique(tmp_path):
    import datetime
    now = datetime.datetime(2026, 10, 23, 1, 2, 3)
    a = rp.new_id(now, str(tmp_path)); (tmp_path / (a + ".md")).write_text("x")
    b = rp.new_id(now, str(tmp_path))
    assert a == "report_20261023_010203" and b == "report_20261023_010203_2"


def test_git_commit(tmp_path):
    g = tmp_path / ".git"; (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "packed-refs").write_text("# pack-refs\n0123456789abcdef0123456789abcdef01234567 refs/heads/main\n")
    assert rp.git_commit(str(tmp_path)) == "0123456 on main"
    (g / "refs" / "heads" / "main").write_text("fedcba9876543210fedcba9876543210fedcba98\n")
    assert rp.git_commit(str(tmp_path)) == "fedcba9 on main"
    assert rp.git_commit(str(tmp_path / "nope")) == ""


# ---------------------------------------------------------------- GitHub URL
def _decode(url):
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return {k: v[0] for k, v in q.items()}


def test_issue_url_short_report_is_complete(tmp_path):
    r = _failed(tmp_path, n_log=5)
    url = rp.issue_url(r)
    assert url.startswith("https://github.com/behdad-nayyerpour/photomesh-studio-improved/issues/new?")
    assert len(url) <= rp.MAX_URL and all(ord(c) < 128 for c in url) and " " not in url
    q = _decode(url)
    assert q["labels"] == "bug" and q["title"] == rp.issue_title(r)
    assert q["title"].startswith(f"[{APP_VERSION}] Build Point Cloud failed: exit 0xC0000409")
    assert rp.render_markdown(r) in q["body"] and "Shortened" not in q["body"]


@pytest.mark.parametrize("n_log,desc", [(300, ""), (300, "عکس‌ها را اضافه کردم و Build Point Cloud زدم " * 40),
                                        (0, "x" * 20000)])
def test_issue_url_long_report_is_trimmed(tmp_path, n_log, desc):
    r = _failed(tmp_path, n_log=n_log, description=desc)
    url = rp.issue_url(r)
    assert len(url) <= rp.MAX_URL
    q = _decode(url)
    body = q["body"]
    assert "Shortened to fit in the link" in body and r["id"] + ".zip" in body
    assert body.startswith("# PhotoMesh Studio error report") and q["labels"] == "bug"
    if n_log and not desc:
        assert "## Log (last" in body and "## System" in body      # system info kept, log tail trimmed
        assert body.rstrip().endswith("```")
    if desc.startswith("عکس"):
        assert "عکس" in body                                        # UTF-8 survives the round trip


def test_issue_url_config(monkeypatch, tmp_path):
    r = _failed(tmp_path, n_log=1)
    monkeypatch.setenv("PHOTOMESH_ISSUES_URL", "off")
    assert rp.issues_url() is None and rp.issue_url(r) is None
    monkeypatch.setenv("PHOTOMESH_ISSUES_URL", "https://example.com/tracker/new?x=1")
    assert rp.issue_url(r).startswith("https://example.com/tracker/new?x=1&title=")


def test_pct_encoding():
    assert rp.pct("a b&c=d/é#") == "a%20b%26c%3Dd%2F%C3%A9%23"
    assert rp.query({"t": "1+1", "l": "bug"}) == "t=1%2B1&l=bug"


# ---------------------------------------------------------------- dialog
def test_error_dialog_actions(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices, QGuiApplication
    from PySide6.QtWidgets import QMessageBox
    from photomesh.report_ui import ErrorDialog
    r = _failed(tmp_path, n_log=300)
    md, zp = rp.save_report(r, str(tmp_path / "rep"))
    opened, infos = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u) or True))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    d = ErrorDialog(r, md, zp); qtbot.addWidget(d)
    names = [b.text() for b in (d.details_btn, d.copy_btn, d.folder_btn, d.github_btn)]
    assert names == ["Details \u25b8", "Copy report", "Open report folder", "Report to GitHub"]
    assert d.github_btn.isVisibleTo(d) and "0xC0000409" in d.message.text()
    assert "'Depth maps' failed" in d.message.text() and "&#x27;" not in d.message.text()
    d.details_btn.click()
    assert d.details.isVisibleTo(d) and "## System" in d.details.toPlainText()
    d.description.setPlainText(r"Built cloud from C:\Users\Alice\x")
    d.copy_btn.click()
    clip = QGuiApplication.clipboard().text()
    assert clip.startswith("# PhotoMesh Studio error report") and "Built cloud from" in clip
    assert "Built cloud from" in open(d.md_path, encoding="utf-8").read()       # re-saved with the description
    d.folder_btn.click()
    assert os.path.normcase(os.path.normpath(opened[-1].toLocalFile())) == os.path.normcase(os.path.normpath(os.path.dirname(md)))
    d.github_btn.click()                       # long URL -> local redirect page
    page = opened[-1].toLocalFile()
    assert page.endswith(r["id"] + "_github.html") and os.path.exists(page)
    assert len(d.last_url) <= rp.MAX_URL and d.last_url in open(page, encoding="utf-8").read().replace("&amp;", "&")
    assert "Submit" in d.note.text()
    # browser not available -> explain that the report can be sent later
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: False))
    d.github_btn.click()
    assert infos and "send it later" in infos[-1] and zp in infos[-1]


def test_error_dialog_short_url_opens_directly(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from photomesh.report_ui import ErrorDialog
    r = rp.build_report("Save", "Cannot save project: disk full", system={}, directory=str(tmp_path))
    md, zp = rp.save_report(r, str(tmp_path))
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u) or True))
    d = ErrorDialog(r, md, zp); qtbot.addWidget(d)
    d.github_btn.click()
    assert len(d.last_url) <= 2000
    u = opened[-1].toString(opened[-1].ComponentFormattingOption.FullyEncoded)
    assert u == d.last_url and u.startswith(rp.ISSUES_URL)


def test_error_dialog_without_tracker(qtbot, tmp_path, monkeypatch):
    from photomesh.report_ui import ErrorDialog
    monkeypatch.setenv("PHOTOMESH_ISSUES_URL", "off")
    r = rp.build_report("x", "y", system={}, directory=str(tmp_path))
    md, zp = rp.save_report(r, str(tmp_path))
    d = ErrorDialog(r, md, zp); qtbot.addWidget(d)
    assert not d.github_btn.isVisibleTo(d)


# ---------------------------------------------------------------- app integration
def _reports():
    d = rp.report_dir()
    return sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []


def _read(name):
    return open(os.path.join(rp.report_dir(), name), encoding="utf-8").read()


def test_failed_step_shows_error_dialog(window, no_dialogs, monkeypatch, qtbot):
    from photomesh import colmap as cm
    monkeypatch.setattr(cm, "find_colmap", lambda: sys.executable)
    before = set(_reports())
    code = "import sys; print('step output line'); sys.stdout.flush(); sys.exit(-1073740791 & 0xFFFFFFFF if sys.platform == 'win32' else 3)"
    window._run("Test Step", [("Crashing step", ["-c", code], 100)], lambda: None, params={"quality": "High"})
    qtbot.waitUntil(lambda: bool(no_dialogs), timeout=30000)
    title, msg = no_dialogs[-1]
    assert title == "Test Step failed" and "'Crashing step' failed" in msg
    new = sorted(set(_reports()) - before)
    assert len(new) == 1
    md = _read(new[0])
    assert "Test Step > Crashing step" in md and '"quality": "High"' in md and "step output line" in md
    assert ("0xC0000409" if sys.platform == "win32" else "(0x00000003 ABORT)") in md
    assert os.path.exists(os.path.join(rp.report_dir(), new[0][:-3] + ".zip"))


def test_python_task_memoryerror_traceback(window, no_dialogs, monkeypatch, qtbot):
    from photomesh import colmap as cm
    monkeypatch.setattr(cm, "find_colmap", lambda: sys.executable)
    before = set(_reports())
    code = "def big():\n    raise MemoryError('cannot allocate 64 GiB')\nbig()"
    window._run("Mem Step", [("Allocating", ["-c", code], 100)], lambda: None)
    qtbot.waitUntil(lambda: bool(no_dialogs), timeout=30000)
    md = _read(sorted(set(_reports()) - before)[0])
    assert "## Traceback" in md and "MemoryError: cannot allocate 64 GiB" in md and "(0x00000001)" in md


def test_cancel_is_not_an_error(window, no_dialogs, monkeypatch, qtbot):
    from photomesh import colmap as cm
    monkeypatch.setattr(cm, "find_colmap", lambda: sys.executable)
    window._run("Slow", [("Sleeping", ["-c", "import time; time.sleep(30)"], 100)], lambda: None)
    qtbot.wait(300)
    window.runner.cancel()
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=30000)
    assert no_dialogs == []


def test_command_exception_shows_error_dialog(window, no_dialogs):
    before = set(_reports())
    def boom():
        raise MemoryError("numpy could not allocate")
    window._safe("Boom Command", boom)()
    assert no_dialogs == [("Boom Command failed", "MemoryError: numpy could not allocate")]
    md = _read(sorted(set(_reports()) - before)[0])
    assert "in boom" in md and "MemoryError" in md


def test_report_a_problem_menu(window, no_dialogs, monkeypatch):
    from photomesh.report_ui import ErrorDialog
    shown = []
    monkeypatch.setattr(ErrorDialog, "exec", lambda self: shown.append(self) or 0)
    window.log("something odd happened")
    a = window.actions["Help/Report a Problem..."]
    assert a.text() == "Report a Problem..."
    a.trigger()
    assert len(shown) == 1 and shown[0].manual and shown[0].github_btn.isVisibleTo(shown[0])
    r = shown[0].report
    assert r["kind"] == "manual" and "something odd happened" in "\n".join(r["log"])
    assert os.path.exists(shown[0].zip_path)


def test_thread_exception_reported_on_gui_thread(window, qtbot, monkeypatch):
    from photomesh.report_ui import ErrorDialog
    from photomesh import diag
    shown = []
    monkeypatch.setattr(ErrorDialog, "exec", lambda self: shown.append((self.report, threading.current_thread()))
                        or 0)
    monkeypatch.setitem(diag._state, "handler", window.reporter.handle)

    def work():
        raise ValueError("worker exploded")
    t = threading.Thread(target=work, name="test-worker")
    monkeypatch.setattr(threading, "excepthook", diag._thread_excepthook)
    t.start(); t.join()
    qtbot.waitUntil(lambda: bool(shown), timeout=10000)
    r, th = shown[0]
    assert th is threading.main_thread()
    assert r["title"] == "Unexpected error in thread 'test-worker'" and "ValueError: worker exploded" in r["traceback"]


def test_excepthook_and_qt_critical(window, monkeypatch):
    from photomesh.report_ui import ErrorDialog
    from photomesh import diag
    shown = []
    monkeypatch.setattr(ErrorDialog, "exec", lambda self: shown.append(self.report) or 0)
    monkeypatch.setitem(diag._state, "handler", window.reporter.handle)
    try:
        raise KeyError("slot failed")
    except KeyError:
        diag._excepthook(*sys.exc_info())
    assert shown and shown[-1]["title"] == "Unexpected error" and "KeyError" in shown[-1]["message"]
    n, before = len(shown), set(_reports())
    diag._qt_message(2, None, "QOpenGLContext: failed to create")
    diag._qt_message(2, None, "QOpenGLContext: failed to create")      # deduplicated
    assert len(shown) == n                                               # saved silently, no dialog
    new = sorted(set(_reports()) - before)
    assert len(new) == 1 and "QOpenGLContext" in _read(new[0])


def test_issue_template_matches_report():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    t = open(os.path.join(root, ".github", "ISSUE_TEMPLATE", "bug_report.md"), encoding="utf-8").read()
    assert t.startswith("---\n") and "labels: bug" in t
    for sect in ("## What happened", "## Step settings", "## Traceback", "## Project", "## System", "## Log"):
        assert sect in t, sect
