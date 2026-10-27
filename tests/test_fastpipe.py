"""Unit tests for fastpipe and the COLMAP command builders (no COLMAP needed)."""
import os

import numpy as np
import pytest

from photomesh import fastpipe as fp
from photomesh import colmap as cm


def test_analyze_parallel_and_cache(image_dir, tmp_path):
    paths = sorted(str(p) for p in image_dir.iterdir())
    res = fp.analyze_images(paths, str(tmp_path / "c"), workers=2)
    assert set(res) == set(paths)
    for r in res.values():
        assert r["width"] == 640 and r["height"] == 480
        assert os.path.exists(r["thumb"]) and np.load(r["desc"]).shape == (320,)
        assert r["lat"] is not None
    res2 = fp.analyze_images(paths, str(tmp_path / "c"), workers=1)
    assert res2[paths[0]]["thumb"] == res[paths[0]]["thumb"]


def test_analyze_bad_file(tmp_path):
    bad = tmp_path / "x.jpg"; bad.write_bytes(b"nope")
    r = fp.analyze_images([str(bad)] * 3, str(tmp_path / "c"), workers=2)
    assert "error" in r[str(bad)]


def test_descriptor_similarity():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 255, (96, 128)).astype(np.uint8)
    b = np.clip(a.astype(int) + rng.integers(-10, 10, a.shape), 0, 255).astype(np.uint8)
    c = rng.integers(0, 255, (96, 128)).astype(np.uint8)
    da, db, dc = (fp.global_descriptor(x) for x in (a, b, c))
    assert np.isclose(np.linalg.norm(da), 1, atol=1e-4)
    assert da @ db > da @ dc


def test_select_pairs_scales_linearly():
    n = 200
    names = [f"im{i:03d}.jpg" for i in range(n)]
    infos = [{"lat": 40 + (i % 20) * 1e-4, "lon": 44 + (i // 20) * 1e-4, "alt": 100} for i in range(n)]
    pairs = fp.select_pairs(names, infos, k_desc=8, k_gps=8, window=3, use_desc=False)
    assert len(pairs) < n * (8 + 3)          # O(n), not O(n^2)
    assert len(pairs) < n * (n - 1) // 2 / 10
    assert all(a < b for a, b in pairs)
    assert ("im000.jpg", "im001.jpg") in pairs   # sequential neighbour
    assert ("im000.jpg", "im020.jpg") in pairs   # GPS neighbour (next strip)


def test_select_pairs_no_metadata():
    names = ["a.jpg", "b.jpg", "c.jpg"]
    assert fp.select_pairs(names, [{}, {}, {}], window=1) == [("a.jpg", "b.jpg"), ("b.jpg", "c.jpg")]


def test_covisible_pairs(tmp_path):
    txt = tmp_path / "images.txt"
    txt.write_text("# header\n"
                   "1 1 0 0 0 0 0 0 1 a.jpg\n" + " ".join(f"0 0 {i}" for i in range(20)) + "\n"
                   "2 1 0 0 0 0 0 0 1 b.jpg\n" + " ".join(f"0 0 {i}" for i in range(20)) + "\n"
                   "3 1 0 0 0 0 0 0 1 c.jpg\n" + " ".join(f"0 0 {i}" for i in range(15, 25)) + "\n")
    pairs, reg = fp.covisible_pairs(str(txt), min_shared=15)
    assert pairs == {("a.jpg", "b.jpg")} and reg == {"a.jpg", "b.jpg", "c.jpg"}


def test_read_images_txt(tmp_path):
    txt = tmp_path / "images.txt"
    txt.write_text("1 1 0 0 0 1 2 3 1 a.jpg\n\n")
    poses = cm.read_images_txt(str(txt))
    C, R = poses["a.jpg"]
    assert np.allclose(R, np.eye(3)) and np.allclose(C, [-1, -2, -3])


def test_opt_fallback(monkeypatch):
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())
    assert cm.opt("x", "feature_extractor", ["New.name", "Old.name"], 5) == ["--Old.name", "5"]
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: {"New.name", "Old.name"})
    assert cm.opt("x", "feature_extractor", ["New.name", "Old.name"], 5) == ["--New.name", "5"]
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: {"Other"})
    assert cm.opt("x", "feature_extractor", ["New.name"], 5) == []


