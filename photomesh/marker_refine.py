"""Automatic marker placement: predicted projections + local refinement.

Two refiners, both local (a few tens of pixels around the prediction):

* saddle_refine   - for checkerboard / "X" targets: strongest saddle point
                    (negative Hessian determinant) near the prediction,
                    sub-pixel by a quadratic fit.  Needs no template, so it
                    works when only marker coordinates are known.
* template_refine - one projection placed by the user is orthorectified onto
                    a local ground plane and matched (NCC) in the other photos,
                    the way Metashape's "Refine Markers" behaves.
"""
from __future__ import annotations

import os

import numpy as np

from . import colmap_io as cio


def load_gray(path, box=None):
    """Grayscale float32 image (optionally a crop box l,t,r,b) - read only."""
    from PIL import Image
    with Image.open(path) as im:
        if box is not None:
            im = im.crop(tuple(int(v) for v in box))
        return np.asarray(im.convert("L"), np.float32)


def saddle_refine(gray, uv, search=25, sigma=1.2, origin=(0, 0)):
    """Find the checkerboard corner nearest/strongest around uv.

    gray: image (or crop starting at origin). Returns (uv, score) or (None, 0).
    score = saddle strength relative to the window's median absolute response."""
    from scipy import ndimage
    # COLMAP/Metashape pixel coordinates: centre of the top-left pixel is (0.5, 0.5)
    x0, y0 = uv[0] - origin[0] - 0.5, uv[1] - origin[1] - 0.5
    r = int(search) + 6
    xa, ya = int(round(x0)) - r, int(round(y0)) - r
    if xa < 0 or ya < 0 or xa + 2 * r + 1 > gray.shape[1] or ya + 2 * r + 1 > gray.shape[0]:
        return None, 0.0
    w = gray[ya:ya + 2 * r + 1, xa:xa + 2 * r + 1].astype(np.float64)
    g = ndimage.gaussian_filter(w, sigma)
    gy, gx = np.gradient(g)
    gxy, gxx = np.gradient(gx)
    gyy, _ = np.gradient(gy)
    s = -(gxx * gyy - gxy * gxy)                     # >0 at saddle points
    s[:6, :] = s[-6:, :] = s[:, :6] = s[:, -6:] = 0
    yy, xx = np.mgrid[:s.shape[0], :s.shape[1]]
    d2 = (xx - (x0 - xa)) ** 2 + (yy - (y0 - ya)) ** 2
    prior = np.exp(-d2 / (2 * (0.6 * search) ** 2))  # prefer corners near the prediction
    sc = np.where(s > 0, s, 0) * prior
    j, i = np.unravel_index(np.argmax(sc), sc.shape)
    if sc[j, i] <= 0:
        return None, 0.0
    # sub-pixel: quadratic through 3x3 neighbourhood
    def peak(a, b, c):
        den = a - 2 * b + c
        return 0.0 if abs(den) < 1e-12 else float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    dx = peak(s[j, i - 1], s[j, i], s[j, i + 1]); dy = peak(s[j - 1, i], s[j, i], s[j + 1, i])
    noise = np.median(np.abs(s)) + 1e-9
    return (xa + i + dx + origin[0] + 0.5, ya + j + dy + origin[1] + 0.5), float(s[j, i] / noise)


def _ray(cam, R, t, uv):
    x, y = cio.undistort_points(cam, np.atleast_2d(np.asarray(uv, float)))
    d = np.stack([x, y, np.ones_like(x)], 1) @ R            # R^T [x y 1]
    C = -R.T @ t
    return C, d / np.linalg.norm(d, axis=1, keepdims=True)


def _plane_hit(C, d, X0, n):
    s = ((X0 - C) @ n) / (d @ n)
    return C + d * s[:, None]


def ortho_patch(gray, origin, cam, R, t, center, n, cell, half):
    """Sample the photo onto a (2*half+1)^2 grid on the plane (center, normal n)
    with spacing `cell` (m). Returns the patch and a validity mask."""
    from scipy import ndimage
    e1 = np.cross(n, [1.0, 0, 0] if abs(n[0]) < 0.9 else [0, 1.0, 0]); e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    k = np.arange(-half, half + 1) * cell
    P = center + k[None, :, None] * e1 + k[:, None, None] * e2           # (h, w, 3)
    uv, z = cio.project(cam, R, t, P.reshape(-1, 3))
    u = uv[:, 0] - origin[0] - 0.5; v = uv[:, 1] - origin[1] - 0.5
    ok = (z > 0) & (u >= 0) & (v >= 0) & (u < gray.shape[1] - 1) & (v < gray.shape[0] - 1)
    val = ndimage.map_coordinates(gray, [v, u], order=1, mode="nearest")
    sh = (2 * half + 1, 2 * half + 1)
    return val.reshape(sh), ok.reshape(sh), (e1, e2)


