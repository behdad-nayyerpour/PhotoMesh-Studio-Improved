"""0.1.7: window title, app icon, splash, busy indicator, CPU/GPU status,
GCPs in the ortho view, project data next to the .pmsx (save before
processing, relative paths, Save As copies the data, migration)."""
import ast
import json
import os
import shutil
import struct
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ 1 title
def test_window_title_shows_version_and_project(window, tmp_path, monkeypatch):
    from photomesh import APP_NAME, APP_VERSION
    from photomesh.mainwindow import window_title
    assert window_title("") == f"{APP_NAME} {APP_VERSION} - Untitled"
    assert window_title(r"C:\x\drone_DJI_001_fast.pmsx", True) == f"{APP_NAME} {APP_VERSION} - drone_DJI_001_fast*"
    window.project.save(str(tmp_path / "site A.pmsx")); window._update_title()
    assert window.windowTitle() == f"{APP_NAME} {APP_VERSION} - site A"


# ------------------------------------------------------------------ 2 icon
def test_app_icon_is_original_multisize_and_installed(window, qtbot):
    from PySide6.QtWidgets import QApplication
    from photomesh import appicon
    ic = appicon.app_icon()
    assert not ic.isNull() and {16, 32, 256} <= {s.width() for s in ic.availableSizes()}
    data = appicon.ico_bytes()
    zero, kind, n = struct.unpack("<HHH", data[:6])
    assert (zero, kind, n) == (0, 1, len(appicon.ICO_SIZES))
    for i in range(n):                                    # PNG entries at the stated offsets
        w, h, _c, _r, _p, _b, size, off = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        assert data[off:off + 8] == b"\x89PNG\r\n\x1a\n" and off + size <= len(data)
    # the committed .ico (shortcut / packages) is a valid multi-size icon with the same sizes;
    # exact bytes depend on the Qt/FreeType build that rendered it, so they are not compared
    disk = open(appicon.ICO_PATH, "rb").read()
    assert struct.unpack("<HHH", disk[:6]) == (0, 1, len(appicon.ICO_SIZES))
    sizes = lambda b: sorted(struct.unpack("<B", b[6 + 16 * i:7 + 16 * i])[0] for i in range(len(appicon.ICO_SIZES)))
    assert sizes(disk) == sizes(data)
    appicon.install(QApplication.instance(), window)
    assert not QApplication.instance().windowIcon().isNull() and not window.windowIcon().isNull()
    # no third-party artwork referenced anywhere in the icon code
    src = open(os.path.join(ROOT, "photomesh", "appicon.py"), encoding="utf-8").read().lower()
    assert "agisoft" not in src.split('"""', 2)[2] and "metashape" not in src


def test_app_user_model_id_only_on_windows(monkeypatch):
    from photomesh import appicon
    monkeypatch.setattr(sys, "platform", "linux")
    assert appicon.set_app_user_model_id() is False
    calls = []

    class Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, s):  # noqa: N802
            calls.append(s); return 0
    import ctypes
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"shell32": Shell32()})(), raising=False)
    assert appicon.set_app_user_model_id() is True and calls == [appicon.APP_USER_MODEL_ID]


# ------------------------------------------------------------------ 3 splash
def test_splash_shown_before_heavy_imports():
    """main(): QApplication -> icon -> splash, and only then NumPy / OpenGL views / main window."""
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    body = src[src.index("def main"):]
    i_app = body.index("QApplication(sys.argv)")
    i_splash = body.index("splash_mod.show()")
    assert i_app < i_splash
    for heavy in ("import numpy", "import photomesh.views", "from photomesh.mainwindow import MainWindow",
                  "MainWindow()"):
        assert i_splash < body.index(heavy), heavy
    assert "set_app_user_model_id()" in body[:i_app]
    # the splash module itself imports only Qt + the package metadata
    tree = ast.parse(open(os.path.join(ROOT, "photomesh", "splash.py"), encoding="utf-8").read())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not mods & {"numpy", "open3d", "pyqtgraph", ".mainwindow", ".views", "scipy"}


