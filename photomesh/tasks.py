"""Long-running Python processing steps, run as a child process so the GUI
stays responsive:  python -m photomesh.tasks <command> <config.json>

Progress lines use COLMAP's "[i/n]" form, which PipelineRunner understands.
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np


def _log(msg):
    print(msg, flush=True)


def _progress(i, n):
    print(f"[{i}/{n}]", flush=True)


def cmd_dense_prepare(cfg):
    from .dense import prepare_workspace
    t0 = time.perf_counter()
    r = prepare_workspace(cfg["model"], cfg["image_root"], cfg["dense"], cfg.get("quality", "Medium"),
                          cfg.get("max_sources", 10), log=_log, progress=_progress)
    _log(f"Undistorted {r['undistorted']} of {r['images']} photos in {time.perf_counter() - t0:.1f} s"
         + (f" ({r['reused']} reused from the previous run)" if r.get("reused") else ""))


def _cameras(model_dir):
    from . import colmap_io as cio
    imgs = cio.read_images(os.path.join(model_dir, "images.bin"))
    C = np.array([im.center() for im in imgs.values()])
    D = np.array([im.R()[2] for im in imgs.values()])   # viewing direction (world)
    return C, D


def cmd_filter_cloud(cfg):
    from . import pcfilter as pf
    from .dense import read_vis_counts
    t0 = time.perf_counter()
    cloud = pf.read_cloud(cfg["input"])
    _log(f"Read {len(cloud['xyz']):,} fused points ({time.perf_counter() - t0:.1f} s)")
    if os.path.exists(cfg.get("vis", "")):
        t1 = time.perf_counter()
        conf = read_vis_counts(cfg["vis"])
        if len(conf) == len(cloud["xyz"]):
            cloud["confidence"] = conf
            _log(f"Confidence (photos per point): median {int(np.median(conf))}, "
                 f"{(conf <= 2).mean() * 100:.1f}% seen in <=2 photos ({time.perf_counter() - t1:.1f} s)")
    C = D = None
    if cfg.get("model") and os.path.isdir(cfg["model"]):
        C, D = _cameras(cfg["model"])
    up = cfg.get("up")
    cloud, stats = pf.filter_cloud(cloud, cfg, C, D, up, log=_log)
    pf.write_cloud(cfg["output"], cloud["xyz"], cloud.get("rgb"), cloud.get("normals"),
                   cloud.get("confidence"))
    _log(f"POINTS {len(cloud['xyz'])}")
    _log(f"Filtering done in {time.perf_counter() - t0:.1f} s -> {cfg['output']}")


def cmd_build_mesh(cfg):
    from .mesh import build_mesh
    r = build_mesh(cfg, log=_log)
    _log(f"FACES {r['faces']}")


def cmd_georef(cfg):
    from . import georef
    georef.run_task(cfg, log=_log)


def cmd_refine_markers(cfg):
    """Tools > Markers > Refine Markers: predict every marker in every aligned
    photo it should be visible in, refine locally (template from a pinned
    placement, else checkerboard saddle), multi-view consensus.
    cfg: model, image_root, markers (Marker dicts), offset (or null),
         use_reference (bool), search (px), output (json)."""
    from . import colmap_io as cio, georef as g, marker_refine as mr
    t0 = time.perf_counter()
    model = cio.read_model(cfg["model"])
    names = g.name_index(model)
    off = np.asarray(cfg.get("offset") or [0, 0, 0], float)
    markers = [g.Marker(**m) for m in cfg["markers"]]
    plan, Xs = {}, {}
    for m in markers:
        X = None
        pos = (cfg.get("positions") or {}).get(m.label)
        if pos is not None:
            X = np.asarray(pos, float)
        elif cfg.get("use_reference") and m.ref is not None:
            X = np.asarray(m.ref, float) - off
        if X is None:
            X, _n = g.triangulate_marker(model, m.projections, names)
        if X is None:
            continue
        Xs[m.label] = X
        for iid in model.images:
            p = g.predict_projection(model, iid, X)
            cam = model.cameras[model.images[iid].camera_id]
            if p is None or not (40 <= p[0] < cam.width - 40 and 40 <= p[1] < cam.height - 40):
                continue
            plan.setdefault(iid, []).append(((m.label, iid), p))
    _log(f"{len(Xs)} markers predicted in {sum(len(v) for v in plan.values())} photo locations")
    views = mr.crop_views(model, cfg["image_root"], plan, radius=200)
    _log(f"Read {len(views)} photo crops ({time.perf_counter() - t0:.1f} s)")
    out = {}
    for k, m in enumerate(markers):
        if m.label not in Xs:
            continue
        ids = [iid for iid in model.images if (m.label, iid) in views]
        pins = {names[g._stem(p)]: (v[0], v[1]) for p, v in m.projections.items()
                if g._stem(p) in names and (len(v) < 3 or v[2])}
        res = mr.auto_place(model, cfg["image_root"], Xs[m.label], [i for i in ids if i not in pins],
                            pinned=pins or None, method="auto", search=int(cfg.get("search", 25)),
                            views=lambda i, lab=m.label: views.get((lab, i)))
        out[m.label] = {g._stem(model.images[i].name): [u, v, False, sc, how] for i, (u, v, sc, how) in res.items()}
        _progress(k + 1, len(markers))
    with open(cfg["output"], "w", encoding="utf-8") as f:
        json.dump(out, f)
    n = sum(len(v) for v in out.values())
    _log(f"Placed {n} marker projections in {time.perf_counter() - t0:.1f} s")


def _result(cfg, info):
    if cfg.get("result"):
        with open(cfg["result"], "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1, default=float)


def model_up(model_dir, georef):
    """Up direction of the chunk: z when georeferenced, else minus the mean
    camera viewing direction of the COLMAP model (aerial blocks)."""
    import numpy as np
    if georef or not model_dir or not os.path.exists(os.path.join(model_dir, "images.bin")):
        return [0.0, 0.0, 1.0]
    from . import colmap_io as cio
    imgs = cio.read_images(os.path.join(model_dir, "images.bin"))
    d = np.mean([im.R()[2] for im in imgs.values()], 0)
    n = np.linalg.norm(d)
    return (-d / n).tolist() if n > 0 else [0.0, 0.0, 1.0]


def cmd_build_texture(cfg):
    """Workflow > Build Texture (see texture.py)."""
    from .texture import build_texture
    info = build_texture(cfg, log=_log, progress=_progress)
    _result(cfg, info)


def cmd_build_tiles(cfg):
    """Workflow > Build Tiled Model (see tiles.py)."""
    from .tiles import build_tiles
    cfg = dict(cfg)
    if not cfg.get("up"):
        cfg["up"] = model_up(cfg.get("model"), bool(cfg.get("georef")))
    info = build_tiles(cfg, log=_log, progress=_progress)
    info["up"] = list(cfg["up"])
    _result(cfg, info)


def cmd_build_ortho(cfg):
    """Workflow > Build Orthomosaic (see ortho.py)."""
    from .ortho import build_orthomosaic
    info = build_orthomosaic(cfg, log=_log, progress=_progress)
    _result(cfg, info)


def cmd_build_dem(cfg):
    """Workflow > Build DEM (see ortho.py)."""
    from .ortho import build_dem
    info = build_dem(cfg, log=_log, progress=_progress)
    _result(cfg, info)


def cmd_export_raster(cfg):
    """File > Export > Export Orthomosaic / Export DEM (large rasters)."""
    from .ortho import export_raster
    info = cfg.pop("info")
    files = export_raster(info, cfg.pop("dst"), log=_log, progress=_progress, **cfg)
    _log("FILES " + json.dumps(files))


COMMANDS = {"build_ortho": cmd_build_ortho, "build_dem": cmd_build_dem, "export_raster": cmd_export_raster,
            "build_texture": cmd_build_texture, "build_tiles": cmd_build_tiles,
            "refine_markers": cmd_refine_markers, "dense_prepare": cmd_dense_prepare, "filter_cloud": cmd_filter_cloud,
            "build_mesh": cmd_build_mesh, "georef": cmd_georef}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2 or argv[0] not in COMMANDS:
        print("usage: python -m photomesh.tasks {%s} config.json" % ",".join(COMMANDS)); return 2
    with open(argv[1], encoding="utf-8-sig") as f:      # tolerate a BOM (Windows editors)
        cfg = json.load(f)
    COMMANDS[argv[0]](cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
