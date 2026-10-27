"""Mapper selection, the coarse-to-fine refinement (no full re-run) and the
offline guarantee. No COLMAP needed except where marked."""
import glob
import os
import re
import sqlite3
import sys

import pytest

from photomesh import fastpipe as fp
from photomesh import colmap as cm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def no_help(monkeypatch):
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())


def _cmds(steps):
    return [a for _, a, _ in steps if isinstance(a, list)]


def _make_db(path, names):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    for t in ("keypoints", "descriptors", "matches", "two_view_geometries"):
        con.execute(f"CREATE TABLE {t} (x INTEGER)")
    con.executemany("INSERT INTO images VALUES (?, ?, 1)", [(i + 1, n) for i, n in enumerate(names)])
    con.commit(); con.close()


def _images_txt(path, names, shared=30):
    with open(path, "w") as f:
        f.write("# images\n")
        for i, n in enumerate(names):
            f.write(f"{i + 1} 1 0 0 0 0 0 {i} 1 {n}\n")
            f.write(" ".join(f"0 0 {k}" for k in range(shared)) + "\n")


# ----------------------------------------------------------------- mappers
def test_choose_mapper(monkeypatch):
    monkeypatch.setattr(cm, "find_glomap", lambda: "glomap.exe")
    assert fp.choose_mapper({"mapper": "Auto"}, 641) == "Global"
    assert fp.choose_mapper({"mapper": "Auto"}, 11) == "Incremental"
    assert fp.choose_mapper({}, 641) == "Global"
    assert fp.choose_mapper({"mapper": "Hierarchical"}, 11) == "Hierarchical"
    monkeypatch.setattr(cm, "find_glomap", lambda: None)
    assert fp.choose_mapper({"mapper": "Auto"}, 641) == "Incremental"
    assert fp.choose_mapper({"mapper": "Global"}, 641) == "Incremental"


def test_global_mapper_steps_use_glomap_with_fallback(no_help, monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "find_glomap", lambda: "glomap.exe")
    out = str(tmp_path / "sp")
    steps = fp.mapper_steps("colmap", "db", "imgs", out, {"mapper": "Global"}, 641)
    g = [a for _, a, _ in steps if isinstance(a, cm.Try)][0]
    assert g[0] == cm.GLOMAP and g[1] == "mapper" and "--database_path" in g
    assert cm.resolve_cmd("colmap", g) == ("glomap.exe", list(g[1:]))
    fb = g.fallback()
    assert any(isinstance(a, list) and a[0] == "mapper" for _, a, _ in fb)
    # an empty output also triggers the incremental fallback
    check = [a for l, a, _ in steps if l.startswith("Checking")][0]
    assert any(isinstance(a, list) and a[0] == "mapper" for _, a, _ in check())


def test_incremental_tuning(no_help):
    a = fp.incremental_mapper_args("colmap", "db", "imgs", "out", "list.txt", n_images=641)
    v = lambda k: a[a.index("--" + k) + 1]
    assert v("Mapper.num_threads") == "-1"
    assert float(v("Mapper.ba_global_images_ratio")) > 1.1
    assert float(v("Mapper.ba_global_points_ratio")) > 1.1
    assert int(v("Mapper.ba_global_max_num_iterations")) < 50
    assert int(v("Mapper.ba_local_max_num_iterations")) >= 10
    small = fp.incremental_mapper_args("colmap", "db", "imgs", "out", n_images=11)
    assert "--Mapper.ba_global_images_ratio" not in small   # defaults for small sets


def test_hierarchical_steps(no_help, monkeypatch):
    steps = fp.mapper_steps("colmap", "db", "imgs", "out", {"mapper": "Hierarchical"}, 641)
    h = [a for _, a, _ in steps if isinstance(a, cm.Try)][0]
    assert h[0] == "hierarchical_mapper" and h[h.index("--num_workers") + 1] == "-1"


def test_model_num_images(tmp_path):
    import struct
    (tmp_path / "images.bin").write_bytes(struct.pack("<Q", 596) + b"x")
    assert fp.model_num_images(str(tmp_path)) == 596
    t = tmp_path / "t"; t.mkdir()
    _images_txt(str(t / "images.txt"), ["a.jpg", "b.jpg"])
    assert fp.model_num_images(str(t)) == 2
    assert fp.model_num_images(str(tmp_path / "missing")) == 0


