"""Point cloud outlier filtering (dense cloud) and gradual selection (tie points).

Dense cloud filters (all multi-threaded through scipy's cKDTree, workers=-1):
  * confidence  - minimum number of photos a fused point was seen in
                  (from COLMAP's fused.ply.vis; stored as 'confidence' attribute)
  * statistical - mean distance to k nearest neighbours > mean + std_ratio*std
  * radius      - fewer than min_neighbors within radius (auto: 3x median spacing)
  * elevation   - terrain sanity: points above the highest camera, or far from
                  the local terrain height (robust per-cell median/MAD)
Tie point gradual selection criteria (Metashape-like):
  reprojection error, reconstruction uncertainty, image count, projection accuracy.
"""
from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ PLY io
def read_cloud(path):
    """dict with xyz (n,3) float64, rgb (n,3) uint8 | None, normals | None,
    confidence (n,) | None, plus 'extra' for other vertex properties."""
    from .plyio import read_fast
    fast = read_fast(path)
    if fast is not None:
        v = fast["vertex"]
    else:
        from plyfile import PlyData
        with open(path, "rb") as fh:          # no memory map (Windows locks mapped files)
            v = PlyData.read(fh, mmap=False)["vertex"].data
    names = v.dtype.names
    out = {"xyz": np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)}
    out["rgb"] = (np.column_stack([v["red"], v["green"], v["blue"]]).astype(np.uint8)
                  if "red" in names else None)
    out["normals"] = (np.column_stack([v["nx"], v["ny"], v["nz"]]).astype(np.float32)
                      if "nx" in names else None)
    out["confidence"] = np.asarray(v["confidence"]).astype(np.int32) if "confidence" in names else None
    return out


