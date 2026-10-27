"""Dense cloud, point filtering, meshing and GCP georeferencing (offline, no COLMAP)."""
import json
import os

import numpy as np
import pytest

from photomesh import colmap_io as cio


def synthetic_model(n_img=8, n_pts=400, seed=0, two_cameras=True):
    """Nadir 'drone' block over a bumpy ground: images on a line at height 50."""
    rng = np.random.default_rng(seed)
    cams = {1: cio.Camera(2, 600, 450, np.array([500.0, 300, 225, -0.02]))}
    if two_cameras:
        cams[2] = cio.Camera(2, 400, 300, np.array([900.0, 200, 150, 0.01]))
    X = np.column_stack([rng.uniform(-30, 30 + 8 * n_img, n_pts), rng.uniform(-20, 20, n_pts),
                         rng.normal(0, 1.0, n_pts)])
    R = np.diag([1.0, -1.0, -1.0])           # looking down (-z)
    images, obs = {}, {i: [] for i in range(n_pts)}
    for k in range(n_img):
        C = np.array([8.0 * k, 0, 50]); t = -R @ C
        cid = 2 if (two_cameras and k % 3 == 2) else 1
        uv, z = cio.project(cams[cid], R, t, X)
        vis = np.flatnonzero((uv[:, 0] > 0) & (uv[:, 0] < cams[cid].width) &
                             (uv[:, 1] > 0) & (uv[:, 1] < cams[cid].height) & (z > 0))
        images[k + 1] = cio.Image(f"img{k:02d}.jpg", cid, cio.rot_to_qvec(R), t,
                                  uv[vis] + rng.normal(0, 0.3, (len(vis), 2)), vis.astype(np.int64))
        for j, p in enumerate(vis):
            obs[p].append((k + 1, j))
    keep = [p for p in range(n_pts) if len(obs[p]) >= 2]
    remap = {p: i for i, p in enumerate(keep)}
    for im in images.values():
        im.point3D_ids = np.array([remap.get(int(p), -1) for p in im.point3D_ids], np.int64)
    tl = [len(obs[p]) for p in keep]
    P = cio.Points(np.arange(len(keep), dtype=np.uint64), X[keep], np.full((len(keep), 3), 128, np.uint8),
                   np.full(len(keep), 0.5), np.r_[0, np.cumsum(tl)].astype(np.int64),
                   np.array([o[0] for p in keep for o in obs[p]], np.uint32),
                   np.array([o[1] for p in keep for o in obs[p]], np.uint32))
    return cio.Model(cams, images, P)


def test_colmap_io_roundtrip(tmp_path):
    m = synthetic_model()
    cio.write_model(str(tmp_path), m)
    r = cio.read_model(str(tmp_path))
    assert set(r.cameras) == {1, 2} and r.cameras[2].width == 400
    assert len(r.images) == 8 and r.images[3].name == "img02.jpg"
    np.testing.assert_allclose(r.points.xyz, m.points.xyz)
    np.testing.assert_array_equal(r.points.track_img, m.points.track_img)
    np.testing.assert_array_equal(r.points.track_idx, m.points.track_idx)
    np.testing.assert_allclose(r.images[2].qvec, m.images[2].qvec)
    errs, _, _ = cio.observation_errors(r)
    assert np.median(errs) < 1.0


def test_rotation_quaternion_roundtrip():
    from scipy.spatial.transform import Rotation
    for R in Rotation.random(20, random_state=1).as_matrix():
        np.testing.assert_allclose(cio.qvec_to_rot(cio.rot_to_qvec(R)), R, atol=1e-9)


def test_select_sources_prefers_overlap_and_angle():
    from photomesh.dense import select_sources
    m = synthetic_model(n_img=10, n_pts=2000)
    src = select_sources(m, max_sources=3, min_shared=5, min_angle=1, ideal_angle=8)
    assert all(1 <= len(v) <= 3 for v in src.values())
    assert 4 in src[5] or 6 in src[5]            # direct neighbours are chosen
    assert 5 not in src[5]