# ------------------------------------------------- coarse-to-fine refinement
def _fast_wd(tmp_path, monkeypatch, n=5, registered=3):
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())
    monkeypatch.setattr(cm, "find_glomap", lambda: None)
    names = [f"im{i}.jpg" for i in range(n)]
    infos = [{"width": 5280, "height": 3956}] * n
    wd = str(tmp_path)
    s = {"accuracy": "Medium", "key_point_limit": 40000, "use_gpu": True, "mapper": "Incremental"}
    steps = fp.fast_align_steps("colmap", wd, names, infos, s)
    _make_db(os.path.join(wd, "coarse.db"), names)
    _make_db(os.path.join(wd, "database.db"), names)
    os.makedirs(os.path.join(wd, "sparse_coarse_txt"), exist_ok=True)
    (tmp_path / "sparse_coarse_txt" / "cameras.txt").write_text("1 SIMPLE_RADIAL 5280 3956 4000 2640 1978 0\n")
    _images_txt(os.path.join(wd, "sparse_coarse_txt", "images.txt"), names[:registered])
    fp.write_pairs(os.path.join(wd, "pairs.txt"), [(names[i], names[i + 1]) for i in range(n - 1)])
    plan = [a for l, a, _ in steps if l == "Planning fine matching"][0]
    return steps, plan, names


def _expand(steps):
    """Expand gates (no failure) and return the step list."""
    out = []
    for l, a, w in steps:
        if l == "gate":
            out += _expand(a())
        else:
            out.append((l, a, w))
    return out


def test_plan_fine_keeps_coarse_poses_when_images_missing(tmp_path, monkeypatch):
    """Regression: 596/641 registered in coarse used to trigger a full
    incremental re-run. Now: triangulate -> register missing -> triangulate -> BA."""
    _, plan, names = _fast_wd(tmp_path, monkeypatch, n=5, registered=3)
    fine = _expand(plan())
    cmds = [a[0] for a in _cmds(fine)]
    assert "mapper" not in cmds and "hierarchical_mapper" not in cmds
    assert cmds == ["matches_importer", "point_triangulator", "image_registrator", "bundle_adjuster",
                    "point_filtering"]
    # second triangulation only when the registrator actually added images
    retri = [a for l, a, _ in fine if l == "Checking registered images"][0]
    monkeypatch.setattr(fp, "model_num_images", lambda d: 3)
    assert not [a for a in _cmds(retri()) if a[0] == "point_triangulator"]
    monkeypatch.setattr(fp, "model_num_images", lambda d: 5)
    assert [a for a in _cmds(retri()) if a[0] == "point_triangulator"]
    known = os.path.join(str(tmp_path), "sparse_known", "images.txt")
    assert open(known).read().count(".jpg") == 3          # coarse poses kept
    # fine pairs still include the pairs of the unregistered images
    pf = open(os.path.join(str(tmp_path), "pairs_fine.txt")).read()
    assert "im3.jpg im4.jpg" in pf


def test_bundle_adjuster_stops_on_convergence(tmp_path, monkeypatch):
    _, plan, _ = _fast_wd(tmp_path, monkeypatch, n=4, registered=4)
    ba = [a for a in _cmds(_expand(plan())) if a[0] == "bundle_adjuster"][0]
    assert float(ba[ba.index("--BundleAdjustment.function_tolerance") + 1]) > 0
    assert int(ba[ba.index("--BundleAdjustment.max_num_iterations") + 1]) <= 30


def test_plan_fine_all_registered_no_registrator(tmp_path, monkeypatch):
    _, plan, _ = _fast_wd(tmp_path, monkeypatch, n=4, registered=4)
    cmds = [a[0] for a in _cmds(_expand(plan()))]
    assert cmds == ["matches_importer", "point_triangulator", "bundle_adjuster", "point_filtering"]


def test_fine_refine_falls_back_once_and_skips_rest(tmp_path):
    """If the refinement fails, the full re-run is used exactly once and the
    remaining refinement steps are skipped (run with a fake 'colmap' that fails)."""
    known = tmp_path / "coarse_txt"; known.mkdir()
    (known / "cameras.txt").write_text("1 PINHOLE 10 10 5 5 5 5\n")
    _images_txt(str(known / "images.txt"), ["a.jpg", "b.jpg"])
    calls = []
    def rerun():
        calls.append(1)
        return [("Full re-run", lambda: None, 1)]
    steps = fp.fine_refine_steps(sys.executable, "db", "imgs", str(known), str(tmp_path),
                                 str(tmp_path / "fine0"), 1, 3, 2, rerun)
    t = fp.run_steps(sys.executable, steps, log=lambda m: None)   # python cannot run 'point_triangulator'
    labels = [l for l, _ in t]
    assert calls == [1]
    assert "Full re-run" in labels
    assert not any(l.startswith(("Registering", "Optimizing")) for l in labels)