def _ncc_search(tpl, img):
    """Exhaustive NCC of tpl over img (valid positions). Returns score map."""
    from numpy.lib.stride_tricks import sliding_window_view
    t = tpl - tpl.mean(); tn = np.sqrt((t * t).sum()) + 1e-9
    W = sliding_window_view(img, tpl.shape)
    m = W.mean(axis=(2, 3), keepdims=True)
    Wc = W - m
    num = np.einsum("ijkl,kl->ij", Wc, t)
    den = np.sqrt(np.einsum("ijkl,ijkl->ij", Wc, Wc)) * tn + 1e-9
    return num / den


def template_refine(src, tgt, X, n=(0, 0, 1.0), cell=None, half=12, search=20):
    """src/tgt: dict(gray, origin, cam, R, t[, uv]). src['uv'] is the user's
    placement. Returns (uv in target, ncc) or (None, ncc)."""
    n = np.asarray(n, float); n /= np.linalg.norm(n)
    Cs, ds = _ray(src["cam"], src["R"], src["t"], src["uv"])
    P = _plane_hit(Cs, ds, np.asarray(X, float), n)[0]
    if cell is None:
        depth = np.linalg.norm(P - Cs)
        cell = depth / src["cam"].K()[0]                        # source ground sample distance
    tpl, okt, (e1, e2) = ortho_patch(src["gray"], src["origin"], src["cam"], src["R"], src["t"], P, n, cell, half)
    img, oki, _ = ortho_patch(tgt["gray"], tgt["origin"], tgt["cam"], tgt["R"], tgt["t"], P, n, cell,
                              half + search)
    if not okt.all() or oki.mean() < 0.9 or tpl.std() < 2:
        return None, 0.0
    sc = _ncc_search(tpl, img)
    j, i = np.unravel_index(np.argmax(sc), sc.shape)
    best = float(sc[j, i])

    def peak(a, b, c):
        den = a - 2 * b + c
        return 0.0 if abs(den) < 1e-12 else float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    dx = peak(sc[j, i - 1], sc[j, i], sc[j, i + 1]) if 0 < i < sc.shape[1] - 1 else 0.0
    dy = peak(sc[j - 1, i], sc[j, i], sc[j + 1, i]) if 0 < j < sc.shape[0] - 1 else 0.0
    sx, sy = (i + dx - search) * cell, (j + dy - search) * cell
    Q = P + sx * e1 + sy * e2
    uv, z = cio.project(tgt["cam"], tgt["R"], tgt["t"], Q[None])
    return (float(uv[0, 0]), float(uv[0, 1])), best


def view_info(model, iid, image_root, uv_center, radius):
    """Crop of a photo around uv_center (read-only) + its camera."""
    im = model.images[iid]
    cam = model.cameras[im.camera_id]
    path = image_root if os.path.isfile(image_root) else os.path.join(image_root, im.name)
    l, t = int(uv_center[0] - radius), int(uv_center[1] - radius)
    l = max(l, 0); t = max(t, 0)
    r = min(int(uv_center[0] + radius), cam.width); b = min(int(uv_center[1] + radius), cam.height)
    if r - l < 8 or b - t < 8:
        return None
    return {"gray": load_gray(path, (l, t, r, b)), "origin": (l, t), "cam": cam, "R": im.R(), "t": im.tvec}


