import numpy as np

from photomesh import colmap_io as cio
from photomesh import marker_refine as mr


def _cam():
    return cio.Camera(2, 400, 300, np.array([350.0, 200.0, 150.0, -0.02]))


def _view(C, yaw=0.0, tilt=0.0):
    from scipy.spatial.transform import Rotation
    # camera looking straight down (z_cam = -Z_world), then yaw/tilt
    R0 = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1.0]])
    R = Rotation.from_euler("zx", [yaw, tilt]).as_matrix() @ R0
    C = np.asarray(C, float)
    return R, -R @ C


def _render(cam, R, t):
    """Ground plane Z=0 with texture + a checkerboard target at the origin."""
    v, u = np.mgrid[:cam.height, :cam.width].astype(float) + 0.5
    C, d = mr._ray(cam, R, t, np.stack([u.ravel(), v.ravel()], 1))
    P = mr._plane_hit(C, d, np.zeros(3), np.array([0, 0, 1.0]))
    x, y = P[:, 0], P[:, 1]
    tex = 100 + 30 * np.sin(3.1 * x) * np.cos(2.3 * y) + 20 * np.sin(7.7 * x + 1.3 * y)
    inside = (np.abs(x) < 0.5) & (np.abs(y) < 0.5)
    chk = np.where((x > 0) ^ (y > 0), 230.0, 20.0)
    img = np.where(inside, chk, tex)
    return img.reshape(cam.height, cam.width).astype(np.float32)


def test_saddle_refine_finds_checkerboard_corner():
    cam = _cam()
    R, t = _view([0.3, -0.2, 20.0], yaw=0.4)
    img = _render(cam, R, t)
    true, _ = cio.project(cam, R, t, np.zeros((1, 3)))
    guess = true[0] + [7.0, -5.0]
    uv, score = mr.saddle_refine(img, guess, search=20)
    assert uv is not None and score > 8
    assert np.hypot(*(np.array(uv) - true[0])) < 0.5


def test_template_refine_transfers_user_placement():
    cam = _cam()
    Rs, ts = _view([0.5, 0.2, 18.0], yaw=0.2)
    Rt, tt = _view([-2.0, 1.0, 22.0], yaw=-0.5, tilt=0.15)
    gs, gt = _render(cam, Rs, ts), _render(cam, Rt, tt)
    uvs, _ = cio.project(cam, Rs, ts, np.zeros((1, 3)))
    uvt, _ = cio.project(cam, Rt, tt, np.zeros((1, 3)))
    src = {"gray": gs, "origin": (0, 0), "cam": cam, "R": Rs, "t": ts, "uv": uvs[0]}
    tgt = {"gray": gt, "origin": (0, 0), "cam": cam, "R": Rt, "t": tt}
    X_wrong = np.array([0.3, -0.25, 0.0])        # model/marker estimate 0.4 m off
    uv, ncc = mr.template_refine(src, tgt, X_wrong, search=15)
    assert uv is not None and ncc > 0.8
    # the source placement defines the point on the ground -> recovered in the target
    assert np.hypot(*(np.array(uv) - uvt[0])) < 0.7