def test_undistort_and_workspace(tmp_path):
    from PIL import Image
    from photomesh import dense
    m = synthetic_model()
    model_dir = tmp_path / "sparse"; cio.write_model(str(model_dir), m)
    photos = tmp_path / "photos"; photos.mkdir()
    rng = np.random.default_rng(0)
    for im in m.images.values():
        c = m.cameras[im.camera_id]
        Image.fromarray(rng.integers(0, 255, (c.height, c.width, 3), dtype=np.uint8)).save(photos / im.name)
    with pytest.raises(RuntimeError):
        dense.prepare_workspace(str(model_dir), str(photos), str(photos / "dense"), "Low", workers=1)
    r = dense.prepare_workspace(str(model_dir), str(photos), str(tmp_path / "dense"), "High",
                                max_sources=4, workers=2)
    assert r["undistorted"] == 8 and r["factor"] == 2
    dm = cio.read_model(str(tmp_path / "dense" / "sparse"))
    assert {c.name for c in dm.cameras.values()} == {"PINHOLE"}
    assert (dm.cameras[1].width, dm.cameras[2].width) == (300, 200)   # each camera / 2
    with Image.open(tmp_path / "dense" / "images" / "img02.jpg") as im:
        assert im.size == (200, 150)
    cfg = open(tmp_path / "dense" / "stereo" / "patch-match.cfg").read().splitlines()
    assert cfg[0] == "img00.jpg" and len(cfg) == 16 and "img01.jpg" in cfg[1]
    assert sorted(os.listdir(photos)) == sorted(im.name for im in m.images.values())  # nothing written there


def test_is_inside_handles_other_drives(monkeypatch):
    from photomesh import dense
    assert dense.is_inside("/a/b/c", "/a/b") and not dense.is_inside("/a/bc", "/a/b")
    def boom(_):
        raise ValueError("Paths don't have the same drive")
    monkeypatch.setattr(dense.os.path, "commonpath", boom)
    assert dense.is_inside("C:/x", "F:/y") is False


def test_remap_identity_without_distortion():
    from photomesh.dense import remap_table
    cam = cio.Camera(1, 40, 30, np.array([50.0, 50, 20, 15]))
    rows, cols = remap_table(cam, 1)
    np.testing.assert_allclose(cols[0, :5], np.arange(5) + 0.5, atol=1e-5)
    np.testing.assert_allclose(rows[:5, 0], np.arange(5) + 0.5, atol=1e-5)


def test_read_vis_counts(tmp_path):
    from photomesh.dense import read_vis_counts
    counts = [2, 5, 3]
    buf = np.array([3, 0], "<u4").tobytes()
    for c in counts:
        buf += np.array([c] + list(range(c)), "<u4").tobytes()
    p = tmp_path / "fused.ply.vis"; p.write_bytes(buf)
    assert read_vis_counts(str(p)).tolist() == counts


def test_depth_filter_modes_get_stricter():
    from photomesh.dense import DEPTH_FILTERS
    order = ["Disabled", "Mild", "Moderate", "Aggressive"]
    px = [DEPTH_FILTERS[k]["min_num_pixels"] for k in order]
    err = [DEPTH_FILTERS[k]["max_reproj_error"] for k in order]
    assert px == sorted(px) and err == sorted(err, reverse=True)


def test_dense_steps_structure(tmp_path):
    from photomesh.dense import dense_steps, PYTHON
    wd = tmp_path / "wd"; (wd / "sparse" / "0").mkdir(parents=True)
    steps = dense_steps("colmap", str(wd), {"quality": "Medium", "depth_filtering": "Aggressive"},
                        str(tmp_path / "photos"))
    labels = [l for l, _, _ in steps]
    assert labels[0].startswith("Undistorting") and steps[0][1][0] == PYTHON
    fu = [a for l, a, _ in steps if l == "gate" and hasattr(a, "fusion_base")][0].fusion_base
    assert fu[fu.index("--StereoFusion.min_num_pixels") + 1] == "8"
    assert json.load(open(wd / "dense" / "prepare.json"))["quality"] == "Medium"


