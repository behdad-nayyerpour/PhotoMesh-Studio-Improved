"""Build DEM / Build Orthomosaic / Export (own implementation, numpy + scipy +
Open3D's Embree ray caster; no third-party photogrammetry code).

Frames
------
All processing happens in the *projection frame*: for a georeferenced chunk
that is the chunk's local frame (world - offset, z up), so X/Y are the
project CRS easting/northing minus the offset. For a chunk that is not
georeferenced, the projection frame is the local frame rotated so that z is
the mean "up" of the cameras (top view). Every file written (GeoTIFF tags,
world files, DEM heights) carries REAL world coordinates: local + offset.

Raster grid: pixel (row r, col c) covers X in [x0 + c*px, x0 + (c+1)*px],
Y in [y1 - (r+1)*px, y1 - r*px]; (x0, y1) = upper-left corner, snapped to a
multiple of the pixel size in world coordinates.

Build Orthomosaic
-----------------
1. Surface: the mesh (or the DEM, triangulated) in an Embree scene. A vertical
   ray per output pixel gives the surface height (DSM) - the photo point to
   sample is (X, Y, height).
2. Coarse grid (16 px cells): for every camera, the cells in its footprint
   get a score = cos^4(off-nadir angle) x image-border fade x resolution
   (most nadir, closest, centre of the photo). Top 6 views per cell; an
   occlusion ray cell -> camera removes hidden views.
3. Colours of the candidates at the cells (1/8 scale photos): per-photo
   exposure gains (least squares in log space), ghosting filter (views that
   disagree with the median colour: moving cars, people).
4. View selection: Potts model on the cell grid solved by ICM (large single-
   photo patches). Refine seamlines adds a seam cost proportional to the
   colour difference of the two photos, so seams run where they agree.
5. Rendering block by block (2048 px blocks, bounded memory, threads): each
   pixel is projected into its view through the full COLMAP camera model
   (lens distortion of each physical camera), tested for occlusion against
   the surface and sampled bilinearly from the photo (falls back to the next
   candidate when hidden / outside). Mosaic blending = two bands: the low
   frequencies are the weighted average of all visible photos (interpolated
   from the cells), the high frequencies come from the selected photo only,
   so seams vanish without ghosting or blur. Average = per-pixel weighted mean
   of the visible photos; Disabled = selected photo, no colour correction.
6. Hole filling: small surface holes get interpolated heights, pixels no
   photo sees are filled from neighbouring pixels. Outside the surface:
   alpha = 0.
7. Output: tiled GeoTIFF (Deflate, RGBA, overviews) + height grid (1/4
   resolution, float32) used for the cursor Z read-out, info.json.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import geotiff as gt

ORTHO_DEFAULTS = {"surface": "Mesh", "blending": "Mosaic", "hole_filling": True, "ghosting": True,
                  "refine_seamlines": False, "pixel_size": 0.0, "max_dimension": 0, "region": None,
                  "block": 2048, "tile": 512, "cell_px": 16, "top_k": 6, "threads": 0, "max_res": 0,
                  "cache_mb": 3000, "height_factor": 4, "seam_beta": 2.0, "smooth": 0.8,
                  "max_pixels": 3_000_000_000}
DEM_DEFAULTS = {"source": "Point cloud", "interpolation": "Enabled", "pixel_size": 0.0, "region": None,
                "max_fill": 10.0, "tile": 512, "max_pixels": 400_000_000}
BLENDING = ("Mosaic", "Average", "Disabled")
NODATA_DEM = -32767.0


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ frames / grids
def georef_offset(georef):
    off = (georef or {}).get("offset") if isinstance(georef, dict) else None
    return np.asarray(off if off is not None else [0, 0, 0], np.float64).reshape(3)


def projection_rotation(views, georef):
    """3x3 R: X_proj = R @ X_local (identity when georeferenced)."""
    if georef:
        return np.eye(3)
    from .texture import up_vector
    up = up_vector(views, False)
    a = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = a - (a @ up) * up; e1 /= np.linalg.norm(e1)      # local x projected on the ground plane
    e2 = np.cross(up, e1)
    return np.stack([e1, e2, up])


def make_grid(xmin, ymin, xmax, ymax, px, off_xy=(0.0, 0.0)):
    """(x0, y1, W, H) in the projection frame with x0/y1 snapped to multiples
    of px in WORLD coordinates (local + off)."""
    ox, oy = off_xy
    x0w = math.floor((xmin + ox) / px + 1e-9) * px
    y1w = math.ceil((ymax + oy) / px - 1e-9) * px
    W = max(1, int(math.ceil((xmax + ox - x0w) / px - 1e-9)))
    H = max(1, int(math.ceil((y1w - (ymin + oy)) / px - 1e-9)))
    return x0w - ox, y1w - oy, W, H


def pixel_to_world(info, col, row):
    """Continuous pixel coordinates (col, row; 0,0 = upper-left corner of the
    raster) -> REAL world X, Y (offset included)."""
    x0, y1 = info["origin"]
    px = info["pixel_size"]
    return x0 + np.asarray(col, float) * px, y1 - np.asarray(row, float) * px


def world_to_pixel(info, X, Y):
    x0, y1 = info["origin"]
    px = info["pixel_size"]
    return (np.asarray(X, float) - x0) / px, (y1 - np.asarray(Y, float)) / px


def nice(v):
    """Round to 3 significant digits."""
    if v <= 0:
        return v
    e = math.floor(math.log10(v)) - 2
    return round(v / 10 ** e) * 10 ** e



# ------------------------------------------------------------------ extent / size planning
def footprint_bounds(cams, R, zground, margin=0.25):
    """Bounding box (xmin, ymin, xmax, ymax) in the projection frame of the photo
    footprints on the plane z = zground. cams: [(C_local, R_cam (world->cam),
    (fx, fy, cx, cy), W, H)]. Each footprint is grown by `margin` x its size
    (relief, lens distortion). None when a photo does not look down at the
    plane (oblique / terrestrial capture): then the surface itself decides."""
    lo = np.array([np.inf, np.inf]); hi = -lo
    for C, Rc, (fx, fy, cx, cy), w, h in cams:
        Cp = R @ np.asarray(C, float)
        if Cp[2] - zground <= 0:
            return None
        corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], float)
        dirs = np.column_stack([(corners[:, 0] - cx) / fx, (corners[:, 1] - cy) / fy, np.ones(4)])
        dw = (dirs @ np.asarray(Rc, float)) @ R.T
        if not (dw[:, 2] < -1e-3).all():
            return None
        P = Cp[None] + dw * ((zground - Cp[2]) / dw[:, 2])[:, None]
        mg = margin * max(np.ptp(P[:, 0]), np.ptp(P[:, 1]))
        lo = np.minimum(lo, P[:, :2].min(0) - mg); hi = np.maximum(hi, P[:, :2].max(0) + mg)
    if not np.isfinite(lo).all():
        return None
    return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


def ortho_extent(lo, hi, fp=None, region=None, off=(0.0, 0.0)):
    """Orthomosaic extent in the projection frame: the region (REAL world X/Y,
    clipped to the surface) when set, else the surface bounding box limited to
    the area the photos cover (fp, see footprint_bounds) - stray triangles far
    outside the photos (meshing / point-cloud outliers, the horizon of an
    oblique photo) must not blow the raster up to tens of gigapixels.
    Returns (xmin, ymin, xmax, ymax, clipped)."""
    if region:
        xmin, ymin, xmax, ymax = [float(v) for v in region]
        xmin -= off[0]; xmax -= off[0]; ymin -= off[1]; ymax -= off[1]
        xmin, ymin = max(xmin, lo[0]), max(ymin, lo[1])
        xmax, ymax = min(xmax, hi[0]), min(ymax, hi[1])
        if xmax <= xmin or ymax <= ymin:
            raise RuntimeError("The region does not overlap the surface")
        return xmin, ymin, xmax, ymax, False
    xmin, ymin, xmax, ymax = float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])
    if fp is not None:
        a = (max(xmin, fp[0]), max(ymin, fp[1]), min(xmax, fp[2]), min(ymax, fp[3]))
        if a[2] > a[0] and a[3] > a[1] and (a[2] - a[0]) * (a[3] - a[1]) < 0.98 * (xmax - xmin) * (ymax - ymin):
            return a[0], a[1], a[2], a[3], True
    return xmin, ymin, xmax, ymax, False


def raster_size(xmin, ymin, xmax, ymax, px, max_dimension=0, off_xy=(0.0, 0.0)):
    """(px, W, H) of the orthomosaic grid for a pixel size / max dimension."""
    if max_dimension:
        px = max(px, max(xmax - xmin, ymax - ymin) / float(max_dimension))
    _x0, _y1, W, H = make_grid(xmin, ymin, xmax, ymax, px, off_xy)
    return px, W, H


def suggest_pixel_size(xmin, ymin, xmax, ymax, max_pixels, gsd=0.0):
    """Smallest 'nice' pixel size that keeps the raster under max_pixels (and
    not finer than the photo GSD)."""
    px = math.sqrt(max(xmax - xmin, 1e-12) * max(ymax - ymin, 1e-12) / float(max_pixels)) * 1.02
    px = max(px, gsd or 0.0)
    e = 10 ** math.floor(math.log10(px))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= px:
            return round(m * e, 12)
    return px


def too_big_message(W, H, px, gsd, unit, suggestion):
    msg = f"Orthomosaic would be {W:,} x {H:,} px ({W * H / 1e9:.1f} Gpx) at a pixel size of {px:.4g} {unit}"
    if gsd and px < gsd / 1.5:
        msg += (f", {gsd / px:.0f}x finer than the photos (photo ground resolution about {nice(gsd):.4g} {unit}/pix;"
                " a smaller pixel adds no detail)")
    return msg + f". Use a pixel size of {suggestion:.4g} {unit} or more, a Max. dimension, or a Region."


def mesh_vertices(path, step_target=2_000_000):
    """Vertices (n,3) of a binary PLY (every k-th, at most ~step_target) without the faces."""
    from .plyio import vertex_layout
    with open(path, "rb") as fh:
        lay = vertex_layout(fh)
        if lay is not None:
            _head, dt, n = lay
            v = np.fromfile(fh, dtype=dt, count=n)
            V = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)
            return V[:: max(1, len(V) // step_target)]
    from .texture import read_mesh
    return read_mesh(path)[0]


def plan_orthomosaic(model_dir, mesh_path, georef=None, region=None):
    """Quick extent estimate for the Build Orthomosaic dialog (no ray casting):
    {'bounds': (xmin, ymin, xmax, ymax) projection frame, 'off': offset, 'clipped'}."""
    from . import colmap_io as cio
    cams = cio.read_cameras(os.path.join(model_dir, "cameras.bin"))
    imgs = cio.read_images(os.path.join(model_dir, "images.bin"))
    if not imgs:
        raise ValueError("no aligned photos")

    class _V:                                   # what projection_rotation needs
        def __init__(self, R):
            self.R = R
    R = projection_rotation([_V(im.R()) for im in imgs.values()], bool(georef))
    V = mesh_vertices(mesh_path) @ R.T
    if not len(V):
        raise ValueError("empty surface")
    off = georef_offset(georef if georef else None)
    zg = float(np.median(V[:, 2]))
    fp = footprint_bounds([(im.center(), im.R(), cams[im.camera_id].K(), cams[im.camera_id].width,
                            cams[im.camera_id].height) for im in imgs.values()], R, zg)
    b = ortho_extent(V.min(0), V.max(0), fp, region, off[:2])
    return {"bounds": b[:4], "off": off, "clipped": b[4]}

# ------------------------------------------------------------------ surface
class Surface:
    """Embree scene of the surface in the projection frame."""

    def __init__(self, V, F):
        import open3d as o3d
        self.o3d = o3d
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor(np.ascontiguousarray(V, np.float32)),
                                 o3d.core.Tensor(np.ascontiguousarray(F, np.uint32)))
        self.zmax = float(V[:, 2].max()) if len(V) else 0.0
        self.zmin = float(V[:, 2].min()) if len(V) else 0.0
        self.lo, self.hi = V.min(0), V.max(0)

    def heights(self, x, y):
        """Top surface height at (x, y) (NaN where there is no surface)."""
        n = len(x)
        top = self.zmax + 10.0
        rays = np.empty((n, 6), np.float32)
        rays[:, 0] = x; rays[:, 1] = y; rays[:, 2] = top
        rays[:, 3] = 0; rays[:, 4] = 0; rays[:, 5] = -1
        t = self.scene.cast_rays(self.o3d.core.Tensor(rays))["t_hit"].numpy()
        h = top - t.astype(np.float64)
        h[~np.isfinite(t)] = np.nan
        return h

    def occluded(self, X, C, eps):
        """X (n,3) surface points, C (n,3) or (3,) camera centres: True if the
        segment X -> C hits the surface (after a small offset eps)."""
        d = np.broadcast_to(C, X.shape) - X
        dist = np.linalg.norm(d, axis=1)
        o = X + d * (eps / np.maximum(dist, 1e-9))[:, None]
        rays = np.concatenate([o, np.broadcast_to(C, X.shape) - o], 1).astype(np.float32)
        r = self.scene.test_occlusions(self.o3d.core.Tensor(rays), tnear=0.0, tfar=0.9995)
        return r.numpy().astype(bool)


def dem_mesh(dem, x0, y1, px, max_cells=3_000_000):
    """Triangulate a DEM grid (heights in the projection frame, NaN = none)."""
    H, W = dem.shape
    step = max(1, int(math.ceil(math.sqrt(H * W / max_cells))))
    d = dem[::step, ::step]
    h, w = d.shape
    jj, ii = np.meshgrid(np.arange(w), np.arange(h))
    X = x0 + (jj * step + 0.5) * px; Y = y1 - (ii * step + 0.5) * px
    V = np.column_stack([X.ravel(), Y.ravel(), np.nan_to_num(d.ravel(), nan=0.0)])
    ok = np.isfinite(d)
    q = ok[:-1, :-1] & ok[1:, :-1] & ok[:-1, 1:] & ok[1:, 1:]
    a = (ii[:-1, :-1] * w + jj[:-1, :-1])[q]
    F = np.concatenate([np.stack([a, a + w, a + 1], 1), np.stack([a + 1, a + w, a + w + 1], 1)])
    return V, F


# ------------------------------------------------------------------ photos
class PhotoCache:
    """LRU cache of decoded photos (bounded by cache_mb); one decode per key."""

    def __init__(self, max_mb, max_side=0):
        self.max = max_mb * 1e6
        self.max_side = max_side
        self.data = OrderedDict()
        self.size = 0
        self.lock = threading.Lock()
        self.key_locks = {}
        self.decodes = 0

    def get(self, view):
        from .texture import load_image
        k = view.idx
        with self.lock:
            if k in self.data:
                self.data.move_to_end(k)
                return self.data[k]
            kl = self.key_locks.setdefault(k, threading.Lock())
        with kl:
            with self.lock:
                if k in self.data:
                    self.data.move_to_end(k); return self.data[k]
            img = load_image(view.path, self.max_side)
            with self.lock:
                self.decodes += 1
                self.data[k] = img; self.size += img.nbytes
                while self.size > self.max and len(self.data) > 1:
                    _k, old = self.data.popitem(last=False)
                    self.size -= old.nbytes
            return img


# ------------------------------------------------------------------ helpers
def ortho_block_bytes(block, top_k=6):
    """Estimated peak memory of one render worker for a block x block tile:
    ~30 float64/int64 values per pixel (grid, X/Y/Z, projections, EDT
    indices), the candidate rows (K views x 4 B + K x 3 colours) and the RGBA
    output, plus a decoded photo held while sampling."""
    px = block * block
    return int(px * (30 * 8 + top_k * 20 + 8) + 70 * 1024 ** 2)


def _log_wrap(log):
    return lambda m: log(m.replace("Texture:", "Ortho:"))


def _project(v, Xp, R):
    """Project projection-frame points into view v: (uv, depth)."""
    return v.project(Xp @ R if R is not None else Xp)


def _border(uv, v, fade):
    m = np.minimum(np.minimum(uv[:, 0], v.W - uv[:, 0]), np.minimum(uv[:, 1], v.H - uv[:, 1]))
    return np.clip(m / fade, 0, 1)


def grid_labels(best_s, best_v, ny, nx, smooth=0.8, colg=None, beta=0.0, iters=10, log=print):
    """Potts ICM on a 4-connected (ny, nx) cell grid. best_* (m, K) sorted by score."""
    m, K = best_v.shape
    valid = best_v >= 0
    smax = np.where(valid, best_s, 0).max(1)
    data = np.where(valid, 1.0 - best_s / np.maximum(smax, 1e-30)[:, None], np.inf).astype(np.float32)
    has = valid.any(1)
    L = np.where(has, best_v[np.arange(m), np.argmin(data, 1)], -1)
    if smooth <= 0:
        return L
    idx = np.arange(m).reshape(ny, nx)
    nbs = []
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        n = np.full((ny, nx), -1, np.int64)
        ys = slice(max(0, -dy), ny - max(0, dy)); yd = slice(max(0, dy), ny - max(0, -dy) if dy < 0 else ny)
        xs = slice(max(0, -dx), nx - max(0, dx)); xd = slice(max(0, dx), nx - max(0, -dx) if dx < 0 else nx)
        n[ys, xs] = idx[yd, xd]
        nbs.append(n.ravel())
    nb = np.stack(nbs, 1)
    rng = np.random.default_rng(0)
    for it in range(iters):
        nl = np.where(nb >= 0, L[np.maximum(nb, 0)], -2)            # (m, 4)
        cost = np.empty((m, K), np.float32)
        for k in range(K):
            lab = best_v[:, k:k + 1]
            diff = (nl != lab) & (nl >= 0)
            if beta > 0 and colg is not None:
                # colour disagreement of the two photos at this cell (seam placement)
                pen = np.ones(diff.shape, np.float32)
                for j in range(K):
                    hit = nl == best_v[:, j:j + 1]
                    dc = np.abs(colg[:, k] - colg[:, j]).mean(1, keepdims=True) / 40.0
                    pen = np.where(hit, np.minimum(dc, 1.0), pen)
                cost[:, k] = data[:, k] + smooth * (diff * (1.0 + beta * pen)).sum(1)
            else:
                cost[:, k] = data[:, k] + smooth * diff.sum(1)
        newL = np.where(has, best_v[np.arange(m), np.argmin(cost, 1)], -1)
        upd = rng.random(m) < 0.5
        changed = int(((newL != L) & upd).sum())
        L = np.where(upd, newL, L)
        if changed < max(1, m * 1e-4):
            break
    return L


# ------------------------------------------------------------------ Build Orthomosaic
def build_orthomosaic(cfg, log=print, progress=None, cancel=None):
    """cfg: mesh (PLY), dem (GeoTIFF from build_dem, for surface 'DEM'), model
    (COLMAP sparse dir), photos {name: path}, out_dir, georef (chunk.georef
    dict or {}), crs + ORTHO_DEFAULTS keys. Returns the info dict (info.json)."""
    from .texture import load_views, read_mesh, load_image, sharpness, bilinear, exposure_gains, ghost_filter
    c = dict(ORTHO_DEFAULTS); c.update({k: v for k, v in cfg.items() if v is not None})
    t_all = time.perf_counter()
    stages = {}
    lg = _log_wrap(log)
    threads = int(c["threads"]) or max(2, min(16, (os.cpu_count() or 4)))

    def check():
        if cancel and cancel():
            raise Cancelled("cancelled")

    def prog(p):
        if progress:
            progress(int(p), 100)

    def stage(name, t0):
        stages[name] = round(time.perf_counter() - t0, 2)

    georef = c.get("georef") or {}
    off = georef_offset(georef)
    is_geo = bool(georef)
    out = c["out_dir"]; os.makedirs(out, exist_ok=True)
    for n in os.listdir(out):
        if n.endswith(".part"):
            try:
                os.remove(os.path.join(out, n))
            except OSError:
                pass
    t0 = time.perf_counter()
    views = load_views(c["model"], c["photos"], lg)
    if not views:
        raise RuntimeError("No aligned photos found - check the image paths (Tools > Change Image Paths...)")
    R = projection_rotation(views, is_geo)
    Rn = None if np.allclose(R, np.eye(3)) else R
    Cp = np.array([R @ v.C for v in views])
    # surface
    if c["surface"] == "DEM":
        if not c.get("dem") or not os.path.exists(c["dem"]):
            raise RuntimeError("Surface 'DEM' needs a DEM - run Workflow > Build DEM first")
        with gt.TiffReader(c["dem"]) as r:
            d = r.read_level(0)[..., 0].astype(np.float64)
            dx0, dy1, dpx, _ = r.geo
            nd = r.nodata
        d[(d == nd) | ~np.isfinite(d)] = np.nan
        d -= off[2]
        V, F = dem_mesh(d, dx0 - off[0], dy1 - off[1], dpx)
        lg(f"Ortho: surface = DEM {d.shape[1]}x{d.shape[0]} ({len(F):,} triangles)")
    else:
        V, F = read_mesh(c["mesh"])
        V = V @ R.T
        lg(f"Ortho: surface = mesh {len(V):,} vertices, {len(F):,} faces")
    if not len(F):
        raise RuntimeError("The surface is empty")
    surf = Surface(V, F)
    stage("load surface + cameras", t0); prog(2); check()
    # ---- extent + pixel size
    fx = np.array([v.cam.K()[0] for v in views])
    axis = np.array([R @ v.R[2] for v in views])          # optical axes in the projection frame
    t0 = time.perf_counter()
    rays = np.concatenate([Cp, axis], 1).astype(np.float32)
    th = surf.scene.cast_rays(surf.o3d.core.Tensor(rays))["t_hit"].numpy()
    ok = np.isfinite(th)
    if ok.any():
        gsd = float(np.median(th[ok] / fx[ok]))
    else:
        gsd = float(np.median(np.abs(Cp[:, 2] - np.median(V[:, 2])) / fx))
    # pixels per metre of each view at the surface (for the resolution term)
    ppm_med = 1.0 / gsd
    # extent: the region, else the surface limited to what the photos cover
    zg0 = float(np.median((Cp + axis * np.where(ok, th, 0)[:, None])[ok, 2])) if ok.any() else float(np.median(V[:, 2]))
    fp = footprint_bounds([(v.C, v.R, v.cam.K(), v.W, v.H) for v in views], R, zg0)
    xmin, ymin, xmax, ymax, clipped = ortho_extent(surf.lo, surf.hi, fp, c.get("region"), off[:2])
    unit = "m" if is_geo else "units"
    if clipped:
        lg(f"Ortho: extent limited to the area covered by the photos ({xmax - xmin:.4g} x {ymax - ymin:.4g} {unit} "
           f"of the {surf.hi[0] - surf.lo[0]:.4g} x {surf.hi[1] - surf.lo[1]:.4g} {unit} surface; "
           "geometry outside the photos is ignored)")
    px = float(c["pixel_size"] or 0)
    if px <= 0:
        px = nice(gsd)
    px, W, H = raster_size(xmin, ymin, xmax, ymax, px, c.get("max_dimension") or 0, off[:2])
    x0, y1, W, H = make_grid(xmin, ymin, xmax, ymax, px, off[:2])
    if W * H > float(c["max_pixels"]):
        raise RuntimeError(too_big_message(W, H, px, gsd, unit,
                                           suggest_pixel_size(xmin, ymin, xmax, ymax, float(c["max_pixels"]), gsd)))
    lg(f"Ortho: photo GSD {gsd * 100 if is_geo else gsd:.3g} {'cm' if is_geo else unit}/pix, "
       f"pixel size {px:.4g} {unit}, {W:,} x {H:,} px ({W * H / 1e6:.1f} Mpx), {threads} threads")
    # ---- coarse cells
    cf = int(c["cell_px"])
    while math.ceil(W / cf) * math.ceil(H / cf) > 4_000_000:
        cf *= 2
    nx, ny = math.ceil(W / cf), math.ceil(H / cf)
    cs = cf * px
    cj, ci = np.meshgrid(np.arange(nx), np.arange(ny))
    cx = (x0 + (cj.ravel() * cf + cf / 2) * px)
    cy = (y1 - (ci.ravel() * cf + cf / 2) * px)
    cz = surf.heights(cx, cy)
    hit = np.isfinite(cz)
    if not hit.any():
        raise RuntimeError("No surface inside the orthomosaic extent")
    from scipy import ndimage
    g = (~hit).reshape(ny, nx)
    dist, (iy, ix) = ndimage.distance_transform_edt(g, return_indices=True)
    czg = cz.reshape(ny, nx)[iy, ix]
    near = (dist <= 3).ravel()
    cz = czg.ravel()
    cells = np.flatnonzero(near)
    m = len(cells)
    Xc = np.column_stack([cx[cells], cy[cells], cz[cells]])
    K = int(c["top_k"])
    best_s = np.zeros((m, K), np.float32); best_v = np.full((m, K), -1, np.int32)
    zground = float(np.median(cz[hit]))
    lock = threading.Lock()
    fade = 0.04
    sharp = np.ones(len(views))

    def cand(vi):
        check()
        v = views[vi]
        C = Cp[vi]
        # footprint: corners of the image on the median ground plane (+ margin)
        hgt = C[2] - zground
        if hgt > 0:
            fxv, fyv, cxv, cyv = v.cam.K()
            corners = np.array([[0, 0], [v.W, 0], [0, v.H], [v.W, v.H]], float)
            dirs = np.column_stack([(corners[:, 0] - cxv) / fxv, (corners[:, 1] - cyv) / fyv, np.ones(4)])
            dw = (dirs @ v.R) @ R.T                     # camera -> local -> projection frame
            if (dw[:, 2] < -1e-3).all():
                tt = (zground - C[2]) / dw[:, 2]
                P = C[None] + dw * tt[:, None]
                mg = 0.25 * max(np.ptp(P[:, 0]), np.ptp(P[:, 1])) + 2 * cs
                lo, hi = P[:, :2].min(0) - mg, P[:, :2].max(0) + mg
                c0 = int(max(0, (lo[0] - x0) // cs)); c1 = int(min(nx, (hi[0] - x0) // cs + 1))
                r0 = int(max(0, (y1 - hi[1]) // cs)); r1 = int(min(ny, (y1 - lo[1]) // cs + 1))
                if c1 <= c0 or r1 <= r0:
                    return
                sub = (ci.ravel()[cells] >= r0) & (ci.ravel()[cells] < r1) & \
                      (cj.ravel()[cells] >= c0) & (cj.ravel()[cells] < c1)
                idx = np.flatnonzero(sub)
            else:
                idx = np.arange(m)
        else:
            idx = np.arange(m)
        if not len(idx):
            return
        uv, z = _project(v, Xc[idx], Rn)
        okk = (z > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < v.W) & (uv[:, 1] >= 0) & (uv[:, 1] < v.H)
        idx, uv = idx[okk], uv[okk]
        if not len(idx):
            return
        d = C[None] - Xc[idx]
        dist = np.linalg.norm(d, axis=1)
        cos = d[:, 2] / np.maximum(dist, 1e-9)
        b = _border(uv, v, fade * min(v.W, v.H))
        res = np.minimum((v.cam.K()[0] / dist) / ppm_med, 1.5)
        s = (np.clip(cos, 0, 1) ** 4 * b * res * sharp[vi]).astype(np.float32)
        keep = s > 1e-4
        idx, s = idx[keep], s[keep]
        with lock:
            cur = best_s[idx]
            mn = cur.argmin(1)
            better = s > cur[np.arange(len(idx)), mn]
            sel = idx[better]; slot = mn[better]
            best_s[sel, slot] = s[better]; best_v[sel, slot] = vi

    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(cand, range(len(views))))
    # occlusion of the candidates
    eps = max(2 * px, 0.002 * (surf.hi[2] - surf.lo[2] + 1))
    flat = np.flatnonzero(best_v.ravel() >= 0)
    if len(flat):
        Xs = Xc[flat // K]
        Cs = Cp[best_v.ravel()[flat]]
        occ = np.zeros(len(flat), bool)
        for a in range(0, len(flat), 4_000_000):
            occ[a:a + 4_000_000] = surf.occluded(Xs[a:a + 4_000_000], Cs[a:a + 4_000_000], eps)
        bs = best_s.ravel(); bv = best_v.ravel()
        bs[flat[occ]] = 0; bv[flat[occ]] = -1
        lg(f"Ortho: {len(flat):,} cell/photo candidates, {occ.mean() * 100:.1f}% occluded")
    order = np.argsort(-best_s, 1)
    best_s = np.take_along_axis(best_s, order, 1); best_v = np.take_along_axis(best_v, order, 1)
    stage("view candidates + visibility", t0); prog(14); check()
    # ---- colour statistics on the 1/8 scale photos
    t0 = time.perf_counter()
    used = np.unique(best_v[best_v >= 0])
    def small(vi):
        check()
        v = views[vi]
        v.small = load_image(v.path, 660)
        return sharpness(v.small)
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(small, used))
    col = np.zeros((m, K, 3), np.float32)
    flatv = best_v.ravel()
    srt = np.argsort(flatv, kind="stable")
    bnd = np.searchsorted(flatv[srt], np.arange(len(views) + 1))
    items = {int(vi): srt[bnd[vi]:bnd[vi + 1]] for vi in used}
    # cell colour = box average over the cell (3x3 samples), i.e. the low band at the cell scale
    offs = [(a * cs / 3, b * cs / 3) for a in (-1, 0, 1) for b in (-1, 0, 1)]
    cfl = col.reshape(-1, 3)
    def cell_col(item):
        vi, sl = item
        v = views[vi]
        sh, sw = v.small.shape[:2]
        acc = np.zeros((len(sl), 3), np.float32)
        Xb = Xc[sl // K]
        for dx, dy in offs:
            uv, _z = _project(v, Xb + np.array([dx, dy, 0.0]), Rn)
            acc += bilinear(v.small, uv[:, 0] * sw / v.W, uv[:, 1] * sh / v.H)
        return sl, acc / len(offs)
    with ThreadPoolExecutor(threads) as ex:
        for sl, cc in ex.map(cell_col, items.items()):
            cfl[sl] = cc
    gains = exposure_gains(best_v, col, len(views), lg) if c["blending"] == "Mosaic" else np.zeros((len(views), 3))
    gmul = np.exp(gains).astype(np.float32)
    colg = col * np.where(best_v[..., None] >= 0, gmul[np.maximum(best_v, 0)], 0)
    if c["ghosting"]:
        ghost_filter(best_s, best_v, colg, lg)
        order = np.argsort(-best_s, 1)
        best_s = np.take_along_axis(best_s, order, 1); best_v = np.take_along_axis(best_v, order, 1)
        colg = np.take_along_axis(colg, order[..., None], 1)
    stage("colour statistics", t0); prog(20); check()
    # ---- view selection on the grid
    t0 = time.perf_counter()
    full_s = np.zeros((ny * nx, K), np.float32); full_v = np.full((ny * nx, K), -1, np.int32)
    full_c = np.zeros((ny * nx, K, 3), np.float32)
    full_s[cells] = best_s; full_v[cells] = best_v; full_c[cells] = colg
    if c["blending"] == "Average":
        Lc = full_v[:, 0].copy()
    else:
        Lc = grid_labels(full_s, full_v, ny, nx, smooth=float(c["smooth"]), colg=full_c,
                         beta=float(c["seam_beta"]) if c["refine_seamlines"] else 0.0, log=lg)
    # low band: weighted average of the visible candidates per cell
    wv = np.where(full_v >= 0, full_s, 0)
    Lavg = (full_c * wv[..., None]).sum(1) / np.maximum(wv.sum(1), 1e-12)[:, None]
    seams = int((Lc.reshape(ny, nx)[:, 1:] != Lc.reshape(ny, nx)[:, :-1]).sum()
                + (Lc.reshape(ny, nx)[1:] != Lc.reshape(ny, nx)[:-1]).sum())
    photos_used = int(len(np.unique(Lc[Lc >= 0])))
    lg(f"Ortho: view selection on {ny}x{nx} cells of {cf} px: {photos_used} photos, {seams:,} seam edges"
       + (" (refined seamlines)" if c["refine_seamlines"] else ""))
    stage("view selection", t0); prog(24); check()
    # ---- render blocks
    t0 = time.perf_counter()
    tile = int(c["tile"]); B = max(tile, int(c["block"]) // tile * tile)
    levels = gt.overview_levels(W, H)
    path = os.path.join(out, "orthomosaic.tif")
    crs = (georef.get("crs") or c.get("crs") or "") if is_geo else ""
    world_geo = (x0 + off[0], y1 + off[1], px, px) if True else None
    writer = gt.TiledTiffWriter(path, levels, 4, np.uint8, tile, "Deflate", alpha=True, geo=world_geo,
                                crs=crs or 'LOCAL_CS["Local Coordinates (m)"]',
                                description="PhotoMesh Studio orthomosaic")
    hf = int(c["height_factor"])
    HW, HH = math.ceil(W / hf), math.ceil(H / hf)
    hgrid = np.full((HH, HW), np.nan, np.float32)
    cache = PhotoCache(float(c["cache_mb"]), int(c["max_res"] or 0))
    blocks = [(r0, c0) for r0 in range(0, H, B) for c0 in range(0, W, B)]
    # serpentine order keeps neighbouring blocks (same photos) together in the cache
    nbx = math.ceil(W / B)
    blocks = [b for k in range(math.ceil(H / B))
              for b in (blocks[k * nbx:(k + 1) * nbx] if k % 2 == 0 else blocks[k * nbx:(k + 1) * nbx][::-1])]
    done = [0]; stat = {"pixels": 0, "holes": 0, "fallback": 0}
    slock = threading.Lock()
    enc_pool = ThreadPoolExecutor(max(2, threads // 2))
    mr = int(c["max_res"] or 0)

    def sample(vi, X, gain=True):
        v = views[vi]
        img = cache.get(v)
        uv, _z = _project(v, X, Rn)
        h, w = img.shape[:2]
        colr = bilinear(img, uv[:, 0] * w / v.W, uv[:, 1] * h / v.H)
        return colr * gmul[vi] if gain else colr

    def render(blk):
        check()
        r0, c0 = blk
        h, w = min(B, H - r0), min(B, W - c0)
        jj, ii = np.meshgrid(np.arange(c0, c0 + w), np.arange(r0, r0 + h))
        X = x0 + (jj.ravel() + 0.5) * px
        Y = y1 - (ii.ravel() + 0.5) * px
        Z = surf.heights(X, Y)
        valid = np.isfinite(Z)
        if c["hole_filling"] and (~valid).any() and valid.any():
            g2 = (~valid).reshape(h, w)
            lab, n = ndimage.label(g2)
            if n:
                border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
                sizes = np.bincount(lab.ravel())
                fill = np.zeros(n + 1, bool); fill[1:] = sizes[1:] < 0.05 * h * w
                fill[border] = False
                fm = fill[lab].ravel()
                if fm.any():
                    _d, (fy, fx_) = ndimage.distance_transform_edt(g2, return_indices=True)
                    Z = np.where(fm, Z.reshape(h, w)[fy, fx_].ravel(), Z)
                    valid = np.isfinite(Z)
        rgba = np.zeros((h * w, 4), np.uint8)
        # height grid (1/hf)
        Zg = Z.reshape(h, w)
        hh, ww = math.ceil(h / hf), math.ceil(w / hf)
        pad = np.full((hh * hf, ww * hf), np.nan, np.float32); pad[:h, :w] = Zg
        # nan-aware block mean without np.nanmean (its "Mean of empty slice" warning
        # cannot be silenced reliably from worker threads: catch_warnings is process-global)
        pr = pad.reshape(hh, hf, ww, hf)
        fin = np.isfinite(pr)
        cnt = fin.sum(axis=(1, 3))
        with np.errstate(all="ignore"):
            hs = (np.where(fin, pr, 0).sum(axis=(1, 3)) / cnt).astype(np.float32)
        hs[cnt == 0] = np.nan
        hgrid[r0 // hf:r0 // hf + hh, c0 // hf:c0 // hf + ww] = hs + off[2]
        sel = np.flatnonzero(valid)
        if not len(sel):
            writer.write_block(0, r0, c0, rgba.reshape(h, w, 4), enc_pool)
            return
        Xp = np.column_stack([X[sel], Y[sel], Z[sel]])
        # cell coordinates (continuous) of the pixels
        u = (jj.ravel()[sel] + 0.5) / cf - 0.5
        vv = (ii.ravel()[sel] + 0.5) / cf - 0.5
        cid = (np.clip(ii.ravel()[sel] // cf, 0, ny - 1) * nx + np.clip(jj.ravel()[sel] // cf, 0, nx - 1))
        candv = full_v[cid]                                  # (n, K)
        lab_ = Lc[cid]
        n = len(sel)
        final = np.full(n, -1, np.int32)
        # attempt order: label, then the candidates by score
        tries = [lab_] + [candv[:, k] for k in range(K)]
        todo = np.arange(n)
        for t_ in tries:
            if not len(todo):
                break
            vt = t_[todo]
            okv = vt >= 0
            todo_v = todo[okv]; vt = vt[okv]
            if not len(todo_v):
                continue
            good = np.zeros(len(todo_v), bool)
            srt_ = np.argsort(vt, kind="stable")
            ub, st_ = np.unique(vt[srt_], return_index=True)
            st_ = list(st_) + [len(vt)]
            for k2, vi in enumerate(ub):
                ids = srt_[st_[k2]:st_[k2 + 1]]
                v = views[vi]
                uv, zc = _project(v, Xp[todo_v[ids]], Rn)
                inside = (zc > 0) & (uv[:, 0] >= 0.5) & (uv[:, 0] < v.W - 0.5) & (uv[:, 1] >= 0.5) & \
                         (uv[:, 1] < v.H - 0.5)
                ins = ids[inside]
                if len(ins):
                    occ = surf.occluded(Xp[todo_v[ins]], Cp[vi], eps)
                    good[ins[~occ]] = True
            final[todo_v[good]] = vt[good]
            todo = np.setdiff1d(todo, todo_v[good], assume_unique=True)
        nfb = int(((final >= 0) & (final != lab_)).sum())
        colr = np.zeros((n, 3), np.float32)
        have = final >= 0
        if c["blending"] == "Average":
            acc = np.zeros((n, 3), np.float32); wsum = np.zeros(n, np.float32)
            sc = full_s[cid]
            for k in range(K):
                vk = candv[:, k]
                for vi in np.unique(vk[vk >= 0]):
                    ids = np.flatnonzero(vk == vi)
                    v = views[vi]
                    uv, zc = _project(v, Xp[ids], Rn)
                    inside = (zc > 0) & (uv[:, 0] >= 0.5) & (uv[:, 0] < v.W - 0.5) & (uv[:, 1] >= 0.5) & \
                             (uv[:, 1] < v.H - 0.5)
                    ids = ids[inside]
                    if not len(ids):
                        continue
                    occ = surf.occluded(Xp[ids], Cp[vi], eps)
                    ids = ids[~occ]
                    if not len(ids):
                        continue
                    wgt = sc[ids, k] * _border(_project(v, Xp[ids], Rn)[0], v, fade * min(v.W, v.H)) + 1e-6
                    acc[ids] += sample(vi, Xp[ids], gain=False) * wgt[:, None]; wsum[ids] += wgt
            have = wsum > 0
            colr[have] = acc[have] / wsum[have, None]
        else:
            for vi in np.unique(final[have]):
                ids = np.flatnonzero(final == vi)
                colr[ids] = sample(vi, Xp[ids], gain=c["blending"] == "Mosaic")
            if c["blending"] == "Mosaic":
                # two-band blending: + bilinear(Lavg - colour of this pixel's photo) over the 4 cells
                u0 = np.floor(u).astype(np.int64); v0 = np.floor(vv).astype(np.int64)
                fu = (u - u0).astype(np.float32); fv = (vv - v0).astype(np.float32)
                corr = np.zeros((n, 3), np.float32); wsum = np.zeros(n, np.float32)
                fh = full_v.reshape(ny, nx, K); fc = full_c.reshape(ny, nx, K, 3)
                La = Lavg.reshape(ny, nx, 3); Lcl = Lc.reshape(ny, nx)
                for dy, dx, wgt in ((0, 0, (1 - fu) * (1 - fv)), (0, 1, fu * (1 - fv)),
                                    (1, 0, (1 - fu) * fv), (1, 1, fu * fv)):
                    yy = np.clip(v0 + dy, 0, ny - 1); xx = np.clip(u0 + dx, 0, nx - 1)
                    cv = fh[yy, xx]                                   # (n, K)
                    match = cv == final[:, None]
                    found = match.any(1) & have
                    kk = np.argmax(match, 1)
                    cl = fc[yy, xx, kk]                               # (n, 3)
                    okc = found & (cl.max(1) > 0) & (La[yy, xx].max(1) > 0)
                    d = La[yy, xx] - cl
                    corr[okc] += d[okc] * wgt[okc, None]; wsum[okc] += wgt[okc]
                okw = wsum > 1e-6
                colr[okw] += corr[okw] / wsum[okw, None]
        rgba_sel = np.zeros((n, 4), np.uint8)
        rgba_sel[have, :3] = np.clip(colr[have] + 0.5, 0, 255).astype(np.uint8)
        rgba_sel[have, 3] = 255
        nh = int((~have).sum())
        rgba[sel] = rgba_sel
        img = rgba.reshape(h, w, 4)
        if nh and c["hole_filling"]:
            hole = np.zeros(h * w, bool); hole[sel[~have]] = True
            hole = hole.reshape(h, w)
            filled = img[..., 3] > 0
            if filled.any():
                dd, (fy, fx_) = ndimage.distance_transform_edt(~filled, return_indices=True)
                fillm = hole & (dd <= 64)
                img[fillm] = img[fy[fillm], fx_[fillm]]
        writer.write_block(0, r0, c0, img, enc_pool)
        with slock:
            stat["pixels"] += int(have.sum()); stat["holes"] += nh; stat["fallback"] += nfb
            done[0] += 1
            prog(24 + 66 * done[0] / len(blocks))

    nworkers = max(1, min(int(c.get("block_workers") or max(2, threads // 3)), len(blocks)))
    # each block worker holds ~ B^2 x (float64 X/Y/Z, projections, distance-transform
    # indices, candidates): cap by free RAM, keeping the photo cache's budget aside
    from .memguard import cap_workers, MB
    capped = cap_workers(nworkers, ortho_block_bytes(B, K), base_bytes=int(float(c["cache_mb"]) * MB))
    if capped < nworkers:
        lg(f"Ortho: {capped} block workers instead of {nworkers} (limited by free RAM)")
        nworkers = capped
    try:
        with ThreadPoolExecutor(nworkers) as ex:
            list(ex.map(render, blocks))
        stage("render blocks", t0)
        lg(f"Ortho: rendered {len(blocks)} blocks of {B} px in {stages['render blocks']:.1f} s "
           f"({nworkers} workers, {cache.decodes} photo decodes); "
           f"{stat['fallback']:,} px from a fallback photo, {stat['holes']:,} px seen by no photo")
        prog(90); check()
        t0 = time.perf_counter()
        gt.build_overviews(writer, alpha_band=3, executor=enc_pool, cancel=cancel)
        writer.close()
    except BaseException:
        writer.abort(); enc_pool.shutdown(wait=False)
        raise
    enc_pool.shutdown()
    # height grid (world Z) for the cursor read-out and as a DSM by-product
    hpath = os.path.join(out, "height.tif")
    hg = np.where(np.isfinite(hgrid), hgrid, NODATA_DEM).astype(np.float32)
    hw_ = gt.TiledTiffWriter(hpath, gt.overview_levels(HW, HH), 1, np.float32, tile, "Deflate",
                             nodata=NODATA_DEM, geo=(x0 + off[0], y1 + off[1], px * hf, px * hf),
                             crs=crs or 'LOCAL_CS["Local Coordinates (m)"]')
    hw_.write_block(0, 0, 0, hg[..., None])
    gt.build_overviews(hw_, nodata=NODATA_DEM)
    hw_.close()
    stage("overviews + height grid", t0)
    secs = round(time.perf_counter() - t_all, 2)
    xw0, yw1 = x0 + off[0], y1 + off[1]
    info = {"format": "photomesh-ortho/1", "dir": out, "path": path, "height_path": hpath,
            "width": W, "height": H, "pixel_size": px, "source_gsd": gsd,
            "origin": [xw0, yw1], "bounds": [xw0, yw1 - H * px, xw0 + W * px, yw1],
            "offset": off.tolist(), "rotation": R.tolist(), "georef": is_geo, "crs": crs or "",
            "surface": c["surface"], "blending": c["blending"], "hole_filling": bool(c["hole_filling"]),
            "ghosting": bool(c["ghosting"]), "refine_seamlines": bool(c["refine_seamlines"]),
            "region": c.get("region"), "photos_used": photos_used, "levels": len(levels),
            "z_range": [float(np.nanmin(hgrid)) if np.isfinite(hgrid).any() else None,
                        float(np.nanmax(hgrid)) if np.isfinite(hgrid).any() else None],
            "covered_pixels": stat["pixels"], "bytes": os.path.getsize(path),
            "seconds": secs, "stages": stages, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "settings": {k: c[k] for k in ("surface", "blending", "hole_filling", "ghosting", "refine_seamlines",
                                           "pixel_size", "max_dimension", "region", "max_res")}}
    with open(os.path.join(out, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    lg("Ortho stages: " + ", ".join(f"{k} {v:.1f} s" for k, v in stages.items()))
    lg(f"Orthomosaic done in {secs:.1f} s: {W:,} x {H:,} px, {px * (100 if is_geo else 1):.4g} "
       f"{'cm' if is_geo else 'units'}/pix, {info['bytes'] / 1e6:.0f} MB -> {path}")
    prog(100)
    return info


# ------------------------------------------------------------------ Build DEM
def build_dem(cfg, log=print, progress=None, cancel=None):
    """DEM (DSM) from the dense point cloud: mean height of the points in each
    cell, holes interpolated (Enabled: up to max_fill metres from data;
    Extrapolated: everywhere in the extent; Disabled: none).
    cfg: cloud (PLY, local frame), model (for the up direction when not
    georeferenced), georef, out_dir + DEM_DEFAULTS keys."""
    c = dict(DEM_DEFAULTS); c.update({k: v for k, v in cfg.items() if v is not None})
    t_all = time.perf_counter(); stages = {}
    def prog(p):
        if progress:
            progress(int(p), 100)
    def check():
        if cancel and cancel():
            raise Cancelled("cancelled")
    georef = c.get("georef") or {}
    off = georef_offset(georef); is_geo = bool(georef)
    out = c["out_dir"]; os.makedirs(out, exist_ok=True)
    t0 = time.perf_counter()
    if c["source"] == "Mesh":
        from .texture import read_mesh
        P, _F = read_mesh(c["mesh"])
    else:
        from .plyio import read_fast
        r = read_fast(c["cloud"])
        if r is not None:
            v = r["vertex"]; P = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)
        else:
            from .pcfilter import read_cloud
            P = read_cloud(c["cloud"])["xyz"]
    if not is_geo and c.get("model"):
        from .texture import load_views
        views = load_views(c["model"], c.get("photos") or {}, lambda m: None) if c.get("photos") else []
        R = projection_rotation(views, False) if views else np.eye(3)
    else:
        R = np.eye(3)
    if not np.allclose(R, np.eye(3)):
        P = P @ R.T
    stages["read points"] = round(time.perf_counter() - t0, 2)
    log(f"DEM: {len(P):,} source points ({stages['read points']:.1f} s)"); prog(10); check()
    t0 = time.perf_counter()
    lo, hi = P.min(0), P.max(0)
    if c.get("region"):
        xmin, ymin, xmax, ymax = [float(v) for v in c["region"]]
        xmin -= off[0]; xmax -= off[0]; ymin -= off[1]; ymax -= off[1]
    else:
        xmin, ymin, xmax, ymax = lo[0], lo[1], hi[0], hi[1]
    px = float(c["pixel_size"] or 0)
    if px <= 0:
        # mean point spacing over the occupied area (coarse occupancy grid)
        g = max(np.ptp(P[:, 0]), np.ptp(P[:, 1])) / 512 or 1.0
        occ = np.unique(((P[:, 0] - lo[0]) // g).astype(np.int64) * 100000 + ((P[:, 1] - lo[1]) // g).astype(np.int64))
        area = len(occ) * g * g
        px = nice(2.0 * math.sqrt(area / max(len(P), 1)))
    x0, y1, W, H = make_grid(xmin, ymin, xmax, ymax, px, off[:2])
    if W * H > float(c["max_pixels"]):
        raise RuntimeError(f"DEM would be {W}x{H} cells - increase the resolution value")
    col = np.floor((P[:, 0] - x0) / px).astype(np.int64)
    row = np.floor((y1 - P[:, 1]) / px).astype(np.int64)
    ok = (col >= 0) & (col < W) & (row >= 0) & (row < H)
    idx = row[ok] * W + col[ok]
    cnt = np.bincount(idx, minlength=W * H)
    s = np.bincount(idx, weights=P[ok, 2], minlength=W * H)
    dem = np.full(W * H, np.nan, np.float64)
    has = cnt > 0
    dem[has] = s[has] / cnt[has]
    dem = dem.reshape(H, W)
    del P, col, row, idx, s
    stages["rasterize"] = round(time.perf_counter() - t0, 2); prog(50); check()
    t0 = time.perf_counter()
    from scipy import ndimage
    hole = ~np.isfinite(dem)
    filled = 0
    if c["interpolation"] != "Disabled" and hole.any() and (~hole).any():
        dist, (iy, ix) = ndimage.distance_transform_edt(hole, return_indices=True)
        if c["interpolation"] == "Extrapolated":
            fm = hole
        else:
            # interior holes only (not connected to the outer border) or near the data
            lab, n = ndimage.label(hole)
            border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
            inner = np.ones(n + 1, bool); inner[0] = False; inner[border] = False
            fm = (inner[lab] | (dist * px <= float(c["max_fill"]) * 0.25)) & (dist * px <= float(c["max_fill"]))
        near = dem[iy, ix]
        # smooth the nearest-neighbour fill (normalized convolution) so filled areas have no terraces
        known = np.where(hole, 0.0, dem)
        wk = (~hole).astype(np.float64)
        sig = 3.0
        num = ndimage.gaussian_filter(known, sig); den = ndimage.gaussian_filter(wk, sig)
        sm = np.where(den > 1e-3, num / np.maximum(den, 1e-12), near)
        dem = np.where(fm, sm, dem)
        filled = int(fm.sum())
    stages["interpolation"] = round(time.perf_counter() - t0, 2); prog(70); check()
    t0 = time.perf_counter()
    world = np.where(np.isfinite(dem), dem + off[2], NODATA_DEM).astype(np.float32)
    path = os.path.join(out, "dem.tif")
    crs = (georef.get("crs") or c.get("crs") or "") if is_geo else ""
    w = gt.TiledTiffWriter(path, gt.overview_levels(W, H), 1, np.float32, int(c["tile"]), "Deflate",
                           nodata=NODATA_DEM, geo=(x0 + off[0], y1 + off[1], px, px),
                           crs=crs or 'LOCAL_CS["Local Coordinates (m)"]', description="PhotoMesh Studio DEM")
    try:
        with ThreadPoolExecutor(max(2, (os.cpu_count() or 4) // 2)) as ex:
            for r0 in range(0, H, 2048):
                w.write_block(0, r0, 0, world[r0:r0 + 2048, :, None], ex)
            gt.build_overviews(w, nodata=NODATA_DEM, executor=ex)
        w.close()
    except BaseException:
        w.abort(); raise
    stages["write"] = round(time.perf_counter() - t0, 2)
    v = world[world != NODATA_DEM]
    xw0, yw1 = x0 + off[0], y1 + off[1]
    info = {"format": "photomesh-dem/1", "dir": out, "path": path, "width": W, "height": H, "pixel_size": px,
            "origin": [xw0, yw1], "bounds": [xw0, yw1 - H * px, xw0 + W * px, yw1], "offset": off.tolist(),
            "rotation": R.tolist(), "georef": is_geo, "crs": crs, "source": c["source"],
            "interpolation": c["interpolation"], "z_range": [float(v.min()), float(v.max())] if len(v) else None,
            "filled_cells": filled, "valid_cells": int(len(v)), "bytes": os.path.getsize(path), "nodata": NODATA_DEM,
            "seconds": round(time.perf_counter() - t_all, 2), "stages": stages,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "settings": {k: c[k] for k in ("source", "interpolation", "pixel_size", "region")}}
    with open(os.path.join(out, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    log(f"DEM done in {info['seconds']:.1f} s: {W:,} x {H:,} cells, {px * (100 if is_geo else 1):.4g} "
        f"{'cm' if is_geo else 'units'}/pix, {filled:,} cells interpolated -> {path}")
    prog(100)
    return info


# ------------------------------------------------------------------ export
def level_pixel_size(info, level_width, lev):
    """Pixel size of overview level `lev`. PhotoMesh builds its overviews on an exact
    2**lev grid (odd edges padded), so the pixel is exactly pixel_size * 2**lev from
    the same origin; for other GeoTIFFs use the GDAL convention (same extent)."""
    if str(info.get("format", "")).startswith("photomesh"):
        return info["pixel_size"] * 2 ** lev
    return info["pixel_size"] * info["width"] / level_width


def _level_for(info, scale=1, max_dimension=0):
    """Overview level (power-of-two reduction) for an export."""
    lev = max(0, int(round(math.log2(max(1, scale))))) if scale else 0
    if max_dimension:
        while max(info["width"], info["height"]) / 2 ** lev > max_dimension:
            lev += 1
    return lev


def _placement_notes(dst, raster_path, info, lev, wpx, hpx, px, zref):
    """Rhino / AutoCAD / QGIS placement help next to the export (+ a Rhino script)."""
    xmin, ymax = info["origin"]
    xmax, ymin = xmin + wpx * px, ymax - hpx * px
    base = os.path.splitext(dst)[0]
    name = os.path.basename(raster_path)
    txt = f"""PhotoMesh Studio - orthomosaic placement ({name})
