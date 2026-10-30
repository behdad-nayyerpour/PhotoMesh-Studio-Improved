"""0.1.17: observed progress, lifecycle, marker actions and error summaries."""
import math
import threading
import time

import numpy as np
import pytest

from photomesh import georef as g
from photomesh.progress import WorkProgress
from test_markers_manual import _scene, _marker


def test_observed_progress_has_no_invented_percentage_and_eta():
    now = [0.]
    meter = WorkProgress(clock=lambda: now[0])
    assert "unavailable" in meter.text()
    now[0] = 10
    meter.update(1, 10)
    assert "estimating" in meter.text()
    now[0] = 14
    meter.update(3, 10)
    assert "30.0%" in meter.text() and "14 s left" in meter.text()
    now[0] = 20
    assert "30.0%" in meter.text()  # idle time cannot invent completed work
    meter.update(0, 0)
    assert "unavailable" in meter.text()


def test_progress_dialog_keeps_unknown_stage_indeterminate(qtbot):
    from photomesh.dialogs import ProgressDialog
    dialog = ProgressDialog("Test")
    qtbot.addWidget(dialog)
    dialog.set_progress(20, "Stage")
    dialog.set_stage_progress(-1, "Loading")
    assert dialog.stage_bar.maximum() == 0
    dialog.set_stage_progress(.4, "4/10 completed")
    assert dialog.stage_bar.value() == 400
    dialog.set_progress(10, "Retry")
    assert dialog.bar.value() == 200
    dialog.running = False


def test_pipeline_callback_does_not_block_gui_and_reports_failure(qtbot):
    from photomesh.colmap import PipelineRunner
    main = threading.get_ident()
    thread = []
    def work():
        thread.append(threading.get_ident())
        time.sleep(.03)
        return []
    runner = PipelineRunner(None, [("Planning", work, 1)])
    done = []
    runner.finished.connect(lambda *args: done.append(args))
    runner.start()
    assert not done
    qtbot.waitUntil(lambda: bool(done), timeout=3000)
    assert done == [(True, "Finished")] and thread[0] != main