# =================================================================== GCPs
GCP_TXT = """# CoordinateSystem: LOCAL_CS["Local Coordinates (m)",LOCAL_DATUM["Local Datum",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]]]
#Label\tX\tY\tZ\tError_(m)\tX_error\tY_error\tZ_error\tX_est\tY_est\tZ_est
1\t26542.098000\t23371.444000\t1321.471000\t0.057668\t0.037962\t0.043304\t-0.003059\t26542.135962\t23371.487304\t1321.467941
14\t26748.740000\t23181.836000\t1284.169000\t\t\t\t\t\t\t
AP-3\t27094.184476\t22643.449610\t1298.699585\t0.008195\t0.006059\t-0.003393\t-0.004352\t27094.190535\t22643.446217\t1298.695233
#Total error\t\t\t\t0.118140\t0.106691\t0.039134\t0.032287\t\t\t
"""


def test_import_metashape_reference_export(tmp_path):
    from photomesh import georef as g
    p = tmp_path / "GCP.txt"; p.write_text(GCP_TXT.replace("\n", "\r\n"), encoding="utf-8")
    ms, crs = g.import_markers(str(p))
    assert crs.kind == "local" and crs.name == "Local Coordinates (m)"
    assert [m.label for m in ms] == ["1", "14", "AP-3"]
    assert ms[0].ref == [26542.098, 23371.444, 1321.471] and ms[0].acc is None   # error cols ignored


def test_import_plain_and_csv(tmp_path):
    from photomesh import georef as g
    p = tmp_path / "a.txt"; p.write_text("1\t26542.098\t23371.444\t1321.471\t\n2\t1\t2\t3\t\n")
    ms, crs = g.import_markers(str(p))
    assert len(ms) == 2 and crs.kind == "local" and ms[1].ref == [1, 2, 3]
    p = tmp_path / "b.csv"
    p.write_text("name,longitude,latitude,altitude,accuracy\nG1,69.76,38.67,1500,0.02\nG2,69.761,38.671,1501,0.03\n")
    ms, crs = g.import_markers(str(p))
    assert crs.kind == "wgs84" and ms[0].ref == [69.76, 38.67, 1500] and ms[1].acc == [0.03, 0.03]


def test_import_survey_and_metashape_xml(tmp_path):
    from photomesh import georef as g
    p = tmp_path / "s.xml"
    p.write_text("<?xml version='1.0'?><surveydata coordinatesystem='XYZ' epsgcode=''><markers>"
                 "<marker id='0' name='GCP01'><images /><gcp x='1' y='2' z='3' checkpoint='false' /></marker>"
                 "<marker id='1' name='GCP02'><images /><gcp x='4' y='5' z='6' checkpoint='true' /></marker>"
                 "</markers></surveydata>")
    ms, crs = g.import_markers(str(p))
    assert crs.kind == "local" and [m.enabled for m in ms] == [True, False]
    p = tmp_path / "m.xml"
    p.write_text("""<document><chunk><cameras><camera id="5" label="DJI_0001_D"/></cameras>
      <markers><marker id="0" label="1"><reference x="10" y="20" z="30" enabled="false"/></marker></markers>
      <reference>LOCAL_CS["Local Coordinates (m)"]</reference>
      <frames><frame><markers><marker marker_id="0"><location camera_id="5" pinned="true" x="100.5" y="200.25"/></marker></markers></frame></frames>
      </chunk></document>""")
    ms, crs = g.import_markers(str(p))
    assert ms[0].projections == {"DJI_0001_D": [100.5, 200.25, True]} and ms[0].enabled is False