def test_try_failure_is_not_fatal_in_run_steps():
    seen = []
    steps = [("bad", cm.Try(["-c", "raise SystemExit(3)"], lambda: [("fb", lambda: seen.append("fb"), 1)]), 1),
             ("ok", ["-c", "pass"], 1)]
    labels = [l for l, _ in fp.run_steps(sys.executable, steps, log=lambda m: None)]
    assert seen == ["fb"] and labels[-1] == "ok"
    with pytest.raises(RuntimeError):
        fp.run_steps(sys.executable, [("bad", ["-c", "raise SystemExit(3)"], 1)], log=lambda m: None)


def test_gui_runner_try_fallback(qtbot):
    seen = []
    steps = [("bad", cm.Try(["-c", "raise SystemExit(3)"], lambda: [("fb", lambda: seen.append(1), 1)]), 1),
             ("ok", ["-c", "pass"], 1)]
    r = cm.PipelineRunner(sys.executable, steps)
    with qtbot.waitSignal(r.finished, timeout=30000) as blocker:
        r.start()
    assert blocker.args[0] is True and seen == [1]


def test_align_dialog_has_mapper_choice(qtbot):
    from photomesh.dialogs import AlignPhotosDialog
    d = AlignPhotosDialog(); qtbot.addWidget(d)
    assert [d.mapper.itemText(i) for i in range(d.mapper.count())] == fp.MAPPERS
    assert d.settings()["mapper"] == "Auto"


# ----------------------------------------------------------------- offline
NET = re.compile(r"^\s*(import|from)\s+(socket|urllib|requests|http|httpx|aiohttp|ftplib|smtplib|"
                 r"websocket|PySide6\.QtNetwork|PySide6\.QtWebEngine\w*)\b", re.M)


def test_source_has_no_network_code():
    files = glob.glob(os.path.join(ROOT, "photomesh", "*.py")) + \
        [os.path.join(ROOT, f) for f in ("main.py", "benchmark.py")]
    for f in files:
        src = open(f, encoding="utf-8").read()
        assert not NET.search(src), f"network import in {f}"
        assert "QtNetwork" not in src and "vocab_tree" not in src, f
        assert not re.search(r"openUrl\(\s*QUrl\(\s*[\"']https?:", src), f"opens a web page: {f}"


def test_pipeline_runs_with_network_blocked(image_dir, tmp_path, monkeypatch):
    """Analysis + pair selection + step building with sockets blocked (see conftest)."""
    import socket
    with pytest.raises(Exception):
        socket.create_connection(("example.com", 80), timeout=1)
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())
    paths = sorted(str(p) for p in image_dir.iterdir())
    infos = fp.analyze_images(paths, str(tmp_path / "c"), workers=2)
    names = [os.path.basename(p) for p in paths]
    steps = fp.fast_align_steps("colmap", str(tmp_path / "wd"), names, [infos[p] for p in paths],
                                {"accuracy": "High", "key_point_limit": 0})
    steps[0][1]()      # pair preselection runs in-process
    assert os.path.getsize(str(tmp_path / "wd" / "pairs.txt")) > 0


def test_online_help_stays_local(window, no_dialogs, monkeypatch):
    """Help > Contents opens the bundled README (the duplicate 'Online Knowledge Base' entry was removed in 0.1.12)."""
    from PySide6.QtGui import QDesktopServices
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: urls.append(u.toString()) or True))
    assert "Help/Online Knowledge Base" not in window.actions
    window.actions["Help/Contents"].trigger()
    assert urls and all(u.startswith("file:") for u in urls)


DATA = os.environ.get("PM_TESTDATA", "")


@pytest.mark.colmap
@pytest.mark.skipif(not (cm.find_colmap() and DATA and os.path.isdir(DATA)),
                    reason="needs COLMAP and PM_TESTDATA images")
