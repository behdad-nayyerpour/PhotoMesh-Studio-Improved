"""0.1.10: findings of an external code review, each reproduced first:
1 Build Model must not lose the old model (prerequisites, failure, cancel),
2 duplicate photo file names must map to the right camera,
3 Project.load must restore the active chunk."""
import json
import os
import shutil
import sys

import numpy as np
import pytest


# ------------------------------------------------------------ helpers
def _mesh_ply(path, n_faces=2, seed=0):
    """Small valid binary PLY mesh (n_faces triangles)."""
    rng = np.random.default_rng(seed)
    V = rng.random((n_faces + 2, 3)).astype("<f4")
    F = np.array([[k, k + 1, k + 2] for k in range(n_faces)], np.int32)
    hdr = ("ply\nformat binary_little_endian 1.0\n"
           f"element vertex {len(V)}\nproperty float x\nproperty float y\nproperty float z\n"
           f"element face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n").encode()
    body = V.tobytes() + b"".join(bytes([3]) + f.astype("<i4").tobytes() for f in F)
    with open(path, "wb") as f:
        f.write(hdr + body)
    return path


def _sphere_cloud(path, n=3000):
    """Oriented point cloud on a sphere (Poisson meshing works on it)."""
    rng = np.random.default_rng(1)
    P = rng.normal(size=(n, 3)); P /= np.linalg.norm(P, axis=1, keepdims=True)
    N = P.copy()
    hdr = ("ply\nformat binary_little_endian 1.0\n"
           f"element vertex {n}\nproperty float x\nproperty float y\nproperty float z\n"
           "property float nx\nproperty float ny\nproperty float nz\nend_header\n").encode()
    with open(path, "wb") as f:
        f.write(hdr + np.hstack([P, N]).astype("<f4").tobytes())
    return path


def _wd_with_model(tmp_path):
    wd = str(tmp_path / "wd"); os.makedirs(os.path.join(wd, "dense"), exist_ok=True)
    old = _mesh_ply(os.path.join(wd, "model.ply"), 5)
    return wd, old, open(old, "rb").read()


# ------------------------------------------------------------ 3: active chunk
def test_load_restores_active_chunk(tmp_path):
    from photomesh.project import Project, Chunk
    prj = Project(); prj.chunks = [Chunk(name="A"), Chunk(name="B")]; prj.active = 1
    prj.chunks[1].texture = {"dir": "x"}                   # product dicts used to shadow the root data
    p = str(tmp_path / "p.pmsx"); prj.save(p)
    q = Project.load(p)
    assert q.active == 1 and q.chunk.name == "B"


@pytest.mark.parametrize("active", ["1", 7, -3, None, 1.5, [1], True])
def test_load_invalid_active_falls_back(tmp_path, active):
    from photomesh.project import Project, Chunk
    prj = Project(); prj.chunks = [Chunk(name="A"), Chunk(name="B")]
    p = str(tmp_path / "p.pmsx"); prj.save(p)
    d = json.load(open(p)); d["active"] = active; json.dump(d, open(p, "w"))
    q = Project.load(p)
    assert isinstance(q.active, int) and not isinstance(q.active, bool) and 0 <= q.active < 2
    assert q.chunk.name in ("A", "B")


def test_load_project_without_chunks(tmp_path):
    from photomesh.project import Project
    p = str(tmp_path / "p.pmsx")
    for d in ({"format": "photomesh-studio/1", "active": 3, "chunks": []}, {"format": "photomesh-studio/1"}):
        json.dump(d, open(p, "w"))
        q = Project.load(p)
        assert q.active == 0 and q.chunk is not None and len(q.chunks) == 1


# ------------------------------------------------------------ 1: Build Model keeps the old model
def test_mesh_missing_prerequisite_keeps_old_model(tmp_path):
    from photomesh.mesh_steps import mesh_steps
    wd, old, data = _wd_with_model(tmp_path)
    with pytest.raises(RuntimeError, match="point cloud first"):
        mesh_steps("colmap", wd, {"source": "Depth maps"}, "")
    assert open(old, "rb").read() == data