def test_splash_messages(qtbot):
    from photomesh import splash
    s = splash.show()
    qtbot.addWidget(s)
    s.step("Loading 3D view (OpenGL)")
    assert s.messages == ["Starting", "Loading 3D view (OpenGL)"]
    assert "Loading 3D view" in s.message()
    s.close()


# ------------------------------------------------------------------ 4 busy
def test_busy_indicator_nested_and_restored():
    from PySide6.QtWidgets import QApplication
    from photomesh.busy import BusyIndicator
    b = BusyIndicator()
    assert QApplication.overrideCursor() is None
    b.begin("Build Point Cloud")
    assert QApplication.overrideCursor() is not None and b.label.text() == "Working: Build Point Cloud..."
    with pytest.raises(ValueError):
        with b("Export"):
            assert b.label.text() == "Working: Export..."
            raise ValueError("boom")
    assert b.titles == ["Build Point Cloud"] and QApplication.overrideCursor() is not None
    b.end(); b.end()                                   # extra end is harmless
    assert QApplication.overrideCursor() is None and not b.titles


def _fake_runner(result):
    from PySide6.QtCore import QObject, Signal

    class FakeRunner(QObject):
        progress = Signal(int, str); detail = Signal(str); log = Signal(str); finished = Signal(bool, str)
        failure = None

        def __init__(self, exe, steps, parent=None):
            super().__init__(parent)

        def start(self):
            from PySide6.QtWidgets import QApplication
            FakeRunner.cursor_while_running = QApplication.overrideCursor() is not None
            FakeRunner.label = FakeRunner.window.busy_indicator.label.text()
            if result == "raise":
                raise OSError("cannot start")
            self.finished.emit(result == "ok", "" if result == "ok" else result)

        def cancel(self):
            pass
    return FakeRunner


@pytest.mark.parametrize("result", ["ok", "step failed", "Cancelled by user", "raise"])
def test_run_shows_busy_and_always_restores_cursor(window, no_dialogs, monkeypatch, result):
    from PySide6.QtWidgets import QApplication
    from photomesh import colmap as cm
    R = _fake_runner(result); R.window = window
    monkeypatch.setattr(cm, "PipelineRunner", R)
    monkeypatch.setattr(cm, "find_colmap", lambda: "colmap")
    if result == "raise":
        with pytest.raises(OSError):
            window._run("Build Point Cloud", [], lambda: None)
    else:
        window._run("Build Point Cloud", [], lambda: None)
    assert R.cursor_while_running and R.label == "Working: Build Point Cloud..."
    assert QApplication.overrideCursor() is None
    assert not window.busy_indicator.active and not window.busy_indicator.label.isVisible()


def test_run_modal_busy_restored_on_error(window):
    from PySide6.QtWidgets import QApplication
    from photomesh.bgjob import run_modal
    seen = []

    def fn(report, cancelled):
        seen.append(window.busy_indicator.active)
        raise RuntimeError("export failed")
    _res, _c, err = run_modal(window, "Export Model", "Exporting...", fn, 0)
    assert "export failed" in str(err) and seen == [True]
    assert QApplication.overrideCursor() is None and not window.busy_indicator.active


# ------------------------------------------------------------------ 5 CPU / GPU
def test_cpu_sampler_without_psutil(monkeypatch):
    from photomesh import statusmon as sm
    s = sm.CpuSampler(); s.ps = None
    x = 0
    for i in range(200000):
        x += i
    v = s.percent()
    if sys.platform.startswith("linux"):
        assert v is not None and 0.0 <= v <= 100.0


