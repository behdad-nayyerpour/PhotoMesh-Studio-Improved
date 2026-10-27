"""Moving projects/photos to another computer and running without an NVIDIA GPU."""
import os
import json
import shutil

from PIL import Image


def _img(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Image.new("RGB", (8, 8)).save(p)


def test_relink_matches_by_name_and_trailing_path(tmp_path):
    from photomesh.project import Chunk, Camera
    from photomesh.relink import relink_chunk, missing_cameras
    new = tmp_path / "E_drive" / "Drone"
    for d in ("DJI_001", "DJI_002"):
        _img(str(new / d / "DJI_0001.JPG"))
    _img(str(new / "DJI_001" / "DJI_0002.JPG"))
    ch = Chunk(cameras=[Camera(path=r"F:\Drone\DJI_001\DJI_0001.JPG"),
                        Camera(path=r"F:\Drone\DJI_002\DJI_0001.JPG"),
                        Camera(path=r"F:\Drone\DJI_001\DJI_0002.JPG"),
                        Camera(path=r"F:\Drone\DJI_001\DJI_0099.JPG")])
    assert len(missing_cameras(ch)) == 4
    labels = [c.label for c in ch.cameras]
    n, nf = relink_chunk(ch, str(new))
    assert n == 3 and len(nf) == 1 and nf[0].endswith("DJI_0099.JPG")
    assert ch.cameras[0].path == str(new / "DJI_001" / "DJI_0001.JPG")
    assert ch.cameras[1].path == str(new / "DJI_002" / "DJI_0001.JPG")      # same name, right folder
    assert [c.label for c in ch.cameras] == labels                           # labels unchanged
    assert missing_cameras(ch) == [3]


def test_moved_project_folder_rebases_products(tmp_path):
    from photomesh.project import Project, Chunk, Camera
    old = tmp_path / "old" / "bench"
    (old / "fast" / "dense").mkdir(parents=True)
    (old / "fast" / "dense" / "point_cloud.ply").write_bytes(b"ply\n")
    (old / "fast" / "model.ply").write_bytes(b"ply\n")
    p = Project()
    p.chunks = [Chunk(workdir=str(old / "fast"), point_cloud_ply=str(old / "fast" / "dense" / "point_cloud.ply"),
                      model_ply=str(old / "fast" / "model.ply"), cameras=[Camera(path="/nowhere/a.jpg")])]
    p.save(str(old / "p.pmsx"))
    new = tmp_path / "other_disk" / "bench"
    shutil.copytree(old, new); shutil.rmtree(tmp_path / "old")
    q = Project.load(str(new / "p.pmsx"))
    ch = q.chunks[0]
    assert ch.workdir == str(new / "fast")
    assert ch.point_cloud_ply == str(new / "fast" / "dense" / "point_cloud.ply")
    assert ch.model_ply == str(new / "fast" / "model.ply")
    assert q.relocated == []                 # 0.1.7: stored relative to the .pmsx, nothing to rebase
    raw = json.load(open(new / "p.pmsx", encoding="utf-8"))
    assert raw["chunks"][0]["workdir"] == "fast" and raw["chunks"][0]["model_ply"] == "fast/model.ply"
    assert raw["chunks"][0]["cameras"][0]["path"] == "/nowhere/a.jpg"     # photos outside: absolute


def test_old_absolute_project_rebases_and_migrates(tmp_path):
    """Pre-0.1.7 projects stored absolute paths: a moved folder is still found
    (rebase), and the next save writes them relative."""
    from photomesh.project import Project
    old = tmp_path / "old" / "bench"
    (old / "fast" / "dense").mkdir(parents=True)
    (old / "fast" / "model.ply").write_bytes(b"ply\n")
    (old / "fast" / "dense" / "point_cloud.ply").write_bytes(b"ply\n")
    d = {"format": "photomesh-studio/1", "active": 0, "chunks": [{
        "name": "Chunk 1", "workdir": str(old / "fast"), "model_ply": str(old / "fast" / "model.ply"),
        "point_cloud_ply": str(old / "fast" / "dense" / "point_cloud.ply"),
        "cameras": [{"path": str(old / "photos" / "a.jpg")}]}]}
    (old / "p.pmsx").write_text(json.dumps(d), encoding="utf-8")
    new = tmp_path / "other_disk" / "bench"
    shutil.copytree(old, new); shutil.rmtree(tmp_path / "old")
    q = Project.load(str(new / "p.pmsx"))
    ch = q.chunks[0]
    assert ch.workdir == str(new / "fast") and ch.model_ply == str(new / "fast" / "model.ply")
    assert len(q.relocated) == 3
    q.save(str(new / "p.pmsx"))
    raw = json.load(open(new / "p.pmsx", encoding="utf-8"))["chunks"][0]
    assert raw["workdir"] == "fast" and raw["point_cloud_ply"] == "fast/dense/point_cloud.ply"
    # the windows path form of an old project also rebases
    from photomesh.relink import rebase_path
    assert rebase_path(r"C:\Users\A\bench\fast\model.ply", str(new)) == str(new / "fast" / "model.ply")
    assert rebase_path(r"C:\Users\A\bench\fast\nothing.ply", str(new)) is None


def test_change_image_paths_ui(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from photomesh.project import Camera
    new = tmp_path / "photos"
    _img(str(new / "IMG_1.JPG")); _img(str(new / "IMG_2.JPG"))
    window.chunk.cameras = [Camera(path=r"X:\gone\IMG_1.JPG"), Camera(path=r"X:\gone\IMG_2.JPG")]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(new)))
    assert "Change Image Paths..." in window.actions
    window.actions["Change Image Paths..."].trigger()
    assert [c.path for c in window.chunk.cameras] == [str(new / "IMG_1.JPG"), str(new / "IMG_2.JPG")]
    assert window.project.modified


def test_open_reports_missing_photos(window, tmp_path):
    from photomesh.project import Project, Chunk, Camera
    p = Project(); p.chunks = [Chunk(cameras=[Camera(path=r"Q:\x\a.jpg")])]
    p.save(str(tmp_path / "m.pmsx"))
    window.open_project(str(tmp_path / "m.pmsx"))
    assert "Change Image Paths" in window.statusBar().currentMessage()


def test_cpu_only_without_nvidia_driver(monkeypatch):
    from photomesh import colmap as cm
    monkeypatch.setenv("PHOTOMESH_FORCE_NO_GPU", "1")
    assert not cm.nvidia_driver_present()
    assert not cm.has_cuda("colmap-that-does-not-exist")          # dense is refused with a message
    assert cm.gpu_status().startswith("CPU only")
    from photomesh import dialogs
    assert dialogs._gpu_ok() is False
