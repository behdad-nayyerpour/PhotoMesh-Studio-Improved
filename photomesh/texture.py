"""Build Texture: photo-based texture atlas for a triangle mesh.

Own implementation (numpy + scipy + Open3D's Embree ray caster), no third-party
texturing code:

1. Cameras: the chunk's current sparse model (same frame as the mesh - after
   Optimize Cameras both are in the georeferenced frame), original photos read
   at a working resolution (JPEG DCT down-scaling, e.g. 5280 -> 2640 px), full
   camera model incl. radial/tangential distortion, any number of sensors.
2. Visibility: for every camera, faces in its frustum that face it are tested
   with rays from the camera centre to the face centroid (+3 inner points);
   Embree tells whether something is in front. Score per (face, view) =
   projected area in working-resolution pixels x cos(view angle) x border
   weight x image sharpness. The best K views per face are kept.
3. Colour statistics from 1/8-scale images: per-view exposure gains (global
   least squares in log space over all face/view pairs), ghosting filter
   (views whose colour of a face differs strongly from the median of all views
   are dropped - moving cars, people), hole filling for faces no photo sees.
4. View selection (Mosaic): data term = score, smoothness = Potts on face
   adjacency, solved with a few ICM sweeps -> large single-photo patches.
5. Seam levelling: colour offsets per (vertex, view) copy that pull both sides
   of every seam to their mean; smoothly diffused inside each patch (sparse
   linear solve), so seams vanish without blurring the detail.
6. UV atlas: Generic = one chart per connected single-photo patch, projected
   with that photo's camera (image-space charts, no distortion of the photo
   texture); Orthophoto = top-down projection in tiles; Adaptive orthophoto =
   horizontal faces orthographic, steep faces Generic; Keep uv = previous
   atlas. Charts are scaled to fit and shelf-packed into the pages.
7. Texture pages are filled texel by texel: the 3D point of every texel is
   projected into its photo(s) and sampled bilinearly (Mosaic: one photo +
   gain + seam offset; Average / Max / Min intensity: all valid photos;
   Disabled: best photo, no colour correction). Chart borders are dilated so
   mip-maps/bilinear filtering show no seams.
Outputs: OBJ + MTL + JPG/PNG pages, uv.npz (per-corner uv + page, used by the
viewer, tiling and 'Keep uv'), info.json.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import colmap_io as cio

MAPPING = ("Generic", "Adaptive orthophoto", "Orthophoto", "Keep uv")
BLENDING = ("Mosaic", "Average", "Max intensity", "Min intensity", "Disabled")
DEFAULTS = {"mapping": "Generic", "blending": "Mosaic", "size": 8192, "count": 1, "hole_filling": True,
            "ghosting": True, "max_res": 2640, "format": "jpg", "jpeg_quality": 92, "top_k": 10,
            "vis_samples": 4, "smooth": 1.5, "threads": 0, "padding": 2, "min_chart_faces": 40,
            "ghost_threshold": 45.0}


SAMPLE_CHUNK = 1_000_000   # texels projected at once per photo (bounds float64 temporaries)


# ------------------------------------------------------------------ images
def load_image(path, max_side=0):
    """RGB uint8 array, decoded at reduced size when max_side is set (JPEG DCT
    scaling: no full-size decode)."""
    from PIL import Image
    with Image.open(path) as im:
        W, H = im.size
        if max_side and max(W, H) > max_side:
            s = max_side / max(W, H)
            tw, th = max(1, int(round(W * s))), max(1, int(round(H * s)))
            im.draft("RGB", (tw, th))
            im = im.convert("RGB")
            if im.size[0] > tw * 1.3:
                im = im.resize((tw, th), Image.BILINEAR)
        else:
            im = im.convert("RGB")
        return np.asarray(im)


def bilinear(img, x, y):
    """Sample img (H,W,3) at continuous pixel coords (x, y) where (0,0) is the
    top-left corner of the first pixel. Returns float32 (n,3)."""
    H, W = img.shape[:2]
    x = np.clip(x - 0.5, 0, W - 1.001); y = np.clip(y - 0.5, 0, H - 1.001)
    x0 = x.astype(np.int32); y0 = y.astype(np.int32)
    fx = (x - x0).astype(np.float32)[:, None]; fy = (y - y0).astype(np.float32)[:, None]
    x1 = np.minimum(x0 + 1, W - 1); y1 = np.minimum(y0 + 1, H - 1)
    a = img[y0, x0].astype(np.float32); b = img[y0, x1].astype(np.float32)
    c = img[y1, x0].astype(np.float32); d = img[y1, x1].astype(np.float32)
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def sharpness(img):
    g = img.astype(np.float32).mean(2)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


# ------------------------------------------------------------------ cameras
class View:
    def __init__(self, idx, name, path, cam, R, t):
        self.idx, self.name, self.path, self.cam = idx, name, path, cam
        self.R = np.asarray(R, np.float64); self.t = np.asarray(t, np.float64)
        self.C = -self.R.T @ self.t
        self.W, self.H = cam.width, cam.height
        self.small = None; self.sharp = 1.0

    def project(self, X):
        """(u, v) full-resolution pixel coords + depth."""
        uv, z = cio.project(self.cam, self.R, self.t, X)
        return uv, z


def load_views(model_dir, photos, log=print):
    cams = cio.read_cameras(os.path.join(model_dir, "cameras.bin"))
    imgs = cio.read_images(os.path.join(model_dir, "images.bin"))
    # fallback for names given with a folder: only file names that are unique
    base_n = Counter(os.path.basename(k).lower() for k in photos)
    by_base = {os.path.basename(k).lower(): v for k, v in photos.items() if base_n[os.path.basename(k).lower()] == 1}
    views, missing = [], []
    for iid in sorted(imgs):
        im = imgs[iid]
        p = photos.get(im.name) or by_base.get(os.path.basename(im.name).lower())
        if not p or not os.path.exists(p):
            missing.append(im.name); continue
        views.append(View(len(views), im.name, p, cams[im.camera_id], im.R(), im.tvec))
    if missing:
        log(f"Texture: {len(missing)} aligned photos not found on disk (skipped), e.g. {missing[0]}")
    sensors = {(v.cam.width, v.cam.height, v.cam.model) for v in views}
    log(f"Texture: {len(views)} cameras, {len(sensors)} sensor(s): "
        + ", ".join(f"{w}x{h} {cio.CAMERA_MODELS[m][0]}" for w, h, m in sorted(sensors)))
    return views


# ------------------------------------------------------------------ mesh helpers
def read_mesh(path):
    from .plyio import read_fast, load_ply
    r = read_fast(path)
    if r is not None and r["face"] is not None:
        v = r["vertex"]
        V = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)
        return V, r["face"].astype(np.int64)
    if path.lower().endswith(".obj"):
        V, F, _uv, _p = read_obj(path)
        return V, F
    m = load_ply(path, max_points=0)
    return m["points"].astype(np.float64), np.asarray(m["faces"], np.int64)


def face_adjacency(F):
    """(m,3) neighbour face across edge k (v_k, v_k+1), -1 at borders."""
    m = len(F)
    a = F[:, [0, 1, 2]].ravel(); b = F[:, [1, 2, 0]].ravel()
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    key = lo * (int(F.max()) + 1) + hi
    order = np.argsort(key, kind="stable")
    ks = key[order]
    same = np.flatnonzero(ks[1:] == ks[:-1])
    nb = np.full(3 * m, -1, np.int64)
    i, j = order[same], order[same + 1]
    nb[i] = j // 3; nb[j] = i // 3
    return nb.reshape(m, 3)


def up_vector(views, georef):
    if georef:
        return np.array([0.0, 0.0, 1.0])
    d = np.mean([v.R[2] for v in views], 0)       # mean viewing direction (world)
    n = np.linalg.norm(d)
    return -d / n if n > 0 else np.array([0.0, 0.0, 1.0])


def plane_basis(up):
    up = up / np.linalg.norm(up)
    a = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, a); e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    return e1, -e2            # x right, y "down" in the texture (north up when up=z)


# ------------------------------------------------------------------ visibility
class Visibility:
    def __init__(self, V, F, log=print):
        import open3d as o3d
        self.o3d = o3d
        self.V, self.F = V, F
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
        P = V[F]                                    # (m,3,3)
        self.cent = P.mean(1)
        n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
        self.area2 = np.linalg.norm(n, axis=1)
        self.N = n / np.maximum(self.area2, 1e-30)[:, None]
        e = np.linalg.norm(P[:, 1] - P[:, 0], axis=1)
        self.edge = float(np.median(e)) if len(e) else 1.0
        self.P = P

    def cast(self, origins, targets):
        d = targets - origins
        dist = np.linalg.norm(d, axis=1)
        rays = np.concatenate([origins, d / np.maximum(dist, 1e-30)[:, None]], 1).astype(np.float32)
        r = self.scene.cast_rays(self.o3d.core.Tensor(rays))
        return r["t_hit"].numpy(), dist

    def orient(self, views):
        """+1 if face normals point towards the cameras (outwards), -1 if flipped."""
        votes = 0
        for v in views[:: max(1, len(views) // 8)]:
            d = self.cent - v.C
            uv, z = v.project(self.cent)
            ok = (z > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < v.W) & (uv[:, 1] >= 0) & (uv[:, 1] < v.H)
            idx = np.flatnonzero(ok)
            if len(idx) > 20000:
                idx = np.random.default_rng(0).choice(idx, 20000, replace=False)
            if not len(idx):
                continue
            th, dist = self.cast(np.repeat(v.C[None], len(idx), 0), self.cent[idx])
            vis = th >= dist - 0.5 * self.edge
            s = np.einsum("ij,ij->i", self.N[idx[vis]], d[idx[vis]])
            votes += int((s < 0).sum() - (s > 0).sum())
        return 1.0 if votes >= 0 else -1.0


def _cells(cent, up, n=48):
    e1, e2 = plane_basis(up)
    x, y = cent @ e1, cent @ e2
    gx = np.clip(((x - x.min()) / max(np.ptp(x), 1e-9) * n).astype(np.int64), 0, n - 1)
    gy = np.clip(((y - y.min()) / max(np.ptp(y), 1e-9) * n).astype(np.int64), 0, n - 1)
    cid = gx * n + gy
    order = np.argsort(cid, kind="stable")
    counts = np.bincount(cid, minlength=n * n)
    starts = np.concatenate([[0], np.cumsum(counts)])
    centres = np.zeros((n * n, 3)); radius = np.zeros(n * n)
    for c in np.flatnonzero(counts):
        pts = cent[order[starts[c]:starts[c + 1]]]
        lo, hi = pts.min(0), pts.max(0)
        centres[c] = (lo + hi) / 2; radius[c] = np.linalg.norm(hi - lo) / 2
    return order, starts, counts, centres, radius


def view_scores(vis, views, up, cfg, log=print, progress=None, cancel=None):
    """Top-K (score, view) per face."""
    K = int(cfg.get("top_k", 6)); m = len(vis.F)
    best_s = np.zeros((m, K), np.float32); best_v = np.full((m, K), -1, np.int32)
    sign = vis.orient(views)
    if sign < 0:
        log("Texture: mesh normals point inwards - using flipped orientation")
    N = vis.N * sign
    order, starts, counts, centres, radius = _cells(vis.cent, up)
    live = np.flatnonzero(counts)
    nsamp = int(cfg.get("vis_samples", 4))
    mr = cfg.get("max_res") or 0
    t0 = time.perf_counter()
    lock = threading.Lock()
    stat = {"rays": 0, "done": 0}

    def one(v):
        if cancel and cancel():
            raise KeyboardInterrupt
        s_work = min(1.0, mr / max(v.W, v.H)) if mr else 1.0
        # cells whose bounding sphere may project into the image
        uvc, zc = v.project(centres[live])
        f = max(v.cam.K()[0], v.cam.K()[1])
        rpx = radius[live] * f / np.maximum(zc, 1e-9)
        okc = (zc + radius[live] > 0) & (uvc[:, 0] > -rpx) & (uvc[:, 0] < v.W + rpx) & \
              (uvc[:, 1] > -rpx) & (uvc[:, 1] < v.H + rpx)
        cs = live[okc]
        if not len(cs):
            return
        idx = np.concatenate([order[starts[c]:starts[c + 1]] for c in cs])
        d = vis.cent[idx] - v.C
        front = np.einsum("ij,ij->i", N[idx], d) < 0
        idx = idx[front]; d = d[front]
        uv, z = v.project(vis.cent[idx])
        ok = (z > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < v.W) & (uv[:, 1] >= 0) & (uv[:, 1] < v.H)
        idx, d, uv = idx[ok], d[ok], uv[ok]
        if not len(idx):
            return
        # occlusion: centroid ray first, then 3 inner points of the faces whose centroid is visible
        nr = len(idx)
        th, dist = vis.cast(np.repeat(v.C[None], len(idx), 0), vis.cent[idx])
        keep = th >= dist - 0.3 * vis.edge - 1e-6 * dist
        idx, d, uv = idx[keep], d[keep], uv[keep]
        if nsamp > 1 and len(idx):
            cnt = np.zeros(len(idx), np.int32)
            for k in range(3):
                tgt = 0.35 * vis.cent[idx] + 0.65 * vis.P[idx, k]
                th, dist = vis.cast(np.repeat(v.C[None], len(idx), 0), tgt)
                cnt += (th >= dist - 0.3 * vis.edge - 1e-6 * dist)
            nr += 3 * len(idx)
            keep = cnt >= 2
            idx, d, uv = idx[keep], d[keep], uv[keep]
        if not len(idx):
            return
        # projected area (working-res px) x cos x border weight x sharpness
        P = vis.P[idx]
        q = np.stack([v.project(P[:, k])[0] for k in range(3)], 1) * s_work
        area = 0.5 * np.abs((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) -
                            (q[:, 2, 0] - q[:, 0, 0]) * (q[:, 1, 1] - q[:, 0, 1]))
        dist = np.linalg.norm(d, axis=1)
        cos = -np.einsum("ij,ij->i", N[idx], d) / np.maximum(dist, 1e-30)
        r = np.hypot((uv[:, 0] - v.W / 2) / (v.W / 2), (uv[:, 1] - v.H / 2) / (v.H / 2)) / math.sqrt(2)
        s = (area * cos * (1 - 0.35 * r ** 4) * v.sharp).astype(np.float32)
        with lock:
            cur = best_s[idx]
            mn = cur.argmin(1)
            better = s > cur[np.arange(len(idx)), mn]
            sel = idx[better]; slot = mn[better]
            best_s[sel, slot] = s[better]; best_v[sel, slot] = v.idx
            stat["rays"] += nr; stat["done"] += 1
            if progress:
                progress(stat["done"], len(views))

    with ThreadPoolExecutor(int(cfg.get("vis_threads") or 3)) as ex:
        list(ex.map(one, views))
    rays = stat["rays"]
    dt = time.perf_counter() - t0
    seen = (best_v >= 0).any(1)
    log(f"Texture: visibility {rays / 1e6:.1f} M rays in {dt:.1f} s; {seen.mean() * 100:.2f}% of faces seen, "
        f"median {int(np.median((best_v >= 0).sum(1)))} candidate views per face (top {K})")
    return best_s, best_v, sign


# ------------------------------------------------------------------ colour statistics
def sample_small(views, X_by_view, threads):
    """{view: (n,3) float colours} sampling the 1/8-scale images."""
    out = {}
    def one(item):
        vi, X = item
        v = views[vi]
        uv, _z = v.project(X)
        sh, sw = v.small.shape[:2]
        return vi, bilinear(v.small, uv[:, 0] * sw / v.W, uv[:, 1] * sh / v.H)
    with ThreadPoolExecutor(threads) as ex:
        for vi, c in ex.map(one, X_by_view.items()):
            out[vi] = c
    return out


def candidate_colours(vis, views, best_v, threads):
    m, K = best_v.shape
    col = np.zeros((m, K, 3), np.float32)
    flat = best_v.ravel()
    order = np.argsort(flat, kind="stable")
    fs = flat[order]
    bounds = np.searchsorted(fs, np.arange(len(views) + 1))
    items = {}
    for vi in range(len(views)):
        sl = order[bounds[vi]:bounds[vi + 1]]
        if len(sl):
            items[vi] = sl
    res = sample_small(views, {vi: vis.cent[sl // K] for vi, sl in items.items()}, threads)
    cf = col.reshape(-1, 3)
    for vi, sl in items.items():
        cf[sl] = res[vi]
    return col


def exposure_gains(best_v, col, nviews, log=print, max_pairs=3_000_000):
    """Per-view log gains g (nviews,3): log c_a + g_a = log c_b + g_b."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr
    m, K = best_v.shape
    L = np.log(np.clip(col, 1, 255))
    good = (best_v >= 0) & (col.min(2) > 8) & (col.max(2) < 248)
    rows_a, rows_b, diff = [], [], []
    for k in range(1, K):
        ok = good[:, 0] & good[:, k]
        rows_a.append(best_v[ok, 0]); rows_b.append(best_v[ok, k]); diff.append(L[ok, k] - L[ok, 0])
    a = np.concatenate(rows_a); b = np.concatenate(rows_b); dd = np.concatenate(diff)
    if len(a) > max_pairs:
        pick = np.random.default_rng(1).choice(len(a), max_pairs, replace=False)
        a, b, dd = a[pick], b[pick], dd[pick]
    g = np.zeros((nviews, 3))
    if len(a) < 10:
        return g
    n = len(a); lam = 0.05 * math.sqrt(n / max(nviews, 1))
    r = np.concatenate([np.arange(n), np.arange(n), n + np.arange(nviews)])
    c = np.concatenate([a, b, np.arange(nviews)])
    w = np.concatenate([np.ones(n), -np.ones(n), np.full(nviews, lam)])
    A = coo_matrix((w, (r, c)), shape=(n + nviews, nviews)).tocsr()
    for ch in range(3):
        # g_a - g_b = L_b - L_a
        rhs = np.concatenate([dd[:, ch], np.zeros(nviews)])
        g[:, ch] = lsqr(A, rhs, atol=1e-8, btol=1e-8, iter_lim=500)[0]
    g -= np.median(g, 0)
    log(f"Texture: exposure gains from {n:,} face pairs: range x{np.exp(g.min()):.2f} .. x{np.exp(g.max()):.2f}")
    return np.clip(g, -1.0, 1.0)


