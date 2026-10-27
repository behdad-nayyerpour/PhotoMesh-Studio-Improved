"""Manual ground control: Add Marker on a photo / on the 3D surface, prediction
into the other photos, editable Markers table with validation + undo, Rename /
Remove, Filter Photos by Markers, Ctrl+click pinning, Refine/Optimize inputs."""
import json
import os

import numpy as np
import pytest

from photomesh import colmap_io as cio
from photomesh import georef as g

R_DOWN = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])      # looking down, image y = -Y


def _ground_ply(path, z=0.0, lo=-100.0, hi=300.0):
    from plyfile import PlyData, PlyElement
    v = np.zeros(4, dtype=[("x", "f4"), ("y", "f4"), ("z", "f4")])
    v["x"] = [lo, hi, hi, lo]; v["y"] = [lo, lo, hi, hi]; v["z"] = z
    f = np.empty(2, dtype=[("vertex_indices", "i4", (3,))]); f["vertex_indices"] = [[0, 1, 2], [0, 2, 3]]
    PlyData([PlyElement.describe(v, "vertex"), PlyElement.describe(f, "face")]).write(path)
    return path


def _scene(window, tmp_path, centres=((100, 100, 60), (120, 100, 60), (140, 100, 60))):
    """Three nadir cameras 60 m above a flat ground (z = 0), 20 m apart in X."""
    window.project.path = str(tmp_path / "proj.pmsx")      # processing needs a saved project
    from PIL import Image
    from photomesh.project import Camera
    ch = window.chunk
    wd = tmp_path / "work"; (wd / "sparse" / "0").mkdir(parents=True)
    ch.workdir = str(wd)
    cam = cio.Camera(1, 1000, 800, np.array([1000.0, 1000.0, 500.0, 400.0]))
    imgs, cams = {}, []
    for k, C in enumerate(centres, 1):
        name = f"IMG_{k:04d}.JPG"
        photo = str(tmp_path / name)
        Image.new("RGB", (1000, 800), (90, 110, 90)).save(photo)
        C = np.asarray(C, float)
        imgs[k] = cio.Image(name, 1, cio.rot_to_qvec(R_DOWN), -R_DOWN @ C, np.zeros((0, 2)), np.zeros(0, np.int64))
        cams.append(Camera(path=photo, label=name, aligned=True, est=C.tolist(), rot=R_DOWN.tolist(),
                           width=1000, height=800))
    pts = cio.Points(np.array([1, 2], np.uint64), np.array([[100.0, 100, 0], [130.0, 95, 0]]),
                     np.zeros((2, 3), np.uint8), np.zeros(2), np.array([0, 0, 0]), np.zeros(0, np.uint32),
                     np.zeros(0, np.uint32))
    cio.write_model(str(wd / "sparse" / "0"), cio.Model({1: cam}, imgs, pts))
    ch.cameras = cams
    ch.tie_point_count = 2
    window.refresh_all(); window.reload_view(); window.wait_view_loaded()
    window._headless = True
    return ch


def _marker(window, label):
    return next(m for m in window.chunk.markers if m["label"] == label)


def test_add_marker_on_photo_pins_and_predicts_other_photos(window, tmp_path):
    ch = _scene(window, tmp_path)
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"))
    window.markers_pane.clearSelection()
    assert window.markers_pane.current_label() is None          # no active marker needed
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)
    assert lab == "point 1"
    m = _marker(window, lab)
    p = m["projections"]
    assert p["img_0001"] == [500.0, 400.0, True]                 # pinned where clicked
    assert p["img_0002"][2] is False                             # predicted, stored unpinned
    assert p["img_0002"][0] == pytest.approx(1000 * -20 / 60 + 500, abs=1e-3)
    assert p["img_0002"][1] == pytest.approx(400.0, abs=1e-3)
    assert "img_0003" not in p                                   # -40 m at 60 m: outside the frame
    assert window.project.modified
    X, n = g.triangulate_marker(window._sparse_model(), p)       # what Optimize Cameras will use
    assert n == 2 and np.allclose(X, [100, 100, 0], atol=1e-3)
    labels = window.model_view.overlay.markers[1]
    assert lab in labels                                          # shown in the 3D view


