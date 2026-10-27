"""GUI smoke tests: menus, every action, panes, project round-trip."""
import os

import numpy as np
import pytest

from photomesh.menus import MENUS


def _names(items):
    for it in items:
        if isinstance(it, tuple):
            yield from _names(it[1])
        elif it != "-":
            yield it.rstrip("*")


def test_top_level_menus(window):
    titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert titles == ["File", "Edit", "View", "Workflow", "Model", "Photo", "Ortho", "Tools", "Help"]


def test_every_menu_item_has_action(window):
    for title, items in MENUS:
        top = title.strip("&")
        for n in _names(items):
            assert f"{top}/{n}" in window.actions, n


def test_no_branding(window):
    texts = " ".join(a.text() for a in window.actions.values()).lower()
    assert "metashape" not in texts and "agisoft" not in texts


@pytest.mark.parametrize("colmap_present", [False, True])
def test_trigger_every_action(window, no_dialogs, monkeypatch, colmap_present):
    """Trigger every action (twice for checkables) - no exceptions, no error boxes."""
    from photomesh import colmap as cm
    monkeypatch.setattr(cm, "find_colmap", lambda: "/usr/bin/colmap" if colmap_present else None)
    monkeypatch.setattr(window, "close", lambda: True)
    seen = set()
    for key, a in list(window.actions.items()):
        if id(a) in seen:
            continue
        seen.add(id(a))
        a.trigger()
        if a.isCheckable():
            a.trigger()
    assert no_dialogs == [], no_dialogs


def test_actions_with_photos_loaded(window, no_dialogs, image_dir, monkeypatch):
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    assert len(window.chunk.cameras) == 6
    assert window.photos.count() == 6
    assert window.reference.rowCount() == 6
    cam = window.chunk.cameras[0]
    assert cam.lat == pytest.approx(40.18, abs=1e-3) and cam.lon == pytest.approx(44.51, abs=1e-3)
    from conftest import wait_thumbnails
    wait_thumbnails(window)
    assert os.path.exists(cam.thumb) and os.path.exists(cam.desc)
    window.open_photo(2)
    assert window.tabs.currentWidget() is window.photo_view
    window.step_photo(1)
    assert window.current_photo == 3
    from photomesh import colmap as cm
    monkeypatch.setattr(cm, "find_colmap", lambda: None)
    monkeypatch.setattr(window, "close", lambda: True)
    for a in {id(a): a for a in window.actions.values()}.values():
        if a.text() != "New":
            a.trigger()
    assert len(window.chunk.cameras) == 6
    window.actions["New"].trigger()          # discards (dialog patched)
    assert window.chunk.cameras == []
    assert no_dialogs == []



@pytest.mark.parametrize("transition", ["new", "open", "chunk"])
def test_project_switch_clears_photo_and_ortho_views(window, image_dir, tmp_path, transition):
    from photomesh.project import Chunk, Project

    photo = sorted(image_dir.iterdir())[0]
    window.photo_view.open(str(photo), "Previous project")
    window.photo_view.set_markers([("old", 10, 10, "pinned")])
    window.current_photo = 0
    window.tabs.setCurrentWidget(window.photo_view)
    window.ortho_view.set_data(np.array([[1.0, 2.0, 3.0]]), [])
    window.ortho_view.set_markers([("old", 1.0, 2.0)])
    window.ortho_view.center = (1.0, 2.0)

    if transition == "new":
        window.new_project()
    elif transition == "open":
        path = str(tmp_path / "empty.pmsx")
        Project().save(path)
        window.open_project(path)
    else:
        window.project.chunks.append(Chunk())
        window.set_active(1)

    assert window.current_photo == -1
    assert window.tabs.currentWidget() is window.model_view
    assert window.photo_view.path is None
    assert window.photo_view.view.scene().items() == []
    assert window.photo_view.markers == []
    assert window.ortho_view.pts is None
    assert window.ortho_view.cams == []
    assert window.ortho_view.markers == []
    assert window.ortho_view.center is None


