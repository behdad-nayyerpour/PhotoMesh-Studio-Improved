"""0.1.11: issue #3 - rebuilding the orthomosaic of a georeferenced project failed
with "Orthomosaic would be 197054x189531 px (37.3 Gpx)" and an unhelpful
"exit 0x00000001 (generic failure)".

Root cause: the texture (built before Optimize Cameras) kept its photo GSD in
the old model units; after georeferencing, Build Orthomosaic took it as the
default pixel size (about 10x too fine), and the size was only checked after
the task had started. Also covered: the extent ignores stray geometry outside
the photos, a failed / cancelled rebuild keeps showing the previous result, and
the task's own message reaches the error dialog and the report title."""
import json
import os
import shutil
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texsynth as ts                                          # noqa: E402
from photomesh import ortho                                    # noqa: E402

QUIET = lambda m: None                                         # noqa: E731
OFF = [26700.0, 23000.0, 1300.0]
GEO = {"offset": OFF, "crs": 'LOCAL_CS["Local Coordinates (m)"]'}


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    return ts.make_scene(str(tmp_path_factory.mktemp("orthoscene0111")))


def _with_outlier(scene, path):
    """The scene mesh plus one stray triangle 400 m away (a meshing outlier)."""
    from photomesh.plyio import save_ply
    V, F = scene["V"], scene["F"]
    n = len(V)
    V2 = np.vstack([V, [[400.0, 380.0, 0.0], [401.0, 380.0, 0.0], [400.0, 381.0, 0.0]]])
    F2 = np.vstack([F, [[n, n + 1, n + 2]]])
    save_ply(path, V2.astype(np.float32), None, F2)
    return path


# ------------------------------------------------------------ task
def test_extent_ignores_geometry_outside_the_photos(scene, tmp_path):
    mesh = _with_outlier(scene, str(tmp_path / "outlier.ply"))
    logs = []
    info = ortho.build_orthomosaic(dict(mesh=mesh, model=scene["model"], photos=scene["photos"],
                                        out_dir=str(tmp_path / "o"), georef=GEO, pixel_size=0.05, block=512),
                                   log=logs.append)
    # the scene is 20 x 20 m: without the fix the raster spans ~401 x 381 m (8000 x 7600 px)
    assert info["width"] < 1000 and info["height"] < 1000, (info["width"], info["height"])
    assert info["width"] >= 390 and info["height"] >= 390
    assert any("extent limited to the area covered by the photos" in l for l in logs), logs
    # the region still wins
    r = ortho.build_orthomosaic(dict(mesh=mesh, model=scene["model"], photos=scene["photos"],
                                     out_dir=str(tmp_path / "r"), georef=GEO, pixel_size=0.05, block=512,
                                     region=[OFF[0] + 5, OFF[1] + 5, OFF[0] + 10, OFF[1] + 10]), log=QUIET)
    assert 95 <= r["width"] <= 102 and 95 <= r["height"] <= 102


def test_plan_matches_the_task(scene, tmp_path):
    mesh = _with_outlier(scene, str(tmp_path / "outlier.ply"))
    p = ortho.plan_orthomosaic(scene["model"], mesh, GEO)
    assert p["clipped"]
    _px, W, H = ortho.raster_size(*p["bounds"], 0.05, 0, OFF[:2])
    info = ortho.build_orthomosaic(dict(mesh=mesh, model=scene["model"], photos=scene["photos"],
                                        out_dir=str(tmp_path / "o"), georef=GEO, pixel_size=0.05, block=512),
                                   log=QUIET)
    # the dialog's read-out is an estimate (median height instead of ray casting): within 15 %
    assert abs(W - info["width"]) <= 0.15 * info["width"] and abs(H - info["height"]) <= 0.15 * info["height"]


