"""Build Texture / Build Tiled Model: synthetic scenes with known albedo
(tests/texsynth.py: 16 ray-cast photos from 2 sensors), the GUI actions, the
exports and the 3D Tiles output. Sceaux variant runs when PM_TESTDATA has a
processed project (PC)."""
import json
import os
import shutil
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texsynth as ts                                          # noqa: E402
from photomesh import texture as tx, tiles, gltf               # noqa: E402

QUIET = lambda m: None                                         # noqa: E731


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("texscene"))
    return ts.make_scene(d, ghost=True)


@pytest.fixture(scope="module")
def generic(scene, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("tex") / "generic")
    info = tx.build_texture(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"], out_dir=out,
                                 size=1024, max_res=0), log=QUIET)
    return info


def test_generic_mosaic_matches_albedo(scene, generic):
    err, *_ = ts.texture_error(generic, scene["V"], scene["F"])
    assert err < 6, err
    assert generic["faces"] == len(scene["F"]) and generic["pages"] == ["texture_0.jpg"]
    assert generic["photos_used"] >= 10
    for f in ("model_textured.obj", "model_textured.mtl", "texture_0.jpg", "uv.npz", "info.json",
              "model_textured.glb", "visibility_cache.npz"):
        assert os.path.exists(os.path.join(generic["dir"], f)), f
    assert set(generic["stages"]) >= {"visibility", "write files"}


@pytest.mark.parametrize("mapping,blending,count", [("Adaptive orthophoto", "Mosaic", 1),
                                                    ("Orthophoto", "Average", 2),
                                                    ("Generic", "Max intensity", 1),
                                                    ("Generic", "Min intensity", 1),
                                                    ("Generic", "Disabled", 1)])
def test_modes(scene, tmp_path, mapping, blending, count):
    info = tx.build_texture(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                 out_dir=str(tmp_path / "t"), size=1024, count=count, max_res=0,
                                 mapping=mapping, blending=blending), log=QUIET)
    err, *_ = ts.texture_error(info, scene["V"], scene["F"])
    assert err < (25 if blending in ("Max intensity", "Min intensity") else 8), (mapping, blending, err)
    # count is an upper bound: pages are not filled with texels upsampled beyond the photo resolution
    assert 1 <= info["count"] <= count and len(info["pages"]) == info["count"]


def test_ghosting_filter_removes_moving_object(scene, tmp_path):
    devs = {}
    for gh in (False, True):
        info = tx.build_texture(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                     out_dir=str(tmp_path / f"g{gh}"), size=1024, max_res=0, blending="Average",
                                     ghosting=gh), log=QUIET)
        _e, col, ref, P = ts.texture_error(info, scene["V"], scene["F"], n=40000)
        g = np.hypot(P[:, 0] - 10, P[:, 1] - 4) < 1.0
        devs[gh] = float(np.abs(col[g] - ref[g]).mean())
    assert devs[True] < devs[False] * 0.5, devs


def test_keep_uv_and_obj_roundtrip(scene, generic, tmp_path):
    info = tx.build_texture(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                 out_dir=str(tmp_path / "keep"), size=1024, max_res=0, mapping="Keep uv",
                                 uv_npz=generic["uv_npz"]), log=QUIET)
    a, b = np.load(generic["uv_npz"]), np.load(info["uv_npz"])
    assert np.array_equal(a["uv"], b["uv"]) and np.array_equal(a["page"], b["page"])
    V, F, uv, page = tx.read_obj(generic["obj"])
    assert len(F) == len(scene["F"]) and np.allclose(V, scene["V"], atol=1e-5)
    assert np.allclose(uv * generic["size"], a["uv"], atol=0.02)


def test_exports(scene, generic, tmp_path):
    d = tmp_path / "exp"; d.mkdir()
    tx.export_obj(scene["mesh"], generic["dir"], str(d / "m.obj"))
    assert (d / "m.mtl").exists() and (d / "texture_0.jpg").exists()
    tx.export_glb(scene["mesh"], generic["dir"], str(d / "m.glb"))
    prims = gltf.read_glb((d / "m.glb").read_bytes(), z_up=True)
    assert sum(len(p["indices"]) for p in prims) == len(scene["F"]) and prims[0]["image"][:2] == b"\xff\xd8"
    tx.export_ply_textured(scene["mesh"], generic["dir"], str(d / "m.ply"))
    raw = (d / "m.ply").read_bytes()
    head = raw[:raw.index(b"end_header")].decode()
    assert "comment TextureFile texture_0.jpg" in head and "texcoord" in head


