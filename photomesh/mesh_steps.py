"""Pipeline steps for Build Model (mesh)."""
import json
import os

from .dense import PYTHON


def mesh_steps(exe, wd, s, cloud_path, cam_center=None):
    """s: source ('Point cloud' | 'Depth maps'), face_count, custom_faces,
    interpolation, voxel, max_points."""
    dense = os.path.join(wd, "dense")
    out = os.path.join(wd, "model.ply")
    source = s.get("source", "Point cloud")
    # prerequisites first: nothing is touched when the step cannot run
    if source == "Depth maps" and not os.path.exists(os.path.join(dense, "fused.ply.vis")):
        raise RuntimeError("Build the point cloud first: Delaunay meshing uses the fused depth maps")
    # the new mesh is written to model.building.ply and replaces model.ply only when it
    # is complete and has faces (mesh.commit_mesh): a failed or cancelled build keeps
    # the previous model
    tmp = os.path.join(wd, "model.building.ply")
    if os.path.exists(tmp):
        os.remove(tmp)                     # leftover of an interrupted build (ours, never a product)
    cfg = {"source": source, "cloud": cloud_path, "out": out, "tmp": tmp,
           "face_count": s.get("face_count", "Medium"), "custom_faces": s.get("custom_faces"),
           "interpolation": s.get("interpolation", "Enabled"), "voxel": s.get("voxel"),
           "max_points": s.get("max_points", 12_000_000), "cam_center": cam_center,
           "long_edge_factor": s.get("long_edge_factor", 8.0),
           "min_component": s.get("min_component", 0.01)}
    steps = []
    if cfg["source"] == "Depth maps":
        raw = os.path.join(dense, "delaunay_raw.ply")
        cfg["delaunay_ply"] = raw
        steps.append(("Generating mesh (Delaunay)", ["delaunay_mesher", "--input_path", dense,
                      "--input_type", "dense", "--output_path", raw,
                      "--DelaunayMeshing.num_threads", "-1"], 70))
    path = os.path.join(wd, "mesh.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    steps.append(("Generating mesh (Poisson)" if cfg["source"] == "Point cloud"
                  else "Cleaning mesh", [PYTHON, "build_mesh", path], 100))
    return steps, out
