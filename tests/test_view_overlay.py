"""3D view: camera-relative lighting, normal orientation, and the label/marker
overlay pass (drawn after all geometry, depth test off)."""
import os

import numpy as np
import pytest


def test_headlight_lights_surfaces_facing_the_viewer():
    from photomesh.lighting import light_intensity
    up = light_intensity([0, 0, 1])                 # terrain seen from straight above
    assert up > 0.9
    # pyqtgraph's 'shaded' program: light from eye-space (1,-1,-1) -> 0.2 for this face
    old = 0.2 + max(np.dot([0, 0, 1], np.array([1, -1, -1]) / np.sqrt(3)), 0) * 0.8
    assert old == pytest.approx(0.2)
    oblique = light_intensity([0, np.sin(np.radians(55)), np.cos(np.radians(55))])   # flat ground, 35 deg elevation
    assert oblique > 0.75
    assert light_intensity([0, 0, -1]) == pytest.approx(up)            # two-sided
    side = light_intensity([1, 0, 0.05])
    assert 0.25 <= side < oblique                                       # relief still visible


def test_normal_matrix_is_inverse_transpose():
    from photomesh.lighting import normal_matrix
    M = np.eye(4); M[:3, :3] = np.diag([2.0, 1.0, 0.5]); M[:3, 3] = [5, 6, 7]
    n = normal_matrix(M) @ np.array([1.0, 1.0, 0.0])
    t = M[:3, :3] @ np.array([1.0, -1.0, 0.0])                         # tangent stays perpendicular
    assert abs(np.dot(n, t)) < 1e-12


def test_orient_normals_up_flips_downward_terrain_only():
    from photomesh.lighting import orient_normals_up
    rng = np.random.default_rng(0)
    N = rng.normal(size=(1000, 3)); N[:, 2] = -np.abs(N[:, 2]) - 0.5
    out, flipped = orient_normals_up(N, min_fraction=0.65)
    assert flipped and (out[:, 2] > 0).all()
    S = rng.normal(size=(1000, 3))                                      # closed object: ~half down
    out, flipped = orient_normals_up(S, min_fraction=0.65)
    assert not flipped and np.allclose(out, S.astype(np.float32))


def _terrain(n=6):
    x, y = np.meshgrid(np.arange(n, dtype=float), np.arange(n, dtype=float))
    V = np.c_[x.ravel(), y.ravel(), 0.1 * np.sin(x.ravel())]
    F = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            F += [[a, a + 1, a + n + 1], [a, a + n + 1, a + n]]      # CCW seen from +Z
    return V, np.array(F)


@pytest.mark.parametrize("text", [False, True])
def test_transform_ply_with_reflection_keeps_normals_and_winding_up(tmp_path, text):
    from plyfile import PlyData, PlyElement
    from photomesh import georef as g
    from photomesh.plyio import vertex_normals
    V, F = _terrain()
    N = vertex_normals(V, F)
    assert (N[:, 2] > 0).all()
    v = np.zeros(len(V), dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("nx", "f4"), ("ny", "f4"), ("nz", "f4")])
    v["x"], v["y"], v["z"] = V.T; v["nx"], v["ny"], v["nz"] = N.T
    f = np.empty(len(F), dtype=[("vertex_indices", "i4", (3,))]); f["vertex_indices"] = F
    p = tmp_path / "m.ply"
    PlyData([PlyElement.describe(v, "vertex"), PlyElement.describe(f, "face")], text=text).write(str(p))
    M = np.eye(4); M[:3, :3] = np.diag([-2.0, 2.0, 2.0]); M[:3, 3] = [100, 200, 300]   # mirror in X, scale 2
    g.transform_ply(str(p), M)
    with open(p, "rb") as fh:
        out = PlyData.read(fh, mmap=False)
    o = out["vertex"].data
    P = np.column_stack([o["x"], o["y"], o["z"]]).astype(float)
    assert np.allclose(P, V @ M[:3, :3].T + M[:3, 3], atol=1e-3)
    Fo = np.stack(out["face"].data["vertex_indices"]).astype(np.int64)
    assert sorted(map(tuple, np.sort(Fo, 1))) == sorted(map(tuple, np.sort(F, 1)))   # same triangles
    fn = np.cross(P[Fo[:, 1]] - P[Fo[:, 0]], P[Fo[:, 2]] - P[Fo[:, 0]])
    assert (fn[:, 2] > 0).all(), "winding must be reversed by a mirror so faces still face up"
    assert (o["nz"] > 0).all(), "stored normals must still point up"
    assert np.allclose(np.column_stack([o["nx"], o["ny"], o["nz"]]), N * [-1, 1, 1], atol=1e-5)


def test_projection_culls_points_behind_the_camera():
    from photomesh.overlay3d import project
    P = np.diag([1.0, 1.0, -1.0, 1.0]); P[3] = [0, 0, -1, 0]; P[2, 3] = -0.2   # simple perspective, eye at 0
    xy, z, ok = project(P, [[0, 0, -5], [0, 0, 5], [1, 0, -2]], 200, 100)
    assert ok.tolist() == [True, False, True]
    assert xy[0] == pytest.approx([100, 50])