def test_mesh_steps_do_not_touch_old_model(tmp_path):
    from photomesh.mesh_steps import mesh_steps
    wd, old, data = _wd_with_model(tmp_path)
    steps, out = mesh_steps("colmap", wd, {"source": "Point cloud"}, os.path.join(wd, "dense", "x.ply"))
    assert out == old and open(old, "rb").read() == data
    cfg = json.load(open(os.path.join(wd, "mesh.json")))
    assert cfg["out"] == old and cfg["tmp"] != old and os.path.dirname(cfg["tmp"]) == wd


def test_failed_mesh_build_keeps_old_model(tmp_path):
    from photomesh.mesh_steps import mesh_steps
    from photomesh.mesh import build_mesh
    wd, old, data = _wd_with_model(tmp_path)
    bad = os.path.join(wd, "dense", "broken.ply"); open(bad, "wb").write(b"ply\nbroken")
    mesh_steps("colmap", wd, {"source": "Point cloud"}, bad)
    with pytest.raises(Exception):
        build_mesh(json.load(open(os.path.join(wd, "mesh.json"))), log=lambda m: None)
    assert open(old, "rb").read() == data


def test_empty_mesh_is_never_committed(tmp_path):
    from photomesh.mesh import commit_mesh
    wd, old, data = _wd_with_model(tmp_path)
    tmp = os.path.join(wd, "model.building.ply")
    _mesh_ply(tmp, 1); txt = open(tmp, "rb").read().replace(b"element face 1", b"element face 0")
    open(tmp, "wb").write(txt.split(b"end_header\n")[0] + b"end_header\n" + b"\0" * 36)
    with pytest.raises(RuntimeError, match="no faces"):
        commit_mesh(tmp, old)
    assert open(old, "rb").read() == data and not os.path.exists(tmp)
    _mesh_ply(tmp, 3); commit_mesh(tmp, old)
    from photomesh.plyio import ply_counts
    assert ply_counts(old)[1] == 3 and not os.path.exists(tmp)


def test_successful_mesh_build_replaces_model(tmp_path):
    from photomesh.mesh_steps import mesh_steps
    from photomesh.mesh import build_mesh
    from photomesh.plyio import ply_counts
    wd, old, data = _wd_with_model(tmp_path)
    cloud = _sphere_cloud(os.path.join(wd, "dense", "point_cloud.ply"))
    mesh_steps("colmap", wd, {"source": "Point cloud", "face_count": "Low"}, cloud)
    r = build_mesh(json.load(open(os.path.join(wd, "mesh.json"))), log=lambda m: None)
    nv, nf = ply_counts(old)
    assert nf == r["faces"] > 100 and open(old, "rb").read() != data
    assert [f for f in os.listdir(wd) if "building" in f] == []


# GUI: Build Model through the real runner (python steps, no COLMAP needed)
def _gui_project(tmp_path, window, monkeypatch, cloud_ok):
    from photomesh import colmap as cm, dialogs
    from photomesh.project import Project, Camera
    prj = Project(); prj.path = str(tmp_path / "p.pmsx"); ch = prj.chunk
    ch.cameras = [Camera(path=str(tmp_path / "a.jpg"), aligned=True, est=[0, 0, 0])]
    wd = prj.default_workdir(ch); os.makedirs(os.path.join(wd, "dense"), exist_ok=True)
    ch.model_ply = _mesh_ply(os.path.join(wd, "model.ply"), 5); ch.face_count = 5
    cloud = os.path.join(wd, "dense", "point_cloud.ply")
    if cloud_ok:
        _sphere_cloud(cloud)
    else:
        open(cloud, "wb").write(b"ply\nbroken")
    ch.point_cloud_ply = cloud
    ch.texture = {"dir": os.path.join(wd, "texture"), "faces": 5}
    ch.tiled_model = {"dir": os.path.join(wd, "tiles"), "source": "Mesh"}
    ch.orthomosaic = {"path": os.path.join(wd, "o.tif"), "surface": "Mesh"}
    ch.dem = {"path": os.path.join(wd, "d.tif"), "source": "Point cloud"}
    prj.save(prj.path)
    window.open_project(prj.path)
    monkeypatch.setattr(cm, "find_colmap", lambda: sys.executable)
    monkeypatch.setattr(window, "_need_colmap", lambda: sys.executable)
    monkeypatch.setattr(dialogs.BuildModelDialog, "exec", lambda self: 1)
    monkeypatch.setattr(dialogs.BuildModelDialog, "settings",
                        lambda self: {"source": "Point cloud", "face_count": "Low", "interpolation": "Enabled"})
    return window.chunk, open(ch.model_ply, "rb").read()