def test_crs_parse_and_utm_against_reference():
    from photomesh import georef as g
    assert g.CRS.parse("EPSG:32642").zone == 42 and g.CRS.parse("EPSG::32642").kind == "utm"
    assert g.CRS.parse('GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]]').kind == "wgs84"
    assert g.CRS.parse("WGS 84 / UTM zone 33S").south
    lat, lon = np.array([38.677838943, 38.67, -33.9]), np.array([69.763040286, 69.0, 18.4])
    zones = [42, 42, 34]
    for la, lo, z in zip(lat, lon, zones):
        E, N = g.utm_forward(la, lo, z, la < 0)
        la2, lo2 = g.utm_inverse(E, N, z, la < 0)
        assert abs(la2 - la) < 1e-9 and abs(lo2 - lo) < 1e-9
    E, N = g.utm_forward(38.677838943, 69.763040286, 42)
    try:
        from pyproj import Transformer
        t = Transformer.from_crs(4326, 32642, always_xy=True)
        e2, n2 = t.transform(69.763040286, 38.677838943)
        assert abs(E - e2) < 1e-3 and abs(N - n2) < 1e-3
    except ImportError:
        assert abs(E - 566326) < 200


def test_helmert_recovers_similarity():
    from photomesh import georef as g
    from scipy.spatial.transform import Rotation
    rng = np.random.default_rng(3)
    X = rng.normal(size=(20, 3)) * 50
    R = Rotation.from_euler("zyx", [30, 5, -10], degrees=True).as_matrix()
    Y = 2.5 * X @ R.T + [1000, 2000, 300]
    Y[4] += 40                                      # one bad point
    s, Rh, t, inl = g.robust_helmert(X, Y)
    assert abs(s - 2.5) < 1e-6 and np.allclose(Rh, R, atol=1e-8) and not inl[4] and inl.sum() == 19


def _markers_for(model, world, rng, n=10, noise=0.3):
    """Synthetic GCPs on the ground with projections from the true model."""
    from photomesh import georef as g
    pts = model.points.xyz[rng.choice(len(model.points), n, replace=False)]
    ms = []
    for k, X in enumerate(pts):
        proj = {}
        for iid, im in model.images.items():
            p = g.predict_projection(model, iid, X)
            if p:
                proj[im.name] = [p[0] + rng.normal(0, noise), p[1] + rng.normal(0, noise), True]
        ms.append(g.Marker(f"G{k}", list(world(X)), projections=proj, enabled=(k % 3 != 0)))
    return ms


def test_gcp_georeference_end_to_end(tmp_path):
    """Model in an arbitrary frame (scaled, rotated) with a perturbed focal
    length: Helmert + GCP bundle adjustment must recover control/check points."""
    from photomesh import georef as g
    from scipy.spatial.transform import Rotation
    rng = np.random.default_rng(5)
    true = synthetic_model(n_img=10, n_pts=1500, two_cameras=True)
    R = Rotation.from_euler("z", 40, degrees=True).as_matrix()
    world = lambda X: 0.5 * np.asarray(X) @ R.T + np.array([26500.0, 23000.0, 1300.0])
    markers = _markers_for(true, world, rng)
    # arbitrary model frame: X_model = 3 * X_true, plus camera / focal perturbation
    work = synthetic_model(n_img=10, n_pts=1500, two_cameras=True)
    g.transform_model(work, 3.0, np.eye(3), np.zeros(3))
    for im in work.images.values():
        C = im.center() + rng.normal(0, 0.3, 3); im.tvec = -im.R() @ C
    work.cameras[1].params[0] *= 1.01
    g.retriangulate(work)
    common = {"markers": [m.to_dict() for m in markers], "crs": 'LOCAL_CS["Local Coordinates (m)"]',
              "tie_points": 2000, "accuracies": {"marker_accuracy": 0.01, "marker_accuracy_z": 0.01}}
    d0 = tmp_path / "helmert"; cio.write_model(str(d0), work)
    helm = g.run_task({"model": str(d0), "bundle": False, **common}, log=lambda *_: None)
    d = tmp_path / "sparse0"; cio.write_model(str(d), work)
    out = g.run_task({"model": str(d), "bundle": True, "report": str(tmp_path / "rep.txt"),
                      "result": str(tmp_path / "res.json"), **common}, log=lambda *_: None)
    # BA with GCPs fixes the perturbed cameras/focal: much better than Helmert alone
    assert out["check_rmse"]["total"] < 0.5 * helm["check_rmse"]["total"]
    assert out["control_rmse"]["total"] < 0.5 * helm["control_rmse"]["total"]
    assert out["check_rmse"]["total"] < 0.06       # ground sampling ~0.05 m/px, 0.3 px noise
    assert os.path.isdir(str(d) + "_original")
    rep = open(tmp_path / "rep.txt").read()
    assert "#Control points RMSE" in rep and "#Check points RMSE" in rep and "\tcheck\t" in rep
    geo = cio.read_model(str(d))
    errs, _, _ = cio.observation_errors(geo)
    assert np.sqrt(np.mean(errs ** 2)) < 1.0