@pytest.mark.parametrize("mapper", ["Global", "Incremental"])
def test_real_align_offline(tmp_path, mapper):
    """Real COLMAP/GLOMAP alignment (Low accuracy) with the network blocked."""
    from benchmark import _Chunk
    if mapper == "Global" and not cm.find_glomap():
        pytest.skip("GLOMAP not bundled")
    exe = cm.find_colmap()
    paths = sorted(glob.glob(os.path.join(DATA, "*.[jJ][pP][gG]")))
    infos = fp.analyze_images(paths, str(tmp_path / "c"))
    wd = str(tmp_path / "wd")
    mapping = cm.prepare_images(_Chunk(paths), wd)
    s = {"mode": "Fast", "accuracy": "Medium", "key_point_limit": 8000, "use_gpu": cm.has_cuda(exe),
         "mapper": mapper}
    steps = cm.align_steps(exe, _Chunk(paths), wd, s, names=[mapping[p] for p in paths],
                           infos=[infos[p] for p in paths])
    log = []
    fp.run_steps(exe, steps, log=log.append, logfile=str(tmp_path / "log.txt"))
    assert not any("falling back" in l for l in log), log
    n = fp.model_num_images(os.path.join(wd, "sparse", "0"))
    assert n >= 0.8 * len(paths)
    assert cm.count_points3d(os.path.join(wd, "sparse_txt", "points3D.txt")) > 500


# ------------------------------------------------------- decode-once cache
def test_work_images_decode_once(image_dir, tmp_path):
    from PIL import Image
    names = sorted(p.name for p in image_dir.iterdir())
    before = {n: os.path.getmtime(image_dir / n) for n in names}
    f, c = str(tmp_path / "f"), str(tmp_path / "c")
    dims = fp.build_work_images(names, str(image_dir), {f: 320, c: 160}, workers=2)
    assert set(dims) == set(names)
    for n in names:
        assert dims[n]["orig"] == (640, 480) and dims[n][f] == (320, 240) and dims[n][c] == (160, 120)
        with Image.open(os.path.join(f, n)) as im:
            assert im.size == (320, 240)
            assert im.getexif().get_ifd(0x8825)          # GPS kept for COLMAP / reference
    assert sorted(p.name for p in image_dir.iterdir()) == names   # nothing written to the source
    assert {n: os.path.getmtime(image_dir / n) for n in names} == before
    with pytest.raises(RuntimeError):
        fp.build_work_images(names, str(image_dir), {str(image_dir / "sub"): 100}, workers=1)


def _feature_db(path, w, h, f, kps):
    import numpy as np
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, "
                "height INTEGER, params BLOB, prior_focal_length INTEGER)")
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    con.execute("CREATE TABLE keypoints (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB)")
    con.execute("INSERT INTO cameras VALUES (1, 2, ?, ?, ?, 1)",
                (w, h, np.array([f, w / 2, h / 2, 0.01]).tobytes()))
    con.execute("INSERT INTO images VALUES (1, 'a.jpg', 1)")
    con.execute("INSERT INTO keypoints VALUES (1, ?, 6, ?)", (len(kps), np.array(kps, np.float32).tobytes()))
    con.commit(); con.close()


def test_rescale_keypoints_and_camera_to_original(tmp_path):
    import numpy as np
    db = str(tmp_path / "d.db")
    _feature_db(db, 2640, 1978, 2000.0, [[100, 50, 2, 0, 0, 2], [2639.5, 1977.5, 1, 1, 1, 1]])
    dims = {"a.jpg": {"orig": (5280, 3956), "W": (2640, 1978)}}
    fp.rescale_db_to_original(db, dims, "W")
    con = sqlite3.connect(db)
    w, h, p = con.execute("SELECT width, height, params FROM cameras").fetchone()
    kp = np.frombuffer(con.execute("SELECT data FROM keypoints").fetchone()[0], np.float32).reshape(-1, 6)
    con.close()
    assert (w, h) == (5280, 3956)
    assert np.allclose(np.frombuffer(p, np.float64), [4000, 2640, 1978, 0.01])
    assert np.allclose(kp[0], [200, 100, 4, 0, 0, 4]) and np.allclose(kp[1, :2], [5279, 3955])
    # fine stage: camera back to working size before extraction, then original again
    fp.set_cameras_to_work_size(db, dims, "W")
    con = sqlite3.connect(db)
    w, h, p = con.execute("SELECT width, height, params FROM cameras").fetchone(); con.close()
    assert (w, h) == (2640, 1978) and np.allclose(np.frombuffer(p, np.float64)[:3], [2000, 1320, 989])
    fp.rescale_db_to_original(db, dims, "W")   # keypoints x2 again, camera to original once
    con = sqlite3.connect(db)
    assert con.execute("SELECT width FROM cameras").fetchone()[0] == 5280
    con.close()