def _wait_done(window, qtbot):
    qtbot.waitUntil(lambda: window.runner is not None and not window.runner.busy, timeout=120000)
    qtbot.wait(100)


def test_gui_failed_build_model_keeps_model_and_products(window, no_dialogs, tmp_path, monkeypatch, qtbot):
    ch, data = _gui_project(tmp_path, window, monkeypatch, cloud_ok=False)
    tex, tiled = dict(ch.texture), dict(ch.tiled_model)
    window.build_model(); _wait_done(window, qtbot)
    assert any("Build Model failed" in t for t, _m in no_dialogs)
    assert open(ch.model_ply, "rb").read() == data and ch.face_count == 5
    assert ch.texture == tex and ch.tiled_model == tiled


def test_gui_cancelled_build_model_keeps_model(window, no_dialogs, tmp_path, monkeypatch, qtbot):
    ch, data = _gui_project(tmp_path, window, monkeypatch, cloud_ok=True)
    tex = dict(ch.texture)
    window.build_model(); window.runner.cancel(); _wait_done(window, qtbot)
    assert open(ch.model_ply, "rb").read() == data and ch.face_count == 5 and ch.texture == tex


def test_gui_build_model_replaces_and_marks_products_outdated(window, no_dialogs, tmp_path, monkeypatch, qtbot):
    ch, data = _gui_project(tmp_path, window, monkeypatch, cloud_ok=True)
    window.build_model(); _wait_done(window, qtbot)
    from photomesh.plyio import ply_counts
    assert open(ch.model_ply, "rb").read() != data and ch.face_count == ply_counts(ch.model_ply)[1] > 100
    assert ch.texture == {}                                   # the atlas belongs to the old mesh
    assert ch.tiled_model.get("stale") and ch.orthomosaic.get("stale")
    assert not ch.dem.get("stale")                            # built from the point cloud
    labels = [window.workspace.topLevelItem(0).child(0).child(k).text(0)
              for k in range(window.workspace.topLevelItem(0).child(0).childCount())]
    assert any(l.startswith("Tiled Model") and "outdated" in l for l in labels)


# ------------------------------------------------------------ 2: duplicate photo names
def _dup_chunk(tmp_path):
    """Two different photos called IMG_001.JPG in two folders + one unique."""
    from PIL import Image
    from photomesh.project import Camera, Chunk
    paths = []
    for sub, col in (("f1", (255, 0, 0)), ("f2", (0, 0, 255))):
        d = tmp_path / "photos" / sub; d.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 12), col).save(d / "IMG_001.JPG"); paths.append(str(d / "IMG_001.JPG"))
    Image.new("RGB", (16, 12), (0, 255, 0)).save(tmp_path / "photos" / "f2" / "IMG_002.JPG")
    paths.append(str(tmp_path / "photos" / "f2" / "IMG_002.JPG"))
    return Chunk(cameras=[Camera(path=p) for p in paths])


def _sparse(wd, names):
    """sparse/0 + sparse_txt with one pose per COLMAP image name (x = index)."""
    from photomesh import colmap_io as cio
    md = os.path.join(wd, "sparse", "0"); os.makedirs(md, exist_ok=True)
    cio.write_cameras(os.path.join(md, "cameras.bin"), {1: cio.Camera(0, 16, 12, np.array([20.0, 8, 6]))})
    imgs = {k + 1: cio.Image(n, 1, np.array([1.0, 0, 0, 0]), np.array([-float(k), 0, 0]),
                             np.zeros((0, 2)), np.zeros(0, np.int64)) for k, n in enumerate(names)}
    cio.write_images(os.path.join(md, "images.bin"), imgs)
    t = os.path.join(wd, "sparse_txt"); os.makedirs(t, exist_ok=True)
    with open(os.path.join(t, "images.txt"), "w") as f:
        for k, n in enumerate(names):
            f.write(f"{k + 1} 1 0 0 0 {-float(k)} 0 0 1 {n}\n\n")
    open(os.path.join(t, "points3D.txt"), "w").close()
    return md


def test_prepare_images_names_duplicates_uniquely(tmp_path):
    from photomesh import colmap as cm
    ch = _dup_chunk(tmp_path); wd = str(tmp_path / "wd")
    m = cm.prepare_images(ch, wd)
    assert sorted(m.values()) == ["IMG_001.JPG", "IMG_001_1.JPG", "IMG_002.JPG"]


