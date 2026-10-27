import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt as _Qt, QCoreApplication as _QCA      # noqa: E402
_QCA.setAttribute(_Qt.AA_ShareOpenGLContexts)     # as main.py: GL views share shader programs
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest
from PIL import Image


class NetworkBlocked(RuntimeError):
    pass


NETWORK_ATTEMPTS = []


@pytest.fixture(scope="session", autouse=True)
def _no_network():
    """PhotoMesh Studio must work fully offline: every test runs with
    outgoing sockets and DNS disabled in-process, so any network use fails."""
    import socket
    def deny(*a, **k):
        NETWORK_ATTEMPTS.append(a[1:2] or a[:1])
        raise NetworkBlocked(f"network access attempted: {a[1:2] or a[:1]}")
    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
             socket.getaddrinfo, socket.gethostbyname)
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    socket.gethostbyname = deny
    yield
    (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
     socket.getaddrinfo, socket.gethostbyname) = saved


@pytest.fixture(scope="session", autouse=True)
def _isolated_reports(tmp_path_factory):
    """Error reports of the tests go to a temp folder, not %LOCALAPPDATA%."""
    old = os.environ.get("PHOTOMESH_REPORT_DIR")
    os.environ["PHOTOMESH_REPORT_DIR"] = str(tmp_path_factory.mktemp("reports"))
    yield
    if old is None:
        os.environ.pop("PHOTOMESH_REPORT_DIR", None)
    else:
        os.environ["PHOTOMESH_REPORT_DIR"] = old


@pytest.fixture(scope="session", autouse=True)
def _isolated_settings(tmp_path_factory):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("PhotoMeshTest")
    QCoreApplication.setApplicationName("PhotoMeshStudioTest")
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path_factory.mktemp("settings")))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    yield


def _make_image(path, seed, gps=None):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
    im = Image.fromarray(base).resize((640, 480), Image.NEAREST)
    exif = Image.Exif()
    if gps:
        lat, lon, alt = gps
        def dms(v):
            v = abs(v); d = int(v); m = int((v - d) * 60); s = (v - d - m / 60) * 3600
            return (float(d), float(m), round(s, 4))
        exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W",
                        4: dms(lon), 5: 0, 6: float(alt)}
    im.save(path, exif=exif)


@pytest.fixture
def image_dir(tmp_path):
    d = tmp_path / "imgs"; d.mkdir()
    for i in range(6):
        _make_image(str(d / f"img_{i:02d}.jpg"), i, gps=(40.18 + i * 1e-4, 44.51 + i * 1e-4, 1000 + i))
    return d


@pytest.fixture
def no_dialogs(monkeypatch):
    """Make every modal dialog return immediately (cancel / empty)."""
    from PySide6.QtWidgets import QFileDialog, QMessageBox, QInputDialog, QDialog
    from PySide6.QtGui import QDesktopServices
    errors = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([], "")))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "about", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[1:]) or QMessageBox.Ok))
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)
    from photomesh import choice                         # named-button boxes: the default button
    monkeypatch.setattr(choice, "ask_choice", lambda *a, **k: k.get("default"))
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda *a, **k: True))
    from photomesh.report_ui import ErrorDialog          # the error dialog replaces QMessageBox.critical
    monkeypatch.setattr(ErrorDialog, "exec", lambda self: (None if self.manual else errors.append(
        (self.report["title"], self.report["message"]))) or 0)
    return errors


@pytest.fixture
def window(qtbot, no_dialogs):
    from photomesh.mainwindow import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    yield w
    w.cancel_thumbnails()
    if w._thumb_job is not None:
        w._thumb_job.wait(10)
    w.project.modified = False


def wait_thumbnails(window, timeout=60):
    """Thumbnails are created by a background job; wait for it and deliver its
    queued signals."""
    import time
    from PySide6.QtWidgets import QApplication
    t0 = time.time()
    while time.time() - t0 < timeout:
        QApplication.processEvents()
        j = window._thumb_job
        if j is None or (j.finished and not window.thumb_progress.isVisible()):
            break
        time.sleep(0.01)
    QApplication.processEvents()


CI_ONLY_LOCAL = {"colmap": "no COLMAP on the CI runner", "gpu": "no GPU / CUDA COLMAP on the CI runner",
                 "dataset": "no local test project on the CI runner", "opengl": "no OpenGL display on the CI runner"}


def pytest_collection_modifyitems(config, items):
    """On CI (GitHub Actions) tests that need COLMAP, a GPU, local data or a
    real OpenGL display are skipped explicitly, so they show up as skipped
    with the reason instead of failing or silently passing."""
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("PM_CI") != "1":
        return
    import pytest
    for item in items:
        for name, why in CI_ONLY_LOCAL.items():
            if item.get_closest_marker(name):
                item.add_marker(pytest.mark.skip(reason=f"CI: {why} ({name})"), append=False); break
