"""Benchmark Build Point Cloud (+ filtering) and Build Mesh on an aligned workdir.

    python dense_benchmark.py <workdir> [--quality Medium] [--filtering Mild]
                              [--max-sources 10] [--mesh Poisson|Delaunay|none]
                              [--faces Medium] [--reuse]
"""
import argparse
import json
import os
import sys
import time

from photomesh import colmap as cm
from photomesh.dense import dense_steps
from photomesh.fastpipe import run_steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--quality", default="Medium")
    ap.add_argument("--filtering", default="Mild")
    ap.add_argument("--max-sources", type=int, default=8)
    ap.add_argument("--window-step", type=int, default=2)
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--mesh", default="Poisson")
    ap.add_argument("--faces", default="Medium")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--elevation", action="store_true")
    a = ap.parse_args()
    wd = os.path.abspath(a.workdir)
    exe = cm.find_colmap()
    print("COLMAP:", exe)
    s = {"quality": a.quality, "depth_filtering": a.filtering, "max_sources": a.max_sources,
         "window_step": a.window_step, "num_iterations": a.iterations, "reuse": a.reuse,
         "filters": {"min_confidence": 3, "statistical": True, "radius": True,
                     "elevation": a.elevation}}
    log = os.path.join(wd, "dense_log.txt")
    t0 = time.perf_counter()
    steps = dense_steps(exe, wd, s, cm.image_dir(wd))
    timings = run_steps(exe, steps, logfile=log)
    td = time.perf_counter() - t0
    print(f"  DENSE TOTAL {td:.1f} s")
    if a.mesh != "none":
        from photomesh.mesh_steps import mesh_steps
        t1 = time.perf_counter()
        ms, _ = mesh_steps(exe, wd, {"source": "Point cloud" if a.mesh == "Poisson" else "Depth maps",
                                  "face_count": a.faces, "interpolation": "Enabled"},
                        os.path.join(wd, "dense", "point_cloud.ply"))
        run_steps(exe, ms, logfile=log)
        print(f"  MESH TOTAL {time.perf_counter() - t1:.1f} s")
    tail = open(log, encoding="utf-8", errors="replace").read().splitlines()
    for l in tail:
        if l.startswith(("POINTS", "FACES", "  removed", "Point cloud:", "PatchMatch sources",
                         "Confidence", "Poisson", "Decimation", "Mesh:", "Cleanup", "Removed",
                         "Voxel", "Undistorted")):
            print("   ", l)


if __name__ == "__main__":
    sys.exit(main())