Coordinates are REAL world coordinates (project CRS: {info.get('crs') or 'Local Coordinates (m)'}).

Raster: {wpx} x {hpx} px, pixel size {px:.6f} m
Lower-left corner  X {xmin:.4f}  Y {ymin:.4f}
Upper-right corner X {xmax:.4f}  Y {ymax:.4f}
Width {xmax - xmin:.4f} m, height {ymax - ymin:.4f} m
Upper-left pixel centre (world file): X {xmin + px / 2:.4f}  Y {ymax - px / 2:.4f}

Rhino (model units = metres):
  Option A - run the script: Tools > PythonScript > Run (or command _RunPythonScript) and pick
    {os.path.basename(base)}_rhino_place.py  (it places the picture at the exact coordinates,
    at Z = {zref:.2f}, converting to the document units if they are not metres).
  Option B - by hand: command Picture, choose {name}, then type the corners:
    first corner  {xmin:.4f},{ymin:.4f},{zref:.2f}   then   second corner  {xmax:.4f},{ymax:.4f},{zref:.2f}
  Tip: Rhino shows very large pictures downsampled; for display use the JPG export with a max dimension
  (e.g. 16384 px) or split tiles.

AutoCAD (drawing units = metres):
  Map 3D / Civil 3D: MAPIINSERT reads the world file ({os.path.basename(gt.world_file_ext(raster_path)) if False else gt.world_file_ext(raster_path)}) - placed automatically.
  Plain AutoCAD: IMAGEATTACH {name}, insertion point {xmin:.4f},{ymin:.4f}, scale {xmax - xmin:.4f}
  (scale = image width in drawing units), rotation 0.