def test_gpu_sampler_nvml_fake_and_missing():
    import ctypes
    from photomesh import statusmon as sm
    assert not sm.GpuSampler(loader=lambda: None).available          # no NVIDIA driver -> hidden

    class FakeNvml:
        def nvmlInit_v2(self): return 0
        def nvmlShutdown(self): return 0
        def nvmlDeviceGetHandleByIndex_v2(self, i, h): return 0
        def nvmlDeviceGetName(self, h, buf, n): buf.value = b"NVIDIA GeForce RTX 3060 Laptop GPU"; return 0
        def nvmlDeviceGetUtilizationRates(self, h, p):
            ctypes.cast(p, ctypes.POINTER(sm._Util)).contents.gpu = 57; return 0
        def nvmlDeviceGetMemoryInfo(self, h, p):
            m = ctypes.cast(p, ctypes.POINTER(sm._Mem)).contents
            m.used, m.total = 2048 * 1024 * 1024, 6144 * 1024 * 1024; return 0
    g = sm.GpuSampler(loader=FakeNvml)
    assert g.available and g.name.startswith("NVIDIA")
    assert g.sample() == (57.0, 2048, 6144)
    assert sm.format_gpu(57.0, (2048, 6144)) == "GPU  57%  2.0/6 GB"

    class Broken(FakeNvml):
        def nvmlInit_v2(self): return 9                                  # driver/library mismatch
    assert not sm.GpuSampler(loader=Broken).available


def test_monitor_samples_off_gui_thread_and_updates_status(window, qtbot):
    import threading
    from photomesh import statusmon as sm

    class Cpu:
        def percent(self):
            Cpu.thread = threading.current_thread().name; return 42.0

    class NoGpu:
        available = False; name = ""
        def sample(self): return None
        def close(self): pass
    mon = sm.ResourceMonitor(interval=0.05, cpu=Cpu(), gpu=NoGpu())
    got = []
    mon.sampled.connect(got.append)
    mon.sampled.connect(window._show_resources)
    mon.start()
    qtbot.waitUntil(lambda: len(got) >= 2, timeout=5000)
    mon.stop()
    assert Cpu.thread == "resource-monitor" and got[0]["cpu"] == 42.0 and got[0]["gpu"] is None
    assert window.cpu_label.text() == "CPU  42%" and not window.gpu_label.isVisible()
    window._show_resources({"cpu": 5.0, "gpu": 80.0, "gpu_mem": (1024, 4096), "gpu_name": "NVIDIA X"})
    assert window.gpu_label.isVisible() and window.gpu_label.text().startswith("GPU  80%")
    # the window's own monitor runs in the background (never nvidia-smi)
    assert window.monitor._thread is not None and window.monitor._thread.daemon
    src = open(sm.__file__, encoding="utf-8").read()
    assert "subprocess" not in src and "import psutil" in src


# ------------------------------------------------------------------ 6 GCP in the ortho view
def _dem(path, z=0.0, x0=60.0, y1=160.0, px=0.5, n=256):
    from photomesh import geotiff as gt
    w = gt.TiledTiffWriter(path, gt.overview_levels(n, n), 1, np.float32, 256, "Deflate", nodata=-32767.0,
                           geo=(x0, y1, px, px), crs='LOCAL_CS["Local Coordinates (m)"]')
    w.write_block(0, 0, 0, np.full((n, n, 1), z, np.float32))
    gt.build_overviews(w, nodata=-32767.0); w.close()
    return {"format": "photomesh-dem/1", "dir": os.path.dirname(path), "path": path, "width": n, "height": n,
            "pixel_size": px, "rotation": np.eye(3).tolist(), "offset": [0, 0, 0], "georef": False}


def test_add_marker_in_ortho_view_with_dem_height(window, tmp_path):
    from tests.test_markers_manual import _scene
    from photomesh import georef as g
    ch = _scene(window, tmp_path)
    ch.dem = _dem(str(tmp_path / "dem.tif"))
    lab = window.add_marker_at_ortho(110.0, 100.0)            # Z read from the DEM (0)
    m = next(x for x in ch.markers if x["label"] == lab)
    p = m["projections"]
    assert set(p) == {"img_0001", "img_0002"} and all(v[2] is False for v in p.values())
    assert p["img_0001"][0] == pytest.approx(500 + 1000 * 10 / 60, abs=1e-3)
    X, n = g.triangulate_marker(window._sparse_model(), p)
    assert n == 2 and np.allclose(X, [110, 100, 0], atol=1e-3)
    assert window.ortho_view.markers and window.ortho_view.markers[-1][0] == lab
    assert window.ortho_view.markers[-1][1] == pytest.approx(110.0, abs=1e-3)
    window.undo_markers()
    assert not ch.markers


