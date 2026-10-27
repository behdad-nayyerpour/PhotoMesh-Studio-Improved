#!/usr/bin/env python3
"""Compare alignment modes: Fast (fastpipe) vs Exhaustive vs Sequential.

Usage:
    python benchmark.py <image_folder> [--modes fast exhaustive sequential]
                        [--accuracy High] [--no-gpu] [--colmap PATH] [--out DIR]

Reports wall time per stage, number of matched pairs, registered images,
3D tie points and mean reprojection error (from `colmap model_analyzer`).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from photomesh import fastpipe as fp
from photomesh.fastpipe import analyze_images, run_steps
from photomesh import colmap as cm
from photomesh.project import IMAGE_EXTS


class _Cam:
    def __init__(self, path):
        self.path, self.enabled, self.label = path, True, os.path.basename(path)


class _Chunk:
    def __init__(self, paths):
        self.cameras = [_Cam(p) for p in paths]


def analyze_model(exe, model_dir):
    r = subprocess.run([exe, "model_analyzer", "--path", model_dir], capture_output=True, text=True)
    t = r.stdout + r.stderr
    def g(pat, cast=float):
        m = re.search(pat, t)
        return cast(m.group(1)) if m else None
    return {"registered": g(r"Registered images:\s*(\d+)", int),
            "points": g(r"Points:\s*(\d+)", int),
            "reproj": g(r"Mean reprojection error:\s*([\d.]+)")}


def count_pairs(db):
    import sqlite3
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM two_view_geometries WHERE rows > 0").fetchone()[0], \
               con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    finally:
        con.close()


def count_pairs_file(p):
    with open(p, encoding="utf-8") as f:
        return sum(1 for l in f if l.strip())


def save_project(path, paths, infos, mapping, wd, exe):
    """Write a .pmsx the app can open, pointing at this run's results."""
    wd = os.path.abspath(wd)          # never store cwd-relative paths in the project
    from photomesh.project import Project, Chunk
    sparse_txt = os.path.join(wd, "sparse_txt")
    prj = Project()
    ch = Chunk(name="Chunk 1", workdir=wd)
    ch.add_photos(paths, infos)
    poses = cm.read_images_txt(os.path.join(sparse_txt, "images.txt"))
    for c in ch.cameras:
        p = poses.get(mapping.get(c.path, ""))
        c.aligned = p is not None
        c.est = p[0].tolist() if p else None
        c.rot = p[1].tolist() if p else None
    ch.tie_points_ply = os.path.join(wd, "tie_points.ply")
    ch.tie_point_count = cm.count_points3d(os.path.join(sparse_txt, "points3D.txt"))
    prj.chunks = [ch]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    prj.save(path)
    print(f"  project saved: {path} ({ch.aligned_count()}/{len(ch.cameras)} aligned)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images")
    ap.add_argument("--modes", nargs="+", default=["fast", "exhaustive", "sequential"])
    ap.add_argument("--accuracy", default="High", choices=cm.ACCURACY)
    ap.add_argument("--key-points", type=int, default=40000)
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--colmap", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--save-project", default=None, help="write a .pmsx with the first mode's result")
    ap.add_argument("--max-pairs-per-image", type=int, default=20)
    ap.add_argument("--mapper", nargs="+", default=["Auto"], choices=["Auto", "Global", "Incremental", "Hierarchical"],
                    help="camera estimation mapper(s); several = compare them")
    a = ap.parse_args()

    exe = cm._resolve(a.colmap) if a.colmap else cm.find_colmap()
    if not exe:
        sys.exit("COLMAP not found on PATH (use --colmap PATH).")
    paths = sorted(os.path.join(a.images, f) for f in os.listdir(a.images)
                   if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
    out_root = a.out or tempfile.mkdtemp(prefix="photomesh_bench_")
    print(f"COLMAP: {exe}\nImages: {len(paths)}  Output: {out_root}\n")

    t0 = time.perf_counter()
    infos = analyze_images(paths, os.path.join(out_root, "cache"))
    t_an = time.perf_counter() - t0
    print(f"Parallel image analysis (EXIF+thumbnail+descriptor): {t_an:.2f} s\n")

    results = []
    runs = [(m, mp) for m in a.modes for mp in a.mapper]
    for mode, mapper in runs:
        tag = mode if len(a.mapper) == 1 else f"{mode}_{mapper.lower()}"
        wd = os.path.join(out_root, tag)
        shutil.rmtree(wd, ignore_errors=True)
        chunk = _Chunk(paths)
        mapping = cm.prepare_images(chunk, wd)
        names = [mapping[p] for p in paths]
        s = {"mode": mode.capitalize(), "accuracy": a.accuracy, "key_point_limit": a.key_points,
             "generic": True, "reference": True, "single_camera": True,
             "use_gpu": not a.no_gpu, "coarse_to_fine": True,
             "max_pairs_per_image": a.max_pairs_per_image, "mapper": mapper}
        print(f"== {mode} / mapper {mapper} -> {fp.choose_mapper(s, len(paths))} ==", flush=True)
        steps = cm.align_steps(exe, chunk, wd, s, names=names, infos=[infos[p] for p in paths])
        t0 = time.perf_counter()
        try:
            timings = run_steps(exe, steps, log=lambda m: print(m, flush=True),
                                logfile=os.path.join(wd, "colmap_log.txt"))
            ok = True
        except Exception as e:
            print("  FAILED:", str(e)[:500]); ok = False
        total = time.perf_counter() - t0
        stats = analyze_model(exe, os.path.join(wd, "sparse", "0")) if ok else {}
        vp, mp = count_pairs(os.path.join(wd, "database.db")) if ok else (None, None)
        results.append((tag, total, mp, vp, stats))
        print(f"  TOTAL {total:.2f} s", flush=True)
        n = len(paths)
        if ok and mp:
            fine = os.path.join(wd, "pairs_fine.txt")
            if os.path.exists(fine):   # exhaustive would match at full accuracy -> use fine-stage rate
                match_t = sum(t for l, t in timings if l.startswith("Matching points (co-visible"))
                match_p = count_pairs_file(fine)
            else:
                match_t = sum(t for l, t in timings if l.startswith("Matching points"))
                pf = os.path.join(wd, "pairs.txt")
                match_p = count_pairs_file(pf) if os.path.exists(pf) else mp
            per_pair = match_t / max(match_p, 1)
            print(f"  matching: {match_p} pairs in {match_t:.1f} s ({per_pair * 1000:.1f} ms/pair); "
                  f"exhaustive would need {n * (n - 1) // 2} pairs -> ~{per_pair * n * (n - 1) / 2 / 60:.0f} min "
                  f"matching at the same rate", flush=True)
        if ok and a.save_project and (mode, mapper) == runs[0]:
            save_project(a.save_project, paths, infos, mapping, wd, exe)
        print()

    print(f"{'run':<12}{'time [s]':>10}{'pairs':>8}{'verified':>10}{'reg. imgs':>11}{'points':>9}{'reproj[px]':>12}")
    for mode, total, mp, vp, st in results:
        print(f"{mode:<12}{total:>10.2f}{str(mp):>8}{str(vp):>10}{str(st.get('registered')):>11}"
              f"{str(st.get('points')):>9}{str(st.get('reproj')):>12}")


if __name__ == "__main__":
    main()
