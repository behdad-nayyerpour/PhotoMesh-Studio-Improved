"""Layer visibility toggles, Metashape-like workspace tree, processing metadata."""
import json
import os

import numpy as np
import pytest

from photomesh.views import LAYERS


def _populate(window, tmp_path, n_cam=8):
    """Synthetic aligned + georeferenced chunk with tie points, cloud and mesh."""
    from photomesh.plyio import save_ply
    from photomesh.project import Camera
    rng = np.random.default_rng(3)
    ch = window.chunk
    ch.workdir = str(tmp_path / "wd"); os.makedirs(ch.workdir, exist_ok=True)
    ch.cameras = [Camera(path=f"/p/IMG_{k:03d}.JPG", aligned=k != 0, est=list(rng.normal(size=3)),
                         rot=np.eye(3).tolist(), width=4000, height=3000) for k in range(n_cam)]
    ch.markers = [{"label": f"GCP{i}", "ref": [0, 0, 0], "acc": None, "enabled": i != 2,
                   "projections": {}, "est": [float(i), 0.0, 0.0]} for i in range(3)]
    tie = tmp_path / "tie.ply"; cloud = tmp_path / "cloud.ply"; mesh = tmp_path / "mesh.ply"
    save_ply(str(tie), rng.random((500, 3)).astype(np.float32), rng.random((500, 3)))
    save_ply(str(cloud), rng.random((5000, 3)).astype(np.float32), rng.random((5000, 3)))
    v = rng.random((50, 3)).astype(np.float32); f = rng.integers(0, 50, (80, 3)).astype(np.int32)
    save_ply(str(mesh), v, rng.random((50, 3)), faces=f)
    ch.tie_points_ply, ch.tie_point_count = str(tie), 500
    ch.point_cloud_ply, ch.point_count = str(cloud), 5000
    ch.model_ply, ch.face_count = str(mesh), 80
    ch.georef = {"crs": "EPSG:32639", "control_rmse": 0.25}
    window.refresh_all()
    return ch


def test_menu_toolbar_share_actions_and_icons_are_16px(window):
    from photomesh.mainwindow import LAYER_ACTIONS
    from PySide6.QtWidgets import QToolBar
    main = [b for b in window.findChildren(QToolBar) if b.objectName() == "toolbar"]
    from photomesh.icons import TOOLBAR_ICON, PANE_ICON
    assert main and main[0].iconSize().width() == TOOLBAR_ICON == main[0].iconSize().height() == 21
    acts = main[0].actions()
    for key in ("tie", "cloud", "mesh", "cameras", "markers", "grid", "region"):
        a = window.layer_action(key)
        assert a is window.actions[f"Model/{LAYER_ACTIONS[key]}"]
        assert a.isCheckable() and a.toolTip()
        assert a in acts, key                              # same QAction on toolbar and menu
        sizes = a.icon().availableSizes()
        assert any(s.width() == 16 for s in sizes)
    ws_bar = [b for b in window.findChildren(QToolBar) if b.objectName() != "toolbar"]
    assert ws_bar and all(b.iconSize().width() == PANE_ICON == 21 for b in ws_bar)
    assert window.workspace.iconSize().width() == 16
    for key, _t, _d in LAYERS:
        assert window.layer_action(key) is not None
    from photomesh.icons import layer_icon
    pm = layer_icon("mesh").pixmap(16, 16)
    assert pm.width() <= 32 and not pm.isNull()


def test_menu_toggle_updates_view_settings_and_workspace(window, tmp_path):
    from PySide6.QtCore import QSettings, Qt
    _populate(window, tmp_path)
    a = window.layer_action("cloud")
    a.setChecked(False)
    assert window.model_view.show["cloud"] is False
    assert QSettings().value("view/show_cloud") in (False, "false")
    it = window.workspace._layer_items["cloud"]
    assert it.checkState(0) == Qt.Unchecked
    # workspace checkbox -> action + view
    it.setCheckState(0, Qt.Checked)
    assert a.isChecked() and window.model_view.show["cloud"] is True
    window.workspace._layer_items["mesh"].setCheckState(0, Qt.Unchecked)
    assert not window.layer_action("mesh").isChecked()
    window.layer_action("mesh").setChecked(True)


def test_mode_actions_solo_layers(window, tmp_path):
    _populate(window, tmp_path)
    window.wait_view_loaded(30)
    window.actions["Model - Shaded"].trigger()
    st = window.layer_states()
    assert st["mesh"] and not st["cloud"] and not st["tie"]
    window.actions["Point Cloud - Colors"].trigger()
    st = window.layer_states()
    assert st["cloud"] and not st["mesh"] and not st["tie"]
    window.actions["Tie Points - Colors"].trigger()
    st = window.layer_states()
    assert st["tie"] and not st["mesh"] and not st["cloud"]
    assert window.layer_action("tie").isChecked() and not window.layer_action("cloud").isChecked()
    window.set_all_layers(True)
    assert all(window.layer_states()[k] for k in ("tie", "cloud", "mesh", "cameras", "markers"))


