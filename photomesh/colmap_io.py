"""Read/write COLMAP binary sparse models (cameras.bin, images.bin, points3D.bin).

Pure numpy + struct, no COLMAP needed. Points are held as flat arrays so
models with millions of observations stay fast:
    Model.cameras  {camera_id: Camera(model, width, height, params)}
    Model.images   {image_id: Image(name, camera_id, qvec, tvec, xys, point3D_ids)}
    Model.points   Points(ids, xyz, rgb, error, track_start, track_img, track_idx)
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

import numpy as np

CAMERA_MODELS = {0: ("SIMPLE_PINHOLE", 3), 1: ("PINHOLE", 4), 2: ("SIMPLE_RADIAL", 4),
                 3: ("RADIAL", 5), 4: ("OPENCV", 8), 5: ("OPENCV_FISHEYE", 8),
                 6: ("FULL_OPENCV", 12)}
MODEL_ID = {v[0]: k for k, v in CAMERA_MODELS.items()}


@dataclass
class Camera:
    model: int
    width: int
    height: int
    params: np.ndarray

    @property
    def name(self):
        return CAMERA_MODELS[self.model][0]

    def K(self):
        """(fx, fy, cx, cy)."""
        p = self.params
        if self.model in (0, 2, 3):
            return p[0], p[0], p[1], p[2]
        return p[0], p[1], p[2], p[3]

    def dist(self):
        """(k1, k2, p1, p2, k3) - unused terms are 0."""
        p = self.params; m = self.model
        if m == 2:
            return p[3], 0.0, 0.0, 0.0, 0.0
        if m == 3:
            return p[3], p[4], 0.0, 0.0, 0.0
        if m == 4:
            return p[4], p[5], p[6], p[7], 0.0
        if m == 6:
            return p[4], p[5], p[6], p[7], p[8]
        return 0.0, 0.0, 0.0, 0.0, 0.0


@dataclass
class Image:
    name: str
    camera_id: int
    qvec: np.ndarray
    tvec: np.ndarray
    xys: np.ndarray
    point3D_ids: np.ndarray

    def R(self):
        return qvec_to_rot(self.qvec)

    def center(self):
        return -self.R().T @ self.tvec


@dataclass
class Points:
    ids: np.ndarray          # (n,) uint64
    xyz: np.ndarray          # (n,3) float64
    rgb: np.ndarray          # (n,3) uint8
    error: np.ndarray        # (n,) float64
    track_start: np.ndarray  # (n+1,) int64 offsets into track arrays
    track_img: np.ndarray    # (m,) uint32 image ids
    track_idx: np.ndarray    # (m,) uint32 point2D index

    def __len__(self):
        return len(self.ids)

    def track_len(self):
        return np.diff(self.track_start)

    def subset(self, keep):
        keep = np.asarray(keep)
        if keep.dtype == bool:
            keep = np.flatnonzero(keep)
        tl = self.track_len()[keep]
        starts = self.track_start[keep]
        idx = np.repeat(starts - np.r_[0, np.cumsum(tl)[:-1]], tl) + np.arange(tl.sum())
        return Points(self.ids[keep], self.xyz[keep], self.rgb[keep], self.error[keep],
                      np.r_[0, np.cumsum(tl)].astype(np.int64),
                      self.track_img[idx], self.track_idx[idx])


@dataclass
class Model:
    cameras: dict
    images: dict
    points: Points


def qvec_to_rot(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * w * z, 2 * x * z + 2 * w * y],
        [2 * x * y + 2 * w * z, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * w * x],
        [2 * x * z - 2 * w * y, 2 * y * z + 2 * w * x, 1 - 2 * x * x - 2 * y * y]])


def rot_to_qvec(R):
    from scipy.spatial.transform import Rotation
    x, y, z, w = Rotation.from_matrix(np.asarray(R, float)).as_quat()
    q = np.array([w, x, y, z])
    return -q if q[0] < 0 else q


# ------------------------------------------------------------------ read
def read_cameras(path):
    b = open(path, "rb").read()
    n = struct.unpack_from("<Q", b, 0)[0]; o = 8; out = {}
    for _ in range(n):
        cid, m, w, h = struct.unpack_from("<iiQQ", b, o); o += 24
        k = CAMERA_MODELS[m][1]
        out[cid] = Camera(m, int(w), int(h), np.array(np.frombuffer(b, "<f8", k, o))); o += 8 * k
    return out


_P2D = np.dtype([("x", "<f8"), ("y", "<f8"), ("id", "<i8")])


def read_images(path):
    b = open(path, "rb").read()
    n = struct.unpack_from("<Q", b, 0)[0]; o = 8; out = {}
    for _ in range(n):
        iid = struct.unpack_from("<I", b, o)[0]
        q = np.array(struct.unpack_from("<4d", b, o + 4)); t = np.array(struct.unpack_from("<3d", b, o + 36))
        cid = struct.unpack_from("<I", b, o + 60)[0]; o += 64
        e = b.index(b"\0", o); name = b[o:e].decode("utf-8", "replace"); o = e + 1
        m = struct.unpack_from("<Q", b, o)[0]; o += 8
        a = np.frombuffer(b, _P2D, m, o); o += 24 * m
        out[iid] = Image(name, cid, q, t, np.stack([a["x"], a["y"]], 1).copy(), a["id"].copy())
    return out


def read_points3D(path):
    b = open(path, "rb").read()
    n = struct.unpack_from("<Q", b, 0)[0]; o = 8
    ids = np.empty(n, np.uint64); xyz = np.empty((n, 3)); rgb = np.empty((n, 3), np.uint8)
    err = np.empty(n); tl = np.empty(n, np.int64); offs = np.empty(n, np.int64)
    unpack = struct.unpack_from
    for i in range(n):
        pid, x, y, z, r, g, bb, e, t = unpack("<Q3d3BdQ", b, o)
        ids[i] = pid; xyz[i] = (x, y, z); rgb[i] = (r, g, bb); err[i] = e; tl[i] = t
        offs[i] = o + 51; o += 51 + 8 * t
    total = int(tl.sum())
    # gather all track bytes with one vectorised index
    start = np.r_[0, np.cumsum(tl)].astype(np.int64)
    byte_idx = np.repeat(offs, tl) + 8 * (np.arange(total) - np.repeat(start[:-1], tl))
    raw = np.frombuffer(b, np.uint8)
    pairs = raw[byte_idx[:, None] + np.arange(8)].view("<u4").reshape(-1, 2) if total else np.empty((0, 2), "<u4")
    return Points(ids, xyz, rgb, err, start, pairs[:, 0].astype(np.uint32), pairs[:, 1].astype(np.uint32))


def read_model(d):
    return Model(read_cameras(os.path.join(d, "cameras.bin")),
                 read_images(os.path.join(d, "images.bin")),
                 read_points3D(os.path.join(d, "points3D.bin")))


# ----------------------------------------------------------------- write
def write_cameras(path, cams):
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(cams)))
        for cid in sorted(cams):
            c = cams[cid]
            f.write(struct.pack("<iiQQ", cid, c.model, c.width, c.height))
            f.write(np.asarray(c.params, "<f8").tobytes())


def write_images(path, imgs):
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(imgs)))
        for iid in sorted(imgs):
            im = imgs[iid]
            f.write(struct.pack("<I4d3dI", iid, *im.qvec, *im.tvec, im.camera_id))
            f.write(im.name.encode("utf-8") + b"\0")
            a = np.empty(len(im.xys), _P2D)
            a["x"], a["y"], a["id"] = im.xys[:, 0], im.xys[:, 1], im.point3D_ids
            f.write(struct.pack("<Q", len(a))); f.write(a.tobytes())


def write_points3D(path, P: Points):
    n = len(P)
    tl = P.track_len()
    head = np.empty(n, np.dtype([("id", "<u8"), ("xyz", "<f8", 3), ("rgb", "u1", 3),
                                  ("err", "<f8"), ("tl", "<u8")]))
    head["id"], head["xyz"], head["rgb"], head["err"], head["tl"] = P.ids, P.xyz, P.rgb, P.error, tl
    tr = np.empty((len(P.track_img), 2), "<u4"); tr[:, 0] = P.track_img; tr[:, 1] = P.track_idx
    hb = head.tobytes(); trb = tr.tobytes()
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", n))
        # interleave headers and tracks in chunks to bound memory
        step = 200_000
        for s in range(0, n, step):
            e = min(n, s + step)
            parts = []
            for i in range(s, e):
                parts.append(hb[51 * i:51 * i + 51])
                parts.append(trb[8 * P.track_start[i]:8 * P.track_start[i + 1]])
            f.write(b"".join(parts))


def write_model(d, model: Model):
    os.makedirs(d, exist_ok=True)
    write_cameras(os.path.join(d, "cameras.bin"), model.cameras)
    write_images(os.path.join(d, "images.bin"), model.images)
    write_points3D(os.path.join(d, "points3D.bin"), model.points)


def remove_points(model: Model, remove):
    """Delete tie points (bool mask) and unlink their observations in images."""
    remove = np.asarray(remove, bool)
    if not remove.any():
        return 0
    gone = np.sort(model.points.ids[remove].astype(np.int64))
    for im in model.images.values():
        ids = np.asarray(im.point3D_ids, np.int64)
        if len(ids):
            m = np.isin(ids, gone, assume_unique=False)
            if m.any():
                ids = ids.copy(); ids[m] = -1; im.point3D_ids = ids
    model.points = model.points.subset(np.flatnonzero(~remove))
    return int(remove.sum())


# --------------------------------------------------------------- helpers
def project(cam: Camera, R, t, X):
    """Project world points X (n,3) with world->camera R, t. Returns (uv, depth)."""
    Xc = X @ R.T + t
    z = Xc[:, 2]
    x = Xc[:, 0] / z; y = Xc[:, 1] / z
    xd, yd = distort(cam, x, y)
    fx, fy, cx, cy = cam.K()
    return np.stack([fx * xd + cx, fy * yd + cy], 1), z


def distort(cam: Camera, x, y):
    """Normalised undistorted -> distorted coordinates (COLMAP camera models:
    SIMPLE_RADIAL, RADIAL, OPENCV, OPENCV_FISHEYE and FULL_OPENCV incl. the
    rational k4..k6 denominator)."""
    p = cam.params
    if cam.model == 5:                                     # OPENCV_FISHEYE (equidistant)
        k1, k2, k3, k4 = (float(v) for v in p[4:8])
        r = np.sqrt(x * x + y * y)
        th = np.arctan(r)
        th2 = th * th
        thd = th * (1 + k1 * th2 + k2 * th2 ** 2 + k3 * th2 ** 3 + k4 * th2 ** 4)
        sc = np.where(r > 1e-12, thd / np.maximum(r, 1e-12), 1.0)
        return x * sc, y * sc
    k1, k2, p1, p2, k3 = cam.dist()
    r2 = x * x + y * y
    rad = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 ** 3
    if cam.model == 6 and len(p) >= 12 and (p[9] or p[10] or p[11]):
        rad = rad / (1 + p[9] * r2 + p[10] * r2 * r2 + p[11] * r2 ** 3)
    return (x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
            y * rad + 2 * p2 * x * y + p1 * (r2 + 2 * y * y))


def undistort_points(cam: Camera, uv, iters=10):
    """Pixel -> normalised undistorted (x, y) by fixed-point iteration."""
    fx, fy, cx, cy = cam.K()
    xd = (uv[:, 0] - cx) / fx; yd = (uv[:, 1] - cy) / fy
    x, y = xd.copy(), yd.copy()
    for _ in range(iters):
        dx, dy = distort(cam, x, y)
        x = x - (dx - xd); y = y - (dy - yd)
    return x, y


def observation_errors(model: Model):
    """Per-observation reprojection error (px), image id and point index."""
    P = model.points
    tl = P.track_len()
    pidx = np.repeat(np.arange(len(P)), tl)
    errs = np.empty(len(pidx)); imgs = P.track_img.astype(np.int64)
    for iid, im in model.images.items():
        sel = np.flatnonzero(imgs == iid)
        if not len(sel):
            continue
        uv, _ = project(model.cameras[im.camera_id], im.R(), im.tvec, P.xyz[pidx[sel]])
        errs[sel] = np.linalg.norm(uv - im.xys[P.track_idx[sel]], axis=1)
    return errs, imgs, pidx