def test_markers_xml_round_trip(tmp_path):
    from photomesh import georef as g
    ms = [g.Marker("1", [100.0, 200.0, 30.0], [0.01, 0.02], True, {"IMG_1": [10.5, 20.25, True], "IMG_2": [5.0, 6.0, False]}),
          g.Marker("2", [1.0, 2.0, 3.0], None, False, {})]
    p = tmp_path / "m.xml"
    g.write_markers_xml(str(p), ms, 'LOCAL_CS["Local Coordinates (m)"]')
    back, crs = g.read_markers_xml(str(p))
    assert crs.kind == "local"
    b = {m.label: m for m in back}
    assert b["1"].ref == [100.0, 200.0, 30.0] and b["1"].acc == [0.01, 0.02] and b["1"].enabled
    assert b["1"].projections == {"IMG_1": [10.5, 20.25, True], "IMG_2": [5.0, 6.0, False]}
    assert not b["2"].enabled


def test_remove_points_unlinks_observations():
    import numpy as np
    from photomesh import colmap_io as cio
    model = synthetic_model()[0] if isinstance(synthetic_model(), tuple) else synthetic_model()
    n0 = len(model.points)
    rm = np.zeros(n0, bool); rm[:3] = True
    gone = set(model.points.ids[:3].tolist())
    assert cio.remove_points(model, rm) == 3
    assert len(model.points) == n0 - 3
    for im in model.images.values():
        assert not (set(np.asarray(im.point3D_ids).tolist()) & gone)


def test_local_outliers_keep_sparse_regions():
    """A dense patch next to a 4x sparser patch: the global radius filter
    deletes the sparse (valid) patch, the local one only the stray points."""
    from photomesh import pcfilter as pf
    rng = np.random.default_rng(1)
    g1 = np.stack(np.meshgrid(np.arange(0, 1, 0.01), np.arange(0, 1, 0.01)), -1).reshape(-1, 2)
    g2 = np.stack(np.meshgrid(np.arange(1.2, 2.2, 0.04), np.arange(0, 1, 0.04)), -1).reshape(-1, 2)
    xy = np.vstack([g1, g2]); xyz = np.c_[xy, rng.normal(0, 0.001, len(xy))]
    stray = np.c_[rng.uniform(0, 2.2, (20, 2)), rng.uniform(0.3, 1.0, 20)]
    pts = np.vstack([xyz, stray])
    ks, kr = pf.local_outliers(pts)
    keep = ks & kr
    assert keep[len(g1):len(xyz)].mean() > 0.97          # sparse patch kept
    assert keep[len(xyz):].mean() < 0.2                  # strays removed
    kg, _ = pf.radius_outliers(pts)
    assert kg[len(g1):len(xyz)].mean() < 0.5             # global radius would delete it