def test_visibility_cache_reused(scene, generic, tmp_path):
    out = str(tmp_path / "again"); os.makedirs(out)
    shutil.copy(os.path.join(generic["dir"], "visibility_cache.npz"), out)
    logs = []
    info = tx.build_texture(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"], out_dir=out,
                                 size=1024, max_res=0), log=logs.append)
    assert any("cache" in m.lower() for m in logs), logs
    assert info["stages"]["visibility"] < generic["stages"]["visibility"]


@pytest.fixture(scope="module")
def tileset(scene, generic, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("tiles") / "tiled")
    return tiles.build_tiles(dict(out_dir=out, mesh=scene["mesh"], texture_dir=generic["dir"], tile_faces=3000,
                                  up=[0, 0, 1]), log=QUIET)


def test_tiles_structure(scene, tileset):
    ts_json = json.load(open(tileset["tileset"], encoding="utf-8"))
    assert ts_json["asset"]["version"] == "1.0"
    idx = json.load(open(tileset["index"], encoding="utf-8"))
    nodes = {n["key"]: n for n in idx["nodes"]}
    assert tileset["levels"] >= 2 and tileset["tiles"] == len(nodes)
    leaves = [n for n in nodes.values() if not n["children"]]
    # leaves keep every original face
    nf = 0
    for n in leaves:
        prims = gltf.read_glb(open(os.path.join(tileset["dir"], "tiles", n["file"]), "rb").read(), z_up=True)
        nf += sum(len(p["indices"]) for p in prims)
    assert nf == len(scene["F"])
    for n in nodes.values():           # parents have larger geometric error than their children
        for k in n["children"]:
            assert n["error"] > nodes[k]["error"]
        assert os.path.getsize(os.path.join(tileset["dir"], "tiles", n["file"])) > 0
    b = open(os.path.join(tileset["dir"], "tiles", leaves[0]["file"]), "rb").read()
    assert b[:4] == b"b3dm" and struct.unpack("<I", b[8:12])[0] == len(b)
    assert tileset["pixel_size"] > 0


def test_tile_texture_matches_albedo(tileset):
    from io import BytesIO
    from PIL import Image
    idx = json.load(open(tileset["index"], encoding="utf-8"))
    leaf = [n for n in idx["nodes"] if not n["children"]][0]
    p = gltf.read_glb(open(os.path.join(tileset["dir"], "tiles", leaf["file"]), "rb").read(), z_up=True)[0]
    img = np.asarray(Image.open(BytesIO(p["image"]))).astype(float)
    P = p["positions"] @ np.array(idx["rotation_model_to_tiles"])
    fi = p["indices"]; rng = np.random.default_rng(0)
    f = rng.integers(0, len(fi), 3000); w = rng.dirichlet([3, 3, 3], 3000)
    X = (P[fi[f]] * w[:, :, None]).sum(1); q = (p["uv"][fi[f]] * w[:, :, None]).sum(1)
    col = img[(q[:, 1] * img.shape[0]).astype(int).clip(0, img.shape[0] - 1),
              (q[:, 0] * img.shape[1]).astype(int).clip(0, img.shape[1] - 1)]
    ref = ts.albedo(X); fx, fy = X[:, 0] % 1, X[:, 1] % 1
    inner = (np.minimum(fx, 1 - fx) > 0.15) & (np.minimum(fy, 1 - fy) > 0.15)
    assert np.median(np.abs(col - ref)[inner]) < 12


