"""Processing metadata of a chunk (what Metashape shows in the Workspace tree
and the Properties pane): parameters, durations, timestamps and software
version of every processing step, stored in the project (.pmsx) under
Chunk.meta.

- record(...)  : called by the app when a step finishes.
- backfill(...): for projects processed before this existed (e.g. by the
  benchmark scripts), reconstruct what can be read from the logs/configs next
  to the project. Anything not in the logs is stored as None and shown as
  "unknown" - never guessed.
- summary(...) / properties(...): text for the tree rows and Properties pane.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re

from . import APP_NAME, APP_VERSION

UNKNOWN = "unknown"
STEPS = ("alignment", "depth_maps", "point_cloud", "model")


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(ts):
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def software():
    return f"{APP_NAME} {APP_VERSION}"


def file_info(path):
    if not path or not os.path.exists(path):
        return {"file": path or None, "file_size": None, "file_modified": None}
    st = os.stat(path)
    return {"file": path, "file_size": st.st_size, "file_modified": _iso(st.st_mtime)}


def record(chunk, step, params=None, seconds=None, stages=None, **results):
    """Store metadata of a step that just ran in the app."""
    meta = dict(getattr(chunk, "meta", None) or {})
    entry = {"params": dict(params or {}), "seconds": seconds, "stages": dict(stages or {}),
             "date": now_iso(), "software": software(), "source": "recorded"}
    entry.update(results)
    meta[step] = entry
    chunk.meta = meta
    return entry


# ------------------------------------------------------------------ log reading
def read_text(path):
    """Logs written by PowerShell Tee-Object are UTF-16; others UTF-8."""
    b = open(path, "rb").read()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    if b[:3] == b"\xef\xbb\xbf":
        b = b[3:]
    return b.decode("utf-8", "replace")


_STAGE = re.compile(r"^\s{2,}(\S.*?\S)\s{2,}(\d+(?:\.\d+)?) s\s*$")
_NUM = lambda s: int(s.replace(",", ""))


def parse_stages(text):
    """'    Detecting points (fine 2640px)     154.34 s' -> {name: seconds} (in order)."""
    out = {}
    for line in text.splitlines():
        m = _STAGE.match(line)
        if m and not m.group(1).startswith("["):
            name = m.group(1).strip()
            k, n = name, 2
            while k in out:
                k = f"{name} #{n}"; n += 1
            out[k] = float(m.group(2))
    return out


def _find(dirs, names):
    for d in dirs:
        for n in names:
            for p in sorted(glob.glob(os.path.join(d, n))):
                if os.path.isfile(p):
                    yield p


def _same(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def _load_json(p):
    try:
        return json.loads(read_text(p))
    except Exception:
        return None


def _backfill_alignment(dirs, chunk):
    for p in _find(dirs, ["benchmark_log*.txt", "align_log*.txt"]):
        t = read_text(p)
        if "registered" not in t and "Matching" not in t:
            continue
        st = parse_stages(t)
        e = {"params": {}, "stages": st, "source": f"backfilled from {os.path.basename(p)}",
             "software": None, "date": _iso(os.path.getmtime(p))}
        m = re.search(r"==\s*(\w+)\s*/\s*mapper\s*(\w+)\s*->\s*(\w+)", t)
        if m:
            e["params"].update(mode=m.group(1).capitalize(), mapper=f"{m.group(2)} -> {m.group(3)}")
        m = re.search(r"Pair preselection:\s*([\d,]+) of ([\d,]+)", t)
        if m:
            e["params"]["preselection"] = f"Estimated ({_NUM(m.group(1)):,} of {_NUM(m.group(2)):,} pairs)"
        m = re.search(r"Kept (\d+) best features per photo", t)
        e["params"]["key_point_limit"] = int(m.group(1)) if m else None
        e["params"]["tie_point_limit"] = None
        m = re.search(r"fine (\d+)\s*px", t)
        e["params"]["accuracy"] = f"fine matching at {m.group(1)} px" if m else None
        m = re.search(r"TOTAL\s+([\d.]+) s", t)
        e["seconds"] = float(m.group(1)) if m else None
        e["matching_seconds"] = round(sum(v for k, v in st.items() if k.startswith("Matching")), 2) or None
        e["alignment_seconds"] = round(sum(v for k, v in st.items() if k.startswith(
            ("Estimating", "Triangulating", "Registering", "Optimizing"))), 2) or None
        m = re.search(r"^\s*\w+\s+[\d.]+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$", t, re.M)
        if m:
            e["pairs"], e["verified_pairs"] = int(m.group(1)), int(m.group(2))
            e["registered"], e["tie_points"] = int(m.group(3)), int(m.group(4))
            e["rms_reprojection_px"] = float(m.group(5))
        e.setdefault("tie_points", chunk.tie_point_count or None)
        e.setdefault("rms_reprojection_px", None)
        e["max_reprojection_px"] = None
        e["mean_key_points"] = None
        m = re.search(r"(\d+) camera group\(s\):\s*(.*)", t)
        if m:
            e["camera_groups"] = m.group(2).strip()
        return e
    return None


def _backfill_dense(dirs, workdir):
    """Depth maps + fusion from dense_benchmark/dense_log and the dense/*.json configs."""
    dense = os.path.join(workdir, "dense")
    cfg = _load_json(os.path.join(dense, "workspace.json")) or _load_json(os.path.join(dense, "prepare.json"))
    if not cfg and not os.path.isdir(os.path.join(dense, "stereo")):
        return None, None
    dm = {"params": {"quality": (cfg or {}).get("quality"), "max_sources": (cfg or {}).get("max_sources"),
                     "depth_filtering": None},
          "stages": {}, "seconds": None, "software": None, "source": "backfilled from dense/workspace.json",
          "date": None}
    fuse = {"stages": {}, "fusion_seconds": None}
    logs = list(_find(dirs, ["dense_benchmark*.txt"]))
    if logs:
        t = read_text(logs[0])
        st = parse_stages(t)
        dm["stages"] = {k: v for k, v in st.items() if k.startswith(("Undistorting", "Building depth", "Cleaning"))}
        dm["seconds"] = st.get("Building depth maps")
        fuse["fusion_seconds"] = st.get("Building point cloud (fusion)")
        dm["source"] += f", {os.path.basename(logs[0])}"
        dm["date"] = _iso(os.path.getmtime(logs[0]))
    for p in _find([workdir], ["dense_log*.txt"]):
        t = read_text(p)
        from .dense import DEPTH_FILTERS
        args = {}
        for m in re.finditer(r"--(PatchMatchStereo|StereoFusion)\.(\w+) (\S+)", t):
            args[m.group(2)] = m.group(3)
        for name, f in DEPTH_FILTERS.items():
            try:
                if all(abs(float(args[k]) - float(v)) < 1e-9 for k, v in f.items() if k in args) and \
                        {"filter_min_num_consistent", "min_num_pixels"} <= set(args):
                    dm["params"]["depth_filtering"] = name; break
            except (KeyError, ValueError):
                pass
        m = re.search(r"===== Building depth maps\s+\[(\d\d:\d\d:\d\d)\]", t)
        if m:
            dm["started"] = m.group(1)
        dm["source"] += f", {os.path.basename(p)}"
        break
    maps = glob.glob(os.path.join(dense, "stereo", "depth_maps", "*.geometric.bin"))
    dm["count"] = len(maps) if maps else None
    return dm, fuse


def _backfill_cloud(dirs, workdir, chunk, fuse):
    path = chunk.point_cloud_ply
    if not path:
        return None
    e = {"params": {}, "filtering": [], "points": chunk.point_count or None, "seconds": None,
         "source": None, "software": None, "date": None}
    e.update(fuse or {})
    for p in _find(dirs, ["filter*.txt", "dense_benchmark*.txt"]):
        t = read_text(p)
        outs = [x.strip() for x in re.findall(r"->\s*(\S[^\n]*?\.ply)\s*$", t, re.M)]
        mine = any(_same(x, path) for x in outs)
        if not mine and "dense_benchmark" in os.path.basename(p):
            # the dense benchmark filters straight into dense/point_cloud.ply (no '->' line)
            mine = os.path.basename(path) == "point_cloud.ply" and "Point cloud:" in t
        if not mine:
            continue
        e["filtering"] = [{"filter": f.strip(), "removed": _NUM(n)}
                          for n, f in re.findall(r"removed ([\d,]+) points by (.+?) filter", t)]
        m = re.search(r"Point cloud:\s*([\d,]+)\s*->\s*([\d,]+) points", t)
        if m:
            e["points_fused"], e["points"] = _NUM(m.group(1)), _NUM(m.group(2))
        m = re.search(r"Filtering done in ([\d.]+) s", t) or re.search(r"Filtering point cloud\s+([\d.]+) s", t)
        e["filtering_seconds"] = float(m.group(1)) if m else None
        e["source"] = f"backfilled from {os.path.basename(p)}"
        e["date"] = _iso(os.path.getmtime(p))
        break
    cfgs = [c for c in (_load_json(p) for p in _find([os.path.join(workdir, "dense")], ["filter*.json"]))
            if c and _same(c.get("output", ""), path)]
    if cfgs:
        e["params"].update({k: v for k, v in cfgs[0].items() if k not in ("input", "vis", "output", "model")})
    e.update(file_info(path))
    return e


def _backfill_model(dirs, workdir, chunk):
    path = chunk.model_ply
    if not path:
        return None
    e = {"params": {}, "faces": chunk.face_count or None, "vertices": None, "seconds": None,
         "source": None, "software": None, "date": None}
    for p in _find(dirs, ["mesh*.txt", "dense_benchmark*.txt"]):
        t = read_text(p)
        m = re.search(r"Mesh:\s*([\d,]+) vertices,\s*([\d,]+) faces \(([\d.]+) s\)\s*->\s*(.+?\.ply)", t)
        if not m or not _same(m.group(4).strip(), path):
            continue
        e.update(vertices=_NUM(m.group(1)), faces=_NUM(m.group(2)), seconds=float(m.group(3)),
                 source=f"backfilled from {os.path.basename(p)}", date=_iso(os.path.getmtime(p)))
        m = re.search(r"Poisson depth (\d+)", t)
        if m:
            e["params"]["poisson_depth"] = int(m.group(1))
        break
    for p in _find([workdir], ["mesh*.json"]):
        c = _load_json(p)
        if c and _same(c.get("out", ""), path):
            e["params"].update(source=c.get("source"), surface_type=c.get("surface", "Arbitrary"),
                               face_count=c.get("face_count"), interpolation=c.get("interpolation"))
            break
    e.update(file_info(path))
    return e


def backfill(chunk, project_path=None):
    """Fill chunk.meta entries that are missing, from logs next to the project.
    Returns the list of steps that were added (empty if nothing changed)."""
    wd = chunk.workdir
    if not wd or not os.path.isdir(wd):
        return []
    meta = dict(getattr(chunk, "meta", None) or {})
    dirs = [wd, os.path.dirname(os.path.abspath(wd))]
    if project_path:
        dirs.append(os.path.dirname(os.path.abspath(project_path)))
    dirs = list(dict.fromkeys(dirs))
    added = []
    try:
        if "alignment" not in meta and chunk.tie_point_count:
            e = _backfill_alignment(dirs, chunk)
            if e:
                meta["alignment"] = e; added.append("alignment")
        fuse = None
        if "depth_maps" not in meta or "point_cloud" not in meta:
            dm, fuse = _backfill_dense(dirs, wd)
            if dm and "depth_maps" not in meta:
                meta["depth_maps"] = dm; added.append("depth_maps")
        if "point_cloud" not in meta and chunk.point_cloud_ply:
            e = _backfill_cloud(dirs, wd, chunk, fuse)
            if e:
                dm = meta.get("depth_maps") or {}
                e["params"].setdefault("quality", (dm.get("params") or {}).get("quality"))
                e["params"].setdefault("depth_filtering", (dm.get("params") or {}).get("depth_filtering"))
                meta["point_cloud"] = e; added.append("point_cloud")
        if "model" not in meta and chunk.model_ply:
            e = _backfill_model(dirs, wd, chunk)
            if e:
                meta["model"] = e; added.append("model")
    except Exception as ex:  # a broken log must never stop the project from opening
        meta.setdefault("_backfill_error", str(ex))
    if added:
        chunk.meta = meta
    return added


# ------------------------------------------------------------------ display
def sparse_stats(model):
    """Tie point statistics of a COLMAP sparse model (Metashape's tie point
    properties): counts, projections, RMS / max reprojection error (px) and
    track lengths. Observations are grouped by photo with one sort (not one
    scan of all observations per photo)."""
    import numpy as np
    from . import colmap_io as cio
    P = model.points
    n = int(len(P))
    out = {"tie_points": n, "photos": int(len(model.images))}
    if not n:
        return out
    tl = np.asarray(P.track_len())
    pidx = np.repeat(np.arange(n), tl)
    imgs = P.track_img.astype(np.int64)
    order = np.argsort(imgs, kind="stable")
    si = imgs[order]
    ids, starts = np.unique(si, return_index=True)
    ends = list(starts[1:]) + [len(si)]
    sq = 0.0; cnt = 0; mx = 0.0
    per_photo = []
    for iid, a, b in zip(ids, starts, ends):
        im = model.images.get(int(iid))
        if im is None:
            continue
        sel = order[a:b]
        uv, z = cio.project(model.cameras[im.camera_id], im.R(), im.tvec, P.xyz[pidx[sel]])
        e = np.linalg.norm(uv - im.xys[P.track_idx[sel]], axis=1)
        e = e[np.isfinite(e) & (z > 0)]
        if len(e):
            sq += float(np.sum(e * e)); cnt += len(e); mx = max(mx, float(e.max()))
        per_photo.append(b - a)
    out.update(projections=int(len(imgs)),
               rms_reprojection_px=round(float(np.sqrt(sq / cnt)), 4) if cnt else None,
               max_reprojection_px=round(mx, 3) if cnt else None,
               mean_track_length=round(float(tl.mean()), 3),
               mean_projections_per_photo=round(float(np.mean(per_photo)), 1) if per_photo else None)
    return out


def fmt(v, unit=""):
    if v is None or v == "":
        return UNKNOWN
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, int):
        return f"{v:,}{unit}"
    if isinstance(v, float):
        return f"{v:,.3f}{unit}" if abs(v) < 100 else f"{v:,.1f}{unit}"
    return f"{v}{unit}"


def fmt_time(s):
    if s is None:
        return UNKNOWN
    s = float(s)
    if s < 60:
        return f"{s:.1f} s"
    h, rem = divmod(int(round(s)), 3600)
    m, sec = divmod(rem, 60)
    return f"{h} h {m:02d} min {sec:02d} s" if h else f"{m} min {sec:02d} s"


def fmt_size(n):
    if n is None:
        return UNKNOWN
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024.0


def _common(e):
    return [("Date created", fmt(e.get("date"))), ("Software version", fmt(e.get("software"))),
            ("Metadata source", fmt(e.get("source")))]


def camera_groups(chunk):
    """Sensor groups by image size: [(label, aligned, total)]."""
    groups = {}
    for c in chunk.cameras:
        key = (c.width, c.height) if c.width and c.height else None
        g = groups.setdefault(key, [0, 0])
        g[1] += 1
        g[0] += 1 if c.aligned else 0
    out = []
    for k, (a, n) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        out.append((f"{k[0]}x{k[1]}" if k else "unknown size", a, n, k))
    return out


def properties(chunk, kind, index=None):
    """[(name, value)] for the Properties pane."""
    meta = getattr(chunk, "meta", None) or {}
    gr = chunk.georef or {}
    if kind == "chunk":
        rows = [("Chunk", chunk.name), ("Images", f"{len(chunk.cameras):,}"),
                ("Aligned", f"{chunk.aligned_count():,}/{len(chunk.cameras):,}"),
                ("Markers", f"{len(chunk.markers or []):,}"),
                ("Tie points", fmt(chunk.tie_point_count or None)),
                ("Coordinate system", gr.get("crs") or chunk.crs or "Local (not georeferenced)"),
                ("Georeferenced", "Yes" if gr else "No"), ("Working folder", chunk.workdir or UNKNOWN)]
        if gr.get("similarity"):
            import numpy as np
            M = np.asarray(gr["similarity"], float)
            s = float(np.cbrt(abs(np.linalg.det(M[:3, :3]))))
            rows += [("Transform scale", f"{s:.6f}"),
                     ("Transform translation", ", ".join(f"{v:.3f}" for v in M[:3, 3])),
                     ("Coordinate offset", ", ".join(f"{v:.1f}" for v in gr.get("offset") or []))]
        return rows
    if kind in ("cameras", "group"):
        rows = [("Images", f"{len(chunk.cameras):,}"), ("Aligned", f"{chunk.aligned_count():,}"),
                ("Disabled", f"{sum(1 for c in chunk.cameras if not c.enabled):,}")]
        for label, a, n, _k in camera_groups(chunk):
            rows.append((f"Camera group {label}", f"{a}/{n} aligned"))
        return rows
    if kind == "markers":
        ms = chunk.markers or []
        rows = [("Markers", f"{len(ms):,}"), ("Control", f"{sum(1 for m in ms if m.get('enabled', True)):,}"),
                ("Check", f"{sum(1 for m in ms if not m.get('enabled', True)):,}"),
                ("Projections", f"{sum(len(m.get('projections', {})) for m in ms):,}")]
        for key, name in (("control_rmse", "Control points RMSE"), ("check_rmse", "Check points RMSE")):
            r = gr.get(key)
            rows.append((name, f"{r['total']:.3f} m (X {r['x']:.3f}, Y {r['y']:.3f}, Z {r['z']:.3f}; n={r['n']})"
                         if r else "not computed (run Optimize Cameras)"))
        return rows
    if kind == "tie":
        e = meta.get("alignment") or {}
        p = e.get("params") or {}
        ss = meta.get("sparse_stats") or {}
        rms = e.get("rms_reprojection_px")
        if rms is None:
            rms = ss.get("rms_reprojection_px")
        bundle_rms = ((gr.get("bundle") or {}).get("tie_rms_px")) if gr else None
        return [("Tie points", fmt(chunk.tie_point_count or e.get("tie_points"))),
                ("Accuracy", fmt(p.get("accuracy"))), ("Key point limit", fmt(p.get("key_point_limit"))),
                ("Tie point limit", fmt(p.get("tie_point_limit"))), ("Preselection", fmt(p.get("preselection"))),
                ("Matching time", fmt_time(e.get("matching_seconds"))),
                ("Alignment time", fmt_time(e.get("alignment_seconds"))),
                ("Total processing time", fmt_time(e.get("seconds"))),
                ("RMS reprojection error", fmt(rms, " px") if rms is not None else UNKNOWN),
                ("Max reprojection error", fmt(e.get("max_reprojection_px") if e.get("max_reprojection_px")
                                               is not None else ss.get("max_reprojection_px"), " px")),
                ("Projections", fmt(ss.get("projections"))),
                ("Mean projections per photo", fmt(ss.get("mean_projections_per_photo"))),
                ("Mean track length", fmt(ss.get("mean_track_length"))),
                ("Mean key points", fmt(e.get("mean_key_points")))] + _common(e)
    if kind == "depth":
        e = meta.get("depth_maps") or {}
        p = e.get("params") or {}
        return [("Depth maps", fmt(e.get("count"))), ("Quality", fmt(p.get("quality"))),
                ("Depth filtering", fmt(p.get("depth_filtering"))), ("Neighbour photos", fmt(p.get("max_sources"))),
                ("Depth maps generation time", fmt_time(e.get("seconds")))] + _common(e)
    if kind == "cloud":
        e = meta.get("point_cloud") or {}
        p = e.get("params") or {}
        rows = [("Points", fmt(chunk.point_count or e.get("points"))), ("Quality", fmt(p.get("quality"))),
                ("Depth filtering", fmt(p.get("depth_filtering"))),
                ("Depth maps generation time", fmt_time((meta.get("depth_maps") or {}).get("seconds"))),
                ("Fusion time", fmt_time(e.get("fusion_seconds"))),
                ("Points after fusion", fmt(e.get("points_fused"))),
                ("Filtering time", fmt_time(e.get("filtering_seconds")))]
        for f in e.get("filtering") or []:
            rows.append((f"Filter: {f['filter']}", f"{f['removed']:,} points removed"))
        if not e.get("filtering"):
            rows.append(("Filtering steps", UNKNOWN))
        rows += [("File size", fmt_size(e.get("file_size") or file_info(chunk.point_cloud_ply)["file_size"])),
                 ("File", chunk.point_cloud_ply)] + _common(e)
        return rows
    if kind == "tiled":
        t = getattr(chunk, "tiled_model", None) or {}
        e = meta.get("tiled_model") or {}
        return [("Levels", fmt(t.get("levels"))), ("Tiles", fmt(t.get("tiles"))),
                ("Pixel size", pixel_size_text(t.get("pixel_size"), t.get("georef"))),
                ("Source data", fmt(t.get("source"))), ("Format", "Cesium 3D Tiles (" + str(t.get("format")) + ")"),
                ("Faces (source)", fmt(t.get("faces"))), ("Points (source)", fmt(t.get("points"))),
                ("Size", fmt_size(t.get("bytes"))), ("Processing time", fmt_time(t.get("seconds"))),
                ("Folder", t.get("dir") or UNKNOWN)] + _common(e)
    if kind in ("ortho", "dem"):
        t = (getattr(chunk, "orthomosaic", None) if kind == "ortho" else getattr(chunk, "dem", None)) or {}
        e = meta.get("orthomosaic" if kind == "ortho" else "dem") or {}
        geo = t.get("georef", bool(chunk.georef))
        b = t.get("bounds")
        rows = [("Size", f"{t.get('width', 0):,} x {t.get('height', 0):,} px"),
                ("Resolution", pixel_size_text(t.get("pixel_size"), geo)),
                ("Coordinate system", t.get("crs") or (chunk.georef or {}).get("crs") or chunk.crs
                 or ("Local Coordinates (m)" if geo else "Local (not georeferenced)")),
                ("Extent X", f"{b[0]:.3f} .. {b[2]:.3f}" if b else UNKNOWN),
                ("Extent Y", f"{b[1]:.3f} .. {b[3]:.3f}" if b else UNKNOWN)]
        zr = t.get("z_range")
        if zr and zr[0] is not None:
            rows.append(("Elevation range", f"{zr[0]:.2f} .. {zr[1]:.2f} m"))
        if kind == "ortho":
            rows += [("Surface", fmt(t.get("surface"))), ("Blending mode", fmt(t.get("blending"))),
                     ("Hole filling", fmt(t.get("hole_filling"))), ("Ghosting filter", fmt(t.get("ghosting"))),
                     ("Refine seamlines", fmt(t.get("refine_seamlines"))),
                     ("Photo ground resolution", pixel_size_text(t.get("source_gsd"), geo)),
                     ("Photos used", fmt(t.get("photos_used")))]
        else:
            rows += [("Source data", fmt(t.get("source"))), ("Interpolation", fmt(t.get("interpolation")))]
        rows += [("Overview levels", fmt(t.get("levels"))), ("Processing time", fmt_time(t.get("seconds"))),
                 ("Date created", fmt(t.get("created"))),
                 ("File size", fmt_size(t.get("bytes"))), ("File", t.get("path") or UNKNOWN)]
        st = t.get("stages") or {}
        for k, v in st.items():
            rows.append((f"Stage: {k}", fmt_time(v)))
        return rows + [r for r in _common(e) if r[0] != "Date created"]
    if kind == "model":
        e = meta.get("model") or {}
        p = e.get("params") or {}
        t = getattr(chunk, "texture", None) or {}
        te = meta.get("texture") or {}
        trows = []
        if t:
            metric = t.get("georef")
            trows = [("Texture", f"{t.get('size')} x {t.get('size')}, {t.get('count')} page(s)"),
                     ("Mapping mode", fmt(t.get("mapping"))), ("Blending mode", fmt(t.get("blending"))),
                     ("Hole filling", fmt(t.get("hole_filling"))), ("Ghosting filter", fmt(t.get("ghosting"))),
                     ("Texel size", pixel_size_text(t.get("texel_size"), metric)),
                     ("Photo ground resolution", pixel_size_text(t.get("source_gsd"), metric)),
                     ("Untextured faces", fmt(t.get("faces_untextured"))),
                     ("Texturing time", fmt_time(t.get("seconds"))), ("Texture file", t.get("obj") or UNKNOWN)]
        return trows + [("Faces", fmt(chunk.face_count or e.get("faces"))), ("Vertices", fmt(e.get("vertices"))),
                ("Source data", fmt(p.get("source"))), ("Surface type", fmt(p.get("surface_type"))),
                ("Face count", fmt(p.get("face_count"))), ("Interpolation", fmt(p.get("interpolation"))),
                ("Processing time", fmt_time(e.get("seconds"))),
                ("File size", fmt_size(e.get("file_size") or file_info(chunk.model_ply)["file_size"])),
                ("File", chunk.model_ply)] + _common(e)
    return []


def summary(chunk, kind):
    """Text in parentheses after a tree row name (Metashape style)."""
    meta = getattr(chunk, "meta", None) or {}
    if kind == "tie":
        return f"{chunk.tie_point_count:,} points"
    if kind == "depth":
        e = meta.get("depth_maps") or {}
        p = e.get("params") or {}
        parts = [f"{e['count']:,}" if e.get("count") else None,
                 f"{p['quality']} quality" if p.get("quality") else None,
                 f"{p['depth_filtering']} filtering" if p.get("depth_filtering") else None]
        return ", ".join(x for x in parts if x) or UNKNOWN
    if kind == "cloud":
        q = ((meta.get("point_cloud") or {}).get("params") or {}).get("quality")
        return f"{chunk.point_count:,} points" + (f", {q} quality" if q else "")
    if kind == "model":
        q = ((meta.get("model") or {}).get("params") or {}).get("face_count")
        t = getattr(chunk, "texture", None) or {}
        tex = f", Texture {t['size']}x{t['size']}" + (f" x {t['count']}" if t.get("count", 1) > 1 else "") \
            if t.get("size") else ""
        return f"{chunk.face_count:,} faces" + (f", {q} face count" if q else "") + tex
    if kind == "tiled":
        t = getattr(chunk, "tiled_model", None) or {}
        return f"{t.get('levels', '?')} levels, {pixel_size_text(t.get('pixel_size'), t.get('georef'))}"
    if kind in ("ortho", "dem"):
        t = (getattr(chunk, "orthomosaic", None) if kind == "ortho" else getattr(chunk, "dem", None)) or {}
        geo = t.get("georef", bool(chunk.georef))
        return f"{t.get('width', 0):,} x {t.get('height', 0):,}, {pixel_size_text(t.get('pixel_size'), geo)}"
    return ""


def pixel_size_text(v, metric):
    if not v:
        return UNKNOWN
    return f"{v * 100:.3g} cm/pix" if metric else f"{v:.3g} units/pix (not georeferenced)"