QGIS: drag the GeoTIFF (or the JPG/PNG with its world file) into the map. With a local CRS set the
project CRS to the same local system (or leave "unknown"); the coordinates match the model exports.
"""
    with open(base + "_placement.txt", "w", encoding="utf-8") as f:
        f.write(txt)
    script = f'''# PhotoMesh Studio: place the orthomosaic in Rhino at its real coordinates.
# Run with _RunPythonScript (Rhino 7/8). Edit Z below to move the picture up/down.
import os
import rhinoscriptsyntax as rs

IMAGE = r"{os.path.abspath(raster_path)}"
XMIN, YMIN, XMAX, YMAX, Z = {xmin!r}, {ymin!r}, {xmax!r}, {ymax!r}, {zref!r}

if not os.path.exists(IMAGE):
    try:
        IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.basename(IMAGE))
    except NameError:
        pass
s = rs.UnitScale(rs.UnitSystem(), 4)       # metres -> document units
plane = rs.PlaneFromFrame((XMIN * s, YMIN * s, Z * s), (1, 0, 0), (0, 1, 0))
obj = rs.AddPictureFrame(plane, IMAGE, (XMAX - XMIN) * s, (YMAX - YMIN) * s, self_illumination=True)
if obj:
    rs.ObjectName(obj, "Orthomosaic")
    print("Orthomosaic placed: X %.3f..%.3f, Y %.3f..%.3f (m)" % (XMIN, XMAX, YMIN, YMAX))
else:
    print("Could not create the picture frame for " + IMAGE)
'''
    with open(base + "_rhino_place.py", "w", encoding="utf-8") as f:
        f.write(script)
    return base + "_placement.txt"


def export_raster(info, dst, fmt=None, compression="LZW", alpha=True, nodata=0, jpeg_quality=90,
                  overviews=True, bigtiff="auto", scale=1, max_dimension=0, split=0, background=(255, 255, 255),
                  kind="ortho", log=print, progress=None, cancel=None):
    """Export Orthomosaic (kind 'ortho') or DEM (kind 'dem') from the internal
    tiled GeoTIFF. fmt: 'tif' | 'jpg' | 'png' (from dst's extension if None).
    split: tile size in px (0 = one file). Writes world files (+ .prj for a
    real WKT CRS) and, for the orthomosaic, Rhino / AutoCAD placement notes.
    Returns the list of written files."""
    fmt = (fmt or os.path.splitext(dst)[1].lstrip(".") or "tif").lower().replace("tiff", "tif").replace("jpeg", "jpg")
    src = gt.TiffReader(info["path"])
    files = []
    try:
        lev = min(_level_for(info, scale, max_dimension), len(src.levels) - 1)
        L = src.levels[lev]
        px = level_pixel_size(info, L.width, lev)
        x0, y1 = info["origin"]
        crs = info.get("crs") or ""
        prj = gt.prj_text(crs)
        zr = info.get("z_range") or [0, 0]
        zref = float(math.floor(zr[0] or 0) - 1.0) if zr and zr[0] is not None else 0.0
        is_dem = kind == "dem"
        if is_dem and fmt != "tif":
            raise ValueError("DEM export: GeoTIFF only (float32)")

        def convert(a):
            """Source tile (RGBA uint8 or float DEM) -> output pixels."""
            if is_dem:
                return a
            if fmt == "tif":
                if alpha and compression != "JPEG":
                    return a
                rgb = a[..., :3].copy()
                m = a[..., 3] == 0
                rgb[~m] = np.maximum(rgb[~m], 1) if nodata == 0 else rgb[~m]
                rgb[m] = nodata
                return rgb
            if fmt == "png" and alpha:
                return a
            rgb = a[..., :3].copy()
            rgb[a[..., 3] == 0] = background
            return rgb

        def tiles_of(level_idx, r0, c0, h, w):
            return convert(src.read_region(level_idx, r0, c0, h, w))

        outs = []
        if split and split > 0:
            S = int(split)
            base, ext = os.path.splitext(dst)
            for r0 in range(0, L.height, S):
                for c0 in range(0, L.width, S):
                    outs.append((f"{base}_{r0 // S:03d}_{c0 // S:03d}{ext or '.' + fmt}", r0, c0,
                                 min(S, L.height - r0), min(S, L.width - c0)))
        else:
            outs.append((dst if os.path.splitext(dst)[1] else dst + "." + fmt, 0, 0, L.height, L.width))
        for k, (path, r0, c0, h, w) in enumerate(outs):
            if cancel and cancel():
                raise Cancelled("cancelled")
            if fmt == "tif":
                bands = 1 if is_dem else (4 if (alpha and compression != "JPEG") else 3)
                levs = [(w, h)]
                srcl = [lev]
                if overviews:
                    for l2 in range(lev + 1, len(src.levels)):
                        f = 2 ** (l2 - lev)
                        if max(w, h) / f < 128:
                            break
                        levs.append((math.ceil(w / f), math.ceil(h / f))); srcl.append(l2)
                big = None if bigtiff == "auto" else (bigtiff in (True, "yes", "Yes"))
                wr = gt.TiledTiffWriter(path, levs, bands, np.float32 if is_dem else np.uint8, 512,
                                        compression, alpha=bands == 4,
                                        nodata=(info.get("nodata", NODATA_DEM) if is_dem else
                                                (None if bands == 4 else nodata)),
                                        geo=(x0 + c0 * px, y1 - r0 * px, px, px), crs=crs or 'LOCAL_CS["Local Coordinates (m)"]',
                                        bigtiff=big, jpeg_quality=jpeg_quality,
                                        description=f"PhotoMesh Studio {'DEM' if is_dem else 'orthomosaic'}")
                try:
                    with ThreadPoolExecutor(max(2, (os.cpu_count() or 4) // 2)) as ex:
                        for li, (lw, lh) in enumerate(levs):
                            f = 2 ** li
                            sr0, sc0 = r0 // f, c0 // f
                            for br in range(0, lh, 2048):
                                if cancel and cancel():
                                    raise Cancelled("cancelled")
                                a = tiles_of(srcl[li], sr0 + br, sc0, min(2048, lh - br), lw)
                                if a.shape[0] < min(2048, lh - br) or a.shape[1] < lw:
                                    pad = np.zeros((min(2048, lh - br), lw, a.shape[2]), a.dtype)
                                    if is_dem:
                                        pad[:] = info.get("nodata", NODATA_DEM)
                                    pad[:a.shape[0], :a.shape[1]] = a; a = pad
                                wr.write_block(li, br, 0, a, ex)
                                if progress and li == 0:
                                    progress(int(100 * (k + (br + 2048) / max(lh, 1)) / len(outs)), 100)
                    wr.close()
                except BaseException:
                    wr.abort(); raise
            else:
                from PIL import Image
                a = tiles_of(lev, r0, c0, h, w)
                if max(a.shape[:2]) > 65000 and fmt == "jpg":
                    raise ValueError("JPEG is limited to 65,000 px per side - use split tiles or a max dimension")
                im = Image.fromarray(a[..., 0] if a.shape[2] == 1 else a)
                tmp = path + ".part"
                if fmt == "jpg":
                    im.save(tmp, "JPEG", quality=int(jpeg_quality))
                else:
                    im.save(tmp, "PNG", compress_level=6)
                from .georef import replace_file
                replace_file(tmp, path)
            files.append(path)
            wf = os.path.splitext(path)[0] + gt.world_file_ext(path)
            gt.write_world_file(wf, x0 + c0 * px, y1 - r0 * px, px)
            files.append(wf)
            if prj:
                with open(os.path.splitext(path)[0] + ".prj", "w", encoding="ascii") as f:
                    f.write(prj)
                files.append(os.path.splitext(path)[0] + ".prj")
            if progress:
                progress(int(100 * (k + 1) / len(outs)), 100)
        if not is_dem:
            first = outs[0]
            if len(outs) == 1:
                files.append(_placement_notes(dst, first[0], info, lev, first[4], first[3], px, zref))
            else:
                # one note for the whole mosaic + one Rhino script per tile set
                files.append(_placement_notes(dst, first[0], info, lev, L.width, L.height, px, zref))
        log(f"Exported {len(outs)} file(s): {outs[0][0]}" + (" ..." if len(outs) > 1 else "")
            + f" ({L.width} x {L.height} px, {px:.4g} m/pix)")
    finally:
        src.close()
    return files


def read_info(folder_or_tif):
    """info dict of an ortho/DEM output folder, or of a GeoTIFF from elsewhere
    (Import Orthomosaic / Import DEM)."""
    p = folder_or_tif
    if os.path.isdir(p):
        with open(os.path.join(p, "info.json"), encoding="utf-8") as f:
            info = json.load(f)
        info["dir"] = p
        info["dir"] = os.path.abspath(p)
        for k in ("path", "height_path"):
            v = info.get(k)
            if v:      # always the files of THIS folder (it may have been copied or moved)
                cand = os.path.join(info["dir"], os.path.basename(v.replace("\\", "/")))
                if os.path.exists(cand):
                    info[k] = cand
                elif not os.path.exists(v):
                    info[k] = None
        if not info.get("path"):
            raise ValueError("the folder has no orthomosaic.tif / dem.tif")
        return info
    with gt.TiffReader(p) as r:
        if r.geo is None:
            raise ValueError("the TIFF has no georeferencing (ModelPixelScale / ModelTiepoint)")
        x0, y1, sx, sy = r.geo
        info = {"format": "imported", "dir": os.path.dirname(os.path.abspath(p)), "path": os.path.abspath(p),
                "width": r.width, "height": r.height, "pixel_size": sx, "origin": [x0, y1],
                "bounds": [x0, y1 - r.height * sy, x0 + r.width * sx, y1], "levels": len(r.levels),
                "bands": r.bands, "nodata": r.nodata, "bytes": os.path.getsize(p),
                "crs": f"EPSG:{r.epsg()}" if r.epsg() else "", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    return info


def estimate_gsd(model_dir, points_ply=None, georef=None, texture_info=None):
    """Quick photo-GSD estimate for the Build Orthomosaic dialog default:
    median over cameras of (height above the median tie point) / focal length.
    The texture's measured source GSD is preferred when it agrees with the
    cameras (within 2x): a texture built before georeferencing measured it in
    the old model units (issue #3: a 10x too fine default pixel size)."""
    est = _camera_gsd(model_dir, points_ply, georef)
    t = float((texture_info or {}).get("source_gsd") or 0)
    if t > 0 and (not est or 0.5 <= t / est <= 2.0):
        return float(nice(t))
    return est


def _camera_gsd(model_dir, points_ply=None, georef=None):
    from . import colmap_io as cio
    try:
        cams = cio.read_cameras(os.path.join(model_dir, "cameras.bin"))
        imgs = cio.read_images(os.path.join(model_dir, "images.bin"))
    except (OSError, ValueError):
        return 0.0
    if not imgs:
        return 0.0
    C = np.array([im.center() for im in imgs.values()])
    f = np.array([cams[im.camera_id].K()[0] for im in imgs.values()])
    up = np.array([0.0, 0, 1]) if georef else -np.mean([im.R()[2] for im in imgs.values()], 0)
    up /= np.linalg.norm(up) or 1
    ref = None
    if points_ply and os.path.exists(points_ply):
        try:
            from .plyio import read_fast
            r = read_fast(points_ply)
            if r is not None:
                v = r["vertex"]
                P = np.column_stack([v["x"], v["y"], v["z"]])[:: max(1, len(v) // 200000)]
                ref = float(np.median(P @ up))
        except Exception:  # noqa: BLE001
            ref = None
    if ref is None:
        ref = float(np.percentile(C @ up, 5)) - 50.0
    h = C @ up - ref
    ok = h > 0
    return float(nice(np.median(h[ok] / f[ok]))) if ok.any() else 0.0