def _georef_setup(tmp_path):
    from photomesh import georef as g
    from photomesh import pcfilter as pf
    from scipy.spatial.transform import Rotation
    rng = np.random.default_rng(7)
    true = synthetic_model(n_img=10, n_pts=1500, two_cameras=True)
    R = Rotation.from_euler("z", 25, degrees=True).as_matrix()
    world = lambda X: 0.5 * np.asarray(X) @ R.T + np.array([26500.0, 23000.0, 1300.0])
    markers = _markers_for(true, world, rng)
    d = tmp_path / "sparse0"; cio.write_model(str(d), true)
    cloud = tmp_path / "cloud.ply"
    pf.write_cloud(str(cloud), true.points.xyz, true.points.rgb, None, None)
    cfg = {"model": str(d), "markers": [m.to_dict() for m in markers], "bundle": False,
           "crs": 'LOCAL_CS["Local Coordinates (m)"]', "transform_files": [str(cloud)],
           "result": str(tmp_path / "res.json")}
    return g, pf, true, world, d, cloud, cfg


def test_georef_transforms_cloud_in_place_without_mmap(tmp_path):
    """Regression (Windows 'OSError: [Errno 22] Invalid argument' in ply.write):
    transform_ply read the PLY memory-mapped and then truncated the same file.
    Now it reads into memory, writes a temp file and replaces the original."""
    g, pf, true, world, d, cloud, cfg = _georef_setup(tmp_path)
    out = g.run_task(cfg, log=lambda *_: None)
    c = pf.read_cloud(str(cloud))
    want = world(true.points.xyz) - np.asarray(out["offset"])
    assert np.abs(c["xyz"] - want).max() < 0.15          # Helmert from noisy markers (0.3 px)
    assert np.abs(true.points.xyz - want).max() > 10      # i.e. it really was transformed
    assert not [p for p in os.listdir(tmp_path) if "georef_tmp" in p]
    assert not os.path.exists(str(d) + "_georef_tmp")


