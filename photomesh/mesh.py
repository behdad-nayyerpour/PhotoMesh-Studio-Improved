"""Build Mesh from the dense cloud: screened Poisson (Open3D) with density
trimming, or COLMAP's Delaunay mesher; then cleanup and decimation.

Face count presets follow the usual photogrammetry rule of thumb
(High = points/5, Medium = points/15, Low = points/45 faces).
Large clouds are voxel-downsampled first so Poisson stays within RAM.
"""
from __future__ import annotations

import os

import numpy as np

FACE_FACTOR = {"High": 5, "Medium": 15, "Low": 45}
# Poisson density trimming quantile per interpolation mode
TRIM_QUANTILE = {"Disabled": 0.08, "Enabled": 0.02, "Extrapolated": 0.0}


def target_faces(n_points, preset="Medium", custom=None):
    if preset == "Custom" and custom:
        return int(custom)
    return max(1000, int(n_points / FACE_FACTOR.get(preset, 15)))


def poisson_depth(n_points, target=None):
    """Octree depth: enough resolution for the point count, capped for RAM.

    With a face target, go one level coarser for every 4x surplus: Poisson
    produces about 2 faces per point at the full depth and each level less
    divides that by 4, so the result stays >= ~3x the target and decimation
    (single-threaded, the slowest step) has far less to do."""
    d = int(np.clip(round(np.log(max(n_points, 1)) / np.log(4)) + 1, 8, 12))
    if target:
        surplus = 2.0 * n_points / (3.0 * max(target, 1))
        if surplus > 1:
            d -= int(np.floor(np.log(surplus) / np.log(4)))
    return int(np.clip(d, 8, 12))


def _o3d():
    import open3d as o3d
    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)
    return o3d


def load_point_cloud(path, max_points=12_000_000, voxel=None, log=print):
    o3d = _o3d()
    pcd = o3d.io.read_point_cloud(path)
    n = len(pcd.points)
    if voxel:
        pcd = pcd.voxel_down_sample(float(voxel))
        log(f"Voxel downsample ({voxel}): {n:,} -> {len(pcd.points):,} points")
    elif n > max_points:
        # choose the voxel size from the bounding box so ~max_points remain
        ext = pcd.get_max_bound() - pcd.get_min_bound()
        area = np.sort(ext)[-2:].prod()          # surveys are ~2.5D
        v = float(np.sqrt(area / max_points))
        pcd = pcd.voxel_down_sample(v)
        log(f"Voxel downsample to fit memory (voxel {v:.4g}): {n:,} -> {len(pcd.points):,} points")
    return pcd, n


def ensure_normals(pcd, cam_center=None):
    o3d = _o3d()
    if not pcd.has_normals():
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamKNN(16))
        if cam_center is not None:
            pcd.orient_normals_towards_camera_location(np.asarray(cam_center, float))
    return pcd


def poisson(pcd, depth=None, trim_quantile=0.02, log=print, target=None):
    o3d = _o3d()
    depth = depth or poisson_depth(len(pcd.points), target)
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=depth, scale=1.1, linear_fit=False, n_threads=-1)
    dens = np.asarray(dens)
    nv = len(mesh.vertices)
    if trim_quantile > 0:
        mesh.remove_vertices_by_mask(dens < np.quantile(dens, trim_quantile))
    log(f"Poisson depth {depth}: {nv:,} vertices, {len(mesh.triangles):,} faces after density trim")
    return mesh


def crop_to_cloud(mesh, pcd, max_dist):
    """Remove faces whose centroid is farther than max_dist from the cloud
    (Poisson surfaces bridging gaps / extrapolated borders)."""
    from scipy.spatial import cKDTree
    tree = cKDTree(np.asarray(pcd.points))
    V = np.asarray(mesh.vertices); F = np.asarray(mesh.triangles)
    cen = V[F].mean(1)
    d, _ = tree.query(cen, k=1, workers=-1)
    mesh.remove_triangles_by_mask(d > max_dist)
    mesh.remove_unreferenced_vertices()
    return int((d > max_dist).sum())


def remove_long_edges(mesh, factor=8.0):
    V = np.asarray(mesh.vertices); F = np.asarray(mesh.triangles)
    if not len(F):
        return 0
    e = np.stack([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1) for i in range(3)], 1).max(1)
    bad = e > factor * np.median(e)
    mesh.remove_triangles_by_mask(bad); mesh.remove_unreferenced_vertices()
    return int(bad.sum())


def remove_small_components(mesh, min_fraction=0.01, min_faces=200):
    tri, counts, _ = mesh.cluster_connected_triangles()
    tri = np.asarray(tri); counts = np.asarray(counts)
    if not len(counts):
        return 0
    thr = max(min_faces, min_fraction * counts.max())
    bad = counts[tri] < thr
    mesh.remove_triangles_by_mask(bad); mesh.remove_unreferenced_vertices()
    return int(bad.sum())


