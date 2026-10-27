"""0.1.9: depth maps not kept by default, Save As 'results only', clean-up
tool, photo copies re-linked, synced-folder warning, unfinished copies."""
import json
import os
import shutil

import pytest

from test_release_017 import _processed

PLY = b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nend_header\n" + b"\0" * 24


def _add_depth(wd, n=3, size=4000):
    for sub in ("depth_maps", "normal_maps", "consistency_graphs"):
        d = os.path.join(wd, "dense", "stereo", sub); os.makedirs(d, exist_ok=True)
        for k in range(n):
            open(os.path.join(d, f"IMG_{k}.JPG.geometric.bin"), "wb").write(b"d" * size)
    return 3 * n * size


# ------------------------------------------------------------ 2: Keep depth maps
def test_ply_vertex_count(tmp_path):
    from photomesh.dense import ply_vertex_count
    p = tmp_path / "a.ply"
    p.write_bytes(PLY); assert ply_vertex_count(str(p)) == 2
    p.write_bytes(PLY.split(b"end_header")[0]); assert ply_vertex_count(str(p)) == 0       # truncated header
    p.write_bytes(PLY.replace(b"vertex 2", b"vertex 0")); assert ply_vertex_count(str(p)) == 0
    p.write_bytes(PLY[:PLY.index(b"end_header") + 11]); assert ply_vertex_count(str(p)) == 0  # no body
    assert ply_vertex_count(str(tmp_path / "missing.ply")) == 0


def test_depth_maps_removed_only_after_verified_cloud(tmp_path):
    from photomesh import dense
    wd = str(tmp_path / "wd"); size = _add_depth(wd)
    d = os.path.join(wd, "dense")
    assert dense.remove_depth_maps(d) == (0, 0)                             # no point cloud: kept
    open(os.path.join(d, "point_cloud.ply"), "wb").write(b"ply\nbroken")
    assert dense.remove_depth_maps(d) == (0, 0) and dense.depth_maps_size(d)[1] == 9
    open(os.path.join(d, "point_cloud.ply"), "wb").write(PLY)
    assert dense.remove_depth_maps(d) == (9, size)
    for sub in dense.DEPTH_DIRS:                                             # layout stays, empty
        assert os.listdir(os.path.join(d, "stereo", sub)) == []
    info = json.load(open(os.path.join(d, dense.DEPTH_INFO)))
    assert info["removed"] and info["maps"] == 3 and info["bytes"] == size


def _steps(tmp_path, **s):
    from photomesh import dense
    wd = str(tmp_path / "wd"); os.makedirs(os.path.join(wd, "sparse", "0"), exist_ok=True)
    st = dense.dense_steps("colmap", wd, dict({"quality": "Medium"}, **s), os.path.join(wd, "images"))
    return wd, st


def test_build_point_cloud_drops_depth_maps_by_default(tmp_path):
    wd, st = _steps(tmp_path)
    assert st[-1][0] == "Removing depth maps"
    _add_depth(wd); open(os.path.join(wd, "dense", "point_cloud.ply"), "wb").write(PLY)
    out = st[-1][1]()
    assert "Removed the depth maps" in out[0][1]
    from photomesh.dense import depth_maps_size
    assert depth_maps_size(os.path.join(wd, "dense"))[1] == 0
    wd2, st2 = _steps(tmp_path / "k", keep_depth_maps=True)
    assert all(lbl != "Removing depth maps" for lbl, *_ in st2)


def test_dialog_keep_depth_maps_default_off(qtbot):
    from photomesh.dialogs import BuildPointCloudDialog
    d = BuildPointCloudDialog(); qtbot.addWidget(d)
    s = d.settings()
    assert s["keep_depth_maps"] is False and s["reuse"] is False
    d.keep_depth.setChecked(True); d.reuse.setChecked(True)
    assert d.settings()["keep_depth_maps"] and d.settings()["reuse"]


def test_reuse_with_kept_depth_maps_still_reuses(tmp_path):
    """Reuse depth maps (kept): existing valid maps are not recomputed."""
    from photomesh import dense
    wd, st = _steps(tmp_path, reuse=True, keep_depth_maps=True)
    assert [lbl for lbl, *_ in st][:3] == ["Undistorting photos (parallel, downscaled)", "gate", "gate"]


# ------------------------------------------------------------ 3 + 5: Save As
def test_save_as_results_only_skips_depth_maps(tmp_path):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    depth = _add_depth(prj.chunk.workdir)
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))
    assert items[0]["parts"]["depth"] == depth and items[0]["bytes"] >= depth
    items[0]["skip_depth"] = True
    assert dfo.item_bytes(items[0]) == items[0]["bytes"] - depth
    done = dfo.apply(dfo.execute(items))
    nw = prj.chunk.workdir
    for sub in ("depth_maps", "normal_maps", "consistency_graphs"):
        assert os.listdir(os.path.join(nw, "dense", "stereo", sub)) == []
    assert os.path.exists(os.path.join(nw, "dense", "point_cloud.ply"))
    assert json.load(open(os.path.join(nw, "dense", "depth_maps_info.json")))["removed"]
    assert done


