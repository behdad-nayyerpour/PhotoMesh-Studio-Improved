"""Build Point Cloud undistortion: float32 shared maps, RAM-capped workers,
serial retry on MemoryError, resume of finished photos, memory estimates."""
import json
import os
import tracemalloc

import numpy as np
import pytest

from photomesh import colmap_io as cio
from photomesh import dense, memguard as mg
from tests.test_dense_mesh_gcp import synthetic_model

GB, MB = mg.GB, mg.MB


# ------------------------------------------------------------ estimates / caps
def test_undistort_memory_estimate_dji_full_res():
    """5280 x 3956 (DJI M3E) at Ultra high: a few hundred MB per worker, not
    the ~2 GB of float64 temporaries the old code needed."""
    W, H = 5280, 3956
    e = mg.undistort_worker_bytes(W, H, W, H)
    assert 250 * MB < e < 700 * MB, e / MB
    # parts: decoded photo x2 + channel + output x2 + float32 coordinate blocks
    assert e >= 2 * W * H * 3 + W * H + 2 * W * H * 3
    # High (1/2): about a quarter
    e2 = mg.undistort_worker_bytes(W // 2, H // 2, W // 2, H // 2)
    base = 120 * MB                                                    # interpreter + libraries
    assert e2 - base < 0.4 * (e - base)
    # extra copy only when the DCT-decoded size differs from the work size
    assert mg.undistort_worker_bytes(330, 248, 330, 248, 660, 495) > mg.undistort_worker_bytes(330, 248, 330, 248)


def test_cap_workers_by_ram():
    per = 500 * MB
    assert mg.cap_workers(16, per, available=22.6 * GB) == 16          # this laptop now
    assert mg.cap_workers(32, per, available=200 * GB) == 16           # 16-worker limit kept
    n = mg.cap_workers(16, per, available=6 * GB)                       # (6-2)/(0.5*1.5) = 5
    assert n == 5
    assert mg.cap_workers(16, per, available=1 * GB) == 1               # never 0
    assert mg.cap_workers(16, 2 * GB, available=12 * GB, base_bytes=3 * GB) == 2
    assert mg.cap_workers(8, 0, available=1 * GB) == 8                  # no estimate: unchanged


def test_old_failure_would_now_be_capped():
    """The failing run: 16 workers on a 32 GB laptop with ~19 GB free while
    the GUI held ~4 GB. The cap must keep the planned total well below free RAM."""
    per = mg.undistort_worker_bytes(5280, 3956, 5280, 3956)
    for free in (19 * GB, 8 * GB, 4 * GB):
        n = mg.cap_workers(16, per, available=free)
        assert n * per * mg.SAFETY <= max(free - mg.RESERVE, per * mg.SAFETY)


def test_ortho_block_estimate_and_cap():
    from photomesh.ortho import ortho_block_bytes
    b = ortho_block_bytes(2048)
    assert 0.8 * GB < b < 2.5 * GB
    assert ortho_block_bytes(1024) < b / 3


# ------------------------------------------------------------ maps
def _distorted_cam(w=900, h=600):
    return cio.Camera(4, w, h, np.array([700., 690., w / 2 + 3, h / 2 - 2, -0.11, 0.04, 0.0008, -0.0005]))


def test_maps_are_float32_and_shared_file(tmp_path):
    cam = _distorted_cam()
    rows, cols = dense.remap_table(cam, 1)
    assert rows.dtype == np.float32 and cols.dtype == np.float32
    p = str(tmp_path / "m.npy")
    dense.write_map(p, cam, 2)
    M = np.load(p, mmap_mode="r")
    assert M.dtype == np.float32 and M.shape == (2, 300, 450)
    assert not os.path.exists(p + ".part.npy")
    # same positions as the full-res table, scaled to the downscaled work image
    r2, c2 = dense.remap_table(cam, 2)
    np.testing.assert_allclose(M[1], c2 * (450 / 900) - 0.5, atol=1e-3)
    np.testing.assert_allclose(M[0], r2 * (300 / 600) - 0.5, atol=1e-3)


def test_remap_table_has_no_full_size_float64_temporaries():
    """Working memory beyond the float32 result is bounded by the row block,
    i.e. it does not grow with the image height (old: ~10 x 8 x W x H)."""
    W = 2000
    extra = []
    for H in (1500, 3000):
        cam = _distorted_cam(W, H)
        tracemalloc.start()
        dense.remap_table(cam, 1)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        extra.append(peak - 2 * 4 * W * H)
    assert extra[1] < 1.2 * extra[0] + MB, [e / MB for e in extra]
    assert extra[1] < 8 * W * 3000, extra[1] / MB             # less than ONE full float64 plane


def test_remap_matches_unblocked_computation():
    cam = _distorted_cam(300, 200)
    r, c = dense.remap_table(cam, 1, block_rows=7)
    u = (np.arange(300) + 0.5 - cam.K()[2]) / cam.K()[0]
    v = (np.arange(200) + 0.5 - cam.K()[3]) / cam.K()[1]
    x, y = np.meshgrid(u, v)
    xd, yd = cio.distort(cam, x, y)
    np.testing.assert_allclose(c, cam.K()[0] * xd + cam.K()[2], atol=2e-3)
    np.testing.assert_allclose(r, cam.K()[1] * yd + cam.K()[3], atol=2e-3)


def _photo(path, w, h, seed=0):
    from PIL import Image
    x, y = np.meshgrid(np.arange(w), np.arange(h))
    a = np.stack([128 + 100 * np.sin(x / 9.), 128 + 100 * np.cos(y / 7.), (x * y) % 255], -1).astype(np.uint8)
    Image.fromarray(a).save(path, quality=97)


def test_worker_memory_is_bounded(tmp_path):
    W, H = 1600, 1200
    cam = _distorted_cam(W, H)
    src = str(tmp_path / "p.jpg"); _photo(src, W, H)
    mp_ = str(tmp_path / "m.npy"); dense.write_map(mp_, cam, 1)
    tracemalloc.start()
    dst, ok, msg, kind, _peak = dense._undistort_one((src, str(tmp_path / "o.jpg"), mp_, (W, H)))
    _c, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert ok, msg
    # photo (PIL tobytes builds it twice, briefly) + one channel + output + coordinate blocks;
    # the old code also held float64 (rows, cols) maps per worker: 2 x 8 x W x H more
    assert peak < (2 * 3 + 1 + 3) * W * H + 4 * 2 * 4 * 256 * W, peak / MB
    assert peak < mg.undistort_worker_bytes(W, H, W, H) - 120 * MB
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".part.jpg")]
    assert dense.valid_output(dst, (W, H))