def test_add_marker_falls_back_to_tie_points_and_reports_no_surface(window, tmp_path):
    _scene(window, tmp_path)                                      # no mesh, no DEM
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)            # ray hits tie point (100, 100, 0)
    assert "img_0002" in _marker(window, lab)["projections"]
    assert window.add_marker_at_ray(np.array([500.0, 500, 50]), np.array([0.0, 0, -1])) is None
    assert len(window.chunk.markers) == 1


def test_add_marker_from_model_view_ray(window, tmp_path):
    ch = _scene(window, tmp_path)
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"))
    lab = window.add_marker_at_ray(np.array([110.0, 100, 50]), np.array([0.0, 0, -1]))
    p = _marker(window, lab)["projections"]
    assert set(p) == {"img_0001", "img_0002"}                     # cam 3 sees it at u = 0: outside
    assert all(v[2] is False for v in p.values())                 # all unpinned / predicted
    assert p["img_0001"][0] == pytest.approx(500 + 1000 * 10 / 60, abs=1e-3)
    assert p["img_0002"][0] == pytest.approx(500 - 1000 * 10 / 60, abs=1e-3)
    # model-view picking: the pyqtgraph MVP ray goes through the projected pixel
    from photomesh.overlay3d import pixel_ray, project
    P = np.array([[1.2, 0, 0, 0], [0, 1.6, 0, 0], [0, 0, -1.0002, -0.20002], [0, 0, -1, 0]])
    V = np.eye(4); V[:3, 3] = [-110, -100, -150]
    mvp = P @ V
    xy, _z, ok = project(mvp, [[113.0, 97.0, 2.0]], 640, 480)
    o, d = pixel_ray(mvp, xy[0, 0], xy[0, 1], 640, 480)
    w = np.array([113.0, 97.0, 2.0]) - o
    assert ok[0] and np.linalg.norm(w - (w @ d) * d) < 1e-6


def test_prediction_skips_photos_where_the_surface_hides_the_marker(window, tmp_path):
    pytest.importorskip("open3d")
    from plyfile import PlyData, PlyElement
    ch = _scene(window, tmp_path)
    # ground + a 10 m high wall at x = 126 between the marker (x = 128) and camera 1 (x = 100)
    v = np.zeros(8, dtype=[("x", "f4"), ("y", "f4"), ("z", "f4")])
    v["x"] = [-100, 300, 300, -100, 126, 126, 126, 126]
    v["y"] = [-100, -100, 300, 300, 0, 200, 200, 0]
    v["z"] = [0, 0, 0, 0, 0, 0, 10, 10]
    f = np.empty(4, dtype=[("vertex_indices", "i4", (3,))])
    f["vertex_indices"] = [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]]
    ch.model_ply = str(tmp_path / "wall.ply")
    PlyData([PlyElement.describe(v, "vertex"), PlyElement.describe(f, "face")]).write(ch.model_ply)
    assert window._raycast_scene() is not None
    pred = window.predicted_projections(np.array([128.0, 100.0, 0.0]))
    assert "img_0002" in pred and "img_0003" in pred
    assert "img_0001" not in pred                                 # behind the wall from camera 1