def test_add_marker_in_ortho_view_signal_and_surface_fallback(window, tmp_path):
    from tests.test_markers_manual import _scene, _ground_ply
    ch = _scene(window, tmp_path)
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"), z=0.0)
    window.ortho_view.addMarkerRequested.emit(130.0, 100.0, None)     # no raster: vertical ray onto the mesh
    m = ch.markers[-1]
    assert set(m["projections"]) == {"img_0002", "img_0003"}
    # outside every photo: refused with a message, nothing added
    assert window.add_marker_at_ortho(1000.0, 1000.0, 0.0) is None and len(ch.markers) == 1


def test_ortho_frame_rotation_roundtrip(window):
    ch = window.chunk
    a = np.radians(30)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])
    ch.orthomosaic = {"rotation": R.tolist(), "offset": [1000.0, 2000.0, 10.0]}
    X = np.array([3.0, -4.0, 5.0])
    P = window.ortho_from_local(X)
    assert np.allclose(P, R @ X + [1000, 2000, 10]) and np.allclose(window.ortho_to_local(P), X)


# ------------------------------------------------------------------ 7 project data
def test_processing_requires_saved_project(window, no_dialogs, monkeypatch, tmp_path, image_dir):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    from photomesh import colmap as cm
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.Cancel))
    monkeypatch.setattr(cm, "find_colmap", lambda: pytest.fail("must not start"))
    for f in (window.align_photos, window.build_point_cloud, window.build_model, window.build_texture,
              window.build_tiled_model, window.build_dem, window.build_orthomosaic, window.optimize_cameras,
              window.refine_markers, window.gradual_selection):
        assert f() is None
    assert len(asked) == 10 and asked[0] == "Align Photos"
    assert window.chunk.workdir == "" and not window.project.path
    with pytest.raises(Exception, match="Save the project"):
        window.project.default_workdir(window.chunk)
    # Save -> Save As dialog right there; cancelled dialog = not started
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Save))
    assert window._require_saved("Align Photos") is False
    out = tmp_path / "site" / "flight1.pmsx"; out.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out)[:-5], "")))
    assert window._require_saved("Align Photos") is True
    assert window.project.path == str(out) and out.exists()
    assert window.project.default_workdir(window.chunk) == str(tmp_path / "site" / "flight1.files" / "chunk0")


def _processed(tmp_path, name="proj"):
    """A saved project with a working folder inside <name>.files, photo links
    and an image_root.txt pointing at them."""
    from PIL import Image
    from photomesh.project import Project, Camera
    photos = tmp_path / "photos"; photos.mkdir(parents=True, exist_ok=True)
    for k in range(2):
        Image.new("RGB", (8, 8)).save(photos / f"IMG_{k}.JPG")
    prj = Project()
    prj.path = str(tmp_path / f"{name}.pmsx")
    ch = prj.chunk
    ch.cameras = [Camera(path=str(photos / f"IMG_{k}.JPG")) for k in range(2)]
    wd = prj.default_workdir(ch)
    os.makedirs(os.path.join(wd, "images"), exist_ok=True)
    os.makedirs(os.path.join(wd, "dense"), exist_ok=True)
    for k in range(2):
        src = photos / f"IMG_{k}.JPG"
        dst = os.path.join(wd, "images", f"IMG_{k}.JPG")
        try:
            os.link(src, dst)
        except OSError:  # restricted Windows filesystems: copy_tree has a copy fallback
            shutil.copy2(src, dst)
    with open(os.path.join(wd, "dense", "point_cloud.ply"), "wb") as f:
        f.write(b"ply\n" + b"0" * 4096)
    with open(os.path.join(wd, "image_root.txt"), "w") as f:
        f.write(os.path.join(wd, "images"))                          # pre-0.1.7: absolute
    ch.point_cloud_ply = os.path.join(wd, "dense", "point_cloud.ply")
    ch.texture = {"dir": os.path.join(wd, "texture"), "obj": os.path.join(wd, "texture", "model.obj")}
    prj.save(prj.path)
    return prj