def test_fast_steps_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())
    names = ["a.jpg", "b.jpg"]
    infos = [{"width": 4000, "height": 3000}] * 2
    s = {"accuracy": "High", "key_point_limit": 40000, "use_gpu": True}
    steps = fp.fast_align_steps("colmap", str(tmp_path), names, infos, s)
    labels = [l for l, _, _ in steps]
    assert labels[0] == "Selecting image pairs"
    assert any("coarse" in l for l in labels) and any("fine" in l for l in labels)
    cmds = [a for _, a, _ in steps if isinstance(a, list)]
    assert any(c[0] == "matches_importer" for c in cmds)
    assert not any(c[0] == "exhaustive_matcher" for c in cmds)
    assert all("1" == c[c.index("--SiftExtraction.use_gpu") + 1] for c in cmds if c[0] == "feature_extractor")
    # low accuracy -> no fine stage
    steps = fp.fast_align_steps("colmap", str(tmp_path), names, infos, dict(s, accuracy="Lowest"))
    assert not any("fine" in l for l, _, _ in steps)


def test_exhaustive_mode_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "colmap_options", lambda exe, c: set())
    monkeypatch.setattr(cm, "image_max_dim", lambda p: 2000)
    class C:  # minimal chunk
        cameras = []
    steps = cm.align_steps("colmap", C, str(tmp_path), {"mode": "Exhaustive", "accuracy": "Medium",
                                                     "key_point_limit": 0, "use_gpu": False})
    assert steps[1][1][0] == "exhaustive_matcher"
    fe = steps[0][1]
    assert fe[fe.index("--SiftExtraction.max_image_size") + 1] == "1000"


def test_ply_roundtrip(tmp_path):
    from photomesh.plyio import save_ply, load_ply
    p = tmp_path / "a.ply"
    pts = np.random.rand(10, 3).astype(np.float32)
    save_ply(str(p), pts, np.random.rand(10, 3), np.array([[0, 1, 2]]))
    d = load_ply(str(p))
    assert np.allclose(d["points"], pts) and d["faces"].shape == (1, 3) and d["colors"].shape == (10, 4)


def test_prepare_images_reads_in_place(image_dir, tmp_path):
    """Photos in one folder are read in place: nothing is written next to them."""
    before = sorted(os.listdir(image_dir))
    class Cam:
        def __init__(self, p): self.path, self.enabled = p, True
    class Ch: pass
    ch = Ch(); ch.cameras = [Cam(str(image_dir / f)) for f in before]
    ch.cameras[0].enabled = False
    wd = tmp_path / "wd"
    mapping = cm.prepare_images(ch, str(wd))
    assert sorted(os.listdir(image_dir)) == before
    assert cm.image_dir(str(wd)) == str(image_dir)
    assert open(cm.image_list(str(wd))).read().split() == before[1:]
    assert not (wd / "images").exists() and len(mapping) == len(before) - 1


def test_covisible_cap(tmp_path):
    txt = tmp_path / "images.txt"
    lines = []
    for i in range(30):   # every image sees the same 50 points -> 435 pairs uncapped
        lines.append(f"{i + 1} 1 0 0 0 0 0 0 1 im{i:02d}.jpg\n" + " ".join(f"0 0 {k}" for k in range(50)) + "\n")
    txt.write_text("".join(lines))
    full, _ = fp.covisible_pairs(str(txt), 15, max_per_image=0)
    capped, _ = fp.covisible_pairs(str(txt), 15, max_per_image=5)
    assert len(full) == 435 and len(capped) <= 30 * 5


def test_gps_altitude_ref_bytes():
    class E:
        def get_ifd(self, _):
            return {1: "N", 2: (38.0, 40.0, 40.2), 3: "E", 4: (69.0, 45.0, 46.9), 5: b"\x01", 6: 12.5}
    lon, lat, alt = fp._gps(E())
    assert alt == -12.5 and lat == pytest.approx(38.6778, abs=1e-4)
