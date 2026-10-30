"""Regression coverage for issue #1, project switches and Ortho marker placement."""
import copy
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from photomesh import colmap_io as cio
from photomesh import georef as g
from test_markers_manual import _scene, _marker
from test_dense_mesh_gcp import synthetic_model


@pytest.mark.parametrize("transition", ["new", "open", "chunk"])
def test_project_switch_drops_marker_overlays_history_and_pending(window, tmp_path, transition):
    from photomesh.project import Chunk, Project
    _scene(window, tmp_path)
    label = window.add_marker_at_photo(500, 400, k=0)
    window.edit_marker_cell(label, 1, "123")
    assert window._mk_undo and window._ref_pending
    assert window.model_view.overlay.markers is not None
    # Queued cell edits must not leak into another project with the same marker label.
    window._marker_edited(label, 2, "999")
    target = Chunk(markers=[g.Marker(label).to_dict()])
    if transition == "new":
        window.new_project()
    elif transition == "open":
        project = Project()
        project.chunks = [target]
        path = str(tmp_path / "next.pmsx")
        project.save(path)
        window.open_project(path)
    else:
        window.project.chunks.append(target)
        window.set_active(1)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    window.wait_view_loaded()
    assert window.model_view.overlay.markers is None
    assert window.ortho_view.markers == []
    assert window.ortho_view.active_marker is None
    assert window._ref_pending == {} and window.markers_pane.pending == {}
    assert not window.undo_markers() and not window.redo_markers()
    assert all(m["ref"] is None for m in window.chunk.markers)


def test_model_clear_drops_marker_overlay_and_backing_data(window):
    window.model_view.set_markers(np.array([[1., 2., 3.]]), labels=["old"])
    window.model_view.clear()
    assert window.model_view.overlay.markers is None
    assert window.model_view._marker_data[0] is None


def test_ortho_never_paints_point_cloud_as_an_orthomosaic(window):
    from PySide6.QtGui import QImage, QPainter
    ov = window.ortho_view
    ov.center, ov.scale = (0., 0.), 5.
    def render():
        image = QImage(ov.size(), QImage.Format_ARGB32)
        painter = QPainter(image)
        try:
            ov._paint(painter)
        finally:
            painter.end()
        return image
    before = render()
    ov.set_data(np.array([[1., 2., 3.], [3., 4., 5.]]), [[5., 6., 7.]])
    assert ov._bounds() is None
    assert before == render()
    ov.center = None
    ov.fit()
    assert ov.center is None


def test_ortho_context_places_selected_control_without_creating_duplicate(window, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QMenu
    ch = _scene(window, tmp_path)
    original = g.Marker("G1", ref=[700., 800., 20.], acc=[.02, .03], enabled=False,
                        projections={"IMG_0001.JPG": [500., 400., True]})
    ch.markers = [original.to_dict()]
    window.refresh_all()
    window._select_marker_row("G1")
    ov = window.ortho_view
    assert ov.active_marker == "G1"
    ov.layers["ortho"] = SimpleNamespace(visible=True, close=lambda: None)
    monkeypatch.setattr(ov, "coordinates_at", lambda *a: (110., 100., 0.))
    actions = []
    def menu_exec(menu, *a):
        actions[:] = [a.text() for a in menu.actions()]
        next(a for a in menu.actions() if a.text() == "Place Marker: G1").trigger()
    monkeypatch.setattr(QMenu, "exec", menu_exec)
    try:
        ov.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(20, 20)))
    finally:
        ov.layers.clear()
    assert "Add Marker" in actions
    assert len(ch.markers) == 1
    marker = _marker(window, "G1")
    assert marker["ref"] == original.ref and marker["acc"] == original.acc
    assert marker["enabled"] is False
    assert marker["projections"]["img_0001"] == [500., 400., True]  # manual pin preserved
    assert marker["projections"]["img_0002"][2] is False
    assert window.undo_markers()
    assert _marker(window, "G1")["projections"] == original.projections
    # Re-selecting multiple rows or clearing selection must not target a stale marker.
    window.markers_pane.clearSelection()
    assert ov.active_marker is None
    before = copy.deepcopy(ch.markers)
    assert window.add_marker_at_ortho(110, 100, 0, label="missing") is None
    assert ch.markers == before