def test_saved_paths_are_relative_and_photos_referenced(tmp_path):
    from photomesh.project import Project
    prj = _processed(tmp_path)
    raw = json.load(open(prj.path, encoding="utf-8"))["chunks"][0]
    assert raw["workdir"] == "proj.files/chunk0"
    assert raw["point_cloud_ply"] == "proj.files/chunk0/dense/point_cloud.ply"
    assert raw["texture"]["obj"] == "proj.files/chunk0/texture/model.obj"
    assert raw["cameras"][0]["path"] == "photos/IMG_0.JPG"           # under the project folder: relative
    q = Project.load(prj.path)
    assert q.chunk.point_cloud_ply == prj.chunk.point_cloud_ply and q.chunk.cameras[0].path == prj.chunk.cameras[0].path
    assert not os.path.exists(os.path.join(tmp_path, "photos", "proj.files"))   # nothing copied next to photos


def test_save_as_copies_data_keeps_originals(tmp_path):
    from photomesh import datafolder as dfo, colmap as cm
    from photomesh.project import Project
    prj = _processed(tmp_path / "a")
    old_wd = prj.chunk.workdir
    new = str(tmp_path / "b" / "copy.pmsx")
    items = dfo.plan(prj, new, save_as=True)
    assert len(items) == 1 and items[0]["dst"] == os.path.join(str(tmp_path / "b" / "copy.files"), "chunk0")
    assert items[0]["bytes"] - items[0]["parts"]["photo_copies"] < 5000  # results excluding photo copies
    assert dfo.check_space(items)[0]
    progress = []
    done = dfo.execute(items, lambda d, n, t: progress.append((d, n)))
    dfo.apply(done)
    prj.save(new)
    nw = prj.chunk.workdir
    assert nw == items[0]["dst"] and os.path.exists(os.path.join(nw, "dense", "point_cloud.ply"))
    assert os.path.exists(os.path.join(old_wd, "dense", "point_cloud.ply"))       # originals kept
    # Links save space where the filesystem permits them; a sandbox can forbid
    # both hard links and symlinks, in which case the copied image stays valid.
    copied = os.path.join(nw, "images", "IMG_0.JPG")
    assert open(copied, "rb").read() == open(os.path.join(old_wd, "images", "IMG_0.JPG"), "rb").read()
    assert cm.image_dir(nw) == os.path.join(nw, "images")                         # image_root remapped
    assert prj.chunk.point_cloud_ply.startswith(nw) and prj.chunk.texture["obj"].startswith(nw)
    assert not os.path.exists(nw + ".partial") and not dfo.is_incomplete(nw)
    q = Project.load(new)
    assert q.chunk.workdir == nw and json.load(open(new))["chunks"][0]["workdir"] == "copy.files/chunk0"


def test_copy_cancel_and_disk_full_leave_nothing(tmp_path, monkeypatch):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))
    with pytest.raises(InterruptedError):
        dfo.execute(items, cancelled=lambda: True)
    assert not os.path.exists(items[0]["dst"] + ".partial") and not os.path.exists(items[0]["dst"])
    monkeypatch.setattr(dfo, "free_space", lambda p: 1000)
    ok, need, free = dfo.check_space(items)
    assert not ok and free == 1000 and need > 0


