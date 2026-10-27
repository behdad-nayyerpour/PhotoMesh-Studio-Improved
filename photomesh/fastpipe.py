"""fastpipe - PhotoMesh Studio's own speed-optimised alignment pipeline.

Original code. Ideas follow well-known published techniques, no proprietary code:
  * Pair preselection instead of exhaustive O(n^2) matching:
      - GPS/EXIF spatial k-nearest neighbours (common in aerial SfM, e.g.
        OpenDroneMap / "spatial matching"),
      - global image descriptor similarity top-k (a tiny GIST-like descriptor:
        Oliva & Torralba 2001; grid of gradient-orientation histograms + a
        downscaled thumbnail), a vocabulary-free alternative to vocab-tree
        retrieval (Nister & Stewenius 2006),
      - sequential window neighbours (video / strip capture order).
    The pair list is fed to COLMAP `matches_importer --match_type pairs`.
  * Coarse-to-fine: align on downscaled images first, then extract features at
    the requested accuracy and match only pairs that were co-visible in the
    coarse model (pose-guided matching); the fine tie points are triangulated
    into the coarse poses (`point_triangulator`) and refined with
    `bundle_adjuster`, which is much cheaper than a second incremental SfM.
  * Parallel image analysis (EXIF, thumbnail, descriptor) with multiprocessing.
  * GPU flags on for SIFT extraction/matching (when the COLMAP build has GPU).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

COARSE_MAX_SIZE = 1024
COARSE_KEYPOINTS = 4000
BA_FUNCTION_TOLERANCE = 1e-6     # relative cost change at which bundle adjustment stops
# Fine-stage features kept per photo after extraction (see select_keypoints).
# GPU brute-force matching is O(features^2): on the drone set all ~50k
# features (clamped to 32768) cost 82 ms/pair; 18k cost 21 ms/pair.
FINE_KEEP = {"Highest": 24000, "High": 24000, "Medium": 18000, "Low": 12000, "Lowest": 8000}


# ------------------------------------------------------------ image analysis
def _rat(v):
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return 0.0


def _gps(exif):
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:
        return None, None, None
    if not gps or 2 not in gps or 4 not in gps:
        return None, None, None

    def dms(vals, ref):
        d = _rat(vals[0]) + _rat(vals[1]) / 60 + _rat(vals[2]) / 3600
        return -d if ref in ("S", "W") else d
    lat = dms(gps[2], gps.get(1, "N"))
    lon = dms(gps[4], gps.get(3, "E"))
    alt = None
    if 6 in gps:
        ref = gps.get(5, 0)
        if isinstance(ref, (bytes, bytearray)):
            ref = ref[0] if ref else 0
        alt = _rat(gps[6]) * (-1 if ref == 1 else 1)
    return lon, lat, alt


def global_descriptor(gray: np.ndarray) -> np.ndarray:
    """GIST-like descriptor: 4x4 grid x 8 orientation bins + 16x12 thumbnail."""
    g = gray.astype(np.float32)
    gx = np.zeros_like(g); gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    mag = np.hypot(gx, gy)
    ang = (np.arctan2(gy, gx) % np.pi) / np.pi * 8
    b = np.minimum(ang.astype(int), 7)
    h, w = g.shape
    feats = []
    for i in range(4):
        for j in range(4):
            sl = (slice(i * h // 4, (i + 1) * h // 4), slice(j * w // 4, (j + 1) * w // 4))
            feats.append(np.bincount(b[sl].ravel(), weights=mag[sl].ravel(), minlength=8))
    hog = np.concatenate(feats)
    hog /= (np.linalg.norm(hog) + 1e-6)
    small = g.reshape(12, h // 12, 16, w // 16).mean(axis=(1, 3)).ravel()
    small -= small.mean()
    small /= (np.linalg.norm(small) + 1e-6)
    d = np.concatenate([hog, 0.5 * small])
    return (d / (np.linalg.norm(d) + 1e-6)).astype(np.float32)


def _cache_key(path):
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:20]


def analyze_image(path: str, cache_dir: str, thumb_size: int = 192) -> dict:
    """Worker: EXIF GPS, image size, cached thumbnail and global descriptor."""
    from PIL import Image, ImageOps
    os.makedirs(cache_dir, exist_ok=True)
    key = _cache_key(path)
    thumb = os.path.join(cache_dir, key + ".jpg")
    desc_p = os.path.join(cache_dir, key + ".npy")
    with Image.open(path) as im:
        w, h = im.size
        lon, lat, alt = _gps(im.getexif())
        if not (os.path.exists(thumb) and os.path.exists(desc_p)):
            im.draft("RGB", (thumb_size * 2, thumb_size * 2))  # fast JPEG DCT downscale
            im = ImageOps.exif_transpose(im).convert("RGB")
            t = im.copy(); t.thumbnail((thumb_size, thumb_size))
            t.save(thumb, quality=85)
            gray = np.asarray(im.convert("L").resize((128, 96)))
            np.save(desc_p, global_descriptor(gray))
    return {"path": path, "width": w, "height": h, "lon": lon, "lat": lat,
            "alt": alt, "thumb": thumb, "desc": desc_p}


def read_header(path: str) -> dict:
    """Image size and EXIF GPS from the file header only (no pixel decoding;
    Pillow reads just the first few KB of a JPEG). Used when photos are added."""
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
        try:
            exif = im.getexif()
        except Exception:
            exif = None
        lon, lat, alt = _gps(exif) if exif else (None, None, None)
    return {"path": path, "width": w, "height": h, "lon": lon, "lat": lat, "alt": alt}


def pool_map(fn, jobs, workers, chunksize=4):
    """Ordered map over worker processes ('spawn'); if the pool cannot start or
    breaks (e.g. antivirus kills the worker processes), redo it in threads."""
    import multiprocessing as mp
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures.process import BrokenProcessPool
    jobs = list(jobs)
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as ex:
            for r in ex.map(fn, jobs, chunksize=chunksize):
                done += 1
                yield r
            return
    except (BrokenProcessPool, OSError) as e:
        import sys
        print(f"worker processes failed after {done}/{len(jobs)} jobs ({e}); continuing with threads",
              file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        yield from ex.map(fn, jobs[done:])


def _thread_count(workers, n):
    return max(1, min(n, workers or min(8, os.cpu_count() or 2)))


def map_images(fn, paths, workers=None, progress=None, cancel=None):
    """fn(path) for every path in a thread pool (Pillow releases the GIL while
    decoding, so threads scale like processes - measured: 641 x 22 MB DJI JPGs,
    8 threads 11.7 s vs 8 processes 11.9 s - without the Windows 'spawn' worker
    processes that antivirus software or pythonw can break). Exceptions become
    {"path": p, "error": msg}. progress(done, total, path, result); cancel() -> True
    stops early (the remaining paths are left out of the result)."""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    out, n = {}, len(paths)
    if not n:
        return out

    def safe(p):
        try:
            return fn(p)
        except Exception as e:  # unreadable / not an image
            return {"path": p, "error": f"{type(e).__name__}: {e}"}
    nt = _thread_count(workers, n)
    with ThreadPoolExecutor(max_workers=nt, thread_name_prefix="images") as ex:
        it = iter(paths)
        running = {}
        for p in it:
            running[ex.submit(safe, p)] = p
            if len(running) >= nt * 2:
                break
        done_n = 0
        while running:
            done, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
            for f in done:
                p = running.pop(f)
                out[p] = f.result(); done_n += 1
                if progress:
                    progress(done_n, n, p, out[p])
            if cancel is not None and cancel():
                for f in running:
                    f.cancel()
                break
            for p in it:
                running[ex.submit(safe, p)] = p
                if len(running) >= nt * 2:
                    break
    return out


def analyze_images(paths, cache_dir, workers=None, progress=None, cancel=None):
    """Thumbnail + global descriptor + EXIF for many images (thread pool).
    progress(done, total) is called (also accepts the 4-argument form)."""
    def prog(i, n, p=None, r=None):
        if progress is None:
            return
        try:
            progress(i, n, p, r)
        except TypeError:
            progress(i, n)
    return map_images(lambda p: analyze_image(p, cache_dir), list(paths), workers, prog, cancel)


def _safe(p, cache_dir):
    try:
        return analyze_image(p, cache_dir)
    except Exception as e:
        return {"path": p, "error": str(e)}


# ------------------------------------------------------------ decode-once cache
def cache_dims(w, h, size):
    """Pixel size of a photo downscaled so its longest side is <= size."""
    sc = min(1.0, size / float(max(w, h)))
    return max(1, round(w * sc)), max(1, round(h * sc))


def shrink_photo(src, outs):
    """Decode src ONCE (JPEG DCT-domain downscale via draft) and write each
    (dst, size) in outs, largest first, keeping the EXIF (focal length, GPS).
    Returns (W, H, {dst: (w, h)})."""
    from PIL import Image
    outs = sorted(outs, key=lambda o: -o[1])
    with Image.open(src) as im:
        W, H = im.size
        exif = im.info.get("exif", b"")
        tw, th = cache_dims(W, H, outs[0][1])
        im.draft("RGB", (tw, th))
        img = im.convert("RGB")
    res = {}
    for dst, size in outs:
        w, h = cache_dims(W, H, size)
        if img.size != (w, h):
            img = img.resize((w, h), Image.LANCZOS)
        tmp = dst + ".part"
        img.save(tmp, "JPEG", quality=95, exif=exif)
        os.replace(tmp, dst)
        res[dst] = (w, h)
    return W, H, res


def _shrink_job(job):
    src, outs = job
    try:
        return src, shrink_photo(src, outs), None
    except Exception as e:  # unreadable photo: COLMAP will report it later
        return src, None, str(e)


def build_work_images(names, src_dir, targets, workers=None, progress=None):
    """Decode every photo once in a process pool and write downscaled copies
    into each target dir ({dir: max_size}). The source folder is only read.
    Returns {name: {"orig": (W, H), dir: (w, h), ...}}."""
    src_abs = os.path.normcase(os.path.abspath(src_dir))
    for d in targets:
        da = os.path.normcase(os.path.abspath(d))
        if da == src_abs or da.startswith(src_abs + os.sep):
            raise RuntimeError("working images must not be written into the photo folder")
        os.makedirs(d, exist_ok=True)
    jobs = [(os.path.join(src_dir, n), [(os.path.join(d, n), sz) for d, sz in targets.items()]) for n in names]
    out, n = {}, len(jobs)
    workers = workers or max(1, min(12, (os.cpu_count() or 2) - 2))

    def take(src, r, err):
        name = os.path.basename(src)
        if r:
            W, H, dims = r
            out[name] = {"orig": (W, H)}
            for d in targets:
                out[name][d] = dims[os.path.join(d, name)]
    if workers == 1 or n < 4:
        for i, j in enumerate(jobs):
            take(*_shrink_job(j))
            if progress: progress(i + 1, n)
        return out
    for i, r in enumerate(pool_map(_shrink_job, jobs, workers)):
        take(*r)
        if progress: progress(i + 1, n)
    return out


# ---- tie point colours from the working images (instead of COLMAP re-reading
# every full-size photo single-threaded: ~100 s on 600 x 22 MB JPEGs)
def _read_images_bin(path):
    """{image_id: (name, points2D (n,2) float64)} from a COLMAP images.bin."""
    import struct
    buf = open(path, "rb").read()
    n = struct.unpack_from("<Q", buf, 0)[0]; o = 8
    out = {}
    p2d = np.dtype([("x", "<f8"), ("y", "<f8"), ("id", "<i8")])
    for _ in range(n):
        iid = struct.unpack_from("<I", buf, o)[0]; o += 4 + 8 * 7 + 4
        e = buf.index(b"\0", o); name = buf[o:e].decode("utf-8", "replace"); o = e + 1
        m = struct.unpack_from("<Q", buf, o)[0]; o += 8
        a = np.frombuffer(buf, p2d, m, o); o += m * 24
        out[iid] = (name, np.stack([a["x"], a["y"]], 1))
    return out


def _points3d_first_obs(path):
    """(buffer, rgb byte offsets, first observation image ids, point2D idx)."""
    import struct
    buf = bytearray(open(path, "rb").read())
    n = struct.unpack_from("<Q", buf, 0)[0]; o = 8
    rgb_off = np.empty(n, np.int64); img = np.empty(n, np.int64); idx = np.empty(n, np.int64)
    unpack = struct.unpack_from
    for k in range(n):
        rgb_off[k] = o + 32
        tl = unpack("<Q", buf, o + 43)[0]
        img[k], idx[k] = unpack("<II", buf, o + 51) if tl else (-1, 0)
        o += 51 + 8 * tl
    return buf, rgb_off, img, idx


def _sample_colors(job):
    path, xy = job
    from PIL import Image
    try:
        with Image.open(path) as im:
            a = np.asarray(im.convert("RGB"))
    except Exception:
        return None
    h, w = a.shape[:2]
    c = np.clip(xy[:, 0].astype(np.int64), 0, w - 1); r = np.clip(xy[:, 1].astype(np.int64), 0, h - 1)
    return a[r, c]


def color_model_from_work_images(model_dir, work_dir, dims, workers=None):
    """Set tie point colours of a BIN model by sampling the downscaled working
    images in parallel. Returns the number of coloured points."""
    pts = os.path.join(model_dir, "points3D.bin")
    ims = os.path.join(model_dir, "images.bin")
    if not (os.path.exists(pts) and os.path.exists(ims)):
        return 0
    images = _read_images_bin(ims)
    buf, rgb_off, img, idx = _points3d_first_obs(pts)
    jobs, sel = [], []
    for iid in np.unique(img):
        if iid not in images:
            continue
        name, p2 = images[iid]
        d = dims.get(name)
        path = os.path.join(work_dir, name)
        if not d or not os.path.exists(path):
            continue
        (W, H), (w, h) = d["orig"], d[work_dir]
        m = np.nonzero(img == iid)[0]
        xy = p2[idx[m]] * np.array([w / W, h / H])
        jobs.append((path, xy)); sel.append(m)
    colored = 0
    arr = np.frombuffer(buf, np.uint8)
    workers = workers or max(1, min(12, (os.cpu_count() or 2) - 2))
    for m, col in zip(sel, pool_map(_sample_colors, jobs, workers)):
        if col is None:
            continue
        arr[rgb_off[m][:, None] + np.arange(3)] = col
        colored += len(m)
    tmp = pts + ".part"
    with open(tmp, "wb") as f:
        f.write(buf)
    os.replace(tmp, pts)
    return colored


def select_keypoints(db, keep, small_frac=0.8):
    """Keep `keep` features per image: small_frac of them with the smallest
    scale (best localised -> low reprojection error) and the rest with the
    largest scale (most repeatable -> robust matching). On the drone subset
    this gave ~1.0 px at 21 ms/pair vs 1.21 px for COLMAP's own cap (which
    keeps only the largest scales) and 82 ms/pair for all 50k features."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT k.image_id, k.rows, k.cols, k.data, d.cols, d.data FROM keypoints k "
                           "JOIN descriptors d ON d.image_id = k.image_id").fetchall()
        changed = 0
        for iid, r, c, kb, dc, dbb in rows:
            if r <= keep or c not in (4, 6):
                continue
            k = np.frombuffer(kb, np.float32).reshape(r, c)
            scale = np.sqrt(np.abs(k[:, 2] * k[:, 5] - k[:, 3] * k[:, 4])) if c == 6 else k[:, 2]
            order = np.argsort(scale, kind="stable")
            a = int(round(small_frac * keep))
            sel = np.sort(np.concatenate([order[:a], order[r - (keep - a):]]))
            d = np.frombuffer(dbb, np.uint8).reshape(r, dc)
            con.execute("UPDATE keypoints SET rows=?, data=? WHERE image_id=?", (len(sel), k[sel].tobytes(), iid))
            con.execute("UPDATE descriptors SET rows=?, data=? WHERE image_id=?", (len(sel), d[sel].tobytes(), iid))
            changed += 1
        con.commit()
        return changed
    finally:
        con.close()


