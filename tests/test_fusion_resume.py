"""Build Point Cloud fusion robustness (0.1.4): map validation (the 'Check
failed: width_ > 0' / exit 0xC0000409 crash), resume that recomputes only
broken views, disk-space check, memory-aware fusion plan with retries, and
the depth-map / fusion progress meters."""
import os
import sys

import numpy as np
import pytest

from photomesh import colmap_io as cio
from photomesh import dense
from photomesh.progress import PatchMatchMeter, FusionMeter, fmt_eta
from tests.test_dense_mesh_gcp import synthetic_model

GB = dense.GB


def write_map(path, w, h, c, value=1.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(f"{w}&{h}&{c}&".encode())
        f.write(np.full(w * h * c, value, np.float32).tobytes())


def make_workspace(tmp_path, kinds=("geometric",), reuse=True):
    """wd/dense with sparse model (600x450 + 400x300 cameras), cfg files and
    valid maps of the given kinds for all 8 views; returns (wd, dense, names, steps)."""
    wd = tmp_path / "wd"; (wd / "sparse" / "0").mkdir(parents=True)
    m = synthetic_model()
    cio.write_model(str(wd / "sparse" / "0"), m)
    dense.dense_steps("colmap", str(wd), {"quality": "Ultra high", "reuse": reuse}, "F:/photos")  # marker
    d = wd / "dense"
    cio.write_model(str(d / "sparse"), m)
    names = [im.name for _, im in sorted(m.images.items())]
    st = d / "stereo"; st.mkdir(exist_ok=True)
    (st / "fusion.cfg").write_text("\n".join(names) + "\n")
    with open(st / "patch-match.cfg", "w") as f:
        for i, n in enumerate(names):
            src = [names[j] for j in (i - 2, i - 1, i + 1, i + 2) if 0 <= j < len(names)]
            f.write(n + "\n" + ", ".join(src) + "\n")
    for i, n in enumerate(names):
        w, h = (400, 300) if m.images[i + 1].camera_id == 2 else (600, 450)
        for kind in kinds:
            write_map(str(st / "depth_maps" / f"{n}.{kind}.bin"), w, h, 1)
            write_map(str(st / "normal_maps" / f"{n}.{kind}.bin"), w, h, 3)
    steps = dense.dense_steps("colmap", str(wd), {"quality": "Ultra high", "reuse": reuse}, "F:/photos",
                              disk_free=10 ** 13)
    return wd, d, names, steps


def gates(steps):
    return [a for l, a, _ in steps if l == "gate"]


# ------------------------------------------------------------ map validation
def test_map_problems(tmp_path):
    p = str(tmp_path / "a.bin")
    write_map(p, 6, 4, 1)
    assert dense.map_problem(p, 1) is None
    assert dense.map_problem(p, 3) .startswith("bad header")             # depth map where a normal map belongs
    assert dense.map_problem(p, 1, expect=(5, 4)).startswith("size 6x4 larger")
    open(p, "r+b").truncate(30)
    assert dense.map_problem(p, 1).startswith("truncated")
    open(p, "wb").close()
    assert dense.map_problem(p, 1) == "empty file (0 bytes)"              # the user's 'width_ > 0' crash
    open(p, "wb").write(b"\x00garbage")
    assert dense.map_problem(p, 1) == "no valid header"
    assert dense.map_problem(str(tmp_path / "nope.bin"), 1) == "missing"


def test_depth_maps_complete_needs_valid_depth_and_normal_maps(tmp_path):
    wd, d, names, _ = make_workspace(tmp_path)
    assert dense.depth_maps_complete(str(d))
    os.remove(d / "stereo" / "normal_maps" / f"{names[2]}.geometric.bin")
    assert not dense.depth_maps_complete(str(d))
    assert dense.depth_map_counts(str(d)) == (8, 0, 7)


# ------------------------------------------------------------ resume / repair
def test_rerun_recomputes_only_broken_views(tmp_path):
    """User's case: patch match finished, photometric maps cleaned up, but one
    geometric depth map is an empty file and one normal map is cut off."""
    wd, d, names, steps = make_workspace(tmp_path)
    st = d / "stereo"
    open(st / "depth_maps" / f"{names[3]}.geometric.bin", "wb").close()
    nm = st / "normal_maps" / f"{names[5]}.geometric.bin"; open(nm, "r+b").truncate(1000)
    maybe_pm = gates(steps)[0]
    out = maybe_pm()
    logs = " | ".join(a for l, a, _ in out if l == "log")
    assert "2 unusable geometric map(s) removed" in logs and names[3] in logs and "empty file" in logs
    assert "truncated" in logs and "Reusing 6 valid depth maps; recomputing 2 view(s)" in logs
    assert not (st / "depth_maps" / f"{names[3]}.geometric.bin").exists() and not nm.exists()
    # restricted patch-match.cfg: the 2 views + their sources; full copy kept
    refs = [l for i, l in enumerate((st / "patch-match.cfg").read_text().splitlines()) if i % 2 == 0]
    assert set(refs) == {names[i] for i in (1, 2, 3, 4, 5, 6, 7)} and len(refs) < len(names)
    assert (st / "patch-match.full.cfg").exists()
    pm = [a for l, a, _ in out if l == "Building depth maps"][0]
    assert pm[0] == "patch_match_stereo"
    assert pm.meter.todo_p == len(refs) and pm.meter.todo_g == 2      # all photometric gone, 2 geometric
    after = [a for l, a, _ in out if l == "Cleaning up depth maps"][0]
    after()
    assert not (st / "patch-match.full.cfg").exists()
    assert len((st / "patch-match.cfg").read_text().splitlines()) == 2 * len(names)


def test_all_valid_maps_skip_patch_match(tmp_path):
    wd, d, names, steps = make_workspace(tmp_path)
    out = gates(steps)[0]()
    assert [a for l, a, _ in out if l == "log"] == [f"Reusing all {len(names)} existing depth maps"]


def test_unfinished_run_resumes_even_without_reuse_checked(tmp_path):
    wd, d, names, steps = make_workspace(tmp_path, reuse=False)
    assert any("did not finish: reusing its depth maps" in str(a) for l, a, _ in steps if l == "log")
    assert dense.depth_maps_complete(str(d))
    assert "Reusing all" in str(gates(steps)[0]())


def test_not_enough_disk_space_fails_before_patch_match(tmp_path):
    wd, d, names, _ = make_workspace(tmp_path, kinds=())
    steps = dense.dense_steps("colmap", str(wd), {"quality": "Ultra high", "reuse": True}, "F:/photos",
                              disk_free=3 * GB)
    need = dense.disk_needed(str(d), names, names)
    assert 0 < need < GB
    out = gates(steps)[0]()                                      # tiny maps + 2 GB margin: 3 GB is enough
    assert any(l == "Building depth maps" for l, _, _ in out)
    steps = dense.dense_steps("colmap", str(wd), {"quality": "Ultra high", "reuse": True}, "F:/photos",
                              disk_free=10)
    with pytest.raises(RuntimeError, match="Not enough disk space"):
        gates(steps)[0]()


def test_disk_estimate_ultra():
    """640 DJI views at 5280x3956: both passes need ~400 GB."""
    px = 5280 * 3956
    assert 380 * GB < 16 * 2 * 640 * px < 450 * GB


# ------------------------------------------------------------ fusion
def test_fusion_skips_views_without_valid_maps_and_plans_memory(tmp_path):
    wd, d, names, steps = make_workspace(tmp_path)
    open(d / "stereo" / "depth_maps" / f"{names[7]}.geometric.bin", "wb").close()
    out = gates(steps)[1]()
    logs = [a for l, a, _ in out if l == "log"]
    assert any("Fusion skips 1 view(s)" in x and names[7] in x for x in logs)
    assert names[7] not in (d / "stereo" / "fusion.cfg").read_text()
    assert any(x.startswith("Fusion plan: 7 depth maps") for x in logs)
    fu = [a for l, a, _ in out if l == "Building point cloud (fusion)"][0]
    for k in ("--StereoFusion.use_cache", "--StereoFusion.cache_size", "--StereoFusion.max_image_size",
              "--StereoFusion.num_threads", "--StereoFusion.min_num_pixels"):
        assert k in fu
    assert fu.meter is not None and fu.fallback is not None


def test_fusion_retries_with_less_memory_then_explains(tmp_path, monkeypatch):
    wd, d, names, steps = make_workspace(tmp_path)
    tp = 600 * 5280 * 3956 + 40 * 4032 * 3024                  # pretend: the user's Ultra workspace
    monkeypatch.setattr(dense, "depth_map_size", lambda _d: (640, 5280, 3956, tp))
    from photomesh import memguard
    monkeypatch.setattr(memguard, "available_bytes", lambda: 55 * GB)
    fu_steps = gates(steps)[1]()
    seen, msg = [], None
    for _ in range(10):
        fu = [a for l, a, _ in fu_steps if l == "Building point cloud (fusion)"][0]
        seen.append((fu[fu.index("--StereoFusion.use_cache") + 1], fu[fu.index("--StereoFusion.max_image_size") + 1]))
        (d / "fused.ply").write_bytes(b"partial")
        fu.exit_code, fu.error, fu.output_tail = -1073740791, "exit code -1073740791", ["std::bad_alloc"]
        try:
            nxt = fu.fallback()
        except RuntimeError as e:
            msg = str(e); break
        assert "retrying with less memory" in nxt[0][1]
        fu_steps = nxt[1][1]()
        assert not (d / "fused.ply").exists()                 # partial output removed
    assert seen[0][0] == "0" and seen[1] == ("1", seen[0][1])   # 1st retry: same size, cached workspace
    sizes = [int(m) for _, m in seen]
    assert all(c == "1" for c, _ in seen[1:]) and sizes == sorted(sizes, reverse=True) and sizes[-1] < sizes[0]
    assert 3 <= len(seen) <= dense.FUSION_RETRIES + 1
    assert "0xC0000409" in msg and "Reuse depth maps" in msg


def test_fusion_ordinary_error_is_not_retried(tmp_path):
    wd, d, names, steps = make_workspace(tmp_path)
    fu = [a for l, a, _ in gates(steps)[1]() if l == "Building point cloud (fusion)"][0]
    fu.exit_code, fu.output_tail = 1, ["ERROR: invalid option"]
    with pytest.raises(RuntimeError, match="depth maps are kept"):
        fu.fallback()


def test_plan_fusion_real_sizes():
    tp = 600 * 5280 * 3956 + 40 * 4032 * 3024
    # Ultra on the 64 GB laptop: never COLMAP's default (all full-res maps in RAM)
    p = dense.plan_fusion(640, 5280, 3956, 55 * GB, threads=32, total_px=tp)
    assert p["peak"] <= p["budget"] < 55 * GB and p["max_image_size"] > 0
    assert p["seconds"] <= dense.MAX_FUSION_S and p["points"] * dense.DOWNSTREAM_POINT_BYTES <= p["budget"]
    default = tp * dense.FUSION_WS_BYTES_PER_PX                 # what use_cache=0 at full size would need
    assert default > 200 * GB
    # Medium on the 32 GB PC with 23.7 GB free: same as the run that worked (full size, all threads)
    p = dense.plan_fusion(640, 1320, 989, 23.7 * GB, threads=16, total_px=600 * 1320 * 989 + 40 * 1008 * 756)
    assert p["max_image_size"] == -1 and p["use_cache"] == 0 and p["num_threads"] == 16
    # little RAM: cached single-thread workspace, still within the budget
    p = dense.plan_fusion(640, 1320, 989, 10 * GB, threads=16)
    assert p["use_cache"] == 1 and p["num_threads"] == 1 and p["peak"] <= p["budget"]


def test_exit_code_text():
    assert "0xC0000409" in dense.exit_code_text(-1073740791)
    assert "0xC0000409" in dense.exit_code_text(3221226505)
    assert dense.exit_code_text(1) == "1"
    assert dense.looks_out_of_memory(1, ["terminate called after throwing an instance of 'std::bad_alloc'"])


# ------------------------------------------------------------ progress meters
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_patch_match_meter_two_passes_and_eta():
    c = Clock()
    m = PatchMatchMeter(4, 4, clock=c)
    out = []
    for i in range(8):
        c.t = 10.0 * i
        out.append(m.feed(f"Processing view {i % 4 + 1} / 4 for img{i % 4}.jpg"))
    assert m.feed("Reading inputs...") is None
    fr = [o[0] for o in out]
    assert fr == sorted(fr) and fr[0] == 0 and fr[-1] < 1
    assert "photometric pass 1/4" in out[0][1] and "geometric pass 1/4" in out[4][1]
    assert "about 40 s left" in out[4][1]                        # 10 s per view, 4 to go
    assert fmt_eta(3725) == "1 h 02 min" and fmt_eta(59) == "59 s"


def test_fusion_meter():
    m = FusionMeter(clock=Clock())
    assert m.feed("Reading workspace...")[1].startswith("Fusion: loading")
    f, t = m.feed("Fusing image [2/10] with index 3")
    assert abs(f - 0.1) < 1e-9 and "image 2/10" in t
    m.feed(" in 0.046s (28637607 points)")
    assert "28.6 M points" in m.feed("Fusing image [3/10] with index 4")[1]


def test_runner_reports_depth_map_detail(qapp, qtbot):
    from photomesh.colmap import PipelineRunner, Cmd
    script = ("import sys\n"
              "for p in range(2):\n"
              "    for i in range(3): print(f'Processing view {i+1} / 3 for v{i}.jpg', flush=True)\n")
    steps = [("Building depth maps", Cmd(["-c", script], meter=PatchMatchMeter(3, 3)), 70),
             ("Building point cloud (fusion)", Cmd(["-c", "print('Fusing image [1/2] with index 0')"],
                                                    meter=FusionMeter()), 15)]
    r = PipelineRunner(sys.executable, steps)
    details, prog = [], []
    r.detail.connect(lambda p, t: details.append((p, t)))
    r.progress.connect(lambda p, l: prog.append((p, l)))
    with qtbot.waitSignal(r.finished, timeout=20000) as blocker:
        r.start()
    assert blocker.args[0] is True
    dm = [d for d in details if d[1].startswith("Depth maps")]
    assert len(dm) == 6 and [d[0] for d in dm] == sorted(d[0] for d in dm)
    assert "geometric pass 3/3" in dm[-1][1] and dm[-1][0] < 100 * 70 / 85
    assert any(t.startswith("Fusion: image 1/2") for _, t in details)
    assert {l for _, l in prog} >= {"Building depth maps", "Building point cloud (fusion)"}


def test_runner_failed_fallback_gives_clear_message(qapp, qtbot):
    from photomesh.colmap import PipelineRunner, Try
    t = Try(["-c", "import sys; print('std::bad_alloc'); sys.exit(3)"])

    def fb():
        assert t.exit_code == 3 and "std::bad_alloc" in t.output_tail
        raise RuntimeError("explained")
    t.fallback = fb
    r = PipelineRunner(sys.executable, [("Building point cloud (fusion)", t, 15)])
    with qtbot.waitSignal(r.finished, timeout=20000) as blocker:
        r.start()
    assert blocker.args == [False, "'Building point cloud (fusion)' failed: explained"]


def test_progress_dialog_detail_and_monotone(qapp):
    from photomesh.dialogs import ProgressDialog
    dlg = ProgressDialog("Build Point Cloud")
    dlg.set_progress(10, "Building depth maps")
    dlg.set_detail(12.34, "Depth maps: photometric pass 5/640 - about 2 h 01 min left")
    assert dlg.bar.value() == 123 and "photometric pass 5/640" in dlg.stage.text() and "12.3%" in dlg.stage.text()
    dlg.set_progress(11, "Building depth maps")                  # int update must not move it back
    assert dlg.bar.value() == 123
    dlg.set_progress(80, "Building point cloud (fusion)")
    assert dlg.bar.value() == 800 and "photometric" not in dlg.stage.text()
