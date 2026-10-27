"""End-to-end alignment through the GUI with a real COLMAP (skipped otherwise).
Set PM_TESTDATA to a folder with overlapping photos (e.g. OpenMVG SceauxCastle)."""
import glob
import os
import shutil

import pytest

DATA = os.environ.get("PM_TESTDATA", "")
from photomesh.colmap import find_colmap

pytestmark = [pytest.mark.colmap, pytest.mark.skipif(not (find_colmap() and DATA and os.path.isdir(DATA)),
                                reason="needs COLMAP on PATH and PM_TESTDATA images")]


@pytest.mark.parametrize("mode", ["Fast", "Exhaustive"])
def test_align_gui(window, no_dialogs, monkeypatch, tmp_path, mode):
    from PySide6.QtCore import QSettings
    from photomesh import dialogs
    QSettings().setValue("gpu/use_gpu", False)
    orig = dialogs.AlignPhotosDialog.settings
    monkeypatch.setattr(dialogs.AlignPhotosDialog, "exec", lambda self: 1)
    monkeypatch.setattr(dialogs.AlignPhotosDialog, "settings",
                        lambda self: dict(orig(self), accuracy="Low", mode=mode))
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)  # non-blocking
    window.project.path = str(tmp_path / "p.pmsx")
    window.add_photos(sorted(glob.glob(os.path.join(DATA, "*.[jJ][pP][gG]"))))
    window.align_photos()
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    window.runner.finished.connect(lambda *a: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(600_000, loop.quit)
    if getattr(window.runner, "busy", False):
        loop.exec()
    ch = window.chunk
    assert ch.aligned_count() >= len(ch.cameras) * 0.8
    assert ch.tie_point_count > 1000
    assert no_dialogs == []


def _wait(window, timeout_ms=1_800_000):
    from PySide6.QtCore import QEventLoop, QTimer
    if not getattr(window.runner, "busy", False):
        return
    loop = QEventLoop()
    window.runner.finished.connect(lambda *a: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


@pytest.mark.gpu
@pytest.mark.skipif(os.environ.get("PM_TEST_DENSE") != "1", reason="set PM_TEST_DENSE=1 (needs CUDA COLMAP)")
def test_dense_and_mesh_gui(window, no_dialogs, monkeypatch, tmp_path):
    """Align (GPU) -> Build Point Cloud (CUDA) -> Build Model from point cloud and from tie points."""
    from PySide6.QtCore import QSettings
    from photomesh import dialogs, colmap as cm
    from PySide6.QtWidgets import QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1:])))
    assert cm.has_cuda(find_colmap()), "COLMAP build without CUDA"
    QSettings().setValue("gpu/use_gpu", True)
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    for cls in (dialogs.AlignPhotosDialog, dialogs.BuildPointCloudDialog, dialogs.BuildModelDialog):
        monkeypatch.setattr(cls, "exec", lambda self: 1)
    a0, p0, m0 = (dialogs.AlignPhotosDialog.settings, dialogs.BuildPointCloudDialog.settings,
                  dialogs.BuildModelDialog.settings)
    monkeypatch.setattr(dialogs.AlignPhotosDialog, "settings", lambda self: dict(a0(self), accuracy="Medium"))
    monkeypatch.setattr(dialogs.BuildPointCloudDialog, "settings", lambda self: dict(p0(self), quality="Low"))
    src = {"v": "Point cloud"}
    monkeypatch.setattr(dialogs.BuildModelDialog, "settings",
                        lambda self: dict(m0(self), source=src["v"], face_count="Low"))
    window.project.path = str(tmp_path / "dense.pmsx")
    window.add_photos(sorted(glob.glob(os.path.join(DATA, "*.[jJ][pP][gG]"))))
    window.align_photos(); _wait(window)
    assert window.chunk.aligned_count() >= 9
    window.build_point_cloud(); _wait(window)
    assert window.chunk.point_count > 10_000, "dense point cloud missing"
    window.build_model(); _wait(window)
    # face_count "Low" = points / 45 (Metashape semantics) -> ~1k faces on this small set
    assert window.chunk.face_count > 500, "poisson mesh missing"
    faces_poisson = window.chunk.face_count
    src["v"] = "Depth maps"
    window.build_model(); _wait(window)
    assert window.chunk.face_count > 500, "delaunay mesh missing"
    # both methods write <workdir>/model.ply; Delaunay leaves its raw mesh in dense/
    assert window.chunk.model_ply.endswith("model.ply")
    assert os.path.exists(os.path.join(os.path.dirname(window.chunk.model_ply), "dense", "delaunay_raw.ply"))
    print(f"\nDENSE: points={window.chunk.point_count} poisson_faces={faces_poisson} "
          f"delaunay_faces={window.chunk.face_count}")
    assert no_dialogs == [] and warnings == [], (no_dialogs, warnings)