def merge_identical_cameras(db):
    """Share one camera between photos with identical model, size and
    focal-length prior. Replaces COLMAP's --ImageReader.single_camera, which
    *rejects* every photo whose size differs from the first one ("Single
    camera specified, but images have different dimensions") - e.g. the 70 mm
    tele camera (4032x3024) of a Mavic 3 Pro mixed with its 5280x3956 main
    camera. Returns {camera_id: number of photos}."""
    con = sqlite3.connect(db)
    try:
        cams = con.execute("SELECT camera_id, model, width, height, params FROM cameras ORDER BY camera_id").fetchall()
        first, remap = {}, {}
        for cid, model, w, h, params in cams:
            key = (model, w, h, bytes(params))
            if key in first:
                remap[cid] = first[key]
            else:
                first[key] = cid
        for old, new in remap.items():
            con.execute("UPDATE images SET camera_id=? WHERE camera_id=?", (new, old))
            con.execute("DELETE FROM cameras WHERE camera_id=?", (old,))
        con.commit()
        return dict(con.execute("SELECT camera_id, COUNT(*) FROM images GROUP BY camera_id").fetchall())
    finally:
        con.close()


def camera_groups_step(db, label="Grouping cameras"):
    def run():
        groups = merge_identical_cameras(db)
        return [("log", f"{len(groups)} camera group(s): "
                        + ", ".join(f"{n} photos" for n in sorted(groups.values(), reverse=True)), 0)]
    return (label, run, 0)