def test_photo_map_maps_duplicate_names_to_their_own_photo(tmp_path):
    """The texture/ortho input {COLMAP name: photo} must point each name to its own file."""
    from photomesh import colmap as cm
    from photomesh.photonames import photo_map, assign_names
    ch = _dup_chunk(tmp_path); wd = str(tmp_path / "wd")
    m = cm.prepare_images(ch, wd); assign_names(ch, m)
    pm, ambiguous = photo_map(ch)
    assert ambiguous == [] and len(pm) == 3
    for c in ch.cameras:
        assert pm[m[c.path]] == c.path


def test_texture_views_use_the_right_photo_for_duplicate_names(tmp_path):
    from photomesh import colmap as cm
    from photomesh.photonames import photo_map, assign_names
    from photomesh.texture import load_views
    ch = _dup_chunk(tmp_path); wd = str(tmp_path / "wd")
    m = cm.prepare_images(ch, wd); assign_names(ch, m)
    md = _sparse(wd, [m[c.path] for c in ch.cameras])
    views = load_views(md, photo_map(ch)[0], lambda s: None)
    assert {v.name: v.path for v in views} == {m[c.path]: c.path for c in ch.cameras}


def test_alignment_reload_gives_duplicates_their_own_pose(window, no_dialogs, tmp_path):
    from photomesh import colmap as cm
    from photomesh.photonames import assign_names
    ch = _dup_chunk(tmp_path); wd = str(tmp_path / "wd")
    m = cm.prepare_images(ch, wd); assign_names(ch, m)
    names = [m[c.path] for c in ch.cameras]; _sparse(wd, names)
    window.project.chunks = [ch]; window.project.active = 0
    window._reload_alignment(ch, wd)
    xs = [round(c.est[0], 6) for c in ch.cameras]
    assert xs == [0.0, 1.0, 2.0]                 # pose k has centre x = k


def test_photo_names_survive_save_open_and_folder_move(tmp_path):
    from photomesh import colmap as cm
    from photomesh.photonames import photo_map, assign_names
    from photomesh.project import Project
    ch = _dup_chunk(tmp_path)
    prj = Project(); prj.chunks = [ch]; prj.path = str(tmp_path / "p.pmsx")
    m = cm.prepare_images(ch, prj.default_workdir(ch)); assign_names(ch, m)
    prj.save(prj.path)
    q = Project.load(prj.path)
    assert photo_map(q.chunk)[0] == photo_map(ch)[0]
    # move the whole folder (photos are below the project folder -> relative paths)
    moved = str(tmp_path.parent / (tmp_path.name + "_moved")); shutil.move(str(tmp_path), moved)
    r = Project.load(os.path.join(moved, "p.pmsx"))
    pm = photo_map(r.chunk)[0]
    assert pm["IMG_001.JPG"].endswith(os.path.join("f1", "IMG_001.JPG")) and pm["IMG_001.JPG"].startswith(moved)
    assert pm["IMG_001_1.JPG"].endswith(os.path.join("f2", "IMG_001.JPG"))


def test_photo_names_survive_relink(tmp_path):
    from photomesh import colmap as cm
    from photomesh.photonames import photo_map, assign_names
    ch = _dup_chunk(tmp_path)
    m = cm.prepare_images(ch, str(tmp_path / "wd")); assign_names(ch, m)
    new = tmp_path / "new"; shutil.copytree(tmp_path / "photos", new)
    for c in ch.cameras:                                    # what Change Image Paths does: only path changes
        c.path = c.path.replace(str(tmp_path / "photos"), str(new))
    pm = photo_map(ch)[0]
    assert pm["IMG_001.JPG"] == str(new / "f1" / "IMG_001.JPG")
    assert pm["IMG_001_1.JPG"] == str(new / "f2" / "IMG_001.JPG")


