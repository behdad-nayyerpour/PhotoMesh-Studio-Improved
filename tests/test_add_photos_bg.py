"""Add Photos must never block the GUI thread (other-laptop freeze with 641 x 22 MB)."""
import os
import subprocess
import sys
import time

import pytest

from conftest import _make_image, wait_thumbnails


def test_read_header_no_decode(image_dir, monkeypatch):
    from PIL import Image
    from photomesh.fastpipe import read_header
    called = []
    monkeypatch.setattr(Image.Image, "load", lambda self, *a: called.append(1))
    p = str(sorted(image_dir.iterdir())[0])
    h = read_header(p)
    assert h["width"] == 640 and h["height"] == 480
    assert h["lat"] == pytest.approx(40.18, abs=1e-3)
    assert called == []


def test_map_images_errors_and_cancel(tmp_path, image_dir):
    from photomesh.fastpipe import map_images, read_header
    bad = tmp_path / "bad.jpg"; bad.write_bytes(b"not a jpeg")
    paths = [str(p) for p in sorted(image_dir.iterdir())] + [str(bad), str(tmp_path / "missing.jpg")]
    r = map_images(read_header, paths, workers=4)
    assert len(r) == 8 and "error" in r[str(bad)] and "error" in r[str(tmp_path / "missing.jpg")]
    n = []
    r = map_images(lambda p: (time.sleep(0.05), read_header(p))[1], paths * 10, workers=2,
                   progress=lambda i, N, p, res: n.append(i), cancel=lambda: len(n) >= 3)
    assert len(n) < 20


def test_pool_map_falls_back_to_threads(monkeypatch):
    import concurrent.futures as cf
    from photomesh import fastpipe

    class Broken:
        def __init__(self, *a, **k): raise OSError("cannot spawn")
    monkeypatch.setattr(fastpipe, "ProcessPoolExecutor", Broken)
    assert sorted(fastpipe.pool_map(abs, [-1, -2, -3], workers=2)) == [1, 2, 3]


def test_add_photos_never_decodes_on_gui_thread(window, image_dir, monkeypatch):
    import threading
    from PySide6.QtGui import QImageReader, QPixmap
    from PIL import Image
    gui = threading.current_thread()
    bad = []
    orig_read = QImageReader.read
    monkeypatch.setattr(QImageReader, "read", lambda self, *a: bad.append(self.fileName()) or orig_read(self, *a))
    orig_load = Image.Image.load
    def load(self, *a):
        if threading.current_thread() is gui:
            bad.append("PIL decode on GUI thread")
        return orig_load(self, *a)
    monkeypatch.setattr(Image.Image, "load", load)
    paths = sorted(str(p) for p in image_dir.iterdir())
    window.add_photos(paths)
    assert len(window.chunk.cameras) == 6 and window.photos.count() == 6
    assert [b for b in bad if not b.endswith(".png") and "thumb" not in b] == []
    wait_thumbnails(window)
    assert all(os.path.exists(c.thumb) for c in window.chunk.cameras)
    # icons updated from the small thumbnails
    assert not window.photos.item(0).icon().isNull()


def test_add_photos_many_stays_responsive(window, tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    d = tmp_path / "many"; d.mkdir()
    paths = []
    for i in range(120):
        p = str(d / f"DJI_{i:04d}.JPG"); _make_image(p, i % 6, gps=(40.18 + i * 1e-5, 44.51, 1000)); paths.append(p)
    ticks = []
    t = QTimer(); t.setInterval(20); t.timeout.connect(lambda: ticks.append(time.perf_counter())); t.start()
    t0 = time.perf_counter()
    window.add_photos(paths)
    dt = time.perf_counter() - t0
    wait_thumbnails(window)
    t.stop()
    assert len(window.chunk.cameras) == 120
    assert window.reference.rowCount() == 120
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 1.0, max(gaps)
    assert dt < 10


def test_add_photos_cancel_adds_nothing(window, image_dir, monkeypatch):
    from photomesh import mainwindow
    monkeypatch.setattr(mainwindow, "run_modal", lambda *a, **k: ({}, True, None))
    assert window.add_photos(sorted(str(p) for p in image_dir.iterdir())) == []
    assert window.chunk.cameras == []


def test_add_photos_persian_path(window, tmp_path):
    d = tmp_path / "عکس‌های پهپاد" / "پرواز ۱"; d.mkdir(parents=True)
    paths = []
    for i in range(3):
        p = str(d / f"تصویر_{i}.jpg"); _make_image(p, i, gps=(40.18, 44.51, 1000)); paths.append(p)
    window.add_photos(paths)
    wait_thumbnails(window)
    cams = window.chunk.cameras
    assert len(cams) == 3 and all(c.width == 640 and c.lat for c in cams)
    assert all(os.path.exists(c.thumb) for c in cams)


def test_add_folder_scandir(window, image_dir, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    (image_dir / "notes.txt").write_text("x")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(image_dir)))
    window.add_folder()
    assert len(window.chunk.cameras) == 6


def test_open_log_folder_action_and_qt_dialog_setting(window, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication
    from photomesh import mainwindow
    monkeypatch.setenv("PHOTOMESH_LOG_DIR", str(tmp_path / "logs"))
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toLocalFile()) or True))
    acts = [a for a in window.findChildren(type(window.menuBar().actions()[0])) if a.text() == "Open Log Folder"]
    assert acts
    acts[0].trigger()
    assert opened and os.path.isdir(opened[0])
    QSettings().setValue("ui/qt_file_dialogs", True)
    assert mainwindow.apply_dialog_setting() and QApplication.testAttribute(Qt.AA_DontUseNativeDialogs)
    QSettings().setValue("ui/qt_file_dialogs", False)
    mainwindow.apply_dialog_setting()
    assert not QApplication.testAttribute(Qt.AA_DontUseNativeDialogs)


HANG_SCRIPT = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from photomesh import diag
p = diag.setup()
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication([])
def block_gui_thread():
    time.sleep(3.5)      # simulated GUI-thread hang
    app.quit()
diag.start_watchdog(app, hang_s=1)
QTimer.singleShot(200, block_gui_thread)
app.exec()
diag.stop_watchdog()
print(p)
'''


def test_diag_log_and_hang_watchdog(tmp_path):
    env = dict(os.environ, PHOTOMESH_LOG_DIR=str(tmp_path / "logs"), QT_QPA_PLATFORM="offscreen")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", HANG_SCRIPT, root], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    logs = list((tmp_path / "logs").glob("photomesh_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8", errors="replace")
    assert "session start" in text and "pillow" in text and "hang watchdog armed" in text
    assert "block_gui_thread" in text          # stack of the blocked GUI thread was dumped