def test_too_large_message_is_clear_and_suggests_a_pixel_size(scene, tmp_path):
    with pytest.raises(RuntimeError) as e:
        ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                     out_dir=str(tmp_path / "o"), georef=GEO, pixel_size=0.002,
                                     max_pixels=20_000_000), log=QUIET)
    msg = str(e.value)
    assert "finer than the photos" in msg and "Use a pixel size of" in msg, msg
    sug = float(msg.split("Use a pixel size of ")[1].split()[0])
    _px, W, H = ortho.raster_size(0, 0, 20, 20, sug)
    assert W * H <= 20_000_000 and sug >= 0.004
    assert ortho.suggest_pixel_size(0, 0, 414, 398, 3e9) == pytest.approx(0.0074, abs=0.0011)


def test_default_pixel_size_ignores_a_texture_gsd_from_the_old_frame(scene, tmp_path):
    true = ortho.estimate_gsd(scene["model"], scene["mesh"], GEO)
    assert 0.03 < true < 0.09
    assert ortho.estimate_gsd(scene["model"], scene["mesh"], GEO, {"source_gsd": true * 1.2}) == \
        pytest.approx(ortho.nice(true * 1.2))
    # texture built before georeferencing: 10x smaller units -> not used (issue #3)
    assert ortho.estimate_gsd(scene["model"], scene["mesh"], GEO, {"source_gsd": true / 10}) == true


def test_georeferencing_rescales_the_texture_and_marks_rasters_outdated(tmp_path):
    from photomesh.project import Chunk
    from photomesh.reference_ui import products_after_georef, GEOREF_STALE
    d = tmp_path / "tex"; d.mkdir()
    (d / "info.json").write_text(json.dumps({"source_gsd": 0.0021, "texel_size": 0.003, "pages": []}))
    ch = Chunk(name="c")
    ch.texture = {"dir": str(d), "source_gsd": 0.0021, "texel_size": 0.003}
    ch.orthomosaic = {"format": "photomesh-ortho/1", "width": 10}
    ch.dem = {"format": "imported", "width": 10}
    M = np.eye(4); M[:3, :3] *= 10.0; M[:3, 3] = [5, 6, 7]
    assert products_after_georef(ch, M.tolist(), QUIET) == pytest.approx(10.0)
    assert ch.texture["source_gsd"] == pytest.approx(0.021) and ch.texture["texel_size"] == pytest.approx(0.03)
    disk = json.loads((d / "info.json").read_text())
    assert disk["source_gsd"] == pytest.approx(0.021) and disk["texel_size"] == pytest.approx(0.03)
    assert ch.orthomosaic["stale"] == GEOREF_STALE and "stale" not in ch.dem


# ------------------------------------------------------------ error surfacing
def test_task_message_replaces_generic_failure():
    from photomesh.report import error_reason, issue_title
    tail = ["Ortho: surface = mesh 10 vertices", "Traceback (most recent call last):",
            '  File "x.py", line 1, in <module>',
            "RuntimeError: Orthomosaic would be 197054x189531 px (37.3 Gpx) - increase the pixel size"]
    assert error_reason(tail).startswith("Orthomosaic would be 197054x189531 px")
    assert error_reason(["OSError: [Errno 13] Access is denied"]) == "OSError: [Errno 13] Access is denied"
    assert error_reason(["all good", "[3/3]"]) == ""
    r = {"title": "Build Orthomosaic failed", "version": "0.1.11",
         "message": "'Building orthomosaic' failed: Orthomosaic would be 10 x 10 px",
         "exit": {"hex": "0x00000001", "meaning": "generic failure; see the log lines above"}}
    t = issue_title(r)
    assert "generic failure" not in t and "Orthomosaic would be 10 x 10 px" in t


# ------------------------------------------------------------ GUI: build, then rebuild
def _setup(window, scene, tmp_path):
    from photomesh.plyio import ply_counts
    ch = window.chunk
    ch.add_photos(sorted(scene["photos"].values()))
    wd = str(tmp_path / "work"); ch.workdir = wd
    shutil.copytree(scene["model"], os.path.join(wd, "sparse", "0"))
    shutil.copy(scene["mesh"], os.path.join(wd, "model.ply"))
    ch.model_ply = os.path.join(wd, "model.ply"); ch.face_count = ply_counts(ch.model_ply)[1]
    ch.tie_points_ply = scene["mesh"]
    ch.georef = dict(GEO, crs_kind="local")
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