def crop_views(model, image_root, requests, radius=200, workers=None):
    """Read many photo crops in parallel (each photo decoded once).
    requests: {iid: [(key, (u, v)), ...]} -> {key: view dict}."""
    from .fastpipe import pool_map
    jobs = []
    for iid, items in requests.items():
        im = model.images[iid]; cam = model.cameras[im.camera_id]
        path = image_root if os.path.isfile(image_root) else os.path.join(image_root, im.name)
        boxes = []
        for key, (u, v) in items:
            l, t = max(int(u - radius), 0), max(int(v - radius), 0)
            boxes.append((key, (l, t, min(int(u + radius), cam.width), min(int(v + radius), cam.height))))
        jobs.append((iid, path, boxes))
    out = {}
    for iid, crops in pool_map(_crop_worker, jobs, workers or max(1, min(12, (os.cpu_count() or 2) - 2))):
        im = model.images[iid]
        for key, origin, gray in crops:
            out[key] = {"gray": gray.astype(np.float32), "origin": origin,
                        "cam": model.cameras[im.camera_id], "R": im.R(), "t": im.tvec}
    return out


def _crop_worker(job):
    from PIL import Image
    iid, path, boxes = job
    with Image.open(path) as im:
        g = im.convert("L")
        return iid, [(key, box[:2], np.asarray(g.crop(box), np.uint8)) for key, box in boxes]


def auto_place(model, image_root, X, photos, pinned=None, method="auto", search=25, min_score=None,
               views=None, consensus=3.0):
    """Predict + refine marker X (model frame) in the given image ids.

    pinned: {iid: (u, v)} user placements used as templates (method template).
    method: 'predict' | 'saddle' | 'template' | 'auto' (template if a pin
    exists, otherwise saddle). views: optional callable iid -> prefetched crop
    (see crop_views). consensus: px tolerance of the multi-view check (0 = off).
    Returns {iid: (u, v, score, method_used)}."""
    from .georef import predict_projection
    out = {}
    src = None
    if pinned and method in ("auto", "template"):
        iid0 = min(pinned, key=lambda i: _obliquity(model, i, X))
        src = views(iid0) if views else view_info(model, iid0, image_root, pinned[iid0], 160)
        if src is not None:
            src["uv"] = pinned[iid0]
    for iid in photos:
        p = predict_projection(model, iid, X)
        if p is None:
            continue
        if method == "predict":
            out[iid] = (p[0], p[1], 0.0, "predict"); continue
        v = views(iid) if views else view_info(model, iid, image_root, p, 220)
        if v is None:
            continue
        if src is not None and method in ("auto", "template"):
            uv, sc = template_refine(src, v, X, search=search)
            ok = uv is not None and sc >= (0.6 if min_score is None else min_score) and \
                np.hypot(uv[0] - p[0], uv[1] - p[1]) <= 2.5 * search
            if ok:
                out[iid] = (uv[0], uv[1], sc, "template"); continue
            if method == "template":
                out[iid] = (p[0], p[1], 0.0, "predict"); continue
        uv, sc = saddle_refine(v["gray"], p, search=search, origin=v["origin"])
        if uv is not None and sc >= (8.0 if min_score is None else min_score):
            out[iid] = (uv[0], uv[1], sc, "saddle")
        else:
            out[iid] = (p[0], p[1], 0.0, "predict")
    if consensus:
        out = _consensus(model, out, X, tol=consensus)
    return out


def _consensus(model, out, X, tol=3.0):
    """Multi-view check of refined placements: triangulate the refined
    detections, drop the worst until all agree within tol px, and replace the
    rejected ones by the reprojection of the consensus point."""
    from .georef import triangulate_marker, predict_projection
    refined = {i: v for i, v in out.items() if v[3] in ("saddle", "template")}
    if len(refined) < 3:
        return out
    keep = dict(refined)
    while len(keep) >= 3:
        P, _ = triangulate_marker(model, {model.images[i].name: v[:2] for i, v in keep.items()})
        if P is None:
            return out
        err = {}
        for i, v in keep.items():
            q = predict_projection(model, i, P)
            err[i] = np.inf if q is None else float(np.hypot(q[0] - v[0], q[1] - v[1]))
        worst = max(err, key=err.get)
        if err[worst] <= tol:
            break
        del keep[worst]
    else:
        return out
    res = dict(out)
    for i, v in out.items():
        if i in keep:
            continue
        q = predict_projection(model, i, P)
        if q is not None:
            res[i] = (q[0], q[1], 0.0, "consensus")
    return res


def _obliquity(model, iid, X):
    im = model.images[iid]
    C = im.center(); R = im.R()
    d = np.asarray(X, float) - C
    return float(np.arccos(np.clip((R[2] @ d) / np.linalg.norm(d), -1, 1)))