def test_photo_copies_are_relinked_to_originals(tmp_path):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    wd = prj.chunk.workdir
    img = os.path.join(wd, "images", "IMG_0.JPG")
    os.remove(img); shutil.copy2(prj.chunk.cameras[0].path, img)            # a plain copy (cross-drive case)
    assert os.stat(img).st_nlink == 1
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))
    assert items[0]["parts"]["photo_copies"] >= os.path.getsize(img)
    dfo.apply(dfo.execute(items))
    new = os.path.join(prj.chunk.workdir, "images", "IMG_0.JPG")
    assert open(new, "rb").read() == open(prj.chunk.cameras[0].path, "rb").read()  # valid if links are blocked


def test_gui_save_as_offers_results_only_by_default(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from photomesh import choice
    prj = _processed(tmp_path / "a")
    _add_depth(prj.chunk.workdir, size=100000)
    window.open_project(prj.path)
    new = tmp_path / "b" / "n.pmsx"; new.parent.mkdir()
    asked = []
    monkeypatch.setattr(choice, "ask_choice", lambda *a, **k: asked.append((a, k)) or k.get("default"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    assert window.save_project_as()
    (a, k), = asked
    keys = [key for key, _l in a[3]]
    assert k["default"] == "results" and keys == ["results", "all", "keep", "cancel"]
    assert "Depth maps: 878.9 KB" in a[2] and "can be recomputed" in a[2]
    assert os.listdir(os.path.join(window.chunk.workdir, "dense", "stereo", "depth_maps")) == []
    assert os.listdir(os.path.join(prj.chunk.workdir, "dense", "stereo", "depth_maps"))      # original kept
    # "Copy everything"
    other = tmp_path / "c" / "m.pmsx"; other.parent.mkdir()
    window.open_project(prj.path)
    monkeypatch.setattr(choice, "ask_choice", lambda *a, **k: "all")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(other), "")))
    assert window.save_project_as()
    assert len(os.listdir(os.path.join(window.chunk.workdir, "dense", "stereo", "depth_maps"))) == 3


def test_ask_choice_returns_clicked_key(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from photomesh import choice
    import importlib; importlib.reload(choice)                              # the real one (not the fixture's)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: self.buttons()[1].click() or 0)
    r = choice.ask_choice(None, "T", "x", [("a", "A"), ("b", "B"), ("cancel", "Cancel")], default="a")
    assert r in ("a", "b") and r != "cancel"


# ------------------------------------------------------------ 7: unfinished copies
def test_stale_copies_detected_safely(tmp_path):
    from photomesh import datafolder as dfo
    root = tmp_path / "Test-Lot3-Drone.files"
    p = root / "chunk0.partial"; (p / "dense").mkdir(parents=True); (p / "dem").mkdir()
    (p / "dense" / "x.bin").write_bytes(b"1" * 500)
    (root / "chunk1.partial").mkdir()                                      # empty, not workdir-like: no
    mine = root / "notes.partial"; (mine / "dense").mkdir(parents=True)      # not our name pattern: no
    inc = root / "chunk2"; inc.mkdir(); (inc / dfo.INCOMPLETE_MARK).write_text("x")
    prj = root / "chunk3.partial"; (prj / "dense").mkdir(parents=True); (prj / "a.pmsx").write_text("{}")
    got = dfo.stale_copies(str(root))
    assert [os.path.basename(x) for x, _b in got] == ["chunk0.partial", "chunk2"]
    assert dict(got)[str(p)] == 500
    assert dfo.stale_copies(str(tmp_path)) == []                           # only inside a <name>.files folder


def test_gui_offers_to_remove_stale_partial(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    prj = _processed(tmp_path / "a")
    window.open_project(prj.path)
    new = tmp_path / "b" / "Test-Lot3-Drone.pmsx"
    stale = tmp_path / "b" / "Test-Lot3-Drone.files" / "chunk0.partial"
    (stale / "dense").mkdir(parents=True); (stale / "dense" / "big.bin").write_bytes(b"0" * 9000)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    assert window.save_project_as()
    assert any("unfinished copy" in q and "chunk0.partial" in q for q in asked)
    assert not stale.exists() and os.path.isdir(window.chunk.workdir)
    assert window.chunk.workdir.endswith("chunk0")


def test_declined_stale_is_kept(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    prj = _processed(tmp_path / "a")
    window.open_project(prj.path)
    new = tmp_path / "b" / "x.pmsx"
    stale = tmp_path / "b" / "x.files" / "chunk0.partial"; (stale / "sparse").mkdir(parents=True)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    assert window.save_project_as() and stale.exists()


# ------------------------------------------------------------ 6: synced folders
def test_synced_folder_detection(tmp_path, monkeypatch):
    from photomesh import datafolder as dfo
    od = tmp_path / "OneDrive - AFRY"
    monkeypatch.setenv("OneDriveCommercial", str(od))
    assert dfo.synced_folder(str(od / "Backup Drawings" / "p.pmsx")) == "OneDrive"
    monkeypatch.delenv("OneDriveCommercial")
    monkeypatch.delenv("OneDrive", raising=False); monkeypatch.delenv("OneDriveConsumer", raising=False)
    assert dfo.synced_folder(str(od / "x" / "p.pmsx")) == "OneDrive"              # by folder name
    app = tmp_path / "appdata"; (app / "Dropbox").mkdir(parents=True)
    (app / "Dropbox" / "info.json").write_text(json.dumps({"business": {"path": str(tmp_path / "Work Box")}}))
    monkeypatch.setenv("APPDATA", str(app))
    assert dfo.synced_folder(str(tmp_path / "Work Box" / "p.pmsx")) == "Dropbox"
    assert dfo.synced_folder(str(tmp_path / "G" / "My Drive" / "p.pmsx")) == "Google Drive"
    assert dfo.synced_folder(str(tmp_path / "PhotoMesh" / "Projects" / "p.pmsx")) is None


def test_gui_warns_for_synced_folder(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    new = tmp_path / "OneDrive - AFRY" / "p.pmsx"; new.parent.mkdir()
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.No))
    assert window.save_project_as() is False and not new.exists() and "OneDrive" in asked[0]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes))
    assert window.save_project_as() and new.exists()
    n = len(asked)
    assert window.save_project_as() and len(asked) == n                   # warned once per folder