def decimate(mesh, target):
    if len(mesh.triangles) > target:
        mesh = mesh.simplify_quadric_decimation(int(target))
        mesh.remove_unreferenced_vertices()
    return mesh


def clean(mesh, log=print, long_edge_factor=8.0, min_component=0.01):
    mesh.remove_duplicated_vertices(); mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles(); mesh.remove_non_manifold_edges()
    a = remove_long_edges(mesh, long_edge_factor) if long_edge_factor else 0
    b = remove_small_components(mesh, min_component) if min_component else 0
    log(f"Cleanup: removed {a:,} long-edge faces, {b:,} faces in small components")
    return mesh


def commit_mesh(tmp, out):
    """Replace out with the freshly written mesh tmp, but only when tmp is a
    complete PLY with faces; otherwise tmp is removed and out is left as it was."""
    from .plyio import read_header
    why = ""
    try:
        with open(tmp, "rb") as fh:
            h = read_header(fh)
        if h is None:
            raise ValueError("not a PLY file")
        head, end, elems = h
        counts = {name: n for name, n, _ in elems}
        nv, nf = counts.get("vertex", 0), counts.get("face", 0)
        body = os.path.getsize(tmp) - len(head)
        if end:                               # binary: the data must be at least this long
            need = 0
            for _name, n, props in elems:
                for pr in props:
                    if pr is None:
                        continue
                    need += n * np.dtype(pr[2] if pr[0] == "list" else pr[1]).itemsize
            if body < need:
                why = f"truncated ({body:,} of at least {need:,} data bytes)"; body = 0
    except Exception as e:  # noqa: BLE001
        nv = nf = body = 0; why = str(e)
    if not (nv and nf and body):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"The new mesh has no faces or is incomplete{': ' + why if why else ''} - the previous model was kept")
    for k in range(20):
        try:
            os.replace(tmp, out); return nv, nf
        except PermissionError:            # Windows: model briefly open by AV/indexer/viewer
            if k == 19:
                raise
            import time; time.sleep(0.25)


def build_mesh(cfg, log=print):
    """cfg: source ('Point cloud'|'Depth maps'), cloud, out, face_count, custom_faces,
    interpolation, voxel, max_points, delaunay_ply (for Depth maps), cam_center."""
    o3d = _o3d()
    import time
    t0 = time.perf_counter()
    if cfg["source"] == "Point cloud":
        pcd, n_in = load_point_cloud(cfg["cloud"], cfg.get("max_points", 12_000_000),
                                     cfg.get("voxel"), log)
        ensure_normals(pcd, cfg.get("cam_center"))
        tgt0 = target_faces(n_in, cfg.get("face_count", "Medium"), cfg.get("custom_faces"))
        mesh = poisson(pcd, cfg.get("depth"), TRIM_QUANTILE.get(cfg.get("interpolation", "Enabled"), 0.02), log,
                       target=tgt0)
        if cfg.get("interpolation", "Enabled") != "Extrapolated":
            sp = np.asarray(pcd.compute_nearest_neighbor_distance())
            n = crop_to_cloud(mesh, pcd, 8 * float(np.median(sp)))
            log(f"Removed {n:,} faces farther than 8x point spacing from the cloud (gaps)")
    else:
        mesh = o3d.io.read_triangle_mesh(cfg["delaunay_ply"])
        n_in = cfg.get("n_points", len(mesh.vertices) * 2)
    mesh = clean(mesh, log, cfg.get("long_edge_factor", 8.0), cfg.get("min_component", 0.01))
    tgt = target_faces(n_in, cfg.get("face_count", "Medium"), cfg.get("custom_faces"))
    nf = len(mesh.triangles)
    mesh = decimate(mesh, tgt)
    log(f"Decimation: {nf:,} -> {len(mesh.triangles):,} faces (target {tgt:,})")
    mesh = clean(mesh, log, 0, cfg.get("min_component", 0.01))
    mesh.compute_vertex_normals()
    if not len(mesh.triangles):
        raise RuntimeError("Meshing produced no faces - the previous model was kept")
    tmp = cfg.get("tmp") or cfg["out"]
    if not o3d.io.write_triangle_mesh(tmp, mesh, write_ascii=False, compressed=False,
                                      write_vertex_normals=True, write_vertex_colors=mesh.has_vertex_colors()):
        raise RuntimeError(f"Could not write {tmp}")
    if tmp != cfg["out"]:
        commit_mesh(tmp, cfg["out"])
    log(f"Mesh: {len(mesh.vertices):,} vertices, {len(mesh.triangles):,} faces "
        f"({time.perf_counter() - t0:.1f} s) -> {cfg['out']}")
    return {"faces": len(mesh.triangles), "vertices": len(mesh.vertices)}