def test_marker_menu_uses_hit_marker_and_movement_can_be_cancelled(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    ch = _scene(window, tmp_path)
    label = window.add_marker_at_photo(500, 400, k=0)
    window.marker_view_action("ignore", label, "ortho")
    assert _marker(window, label)["disabled"] is True
    assert _marker(window, label)["enabled"] is True  # still a control, not a check
    assert window.model_view.marker_states[label] == "predicted"
    window.marker_view_action("use", label, "model")
    window.marker_view_action("pin", label, "ortho")
    assert _marker(window, label)["locked"]
    count = len(window._mk_undo)
    for name, view in (("photo", window.photo_view), ("ortho", window.ortho_view), ("model", window.model_view)):
        assert window.marker_view_action("move", label, name)
        assert view._moving_marker == label
        view.cancel_marker_move()
        assert view._moving_marker is None
    assert len(window._mk_undo) == count  # cancelling a pick makes no model edit
    assert window.undo_markers()
    assert not _marker(window, label)["locked"]


def test_photo_move_click_pins_same_marker(window, tmp_path, qtbot):
    from PySide6.QtCore import Qt, QPoint
    _scene(window, tmp_path)
    label = window.add_marker_at_photo(500, 400, k=0)
    window.open_photo(0)
    view = window.photo_view
    view.begin_marker_move(label)
    scene = view.view.mapToScene(QPoint(100, 100))
    qtbot.mouseClick(view.view.viewport(), Qt.LeftButton, pos=QPoint(100, 100))
    point = _marker(window, label)["projections"]["img_0001"]
    assert np.allclose(point[:2], [scene.x(), scene.y()]) and point[2]
    assert len(window.chunk.markers) == 1 and view._moving_marker is None


def test_ortho_hit_testing_and_escape(window, qtbot):
    from PySide6.QtCore import Qt
    view = window.ortho_view
    view.center, view.scale = (100., 100.), 2.
    view.set_markers([("G1", 100., 100.)])
    x, y = view.to_screen(100., 100.)
    assert view.marker_at(x + 2, y) == "G1"
    view.begin_marker_move("G1")
    qtbot.keyClick(view, Qt.Key_Escape)
    assert view._moving_marker is None
    view.vis["markers"] = False
    assert view.marker_at(x, y) is None


def test_control_check_groups_and_rmse_are_independent_of_ignore(window):
    ch = window.chunk
    ch.markers = [g.Marker("A", enabled=True).to_dict(), g.Marker("B", enabled=True).to_dict(),
                  g.Marker("C", enabled=False).to_dict(), g.Marker("D", disabled=True).to_dict()]
    ch.georef = {"rows": [{"label": "A", "total": 3., "pix": 2.},
                         {"label": "B", "total": 4., "pix": 4.},
                         {"label": "C", "total": 12., "pix": 6.},
                         {"label": "D", "total": 1000., "pix": 1000.}]}
    window.refresh_all()
    pane = window.markers_pane
    totals = pane.error_summary
    assert totals["Control points"]["error"] == pytest.approx(math.sqrt(12.5))
    assert totals["Check points"]["error"] == 12.
    assert totals["All points"]["count"] == 3
    pane.filter_group(1)
    assert not pane.isRowHidden(0) and pane.isRowHidden(2)
    pane.filter_group(2)
    assert pane.isRowHidden(0) and not pane.isRowHidden(2)
    assert "Total Error" in pane._summary_label.text()


def test_camera_updates_mark_visible_products_stale_and_imports_stay_valid(window):
    ch = window.chunk
    ch.model_ply = "existing-model.ply"
    ch.orthomosaic = {"path": "ortho.tif"}
    ch.dem = {"path": "external.tif", "format": "imported"}
    window._invalidate_camera_products(ch)
    window.refresh_all()
    assert ch.meta["model"]["stale"]
    assert window.model_view.stale_label.text().startswith("Update required")
    assert window.ortho_view.stale_message.startswith("Update required")
    assert "stale" not in ch.dem
    ch.meta["model"].pop("stale")
    ch.orthomosaic.pop("stale")
    window.refresh_all()
    assert not window.model_view.stale_label.text() and not window.ortho_view.stale_message


def test_marker_state_palette():
    from photomesh.marker_style import color, marker_state
    assert color("pinned").getRgb()[:3] == (40, 220, 60)
    assert color("placed").getRgb()[:3] == (60, 140, 255)
    assert color("predicted").getRgb()[:3] == (190, 190, 190)
    assert marker_state(g.Marker("A", projections={"photo": [1, 2, False]}).to_dict()) == "placed"
    assert marker_state(g.Marker("A", projections={"photo": [1, 2, True]}).to_dict()) == "pinned"
    assert marker_state(g.Marker("A", disabled=True, locked=True).to_dict()) == "predicted"


def test_background_status_is_counted_independently(window):
    busy = window.busy_indicator
    busy.background("tiles", "Loading tiles", 2, 10)
    busy.background("texture", "Loading texture")
    busy.background("tiles")
    assert busy.active and "Loading texture" in busy.label.text()
    busy.background("texture")
    assert not busy.active


def test_moving_marker_changes_estimate_without_overwriting_reference_xyz(window, tmp_path):
    ch = _scene(window, tmp_path)
    ch.georef = {"offset": [1000., 2000., 0.]}
    ch.markers = [g.Marker("G1", ref=[1110., 2100., 0.]).to_dict()]
    assert window.add_marker_at_ortho(1110., 2100., 0., label="G1") == "G1"
    assert _marker(window, "G1")["ref"] == [1110., 2100., 0.]
    assert window.add_marker_at_ortho(1115., 2100., 0., label="G1") == "G1"
    assert _marker(window, "G1")["ref"] == [1110., 2100., 0.]
    assert window.ortho_view.markers[0][1:] == pytest.approx((1115., 2100.), abs=1e-3)


def test_estimated_world_marker_offset_is_applied_once(window):
    window.chunk.georef = {"offset": [1000., 2000., 0.]}
    window.chunk.markers = [g.Marker("G1", est=[1110., 2100., 0.]).to_dict()]
    window._update_ortho_markers()
    assert window.ortho_view.markers == [("G1", 1110., 2100.)]


def test_explicit_surface_move_can_replace_pins_and_is_undoable(window, tmp_path):
    ch = _scene(window, tmp_path)
    label = window.add_marker_at_photo(500., 400., k=0)
    old = dict(_marker(window, label)["projections"])
    window.ortho_view.moveMarkerRequested.emit(label, 110., 100., 0.)
    points = _marker(window, label)["projections"]
    assert all(not p[2] for p in points.values())
    assert window.ortho_view.markers[0][1:] == pytest.approx((110., 100.), abs=1e-3)
    assert window.undo_markers()
    assert _marker(window, label)["projections"] == old