def test_imported_xyz_alone_is_not_a_usable_control(window, tmp_path):
    ch = _scene(window, tmp_path)
    markers = [g.Marker(f"G{i}", [i, i * i, 10.]) for i in range(94)]
    status = g.reference_status(window._sparse_model(), markers, g.CRS.parse(ch.crs))
    assert status == {"imported": 94, "references": 94, "usable": 0, "gps": 0}
    # Two differently spelled keys for one photo still count as only one observation.
    X, n = g.triangulate_marker(window._sparse_model(), {"IMG_0001.JPG": [500, 400],
                                                      "img_0001": [500, 400]})
    assert X is None and n == 1


@pytest.mark.parametrize("accept", [False, True])
def test_optimize_preflight_offers_local_mode_explicitly(window, tmp_path, monkeypatch, accept):
    from PySide6.QtWidgets import QMessageBox
    from photomesh import dialogs
    ch = _scene(window, tmp_path)
    ch.markers = [g.Marker(f"G{i}", [i, i * i, 10.]).to_dict() for i in range(94)]
    monkeypatch.setattr(dialogs.OptimizeCamerasDialog, "exec", lambda self: 1)
    prompts, ran = [], []
    def question(*a, **kw):
        prompts.append(a[2])
        return QMessageBox.Yes if accept else QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(window, "_run", lambda *a, **k: ran.append((a, k)))
    window.optimize_cameras()
    assert "94 markers imported" in prompts[0]
    assert "0 usable controls" in prompts[0]
    assert bool(ran) is accept
    assert not ch.georef
    if accept:
        path = os.path.join(ch.workdir, "georef_task.json")
        cfg = json.load(open(path))
        assert cfg["local_only"] is True
        assert "local bundle adjustment" in ran[0][0][1][0][0]


def test_local_optimization_reduces_error_and_retains_frame(tmp_path):
    model = synthetic_model(n_img=5, n_pts=250)
    rng = np.random.default_rng(94)
    for im in model.images.values():
        im.tvec = -im.R() @ (im.center() + rng.normal(0, .02, 3))
    g.retriangulate(model)
    ids = sorted(model.images)
    first = model.images[ids[0]]
    C0, R0 = first.center().copy(), first.R().copy()
    other = max(ids[1:], key=lambda i: np.linalg.norm(model.images[i].center() - C0))
    baseline = np.linalg.norm(model.images[other].center() - C0)
    err0 = np.mean(cio.observation_errors(model)[0] ** 2)
    path = str(tmp_path / "sparse")
    cio.write_model(path, model)
    cloud = tmp_path / "unchanged.ply"
    cloud.write_bytes(b"dense data must not be transformed in local mode")
    out = g.run_task({"model": path, "local_only": True, "fit_intrinsics": False,
                      "max_iterations": 8, "transform_files": [str(cloud)],
                      "markers": [g.Marker(f"G{i}", [i, i * i, 10.]).to_dict() for i in range(94)]},
                     log=lambda *_: None)
    result = cio.read_model(path)
    assert out["local_only"] and out["similarity"] is None
    assert np.allclose(result.images[ids[0]].center(), C0, atol=1e-8)
    assert np.allclose(result.images[ids[0]].R(), R0, atol=1e-8)
    assert np.linalg.norm(result.images[other].center() - C0) == pytest.approx(baseline, abs=1e-8)
    assert np.mean(cio.observation_errors(result)[0] ** 2) < err0 * .5
    assert cloud.read_bytes() == b"dense data must not be transformed in local mode"
    assert all(m["est"] is None for m in out["markers"].values())
    assert not os.path.exists(path + "_optimize_tmp")


def test_camera_exif_gps_can_be_used_in_utm(window, tmp_path):
    _scene(window, tmp_path)
    gps = {"IMG_0001.JPG": [69.763, 38.678, 1580.7]}
    got = g.camera_gps_metric(window._sparse_model(), gps, g.CRS.parse("EPSG:32642"))
    E, N = g.utm_forward(38.678, 69.763, 42)
    assert np.allclose(got[1], [E, N, 1580.7])
    assert g.camera_gps_metric(window._sparse_model(), gps, g.CRS.parse("")) == {}