def write_cloud(path, xyz, rgb=None, normals=None, confidence=None, offset=None, double=False):
    from plyfile import PlyData, PlyElement
    ft = "f8" if double else "f4"
    dt = [("x", ft), ("y", ft), ("z", ft)]
    if normals is not None:
        dt += [("nx", "f4"), ("ny", "f4"), ("nz", "f4")]
    if rgb is not None:
        dt += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    if confidence is not None:
        dt += [("confidence", "u2")]
    a = np.empty(len(xyz), dtype=dt)
    p = xyz + (offset if offset is not None else 0)
    a["x"], a["y"], a["z"] = p[:, 0], p[:, 1], p[:, 2]
    if normals is not None:
        a["nx"], a["ny"], a["nz"] = normals[:, 0], normals[:, 1], normals[:, 2]
    if rgb is not None:
        a["red"], a["green"], a["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    if confidence is not None:
        a["confidence"] = np.clip(confidence, 0, 65535)
    PlyData([PlyElement.describe(a, "vertex")], text=False).write(path)


def subset(cloud, keep):
    return {k: (v[keep] if isinstance(v, np.ndarray) and len(v) == len(cloud["xyz"]) else v)
            for k, v in cloud.items()}


# ----------------------------------------------------------- dense filters
def _tree(xyz):
    from scipy.spatial import cKDTree
    return cKDTree(xyz, leafsize=32, balanced_tree=False, compact_nodes=False)


def median_spacing(xyz, sample=200_000, tree=None):
    tree = tree or _tree(xyz)
    idx = np.random.default_rng(0).choice(len(xyz), min(sample, len(xyz)), replace=False)
    d, _ = tree.query(xyz[idx], k=2, workers=-1)
    return float(np.median(d[:, 1]))


def statistical_outliers(xyz, k=16, std_ratio=2.0, tree=None, chunk=2_000_000):
    """Keep mask: mean distance to k neighbours <= mean + std_ratio * std."""
    tree = tree or _tree(xyz)
    md = np.empty(len(xyz), np.float32)
    for s in range(0, len(xyz), chunk):
        d, _ = tree.query(xyz[s:s + chunk], k=k + 1, workers=-1)
        md[s:s + chunk] = d[:, 1:].mean(1)
    mu, sd = float(md.mean()), float(md.std())
    return md <= mu + std_ratio * sd


def radius_outliers(xyz, radius=None, min_neighbors=4, tree=None, chunk=2_000_000):
    """Keep mask: at least min_neighbors other points within radius."""
    tree = tree or _tree(xyz)
    radius = radius or 3.0 * median_spacing(xyz, tree=tree)
    cnt = np.empty(len(xyz), np.int32)
    for s in range(0, len(xyz), chunk):
        cnt[s:s + chunk] = tree.query_ball_point(xyz[s:s + chunk], radius, workers=-1,
                                                 return_length=True)
    return cnt - 1 >= min_neighbors, radius


def local_outliers(xyz, k=16, kr=4, stat_ratio=3.0, radius_ratio=3.0, global_cap=10.0, tree=None,
                   chunk=1_000_000):
    """Density-adaptive statistical + radius filter for clouds whose point
    spacing varies a lot (near/far ground, wide and tele photos).

    md  = mean distance to the k nearest neighbours, dk = distance to the
    kr-th neighbour. A point is an outlier when its value is more than
    *_ratio times the median of the same value over its k neighbours, i.e.
    it is isolated relative to its own surroundings, not to the global median.
    Small clumps of floating points vouch for each other locally, so a point
    is also removed when dk exceeds global_cap x the median dk of the cloud.
    Returns (keep_statistical, keep_radius)."""
    tree = tree or _tree(xyz)
    n = len(xyz)
    md = np.empty(n, np.float32); dk = np.empty(n, np.float32)
    for s in range(0, n, chunk):
        d, _ = tree.query(xyz[s:s + chunk], k=k + 1, workers=-1)
        md[s:s + chunk] = d[:, 1:].mean(1); dk[s:s + chunk] = d[:, kr]
    keep_s = np.ones(n, bool); keep_r = np.ones(n, bool)
    cap = global_cap * float(np.median(dk)) if global_cap else np.inf
    for s in range(0, n, chunk):
        _, idx = tree.query(xyz[s:s + chunk], k=k + 1, workers=-1)
        nb = idx[:, 1:]
        ref_md = np.median(md[nb], axis=1); ref_dk = np.median(dk[nb], axis=1)
        keep_s[s:s + chunk] = md[s:s + chunk] <= stat_ratio * ref_md
        keep_r[s:s + chunk] = (dk[s:s + chunk] <= radius_ratio * ref_dk) & (dk[s:s + chunk] <= cap)
    return keep_s, keep_r


def up_vector(cam_dirs):
    """Up direction for nadir/oblique surveys: opposite of mean viewing direction."""
    d = np.asarray(cam_dirs, float)
    u = -d.mean(0)
    return u / (np.linalg.norm(u) or 1)


def elevation_outliers(xyz, cam_centers, up=None, cell=None, k=6.0, min_dev=None):
    """Keep mask for terrain sanity in an up-aligned frame.

    Removes points above the highest camera centre and points whose height
    deviates more than max(k*1.4826*MAD, min_dev) from the median height of
    their (cell x cell) ground cell.
    """
    C = np.asarray(cam_centers, float)
    up = np.asarray(up if up is not None else [0, 0, 1], float); up /= np.linalg.norm(up)
    a = np.cross(up, [1, 0, 0] if abs(up[0]) < 0.9 else [0, 1, 0]); a /= np.linalg.norm(a)
    b = np.cross(up, a)
    h = xyz @ up; ch = C @ up
    keep = h < ch.max()
    flying = np.median(ch) - np.median(h)                 # typical flying height
    cell = cell or max(flying / 4, 1e-6)
    min_dev = min_dev if min_dev is not None else 0.25 * flying
    gx = np.floor((xyz @ a) / cell).astype(np.int64); gy = np.floor((xyz @ b) / cell).astype(np.int64)
    key = (gx - gx.min()) * (gy.max() - gy.min() + 1) + (gy - gy.min())
    order = np.argsort(key, kind="stable"); ks = key[order]; hs = h[order]
    starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]]); ends = np.r_[starts[1:], len(ks)]
    dev = np.empty(len(h))
    for s, e in zip(starts, ends):
        seg = hs[s:e]
        m = np.median(seg); mad = np.median(np.abs(seg - m)) * 1.4826
        dev[order[s:e]] = np.abs(seg - m) / max(k * mad, min_dev)
    return keep & (dev <= 1.0)