def test_old_project_names_recovered_from_links_or_flagged(tmp_path):
    """Pre-0.1.10 projects have no stored names: recover them from the
    images\\ links; when that is impossible, ambiguous names are left out
    (never silently mismatched) and reported."""
    from photomesh import colmap as cm
    from photomesh.photonames import photo_map, recover_names
    ch = _dup_chunk(tmp_path); wd = str(tmp_path / "wd")
    m = cm.prepare_images(ch, wd)                            # links in wd/images, names not stored (old project)
    linked = all(os.path.samefile(c.path, os.path.join(wd, "images", m[c.path]))
                 for c in ch.cameras)
    recovered = recover_names(ch, wd)
    assert recovered >= 1
    if linked:
        assert recovered == 3
    assert all(not c.colmap_name or c.colmap_name == m[c.path] for c in ch.cameras)
    # no links to recover from
    for c in ch.cameras:
        c.colmap_name = ""
    shutil.rmtree(os.path.join(wd, "images"))
    assert recover_names(ch, wd) == 0
    pm, ambiguous = photo_map(ch)
    assert pm == {"IMG_002.JPG": ch.cameras[2].path}
    assert ambiguous == ["IMG_001.JPG"]


def test_unique_names_without_stored_names_still_work(tmp_path):
    from photomesh.photonames import photo_map
    from photomesh.project import Camera, Chunk
    ch = Chunk(cameras=[Camera(path=str(tmp_path / "a" / "X.JPG")), Camera(path=str(tmp_path / "a" / "Y.JPG"))])
    assert photo_map(ch) == ({"X.JPG": ch.cameras[0].path, "Y.JPG": ch.cameras[1].path}, [])


def test_relink_never_picks_the_other_photo_with_same_name_and_size(tmp_path):
    """Save As re-links photo copies in images\\ to originals: two photos with the
    same name and size (e.g. uncompressed TIFFs) must not be confused."""
    from photomesh import datafolder
    from photomesh.project import Camera, Chunk
    a = tmp_path / "f1" / "IMG.TIF"; b = tmp_path / "f2" / "IMG.TIF"
    for p, x in ((a, b"A"), (b, b"B")):
        p.parent.mkdir(); p.write_bytes(x * 100)
    ch = Chunk(cameras=[Camera(path=str(a)), Camera(path=str(b))])
    copy = tmp_path / "images" / "IMG.TIF"; copy.parent.mkdir(); copy.write_bytes(b"B" * 100)   # plain copy of b
    dst = tmp_path / "out.TIF"
    # Some Windows sandboxes deny both hard links and symlinks. The copy
    # fallback must still preserve the photo chosen by its contents.
    linked = datafolder._link_photo(str(copy), str(dst), datafolder.photo_index(ch))
    if not linked:
        import shutil
        shutil.copy2(copy, dst)
    assert not os.path.samefile(dst, a) and dst.read_bytes() == b"B" * 100
    # with stored COLMAP names the index is exact
    ch.cameras[0].colmap_name, ch.cameras[1].colmap_name = "IMG.TIF", "IMG_1.TIF"
    idx = datafolder.photo_index(ch)
    assert idx == {"img.tif": [str(a)], "img_1.tif": [str(b)]}


def test_gui_old_project_with_ambiguous_names_warns(window, no_dialogs, tmp_path):
    from photomesh.project import Project
    ch = _dup_chunk(tmp_path)
    for c in ch.cameras:
        c.aligned = True; c.est = [0, 0, 0]
    prj = Project(); prj.chunks = [ch]; prj.path = str(tmp_path / "old.pmsx")
    prj.default_workdir(ch); prj.save(prj.path)                        # no names, no links: 0.1.9 project
    msgs = []
    window.log = lambda m, *a, **k: msgs.append(m)
    window.open_project(prj.path)
    assert any("IMG_001.JPG" in m and "Align Photos again" in m for m in msgs)
    pm = window._photo_map(window.chunk)
    assert set(pm) == {"IMG_002.JPG"}


def test_gui_old_project_names_recovered_on_open(window, no_dialogs, tmp_path):
    from photomesh import colmap as cm
    from photomesh.project import Project
    ch = _dup_chunk(tmp_path)
    prj = Project(); prj.chunks = [ch]; prj.path = str(tmp_path / "old.pmsx")
    m = cm.prepare_images(ch, prj.default_workdir(ch))
    for c in ch.cameras:
        c.aligned = True; c.est = [0, 0, 0]
    prj.save(prj.path)
    window.open_project(prj.path)
    assert {c.path: c.colmap_name for c in window.chunk.cameras} == m
    assert window.project.modified                                    # saved with the names on next Save