def test_legacy_temp_data_is_moved_on_save(tmp_path, monkeypatch):
    import tempfile
    from photomesh import datafolder as dfo
    from photomesh.project import Project, Chunk
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    wd = os.path.join(dfo.legacy_temp_root(), "chunk0"); os.makedirs(os.path.join(wd, "sparse", "0"))
    open(os.path.join(wd, "sparse", "0", "points3D.bin"), "wb").write(b"x" * 100)
    prj = Project(); prj.chunks = [Chunk(workdir=wd, tie_points_ply=os.path.join(wd, "tie.ply"))]
    path = str(tmp_path / "p" / "new.pmsx")
    items = dfo.plan(prj, path, save_as=False)
    assert items and items[0]["move"]
    dfo.apply(dfo.execute(items))
    assert not os.path.exists(wd) and prj.chunk.workdir == os.path.join(str(tmp_path / "p" / "new.files"), "chunk0")
    assert prj.chunk.tie_points_ply == os.path.join(prj.chunk.workdir, "tie.ply")


def test_user_style_project_keeps_working_on_save(tmp_path):
    """bench_drone5\\drone_DJI_001_fast.pmsx with its data in bench_drone5\\fast (absolute
    paths, 0.1.6): opens unchanged, nothing is copied, the next save stores 'fast/...'."""
    from photomesh import datafolder as dfo
    from photomesh.project import Project
    bench = tmp_path / "bench_drone5"; (bench / "fast" / "sparse" / "0").mkdir(parents=True)
    (bench / "fast" / "tie_points.ply").write_bytes(b"ply\n")
    d = {"format": "photomesh-studio/1", "active": 0, "chunks": [{
        "name": "Chunk 1", "workdir": str(bench / "fast"), "tie_points_ply": str(bench / "fast" / "tie_points.ply"),
        "cameras": [{"path": "F:\\DJI_001\\DJI_0001.JPG", "label": "DJI_0001.JPG"}]}]}
    pm = bench / "drone_DJI_001_fast.pmsx"; pm.write_text(json.dumps(d), encoding="utf-8")
    q = Project.load(str(pm))
    assert q.chunk.workdir == str(bench / "fast") and not q.relocated
    assert dfo.plan(q, str(pm), save_as=False) == []                 # inside the project folder: not moved
    q.save(str(pm))
    raw = json.load(open(pm, encoding="utf-8"))["chunks"][0]
    assert raw["workdir"] == "fast" and raw["tie_points_ply"] == "fast/tie_points.ply"
    assert raw["cameras"][0]["path"] == "F:\\DJI_001\\DJI_0001.JPG"   # photos elsewhere: absolute, untouched
    assert Project.load(str(pm)).chunk.tie_points_ply == str(bench / "fast" / "tie_points.ply")


def test_gui_save_as_copies_with_progress(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    from photomesh.project import Project
    prj = _processed(tmp_path / "a")
    window.open_project(prj.path)
    new = tmp_path / "b" / "moved.pmsx"; new.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    assert window.save_project_as()
    assert window.project.path == str(new)
    assert window.chunk.workdir == str(tmp_path / "b" / "moved.files" / "chunk0")
    assert os.path.exists(os.path.join(window.chunk.workdir, "dense", "point_cloud.ply"))
    assert os.path.exists(os.path.join(prj.chunk.workdir, "dense", "point_cloud.ply"))      # original kept
    assert no_dialogs == []
    # not enough space: warning, nothing saved
    from photomesh import datafolder as dfo
    monkeypatch.setattr(dfo, "free_space", lambda p: 10)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    other = tmp_path / "c" / "x.pmsx"; other.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(other), "")))
    assert window.save_project_as() is False
    assert warned and "Not enough free disk space" in warned[0] and not other.exists()
    assert window.project.path == str(new)


def test_open_offers_relink_for_missing_photos(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from photomesh.project import Project, Chunk, Camera
    p = Project(); p.chunks = [Chunk(cameras=[Camera(path=r"Q:\x\a.jpg")])]
    p.save(str(tmp_path / "m.pmsx"))
    asked, relinked = [], []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.Yes))
    monkeypatch.setattr(window, "change_image_paths", lambda *a: relinked.append(1))
    window.open_project(str(tmp_path / "m.pmsx"))
    assert asked == ["Missing Photos"] and relinked == [1]