def test_markers_table_edits_validate_and_undo(window, tmp_path):
    _scene(window, tmp_path)
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)
    assert window.edit_marker_cell(lab, 1, "abc") is False        # rejected, nothing stored
    assert _marker(window, lab)["ref"] is None
    assert window.edit_marker_cell(lab, 1, "12,5")                # decimal comma accepted
    assert _marker(window, lab)["ref"] is None                     # incomplete: pending only
    assert window._ref_pending[lab] == [12.5, None, None]
    assert window.markers_pane.item(0, 1).text() == "12.5000"
    window.edit_marker_cell(lab, 2, "20"); window.edit_marker_cell(lab, 3, "-3.25")
    assert _marker(window, lab)["ref"] == [12.5, 20.0, -3.25] and lab not in window._ref_pending
    assert window.edit_marker_cell(lab, 4, "0.02/0.05") and _marker(window, lab)["acc"] == [0.02, 0.05]
    assert window.edit_marker_cell(lab, 4, "-1") is False and _marker(window, lab)["acc"] == [0.02, 0.05]
    assert window.edit_marker_cell(lab, 4, "") and _marker(window, lab)["acc"] is None
    window.add_marker()                                           # Tools > Markers > Add Marker -> point 2
    assert window.edit_marker_cell(lab, 0, "point 2") is False    # duplicate label
    assert window.edit_marker_cell(lab, 0, "  ") is False         # empty label
    assert window.edit_marker_cell(lab, 0, "GCP 7")
    assert [m["label"] for m in window.chunk.markers] == ["GCP 7", "point 2"]
    assert window.undo_markers() and [m["label"] for m in window.chunk.markers] == [lab, "point 2"]
    assert window.undo_markers() and len(window.chunk.markers) == 1        # Add Marker undone
    assert window.redo_markers() and len(window.chunk.markers) == 2
    window.actions["Edit/Undo"].trigger()                          # Edit > Undo is wired
    assert len(window.chunk.markers) == 1


def test_table_double_click_edit_goes_through_signal(window, tmp_path, qtbot):
    _scene(window, tmp_path)
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)
    pane = window.markers_pane
    from photomesh.panes import MarkersPane
    from PySide6.QtWidgets import QAbstractItemView
    assert pane.editTriggers() & QAbstractItemView.DoubleClicked
    from PySide6.QtCore import Qt
    assert all(pane.item(0, c).flags() & Qt.ItemIsEditable for c in MarkersPane.EDITABLE)
    assert not pane.item(0, 5).flags() & Qt.ItemIsEditable
    pane.item(0, 3).setText("101.5")                               # what the cell editor commits
    qtbot.waitUntil(lambda: window._ref_pending.get(lab) == [None, None, 101.5], timeout=2000)
    pane.item(0, 0).setText("renamed")
    qtbot.waitUntil(lambda: window.chunk.markers[0]["label"] == "renamed", timeout=2000)
    assert window._ref_pending.get("renamed") == [None, None, 101.5]


def test_rename_remove_filter_and_workspace_menu(window, tmp_path):
    ch = _scene(window, tmp_path)
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"))
    a = window.add_marker_at_photo(500.0, 400.0, k=0)              # on photos 1, 2
    b = window.add_marker_at_photo(500.0, 400.0, k=2)              # on photos 3, 2
    assert window.rename_marker(a, "G1") and window.rename_marker(b, "G2")
    shown = window.filter_photos_by_markers(["G1"])
    assert shown == {0, 1} and window.photos.visible_indices() == [0, 1]
    window._workspace_action("marker:Filter Photos by Markers:G2", window.project.active)
    assert window.photos.visible_indices() == [1, 2]
    window._workspace_action("marker:Reset Filter:", window.project.active)
    assert window.photos.visible_indices() == [0, 1, 2]
    window._workspace_action("marker:Remove Marker:G1", window.project.active)
    assert [m["label"] for m in window.chunk.markers] == ["G2"]
    assert window.undo_markers() and [m["label"] for m in window.chunk.markers] == ["G1", "G2"]
    assert window.remove_markers(["G1", "G2"], confirm=False) == 2 and window.chunk.markers == []
    rows = []
    window.workspace.refresh(window.project)
    assert not any(it.text(0).startswith("Markers (") for it in window.workspace._iter())