def test_rebuild_after_georeferencing_with_old_texture(window, no_dialogs, monkeypatch, scene, tmp_path):
    """The reported sequence: orthomosaic + texture exist, the project is (re)georeferenced,
    Build Orthomosaic again with the dialog defaults."""
    window.project.path = str(tmp_path / "proj.pmsx")
    from photomesh import dialogs
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "exec", lambda self: 1)
    ch = _setup(window, scene, tmp_path)
    true = ortho.estimate_gsd(window._model_dir(ch), scene["mesh"], GEO)
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    first = dict(ch.orthomosaic)
    assert first.get("width") and first["pixel_size"] == pytest.approx(true), window.chunk.log
    # a texture measured in the pre-georeferencing units (10x smaller)
    ch.texture = {"source_gsd": true / 10, "texel_size": true / 10}
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    o = ch.orthomosaic
    assert o["pixel_size"] == pytest.approx(true) and o["width"] == first["width"], window.chunk.log
    assert window.ortho_view.has_raster("ortho") and no_dialogs == []


def test_too_large_is_caught_before_the_build_starts(window, no_dialogs, monkeypatch, scene, tmp_path):
    window.project.path = str(tmp_path / "proj.pmsx")
    from photomesh import dialogs, choice
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "exec", lambda self: 1)
    s0 = dialogs.BuildOrthomosaicDialog.settings
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "settings", lambda self: dict(s0(self), pixel_size=0.0002))
    asked = []
    monkeypatch.setattr(choice, "ask_choice", lambda *a, **k: asked.append(a[2]) or k.get("default"))
    ch = _setup(window, scene, tmp_path)
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    assert asked and "Use a pixel size of" in asked[0], asked
    o = ch.orthomosaic
    assert o.get("width") and o["pixel_size"] > 0.0002 and o["width"] * o["height"] < 3e9, window.chunk.log
    # Cancel in the question: nothing starts, the result stays
    monkeypatch.setattr(choice, "ask_choice", lambda *a, **k: "cancel")
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    assert ch.orthomosaic == o and no_dialogs == []


def test_dialog_shows_total_size(qtbot, scene):
    from photomesh.dialogs import BuildOrthomosaicDialog
    plan = ortho.plan_orthomosaic(scene["model"], scene["mesh"], GEO)
    dlg = BuildOrthomosaicDialog(None, ("Mesh",), gsd=0.05, plan=plan)
    qtbot.addWidget(dlg)
    W, H, px = dlg.estimated_size()
    assert px == 0.05 and 390 <= W <= 640 and "Mpx" in dlg.size_label.text()
    dlg.pixel.setValue(0.0001)
    assert "too large" in dlg.size_label.text()
    dlg.r_max.setChecked(True); dlg.maxdim.setValue(4096)
    W, H, _ = dlg.estimated_size()
    assert max(W, H) <= 4097 and "too large" not in dlg.size_label.text()


def test_failed_rebuild_keeps_and_shows_the_previous_orthomosaic(window, no_dialogs, monkeypatch, scene, tmp_path):
    window.project.path = str(tmp_path / "proj.pmsx")
    from photomesh import dialogs
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "exec", lambda self: 1)
    s0 = dialogs.BuildOrthomosaicDialog.settings
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "settings", lambda self: dict(s0(self), pixel_size=0.05))
    ch = _setup(window, scene, tmp_path)
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    o = dict(ch.orthomosaic)
    assert o.get("width"), window.chunk.log
    far = [OFF[0] + 900, OFF[1] + 900, OFF[0] + 950, OFF[1] + 950]
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "settings",
                        lambda self: dict(s0(self), pixel_size=0.05, region=far))
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    assert no_dialogs and "does not overlap" in no_dialogs[-1][1] and "generic" not in no_dialogs[-1][1], no_dialogs
    assert ch.orthomosaic == o and os.path.exists(o["path"])
    assert window.ortho_view.has_raster("ortho")