# ------------------------------------------------------------ 4: clean up
def test_cleanup_candidates_and_protection(tmp_path):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    wd = prj.chunk.workdir
    depth = _add_depth(wd)
    os.makedirs(os.path.join(wd, "dense", "images")); open(os.path.join(wd, "dense", "images", "u.jpg"), "wb").write(b"u" * 300)
    open(os.path.join(wd, "database.db"), "wb").write(b"q" * 200)
    stale = os.path.join(dfo.data_folder(prj.path), "chunk0.partial"); os.makedirs(os.path.join(stale, "dense"))
    open(os.path.join(stale, "dense", "z"), "wb").write(b"z" * 50)
    parts = {p["key"]: p for p in dfo.cleanup_candidates(prj)}
    assert set(parts) == {"depth", "undistorted", "database", "stale"}
    assert parts["depth"]["bytes"] == depth and parts["depth"]["checked"] and parts["stale"]["checked"]
    assert not parts["undistorted"]["checked"] and not parts["database"]["checked"]
    # a product inside a part: that part is not offered / not deleted
    prj.chunk.orthomosaic = {"path": os.path.join(wd, "dense", "images", "u.jpg")}
    assert "undistorted" not in {p["key"] for p in dfo.cleanup_candidates(prj)}
    freed, left = dfo.delete_parts([parts["undistorted"], parts["depth"], parts["stale"]], prj)
    assert left == [os.path.join(wd, "dense", "images")] and freed == depth + 50
    assert os.path.exists(os.path.join(wd, "dense", "images", "u.jpg"))
    assert os.path.isdir(os.path.join(wd, "dense", "stereo", "depth_maps")) and not os.path.exists(stale)
    assert os.path.exists(os.path.join(wd, "dense", "point_cloud.ply"))
    assert all(os.path.exists(c.path) for c in prj.chunk.cameras)


def test_gui_cleanup(window, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from photomesh import cleanup_ui
    prj = _processed(tmp_path / "a")
    depth = _add_depth(prj.chunk.workdir)
    window.open_project(prj.path)
    seen = []
    monkeypatch.setattr(cleanup_ui.CleanupDialog, "exec", lambda self: seen.append(self.total.text()) or 1)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    assert window.clean_up_data() is None and len(os.listdir(os.path.join(window.chunk.workdir, "dense", "stereo", "depth_maps")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    assert window.clean_up_data() == depth
    assert seen and seen[0].startswith("Selected:") and no_dialogs == []
    assert os.listdir(os.path.join(window.chunk.workdir, "dense", "stereo", "depth_maps")) == []
    infos = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    assert window.clean_up_data() is None and "no intermediate data" in infos[0]


def test_cleanup_dialog_selection(qtbot):
    from PySide6.QtCore import Qt
    from photomesh.cleanup_ui import CleanupDialog
    parts = [{"key": "depth", "label": "Depth maps", "note": "n", "checked": True, "chunk": "Chunk 1",
              "paths": ["/x"], "bytes": 2 * 1024 ** 3},
             {"key": "database", "label": "DB", "note": "n", "checked": False, "chunk": "Chunk 1",
              "paths": ["/y"], "bytes": 1024}]
    d = CleanupDialog(parts); qtbot.addWidget(d)
    assert [p["key"] for p in d.selected()] == ["depth"] and "2.0 GB" in d.total.text()
    d.tree.topLevelItem(1).setCheckState(0, Qt.Checked)
    assert len(d.selected()) == 2