def test_label_rect_stays_inside_viewport():
    from photomesh.overlay3d import label_rect
    for anchor in [(0, 0), (199, 0), (199, 99), (0, 99), (100, 50), (-1, 50)]:
        r = label_rect(anchor, 40, 12, 200, 100)
        assert r.left() >= 1 and r.top() >= 1 and r.right() <= 199 and r.bottom() <= 99, anchor


def _identity_view():
    return np.diag([0.01, 0.01, 0.001, 1.0])                            # orthographic, depth ~ 0.5


def test_overlay_draws_labels_over_geometry_and_marks_occluded(qapp):
    from PySide6.QtGui import QImage, QPainter, QColor
    from photomesh.overlay3d import Overlay
    ov = Overlay(); ov.legend = False
    ov.markers = (np.array([[0.0, 0.0, 0.0], [50.0, 20.0, 0.0]]), ["GCP1", "GCP2"], np.array([True, False]))
    img = QImage(200, 100, QImage.Format_RGB32); img.fill(QColor(180, 120, 90))   # "textured mesh"
    depth = lambda xy: np.where(xy[:, 0] > 120, 0.0, 1.0)              # surface in front of the 2nd marker
    items = ov.items(_identity_view(), 200, 100, depth_fn=depth)
    assert [i[2] for i in items] == ["GCP1", "GCP2"] and [i[4] for i in items] == [False, True]
    p = QPainter(img); rects = ov.paint(p, items, 200, 100); p.end()
    assert len(rects) == 2 and ov.last["occluded"] == 1
    for _k, _t, r in rects:                                             # label boxes painted, fully
        c = img.pixelColor(int(r.left() + 1), int(r.center().y()))
        assert c.red() < 90 and c.green() < 90, c.name()
    assert img.pixelColor(100, 50) == QColor(255, 214, 40)             # filled control dot at its pixel


def test_label_visibility_and_legend(qapp):
    from PySide6.QtGui import QImage, QPainter, QColor
    from photomesh.views import ModelView
    mv = ModelView()
    mv.set_markers(np.array([[0.0, 0.0, 0.0]]), labels=["7"], control=[False])
    assert mv.overlay.markers is not None and mv.overlay.show["marker_labels"]
    mv.set_visible("marker_labels", False)
    items = mv.overlay.items(_identity_view(), 200, 100)
    assert items and items[0][2] == "" and items[0][3] == QColor(60, 220, 255)    # check point colour
    img = QImage(400, 200, QImage.Format_RGB32); img.fill(QColor(0, 0, 0))
    p = QPainter(img); mv.overlay.paint(p, items, 400, 200); p.end()
    assert mv.overlay.last["labels"] == 0
    mv.set_visible("markers", False)
    assert mv.overlay.items(_identity_view(), 200, 100) == []
    mv.set_cameras([(np.zeros(3), np.eye(3)), (np.ones(3), np.eye(3))], labels=["a", "b"])
    mv.set_visible("cameras", True); mv.set_visible("camera_labels", True)
    assert [i[2] for i in mv.overlay.items(_identity_view(), 200, 100)] == ["a", "b"]
    mv.set_shapes([{"color": (255, 0, 0), "points": np.zeros((1, 3)), "labels": ["S1"], "lines": []}])
    assert "S1" in [i[2] for i in mv.overlay.items(_identity_view(), 200, 100)]


def test_camera_icon_scale_follows_camera_spacing():
    from photomesh.views import camera_icon_scale
    g = np.array([[x * 20.0, y * 20.0, 100.0] for x in range(10) for y in range(10)])
    assert camera_icon_scale(g, 1000.0) == pytest.approx(12.0)
    assert camera_icon_scale(g, 100.0) == pytest.approx(3.0)            # capped at 3% of the radius


@pytest.mark.opengl
@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM", "") in ("offscreen", "minimal") or
                    not os.environ.get("PHOTOMESH_GL_TESTS"), reason="needs a real OpenGL display")
def test_gl_overlay_pass_runs_after_geometry_with_depth_test_off(qapp):
    import time
    from photomesh.views import ModelView
    from PySide6.QtWidgets import QWidget
    mv = ModelView(); mv.resize(400, 300); QWidget.show(mv)      # ModelView.show is the layer dict
    V = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float); F = np.array([[0, 1, 2], [0, 2, 3]])
    mv.set_mesh(V, F)
    mv.set_markers(np.array([[0.0, 0.0, -0.5], [0.5, 0.5, 0.5]]), labels=["below", "above"], control=[True, True])
    mv.gl.setCameraPosition(elevation=90, azimuth=0)
    for _ in range(20):
        qapp.processEvents(); time.sleep(0.02)
    img = mv.gl.grabFramebuffer()
    assert mv.gl.pass_log == ["geometry", "overlay"]
    assert mv.overlay.last_depth_test is False
    assert mv.overlay.last["labels"] == 2 and mv.overlay.last["occluded"] == 1     # 'below' is under the plane
    k = img.width() / mv.gl.width()                  # framebuffer is in device pixels (HiDPI: 1.5 x)
    for _k, t, r in mv.overlay.last["label_rects"]:
        c = img.pixelColor(int((r.left() + 2) * k), int(r.center().y() * k))
        assert c.red() < 110, (t, c.name())                               # dark label box, not the mesh
