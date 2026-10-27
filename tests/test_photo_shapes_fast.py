"""Shapes on a photo: vectorised batch projection (thousands of shapes), fast
first draw without the mesh occlusion test, occluded version from a
background thread; tie point statistics on project open."""
import time

import numpy as np
import pytest

from photomesh import colmap_io as cio
from photomesh import shapes as S
from tests.test_markers_manual import _scene, _ground_ply


def _many_shapes(n_lines=6861, n_poly=756, seed=0):
    """Like the user's OVS Slope.dxf: 6861 LINEs + 756 LWPOLYLINEs (+ polygons,
    points) over a 400 m site; the photos see ~60 x 48 m of it."""
    rng = np.random.default_rng(seed)
    sset = S.new_set()
    a = S.add_layer(sset, "OVS-Edge"); b = S.add_layer(sset, "Tana Excavation"); c = S.add_layer(sset, "pts")
    for _ in range(n_lines):
        p = rng.uniform([-60, -60, 0], [300, 260, 3]); q = p + rng.normal(0, 4, 3)
        sset["shapes"].append(S.make_shape(a, "polyline", [np.array([p, q])]))
    for k in range(n_poly):
        P = np.cumsum(rng.normal(0, 3, (6, 3)), 0) + rng.uniform([60, 60, 0], [160, 140, 1])
        kind = "polygon" if k % 20 == 0 else "polyline"
        sset["shapes"].append(S.make_shape(b, kind, [P], label=f"P{k}"))
    for k in range(40):
        sset["shapes"].append(S.make_shape(c, "point", [rng.uniform([80, 80, 0], [140, 120, 0])[None]], label=f"G{k}"))
    return sset


def _window_with_shapes(window, tmp_path, sset):
    ch = _scene(window, tmp_path)
    window._store_shapes(ch, sset)
    window.set_layer_style("Tana Excavation", {"fill_opacity": 0.3, "label_field": ""})
    return ch


def _reference_items(window, k):
    """The old per-shape loop (S.project_polylines per shape) for comparison."""
    ch = window.chunk
    sset, vis = window._visible_shapes(ch)
    model = window._sparse_model()
    from photomesh import georef as g
    im = model.images[g.name_index(model)[g._stem(ch.cameras[k].label)]]
    cam = model.cameras[im.camera_id]
    frame = window._frame(ch)
    out = {}
    for i in vis:
        s = sset["shapes"][i]
        parts = [frame.to_local(p) for p in S.display_parts(s)]
        if s["type"] == "point":
            continue
        ls = S.project_polylines(parts, cam, im.R(), im.tvec, closed=s["type"] == "polygon")
        if ls:
            out[i] = ls
    return out


def test_batch_projection_matches_the_per_shape_projection(window, tmp_path):
    _window_with_shapes(window, tmp_path, _many_shapes(800, 200))
    items = window.photo_shape_items(0, occlusion=False)
    ref = _reference_items(window, 0)
    got = {it["index"]: it["lines"] for it in items if it["lines"]}
    assert set(got) == set(ref) and len(got) > 20
    for i in ref:
        assert len(got[i]) == len(ref[i])
        for x, y in zip(got[i], ref[i]):
            np.testing.assert_allclose(x, y, atol=1e-6)
    # points, polygon fills and label anchors still produced
    assert any(it["points"] for it in items)
    assert any(it["fills"] for it in items) and any(it["anchors"] for it in items)


def test_first_draw_is_fast_for_thousands_of_shapes(window, tmp_path):
    """7657 shapes in the project (the user's DXF size); the old per-shape loop
    took 7.5 s per photo on the laptop. Projection (all shapes, vectorised) +
    drawing must stay well below that; the per-version preparation is done
    once and reused by every photo."""
    _window_with_shapes(window, tmp_path, _many_shapes())
    window.open_photo(0)
    window._photo_prep_cache = None; window._photo_shape_cache.clear()
    t = time.perf_counter(); first = window.photo_shape_items(0, occlusion=False); t_first = time.perf_counter() - t
    window._photo_shape_cache.clear()
    t = time.perf_counter(); items = window.photo_shape_items(1, occlusion=False); t_photo = time.perf_counter() - t
    t = time.perf_counter(); window.photo_view.set_shapes(items); t_draw = time.perf_counter() - t
    window._photo_shape_cache.clear()
    t = time.perf_counter(); window._update_photo_shapes(); t_gui = time.perf_counter() - t
    n = len(items)
    assert n > 150, n
    print(f"\nshapes on photo: {n}; prep+project {t_first:.3f} s, project {t_photo:.3f} s, "
          f"draw {t_draw:.3f} s, GUI update {t_gui:.3f} s")
    assert t_photo < 0.3, t_photo
    assert t_first < 1.5, t_first
    assert t_gui < 0.6, t_gui


def test_occlusion_runs_in_the_background_after_a_fast_first_draw(window, tmp_path, monkeypatch):
    ch = _window_with_shapes(window, tmp_path, _many_shapes(400, 100))
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"))
    window.open_photo(0)
    window._occl.update(key=window._occl_key(ch), scene=object(), busy=False)   # scene "ready"
    calls = []

    def slow_occluded(scene, Xc, R, t, tol):
        calls.append(len(Xc)); time.sleep(0.4)                 # a slow ray cast
        Xw = (Xc - t) @ R
        return Xw[:, 0] < 110.0                                  # west part "under the surface"
    monkeypatch.setattr(S, "_occluded", slow_occluded)
    window._photo_shape_cache.clear()
    t = time.perf_counter(); window._update_photo_shapes(); dt = time.perf_counter() - t
    assert dt < 0.3, dt                                          # the GUI thread didn't wait for the rays
    first = window.photo_view.shapes
    assert first and not any(it["hidden_lines"] for it in first)
    assert window.wait_photo_occlusion(10)
    after = window.photo_view.shapes
    assert any(it["hidden_lines"] for it in after) and calls
    assert len(calls) <= 2                                       # one batched cast (lines) + points
    # reopening the photo uses the cached occluded result directly
    calls.clear(); window._update_photo_shapes()
    assert any(it["hidden_lines"] for it in window.photo_view.shapes) and not calls