def test_show_layer_never_reads_ply_on_gui_thread(window, tmp_path, monkeypatch):
    _populate(window, tmp_path)
    window.wait_view_loaded(30)
    import photomesh.mainwindow as mw
    import photomesh.plyio as plyio

    def boom(*a, **k):
        raise AssertionError("load_ply called on the GUI thread")
    monkeypatch.setattr(mw, "load_ply", boom, raising=False)
    monkeypatch.setattr(plyio, "load_ply", boom)
    window.show_layer("cloud", solid=True)
    window.show_layer("tie")
    window.show_layer("cloud")
    window.show_model_mode("wireframe")
    assert window.layer_states()["mesh"]


def test_layer_state_persists_in_qsettings(qtbot, no_dialogs):
    from photomesh.mainwindow import MainWindow
    w = MainWindow(); qtbot.addWidget(w)
    w.layer_action("grid").setChecked(False); w.layer_action("region").setChecked(True)
    w.project.modified = False; w.close()
    w2 = MainWindow(); qtbot.addWidget(w2)
    try:
        assert not w2.layer_action("grid").isChecked() and w2.layer_action("region").isChecked()
        assert w2.model_view.show["grid"] is False and w2.model_view.show["region"] is True
    finally:
        w2.layer_action("grid").setChecked(True); w2.layer_action("region").setChecked(False)
        w2.project.modified = False


def test_workspace_rows_metashape_style(window, tmp_path):
    from PySide6.QtCore import Qt
    rows = [it.text(0) for it in window.workspace._iter()]
    assert rows[0].startswith("Workspace (1 chunks")
    assert not any(r.startswith(("Tie Points", "Point Cloud", "3D Model", "Depth Maps")) for r in rows)
    _populate(window, tmp_path)
    rows = [it.text(0) for it in window.workspace._iter()]
    chunk = next(r for r in rows if r.startswith("Chunk 1"))
    assert chunk.endswith("[R]") and "8 images" in chunk and "3 markers" in chunk and "500 tie points" in chunk
    assert "Images (7/8 aligned)" in rows
    assert "Markers (3)" in rows
    assert any(r.startswith("Tie Points (500 points") for r in rows)
    assert any(r.startswith("Point Cloud (5,000 points") for r in rows)
    assert any(r.startswith("3D Model (80 faces") for r in rows)
    assert any(r.endswith("(NA)") for r in rows) and any("(check)" in r for r in rows)
    chunk_item = next(i for i in window.workspace._iter() if i.text(0) == chunk)
    assert chunk_item.font(0).bold()
    for k in ("cameras", "markers", "tie", "cloud", "mesh"):
        it = window.workspace._layer_items[k]
        assert it.checkState(0) in (Qt.Checked, Qt.Unchecked) and not it.icon(0).isNull()


def test_workspace_selection_fills_properties(window, tmp_path):
    from photomesh import procmeta
    ch = _populate(window, tmp_path)
    procmeta.record(ch, "point_cloud", params={"quality": "High", "depth_filtering": "Mild"}, seconds=12.5,
                    points=5000)
    window.refresh_all()
    it = next(i for i in window.workspace._iter() if i.text(0).startswith("Point Cloud"))
    assert "High quality" in it.text(0)
    window.workspace.setCurrentItem(it)
    txt = " | ".join(f"{window.properties.item(r, 0).text()}={window.properties.item(r, 1).text()}"
                     for r in range(window.properties.rowCount()))
    assert "Mild" in txt and "5,000" in txt


def test_record_roundtrips_through_pmsx(tmp_path):
    from photomesh import procmeta
    from photomesh.project import Project, Chunk
    p = Project(); p.chunks = [Chunk()]
    e = procmeta.record(p.chunks[0], "model", params={"face_count": "High"}, seconds=3.2,
                        stages={"Meshing": 3.0}, faces=10, vertices=7)
    assert e["software"] and e["date"]
    path = str(tmp_path / "x.pmsx"); p.save(path)
    q = Project.load(path)
    m = q.chunks[0].meta["model"]
    assert m["faces"] == 10 and m["params"]["face_count"] == "High" and m["seconds"] == 3.2


