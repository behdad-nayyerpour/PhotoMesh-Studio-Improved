"""Exports in world coordinates (local + georeference offset): PLY (streamed,
double x/y/z), textured PLY, OBJ, GLB (offset in the root node translation),
DXF (polyface mesh / points), cameras CSV; no offset = unchanged output."""
import json
import os
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photomesh import export as ex, texture as tx, gltf, pcfilter        # noqa: E402
from photomesh.plyio import read_fast, save_ply                           # noqa: E402

OFF = np.array([26700.0, 23000.0, 1300.0])


def _grid_mesh(n=60):
    xs, ys = np.meshgrid(np.linspace(-50, 50, n), np.linspace(-40, 40, n))
    V = np.column_stack([xs.ravel(), ys.ravel(), np.sin(xs.ravel() / 7) * 3]).astype(np.float32)
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    F = np.concatenate([np.column_stack([a, b, d]), np.column_stack([a, d, c])]).astype(np.int32)
    return V, F


@pytest.fixture
def mesh(tmp_path):
    V, F = _grid_mesh()
    cols = np.random.default_rng(1).random((len(V), 4)).astype(np.float32)
    p = str(tmp_path / "model_local.ply")
    save_ply(p, V, cols, F)
    return p, V.astype(np.float64), F


def _xyz(r):
    v = r["vertex"]
    return np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)


def test_ply_mesh_world_roundtrip(mesh, tmp_path):
    p, V, F = mesh
    dst = str(tmp_path / "w.ply")
    ex.export_ply_world(p, dst, OFF, chunk=777)                   # several chunks
    r = read_fast(dst)
    assert r["vertex"].dtype["x"] == np.dtype("<f8")
    assert np.abs(_xyz(r) - (V + OFF)).max() < 1e-6
    assert np.array_equal(r["face"], F)
    src = read_fast(p)["vertex"]
    for c in ("red", "green", "blue"):
        assert np.array_equal(r["vertex"][c], src[c])
    head = open(dst, "rb").read(600).split(b"end_header")[0].decode()
    assert "property double x" in head and "binary_little_endian" in head and "vertex_indices" in head
    assert not os.path.exists(dst + ".part")
    # plyfile (what other tools see) agrees
    from plyfile import PlyData
    with open(dst, "rb") as fh:
        ply = PlyData.read(fh)
    assert np.allclose(ply["vertex"]["x"], V[:, 0] + OFF[0], atol=1e-6)


def test_cloud_world_with_normals_confidence(tmp_path):
    rng = np.random.default_rng(0)
    xyz = rng.normal(size=(5000, 3)) * 30
    nrm = rng.normal(size=(5000, 3)).astype(np.float32)
    rgb = rng.integers(0, 255, (5000, 3)).astype(np.uint8)
    conf = rng.integers(3, 20, 5000)
    p = str(tmp_path / "c.ply")
    pcfilter.write_cloud(p, xyz.astype(np.float32), rgb, nrm, conf)
    dst = str(tmp_path / "cw.ply")
    ex.export_ply_world(p, dst, OFF, chunk=1000)
    c0, c1 = pcfilter.read_cloud(p), pcfilter.read_cloud(dst)
    assert np.abs(c1["xyz"] - (c0["xyz"] + OFF)).max() < 1e-6
    assert np.array_equal(c1["rgb"], c0["rgb"]) and np.array_equal(c1["normals"], c0["normals"])
    assert np.array_equal(c1["confidence"], c0["confidence"])


def test_ascii_ply_general_path(tmp_path):
    from plyfile import PlyData, PlyElement
    a = np.array([(1.0, 2.0, 3.0), (4.5, 5.5, 6.5)], dtype=[("x", "f4"), ("y", "f4"), ("z", "f4")])
    p = str(tmp_path / "a.ply")
    PlyData([PlyElement.describe(a, "vertex")], text=True).write(p)
    dst = str(tmp_path / "aw.ply")
    ex.export_ply_world(p, dst, OFF)
    got = _xyz(read_fast(dst))
    assert np.abs(got - (np.array([[1, 2, 3], [4.5, 5.5, 6.5]]) + OFF)).max() < 1e-6


def test_no_offset_is_plain_copy(mesh, tmp_path):
    p, _V, _F = mesh
    dst = str(tmp_path / "copy.ply")
    ex.export_ply_world(p, dst, ex.georef_offset({}))
    assert open(dst, "rb").read() == open(p, "rb").read()
    assert ex.georef_offset(None) is None and ex.georef_offset({"offset": [0, 0, 0]}) is None
    assert np.array_equal(ex.georef_offset({"offset": list(OFF)}), OFF)


@pytest.fixture
def textured(mesh, tmp_path):
    """A fake texture folder (uv.npz + info.json + one page) for the mesh."""
    from PIL import Image
    p, V, F = mesh
    d = tmp_path / "tex"; d.mkdir()
    size = 256
    uv = (np.random.default_rng(2).random((len(F), 3, 2)) * size).astype(np.float32)
    np.savez(d / "uv.npz", uv=uv, page=np.zeros(len(F), np.int32), size=size)
    Image.new("RGB", (size, size), (120, 90, 60)).save(d / "texture_0.jpg")
    (d / "info.json").write_text(json.dumps({"pages": ["texture_0.jpg"]}))
    return p, V, F, str(d), uv, size