def test_stale_background_result_is_dropped(window, tmp_path, monkeypatch):
    ch = _window_with_shapes(window, tmp_path, _many_shapes(200, 50))
    ch.model_ply = _ground_ply(str(tmp_path / "ground.ply"))
    window.open_photo(0)
    window._occl.update(key=window._occl_key(ch), scene=object(), busy=False)
    monkeypatch.setattr(S, "_occluded", lambda sc, Xc, R, t, tol: (time.sleep(0.3), np.ones(len(Xc), bool))[1])
    window._photo_shape_cache.clear(); window._update_photo_shapes()
    window.current_photo = 1                                     # user moved on to another photo
    window.wait_photo_occlusion(10)
    assert not any(it["hidden_lines"] for it in window.photo_view.shapes)


# ------------------------------------------------------------ tie point statistics
def test_sparse_stats():
    from photomesh import procmeta
    from tests.test_dense_mesh_gcp import synthetic_model
    m = synthetic_model()
    st = procmeta.sparse_stats(m)
    errs, _i, _p = cio.observation_errors(m)
    assert st["tie_points"] == len(m.points) and st["projections"] == len(errs)
    assert st["rms_reprojection_px"] == pytest.approx(np.sqrt(np.mean(errs ** 2)), rel=1e-3)
    assert st["max_reprojection_px"] == pytest.approx(errs.max(), rel=1e-3)
    assert st["mean_track_length"] == pytest.approx(len(errs) / len(m.points), rel=1e-3)
    assert procmeta.sparse_stats(cio.Model({}, {}, cio.Points(*[np.zeros(0)] * 3, np.zeros(0), np.zeros(1, np.int64),
                                                              np.zeros(0, np.uint32), np.zeros(0, np.uint32))))["tie_points"] == 0


def test_tie_point_statistics_on_open(window, tmp_path, monkeypatch):
    """Opening a project with tie points computes the statistics in the view
    loader: no 'has no attribute sparse_stats' error, shown in Properties,
    and the project is not marked modified by it."""
    from photomesh import procmeta
    from tests.test_dense_mesh_gcp import synthetic_model
    logs = []
    monkeypatch.setattr(window, "log", lambda m, *a, **k: logs.append(str(m)))
    ch = window.chunk
    wd = tmp_path / "w"; (wd / "sparse" / "0").mkdir(parents=True)
    cio.write_model(str(wd / "sparse" / "0"), synthetic_model())
    ch.workdir = str(wd); ch.tie_point_count = 100
    window.project.modified = False
    window.reload_view(); window.wait_view_loaded(30)
    assert not [l for l in logs if "Tie point statistics" in l], logs
    st = (ch.meta or {}).get("sparse_stats")
    assert st and st["projections"] > 0 and st["rms_reprojection_px"] is not None
    assert not window.project.modified
    rows = dict(procmeta.properties(ch, "tie"))
    assert rows["Projections"] != procmeta.UNKNOWN and "px" in rows["RMS reprojection error"]


def test_photo_view_draws_thousands_of_shapes_as_few_items(qapp, tmp_path):
    """PhotoView.set_shapes merges line paths per pen: 6000 shapes are a few scene
    items, not two per shape, and it draws well under the 0.3 s budget."""
    from PySide6.QtGui import QImage
    from photomesh.views import PhotoView, _polyline_path
    img = QImage(2000, 1500, QImage.Format_RGB888); img.fill(0); p = str(tmp_path / "p.png"); img.save(p)
    pv = PhotoView(); pv.open(p)
    rng = np.random.default_rng(0); items = []
    for i in range(6000):
        L = np.cumsum(rng.normal(0, 5, (44, 2)), 0) + rng.uniform(0, 2000, 2)
        it = {"color": (255, 140, 0) if i % 3 else (0, 200, 255), "width": 2.0, "lines": [L], "points": [],
              "selected": i == 7}
        if i % 300 == 0:
            it.update(fills=[L], fill_opacity=0.3, anchors=[(L[0, 0], L[0, 1], f"P{i}")])
        items.append(it)
    pv.set_shapes(items)                                   # warm-up
    t = time.perf_counter(); pv.set_shapes(items); dt = time.perf_counter() - t
    print(f"\nPhotoView.set_shapes 6000 shapes: {dt:.3f} s, {len(pv._shape_items)} items")
    assert dt < 0.3 and len(pv._shape_items) < 100
    # the merged path keeps the polylines apart and skips NaN gaps
    path = _polyline_path([np.array([[0, 0], [10, 0]]), np.array([[0, 5], [np.nan, np.nan], [10, 5]]),
                           np.array([[0, 9], [5, 9], [10, 9]])])
    from PySide6.QtCore import QPointF
    starts = [path.elementAt(i) for i in range(path.elementCount()) if path.elementAt(i).isMoveTo()]
    assert len(starts) == 4 and path.elementCount() == 7