def test_photo_flags_kinds_ctrl_click_and_context(window, tmp_path):
    from PySide6.QtCore import Qt
    _scene(window, tmp_path)
    window.open_photo(1)
    pv = window.photo_view
    got = []
    pv.addMarker.connect(lambda u, v: got.append(("add", u, v)))
    pv.pinMarker.connect(lambda l, u, v: got.append(("pin", l, u, v)))
    pv.active_marker = None; pv.ctrl_click(10, 20)
    pv.active_marker = "G9"; pv.ctrl_click(30, 40)
    assert got == [("add", 10.0, 20.0), ("pin", "G9", 30.0, 40.0)]
    pv.set_markers([("A", 100, 100, "pinned"), ("B", 200, 100, "placed"), ("C", 300, 100, "predicted")])
    pens = {}
    for it in pv._overlay:
        if hasattr(it, "pen") and it.zValue() == 7:
            pens.setdefault(it.pen().color().name(), set()).add(it.pen().style())
    assert pens["#28dc3c"] == {Qt.SolidLine}                      # pinned: green
    assert pens["#3c8cff"] == {Qt.SolidLine}                      # placed, not pinned: blue
    assert pens["#bebebe"] == {Qt.DashLine}                       # predicted: grey, dashed
    assert [m[0] for m in pv.markers_near(105, 100)] == ["A"]
    # pinning a blue flag through the window keeps one projection per photo and pins it
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)
    assert _marker(window, lab)["projections"]["img_0002"][2] is False
    window.pin_marker(lab, 170.0, 401.0, k=1)
    assert _marker(window, lab)["projections"]["img_0002"] == [170.0, 401.0, True]


def test_refine_and_optimize_inputs_include_new_markers(window, tmp_path, monkeypatch):
    ch = _scene(window, tmp_path)
    lab = window.add_marker_at_photo(500.0, 400.0, k=0)
    window.edit_marker_cell(lab, 1, "100"); window.edit_marker_cell(lab, 2, "100"); window.edit_marker_cell(lab, 3, "0")
    ran = []
    monkeypatch.setattr(window, "_run", lambda name, steps, *a, **k: ran.append((name, steps)))
    window.refine_markers()
    assert ran and ran[0][0] == "Refine Markers"
    cfg = json.load(open(os.path.join(window.project.default_workdir(ch), "refine_task.json")))
    assert any(m["label"] == lab and len(m["projections"]) == 2 for m in cfg["markers"])
    assert np.allclose(cfg["positions"][lab], [100, 100, 0], atol=1e-3)
    # the refine task uses the GUI position (CRS-aware) before the raw reference
    from photomesh import tasks
    called = {}
    def fake_predict(model, iid, X):
        called["X"] = np.asarray(X, float)
        return None
    monkeypatch.setattr(g, "predict_projection", fake_predict)
    monkeypatch.setattr("photomesh.marker_refine.crop_views", lambda *a, **k: {})
    cfg["output"] = str(tmp_path / "out.json")
    tasks.cmd_refine_markers(cfg)
    assert np.allclose(called["X"], [100, 100, 0])
    # Optimize Cameras sees it as a control point with two projections
    m = g.Marker(**_marker(window, lab))
    assert m.enabled and m.ref == [100.0, 100.0, 0.0] and len(m.projections) == 2


def test_parse_helpers():
    from photomesh.markers_ui import parse_coordinate, parse_accuracy, ray_mesh
    assert parse_coordinate(" 1 234.5 ") == 1234.5 and parse_coordinate("") is None
    with pytest.raises(ValueError):
        parse_coordinate("nan")
    assert parse_accuracy("0.01") == [0.01, 0.01] and parse_accuracy("0.01/0.02") == [0.01, 0.02]
    with pytest.raises(ValueError):
        parse_accuracy("0/1")
    V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    assert ray_mesh(V, np.array([[0, 1, 2]]), [0.2, 0.2, 5], [0, 0, -1]) == pytest.approx(5.0)
    assert ray_mesh(V, np.array([[0, 1, 2]]), [2, 2, 5], [0, 0, -1]) is None