def test_georef_failure_leaves_model_and_cloud_untouched(tmp_path):
    """If any output can't be written, nothing is committed: model and PLYs
    stay in the old (consistent) frame and no temp files are left behind
    (the old code wrote the georeferenced model first, then crashed on the cloud)."""
    g, pf, true, world, d, cloud, cfg = _georef_setup(tmp_path)
    bad = tmp_path / "truncated.ply"
    data = open(cloud, "rb").read()
    open(bad, "wb").write(data[: len(data) // 2])          # e.g. an interrupted earlier write
    cfg["transform_files"] = [str(cloud), str(bad)]
    before = {p: open(p, "rb").read() for p in [str(cloud), str(bad)] + [str(d / f) for f in
              ("cameras.bin", "images.bin", "points3D.bin")]}
    with pytest.raises(ValueError, match="truncated"):
        g.run_task(cfg, log=lambda *_: None)
    for p, data in before.items():
        assert open(p, "rb").read() == data, p
    assert not [p for p in os.listdir(tmp_path) if "georef_tmp" in p]
    assert not os.path.exists(str(d) + "_georef_tmp")


def test_ply_readers_do_not_memory_map(tmp_path):
    """Viewers/filters must not keep PLYs mapped (Windows can't replace them)."""
    from photomesh import pcfilter as pf
    from photomesh.plyio import load_ply
    p = tmp_path / "c.ply"
    pf.write_cloud(str(p), np.random.rand(50, 3), (np.random.rand(50, 3) * 255).astype(np.uint8), None, None)
    d = load_ply(str(p)); c = pf.read_cloud(str(p))
    os.replace(str(p), str(tmp_path / "moved.ply"))        # would fail on Windows if still mapped
    assert len(d["points"]) == 50 and len(c["xyz"]) == 50


@pytest.mark.parametrize("fmt", ["binary_little_endian", "binary_big_endian", "ascii"])
def test_transform_ply_streams_mesh_with_normals(tmp_path, fmt):
    """Chunked streaming transform: vertices + normals transformed, faces and
    colours copied unchanged, identical to a reference computed in memory."""
    from plyfile import PlyData, PlyElement
    from photomesh import georef as g
    from scipy.spatial.transform import Rotation
    rng = np.random.default_rng(3)
    n = 1003
    v = np.zeros(n, dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
                           ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    for k in ("x", "y", "z"):
        v[k] = rng.normal(0, 10, n)
    nn = rng.normal(size=(n, 3)); nn /= np.linalg.norm(nn, axis=1, keepdims=True)
    v["nx"], v["ny"], v["nz"] = nn.T
    v["red"] = rng.integers(0, 255, n)
    f = np.empty(500, dtype=[("vertex_indices", "i4", (3,))])
    f["vertex_indices"] = rng.integers(0, n, (500, 3))
    p = tmp_path / "m.ply"
    PlyData([PlyElement.describe(v, "vertex"), PlyElement.describe(f, "face")],
            text=(fmt == "ascii"), byte_order="<" if fmt != "binary_big_endian" else ">").write(str(p))
    R = Rotation.from_euler("xyz", [10, 20, 30], degrees=True).as_matrix()
    M = np.eye(4); M[:3, :3] = 2.5 * R; M[:3, 3] = [100, -50, 7]
    g.transform_ply(str(p), M, chunk=97)
    assert os.listdir(tmp_path) == ["m.ply"]
    with open(p, "rb") as fh:
        out = PlyData.read(fh, mmap=False)
    o = out["vertex"].data
    X = np.column_stack([v["x"], v["y"], v["z"]]).astype(float) @ M[:3, :3].T + M[:3, 3]
    assert np.allclose(np.column_stack([o["x"], o["y"], o["z"]]), X, atol=1e-3)
    assert np.allclose(np.column_stack([o["nx"], o["ny"], o["nz"]]), nn @ R.T, atol=1e-5)
    assert (o["red"] == v["red"]).all()
    assert (np.stack(out["face"].data["vertex_indices"]) == f["vertex_indices"]).all()


@pytest.mark.parametrize("fmt", ["binary_little_endian", "binary_big_endian", "ascii"])
@pytest.mark.parametrize("nv", [3, 4])
def test_load_ply_fast_reader_matches_plyfile(tmp_path, fmt, nv):
    from plyfile import PlyData, PlyElement
    from photomesh.plyio import load_ply
    from photomesh import pcfilter as pf
    rng = np.random.default_rng(1)
    v = np.zeros(200, dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    for k in v.dtype.names:
        v[k] = rng.uniform(0, 200, 200)
    f = np.empty(50, dtype=[("vertex_indices", "i4", (nv,))]); f["vertex_indices"] = rng.integers(0, 200, (50, nv))
    p = tmp_path / "m.ply"
    PlyData([PlyElement.describe(v, "vertex"), PlyElement.describe(f, "face")], text=(fmt == "ascii"),
            byte_order="<" if fmt != "binary_big_endian" else ">").write(str(p))
    d = load_ply(str(p))
    assert np.allclose(d["points"], np.column_stack([v["x"], v["y"], v["z"]]))
    assert np.allclose(d["colors"][:, 0] * 255, v["red"], atol=0.01)
    assert (d["faces"] == f["vertex_indices"][:, :3]).all()
    assert np.allclose(pf.read_cloud(str(p))["xyz"], np.column_stack([v["x"], v["y"], v["z"]]))


def test_crlf_header_ply_uses_fast_path(tmp_path):
    """COLMAP's fused.ply on Windows has a CRLF header: must still stream fast
    (and keep the header byte-for-byte)."""
    from photomesh import georef as g
    from photomesh.plyio import read_fast
    v = np.zeros(10, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    v["x"] = np.arange(10)
    head = b"ply\r\nformat binary_little_endian 1.0\r\nelement vertex 10\r\nproperty float x\r\nproperty float y\r\n" \
           b"property float z\r\nproperty uchar red\r\nproperty uchar green\r\nproperty uchar blue\r\nend_header\r\n"
    p = tmp_path / "fused.ply"
    open(p, "wb").write(head + v.tobytes())
    assert read_fast(str(p)) is not None
    M = np.eye(4); M[:3, 3] = [1, 2, 3]
    g.transform_ply(str(p), M)
    data = open(p, "rb").read()
    assert data.startswith(head) and len(data) == len(head) + v.nbytes
    assert np.allclose(read_fast(str(p))["vertex"]["x"], np.arange(10) + 1)