def test_duplicate_photos_ignored(window, image_dir):
    paths = sorted(str(p) for p in image_dir.iterdir())
    window.add_photos(paths)
    window.add_photos(paths)
    assert len(window.chunk.cameras) == 6


def test_project_roundtrip(window, image_dir, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from photomesh.plyio import save_ply
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    ply = tmp_path / "cloud.ply"
    save_ply(str(ply), np.random.rand(500, 3).astype(np.float32), np.random.rand(500, 3))
    window.load_result("cloud", str(ply))
    assert window.chunk.point_count == 500
    out = tmp_path / "test.pmsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    assert window.save_project()
    window.open_project(str(out))
    assert len(window.chunk.cameras) == 6 and window.chunk.point_count == 500
    from photomesh import APP_NAME, APP_VERSION
    assert window.windowTitle() == f"{APP_NAME} {APP_VERSION} - test"


def test_workspace_chunk_actions(window, no_dialogs):
    window._workspace_action("Add Chunk", -1)
    assert len(window.project.chunks) == 2 and window.project.active == 1
    window._workspace_action("Duplicate Chunk", 1)
    assert len(window.project.chunks) == 3
    window._workspace_action("Remove Chunk", 2)
    window._workspace_action("Remove Chunk", 1)
    assert len(window.project.chunks) == 1
    window.merge_chunks()  # < 2 chunks: just an info box
    assert no_dialogs == []


def test_import_mesh_and_modes(window, tmp_path):
    from photomesh.plyio import save_ply
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float32)
    f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    p = tmp_path / "m.ply"; save_ply(str(p), v, faces=f)
    window.load_result("model", str(p))
    assert window.chunk.face_count == 4
    for m in ("Model - Shaded", "Model - Solid", "Model - Wireframe", "Model - Textured"):
        window.actions[m].trigger()


def test_broken_ply_reported_not_crash(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    bad = tmp_path / "bad.ply"; bad.write_text("not a ply")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))
    window.actions["Import Point Cloud..."].trigger()
    assert len(no_dialogs) == 1  # error shown in a message box, app still alive


def test_align_requires_colmap(window, no_dialogs, image_dir, monkeypatch, tmp_path):
    from photomesh import colmap as cm
    window.project.path = str(tmp_path / "p.pmsx")
    msgs = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: msgs.append(a[1])))
    monkeypatch.setattr(cm, "find_colmap", lambda: None)
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    window.align_photos()
    assert msgs == ["COLMAP not found"]


def test_console_python(window):
    window.console.inp.setText("1+2"); window.console._run()
    assert "3" in window.console.out.toPlainText()
    window.console.inp.setText("x = len(project.chunks)"); window.console._run()
    window.console.inp.setText("raise ValueError('boom')"); window.console._run()
    assert "ValueError: boom" in window.console.out.toPlainText()


def test_dialogs_construct(qtbot):
    from photomesh import dialogs
    for cls in (dialogs.AlignPhotosDialog, dialogs.BuildPointCloudDialog, dialogs.BuildModelDialog,
                dialogs.BuildTextureDialog, dialogs.PreferencesDialog):
        d = cls(); qtbot.addWidget(d)
        if hasattr(d, "settings"):
            assert isinstance(d.settings(), dict)
    s = dialogs.AlignPhotosDialog().settings()
    assert s["mode"] == "Fast" and s["key_point_limit"] == 40000 and s["accuracy"] == "High"


def test_dense_without_cuda_warns(window, no_dialogs, monkeypatch, tmp_path):
    from photomesh import colmap as cm
    window.project.path = str(tmp_path / "p.pmsx")
    from PySide6.QtWidgets import QMessageBox
    msgs = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: msgs.append(a[1])))
    monkeypatch.setattr(cm, "find_colmap", lambda: "colmap")
    monkeypatch.setattr(cm, "has_cuda", lambda exe: False)
    window.chunk.cameras = [type("C", (), {"aligned": True})()]
    window.build_point_cloud()
    assert msgs == ["Build Point Cloud"]
    window.chunk.cameras = []