def test_fast_steps_use_cache_and_feature_cap(tmp_path, monkeypatch):
    _fast_wd(tmp_path, monkeypatch)  # sets option/glomap stubs
    names = ["a.jpg", "b.jpg"]
    infos = [{"width": 5280, "height": 3956}] * 2
    steps = fp.fast_align_steps("colmap", str(tmp_path / "w"), names, infos,
                                {"accuracy": "Medium", "key_point_limit": 40000})
    labels = [l for l, _, _ in steps]
    assert labels.index("Decoding photos (once, parallel)") < labels.index("Detecting points (coarse 1024px)")
    fe = [a for a in _cmds(steps) if a[0] == "feature_extractor"]
    assert all("_work_" in a[a.index("--image_path") + 1] for a in fe)
    fine = fe[-1]
    assert fine[fine.index("--SiftExtraction.max_num_features") + 1] == "40000"
    assert "Selecting fine key points" in labels and "Coloring tie points" in labels
    assert labels.count("Rescaling key points") == 2
    steps = fp.fast_align_steps("colmap", str(tmp_path / "w2"), names, infos,
                                {"accuracy": "Medium", "key_point_limit": 40000, "decode_cache": False,
                                 "fast_feature_cap": False})
    fe = [a for a in _cmds(steps) if a[0] == "feature_extractor"]
    assert not any("_work_" in a[a.index("--image_path") + 1] for a in fe)
    assert fe[-1][fe[-1].index("--SiftExtraction.max_num_features") + 1] == "40000"


def test_select_keypoints_keeps_small_and_large(tmp_path):
    import numpy as np
    db = str(tmp_path / "k.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE keypoints (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB)")
    con.execute("CREATE TABLE descriptors (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB)")
    sc = np.arange(1, 101, dtype=np.float32)            # scales 1..100
    k = np.zeros((100, 6), np.float32); k[:, 0] = sc; k[:, 2] = sc; k[:, 5] = sc
    d = np.repeat(np.arange(100, dtype=np.uint8)[:, None], 128, 1)
    con.execute("INSERT INTO keypoints VALUES (1, 100, 6, ?)", (k.tobytes(),))
    con.execute("INSERT INTO descriptors VALUES (1, 100, 128, ?)", (d.tobytes(),))
    con.commit(); con.close()
    assert fp.select_keypoints(db, 10, small_frac=0.8) == 1
    con = sqlite3.connect(db)
    r, kb = con.execute("SELECT rows, data FROM keypoints").fetchone()
    dr, dbb = con.execute("SELECT rows, data FROM descriptors").fetchone()
    con.close()
    kk = np.frombuffer(kb, np.float32).reshape(r, 6); dd = np.frombuffer(dbb, np.uint8).reshape(dr, 128)
    assert r == dr == 10
    assert sorted(kk[:, 0].tolist()) == [1, 2, 3, 4, 5, 6, 7, 8, 99, 100]
    assert (dd[:, 0] == (kk[:, 0] - 1)).all()            # descriptors stay aligned




def test_select_keypoints_skips_small_and_unsupported_images(tmp_path):
    import numpy as np
    db = str(tmp_path / "many.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE keypoints (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB)")
    con.execute("CREATE TABLE descriptors (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB)")
    for iid, n, cols in ((1, 40, 4), (2, 5, 4), (3, 40, 3), (4, 40, 6)):
        kp = np.ones((n, cols), np.float32)
        desc = np.full((n, 128), iid, np.uint8)
        con.execute("INSERT INTO keypoints VALUES (?, ?, ?, ?)", (iid, n, cols, kp.tobytes()))
        con.execute("INSERT INTO descriptors VALUES (?, ?, 128, ?)", (iid, n, desc.tobytes()))
    con.commit()
    before = dict(con.execute("SELECT image_id, data FROM keypoints"))
    con.close()
    assert fp.select_keypoints(db, 10) == 2
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT image_id, rows FROM keypoints"))
    after = dict(con.execute("SELECT image_id, data FROM keypoints"))
    con.close()
    assert rows == {1: 10, 2: 5, 3: 40, 4: 10}
    assert after[2] == before[2] and after[3] == before[3]