def ghost_filter(best_s, best_v, colg, log=print, thresh=45.0):
    """Drop (face, view) pairs whose colour is an outlier among the face's
    views: |c - median| > thresh (0..255, mean over RGB) and > 3x the face's
    median deviation. Needs >= 4 views (the median must be a majority)."""
    valid = best_v >= 0
    nvalid = valid.sum(1)
    sel = nvalid >= 4
    c = np.where(valid[sel][..., None], colg[sel], np.nan)
    med = np.nanmedian(c, axis=1)
    dev = np.abs(c - med[:, None, :]).mean(2)
    mdev = np.nanmedian(dev, axis=1)
    sub = (dev > thresh) & (dev > 3 * mdev[:, None] + 1e-6) & valid[sel]
    bad = np.zeros_like(valid)
    bad[sel] = sub
    best_s[bad] = 0; best_v[bad] = -1
    log(f"Texture: ghosting filter removed {int(bad.sum()):,} face/photo pairs "
        f"({(bad.any(1)).mean() * 100:.2f}% of faces affected)")
    return int(bad.sum())


def select_labels(best_s, best_v, nb, smooth=0.6, iters=8, log=print):
    m, K = best_v.shape
    valid = best_v >= 0
    smax = np.where(valid, best_s, 0).max(1)
    data = np.where(valid, 1.0 - best_s / np.maximum(smax, 1e-30)[:, None], np.inf).astype(np.float32)
    lab_k = np.argmin(data, 1)
    has = valid.any(1)
    L = np.where(has, best_v[np.arange(m), lab_k], -1)
    if smooth <= 0:
        return L
    rng = np.random.default_rng(0)
    deg = (nb >= 0).sum(1)
    for it in range(iters):
        nl = np.where(nb >= 0, L[np.maximum(nb, 0)], -2)
        cost = np.empty((m, K), np.float32)
        for k in range(K):
            same = (nl == best_v[:, k:k + 1]).sum(1)
            cost[:, k] = data[:, k] + smooth * (deg - same)
        newk = np.argmin(cost, 1)
        newL = np.where(has, best_v[np.arange(m), newk], -1)
        upd = rng.random(m) < 0.5
        changed = int(((newL != L) & upd).sum())
        L = np.where(upd, newL, L)
        if changed < m * 1e-4:
            break
    seams = int(((nb >= 0) & (L[np.maximum(nb, 0)] != L[:, None])).sum() // 2)
    log(f"Texture: view selection (ICM) - {seams:,} seam edges before merging small patches")
    return L


def merge_small(L, best_v, nb, min_faces=40, iters=6, log=print):
    """Re-label faces of tiny single-photo patches with the most common
    neighbouring photo that also sees them -> fewer, larger charts."""
    m = len(L)
    for it in range(iters):
        comp = charts_from_labels(np.arange(m), L, nb)
        size = np.bincount(comp)[comp]
        small = np.flatnonzero((size < min_faces) & (L >= 0))
        if not len(small):
            break
        nl = L[np.maximum(nb[small], 0)]
        nl = np.where(nb[small] >= 0, nl, -1)
        ns = np.where(nb[small] >= 0, size[np.maximum(nb[small], 0)], 0)
        # candidate: neighbour label with the largest patch, among this face's valid views
        ok = (nl >= 0) & (nl != L[small][:, None]) & \
             (nl[:, :, None] == best_v[small][:, None, :]).any(2)
        ns = np.where(ok, ns, -1)
        k = np.argmax(ns, 1)
        ch = ns[np.arange(len(small)), k] > size[small]
        if not ch.any():
            break
        L = L.copy(); L[small[ch]] = nl[np.arange(len(small)), k][ch]
    comp = charts_from_labels(np.arange(m), L, nb)
    seams = int(((nb >= 0) & (L[np.maximum(nb, 0)] != L[:, None])).sum() // 2)
    log(f"Texture: view selection - {len(np.unique(L[L >= 0])):,} photos used, {comp.max() + 1:,} patches, {seams:,} seam edges")
    return L


def seam_offsets(V, F, L, views, gains, threads, log=print):
    """Per-corner colour offsets (m,3,3) that level the seams."""
    from scipy.sparse import coo_matrix, diags
    from scipy.sparse.linalg import cg
    m = len(F)
    lab = np.repeat(L, 3); vid = F.ravel()
    good = lab >= 0
    key = vid.astype(np.int64) * (len(views) + 1) + (lab + 1)
    uk, cid = np.unique(key, return_inverse=True)
    nc = len(uk)
    cv = uk // (len(views) + 1); cl = uk % (len(views) + 1) - 1
    col = np.zeros((nc, 3), np.float32)
    by_view = {}
    order = np.argsort(cl, kind="stable")
    bounds = np.searchsorted(cl[order], np.arange(len(views) + 1))
    for vi in range(len(views)):
        sl = order[bounds[vi]:bounds[vi + 1]]
        if len(sl):
            by_view[vi] = sl
    res = sample_small(views, {vi: V[cv[sl]] for vi, sl in by_view.items()}, threads)
    for vi, sl in by_view.items():
        col[sl] = res[vi] * np.exp(gains[vi]).astype(np.float32)
    real = cl >= 0
    # mean colour of each vertex over its copies
    sums = np.zeros((len(V), 3)); cnt = np.bincount(cv[real], minlength=len(V)).astype(np.float64)
    for ch in range(3):
        sums[:, ch] = np.bincount(cv[real], weights=col[real, ch], minlength=len(V))
    mean = sums / np.maximum(cnt, 1)[:, None]
    seam = real & (cnt[cv] > 1)
    target = np.where(seam[:, None], mean[cv] - col, 0.0)
    target = np.clip(target, -60, 60)
    # graph between copies along face edges
    c3 = cid.reshape(m, 3)
    ea = np.concatenate([c3[:, 0], c3[:, 1], c3[:, 2]]); eb = np.concatenate([c3[:, 1], c3[:, 2], c3[:, 0]])
    A = coo_matrix((np.ones(len(ea) * 2), (np.concatenate([ea, eb]), np.concatenate([eb, ea]))), shape=(nc, nc)).tocsr()
    A.data[:] = 1.0
    deg = np.asarray(A.sum(1)).ravel()
    w = np.where(seam, 10.0, 0.0) + 1e-3
    M = diags(deg + w) - A
    off = np.zeros((nc, 3), np.float32)
    Dinv = 1.0 / (deg + w)
    from scipy.sparse.linalg import LinearOperator
    pre = LinearOperator((nc, nc), matvec=lambda x: Dinv * x)
    for ch in range(3):
        sol, _info = cg(M, w * target[:, ch], rtol=1e-4, maxiter=400, M=pre)
        off[:, ch] = sol
    off[~real] = 0
    log(f"Texture: seam levelling over {int(seam.sum()):,} seam vertex copies "
        f"(mean |offset| {np.abs(off[real]).mean():.1f} / 255)")
    return off[cid].reshape(m, 3, 3)


def fill_holes(V, F, L, nb, vis, views, gains, best_v, threads, enabled, log=print):
    """Vertex colours for faces with no photo: diffused from textured neighbours."""
    holes = L < 0
    if not holes.any():
        return None
    if not enabled:
        log(f"Texture: {int(holes.sum()):,} faces not seen by any photo (hole filling off: grey)")
        return np.full((len(V), 3), 128.0, np.float32)
    tex = ~holes
    col = np.zeros((len(F), 3), np.float32)
    by_view = {}
    idx = np.flatnonzero(tex)
    order = idx[np.argsort(L[idx], kind="stable")]
    lv = L[order]
    bounds = np.searchsorted(lv, np.arange(len(views) + 1))
    for vi in range(len(views)):
        sl = order[bounds[vi]:bounds[vi + 1]]
        if len(sl):
            by_view[vi] = sl
    res = sample_small(views, {vi: vis.cent[sl] for vi, sl in by_view.items()}, threads)
    for vi, sl in by_view.items():
        col[sl] = res[vi] * np.exp(gains[vi]).astype(np.float32)
    vc = np.zeros((len(V), 3)); vn = np.zeros(len(V))
    for k in range(3):
        np.add.at(vn, F[tex, k], 1.0)
        for ch in range(3):
            vc[:, ch] += np.bincount(F[tex, k], weights=col[tex, ch], minlength=len(V))
    known = vn > 0
    vc[known] /= vn[known, None]
    # diffuse into the holes over the vertices of hole faces
    hf = F[holes]
    for _ in range(200):
        unknown = ~known
        if not unknown[hf].any():
            break
        s = np.zeros((len(V), 3)); n = np.zeros(len(V))
        for a in range(3):
            for b in range(3):
                if a == b:
                    continue
                src = hf[:, b]; dst = hf[:, a]
                ok = known[src]
                np.add.at(n, dst[ok], 1.0)
                for ch in range(3):
                    s[:, ch] += np.bincount(dst[ok], weights=vc[src[ok], ch], minlength=len(V))
        newk = unknown & (n > 0)
        vc[newk] = s[newk] / n[newk, None]
        known = known | newk
        if not newk.any():
            break
    vc[~known] = 128
    log(f"Texture: hole filling for {int(holes.sum()):,} faces not seen by any photo")
    return vc.astype(np.float32)


# ------------------------------------------------------------------ charts / atlas
def generic_uv(V, F, L, views, faces, s_work):
    """Per-corner uv (len(faces),3,2) in working-res pixels of the face's photo."""
    uv = np.zeros((len(faces), 3, 2), np.float64)
    lab = L[faces]
    order = np.argsort(lab, kind="stable")
    bounds = np.searchsorted(lab[order], np.arange(len(views) + 1))
    for vi in range(len(views)):
        sl = order[bounds[vi]:bounds[vi + 1]]
        if not len(sl):
            continue
        v = views[vi]
        for k in range(3):
            uv[sl, k] = v.project(V[F[faces[sl], k]])[0] * s_work[vi]
    return uv


def planar_uv(V, F, faces, e1, e2, px_per_unit):
    P = V[F[faces]]                                   # (n,3,3)
    return np.stack([P @ e1, P @ e2], -1) * px_per_unit


def charts_from_labels(faces, lab, nb):
    """Connected components of `faces` with equal label (face adjacency)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    m = len(nb)
    pos = np.full(m, -1, np.int64); pos[faces] = np.arange(len(faces))
    a = np.repeat(np.arange(len(faces)), 3); b = nb[faces].ravel()
    ok = b >= 0
    a, b = a[ok], b[ok]
    pb = pos[b]
    ok = pb >= 0
    ok[ok] = lab[faces[a[ok]]] == lab[b[ok]]
    G = coo_matrix((np.ones(int(ok.sum())), (a[ok], pb[ok])), shape=(len(faces), len(faces)))
    _n, comp = connected_components(G, directed=False)
    return comp


def grid_charts(uv, cell):
    c = uv.mean(1)
    gx = np.floor(c[:, 0] / cell).astype(np.int64); gy = np.floor(c[:, 1] / cell).astype(np.int64)
    _u, comp = np.unique(gx * 1_000_003 + gy, return_inverse=True)
    return comp


def orient_charts(uv, comp):
    """Rotate every chart so its principal axis is horizontal (tighter
    bounding boxes -> more texels for the model)."""
    c = uv.reshape(-1, 2); cc = np.repeat(comp, 3)
    n = np.bincount(cc).astype(np.float64)
    mx = np.bincount(cc, c[:, 0]) / np.maximum(n, 1); my = np.bincount(cc, c[:, 1]) / np.maximum(n, 1)
    dx = c[:, 0] - mx[cc]; dy = c[:, 1] - my[cc]
    sxx = np.bincount(cc, dx * dx); syy = np.bincount(cc, dy * dy); sxy = np.bincount(cc, dx * dy)
    a = 0.5 * np.arctan2(2 * sxy, sxx - syy)
    ca, sa = np.cos(-a)[cc], np.sin(-a)[cc]
    out = np.stack([dx * ca - dy * sa, dx * sa + dy * ca], 1)
    return out.reshape(uv.shape)


def split_large(uv, comp, limit):
    """Split charts whose bbox exceeds `limit` px (at scale 1) along the long axis."""
    comp = comp.copy()
    nxt = comp.max() + 1 if len(comp) else 0
    for _round in range(40):
        order = np.argsort(comp, kind="stable")
        cs = comp[order]
        b = np.flatnonzero(np.diff(cs)) + 1
        starts = np.concatenate([[0], b]); ends = np.concatenate([b, [len(cs)]])
        lo = np.minimum.reduceat(uv.min(1)[order], starts); hi = np.maximum.reduceat(uv.max(1)[order], starts)
        ext = hi - lo
        big = np.flatnonzero((ext > limit).any(1))
        if not len(big):
            break
        for ci in big:
            sl = order[starts[ci]:ends[ci]]
            ax = int(np.argmax(ext[ci]))
            c = uv[sl].mean(1)[:, ax]
            half = sl[c > np.median(c)]
            if len(half) == 0 or len(half) == len(sl):
                half = sl[: len(sl) // 2]
            comp[half] = nxt; nxt += 1
    return comp


def pack(uv, comp, size, count, pad, scale_max, log=print):
    """Shelf-pack charts into `count` pages of size^2. Returns (page (n,), uv_px (n,3,2), scale)."""
    order = np.argsort(comp, kind="stable")
    cs = comp[order]
    b = np.flatnonzero(np.diff(cs)) + 1
    starts = np.concatenate([[0], b]); ends = np.concatenate([b, [len(cs)]])
    lo = np.minimum.reduceat(uv.min(1)[order], starts); hi = np.maximum.reduceat(uv.max(1)[order], starts)
    ext = hi - lo
    area = float(((ext[:, 0] + 1) * (ext[:, 1] + 1)).sum())
    s = min(scale_max, math.sqrt(0.72 * count * size * size / max(area, 1.0)))
    s = min(s, (size - 2 * pad - 2) / max(float(ext.max()), 1e-9))
    for attempt in range(60):
        wh = np.ceil(ext * s).astype(np.int64) + 1 + 2 * pad
        o = np.lexsort((-wh[:, 0], -wh[:, 1]))
        pos = np.zeros((len(wh), 2), np.int64); pg = np.zeros(len(wh), np.int64)
        page, x, y, shelf = 0, 0, 0, 0
        ok = True
        for i in o:
            w, h = wh[i]
            if x + w > size:
                x = 0; y += shelf; shelf = 0
            if y + h > size:
                page += 1; x = 0; y = 0; shelf = 0
                if page >= count:
                    ok = False; break
            pos[i] = (x, y); pg[i] = page
            x += w; shelf = max(shelf, h)
        if ok:
            break
        s *= 0.94
    else:
        raise RuntimeError("texture atlas packing failed")
    cidx = np.empty(len(comp), np.int64)
    for ci in range(len(starts)):
        cidx[order[starts[ci]:ends[ci]]] = ci
    uvp = (uv - lo[cidx][:, None, :]) * s + (pos[cidx] + pad + 0.5)[:, None, :]
    used = float((wh[:, 0] * wh[:, 1]).sum()) / (count * size * size)
    log(f"Texture: {len(starts):,} charts packed into {pg.max() + 1} page(s) {size}x{size}, "
        f"scale {s:.3f} of working resolution, {used * 100:.0f}% of the atlas used")
    return pg[cidx], uvp.astype(np.float32), s


def pack_fit(uv, comp, size, count, pad, log=print):
    """pack() that does not give up when the charts' fixed borders alone do not
    fit (many tiny charts, e.g. a rough drone mesh): retry with more pages (up to
    8x), then with a 1 px border."""
    tries = [(c, pad) for c in (count, count * 2, count * 4, count * 8)] + [(count * 8, 1)]
    for c, p in tries:
        try:
            r = pack(uv, comp, size, c, p, 1.0, log)
        except RuntimeError:
            continue
        if c != count or p != pad:
            log(f"Texture: {count} page(s) were not enough for {int(comp.max()) + 1:,} charts - "
                f"using up to {c} page(s){', 1 px border' if p != pad else ''}")
        return r
    raise RuntimeError(f"texture atlas packing failed: {int(comp.max()) + 1:,} charts do not fit "
                       f"in {count * 8} pages of {size}x{size} - use a larger texture size")


def rasterize(uvp, page, npages, size):
    """Texels covered by each triangle: (page, y, x, face, b1, b2) arrays."""
    out = []
    lo = np.floor(uvp.min(1) - 0.5).astype(np.int64); hi = np.ceil(uvp.max(1) + 0.5).astype(np.int64)
    ext = (hi - lo).max(1)
    classes = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384]
    cls = np.searchsorted(classes, ext)
    for ci, S in enumerate(classes):
        fsel = np.flatnonzero(cls == ci)
        if not len(fsel):
            continue
        batch = max(1, int(8_000_000 // (S * S)))
        gy, gx = np.mgrid[0:S, 0:S]
        gx = gx.ravel(); gy = gy.ravel()
        for s0 in range(0, len(fsel), batch):
            fs = fsel[s0:s0 + batch]
            X = lo[fs, 0][:, None] + gx[None]; Y = lo[fs, 1][:, None] + gy[None]
            px = X + 0.5; py = Y + 0.5
            a, b, c = uvp[fs, 0], uvp[fs, 1], uvp[fs, 2]
            v0 = b - a; v1 = c - a
            den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
            den = np.where(np.abs(den) < 1e-12, 1e-12, den)
            qx = px - a[:, 0:1]; qy = py - a[:, 1:2]
            b1 = (qx * v1[:, 1:2] - v1[:, 0:1] * qy) / den[:, None]
            b2 = (v0[:, 0:1] * qy - qx * v0[:, 1:2]) / den[:, None]
            eps = 1e-6
            inside = (b1 >= -eps) & (b2 >= -eps) & (b1 + b2 <= 1 + eps) & (X >= 0) & (Y >= 0) & (X < size) & (Y < size)
            r, k = np.nonzero(inside)
            out.append((page[fs[r]].astype(np.int16), Y[r, k].astype(np.int32), X[r, k].astype(np.int32),
                        fs[r].astype(np.int32), b1[r, k].astype(np.float32), b2[r, k].astype(np.float32)))
    if not out:
        z = np.zeros(0)
        return z.astype(np.int16), z.astype(np.int32), z.astype(np.int32), z.astype(np.int32), z.astype(np.float32), z.astype(np.float32)
    cat = [np.concatenate([o[j] for o in out]) for j in range(6)]
    # a texel on a shared edge belongs to one face only
    key = (cat[0].astype(np.int64) * size + cat[1]) * size + cat[2]
    _u, first = np.unique(key, return_index=True)
    return tuple(c[first] for c in cat)


def dilate(img, mask, iters):
    """Fill empty texels with the colour of the nearest filled texel (chart
    gutters, so bilinear filtering / mip-maps show no seams). `iters` is kept
    for API compatibility; every empty texel is filled."""
    from scipy import ndimage
    img = np.asarray(img, np.float32)
    if mask.all() or not mask.any():
        return img, mask.copy()
    # work on a 1/1 grid but only inside the bounding box of the used area
    ys, xs = np.nonzero(mask.any(1))[0], np.nonzero(mask.any(0))[0]
    y0, y1 = ys.min(), ys.max() + 1; x0, x1 = xs.min(), xs.max() + 1
    sub = mask[y0:y1, x0:x1]
    _d, (iy, ix) = ndimage.distance_transform_edt(~sub, return_indices=True)
    out = img.copy()
    out[y0:y1, x0:x1] = img[y0:y1, x0:x1][iy, ix]
    m = np.zeros_like(mask); m[y0:y1, x0:x1] = True
    return out, m


# ------------------------------------------------------------------ main
def build_texture(cfg, log=print, progress=None, cancel=None):
    """cfg: mesh, model, photos {image name: path}, out_dir, georef (bool) +
    DEFAULTS keys; uv_npz for 'Keep uv'. Returns info dict (also info.json)."""
    c = dict(DEFAULTS); c.update({k: v for k, v in cfg.items() if v is not None})
    t_all = time.perf_counter()
    stages = {}
    def stage(name, t0):
        stages[name] = round(time.perf_counter() - t0, 2)
    threads = int(c["threads"]) or max(2, min(12, (os.cpu_count() or 4) - 2))
    def prog(p):
        if progress:
            progress(int(p), 100)
    out = c["out_dir"]; os.makedirs(out, exist_ok=True)
    t0 = time.perf_counter()
    V, F = read_mesh(c["mesh"])
    log(f"Texture: mesh {len(V):,} vertices, {len(F):,} faces")
    views = load_views(c["model"], c["photos"], log)
    if not views:
        raise RuntimeError("No aligned photos found - check the image paths (Tools > Change Image Paths...)")
    stage("load mesh + cameras", t0); prog(2)
    # small images: sharpness + colour statistics
    t0 = time.perf_counter()
    def small(v):
        v.small = load_image(v.path, 660)
        return sharpness(v.small)
    with ThreadPoolExecutor(threads) as ex:
        sh = list(ex.map(small, views))
    med = float(np.median(sh)) or 1.0
    for v, s in zip(views, sh):
        v.sharp = float(np.clip(math.sqrt(s / med), 0.7, 1.3))
    stage("read photos (1/8 scale)", t0); prog(8)
    log(f"Texture: read {len(views)} photos at 1/8 scale in {stages['read photos (1/8 scale)']:.1f} s "
        f"({threads} threads); sharpness range {min(v.sharp for v in views):.2f}..{max(v.sharp for v in views):.2f}")
    up = up_vector(views, c.get("georef"))
    t0 = time.perf_counter()
    vis = Visibility(V, F, log)
    nb = face_adjacency(F)
    # visibility depends only on mesh + cameras: cached, so re-texturing with
    # other blending / size / mapping settings skips the ray casting
    def _sig(p):
        st = os.stat(p); return [os.path.abspath(p), st.st_size, int(st.st_mtime)]
    key = json.dumps([_sig(c["mesh"]), _sig(os.path.join(c["model"], "images.bin")),
                      _sig(os.path.join(c["model"], "cameras.bin")), [v.name for v in views],
                      c["max_res"], c["vis_samples"], c["top_k"]])
    cache = os.path.join(out, "visibility_cache.npz")
    best_s = None
    if os.path.exists(cache):
        try:
            z = np.load(cache)
            if str(z["key"]) == key:
                best_s, best_v, sign = z["best_s"], z["best_v"], float(z["sign"])
                log("Texture: visibility reused from the previous run (same model and cameras)")
        except Exception:
            best_s = None
    if best_s is None:
        best_s, best_v, sign = view_scores(vis, views, up, c, log,
                                           progress=lambda i, n: prog(8 + 37 * i / n), cancel=cancel)
        try:
            np.savez(cache, key=key, best_s=best_s, best_v=best_v, sign=sign)
        except OSError:
            pass
    best_s = best_s.copy(); best_v = best_v.copy()
    stage("visibility", t0)
    t0 = time.perf_counter()
    col = candidate_colours(vis, views, best_v, threads)
    gains = exposure_gains(best_v, col, len(views), log) if c["blending"] in ("Mosaic", "Average") \
        else np.zeros((len(views), 3))
    if c["ghosting"]:
        ghost_filter(best_s, best_v, col * np.exp(gains)[np.maximum(best_v, 0)].astype(np.float32), log,
                     float(c["ghost_threshold"]))
    stage("colour statistics", t0); prog(50)
    t0 = time.perf_counter()
    smooth = c["smooth"] if c["blending"] in ("Mosaic", "Average", "Max intensity", "Min intensity") else 0.0
    L = select_labels(best_s, best_v, nb, smooth, log=log)
    if smooth > 0 and c.get("min_chart_faces"):
        L = merge_small(L, best_v, nb, int(c["min_chart_faces"]), log=log)
    stage("view selection", t0)
    t0 = time.perf_counter()
    offsets = seam_offsets(V, F, L, views, gains, threads, log) if c["blending"] == "Mosaic" else None
    hole_vc = fill_holes(V, F, L, nb, vis, views, gains, best_v, threads, c["hole_filling"], log)
    stage("seam levelling + hole filling", t0); prog(58)
    # ---- uv atlas
    t0 = time.perf_counter()
    mr = c["max_res"] or 0
    s_work = np.array([min(1.0, mr / max(v.W, v.H)) if mr else 1.0 for v in views])
    # pixels per model unit at working resolution (median over seen faces) -> GSD
    seen = np.flatnonzero(L >= 0)
    samp = seen[:: max(1, len(seen) // 200000)]
    if len(samp):
        dist = np.array([np.linalg.norm(vis.cent[f] - views[L[f]].C) for f in samp[:20000]])
        fpx = np.array([views[L[f]].cam.K()[0] * s_work[L[f]] for f in samp[:20000]])
        px_per_unit = float(np.median(fpx / dist))
        src_gsd = float(np.median(dist / np.array([views[L[f]].cam.K()[0] for f in samp[:20000]])))
    else:
        px_per_unit, src_gsd = 1.0 / vis.edge, vis.edge
    size, count, pad = int(c["size"]), int(c["count"]), int(c["padding"])
    m = len(F)
    if c["mapping"] == "Keep uv":
        z = np.load(c["uv_npz"])
        uvp, page = z["uv"].astype(np.float32), z["page"].astype(np.int64)
        if len(uvp) != m:
            raise RuntimeError("Keep uv: the model changed since the previous texture (face count differs)")
        old = int(z["size"]) if "size" in z else size
        if old != size:
            uvp = uvp * (size / old)
        count = int(page.max()) + 1
        scale = float(z["scale"]) * size / old if "scale" in z else 1.0
        log(f"Texture: keeping the previous uv atlas ({count} page(s))")
    else:
        e1, e2 = plane_basis(up)
        uv = np.zeros((m, 3, 2), np.float64)
        comp = np.zeros(m, np.int64)
        N = vis.N * sign
        if c["mapping"] == "Orthophoto":
            ortho = np.ones(m, bool)
        elif c["mapping"] == "Adaptive orthophoto":
            ortho = (N @ up) >= 0.5
        else:
            ortho = np.zeros(m, bool)
        gen = np.flatnonzero(~ortho & (L >= 0))
        hole = np.flatnonzero(~ortho & (L < 0))
        ort = np.flatnonzero(ortho)
        nxt = 0
        if len(gen):
            uv[gen] = generic_uv(V, F, L, views, gen, s_work)
            cc = charts_from_labels(gen, L, nb)
            comp[gen] = cc; nxt = cc.max() + 1
        if len(hole):
            # untextured faces: planar charts along the dominant normal axis
            ax = np.argmax(np.abs(N[hole]), 1)
            for a in range(3):
                hs = hole[ax == a]
                if not len(hs):
                    continue
                b1 = np.eye(3)[(a + 1) % 3]; b2 = np.eye(3)[(a + 2) % 3]
                uv[hs] = planar_uv(V, F, hs, b1, b2, px_per_unit)
                pseudo = np.full(m, -1, np.int64); pseudo[hs] = a
                cc = charts_from_labels(hs, pseudo, nb)
                comp[hs] = cc + nxt; nxt = comp[hs].max() + 1
        if len(ort):
            uv[ort] = planar_uv(V, F, ort, e1, e2, px_per_unit)
            cc = grid_charts(uv[ort], size * 0.45)
            comp[ort] = cc + nxt
        uv = orient_charts(uv, comp)
        comp = split_large(uv, comp, size * 0.9)
        page, uvp, scale = pack_fit(uv, comp, size, count, pad, log)
        count = int(page.max()) + 1
    stage("uv atlas", t0); prog(62)
    t0 = time.perf_counter()
    tp, ty, tx, tf, b1, b2 = rasterize(uvp, page, count, size)
    stage("rasterize atlas", t0)
    log(f"Texture: {len(tf) / 1e6:.1f} M texels cover the model ({stages['rasterize atlas']:.1f} s)")
    # ---- fill texels
    t0 = time.perf_counter()
    pages = np.zeros((count, size, size, 3), np.float32)
    filled = np.zeros((count, size, size), bool)
    b0 = 1.0 - b1 - b2
    def texel_points(sel):
        f = F[tf[sel]]
        return (V[f[:, 0]] * b0[sel, None] + V[f[:, 1]] * b1[sel, None] + V[f[:, 2]] * b2[sel, None])
    lab_t = L[tf]
    lock = threading.Lock()
    done = [0]
    def sample_view(vi, sel):
        """Colours of texels `sel` from photo vi. The float64 texel positions and
        projections are made in chunks (SAMPLE_CHUNK texels), not for all texels
        of the photo at once (millions per photo on big pages, x ~200 bytes)."""
        v = views[vi]
        img = load_image(v.path, mr)
        h, w = img.shape[:2]
        out_c = np.empty((len(sel), 3), np.float32)
        for a in range(0, len(sel), SAMPLE_CHUNK):
            part = sel[a:a + SAMPLE_CHUNK]
            uv_, _z = v.project(texel_points(part))
            out_c[a:a + len(part)] = bilinear(img, uv_[:, 0] * w / v.W, uv_[:, 1] * h / v.H)
            del uv_, _z
        del img
        return out_c
    # threads that sample photos hold one decoded photo + one chunk each: cap by free RAM
    from .memguard import cap_workers, MB
    _cw = views[0].cam if views else None
    _photo = (int(_cw.width) * int(_cw.height) * 3 * 2) if _cw is not None else 0
    if mr and _cw is not None and max(_cw.width, _cw.height) > mr:
        _photo = int(_photo * (mr / max(_cw.width, _cw.height)) ** 2)
    sample_threads = cap_workers(min(threads, 6), _photo + SAMPLE_CHUNK * 220 + 50 * MB)
    if sample_threads < min(threads, 6):
        log(f"Texture: {sample_threads} photo sampling threads (limited by free RAM)")
    if c["blending"] in ("Mosaic", "Disabled"):
        order = np.argsort(lab_t, kind="stable")
        bounds = np.searchsorted(lab_t[order], np.arange(-1, len(views) + 1))
        jobs = [(vi, order[bounds[vi + 1]:bounds[vi + 2]]) for vi in range(len(views))
                if bounds[vi + 2] > bounds[vi + 1]]
        def run(job):
            vi, sel = job
            colr = sample_view(vi, sel)
            if c["blending"] == "Mosaic":
                colr = colr * np.exp(gains[vi]).astype(np.float32)
                if offsets is not None:
                    o = offsets[tf[sel]]
                    colr += o[:, 0] * b0[sel, None] + o[:, 1] * b1[sel, None] + o[:, 2] * b2[sel, None]
            with lock:
                pages[tp[sel], ty[sel], tx[sel]] = colr
                filled[tp[sel], ty[sel], tx[sel]] = True
                done[0] += 1
            prog(62 + 30 * done[0] / max(1, len(jobs)))
            if cancel and cancel():
                raise KeyboardInterrupt
        with ThreadPoolExecutor(sample_threads) as ex:
            list(ex.map(run, jobs))
    else:
        # multi-view blending over each face's valid candidate photos
        n = len(tf)
        acc = np.zeros((n, 3), np.float32); wsum = np.zeros(n, np.float32)
        best = np.full(n, -1.0 if c["blending"] == "Max intensity" else 1e9, np.float32)
        tord = np.argsort(tf, kind="stable"); tfs = tf[tord]
        fstart = np.searchsorted(tfs, np.arange(m)); fcnt = np.searchsorted(tfs, np.arange(m), "right") - fstart
        K = best_v.shape[1]
        jobs = []
        for vi in range(len(views)):
            fk = np.argwhere(best_v == vi)
            if not len(fk):
                continue
            faces_v = fk[:, 0]; w_v = best_s[faces_v, fk[:, 1]]
            cnt = fcnt[faces_v]
            if not cnt.sum():
                continue
            st = np.repeat(fstart[faces_v] - np.cumsum(cnt) + cnt, cnt) + np.arange(cnt.sum())
            jobs.append((vi, tord[st], np.repeat(w_v, cnt)))
        def runb(job):
            vi, sel, w = job
            colr = sample_view(vi, sel)
            if c["blending"] == "Average":
                colr = colr * np.exp(gains[vi]).astype(np.float32)
            lum = colr.mean(1)
            with lock:
                if c["blending"] == "Average":
                    acc[sel] += colr * w[:, None]; wsum[sel] += w
                elif c["blending"] == "Max intensity":
                    b = lum > best[sel]; s2 = sel[b]; acc[s2] = colr[b]; best[s2] = lum[b]; wsum[s2] = 1
                else:
                    b = lum < best[sel]; s2 = sel[b]; acc[s2] = colr[b]; best[s2] = lum[b]; wsum[s2] = 1
                done[0] += 1
            prog(62 + 30 * done[0] / max(1, len(jobs)))
        with ThreadPoolExecutor(sample_threads) as ex:
            list(ex.map(runb, jobs))
        ok = wsum > 0
        colr = acc[ok] / wsum[ok, None]
        pages[tp[ok], ty[ok], tx[ok]] = colr; filled[tp[ok], ty[ok], tx[ok]] = True
    # holes (faces seen by no photo)
    hsel = np.flatnonzero(L[tf] < 0)
    if len(hsel) and hole_vc is not None:
        f = F[tf[hsel]]
        colr = hole_vc[f[:, 0]] * b0[hsel, None] + hole_vc[f[:, 1]] * b1[hsel, None] + hole_vc[f[:, 2]] * b2[hsel, None]
        pages[tp[hsel], ty[hsel], tx[hsel]] = colr; filled[tp[hsel], ty[hsel], tx[hsel]] = True
    stage("sample photos", t0); prog(93)
    # ---- write
    t0 = time.perf_counter()
    from PIL import Image
    ext = "png" if c["format"].lower() == "png" else "jpg"
    names = []
    for p in range(count):
        img, _m = dilate(pages[p], filled[p], pad + 2)
        im = Image.fromarray(np.clip(img + 0.5, 0, 255).astype(np.uint8))
        name = f"texture_{p}.{ext}"
        if ext == "jpg":
            im.save(os.path.join(out, name), quality=int(c["jpeg_quality"]), subsampling=0)
        else:
            im.save(os.path.join(out, name), compress_level=3)
        names.append(name)
    np.savez(os.path.join(out, "uv.npz"), uv=uvp, page=page.astype(np.int16), size=size, scale=scale)
    obj = os.path.join(out, "model_textured.obj")
    write_obj(obj, V, F, uvp, page, size, names)
    glb = None
    if c.get("write_glb", True):
        glb = os.path.join(out, "model_textured.glb")
        write_textured_glb(glb, V, F, uvp, page, size, [os.path.join(out, n) for n in names])
    stage("write files", t0)
    # texel size: model surface area / covered texels
    area_total = float(vis.area2.sum() / 2)
    texel = math.sqrt(area_total / max(len(tf), 1))
    info = {"faces": int(m), "vertices": int(len(V)), "size": size, "count": int(count), "pages": names,
            "obj": obj, "glb": glb, "uv_npz": os.path.join(out, "uv.npz"), "dir": out,
            "mapping": c["mapping"], "blending": c["blending"], "hole_filling": bool(c["hole_filling"]),
            "ghosting": bool(c["ghosting"]), "max_res": mr, "photos_used": int(len(np.unique(L[L >= 0]))),
            "faces_untextured": int((L < 0).sum()), "texel_size": texel, "source_gsd": src_gsd,
            "scale_of_working_res": float(scale), "texels": int(len(tf)),
            "georef": bool(c.get("georef")), "seconds": round(time.perf_counter() - t_all, 2), "stages": stages}
    with open(os.path.join(out, "info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    unit = "m" if c.get("georef") else "model units"
    log(f"Texture: texel size {texel * (100 if c.get('georef') else 1):.3g} "
        f"{'cm' if c.get('georef') else unit}, photo GSD {src_gsd * (100 if c.get('georef') else 1):.3g} "
        f"{'cm/pix' if c.get('georef') else unit + '/pix'}")
    log("Texture stages: " + ", ".join(f"{k} {v:.1f} s" for k, v in stages.items()))
    log(f"Texture done in {info['seconds']:.1f} s -> {obj}")
    prog(100)
    return info


# ------------------------------------------------------------------ export
def load_textured(mesh, tex_dir):
    """(V, F, uv per corner in pixels, page per face, size, texture paths)."""
    V, F = read_mesh(mesh)
    z = np.load(os.path.join(tex_dir, "uv.npz"))
    with open(os.path.join(tex_dir, "info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    uv, page, size = z["uv"], z["page"].astype(np.int64), int(z["size"])
    if len(uv) != len(F):
        raise ValueError("texture does not belong to this model (face count differs)")
    return V, F, uv, page, size, [os.path.join(tex_dir, n) for n in info["pages"]]


def _copy_pages(textures, dst_dir):
    import shutil
    names = []
    for t in textures:
        d = os.path.join(dst_dir, os.path.basename(t))
        if os.path.abspath(d) != os.path.abspath(t):
            shutil.copyfile(t, d)
        names.append(os.path.basename(t))
    return names


def export_obj(mesh, tex_dir, dst, offset=None):
    """OBJ + MTL + texture pages next to it; vertices + offset (world) if given."""
    V, F, uv, page, size, tex = load_textured(mesh, tex_dir)
    names = _copy_pages(tex, os.path.dirname(os.path.abspath(dst)))
    write_obj(dst, V, F, uv, page, size, names, offset=offset)
    return dst


def write_textured_glb(path, V, F, uv, page, size, textures, z_up=True, offset=None):
    from .gltf import split_corners, write_glb
    prims = []
    for k, t in enumerate(textures):
        sel = np.flatnonzero(page == k)
        if not len(sel):
            continue
        pos, uvs, idx = split_corners(V, F[sel], uv[sel] / size)
        with open(t, "rb") as fh:
            img = fh.read()
        prims.append({"positions": pos, "indices": idx, "uv": uvs, "colors": None, "image": img,
                      "mime": "image/png" if t.lower().endswith(".png") else "image/jpeg"})
    write_glb(path, prims, z_up=z_up, translation=offset,
              extras={"world_offset": [float(x) for x in offset],
                      "note": "positions are local; root node translation adds the world offset"}
              if offset is not None else None)
    return path


def export_glb(mesh, tex_dir, dst, z_up=True, offset=None):
    """glTF positions are float32: vertices stay local, the offset goes into the
    root node translation (glTF Y-up frame when z_up)."""
    V, F, uv, page, size, tex = load_textured(mesh, tex_dir)
    return write_textured_glb(dst, V, F, uv, page, size, tex, z_up, offset=offset)


def export_ply_textured(mesh, tex_dir, dst, offset=None):
    """Binary PLY with 'comment TextureFile' lines and per-face texcoord lists
    (+ texnumber), the layout Metashape / MeshLab use for textured PLY."""
    V, F, uv, page, size, tex = load_textured(mesh, tex_dir)
    names = _copy_pages(tex, os.path.dirname(os.path.abspath(dst)))
    m = len(F)
    ft = "float" if offset is None else "double"
    hdr = ["ply", "format binary_little_endian 1.0", "comment PhotoMesh Studio textured model"]
    if offset is not None:
        hdr.append("comment world coordinates (local + offset " + " ".join(f"{x:.3f}" for x in offset) + ")")
    hdr += [f"comment TextureFile {n}" for n in names]
    hdr += [f"element vertex {len(V)}", f"property {ft} x", f"property {ft} y", f"property {ft} z",
            f"element face {m}", "property list uchar int vertex_indices",
            "property list uchar float texcoord", "property int texnumber", "end_header"]
    t = (uv / size).astype(np.float32).copy()
    t[..., 1] = 1.0 - t[..., 1]
    face = np.zeros(m, dtype=[("n", "u1"), ("f", "<i4", 3), ("nt", "u1"), ("t", "<f4", 6), ("p", "<i4")])
    face["n"] = 3; face["f"] = F; face["nt"] = 6; face["t"] = t.reshape(m, 6); face["p"] = page
    from .export import atomic_write
    Vw = V.astype("<f4") if offset is None else (V.astype(np.float64) + np.asarray(offset, np.float64)).astype("<f8")
    with atomic_write(dst) as fh:
        fh.write(("\n".join(hdr) + "\n").encode("ascii"))
        fh.write(Vw.tobytes()); fh.write(face.tobytes())
    return dst


# ------------------------------------------------------------------ OBJ
def write_obj(path, V, F, uvp, page, size, textures, offset=None):
    """OBJ + MTL with one material per texture page; vt per face corner.
    offset: added to the vertices (world coordinates, 6 decimals)."""
    from .export import atomic_write
    base = os.path.splitext(os.path.basename(path))[0]
    mtl = os.path.join(os.path.dirname(path), base + ".mtl")
    with atomic_write(mtl, "w", encoding="utf-8") as fh:
        for k, t in enumerate(textures):
            fh.write(f"newmtl material_{k}\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nillum 1\nmap_Kd {t}\n\n")
    m = len(F)
    # one vt per distinct (vertex, page, uv): corners inside a chart share it
    q = np.round(uvp.reshape(-1, 2).astype(np.float64) * 64).astype(np.int64)
    key = np.stack([F.ravel(), np.repeat(page, 3), q[:, 0], q[:, 1]], 1)
    _uk, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    vt = uvp.reshape(-1, 2)[first].astype(np.float64) / size
    vt[:, 1] = 1.0 - vt[:, 1]
    corner_vt = inv.reshape(m, 3)
    if offset is not None:
        V = np.asarray(V, np.float64) + np.asarray(offset, np.float64)
    with atomic_write(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# PhotoMesh Studio textured model: {len(V)} vertices, {m} faces\n")
        if offset is not None:
            fh.write("# world coordinates (local + offset " + " ".join(f"{x:.3f}" for x in offset) + ")\n")
        fh.write(f"mtllib {base}.mtl\n")
        _write_rows(fh, "v", V, "%.6f")
        _write_rows(fh, "vt", vt, "%.6f")
        order = np.argsort(page, kind="stable")
        bounds = np.searchsorted(page[order], np.arange(len(textures) + 1))
        for k in range(len(textures)):
            sel = order[bounds[k]:bounds[k + 1]]
            if not len(sel):
                continue
            fh.write(f"usemtl material_{k}\n")
            fv = F[sel] + 1; ft = corner_vt[sel] + 1
            arr = np.empty((len(sel), 6), np.int64); arr[:, 0::2] = fv; arr[:, 1::2] = ft
            _write_rows(fh, "f", arr, "%d/%d %d/%d %d/%d", grouped=True)


def _write_rows(fh, tag, arr, fmt, grouped=False, chunk=200000):
    n = len(arr)
    cols = arr.shape[1]
    line = f"{tag} " + (fmt if grouped else " ".join([fmt] * cols)) + "\n"
    for s in range(0, n, chunk):
        blk = arr[s:s + chunk]
        fh.write((line * len(blk)) % tuple(blk.ravel()))


def read_obj(path):
    """Minimal OBJ reader for our own output: V, F, per-corner uv (pixels of a
    unit atlas, i.e. 0..1), page per face."""
    V, VT, F, FT, P = [], [], [], [], []
    mat = {}; cur = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                V.append(line[2:].split()[:3])
            elif line.startswith("vt "):
                VT.append(line[3:].split()[:2])
            elif line.startswith("usemtl"):
                name = line.split(None, 1)[1].strip()
                cur = mat.setdefault(name, len(mat))
            elif line.startswith("f "):
                parts = line[2:].split()
                if len(parts) != 3:
                    continue
                vv = [p.split("/") for p in parts]
                F.append([int(x[0]) for x in vv])
                FT.append([int(x[1]) if len(x) > 1 and x[1] else 0 for x in vv]); P.append(cur)
    V = np.asarray(V, np.float64); F = np.asarray(F, np.int64) - 1
    uv = None
    if VT:
        VT = np.asarray(VT, np.float64); FT = np.asarray(FT, np.int64) - 1
        uv = VT[FT]; uv[..., 1] = 1 - uv[..., 1]
    return V, F, uv, np.asarray(P, np.int64)