def test_backfill_from_utf16_logs_keeps_unknowns(tmp_path):
    from photomesh import procmeta
    from photomesh.project import Chunk
    root = tmp_path / "bench"; wd = root / "fast"; (wd / "dense").mkdir(parents=True)
    (root / "benchmark_log.txt").write_text(
        "== fast / mapper glomap -> glomap\nPair preselection: 1,000 of 5,000\nKept 18000 best features per photo\n"
        "Matching (sequential)   10.0 s\nRegistering images   5.0 s\nTOTAL   20.5 s\n"
        "fast   20.5   1000   900   7   500   1.092\n", encoding="utf-16")
    mesh = wd / "model.ply"; mesh.write_bytes(b"ply\n")
    (root / "mesh_local.txt").write_text(f"Poisson depth 10\nMesh: 1,000 vertices, 2,000 faces (4.5 s) -> {mesh}\n",
                                         encoding="utf-16")
    ch = Chunk(workdir=str(wd), tie_point_count=500, model_ply=str(mesh), face_count=2000)
    added = procmeta.backfill(ch, str(root / "p.pmsx"))
    assert "alignment" in added and "model" in added
    a = ch.meta["alignment"]
    assert a["seconds"] == 20.5 and a["registered"] == 7 and a["rms_reprojection_px"] == 1.092
    assert a["params"]["key_point_limit"] == 18000
    assert a["params"]["tie_point_limit"] is None and a["max_reprojection_px"] is None      # unknown, not invented
    assert a["software"] is None and "backfilled" in a["source"]
    m = ch.meta["model"]
    assert m["faces"] == 2000 and m["vertices"] == 1000 and m["params"]["poisson_depth"] == 10
    rows = dict(procmeta.properties(ch, "tie"))
    assert procmeta.UNKNOWN in " ".join(str(v) for v in rows.values())
    assert procmeta.backfill(ch, str(root / "p.pmsx")) == []          # idempotent


def test_open_backfills_in_memory_without_writing(window, tmp_path):
    from photomesh.project import Project, Chunk
    root = tmp_path / "bench"; wd = root / "fast"; wd.mkdir(parents=True)
    (root / "benchmark_log.txt").write_text("Matching   1.0 s\nTOTAL   2.0 s\n", encoding="utf-16")
    p = Project(); p.chunks = [Chunk(workdir=str(wd), tie_point_count=10)]
    path = str(root / "p.pmsx"); p.save(path)
    before = open(path, "rb").read(); mtime = os.path.getmtime(path)
    window.open_project(path)
    assert "alignment" in window.chunk.meta
    assert window.project.modified                                    # saved on next Save
    assert open(path, "rb").read() == before and os.path.getmtime(path) == mtime
    window.save_project()
    assert "alignment" in json.load(open(path, encoding="utf-8"))["chunks"][0]["meta"]


def test_view_overlays(window):
    mv = window.model_view
    assert mv.proj_label.text() == "Perspective 30\u00b0"
    assert not hasattr(mv, "snap_label")          # 0.1.12: the snap toggles did not snap anything
    mv.set_orthographic(True); assert mv.proj_label.text() == "Orthographic"
    mv.set_orthographic(False)
    # gizmo: top view -> Z points at the viewer, X right
    mv.gizmo.set_angles(-90, 90)
    ax = mv.gizmo.axes_2d()
    assert ax["X"][0] > 0.9 and abs(ax["Z"][0]) < 1e-6 and abs(ax["Z"][1]) < 1e-6
    tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert tabs[:2] == ["Model", "Ortho"]


def test_mode_buttons_are_large_split_buttons(window, tmp_path):
    from PySide6.QtWidgets import QToolButton
    from photomesh.toolbuttons import ARROW_W
    _populate(window, tmp_path)
    window.wait_view_loaded(30)
    from photomesh.icons import TOOLBAR_ICON, MODE_ICON
    assert window.main_toolbar.iconSize().width() == TOOLBAR_ICON == 21    # was 16 px (+30 %)
    for b in window.mode_buttons:
        assert b.popupMode() == QToolButton.MenuButtonPopup
        assert b.iconSize().width() == MODE_ICON == 26 and b.text()       # was 20 px (+30 %)
        assert ARROW_W == 21 and b.sizeHint().width() >= 26 + ARROW_W + 10
        assert b.menu() is not None and b.menu().actions()
    mb = window.model_mode_button
    assert mb.text() == "Shaded"
    window.actions["Model - Wireframe"].trigger()                   # from the menu bar
    assert mb.text() == "Wireframe" and mb.defaultAction() is window.actions["Model - Wireframe"]
    window.show_model_mode("solid")                                  # from code
    assert mb.text() == "Solid"
    window.actions["Point Cloud - Solid"].trigger()
    assert window.cloud_mode_button.text() == "Solid"
    assert window.layer_states()["cloud"] and not window.layer_states()["mesh"]
    mb.click()                                                       # main part runs the current mode
    assert window.layer_states()["mesh"] and not window.layer_states()["cloud"]
    assert not hasattr(window, "selection_button")   # 0.1.12: selection tools were not implemented
