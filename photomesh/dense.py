"""Dense point cloud (Build Point Cloud) on top of COLMAP's PatchMatch stereo.

What we do ourselves instead of COLMAP's image_undistorter:
  * photos are decoded once, in parallel, straight at the target resolution
    (JPEG DCT scaling via PIL draft) and undistorted per *camera group* with one
    precomputed remap table - each camera (e.g. Mavic 3 wide + tele) keeps its
    own calibration and is downscaled by the same quality factor
    (Metashape-like: Ultra high 1, High 2, Medium 4, Low 8, Lowest 16);
  * the source photos are read in place (never copied); only the downscaled
    undistorted images go to the project folder;
  * PatchMatch source images are chosen from the sparse model (shared tie
    points weighted by triangulation angle), at most `max_sources` per photo,
    instead of COLMAP's "__auto__" list based on shared points only.
Depth filtering (Disabled/Mild/Moderate/Aggressive) maps to PatchMatch filter
and stereo-fusion thresholds.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

import numpy as np

from . import colmap_io as cio

QUALITY_FACTOR = {"Ultra high": 1, "High": 2, "Medium": 4, "Low": 8, "Lowest": 16}

# PatchMatch filter + StereoFusion thresholds per depth-filtering mode
DEPTH_FILTERS = {
    "Disabled":   dict(filter_min_num_consistent=1, filter_min_ncc=0.05, min_num_pixels=2,
                       max_reproj_error=4.0, max_depth_error=0.03, max_normal_error=25),
    "Mild":       dict(filter_min_num_consistent=2, filter_min_ncc=0.1, min_num_pixels=3,
                       max_reproj_error=2.0, max_depth_error=0.01, max_normal_error=15),
    "Moderate":   dict(filter_min_num_consistent=2, filter_min_ncc=0.1, min_num_pixels=5,
                       max_reproj_error=2.0, max_depth_error=0.01, max_normal_error=10),
    "Aggressive": dict(filter_min_num_consistent=3, filter_min_ncc=0.15, min_num_pixels=8,
                       max_reproj_error=1.5, max_depth_error=0.005, max_normal_error=8),
}

PYTHON = "@python"   # step sentinel: run `python -m photomesh.tasks ...`


# ----------------------------------------------------------- source choice
def select_sources(model: cio.Model, max_sources=10, min_shared=20,
                   min_angle=2.0, max_angle=45.0, ideal_angle=10.0):
    """{image_id: [source image ids]} from the sparse model.

    score = shared tie points * w(angle), where angle is the triangulation
    angle at the scene (baseline vs. median depth of the reference photo):
    0 below min_angle / above max_angle, ramps up to 1 at ideal_angle.
    """
    from scipy import sparse
    P = model.points
    ids = sorted(model.images)
    lut = np.full(max(ids) + 1, -1, np.int64)
    lut[ids] = np.arange(len(ids))
    timg = P.track_img.astype(np.int64)
    r = np.where(timg < len(lut), lut[np.minimum(timg, len(lut) - 1)], -1)
    c = np.repeat(np.arange(len(P)), P.track_len())
    ok = r >= 0
    A = sparse.csr_matrix((np.ones(ok.sum(), np.float32), (r[ok], c[ok])),
                          shape=(len(ids), len(P)))
    order_ids = np.argsort(P.ids); sorted_ids = P.ids[order_ids]
    S = (A @ A.T).toarray(); np.fill_diagonal(S, 0)
    C = np.array([model.images[i].center() for i in ids])
    depth = np.empty(len(ids))
    for k, iid in enumerate(ids):
        im = model.images[iid]
        pid = im.point3D_ids[im.point3D_ids >= 0]
        if len(pid) == 0:
            depth[k] = np.nan; continue
        pid = pid[:: max(1, len(pid) // 500)]
        k2 = np.searchsorted(sorted_ids, pid.astype(np.uint64))
        k2 = k2[k2 < len(sorted_ids)]
        sel = order_ids[k2]
        z = (P.xyz[sel] @ im.R().T + im.tvec)[:, 2]
        depth[k] = np.median(z[z > 0]) if np.any(z > 0) else np.nan
    depth = np.where(np.isfinite(depth), depth, np.nanmedian(depth))
    base = np.linalg.norm(C[:, None] - C[None], axis=2)
    ang = np.degrees(2 * np.arctan(base / (2 * depth[:, None])))
    w = np.clip((ang - min_angle) / max(ideal_angle - min_angle, 1e-6), 0, 1)
    w[(ang < min_angle) | (ang > max_angle)] = 0
    score = S * (0.05 + w)          # keep tiny-angle neighbours as last resort
    score[S < min_shared] = 0
    out = {}
    for k, iid in enumerate(ids):
        order = np.argsort(-score[k])
        out[iid] = [ids[j] for j in order[:max_sources] if score[k, j] > 0]
    return out


# ---------------------------------------------------------- undistortion
def output_size(w, h, factor):
    return max(1, int(round(w / factor))), max(1, int(round(h / factor)))


def remap_table(cam: cio.Camera, factor, scale=1.0, offset=0.0, out=None,
                block_rows=None):
    """(rows, cols) float32 sampling positions for every pixel of the
    undistorted, downscaled PINHOLE image, in *full-resolution* pixel
    coordinates (scale=1, offset=0) or in the coordinates of an image that was
    downscaled by `scale` before the remap (pos * scale + offset).

    Computed in row blocks: float64 only for one block of rows at a time (the
    old version built ~10 full-size float64 temporaries, 159 MiB each at
    5280 x 3956, in every worker). `out`: optional (2, oh, ow) float32 array or
    memmap to fill (rows, cols) - e.g. a shared map file."""
    from .memguard import UNDISTORT_BLOCK_ROWS
    ow, oh = output_size(cam.width, cam.height, factor)
    sx, sy = ow / cam.width, oh / cam.height
    fx, fy, cx, cy = cam.K()
    u = (np.arange(ow) + 0.5 - cx * sx) / (fx * sx)
    if out is None:
        out = np.empty((2, oh, ow), np.float32)
    br = int(block_rows or UNDISTORT_BLOCK_ROWS)
    for r0 in range(0, oh, br):
        r1 = min(oh, r0 + br)
        v = (np.arange(r0, r1) + 0.5 - cy * sy) / (fy * sy)
        x = np.broadcast_to(u[None, :], (r1 - r0, ow)); y = np.broadcast_to(v[:, None], (r1 - r0, ow))
        xd, yd = cio.distort(cam, x, y)
        s_r, s_c = (scale, scale) if np.isscalar(scale) else scale
        out[0, r0:r1] = (fy * yd + cy) * s_r + offset
        out[1, r0:r1] = (fx * xd + cx) * s_c + offset
        del x, y, xd, yd
    return out[0], out[1]


def pinhole_camera(cam: cio.Camera, factor):
    ow, oh = output_size(cam.width, cam.height, factor)
    sx, sy = ow / cam.width, oh / cam.height
    fx, fy, cx, cy = cam.K()
    return cio.Camera(1, ow, oh, np.array([fx * sx, fy * sy, cx * sx, cy * sy]))


def decoded_size(path, target):
    """Size PIL's JPEG DCT scaling decodes `path` at for a `target` size
    (header only, no pixel decode)."""
    from PIL import Image
    with Image.open(path) as im:
        im.draft("RGB", target)
        return im.size


def work_size(cam: cio.Camera, factor):
    """The photo is reduced to the output size *before* the remap (JPEG DCT
    scaling + a box filter for the remainder), so the maps are computed at the
    downscaled size."""
    return output_size(cam.width, cam.height, factor)


def write_map(path, cam: cio.Camera, factor):
    """Float32 (2, oh, ow) remap table for one camera group in the coordinates
    of the downscaled work image (map_coordinates convention: pixel centres at
    integers), written block by block to a .npy that all workers memory-map
    (one copy in the OS page cache instead of one per worker)."""
    from numpy.lib.format import open_memmap
    ow, oh = output_size(cam.width, cam.height, factor)
    ww, wh = work_size(cam, factor)
    tmp = path + ".part.npy"
    mm = open_memmap(tmp, mode="w+", dtype=np.float32, shape=(2, oh, ow))
    # full-res position p -> work image p * (ww / W); centre convention -0.5
    remap_table(cam, factor, scale=(wh / cam.height, ww / cam.width), offset=-0.5, out=mm)
    mm.flush(); del mm
    os.replace(tmp, path)
    return path


def valid_output(path, size):
    """A finished undistorted JPEG of the expected size (resume): non-empty,
    complete (ends with the EOI marker) and readable."""
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as f:
            f.seek(-2, os.SEEK_END)
            if f.read(2) != b"\xff\xd9":
                return False
        from PIL import Image
        with Image.open(path) as im:
            return im.size == tuple(size)
    except Exception:
        return False


def _undistort_one(job):
    """job = (src, dst, map_path, (ww, wh) work size). Returns
    (dst, ok, message, error type, peak bytes of this process).

    Memory per photo: the decoded photo (uint8, already reduced by JPEG DCT
    scaling), one contiguous uint8 channel, the uint8 output; coordinates come
    from the shared float32 map in blocks of rows - no float64 arrays."""
    from PIL import Image
    from scipy.ndimage import map_coordinates
    from .memguard import process_peak_bytes, UNDISTORT_BLOCK_ROWS
    src, dst, map_path, (ww, wh) = job
    tmp = dst + ".part.jpg"
    try:
        M = np.load(map_path, mmap_mode="r")
        oh, ow = M.shape[1:]
        with Image.open(src) as im:
            im.draft("RGB", (ww, wh))
            im = im.convert("RGB")
            if im.size != (ww, wh):                        # remainder of the downscale (box filter)
                im = im.resize((ww, wh), Image.BOX)
            a = np.asarray(im)
            del im
        out = np.empty((oh, ow, 3), np.uint8)
        blk = np.empty((UNDISTORT_BLOCK_ROWS, ow), np.uint8)
        for ch in range(3):
            plane = np.ascontiguousarray(a[..., ch])
            for r0 in range(0, oh, UNDISTORT_BLOCK_ROWS):
                r1 = min(oh, r0 + UNDISTORT_BLOCK_ROWS)
                coords = np.asarray(M[:, r0:r1])           # (2, rows, ow) float32 block
                b = blk[:r1 - r0]
                map_coordinates(plane, coords, output=b, order=1, mode="constant", cval=0, prefilter=False)
                out[r0:r1, :, ch] = b
                del coords
            del plane
        del a, M
        img = Image.fromarray(out); del out
        img.save(tmp, "JPEG", quality=95)
        del img
        os.replace(tmp, dst)                               # never leave a half-written output
        return dst, True, "", "", process_peak_bytes()
    except BaseException as e:  # pragma: no cover - reported to the log
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, KeyboardInterrupt):
            raise
        return dst, False, f"{type(e).__name__}: {e}", type(e).__name__, process_peak_bytes()


def run_undistort(jobs, workers, log=print, progress=None, executor_factory=None):
    """Undistort `jobs` with a process pool; photos that fail with
    MemoryError (or whose worker died) are retried once, serially, after the
    pool is gone. Returns (failed messages, stats)."""
    import gc
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool
    import multiprocessing as mp
    from .memguard import PeakMonitor
    mon = PeakMonitor().start()
    bad, retry, peaks, done = [], [], [], 0
    n = len(jobs)
    try:
        if workers > 1 and n > 1:
            make = executor_factory or (lambda w: ProcessPoolExecutor(w, mp_context=mp.get_context("spawn")))
            pending = {}
            try:
                with make(workers) as ex:
                    pending = {ex.submit(_undistort_one, j): j for j in jobs}
                    for fut in as_completed(pending):
                        j = pending[fut]
                        try:
                            dst, ok, msg, kind, peak = fut.result()
                        except BrokenProcessPool:
                            retry.append(j); continue
                        except MemoryError:
                            retry.append(j); continue
                        peaks.append(peak)
                        if ok:
                            done += 1
                        elif kind == "MemoryError":
                            retry.append(j)
                        else:
                            bad.append(f"{os.path.basename(dst)}: {msg}")
                        if progress and (done % 10 == 0 or done == n):
                            progress(done, n)
            except BrokenProcessPool:
                retry += [j for f, j in pending.items() if not f.done() or f.exception() is not None]
            retry = list(dict.fromkeys(retry))
        else:
            for j in jobs:                                 # one worker: in this process
                dst, ok, msg, kind, peak = _undistort_one(j)
                peaks.append(peak)
                if ok:
                    done += 1
                elif kind == "MemoryError":
                    retry.append(j)
                else:
                    bad.append(f"{os.path.basename(dst)}: {msg}")
                if progress and (done % 10 == 0 or done == n):
                    progress(done, n)
        if retry:
            log(f"Retrying {len(retry)} photo(s) one at a time after running out of memory "
                f"in the parallel pass")
            for j in retry:
                gc.collect()
                dst, ok, msg, kind, peak = _undistort_one(j)
                if ok:
                    done += 1
                else:
                    bad.append(f"{os.path.basename(dst)}: {msg} (also failed when retried alone)")
                if progress:
                    progress(done, n)
    finally:
        mem = mon.stop()
    stats = {"done": done, "retried": len(retry), "worker_peak": max(peaks) if peaks else 0, **mem}
    return bad, stats


def is_inside(path, folder):
    """True if path is folder or below it (False for different drives)."""
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:      # different drives on Windows
        return False


def prepare_workspace(model_dir, image_root, dense_dir, quality="Medium", max_sources=10,
                      workers=None, log=print, progress=None):
    """Write a COLMAP dense workspace (images/, sparse/, stereo/*.cfg) to dense_dir."""
    factor = QUALITY_FACTOR[quality]
    model = cio.read_model(model_dir)
    if is_inside(dense_dir, image_root):     # check before creating anything
        raise RuntimeError("Dense workspace must not be inside the source photo folder")
    img_dir = os.path.join(dense_dir, "images")
    for d in ("images", "sparse", "stereo/depth_maps", "stereo/normal_maps",
              "stereo/consistency_graphs"):
        os.makedirs(os.path.join(dense_dir, d), exist_ok=True)
    # sparse model for the undistorted images
    new_cams = {cid: pinhole_camera(c, factor) for cid, c in model.cameras.items()}
    new_imgs = {}
    for iid, im in model.images.items():
        c, n = model.cameras[im.camera_id], new_cams[im.camera_id]
        s = np.array([n.width / c.width, n.height / c.height])
        new_imgs[iid] = cio.Image(im.name, im.camera_id, im.qvec, im.tvec, im.xys * s, im.point3D_ids)
    cio.write_model(os.path.join(dense_dir, "sparse"), cio.Model(new_cams, new_imgs, model.points))
    # PatchMatch sources from the sparse model
    src = select_sources(model, max_sources=max_sources)
    names = {iid: im.name for iid, im in model.images.items()}
    with open(os.path.join(dense_dir, "stereo", "patch-match.cfg"), "w", encoding="utf-8") as f:
        for iid in sorted(src):
            f.write(names[iid] + "\n")
            f.write(", ".join(names[j] for j in src[iid]) if src[iid] else "__auto__, 10")
            f.write("\n")
    with open(os.path.join(dense_dir, "stereo", "fusion.cfg"), "w", encoding="utf-8") as f:
        f.write("\n".join(names[i] for i in sorted(names)) + "\n")
    nsrc = [len(v) for v in src.values()]
    log(f"PatchMatch sources: median {int(np.median(nsrc))}, min {min(nsrc)} per photo "
        f"(max {max_sources}); quality {quality} = 1/{factor} resolution; "
        f"{len(model.cameras)} camera(s): " +
        ", ".join(f"{c.width}x{c.height}->{new_cams[k].width}x{new_cams[k].height}"
                  for k, c in model.cameras.items()))
    # undistort; resume: finished, valid outputs of an earlier (interrupted) run are kept
    from . import memguard as mg
    import time as _time
    for fn in os.listdir(img_dir):                         # leftovers of a killed run
        if fn.endswith(".part.jpg"):
            try:
                os.remove(os.path.join(img_dir, fn))
            except OSError:
                pass
    todo, kept = [], 0
    for iid, im in model.images.items():
        dst = os.path.join(img_dir, im.name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        n = new_cams[im.camera_id]
        if valid_output(dst, (n.width, n.height)):
            kept += 1
        else:
            todo.append((iid, im, dst))
    if kept:
        log(f"Reusing {kept} already undistorted photo(s); {len(todo)} to do")
    jobs, maps = [], {}
    map_dir = os.path.join(dense_dir, "undistort_maps")
    est = 0
    if todo:
        os.makedirs(map_dir, exist_ok=True)
        t0 = _time.perf_counter()
        for _iid, im, _dst in todo:
            cid = im.camera_id
            if cid not in maps:
                c = model.cameras[cid]
                ww, wh = work_size(c, factor)
                mp_ = os.path.join(map_dir, f"camera{cid}_{c.width}x{c.height}_f{factor}.npy")
                write_map(mp_, c, factor)                  # once per camera group
                try:
                    dw, dh = decoded_size(os.path.join(image_root, im.name), (ww, wh))
                except Exception:
                    dw, dh = c.width, c.height
                ow, oh = output_size(c.width, c.height, factor)
                maps[cid] = (mp_, (ww, wh))
                est = max(est, mg.undistort_worker_bytes(ww, wh, ow, oh, dw, dh))
            jobs.append((os.path.join(image_root, im.name), _dst, maps[cid][0], maps[cid][1]))
        log(f"Undistortion maps (float32, {len(maps)} camera group(s)) in {_time.perf_counter() - t0:.1f} s")
    want = workers or max(1, min(mg.MAX_WORKERS, (os.cpu_count() or 4)))
    avail = mg.available_bytes()
    nw = min(mg.cap_workers(want, est, available=avail), len(jobs)) if jobs else 0
    if jobs:
        log(f"Undistorting {len(jobs)} photo(s) with {nw} worker(s): ~{est / mg.MB:.0f} MB per worker "
            f"(estimate), {mg.fmt_gb(avail)} of {mg.fmt_gb(mg.total_bytes())} RAM available"
            + (f"; capped from {want} by free RAM" if nw < want else ""))
    bad, st = [], {}
    if jobs:
        bad, st = run_undistort(jobs, nw, log=log, progress=progress)
        log(f"Undistortion peak memory: {mg.fmt_gb(st.get('peak_tree'))} for all workers together "
            f"(largest worker {st.get('worker_peak', 0) / mg.MB:.0f} MB), lowest free RAM "
            f"{mg.fmt_gb(st.get('min_available'))}" + (f"; {st['retried']} photo(s) retried serially"
                                                         if st.get("retried") else ""))
    if bad:
        raise RuntimeError("Could not undistort: " + "; ".join(bad[:5])
                           + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""))
    import shutil
    shutil.rmtree(map_dir, ignore_errors=True)             # 2 x 4 bytes per output pixel: don't keep
    return {"images": len(model.images), "undistorted": len(jobs), "reused": kept, "factor": factor,
            "sources_median": float(np.median(nsrc)), "workers": nw, "memory": st}


def _fusion_names(dense_dir):
    cfg = os.path.join(dense_dir, "stereo", "fusion.cfg")
    if not os.path.exists(cfg):
        return []
    with open(cfg, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def _maps_exist(dense_dir, name, kind):
    st = os.path.join(dense_dir, "stereo")
    return all(os.path.isfile(os.path.join(st, sub, f"{name}.{kind}.bin")) and
               os.path.getsize(os.path.join(st, sub, f"{name}.{kind}.bin")) > 0
               for sub in ("depth_maps", "normal_maps"))


_HEADER = re.compile(rb"^(\d+)&(\d+)&(\d+)&")


def read_map_header(path):
    """(width, height, channels, header bytes) of a COLMAP depth/normal map
    ("W&H&C&" text header + float32 data), or None if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    m = _HEADER.match(head)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.end()


def map_problem(path, channels, expect=None):
    """None if the map file is valid, else a short reason. A COLMAP map that
    was cut off (disk full, process killed while writing) has an empty or
    short file; stereo_fusion then aborts with 'Check failed: width_ > 0'
    (exit 0xC0000409) and patch_match_stereo treats the file as finished."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "missing"
    if size == 0:
        return "empty file (0 bytes)"
    hd = read_map_header(path)
    if hd is None:
        return "no valid header"
    w, h, c, hl = hd
    if w <= 0 or h <= 0 or c != channels:
        return f"bad header {w}x{h}x{c}"
    want = hl + 4 * w * h * c
    if size != want:
        return f"truncated ({size:,} of {want:,} bytes)"
    if expect and (w > expect[0] or h > expect[1]):
        return f"size {w}x{h} larger than the photo {expect[0]}x{expect[1]}"
    return None


def view_problems(dense_dir, name, kind, expect=None):
    """Reasons why the depth+normal maps of one view/kind are unusable ([] = ok)."""
    st = os.path.join(dense_dir, "stereo")
    out = []
    for sub, ch in (("depth_maps", 1), ("normal_maps", 3)):
        r = map_problem(os.path.join(st, sub, f"{name}.{kind}.bin"), ch, expect)
        if r:
            out.append(f"{sub[:-5]} {r}")
    return out


def _view_sizes(dense_dir):
    """{image name: (width, height)} of the undistorted photos (dense/sparse)."""
    from . import colmap_io as cio
    try:
        cams = cio.read_cameras(os.path.join(dense_dir, "sparse", "cameras.bin"))
        imgs = cio.read_images(os.path.join(dense_dir, "sparse", "images.bin"))
    except Exception:
        return {}
    return {im.name: (cams[im.camera_id].width, cams[im.camera_id].height) for im in imgs.values()}


def check_depth_maps(dense_dir, kinds=("photometric", "geometric")):
    """Validate every view of fusion.cfg. Returns {kind: {"ok": [names],
    "missing": [names], "bad": {name: reason}}}."""
    names = _fusion_names(dense_dir)
    sizes = _view_sizes(dense_dir)
    res = {}
    for kind in kinds:
        ok, missing, bad = [], [], {}
        for n in names:
            probs = view_problems(dense_dir, n, kind, sizes.get(n))
            if not probs:
                ok.append(n)
            elif all(p.endswith("missing") for p in probs) and len(probs) == 2:
                missing.append(n)
            else:
                bad[n] = "; ".join(probs)
        res[kind] = {"ok": ok, "missing": missing, "bad": bad}
    return res


def remove_view_maps(dense_dir, names, kind):
    """Delete the (unusable) maps of these views so COLMAP recomputes them:
    patch_match_stereo skips a view as soon as its files EXIST."""
    st = os.path.join(dense_dir, "stereo"); n = 0
    for name in names:
        for sub in ("depth_maps", "normal_maps", "consistency_graphs"):
            f = os.path.join(st, sub, f"{name}.{kind}.bin")
            if os.path.exists(f):
                os.remove(f); n += 1
    return n


def read_patch_match_cfg(path):
    """[(ref name, sources line)] of stereo/patch-match.cfg."""
    with open(path, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]
    lines = [l for l in lines if l.strip()]
    return [(lines[i].strip(), lines[i + 1].strip() if i + 1 < len(lines) else "__auto__, 10")
            for i in range(0, len(lines), 2)]


def restrict_patch_match(dense_dir, todo):
    """Write a patch-match.cfg with only the views to recompute plus their
    source views (the geometric pass needs the sources' photometric maps).
    The full file is kept as patch-match.full.cfg (restored by
    restore_patch_match; prepare_workspace also rewrites it on every run)."""
    st = os.path.join(dense_dir, "stereo")
    full = os.path.join(st, "patch-match.full.cfg")
    cur = os.path.join(st, "patch-match.cfg")
    if not os.path.exists(full):
        import shutil
        shutil.copyfile(cur, full)
    pairs = read_patch_match_cfg(full)
    src = dict(pairs)
    keep = set(todo)
    for n in todo:
        line = src.get(n, "")
        if not line.startswith("__"):
            keep.update(x.strip() for x in line.split(",") if x.strip())
    with open(cur, "w", encoding="utf-8") as f:
        for ref, line in pairs:
            if ref in keep:
                f.write(ref + "\n" + line + "\n")
    return sorted(keep)


def restore_patch_match(dense_dir):
    st = os.path.join(dense_dir, "stereo")
    full = os.path.join(st, "patch-match.full.cfg")
    if os.path.exists(full):
        os.replace(full, os.path.join(st, "patch-match.cfg"))
        return True
    return False


def depth_map_counts(dense_dir):
    """(views, with photometric maps, with geometric maps): a view counts when
    both its depth and normal map exist (non-empty) - what fusion and a
    resumed patch_match_stereo look at."""
    names = _fusion_names(dense_dir)
    return (len(names), sum(_maps_exist(dense_dir, n, "photometric") for n in names),
            sum(_maps_exist(dense_dir, n, "geometric") for n in names))


def depth_maps_complete(dense_dir):
    """True if every view of fusion.cfg has its geometric depth AND normal map
    (stereo_fusion --input_type geometric reads both; the photometric maps
    and consistency graphs are not needed)."""
    names = _fusion_names(dense_dir)
    return bool(names) and all(_maps_exist(dense_dir, n, "geometric") for n in names)


def remove_photometric_maps(dense_dir):
    """Fusion only needs the geometric maps: free ~half the disk space."""
    n = 0
    for sub in ("depth_maps", "normal_maps"):
        d = os.path.join(dense_dir, "stereo", sub)
        for f in os.listdir(d) if os.path.isdir(d) else []:
            if f.endswith(".photometric.bin"):
                os.remove(os.path.join(d, f)); n += 1
    return n


# ------------------------------------------------------------- fusion plan
# COLMAP 3.8 stereo_fusion memory (src/mvs/fusion.cc, workspace.cc):
#  * use_cache=0 (COLMAP's default) loads EVERY image's RGB bitmap (3 B/px),
#    depth map (4 B/px) and normal map (12 B/px) before fusing: 19 B per
#    depth-map pixel x all images. At Ultra 640 x 5280x3956 that is ~254 GB
#    -> std::bad_alloc -> abort -> exit 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN).
#  * use_cache=1 keeps at most cache_size GB of images, but fuses in 1 thread.
#  * always: a 1 B/px "fused" mask for every image, and the fused points
#    (PlyPoint + visibility list, held twice for a moment at the end).
#  * max_image_size downsizes the maps while reading (fewer pixels, points).
FUSION_WS_BYTES_PER_PX = 19
FUSION_MASK_BYTES_PER_PX = 1
FUSED_POINT_BYTES = 140          # PlyPoint 28 + visibility vector ~70, x1.4 at the final merge
DOWNSTREAM_POINT_BYTES = 160     # Filtering point cloud: float64 xyz, normals, colours, KD-tree
POINTS_PER_PIXEL = 0.036         # fused points per depth-map pixel (bench_drone5 Medium: 0.034)
FUSION_PX_PER_THREAD_S = 0.28e6  # bench_drone5 Medium: 835 Mpx in 3.1 min with 16 threads
MAX_FUSION_S = 2 * 3600          # prefer the largest size that fuses within ~2 h
MIN_FUSION_SIZE = 1000           # never fuse below this image size (longer side, px)
FUSION_RETRIES = 3
GB = 1024 ** 3

# Windows NTSTATUS exit codes of a crashed COLMAP (as signed and unsigned int)
CRASH_CODES = {
    0xC0000409: "0xC0000409 (fail-fast / abort, usually an out-of-memory std::bad_alloc)",
    0xC0000005: "0xC0000005 (access violation)",
    0xC0000017: "0xC0000017 (out of memory)",
    0xC00000FD: "0xC00000FD (stack overflow)",
    0xE06D7363: "0xE06D7363 (unhandled C++ exception)",
}


def exit_code_text(code):
    if code is None:
        return ""
    try:
        c = int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return str(code)
    return CRASH_CODES.get(c, f"{int(code)}")


def looks_out_of_memory(code, tail=()):
    txt = "\n".join(tail or []).lower()
    c = (int(code) & 0xFFFFFFFF) if code is not None else None
    return c in (0xC0000409, 0xC0000017, 0xE06D7363) or "bad_alloc" in txt or "out of memory" in txt


def depth_map_size(dense_dir):
    """(n views, max width, max height, total pixels) of the depth maps
    (undistorted cameras of the views in fusion.cfg)."""
    names = _fusion_names(dense_dir)
    sizes = _view_sizes(dense_dir)
    dims = [sizes[n] for n in names if n in sizes] or list(sizes.values())
    w = max(d[0] for d in dims); h = max(d[1] for d in dims)
    return len(names), int(w), int(h), float(sum(float(a) * b for a, b in dims))


def fusion_sizes(w, h):
    """Candidate fusion sizes (longer side): full, then /sqrt(2) steps."""
    m = max(w, h); out = [m]
    while out[-1] / 2 ** 0.5 >= MIN_FUSION_SIZE:
        out.append(int(out[-1] / 2 ** 0.5))
    return out


def plan_fusion(n, w, h, available, threads=None, start=0, points_per_pixel=POINTS_PER_PIXEL,
                force_cache=False, total_px=None):
    """Memory-aware stereo_fusion settings, sizes from `start` (index into
    fusion_sizes) down. Per size two options: the in-memory multi-threaded
    workspace (fewer loading threads when maps are downsized, as each thread
    reads a full map first) and COLMAP's cached workspace, which fuses in ONE
    thread. Pick the largest size whose estimated time is <= MAX_FUSION_S
    (multi-threaded preferred at equal size), else the fastest that fits.
    Returns dict: index, size, max_image_size (-1 = full), use_cache,
    cache_gb, num_threads, peak, points, seconds (estimates), budget."""
    threads = int(threads or os.cpu_count() or 4)
    budget = max(1.0 * GB, 0.92 * float(available) - 0.75 * GB)
    sizes = fusion_sizes(w, h)
    full_px = float(w) * h
    tot = float(total_px) if total_px else n * full_px
    options = []
    for i in range(min(start, len(sizes) - 1), len(sizes)):
        m = sizes[i]
        k = (m / max(w, h)) ** 2
        img = full_px * k * FUSION_WS_BYTES_PER_PX       # largest view in the cache
        all_px = tot * k
        mask = all_px * FUSION_MASK_BYTES_PER_PX
        pts = points_per_pixel * all_px
        pts_b = pts * FUSED_POINT_BYTES
        common = dict(index=i, size=m, max_image_size=m if i else -1, points=pts, budget=budget)
        if pts * DOWNSTREAM_POINT_BYTES > budget:          # filtering could not hold the cloud
            continue
        per_thread = full_px * FUSION_WS_BYTES_PER_PX if i else 0
        base = mask + pts_b + all_px * FUSION_WS_BYTES_PER_PX
        nt = min(threads, n) if not per_thread else int(min(threads, n, max(0, budget - base) // per_thread))
        if not force_cache and base <= budget and nt >= min(4, threads):
            options.append(dict(common, use_cache=0, num_threads=nt, peak=base + nt * per_thread,
                                cache_gb=max(2, int(all_px * FUSION_WS_BYTES_PER_PX / GB) + 1),
                                seconds=all_px / (FUSION_PX_PER_THREAD_S * nt)))
        cache = budget - mask - pts_b - (full_px * FUSION_WS_BYTES_PER_PX if i else 0)
        if cache >= max(2 * GB, 20 * img):
            cg = max(2, int(min(cache, all_px * FUSION_WS_BYTES_PER_PX * 1.05 + GB) / GB))
            options.append(dict(common, use_cache=1, num_threads=1, cache_gb=cg, peak=mask + pts_b + cg * GB,
                                seconds=all_px / FUSION_PX_PER_THREAD_S * (1.0 if cg * GB >= all_px * 19 else 1.3)))
    if options:
        ok = [o for o in options if o["seconds"] <= MAX_FUSION_S]
        if ok:
            return min(ok, key=lambda o: (o["index"], o["use_cache"]))
        return min(options, key=lambda o: o["seconds"])
    i = len(sizes) - 1; m = sizes[i]; k = (m / max(w, h)) ** 2
    return dict(index=i, size=m, max_image_size=m, use_cache=1, num_threads=1, cache_gb=2,
                points=points_per_pixel * tot * k, budget=budget, last_resort=True,
                peak=tot * k * (1 + points_per_pixel * FUSED_POINT_BYTES) + 2 * GB,
                seconds=tot * k / FUSION_PX_PER_THREAD_S)


def describe_plan(p, n, w, h, available):
    size = "full resolution" if p["max_image_size"] < 0 else \
        f"max {p['max_image_size']} px (depth maps {w}x{h} downsized)"
    mode = (f"cached workspace {p['cache_gb']} GB, 1 thread" if p["use_cache"]
            else f"all {n} depth maps in memory, {p['num_threads']} threads")
    from .progress import fmt_eta
    return (f"Fusion plan: {n} depth maps at {size}; {mode}; estimated peak {p['peak'] / GB:.1f} GB, "
            f"~{p['points'] / 1e6:.0f} M points, ~{fmt_eta(p.get('seconds'))}; "
            f"{available / GB:.1f} GB RAM available")


def fusion_args(base, p):
    return list(base) + ["--StereoFusion.max_image_size", str(p["max_image_size"]),
                         "--StereoFusion.use_cache", "1" if p["use_cache"] else "0",
                         "--StereoFusion.cache_size", str(p["cache_gb"]),
                         "--StereoFusion.num_threads", str(p["num_threads"])]


def fusion_steps(base, dense, label="Building point cloud (fusion)", available=None, attempt=0,
                 start=0, force_cache=False, history=None):
    """Steps for stereo_fusion with a memory plan and automatic retries: after
    a crash (e.g. 0xC0000409 = out of memory) the next attempt uses the cached
    workspace, then smaller image sizes. The depth maps are never touched."""
    from . import memguard as mg
    from .colmap import Try
    from .progress import FusionMeter
    history = list(history or [])
    n, w, h, tpx = depth_map_size(dense)
    avail = mg.available_bytes() if available is None else available
    ppp = POINTS_PER_PIXEL * (1.5 ** attempt)        # each retry assumes more points
    p = plan_fusion(n, w, h, avail, start=start, points_per_pixel=ppp, force_cache=force_cache, total_px=tpx)
    out = os.path.join(dense, "fused.ply")
    for fn in (out, out + ".vis"):                    # stale or partial output
        if os.path.exists(fn):
            os.remove(fn)
    args = Try(fusion_args(base, p), meter=FusionMeter(n))
    sizes = fusion_sizes(w, h)

    def retry():
        code = args.exit_code
        why = exit_code_text(code) or args.error or "unknown error"
        hist = history + [f"attempt {attempt + 1}: exit code {why}"]
        crashed = code is None or (int(code) & 0xFFFFFFFF) >= 0xC0000000 or \
            looks_out_of_memory(code, args.output_tail)
        if p["use_cache"]:
            nxt, cache = p["index"] + 1, True
        else:
            nxt, cache = p["index"], True             # same size, cached workspace first
        if not crashed or attempt + 1 > FUSION_RETRIES or nxt >= len(sizes):
            raise RuntimeError(
                "COLMAP stereo_fusion stopped (" + "; ".join(hist) + "). The depth maps are kept: "
                "close other programs and run Build Point Cloud again with the same settings and "
                "'Reuse depth maps' checked - only the fusion runs again. See Console for details.")
        return [("log", f"Fusion attempt {attempt + 1} failed (exit code {why}); retrying with less "
                        "memory - the depth maps are kept", 0),
                ("gate", lambda: fusion_steps(base, dense, label, None, attempt + 1, nxt, cache, hist), 0)]
    args.fallback = retry
    return [("log", describe_plan(p, n, w, h, avail) + (f" (retry {attempt})" if attempt else ""), 0),
            (label, args, 15)]


# ------------------------------------------------------------- vis file
def read_vis_counts(path):
    """Number of images each fused point was seen in (COLMAP fused.ply.vis:
    uint64 n, then per point uint32 count + count uint32 image indices)."""
    from array import array
    a = array("I")
    with open(path, "rb") as f:
        a.frombytes(f.read())
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    n = a[0] | (a[1] << 32)
    counts = array("i", bytes(4 * n))
    pos = 2
    for i in range(n):
        c = a[pos]; counts[i] = c; pos += 1 + c
    return np.frombuffer(counts, np.int32).copy()


# ------------------------------------------------------------ the steps
UNDISTORT_KEYS = ("model", "image_root", "dense", "quality")


def model_signature(model_dir):
    """(size, mtime) of the sparse model files: the undistorted photos depend
    on the cameras and poses, so a re-alignment invalidates them."""
    sig = []
    for fn in ("cameras.bin", "images.bin"):
        p = os.path.join(model_dir, fn)
        try:
            st = os.stat(p); sig.append([fn, st.st_size, int(st.st_mtime)])
        except OSError:
            sig.append([fn, None, None])
    return sig


def workspace_reuse(marker, cfg, reuse_depth):
    """(keep undistorted photos, keep depth maps) for a rerun.

    The undistorted photos are kept whenever the model, photo folder and
    quality are unchanged - also when 'Reuse depth maps' is off (resume after
    a failed or cancelled undistortion). The depth maps are kept only with
    'Reuse depth maps' and identical settings. A marker written before the
    model signature existed counts as matching when the model files are older
    than the marker."""
    if not os.path.exists(marker):
        return True, False          # nothing recorded: per-photo validity check decides
    try:
        old = json.load(open(marker, encoding="utf-8"))
    except Exception:
        return False, False
    same = all(old.get(k) == cfg.get(k) for k in UNDISTORT_KEYS)
    if same:
        if "model_sig" in old:
            same = old["model_sig"] == cfg.get("model_sig")
        else:                       # legacy marker: model unchanged since it was written?
            try:
                mt = os.path.getmtime(marker)
                same = all(s[2] is not None and s[2] <= mt for s in cfg.get("model_sig") or [])
            except OSError:
                same = False
    keep_depth = same and reuse_depth and all(old.get(k) == cfg.get(k) for k in cfg if k != "model_sig") \
        and old.get("model_sig", cfg.get("model_sig")) == cfg.get("model_sig")
    return same, keep_depth


DEPTH_DIRS = ("depth_maps", "normal_maps", "consistency_graphs")     # below dense/stereo
DEPTH_INFO = "depth_maps_info.json"                                   # dense/: what was removed


def ply_vertex_count(path):
    """Vertices of a PLY file whose header is complete and whose body is
    present (size > header), else 0."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return 0
    end = head.find(b"end_header")
    if not head.startswith(b"ply") or end < 0:
        return 0
    n = 0
    for line in head[:end].decode("ascii", "ignore").splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "element" and p[1] == "vertex":
            try:
                n = int(p[2])
            except ValueError:
                return 0
    body = head.find(b"\n", end)
    return n if n > 0 and body >= 0 and size > body + 1 else 0


def depth_maps_size(dense_dir):
    """(bytes, files) of the depth/normal maps and consistency graphs."""
    total = n = 0
    for sub in DEPTH_DIRS:
        d = os.path.join(dense_dir, "stereo", sub)
        if not os.path.isdir(d):
            continue
        for e in os.scandir(d):
            if e.is_file(follow_symlinks=False):
                n += 1
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return total, n


def remove_depth_maps(dense_dir, require_cloud=True):
    """Delete the depth/normal maps and consistency graphs ('Keep depth maps'
    off). With require_cloud, only when dense/point_cloud.ply is a complete
    PLY with points. Returns (removed files, freed bytes)."""
    import shutil
    if require_cloud and not ply_vertex_count(os.path.join(dense_dir, "point_cloud.ply")):
        return 0, 0
    size, n = depth_maps_size(dense_dir)
    count = len([f for f in os.listdir(os.path.join(dense_dir, "stereo", "depth_maps"))
                 if f.endswith(".geometric.bin")]) if os.path.isdir(os.path.join(dense_dir, "stereo", "depth_maps")) else 0
    for sub in DEPTH_DIRS:
        d = os.path.join(dense_dir, "stereo", sub)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)              # COLMAP workspace layout stays valid
    if n:
        try:
            with open(os.path.join(dense_dir, DEPTH_INFO), "w", encoding="utf-8") as fh:
                json.dump({"removed": True, "maps": count, "bytes": size}, fh)
        except OSError:
            pass
    return n, size


DEPTH_BYTES_PER_PX = 4 * (1 + 3) * 2     # photometric + geometric, depth (1) + normal (3) float32


def disk_needed(dense_dir, todo_photometric, todo_geometric):
    """Bytes patch_match_stereo will write for these views."""
    sizes = _view_sizes(dense_dir)
    px = lambda n: float(sizes.get(n, (0, 0))[0]) * sizes.get(n, (0, 0))[1]
    return 16 * (sum(px(n) for n in todo_photometric) + sum(px(n) for n in todo_geometric))


def dense_steps(exe, wd, s, image_root, opt=None, disk_free=None):
    """Steps for Build Point Cloud. s: quality, depth_filtering, max_sources,
    reuse, keep_photometric, keep_depth_maps, filters (dict for pcfilter, optional).

    keep_depth_maps False (default, like Metashape): after the point cloud is
    written and verified, the depth/normal maps and consistency graphs are
    deleted (at Ultra high they are ~16 bytes per photo pixel, e.g. ~200 GB
    for 640 x 20 MPix photos). 'Reuse depth maps' then simply recomputes them.

    Resume: with 'Reuse depth maps' (or automatically, when the previous run
    with identical settings ended before the point cloud was written) every
    valid geometric depth+normal map is kept; corrupt/empty/truncated maps are
    removed and only the missing views are recomputed (restricted
    patch-match.cfg: those views plus their sources); fusion then gets a
    memory plan and automatic retries (fusion_steps)."""
    import shutil
    from .colmap import Cmd
    from .progress import PatchMatchMeter
    dense = os.path.join(wd, "dense")
    quality = s.get("quality", "Medium")
    mode = s.get("depth_filtering", "Mild")
    f = DEPTH_FILTERS[mode]
    os.makedirs(dense, exist_ok=True)
    cfg = {"model": os.path.join(wd, "sparse", "0"), "image_root": image_root, "dense": dense,
           "quality": quality, "max_sources": int(s.get("max_sources", 8))}
    marker = os.path.join(dense, "workspace.json")
    cfg_marker = dict(cfg, model_sig=model_signature(cfg["model"]))
    reuse = bool(s.get("reuse"))
    notes = []
    if not reuse:
        # previous run with the same settings stopped after (part of) the depth
        # maps, before the point cloud: resume instead of hours of recomputing
        _same, could = workspace_reuse(marker, cfg_marker, True)
        if could and not os.path.exists(os.path.join(dense, "point_cloud.ply")) and \
                os.path.isdir(os.path.join(dense, "stereo", "depth_maps")) and \
                any(fn.endswith(".geometric.bin") for fn in os.listdir(os.path.join(dense, "stereo", "depth_maps"))):
            reuse = True
            notes.append(("log", "The previous Build Point Cloud with these settings did not finish: "
                                 "reusing its depth maps (resume)", 0))
    keep_images, keep_depth = workspace_reuse(marker, cfg_marker, reuse)
    if not keep_images or not keep_depth:
        for sub in (("images", "sparse", "stereo") if not keep_images else ("stereo",)):
            shutil.rmtree(os.path.join(dense, sub), ignore_errors=True)
        for fn in ("fused.ply", "fused.ply.vis", "point_cloud.ply"):
            if os.path.exists(os.path.join(dense, fn)):
                os.remove(os.path.join(dense, fn))
    cfg_path = os.path.join(dense, "prepare.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    pm = ["patch_match_stereo", "--workspace_path", dense,
          "--PatchMatchStereo.geom_consistency", "1",
          "--PatchMatchStereo.filter", "1",
          "--PatchMatchStereo.filter_min_num_consistent", str(f["filter_min_num_consistent"]),
          "--PatchMatchStereo.filter_min_ncc", str(f["filter_min_ncc"]),
          "--PatchMatchStereo.window_radius", str(s.get("window_radius", 5)),
          "--PatchMatchStereo.window_step", str(s.get("window_step", 2)),
          "--PatchMatchStereo.num_iterations", str(s.get("num_iterations", 3)),
          "--PatchMatchStereo.num_samples", str(s.get("num_samples", 15)),
          "--PatchMatchStereo.cache_size", str(s.get("cache_gb", 12)),
          "--PatchMatchStereo.min_triangulation_angle", "1"]
    fu = ["stereo_fusion", "--workspace_path", dense, "--input_type", "geometric",
          "--output_path", os.path.join(dense, "fused.ply"),
          "--StereoFusion.min_num_pixels", str(f["min_num_pixels"]),
          "--StereoFusion.max_reproj_error", str(f["max_reproj_error"]),
          "--StereoFusion.max_depth_error", str(f["max_depth_error"]),
          "--StereoFusion.max_normal_error", str(f["max_normal_error"])]
    filt = dict(s.get("filters") or {})
    fcfg = os.path.join(dense, "filter.json")
    with open(fcfg, "w", encoding="utf-8") as fh:
        json.dump({"input": os.path.join(dense, "fused.ply"),
                   "vis": os.path.join(dense, "fused.ply.vis"),
                   "output": os.path.join(dense, "point_cloud.ply"),
                   "model": os.path.join(wd, "sparse", "0"), **filt}, fh)

    def after_pm():
        out = []
        if restore_patch_match(dense):
            out.append(("log", "Restored the full patch-match.cfg", 0))
        if not s.get("keep_photometric"):
            n = remove_photometric_maps(dense)
            out.append(("log", f"Removed {n} photometric maps (fusion uses the geometric ones)", 0))
        return out

    def maybe_pm():
        restore_patch_match(dense)                   # a crashed repair run
        names = _fusion_names(dense)
        st = check_depth_maps(dense)
        out = []
        for kind in ("photometric", "geometric"):
            bad = st[kind]["bad"]
            if bad:
                ex = ", ".join(f"{k} ({v})" for k, v in list(bad.items())[:5])
                out.append(("log", f"{len(bad)} unusable {kind} map(s) removed, they are recomputed: {ex}"
                                   + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""), 0))
                remove_view_maps(dense, bad, kind)
        geo_ok = set(st["geometric"]["ok"])
        todo_g = [n for n in names if n not in geo_ok]
        if reuse and not todo_g:
            return out + [("log", f"Reusing all {len(names)} existing depth maps", 0)]
        pho_ok = set(st["photometric"]["ok"])
        refs = names
        if reuse and geo_ok:
            refs = restrict_patch_match(dense, todo_g)
            out.append(("log", f"Reusing {len(geo_ok)} valid depth maps; recomputing {len(todo_g)} view(s) "
                               f"({len(refs)} incl. their source photos): {', '.join(todo_g[:8])}"
                               + (" ..." if len(todo_g) > 8 else ""), 0))
        todo_p = [n for n in refs if n not in pho_ok]
        todo_gr = [n for n in refs if n not in geo_ok]
        need = disk_needed(dense, todo_p, todo_gr) + 2 * GB
        free = shutil.disk_usage(dense).free if disk_free is None else disk_free
        if free < need:
            restore_patch_match(dense)
            raise RuntimeError(
                f"Not enough disk space for the depth maps: about {need / GB:.0f} GB are needed in {dense}, "
                f"{free / GB:.0f} GB are free. Free disk space (or move the project to a larger drive) and "
                "run Build Point Cloud again with 'Reuse depth maps' checked - finished depth maps are kept.")
        out.append(("log", f"Depth maps to compute: {len(todo_p)} photometric + {len(todo_gr)} geometric; "
                           f"disk: ~{need / GB:.0f} GB needed, {free / GB:.0f} GB free", 0))
        meter = PatchMatchMeter(len(todo_p), len(todo_gr), len(refs))
        return out + [("Building depth maps", Cmd(pm, meter=meter), 70), ("Cleaning up depth maps", after_pm, 1)]

    def fusion_gate():
        names = _fusion_names(dense)
        st = check_depth_maps(dense, ("geometric",))["geometric"]
        good = st["ok"]
        if not good:
            raise RuntimeError("No valid depth maps to fuse (see Console). Run Build Point Cloud again "
                               "with 'Reuse depth maps' checked to recompute the missing ones.")
        out = []
        if len(good) < len(names):
            skip = [n for n in names if n not in set(good)]
            reasons = {**{n: "missing" for n in st["missing"]}, **st["bad"]}
            with open(os.path.join(dense, "stereo", "fusion.cfg"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(good) + "\n")
            out.append(("log", f"Fusion skips {len(skip)} view(s) without valid depth maps: "
                               + ", ".join(f"{n} ({reasons.get(n, '?')})" for n in skip[:8])
                               + (" ..." if len(skip) > 8 else ""), 0))
        return out + fusion_steps(fu, dense)
    fusion_gate.fusion_base = fu

    def drop_depth_maps():
        n, size = remove_depth_maps(dense)
        if not n:
            return [("log", "Depth maps kept: the point cloud could not be verified", 0)] \
                if depth_maps_size(dense)[1] else []
        return [("log", f"Removed the depth maps ({n} files, {size / GB:.1f} GB); "
                        "check 'Keep depth maps' to keep them for 'Reuse depth maps'", 0)]

    with open(marker, "w", encoding="utf-8") as fh:
        json.dump(cfg_marker, fh)
    try:
        os.remove(os.path.join(dense, DEPTH_INFO))
    except OSError:
        pass
    tail = [] if s.get("keep_depth_maps") else [("Removing depth maps", drop_depth_maps, 1)]
    return notes + [
        ("Undistorting photos (parallel, downscaled)", [PYTHON, "dense_prepare", cfg_path], 8),
        ("gate", maybe_pm, 0),
        ("gate", fusion_gate, 0),
        ("Filtering point cloud", [PYTHON, "filter_cloud", fcfg], 6),
    ] + tail