# COLMAP camera model id -> indices of (x-scaled, y-scaled) params
_CAM_SCALE = {0: ([0, 1], [2]), 1: ([0, 2], [1, 3]), 2: ([0, 1], [2]), 3: ([0, 1], [2]),
              4: ([0, 2], [1, 3]), 6: ([0, 2], [1, 3]), 8: ([0, 1], [2]), 9: ([0, 1], [2])}


def _scale_cameras(con, factors):
    """factors: {camera_id: (sx, sy, new_w, new_h)}"""
    for cid, (sx, sy, w, h) in factors.items():
        model, blob = con.execute("SELECT model, params FROM cameras WHERE camera_id=?", (cid,)).fetchone()
        if model not in _CAM_SCALE:
            raise RuntimeError(f"camera model {model} not supported by the decode cache")
        p = np.frombuffer(blob, np.float64).copy()
        xs, ys = _CAM_SCALE[model]
        p[xs] *= sx; p[ys] *= sy
        con.execute("UPDATE cameras SET width=?, height=?, params=? WHERE camera_id=?",
                    (int(w), int(h), p.tobytes(), cid))


def rescale_db_to_original(db, dims, work_dir):
    """Key points were detected on downscaled working images: scale them (and
    the cameras) to the original photo resolution, exactly as COLMAP does
    internally with max_image_size, so models match the original photos."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT image_id, name, camera_id FROM images").fetchall()
        cams = {}
        for iid, name, cid in rows:
            d = dims.get(name)
            if not d:
                continue
            (W, H), (w, h) = d["orig"], d[work_dir]
            sx, sy = W / w, H / h
            cams.setdefault(cid, (sx, sy, W, H))
            kp = con.execute("SELECT rows, cols, data FROM keypoints WHERE image_id=?", (iid,)).fetchone()
            if not kp or not kp[0]:
                continue
            r, c, blob = kp
            a = np.frombuffer(blob, np.float32).reshape(r, c).copy()
            a[:, 0] *= sx; a[:, 1] *= sy
            if c == 4:
                a[:, 2] *= sx                      # scale
            elif c == 6:
                a[:, 2:4] *= sx; a[:, 4:6] *= sy   # affine shape
            con.execute("UPDATE keypoints SET data=? WHERE image_id=?", (a.tobytes(), iid))
        cur = {cid: con.execute("SELECT width, height FROM cameras WHERE camera_id=?", (cid,)).fetchone()
               for cid in cams}
        # only cameras still at working size (not already original) are scaled
        _scale_cameras(con, {cid: f for cid, f in cams.items() if tuple(cur[cid]) != (f[2], f[3])})
        con.commit()
    finally:
        con.close()


def set_cameras_to_work_size(db, dims, work_dir):
    """Before extracting into an existing database (fine stage), cameras must
    have the working-image size or COLMAP rejects the images."""
    con = sqlite3.connect(db)
    try:
        f = {}
        for name, cid in con.execute("SELECT name, camera_id FROM images").fetchall():
            d = dims.get(name)
            if d and cid not in f:
                (W, H), (w, h) = d["orig"], d[work_dir]
                f[cid] = (w / W, h / H, w, h)
        _scale_cameras(con, f)
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------ pair selection
def select_pairs(names, infos, k_desc=8, k_gps=8, window=3, use_desc=True, use_gps=True):
    """Return a sorted set of (name_a, name_b) image pairs to match.

    names : COLMAP image names (in the images folder)
    infos : list of analysis dicts aligned with names (may lack keys)
    """
    n = len(names)
    pairs = set()

    def add(i, j):
        if i != j:
            pairs.add((min(i, j), max(i, j)))
    order = sorted(range(n), key=lambda i: names[i])
    for a in range(n):          # sequential neighbours (capture order)
        for b in range(a + 1, min(n, a + 1 + window)):
            add(order[a], order[b])
    if use_gps:                 # spatial kNN from EXIF GPS
        idx = [i for i in range(n) if infos[i].get("lat") is not None]
        if len(idx) >= 2:
            lat0 = math.radians(np.mean([infos[i]["lat"] for i in idx]))
            xy = np.array([[infos[i]["lon"] * 111320 * math.cos(lat0),
                            infos[i]["lat"] * 110540,
                            infos[i].get("alt") or 0.0] for i in idx])
            d = np.linalg.norm(xy[:, None, :] - xy[None, :, :], axis=2)
            for a, i in enumerate(idx):
                for b in np.argsort(d[a])[1:k_gps + 1]:
                    add(i, idx[b])
    if use_desc:                # global descriptor top-k (cosine similarity)
        idx, D = [], []
        for i in range(n):
            p = infos[i].get("desc")
            if p and os.path.exists(p):
                idx.append(i); D.append(np.load(p))
        if len(idx) >= 2:
            D = np.stack(D)
            S = D @ D.T
            np.fill_diagonal(S, -np.inf)
            for a, i in enumerate(idx):
                for b in np.argsort(-S[a])[:k_desc]:
                    add(i, idx[b])
    return sorted((names[i], names[j]) for i, j in pairs)


def write_pairs(path, pairs):
    with open(path, "w", encoding="utf-8") as f:
        for a, b in pairs:
            f.write(f"{a} {b}\n")
    return path


def covisible_pairs(images_txt, min_shared=15, max_per_image=20):
    """Pairs of image names sharing >= min_shared 3D points in a model.
    Each image keeps at most its max_per_image strongest partners (a pair is
    kept if either image ranks it), so the count stays O(n) on dense grids."""
    names, obs = {}, {}
    with open(images_txt, encoding="utf-8", errors="ignore") as f:
        lines = [l for l in f if not l.startswith("#")]
    for i in range(0, len(lines) - 1, 2):
        head = lines[i].split()
        if len(head) < 10:
            continue
        iid = int(head[0]); names[iid] = head[9]
        vals = lines[i + 1].split()
        pids = [int(vals[k]) for k in range(2, len(vals), 3) if vals[k] != "-1"]
        for pid in pids:
            obs.setdefault(pid, []).append(iid)
    counts = {}
    for ims in obs.values():
        ims = sorted(set(ims))
        for a in range(len(ims)):
            for b in range(a + 1, len(ims)):
                counts[(ims[a], ims[b])] = counts.get((ims[a], ims[b]), 0) + 1
    strong = [(c, a, b) for (a, b), c in counts.items() if c >= min_shared]
    if max_per_image:
        per = {}
        for c, a, b in strong:
            per.setdefault(a, []).append((c, b)); per.setdefault(b, []).append((c, a))
        keep = set()
        for a, lst in per.items():
            for c, b in sorted(lst, reverse=True)[:max_per_image]:
                keep.add((min(a, b), max(a, b)))
        strong = [(c, a, b) for c, a, b in strong if (a, b) in keep]
    return {(names[a], names[b]) for c, a, b in strong}, set(names.values())


def write_known_pose_model(src_txt_dir, dst_dir):
    """Copy a TXT model keeping cameras + image poses but no observations
    (COLMAP's documented 'triangulate from known poses' input). Works with
    old (3.8) and new COLMAP, which check 2D point counts against the DB."""
    os.makedirs(dst_dir, exist_ok=True)
    import shutil
    shutil.copyfile(os.path.join(src_txt_dir, "cameras.txt"), os.path.join(dst_dir, "cameras.txt"))
    with open(os.path.join(src_txt_dir, "images.txt"), encoding="utf-8", errors="ignore") as f:
        lines = [l for l in f if not l.startswith("#")]
    with open(os.path.join(dst_dir, "images.txt"), "w", encoding="utf-8") as f:
        for i in range(0, len(lines) - 1, 2):
            if len(lines[i].split()) >= 10:
                f.write(lines[i].rstrip("\n") + "\n\n")
    open(os.path.join(dst_dir, "points3D.txt"), "w").close()
    return dst_dir


def db_image_ids(db):
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT name, image_id FROM images").fetchall())
    finally:
        con.close()


def model_num_images(model_dir):
    """Registered images in a COLMAP model folder (BIN or TXT), 0 if missing."""
    b = os.path.join(model_dir, "images.bin")
    if os.path.exists(b):
        import struct
        with open(b, "rb") as f:
            head = f.read(8)
        return struct.unpack("<Q", head)[0] if len(head) == 8 else 0
    t = os.path.join(model_dir, "images.txt")
    if os.path.exists(t):
        with open(t, encoding="utf-8", errors="ignore") as f:
            lines = [l for l in f if not l.startswith("#")]
        return sum(1 for i in range(0, len(lines), 2) if len(lines[i].split()) >= 10)
    return 0


def filter_step(exe, model):
    """Remove tie points with large reprojection error / tiny triangulation
    angle after the final bundle adjustment (as COLMAP's mapper does). This
    also refreshes the stored per-point errors, which bundle_adjuster does not
    update - without it the reported reprojection error is the pre-BA value."""
    from .colmap import Try, opt
    a = ["point_filtering", "--input_path", model, "--output_path", model]
    a += opt(exe, "point_filtering", ["max_reproj_error"], 4)
    a += opt(exe, "point_filtering", ["min_track_len"], 2)
    a += opt(exe, "point_filtering", ["min_tri_angle"], 1.5)
    return ("Filtering tie points", Try(a, None), 1)


# ------------------------------------------------------------ mappers
MAPPERS = ["Auto", "Global", "Incremental", "Hierarchical"]
AUTO_GLOBAL_MIN_IMAGES = 100     # below this incremental SfM is fast and most robust


def choose_mapper(s, n_images):
    """Resolve 'Auto' -> 'Global' (GLOMAP, when bundled) for large sets."""
    from .colmap import find_glomap
    m = s.get("mapper", "Auto")
    if m == "Global" and not find_glomap():
        return "Incremental"
    if m == "Auto":
        return "Global" if (n_images >= AUTO_GLOBAL_MIN_IMAGES and find_glomap()) else "Incremental"
    return m if m in MAPPERS else "Incremental"


def incremental_mapper_args(exe, db, img_dir, out, ilist=None, n_images=0, coarse=False):
    """COLMAP incremental mapper tuned for speed on large sets: all threads,
    global BA less often (it dominates run time: O(n) times a full problem)
    and with fewer iterations; local BA keeps quality between global BAs."""
    from .colmap import opt
    a = ["mapper", "--database_path", db, "--image_path", img_dir, "--output_path", out]
    if ilist:
        a += opt(exe, "mapper", ["image_list_path"], ilist)
    a += opt(exe, "mapper", ["Mapper.num_threads"], -1)
    big = n_images >= AUTO_GLOBAL_MIN_IMAGES
    if big:
        a += opt(exe, "mapper", ["Mapper.ba_global_images_ratio"], 1.4)
        a += opt(exe, "mapper", ["Mapper.ba_global_points_ratio"], 1.4)
        a += opt(exe, "mapper", ["Mapper.ba_global_images_freq"], 2000)
        a += opt(exe, "mapper", ["Mapper.ba_global_points_freq"], 1000000)
    a += opt(exe, "mapper", ["Mapper.ba_global_max_num_iterations"], 15 if coarse else (25 if big else 50))
    a += opt(exe, "mapper", ["Mapper.ba_global_max_refinements"], 2 if (coarse or big) else 5)
    a += opt(exe, "mapper", ["Mapper.ba_local_max_num_iterations"], 10 if coarse else 25)
    a += opt(exe, "mapper", ["Mapper.ba_local_max_refinements"], 2)
    return a


def mapper_steps(exe, db, img_dir, out, s, n_images, ilist=None, coarse=False, tag=""):
    """Camera-pose estimation steps for the selected mapper. GLOMAP and the
    hierarchical mapper fall back to the tuned incremental mapper if they fail."""
    from .colmap import GLOMAP, Try, opt
    kind = choose_mapper(s, n_images)
    inc = incremental_mapper_args(exe, db, img_dir, out, ilist, n_images, coarse)
    label = f"Estimating camera locations{tag}"

    def fallback():
        import shutil
        shutil.rmtree(out, ignore_errors=True); os.makedirs(out, exist_ok=True)
        return [("log", "falling back to the incremental mapper", 0),
                (label + " - incremental", inc, 25)]
    if kind == "Global":
        # GLOMAP 1.2 reading a COLMAP 3.8 database aborts in its final
        # re-triangulation (frame/rig check, legacy DB has no frames), so that
        # stage is skipped and done by COLMAP instead (fine stage or below).
        # Its BIN output is read fine by COLMAP 3.8 (verified: identical model
        # to TXT) and avoids ~100 s of TXT export on 600+ photos.
        g = [GLOMAP, "mapper", "--database_path", db, "--output_path", out,
             "--output_format", "bin", "--skip_retriangulation", "1",
             "--GlobalPositioning.use_gpu", "0", "--BundleAdjustment.use_gpu", "0"]
        if not coarse:   # coarse colours are never used: skip reading every full-size photo
            g[4:4] = ["--image_path", img_dir]
        steps = [("log", f"Mapper: global (GLOMAP) on {n_images} images", 0),
                 (label + " - global", Try(g, fallback), 25),
                 (f"Checking {tag.strip(' ()') or 'model'}", _check_model(out, n_images, fallback), 0)]
        if not coarse:
            steps.append(("Re-triangulating global model", _retriangulate(exe, db, img_dir, out), 0))
        return steps
    if kind == "Hierarchical":
        h = ["hierarchical_mapper", "--database_path", db, "--image_path", img_dir, "--output_path", out]
        h += opt(exe, "hierarchical_mapper", ["num_workers"], -1)
        h += opt(exe, "hierarchical_mapper", ["leaf_max_num_images"], max(100, min(500, n_images // 4)))
        h += opt(exe, "hierarchical_mapper", ["image_overlap"], 50)
        h += opt(exe, "hierarchical_mapper", ["Mapper.ba_global_max_num_iterations"], 15 if coarse else 25)
        h += opt(exe, "hierarchical_mapper", ["Mapper.ba_global_max_refinements"], 2)
        return [("log", f"Mapper: hierarchical (parallel sub-models) on {n_images} images", 0),
                (label + " - hierarchical", Try(h, fallback), 25),
                (f"Checking {tag.strip(' ()') or 'model'}", _check_model(out, n_images, fallback), 0)]
    return [("log", f"Mapper: incremental on {n_images} images", 0), (label + " - incremental", inc, 25)]


def _retriangulate(exe, db, img_dir, out):
    """Callable step: COLMAP re-triangulation + bundle adjustment of a GLOMAP
    model (keeps GLOMAP's model if either fails)."""
    from .colmap import Try, opt

    def plan():
        m0 = os.path.join(out, "0")
        if model_num_images(m0) == 0:
            return []
        txt = os.path.join(out, "glomap_txt")
        os.makedirs(txt, exist_ok=True)

        def tri_ba():
            if not os.path.exists(os.path.join(txt, "images.txt")):
                return []
            known = write_known_pose_model(txt, os.path.join(out, "known"))
            tri = ["point_triangulator", "--database_path", db, "--image_path", img_dir,
                   "--input_path", known, "--output_path", m0]
            tri += opt(exe, "point_triangulator", ["Mapper.num_threads"], -1)
            tri += opt(exe, "point_triangulator", ["Mapper.ba_global_function_tolerance"], BA_FUNCTION_TOLERANCE)
            ba = ["bundle_adjuster", "--input_path", m0, "--output_path", m0]
            ba += opt(exe, "bundle_adjuster", ["BundleAdjustment.max_num_iterations"], 30)
            ba += opt(exe, "bundle_adjuster", ["BundleAdjustment.function_tolerance"], BA_FUNCTION_TOLERANCE)
            return [("Triangulating tie points", Try(tri, None), 5),
                    ("Optimizing cameras (bundle adjustment)", Try(ba, None), 5),
                    filter_step(exe, m0)]
        return [("Reading global model", Try(["model_converter", "--input_path", m0, "--output_path", txt,
                                              "--output_type", "TXT"], None), 1),
                ("gate", tri_ba, 0)]
    return plan


def _check_model(out, n_images, fallback, min_frac=0.5):
    """Callable step: if the mapper wrote no usable model 0, run fallback()."""
    def check():
        got = model_num_images(os.path.join(out, "0"))
        if got >= max(2, int(min_frac * n_images)):
            return [("log", f"{got}/{n_images} images registered", 0)]
        return [("log", f"only {got}/{n_images} images registered", 0)] + fallback()
    return check


# ------------------------------------------------------------ step builders
def fast_align_steps(exe, wd, names, infos, s):
    """Build the Fast pipeline. Steps are (label, args|callable, weight).

    A callable step runs in-process and may return a list of steps that is
    inserted right after it (used for decisions that depend on results).
    """
    from .colmap import opt, ACCURACY_DOWNSCALE
    from .colmap import image_dir, image_list
    img_dir = image_dir(wd)
    ilist = image_list(wd)
    gpu = "1" if s.get("use_gpu", True) else "0"
    maxdim = max([i.get("width", 0) for i in infos] + [i.get("height", 0) for i in infos] + [0]) or 3200
    fine_size = max(256, maxdim // ACCURACY_DOWNSCALE[s["accuracy"]])
    do_fine = s.get("coarse_to_fine", True) and fine_size > COARSE_MAX_SIZE * 1.25
    coarse_size = min(COARSE_MAX_SIZE, fine_size)
    cdb, fdb = os.path.join(wd, "coarse.db"), os.path.join(wd, "database.db")
    csparse, sparse = os.path.join(wd, "sparse_coarse"), os.path.join(wd, "sparse")
    pairs_txt = os.path.join(wd, "pairs.txt")
    for p in (cdb, fdb):
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(csparse, exist_ok=True); os.makedirs(sparse, exist_ok=True)

    use_cache = s.get("decode_cache", True) and all(i.get("width") and i.get("height") for i in infos)
    work_f, work_c = os.path.join(wd, "_work_fine"), os.path.join(wd, "_work_coarse")
    dims = {}
    fine_kp = s["key_point_limit"]
    fine_keep = FINE_KEEP.get(s["accuracy"], 18000) if s.get("fast_feature_cap", True) else 0
    if fine_kp > 0 and fine_keep >= fine_kp:
        fine_keep = 0

    def decode_once():
        import shutil
        targets = {work_f: fine_size}
        if do_fine:
            targets[work_c] = coarse_size
        for d in targets:
            shutil.rmtree(d, ignore_errors=True)
        dims.update(build_work_images(names, img_dir, targets))
        bad = len(names) - len(dims)
        return [("log", f"Decoded {len(dims)} photos once into working images "
                        f"({', '.join(str(v) for v in targets.values())} px)"
                        + (f"; {bad} unreadable" if bad else ""), 0)]

    def rescale(db, work):
        def run():
            rescale_db_to_original(db, dims, work)
            return [("log", "Key points rescaled to original photo resolution", 0)]
        return run

    def cleanup():
        import shutil
        for d in (work_f, work_c):
            shutil.rmtree(d, ignore_errors=True)

    def extract(db, size, kp, src=None):
        # one camera per photo here; photos from the same physical camera are
        # merged afterwards (merge_identical_cameras) so mixed-camera sets work
        a = ["feature_extractor", "--database_path", db, "--image_path", src or img_dir,
             "--image_list_path", ilist, "--ImageReader.single_camera", "0"]
        a += opt(exe, "feature_extractor", ["FeatureExtraction.max_image_size", "SiftExtraction.max_image_size"], size)
        if kp > 0:
            a += opt(exe, "feature_extractor", ["SiftExtraction.max_num_features"], kp)
        a += opt(exe, "feature_extractor", ["FeatureExtraction.use_gpu", "SiftExtraction.use_gpu"], gpu)
        return a

    def import_matches(db, ptxt):
        a = ["matches_importer", "--database_path", db, "--match_list_path", ptxt, "--match_type", "pairs"]
        a += opt(exe, "matches_importer", ["FeatureMatching.use_gpu", "SiftMatching.use_gpu"], gpu)
        if s.get("guided_matching"):
            a += opt(exe, "matches_importer", ["FeatureMatching.guided_matching", "SiftMatching.guided_matching"], "1")
        return a

    def preselect():
        pairs = select_pairs(names, infos, k_desc=s.get("k_desc", 8), k_gps=s.get("k_gps", 8),
                             window=s.get("window", 3), use_desc=s.get("generic", True),
                             use_gps=s.get("reference", True))
        write_pairs(pairs_txt, pairs)
        total = len(names) * (len(names) - 1) // 2
        return [("log", f"Pair preselection: {len(pairs)} of {total} possible pairs", 0)]

    first_db, first_sparse = (cdb, csparse) if do_fine else (fdb, sparse)
    n = len(names)
    coarse_kp = min(COARSE_KEYPOINTS, s["key_point_limit"]) if s["key_point_limit"] > 0 else COARSE_KEYPOINTS
    first_work = work_c if do_fine else work_f
    steps = [("Selecting image pairs", preselect, 1)]
    if use_cache:
        steps.append(("Decoding photos (once, parallel)", decode_once, 6))
    steps += [
        (f"Detecting points ({'coarse ' if do_fine else ''}{coarse_size if do_fine else fine_size}px)",
         extract(first_db, coarse_size if do_fine else fine_size, coarse_kp if do_fine else fine_kp,
                 first_work if use_cache else None), 15)]
    if s.get("single_camera", True):
        steps.append(camera_groups_step(first_db))
    if use_cache:
        steps.append(("Rescaling key points", rescale(first_db, first_work), 1))
        if not do_fine:
            steps.append(("Removing working images", cleanup, 0))
    steps += [
        ("Matching points (preselected pairs)", import_matches(first_db, pairs_txt), 15),
    ] + mapper_steps(exe, first_db, img_dir, first_sparse, s, n, ilist, coarse=do_fine,
                     tag=" (coarse)" if do_fine else "")
    if do_fine:
        ctxt = os.path.join(wd, "sparse_coarse_txt")
        fine_pairs = os.path.join(wd, "pairs_fine.txt")
        os.makedirs(ctxt, exist_ok=True)

        def plan_fine():
            os.makedirs(ctxt, exist_ok=True)
            images_txt = os.path.join(ctxt, "images.txt")
            if not os.path.exists(images_txt):
                return [("log", "Coarse model missing - fine stage skipped", 0)]
            cov, registered = covisible_pairs(images_txt, s.get("min_shared", 15), s.get("max_pairs_per_image", 20))
            # unregistered images keep their preselected pairs
            pre = [tuple(l.split()) for l in open(pairs_txt, encoding="utf-8")]
            extra = {p for p in pre if p[0] not in registered or p[1] not in registered}
            pairs = sorted(cov | extra)
            write_pairs(fine_pairs, pairs)
            out = [("log", f"Coarse-to-fine: {len(registered)} images registered at {coarse_size}px; "
                            f"matching {len(pairs)} co-visible pairs at {fine_size}px", 0),
                   ("Matching points (co-visible pairs)", import_matches(fdb, fine_pairs), 15)]
            same_ids = all(db_image_ids(cdb).get(k) == v for k, v in db_image_ids(fdb).items())
            fine0 = os.path.join(sparse, "0")
            os.makedirs(fine0, exist_ok=True)
            missing = n - len(registered)

            def full_rerun():
                import shutil
                shutil.rmtree(sparse, ignore_errors=True); os.makedirs(sparse, exist_ok=True)
                return [("log", "Fine refinement of the coarse model failed - full re-alignment", 0)] + \
                    mapper_steps(exe, fdb, img_dir, sparse, s, n, ilist, tag=" (fine)")
            if not same_ids:
                return out + full_rerun()
            tri_img = img_dir
            if use_cache:
                # colours are sampled later from the working images
                # (color_model_from_work_images); an empty folder stops COLMAP
                # from re-reading every full-size photo
                tri_img = os.path.join(wd, "_no_images")
                os.makedirs(tri_img, exist_ok=True)
            out += fine_refine_steps(exe, fdb, tri_img, ctxt, wd, fine0, missing, n, len(registered),
                                     full_rerun)
            return out

        def prepare_fine_db():
            # Keep image/camera ids of the coarse model, drop its features,
            # so feature_extractor re-extracts at full accuracy with same ids.
            import shutil
            shutil.copyfile(cdb, fdb)
            con = sqlite3.connect(fdb)
            for t in ("keypoints", "descriptors", "matches", "two_view_geometries"):
                con.execute(f"DELETE FROM {t}")
            con.commit(); con.execute("VACUUM"); con.close()
            if use_cache:
                set_cameras_to_work_size(fdb, dims, work_f)

        steps += [
            ("Preparing fine database", prepare_fine_db, 1),
            ("Exporting coarse cameras", ["model_converter", "--input_path", os.path.join(csparse, "0"),
             "--output_path", ctxt, "--output_type", "TXT"], 1),
            (f"Detecting points (fine {fine_size}px)", extract(fdb, fine_size, fine_kp,
                                                                work_f if use_cache else None), 20),
        ]
        if use_cache:
            steps += [("Rescaling key points", rescale(fdb, work_f), 1)]
        if fine_keep:
            def sel():
                k = select_keypoints(fdb, fine_keep)
                return [("log", f"Kept {fine_keep} best features per photo ({k} photos reduced)", 0)]
            steps += [("Selecting fine key points", sel, 1)]
        steps += [
            ("Planning fine matching", plan_fine, 1),
        ]
    os.makedirs(os.path.join(wd, "sparse_txt"), exist_ok=True)
    if use_cache and do_fine:
        def colorize():
            n_col = color_model_from_work_images(os.path.join(sparse, "0"), work_f, dims)
            cleanup()
            return [("log", f"Coloured {n_col} tie points from the working images", 0)]
        steps.append(("Coloring tie points", colorize, 1))
    steps += [
        ("Exporting tie points", ["model_converter", "--input_path", os.path.join(sparse, "0"),
         "--output_path", os.path.join(wd, "tie_points.ply"), "--output_type", "PLY"], 1),
        ("Exporting cameras", ["model_converter", "--input_path", os.path.join(sparse, "0"),
         "--output_path", os.path.join(wd, "sparse_txt"), "--output_type", "TXT"], 1),
    ]
    return steps


def fine_refine_steps(exe, fdb, img_dir, coarse_txt, wd, fine0, missing, n, n_coarse, full_rerun):
    """Refine the coarse model with fine features instead of re-running SfM:
    keep coarse poses -> triangulate fine matches -> register the images the
    coarse stage missed -> triangulate again -> one bundle adjustment.
    full_rerun() steps are used only if this fails or loses images; once it
    is triggered the remaining refinement steps are skipped."""
    from .colmap import Try, opt
    known = write_known_pose_model(coarse_txt, os.path.join(wd, "sparse_known"))
    state = {"failed": False}

    def rerun():
        if state["failed"]:
            return []
        state["failed"] = True
        return full_rerun()

    def tri(inp, label):
        a = ["point_triangulator", "--database_path", fdb, "--image_path", img_dir,
             "--input_path", inp, "--output_path", fine0]
        a += opt(exe, "point_triangulator", ["Mapper.num_threads"], -1)
        # the final bundle_adjuster refines everything, so the triangulator's
        # internal BA/retriangulation loop only needs a short pass
        a += opt(exe, "point_triangulator", ["Mapper.ba_global_max_refinements"], 1)
        a += opt(exe, "point_triangulator", ["Mapper.ba_global_max_num_iterations"], 15)
        a += opt(exe, "point_triangulator", ["Mapper.ba_global_function_tolerance"], BA_FUNCTION_TOLERANCE)
        return (label, Try(a, rerun), 8)
    seq = [tri(known, "Triangulating fine tie points (coarse poses)")]
    if missing > 0:
        reg = ["image_registrator", "--database_path", fdb, "--input_path", fine0, "--output_path", fine0]
        reg += opt(exe, "image_registrator", ["Mapper.num_threads"], -1)

        def retri():
            # a second triangulation only helps if new images were added
            added = model_num_images(fine0) - n_coarse
            if added <= 0:
                return [("log", "No additional images registered - second triangulation skipped", 0)]
            return [("log", f"{added} additional image(s) registered", 0),
                    tri(fine0, "Triangulating tie points (all images)")]
        seq += [("log", f"Registering {missing} image(s) the coarse stage missed", 0),
                (f"Registering {missing} remaining images", Try(reg, None), 6),
                ("Checking registered images", retri, 0)]
    ba = ["bundle_adjuster", "--input_path", fine0, "--output_path", fine0]
    ba += opt(exe, "bundle_adjuster", ["BundleAdjustment.max_num_iterations"], 30)
    # stop when the relative cost change < 1e-6 (the drone run converged by
    # iteration ~10 of 39; the remaining iterations changed nothing)
    ba += opt(exe, "bundle_adjuster", ["BundleAdjustment.function_tolerance"], BA_FUNCTION_TOLERANCE)
    seq.append(("Optimizing cameras (bundle adjustment)", Try(ba, rerun), 7))
    seq.append(filter_step(exe, fine0))

    def check():
        if state["failed"]:
            return []
        got = model_num_images(fine0)
        if got >= max(2, int(0.95 * n_coarse)):
            return [("log", f"Fine model: {got}/{n} images registered "
                            f"({got - n_coarse:+d} vs coarse)", 0)]
        return [("log", f"Fine model lost images ({got} < {n_coarse})", 0)] + rerun()
    steps = [seq[0]]
    for st in seq[1:]:
        steps.append(st if st[0] == "log" else ("gate", (lambda st=st: [] if state["failed"] else [st]), 0))
    steps.append(("Checking fine model", check, 0))
    return steps


# ------------------------------------------------------------ headless runner
def _env_for(args):
    if args[:2] == ["-m", "photomesh.tasks"]:
        from .colmap import task_env
        return task_env()
    return None


def _run_logged(prog, args, label, logfile):
    if logfile:
        with open(logfile, "a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"\n===== {label}  [{time.strftime('%H:%M:%S')}]\n> {os.path.basename(prog)} {' '.join(args)}\n")
            lf.flush()
            r = subprocess.run([prog] + args, stdout=lf, stderr=subprocess.STDOUT, env=_env_for(args))
        if r.returncode != 0:
            with open(logfile, encoding="utf-8", errors="replace") as lf:
                tail = lf.read()[-2000:]
            raise RuntimeError(f"{label} failed (exit {r.returncode}):\n{tail}")
    else:
        r = subprocess.run([prog] + args, capture_output=True, text=True, env=_env_for(args))
        if r.returncode != 0:
            raise RuntimeError(f"{label} failed:\n{(r.stdout + r.stderr)[-2000:]}")


def run_steps(exe, steps, log=print, logfile=None):
    """Run steps synchronously (used by benchmark.py). Returns list of (label, seconds).
    COLMAP output is appended to logfile (if given) as it runs."""
    timings, queue = [], list(steps)
    while queue:
        label, action, _ = queue.pop(0)
        if label == "log":
            log(f"  [info] {action}")
            continue
        if label == "gate":           # conditional step: just expand it
            queue[0:0] = action() or []
            continue
        t0 = time.perf_counter()
        if callable(action):
            extra = action() or []
            queue[0:0] = extra
        else:
            from .colmap import resolve_cmd, Try
            try:
                prog, args = resolve_cmd(exe, action)
                _run_logged(prog, args, label, logfile)
            except Exception as e:
                if not isinstance(action, Try):
                    raise
                action.error = str(e).splitlines()[0][:200]
                m = re.search(r"exit (-?\d+)", str(e))
                action.exit_code = int(m.group(1)) if m else None
                action.output_tail = str(e).splitlines()[-40:]
                extra = action.fallback() if action.fallback else []
                log(f"  [info] '{label}' did not succeed ({str(e).splitlines()[0][:200]}) - "
                    + ("using fallback" if extra else "continuing"))
                queue[0:0] = extra
        dt = time.perf_counter() - t0
        timings.append((label, dt))
        log(f"  {label:<45s} {dt:7.2f} s")
    return timings