# ------------------------------------------------------------ retry / resume
def _workspace(tmp_path):
    from PIL import Image
    m = synthetic_model()
    model_dir = tmp_path / "sparse"; cio.write_model(str(model_dir), m)
    photos = tmp_path / "photos"; photos.mkdir()
    for k, im in enumerate(m.images.values()):
        c = m.cameras[im.camera_id]
        _photo(str(photos / im.name), c.width, c.height, k)
    return m, str(model_dir), str(photos)


def test_memory_error_is_retried_serially(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    real = dense._undistort_one
    seen = []

    def flaky(job):
        if job[1] not in seen:                                 # first attempt of every 2nd photo
            seen.append(job[1])
            if len(seen) % 2 == 0:
                return job[1], False, "MemoryError: Unable to allocate 159. MiB", "MemoryError", 0
        return real(job)
    monkeypatch.setattr(dense, "_undistort_one", flaky)
    m, model_dir, photos = _workspace(tmp_path)
    logs = []
    orig = dense.run_undistort
    monkeypatch.setattr(dense, "run_undistort",
                        lambda jobs, w, log=print, progress=None: orig(jobs, 3, log, progress,
                                                                       executor_factory=ThreadPoolExecutor))
    r = dense.prepare_workspace(model_dir, photos, str(tmp_path / "dense"), "High", workers=3, log=logs.append)
    assert r["undistorted"] == 8 and r["memory"]["retried"] == 4
    assert any("Retrying 4 photo(s) one at a time" in l for l in logs)
    assert any("peak memory" in l for l in logs)
    assert len(os.listdir(tmp_path / "dense" / "images")) == 8


def test_persistent_failure_still_reported(tmp_path, monkeypatch):
    def always(job):
        return job[1], False, "MemoryError: nope", "MemoryError", 0
    monkeypatch.setattr(dense, "_undistort_one", always)
    m, model_dir, photos = _workspace(tmp_path)
    with pytest.raises(RuntimeError, match="also failed when retried alone"):
        dense.prepare_workspace(model_dir, photos, str(tmp_path / "dense"), "High", workers=1, log=lambda *_: None)


def test_resume_skips_valid_outputs_and_redoes_broken_ones(tmp_path):
    m, model_dir, photos = _workspace(tmp_path)
    dd = str(tmp_path / "dense")
    r = dense.prepare_workspace(model_dir, photos, dd, "High", workers=1, log=lambda *_: None)
    assert r["undistorted"] == 8 and r["reused"] == 0
    img = os.path.join(dd, "images")
    # truncated file (killed while writing) and a leftover temp file
    p = os.path.join(img, "img03.jpg"); data = open(p, "rb").read(); open(p, "wb").write(data[: len(data) // 2])
    open(os.path.join(img, "img04.jpg.part.jpg"), "wb").write(b"x")
    logs = []
    r = dense.prepare_workspace(model_dir, photos, dd, "High", workers=1, log=logs.append)
    assert r["undistorted"] == 1 and r["reused"] == 7
    assert any("Reusing 7 already undistorted" in l for l in logs)
    assert not os.path.exists(os.path.join(img, "img04.jpg.part.jpg"))
    assert not os.path.exists(os.path.join(dd, "undistort_maps"))
    r = dense.prepare_workspace(model_dir, photos, dd, "High", workers=1, log=lambda *_: None)
    assert r["undistorted"] == 0 and r["reused"] == 8


def test_valid_output_checks_size_and_completeness(tmp_path):
    from PIL import Image
    p = str(tmp_path / "a.jpg")
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (64, 80, 3), np.uint8)).save(p)
    assert dense.valid_output(p, (80, 64))
    assert not dense.valid_output(p, (40, 32))
    assert not dense.valid_output(str(tmp_path / "missing.jpg"), (80, 64))


def _steps(wd, image_root, quality="Ultra high", reuse=False):
    return dense.dense_steps("colmap", str(wd), {"quality": quality, "reuse": reuse}, image_root)


def test_rerun_keeps_undistorted_photos_without_reuse_depth_maps(tmp_path):
    """Clicking Build Point Cloud again after the failure ('Reuse depth maps'
    off, same settings) must keep the finished photos; the depth maps go."""
    wd = tmp_path / "wd"; (wd / "sparse" / "0").mkdir(parents=True)
    cio.write_model(str(wd / "sparse" / "0"), synthetic_model())
    _steps(wd, "F:/photos")
    img = wd / "dense" / "images"; img.mkdir(exist_ok=True); (img / "a.jpg").write_bytes(b"done")
    st = wd / "dense" / "stereo" / "depth_maps"; st.mkdir(parents=True, exist_ok=True)
    (st / "a.jpg.geometric.bin").write_bytes(b"d")
    _steps(wd, "F:/photos")                                    # same settings, reuse off, run unfinished:
    assert (img / "a.jpg").exists() and (st / "a.jpg.geometric.bin").exists()   # resumed automatically
    (wd / "dense" / "point_cloud.ply").write_bytes(b"ply")      # a finished run ...
    _steps(wd, "F:/photos")                                    # ... and reuse off: depth maps are redone
    assert (img / "a.jpg").exists() and not (st / "a.jpg.geometric.bin").exists()
    (st.mkdir(parents=True, exist_ok=True), (st / "a.jpg.geometric.bin").write_bytes(b"d"))
    _steps(wd, "F:/photos", reuse=True)                         # reuse on: depth maps kept too
    assert (st / "a.jpg.geometric.bin").exists()
    _steps(wd, "F:/photos", quality="High")                     # other quality: everything redone
    assert not (img / "a.jpg").exists()


def test_rerun_after_realignment_redoes_photos(tmp_path):
    wd = tmp_path / "wd"; (wd / "sparse" / "0").mkdir(parents=True)
    cio.write_model(str(wd / "sparse" / "0"), synthetic_model())
    _steps(wd, "F:/photos")
    img = wd / "dense" / "images"; img.mkdir(exist_ok=True); (img / "a.jpg").write_bytes(b"done")
    cio.write_model(str(wd / "sparse" / "0"), synthetic_model(n_img=9))   # new alignment
    _steps(wd, "F:/photos")
    assert not (img / "a.jpg").exists()


def test_legacy_marker_without_model_signature(tmp_path):
    """The marker of the failed run (no model signature yet) is accepted when
    the model files are older than it - the user's case."""
    wd = tmp_path / "wd"; (wd / "sparse" / "0").mkdir(parents=True)
    cio.write_model(str(wd / "sparse" / "0"), synthetic_model())
    d = wd / "dense"; (d / "images").mkdir(parents=True); (d / "images" / "a.jpg").write_bytes(b"done")
    cfg = {"model": str(wd / "sparse" / "0"), "image_root": "F:/photos", "dense": str(d),
           "quality": "Ultra high", "max_sources": 8}
    (d / "workspace.json").write_text(json.dumps(cfg))
    t = os.path.getmtime(wd / "sparse" / "0" / "images.bin") + 10
    os.utime(d / "workspace.json", (t, t))
    _steps(wd, "F:/photos")
    assert (d / "images" / "a.jpg").exists()
    assert "model_sig" in json.load(open(d / "workspace.json"))


def test_peak_monitor_reports():
    mon = mg.PeakMonitor(0.05).start()
    a = np.ones(20 * MB, np.uint8); a[::4096] = 2
    r = mon.stop()
    del a
    assert r["peak_tree"] > 0 and r["min_available"] is None or r["min_available"] > 0
