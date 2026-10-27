"""Georeference an aligned model with GCPs and print per-marker errors.

    python gcp_benchmark.py <model_dir> <markers file> [--check L1,L2] [--holdout N]
                            [--no-bundle] [--report out.txt] [--copy-to DIR]
--holdout N turns every N-th control marker into a check point (independent accuracy test).
--copy-to runs on a copy of the model (the original stays untouched).
"""
import argparse
import json
import os
import shutil
import sys
import time

import numpy as np

from photomesh import georef as g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model"); ap.add_argument("markers")
    ap.add_argument("--check", default=""); ap.add_argument("--holdout", type=int, default=0)
    ap.add_argument("--no-bundle", action="store_true"); ap.add_argument("--report")
    ap.add_argument("--copy-to"); ap.add_argument("--tie-points", type=int, default=40000)
    ap.add_argument("--marker-acc", type=float, default=0.005)
    ap.add_argument("--control-auto", type=int, default=0,
                    help="use only N well-spread markers as control, all others become check points")
    ap.add_argument("--iterations", type=int, default=30)
    ap.add_argument("--intrinsics", default="f,cx,cy,k1-k3,p1,p2", choices=list(g.INTRINSIC_SETS))
    a = ap.parse_args()
    model = a.model
    if a.copy_to:
        shutil.rmtree(a.copy_to, ignore_errors=True)
        shutil.copytree(model, a.copy_to); model = a.copy_to
    ms, crs = g.import_markers(a.markers)
    checks = {c.strip() for c in a.check.split(",") if c.strip()}
    k = 0
    for m in ms:
        if m.label in checks:
            m.enabled = False
        elif a.holdout and m.enabled and len(m.projections) >= 2:
            k += 1
            if k % a.holdout == 0:
                m.enabled = False
    if a.control_auto:
        import numpy as np
        cand = [m for m in ms if m.enabled and m.ref is not None and len(m.projections) >= 5]
        xy = np.array([m.ref[:2] for m in cand], float)
        pick = [int(np.argmin(np.linalg.norm(xy - xy.mean(0), axis=1)))]
        while len(pick) < min(a.control_auto, len(cand)):
            d = np.min(np.linalg.norm(xy[:, None] - xy[pick][None], axis=2), axis=1)
            pick.append(int(np.argmax(d)))
        keep = {cand[i].label for i in pick}
        for m in ms:
            m.enabled = m.label in keep
        print("control markers:", ", ".join(sorted(keep, key=lambda l: (len(l), l))), flush=True)
    t0 = time.perf_counter()
    out = g.run_task({"model": model, "markers": [m.to_dict() for m in ms], "crs": crs.name if crs.kind != "local"
                      else 'LOCAL_CS["Local Coordinates (m)"]', "bundle": not a.no_bundle,
                      "tie_points": a.tie_points, "intrinsics": a.intrinsics,
                      "fit_intrinsics": a.intrinsics != "none", "verbose": True, "max_iterations": a.iterations, "report": a.report,
                      "result": (a.report or os.path.join(model, "gcp")) + ".json",
                      "accuracies": {"marker_accuracy": a.marker_acc, "marker_accuracy_z": a.marker_acc}},
                     log=lambda *x: print(*x, flush=True))
    print(f"TOTAL {time.perf_counter() - t0:.1f} s")
    rows = sorted(out["rows"], key=lambda r: -r["total"])
    print("worst markers:", ", ".join(f"{r['label']}({r['type']}) {r['total']:.3f} m" for r in rows[:8]))


if __name__ == "__main__":
    sys.exit(main())
