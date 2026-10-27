"""UI: markers pane, marker import/placement, new processing dialogs."""
import numpy as np

from tests.test_dense_mesh_gcp import GCP_TXT


def test_import_markers_and_place(window, image_dir, tmp_path, no_dialogs):
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    p = tmp_path / "GCP.txt"; p.write_text(GCP_TXT, encoding="utf-8")
    window.import_markers(str(p))
    n = window.markers_pane.rowCount()
    assert n >= 3 and window.chunk.crs.startswith("LOCAL_CS")
    window.open_photo(0)
    window.markers_pane.selectRow(0)
    window.place_marker(12.5, 34.0)
    lab = window.markers_pane.item(0, 0).text()
    m = [m for m in window.chunk.markers if m["label"] == lab][0]
    assert list(m["projections"].values()) == [[12.5, 34.0, True]]
    assert any(x[0] == lab and x[3] == "pinned" for x in window.photo_view.markers)
    window.remove_marker_projection(lab)
    m = [m for m in window.chunk.markers if m["label"] == lab][0]
    assert m["projections"] == {}
    window.toggle_marker(lab, False)
    assert not [m for m in window.chunk.markers if m["label"] == lab][0]["enabled"]
    window.add_marker()
    assert window.markers_pane.rowCount() == n + 1


def test_new_dialogs_settings(window):
    from photomesh import dialogs as d
    s = d.BuildPointCloudDialog(window).settings()
    assert s["quality"] == "Medium" and s["max_sources"] == 8 and s["window_step"] == 2
    assert s["filters"]["min_confidence"] == 3
    s = d.BuildModelDialog(window).settings()
    assert s["face_count"] == "Medium" and s["interpolation"] == "Enabled"
    s = d.FilterPointCloudDialog(window, True).settings()
    assert s["min_confidence"] == 3 and not s["statistical"]
    s = d.OptimizeCamerasDialog(window).settings()
    assert abs(s["accuracies"]["marker_accuracy"] - 0.005) < 1e-9
    m = {"Reprojection error": np.array([0.1, 0.5, 2.0]), "Image count": np.array([2.0, 3, 5])}
    g = d.GradualSelectionDialog(m, window)
    g.crit.setCurrentText("Image count"); g.level.setValue(3)
    assert g.selection().tolist() == [True, False, False]