def _write_model_bin(d, img_w=400, img_h=300):
    import struct
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "images.bin"), "wb") as f:
        f.write(struct.pack("<Q", 1))
        f.write(struct.pack("<I", 7) + struct.pack("<7d", 1, 0, 0, 0, 0, 0, 0) + struct.pack("<I", 1))
        f.write(b"a.jpg\0" + struct.pack("<Q", 2))
        f.write(struct.pack("<ddq", 10.0, 10.0, 1) + struct.pack("<ddq", 390.0, 290.0, 2))
    with open(os.path.join(d, "points3D.bin"), "wb") as f:
        f.write(struct.pack("<Q", 2))
        for pid, i2 in ((1, 0), (2, 1)):
            f.write(struct.pack("<Q3d3BdQ", pid, 0, 0, 0, 0, 0, 0, 0.5, 2) + struct.pack("<IIII", 7, i2, 9, 0))


def test_color_model_from_work_images(tmp_path):
    import struct
    from PIL import Image
    model = str(tmp_path / "m"); _write_model_bin(model)
    work = str(tmp_path / "w"); os.makedirs(work)
    im = Image.new("RGB", (200, 150), (0, 0, 255)); im.paste((255, 0, 0), (0, 0, 100, 75))
    im.save(os.path.join(work, "a.jpg"), quality=100)
    dims = {"a.jpg": {"orig": (400, 300), work: (200, 150)}}
    assert fp.color_model_from_work_images(model, work, dims, workers=1) == 2
    buf = open(os.path.join(model, "points3D.bin"), "rb").read()
    c1 = struct.unpack_from("<3B", buf, 8 + 32)
    c2 = struct.unpack_from("<3B", buf, 8 + 51 + 16 + 32)
    assert c1[0] > 200 and c1[2] < 60          # top-left: red
    assert c2[2] > 200 and c2[0] < 60          # bottom-right: blue


# ------------------------------------------------ mixed cameras (Mavic 3 Pro)
def test_merge_identical_cameras_keeps_mixed_cameras(tmp_path):
    import numpy as np
    db = str(tmp_path / "c.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, "
                "height INTEGER, params BLOB, prior_focal_length INTEGER)")
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    wide = np.array([3700.0, 2640, 1978, 0]).tobytes(); tele = np.array([9500.0, 2016, 1512, 0]).tobytes()
    cams = [(1, 5280, 3956, wide), (2, 5280, 3956, wide), (3, 4032, 3024, tele), (4, 5280, 3956, wide),
            (5, 4032, 3024, tele)]
    for cid, w, h, p in cams:
        con.execute("INSERT INTO cameras VALUES (?, 2, ?, ?, ?, 1)", (cid, w, h, p))
        con.execute("INSERT INTO images VALUES (?, ?, ?)", (cid, f"im{cid}.jpg", cid))
    con.commit(); con.close()
    assert fp.merge_identical_cameras(db) == {1: 3, 3: 2}
    con = sqlite3.connect(db)
    assert dict(con.execute("SELECT name, camera_id FROM images")) == \
        {"im1.jpg": 1, "im2.jpg": 1, "im3.jpg": 3, "im4.jpg": 1, "im5.jpg": 3}
    assert con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 2
    con.close()


def test_extractor_never_uses_colmap_single_camera(tmp_path, monkeypatch):
    """COLMAP's single_camera rejects photos of a different size (the 44 tele
    photos of the drone set were silently skipped); we merge cameras instead."""
    _fast_wd(tmp_path, monkeypatch)
    infos = [{"width": 5280, "height": 3956}, {"width": 4032, "height": 3024}]
    steps = fp.fast_align_steps("colmap", str(tmp_path / "w"), ["a.jpg", "b.jpg"], infos,
                                {"accuracy": "Medium", "key_point_limit": 40000, "single_camera": True})
    fe = [a for a in _cmds(steps) if a[0] == "feature_extractor"]
    assert fe and all(a[a.index("--ImageReader.single_camera") + 1] == "0" for a in fe)
    labels = [l for l, _, _ in steps]
    assert labels.index("Grouping cameras") < labels.index("Rescaling key points")


def test_unaligned_photos_reported(window, image_dir):
    window.add_photos(sorted(str(p) for p in image_dir.iterdir()))
    cams = window.chunk.cameras
    window._report_unaligned(cams[:2], len(cams))
    assert f"{len(cams) - 2}/{len(cams)} photos aligned" in window.statusBar().currentMessage()