def test_obj_world_and_unchanged(textured, tmp_path):
    p, V, F, d, uv, size = textured
    o = tmp_path / "o"; o.mkdir()
    tx.export_obj(p, d, str(o / "w.obj"), offset=OFF)
    Vw, Fw, uvw, _pg = tx.read_obj(str(o / "w.obj"))
    assert np.abs(Vw - (V + OFF)).max() < 1e-6 and np.array_equal(Fw, F)
    assert np.allclose(uvw * size, uv, atol=0.02)
    tx.export_obj(p, d, str(o / "l.obj"))
    Vl, *_ = tx.read_obj(str(o / "l.obj"))
    assert np.abs(Vl - V).max() < 1e-5
    assert "world coordinates" not in open(o / "l.obj").read(300)


def test_textured_ply_world(textured, tmp_path):
    p, V, F, d, *_ = textured
    dst = str(tmp_path / "t.ply")
    tx.export_ply_textured(p, d, dst, offset=OFF)
    from plyfile import PlyData
    with open(dst, "rb") as fh:
        ply = PlyData.read(fh)
    v = ply["vertex"]
    P = np.column_stack([v["x"], v["y"], v["z"]])
    assert P.dtype == np.float64 and np.abs(P - (V + OFF)).max() < 1e-6
    assert np.array_equal(np.vstack(ply["face"]["vertex_indices"]), F)
    tx.export_ply_textured(p, d, str(tmp_path / "t0.ply"))       # no offset: float, as before
    assert b"property float x" in open(tmp_path / "t0.ply", "rb").read(500)


def _glb_json(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20:20 + n])


def test_glb_node_translation(textured, tmp_path):
    p, V, F, d, *_ = textured
    dst = str(tmp_path / "w.glb")
    tx.export_glb(p, d, dst, z_up=True, offset=OFF)
    js = _glb_json(dst)
    T = np.array(js["nodes"][0]["translation"])
    assert np.allclose(T, gltf.ZUP_TO_YUP @ OFF)                   # Y-up: (x, z, -y)
    assert js["extras"]["world_offset"] == list(OFF)
    prims = gltf.read_glb(open(dst, "rb").read(), z_up=True)
    P = np.concatenate([q["positions"] for q in prims])
    # local positions + node translation (mapped back to Z-up) = world
    world = P + gltf.ZUP_TO_YUP.T @ T
    assert np.abs(world.min(0) - (V + OFF).min(0)).max() < 1e-3
    tx.export_glb(p, d, str(tmp_path / "l.glb"), z_up=True)
    assert "translation" not in _glb_json(str(tmp_path / "l.glb"))["nodes"][0]


def test_dxf_mesh_roundtrip_and_split(mesh, tmp_path):
    p, V, F = mesh
    dst = str(tmp_path / "m.dxf")
    ex.export_dxf_mesh(p, dst, OFF)
    meshes = ex.polyface_meshes(dst)
    assert len(meshes) == 1
    Vd, Fd = meshes[0]
    assert len(Fd) == len(F)
    assert np.abs(Vd[Fd] - (V[F] + OFF)).max() < 1e-6
    txt = open(dst).read(200)
    assert "AC1009" in txt
    # > 32767 vertices per block is split into several polyfaces
    V2, F2 = _grid_mesh(260)
    dst2 = str(tmp_path / "big.dxf")
    ex.write_dxf_mesh(dst2, V2, F2, OFF)
    ms = ex.polyface_meshes(dst2)
    assert len(ms) >= 3 and all(len(v) <= ex.DXF_MAX_VERTS for v, _ in ms)
    assert sum(len(f) for _, f in ms) == len(F2)
    got = np.concatenate([v[f] for v, f in ms]); want = V2.astype(np.float64)[F2] + OFF
    assert np.abs(got - want).max() < 1e-6


def test_dxf_points(tmp_path):
    xyz = np.random.default_rng(3).normal(size=(2500, 3)).astype(np.float32) * 10
    p = str(tmp_path / "c.ply")
    pcfilter.write_cloud(p, xyz, None)
    dst = str(tmp_path / "c.dxf")
    ex.export_dxf_points(p, dst, OFF, chunk=700)
    ents = [e for e in ex.read_dxf_entities(dst) if e[0] == "POINT"]
    P = np.array([[float(g[10][0]), float(g[20][0]), float(g[30][0])] for _, g in ents])
    assert len(P) == 2500 and np.abs(P - (xyz.astype(np.float64) + OFF)).max() < 1e-6


def test_cameras_csv_world(tmp_path):
    cams = [SimpleNamespace(label="a", est=[1.5, 2.0, 3.0]), SimpleNamespace(label="b", est=None)]
    dst = str(tmp_path / "c.csv")
    ex.write_cameras_csv(dst, cams, OFF)
    lines = open(dst).read().split()
    assert lines[0] == "label,x,y,z" and len(lines) == 2
    assert np.allclose([float(x) for x in lines[1].split(",")[1:]], np.array([1.5, 2, 3]) + OFF, atol=1e-6)