def test_sse_selection(tileset):
    """Far away only the root is drawn; close to the ground leaves are reached."""
    from photomesh.glitems import TiledModelItem
    it = TiledModelItem.__new__(TiledModelItem)
    idx = json.load(open(tileset["index"], encoding="utf-8"))
    it.nodes = {n["key"]: n for n in idx["nodes"]}
    for n in idx["nodes"]:
        lo, hi = np.array(n["bmin"]), np.array(n["bmax"]); n["c"] = (lo + hi) / 2
        n["r"] = float(np.linalg.norm(hi - lo) / 2)
    it.root = idx["nodes"][0]["key"]; it.sse = 16.0; it.pending = set(); it.stats = {"requests": 0}
    it.loaded = {k: [] for k in it.nodes}
    it.request = lambda k: None
    far = it.select(np.array([10, 10, 5000.0]), None, 800, 30)
    assert far == [it.root]
    near = it.select(np.array([10, 10, 2.0]), None, 800, 30)
    depth = max(it.nodes[k]["depth"] for k in near)
    assert depth == max(n["depth"] for n in it.nodes.values())


def test_point_cloud_tiles_and_zip(scene, tmp_path):
    from photomesh.glitems import read_pnts
    r = tiles.build_tiles(dict(out_dir=str(tmp_path / "pt"), source="Point cloud", cloud=scene["mesh"],
                               tile_points=1500, up=[0, 0, 1]), log=QUIET)
    idx = json.load(open(r["index"], encoding="utf-8"))
    leaves = [n for n in idx["nodes"] if not n["children"]]
    tot = sum(len(read_pnts(open(os.path.join(r["dir"], "tiles", n["file"]), "rb").read())[0]) for n in leaves)
    assert tot == len(scene["V"])
    z = tiles.zip_tiles(r["dir"], str(tmp_path / "pt.zip"))
    import zipfile
    names = zipfile.ZipFile(z).namelist()
    assert "tileset.json" in names and "photomesh_tiles.json" in names


def test_not_georeferenced_up_rotation(scene, generic, tmp_path):
    """A tilted model (not georeferenced, arbitrary up) is levelled for the
    quadtree and the tile frame -> model frame rotation is recorded."""
    from photomesh.plyio import save_ply
    th = np.radians(35); R = np.array([[1, 0, 0], [0, np.cos(th), -np.sin(th)], [0, np.sin(th), np.cos(th)]])
    V = scene["V"] @ R.T
    save_ply(str(tmp_path / "tilt.ply"), V.astype(np.float32), None, scene["F"])
    r = tiles.build_tiles(dict(out_dir=str(tmp_path / "tt"), mesh=str(tmp_path / "tilt.ply"), tile_faces=3000,
                               up=(R @ [0, 0, 1.0]).tolist()), log=QUIET)
    idx = json.load(open(r["index"], encoding="utf-8"))
    Rup = np.array(idx["rotation_model_to_tiles"])
    Vt = V @ Rup.T
    assert np.ptp(Vt[:, 2]) < 0.3 * np.ptp(Vt[:, 0])          # levelled
    assert not idx["georeferenced"]


# ------------------------------------------------------------------ GUI
def _setup_chunk(window, scene, tmp_path, georef=False):
    from photomesh.plyio import ply_counts
    ch = window.chunk
    ch.add_photos(sorted(scene["photos"].values()))
    wd = str(tmp_path / "work"); ch.workdir = wd
    shutil.copytree(scene["model"], os.path.join(wd, "sparse", "0"))
    shutil.copy(scene["mesh"], os.path.join(wd, "model.ply"))
    ch.model_ply = os.path.join(wd, "model.ply"); ch.face_count = ply_counts(ch.model_ply)[1]
    if georef:
        ch.georef = {"crs_kind": "local", "crs": "Local Coordinates (m)", "offset": [0, 0, 0]}
    window.refresh_all()
    return ch