def filter_cloud(cloud, cfg, cam_centers=None, cam_dirs=None, up=None, log=print):
    """Apply the configured filters in order; returns (cloud, stats list)."""
    stats = []
    n0 = len(cloud["xyz"])
    if cfg.get("min_confidence", 0) and cloud.get("confidence") is not None:
        keep = cloud["confidence"] >= int(cfg["min_confidence"])
        if cfg.get("max_confidence"):
            keep &= cloud["confidence"] <= int(cfg["max_confidence"])
        cloud = subset(cloud, keep); stats.append(("confidence", int((~keep).sum())))
    if cfg.get("elevation", False) and cam_centers is not None and len(cloud["xyz"]):
        u = up if up is not None else up_vector(cam_dirs)
        keep = elevation_outliers(cloud["xyz"], cam_centers, u)
        cloud = subset(cloud, keep); stats.append(("elevation", int((~keep).sum())))
    tree = None
    if cfg.get("outlier_mode", "local") == "local" and (cfg.get("statistical", True) or cfg.get("radius", True)) \
            and len(cloud["xyz"]) > 32:
        ks, kr = local_outliers(cloud["xyz"], k=int(cfg.get("sor_k", 16)), kr=int(cfg.get("radius_min", 4)),
                                stat_ratio=float(cfg.get("local_ratio", 3.0)),
                                radius_ratio=float(cfg.get("local_ratio", 3.0)))
        keep = np.ones(len(ks), bool)
        if cfg.get("statistical", True):
            keep &= ks; stats.append(("statistical (local)", int((~ks).sum())))
        if cfg.get("radius", True):
            stats.append(("radius (local)", int((~kr & ks).sum() if cfg.get("statistical", True) else (~kr).sum())))
            keep &= kr
        cloud = subset(cloud, keep)
        cfg = dict(cfg, statistical=False, radius=False)
    if cfg.get("statistical", True) and len(cloud["xyz"]) > 32:
        tree = _tree(cloud["xyz"])
        keep = statistical_outliers(cloud["xyz"], k=int(cfg.get("sor_k", 16)),
                                    std_ratio=float(cfg.get("sor_std", 2.0)), tree=tree)
        cloud = subset(cloud, keep); stats.append(("statistical", int((~keep).sum()))); tree = None
    if cfg.get("radius", True) and len(cloud["xyz"]) > 32:
        keep, r = radius_outliers(cloud["xyz"], radius=cfg.get("radius_value") or None,
                                  min_neighbors=int(cfg.get("radius_min", 4)))
        cloud = subset(cloud, keep); stats.append((f"radius (r={r:.4g})", int((~keep).sum())))
    for name, n in stats:
        log(f"  removed {n:,} points by {name} filter")
    log(f"Point cloud: {n0:,} -> {len(cloud['xyz']):,} points after filtering")
    return cloud, stats


# --------------------------------------------- tie points: gradual selection
CRITERIA = ["Reprojection error", "Reconstruction uncertainty", "Image count",
            "Projection accuracy"]


def tie_point_metrics(model, keypoint_scales=None):
    """Per tie point: dict of criterion -> value (higher = worse, except
    image count where lower = worse)."""
    from .colmap_io import observation_errors
    P = model.points
    errs, imgs, pidx = observation_errors(model)
    n = len(P)
    reproj = np.zeros(n); np.maximum.at(reproj, pidx, errs)
    count = P.track_len().astype(float)
    # reconstruction uncertainty: sqrt(lmax/lmin) of sum of ray projectors
    C = {iid: im.center() for iid, im in model.images.items()}
    centers = np.array([C[int(i)] for i in imgs])
    d = P.xyz[pidx] - centers
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    M = np.zeros((n, 3, 3))
    proj = np.eye(3)[None] - d[:, :, None] * d[:, None, :]
    np.add.at(M, pidx, proj)
    ev = np.linalg.eigvalsh(M)
    unc = np.sqrt(np.maximum(ev[:, 2], 1e-12) / np.maximum(ev[:, 0], 1e-12))
    out = {"Reprojection error": reproj, "Reconstruction uncertainty": unc,
           "Image count": count}
    if keypoint_scales is not None:
        sc = np.zeros(n); np.add.at(sc, pidx, keypoint_scales)
        out["Projection accuracy"] = sc / np.maximum(count, 1)
    return out


def gradual_select(metrics, criterion, level):
    """Selected (= to remove) mask, Metashape semantics: values above level,
    or for Image count, points seen in fewer than level images."""
    v = metrics[criterion]
    return v < level if criterion == "Image count" else v > level


def keypoint_scales(db_path, model):
    """Mean keypoint scale for every observation (for Projection accuracy)."""
    import sqlite3
    P = model.points
    con = sqlite3.connect(db_path)
    name_to_db = dict(con.execute("SELECT name, image_id FROM images"))
    scales = np.ones(len(P.track_img))
    imgs = P.track_img.astype(np.int64)
    for iid, im in model.images.items():
        dbid = name_to_db.get(im.name)
        row = con.execute("SELECT rows, cols, data FROM keypoints WHERE image_id=?", (dbid,)).fetchone()
        if not row or row[1] < 4:
            continue
        kp = np.frombuffer(row[2], np.float32).reshape(row[0], row[1])
        s = (np.sqrt(np.abs(kp[:, 2] * kp[:, 5] - kp[:, 3] * kp[:, 4])) if row[1] == 6
             else kp[:, 2])
        sel = np.flatnonzero(imgs == iid)
        idx = P.track_idx[sel].astype(np.int64)
        ok = idx < len(s)
        scales[sel[ok]] = s[idx[ok]]
    con.close()
    return scales
