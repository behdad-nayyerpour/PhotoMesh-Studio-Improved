"""Regression: File > Save and project open must not block the UI on big projects.

Old behaviour on the 641-photo drone project: Save took ~2 min ("Not responding")
because refilling the Reference table with ResizeToContents columns was
O(rows^2); open spent ~1 min computing mesh normals in pyqtgraph's Python loop
on the GUI thread and loaded the 28M-point cloud synchronously."""
import json
import os
import time

import numpy as np


def _big_chunk(window, n_cam=641, n_mark=98):
    from photomesh.project import Camera
    rng = np.random.default_rng(0)
    ch = window.chunk
    ch.cameras = []
    for k in range(n_cam):
        c = Camera(path=f"C:/photos/DJI_{k:04d}.JPG", aligned=True, lat=34 + rng.random(), lon=50 + rng.random(),
                   alt=1300 + rng.random(), est=list(rng.normal(size=3)), rot=np.eye(3).tolist(),
                   width=5280, height=3956)
        ch.cameras.append(c)
    ch.markers = [{"label": str(i), "ref": list(rng.normal(size=3) * 100), "acc": None, "enabled": i % 10 != 0,
                   "projections": {f"DJI_{j:04d}": [float(rng.random() * 5000), float(rng.random() * 4000), True]
                                   for j in rng.choice(n_cam, 30, replace=False)}, "est": None}
                  for i in range(n_mark)]
    ch.tie_point_count = 620917
    return ch


def test_save_is_fast_on_large_project(window, tmp_path):
    _big_chunk(window)
    window.refresh_all()
    t = time.perf_counter(); window.refresh_all(); t_refresh = time.perf_counter() - t
    assert window.reference.rowCount() == 641 and window.markers_pane.rowCount() == 98
    window.project.path = str(tmp_path / "big.pmsx")
    t = time.perf_counter()
    assert window.save_project()
    t_save = time.perf_counter() - t
    assert t_save < 1.0, f"save took {t_save:.2f} s"
    assert t_refresh < 2.0, f"pane refresh took {t_refresh:.2f} s"
    assert not os.path.exists(str(tmp_path / "big.pmsx.tmp"))      # atomic write, no leftovers
    d = json.load(open(tmp_path / "big.pmsx", encoding="utf-8"))
    assert len(d["chunks"][0]["cameras"]) == 641 and len(d["chunks"][0]["markers"]) == 98
    assert "*" not in window.windowTitle()


def test_reference_table_refill_stays_linear(window):
    """Refill time must not blow up with row count (was quadratic)."""
    from photomesh.project import Chunk, Camera
    def timed(n):
        ch = Chunk(cameras=[Camera(path=f"/p/{k}.jpg", lat=1.0, lon=2.0, alt=3.0, est=[1, 2, 3]) for k in range(n)])
        window.reference.refresh(ch)
        best = 1e9
        for _ in range(3):                    # best of 3: robust to other load on the machine
            t = time.perf_counter(); window.reference.refresh(ch); best = min(best, time.perf_counter() - t)
        return best
    t_small, t_big = timed(100), timed(800)
    assert t_big < 1.0 and t_big < 20 * max(t_small, 0.01)


def test_view_loads_in_background(window, tmp_path):
    """load_result / reload_view return immediately; data arrives via the loader thread."""
    from photomesh.plyio import save_ply
    n = 3_000_000
    rng = np.random.default_rng(1)
    p = tmp_path / "cloud.ply"
    save_ply(str(p), rng.random((n, 3)).astype(np.float32), rng.random((n, 3)))
    t = time.perf_counter()
    window.load_result("cloud", str(p))
    t_call = time.perf_counter() - t
    assert window.chunk.point_count == n                       # from the header
    assert window.wait_view_loaded(60)
    assert window.ortho_view.pts is not None and len(window.ortho_view.pts) > 1000
    assert t_call < 1.0, f"load_result blocked {t_call:.2f} s"
    assert not window.view_progress.isVisible()


def test_vertex_normals_match_pyqtgraph():
    from photomesh.plyio import vertex_normals
    gl = __import__("pytest").importorskip("pyqtgraph.opengl")
    rng = np.random.default_rng(2)
    v = rng.random((60, 3)).astype(np.float32)
    f = np.array([rng.choice(60, 3, replace=False) for _ in range(150)])
    ref = gl.MeshData(vertexes=v, faces=f).vertexNormals()
    used = np.unique(f)
    assert np.allclose(vertex_normals(v, f)[used], ref[used], atol=1e-4)


def test_display_decimation(tmp_path):
    from photomesh.plyio import save_ply, load_ply, ply_counts
    p = tmp_path / "c.ply"
    save_ply(str(p), np.random.rand(50_000, 3).astype(np.float32), np.random.rand(50_000, 3))
    d = load_ply(str(p), max_points=10_000)
    assert 9_000 < len(d["points"]) < 11_000 and d["colors"].shape == (len(d["points"]), 4)
    assert d["total"] == 50_000 and ply_counts(str(p)) == (50_000, 0)