def _wait(window):
    from PySide6.QtCore import QEventLoop, QTimer
    if not getattr(window.runner, "busy", False):
        return
    loop = QEventLoop()
    window.runner.finished.connect(lambda *a: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(600_000, loop.quit)
    loop.exec()


@pytest.mark.parametrize("georef", [True, False])
def test_gui_build_texture_and_tiles(window, no_dialogs, monkeypatch, scene, tmp_path, georef):
    window.project.path = str(tmp_path / "proj.pmsx")
    from photomesh import dialogs
    from photomesh import procmeta as pm
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    monkeypatch.setattr(dialogs.BuildTextureDialog, "exec", lambda self: 1)
    s0 = dialogs.BuildTextureDialog.settings
    monkeypatch.setattr(dialogs.BuildTextureDialog, "settings", lambda self: dict(s0(self), size=1024))
    monkeypatch.setattr(dialogs.BuildTiledModelDialog, "exec", lambda self: 1)
    t0 = dialogs.BuildTiledModelDialog.settings
    monkeypatch.setattr(dialogs.BuildTiledModelDialog, "settings", lambda self: dict(t0(self), tile_faces=3000))
    ch = _setup_chunk(window, scene, tmp_path, georef)
    window.actions["Build Texture..."].trigger(); _wait(window)
    assert ch.texture.get("size") == 1024, window.chunk.log
    assert os.path.exists(ch.texture["obj"])
    assert "Texture 1024x1024" in pm.summary(ch, "model")
    rows = dict(pm.properties(ch, "model"))
    assert rows["Mapping mode"] == "Generic" and ("cm/pix" in rows["Texel size"]) == georef
    window.actions["Build Tiled Model..."].trigger(); _wait(window)
    t = ch.tiled_model
    assert t.get("levels", 0) >= 2, window.chunk.log
    txt = pm.summary(ch, "tiled")
    assert ("cm/pix" in txt) == georef and ("not georeferenced" in txt) == (not georef)
    labels = []
    def walk(it):
        for i in range(it.childCount()):
            labels.append(it.child(i).text(0)); walk(it.child(i))
    walk(window.workspace.invisibleRootItem())
    assert any(l.startswith("Tiled Model (") for l in labels), labels
    assert any("Texture 1024x1024" in l for l in labels), labels
    # view modes do not raise without a GL context
    window.actions["Model - Textured"].trigger(); window.actions["Tiled Model - Solid"].trigger()
    # exports
    p = window.export_model_to(str(tmp_path / "exp" / "m"), "obj") if os.makedirs(tmp_path / "exp") is None else None
    assert os.path.exists(p) and os.path.exists(str(tmp_path / "exp" / "texture_0.jpg"))
    assert os.path.exists(window.export_model_to(str(tmp_path / "exp" / "m"), "glb"))
    assert os.path.exists(window.export_model_to(str(tmp_path / "exp" / "m_tex"), "ply_tex"))
    z = window.export_tiled_to(str(tmp_path / "exp" / "tiles"), "zip")
    assert os.path.getsize(z) > 1000
    f = window.export_tiled_to(str(tmp_path / "exp" / "tiles_dir"), "folder")
    assert os.path.exists(os.path.join(f, "tileset.json"))
    # world-coordinate exports from the GUI (non-zero offset added back to the local frame)
    from photomesh import export as ex
    from photomesh.plyio import read_fast
    V0, F0 = tx.read_mesh(ch.model_ply)
    off = np.array([26700.0, 23000.0, 1300.0]) if georef else np.zeros(3)
    saved = ch.georef
    ch.georef = dict(ch.georef or {}, offset=off.tolist()) if georef else {}
    try:
        w = window.export_model_to(str(tmp_path / "exp" / "world"), "ply")
        v = read_fast(w)["vertex"]
        assert np.abs(np.column_stack([v["x"], v["y"], v["z"]]) - (V0 + off)).max() < 1e-6
        Vo, *_ = tx.read_obj(window.export_model_to(str(tmp_path / "exp" / "world"), "obj"))
        assert np.abs(Vo - (V0 + off)).max() < 1e-5
        (Vd, Fd), = ex.polyface_meshes(window.export_model_to(str(tmp_path / "exp" / "world"), "dxf"))
        assert len(Fd) == len(F0) and np.abs(Vd[Fd] - (V0[F0] + off)).max() < 1e-6
        js_t = window.export_model_to(str(tmp_path / "exp" / "world"), "glb")
        assert js_t.endswith(".glb")
        cpath = window.export_cloud_to(str(tmp_path / "exp" / "cloud"), "dxf", src=ch.model_ply)
        assert any(e[0] == "POINT" for e in ex.read_dxf_entities(cpath, limit=5))
    finally:
        ch.georef = saved
    # saved / reloaded project keeps the results (relative paths rebased)
    window.project.path = str(tmp_path / "p.pmsx"); window.project.save(window.project.path)
    from photomesh.project import Project
    p2 = Project.load(window.project.path)
    assert p2.chunks[0].texture["size"] == 1024 and p2.chunks[0].tiled_model["levels"] == t["levels"]
    # rebuilding the model invalidates the texture
    window.load_result("model", ch.model_ply)
    assert ch.texture   # same mesh -> kept
    other = str(tmp_path / "other.ply"); shutil.copy(ch.model_ply, other)
    window.load_result("model", other)
    assert ch.texture == {}
    assert no_dialogs == []


def test_gui_import_texture_and_tiles(window, no_dialogs, scene, generic, tileset, tmp_path):
    """File > Import Texture / Import Tiled Model attach existing output folders
    (copied elsewhere, as when the outputs came from a run outside the project)."""
    from photomesh import procmeta as pm
    ch = _setup_chunk(window, scene, tmp_path, georef=True)
    tex = str(tmp_path / "moved_tex"); shutil.copytree(generic["dir"], tex)
    assert window.import_texture(tex, quiet=True)
    assert ch.texture["dir"] == os.path.abspath(tex) and ch.texture["uv_npz"].startswith(os.path.abspath(tex))
    assert "Texture 1024x1024" in pm.summary(ch, "model")
    assert ch.meta["texture"]["source"] == "imported"
    tdir = str(tmp_path / "moved_tiles"); shutil.copytree(os.path.dirname(tileset["index"]), tdir)
    assert window.import_tiled_model(tdir, quiet=True)
    assert ch.tiled_model["levels"] == tileset["levels"] and ch.tiled_model["index"].startswith(os.path.abspath(tdir))
    assert ch.tiled_model["bytes"] > 0
    # timing is picked up from a sibling *_result.json written by the build
    json.dump({"dir": tdir, "seconds": 12.5}, open(str(tmp_path / "tiled_result.json"), "w", encoding="utf-8"))
    assert window.import_tiled_model(tdir, quiet=True)
    assert dict(pm.properties(ch, "tiled"))["Processing time"] != "unknown"
    # survives save / load
    window.project.path = str(tmp_path / "imp.pmsx"); window.project.save(window.project.path)
    from photomesh.project import Project
    c2 = Project.load(window.project.path).chunks[0]
    assert c2.texture["size"] == 1024 and c2.tiled_model["tiles"] == tileset["tiles"]
    # a texture of another mesh / a wrong folder is refused and nothing changes
    old = dict(ch.texture)
    bad = str(tmp_path / "bad_tex"); shutil.copytree(generic["dir"], bad)
    info = json.load(open(os.path.join(bad, "info.json"), encoding="utf-8")); info["faces"] += 1
    json.dump(info, open(os.path.join(bad, "info.json"), "w", encoding="utf-8"))
    assert not window.import_texture(bad, quiet=True) and ch.texture == old
    assert not window.import_texture(str(tmp_path), quiet=True)
    assert not window.import_tiled_model(str(tmp_path), quiet=True)
    assert no_dialogs == []


SCEAUX = os.environ.get("PM_TESTDATA_PROJECT", "")


@pytest.mark.dataset
@pytest.mark.skipif(not (SCEAUX and os.path.exists(SCEAUX)), reason="set PM_TESTDATA_PROJECT to a processed .pmsx")
def test_real_project_texture(tmp_path):
    """Texture a real processed project (read-only: outputs go to tmp_path)."""
    from photomesh.project import Project
    prj = Project.load(SCEAUX)
    ch = prj.chunks[prj.active]
    wd = prj.default_workdir(ch)
    photos = {os.path.basename(c.path): c.path for c in ch.cameras}
    info = tx.build_texture(dict(mesh=ch.model_ply, model=os.path.join(wd, "sparse", "0"), photos=photos,
                                 out_dir=str(tmp_path / "tex"), size=2048, georef=bool(ch.georef)), log=QUIET)
    assert info["photos_used"] >= 5 and info["faces_untextured"] < 0.2 * info["faces"]
    r = tiles.build_tiles(dict(out_dir=str(tmp_path / "tiles"), mesh=ch.model_ply, texture_dir=info["dir"],
                               tile_faces=20000, up=[0, 0, 1] if ch.georef else None), log=QUIET)
    assert r["levels"] >= 1
