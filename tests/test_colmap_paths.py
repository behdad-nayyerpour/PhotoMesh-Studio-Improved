"""Regression tests for executable capability detection and photo staging."""
from types import SimpleNamespace

from photomesh import colmap
from photomesh.project import Camera, Chunk


def test_has_cuda_requires_a_readable_banner(monkeypatch):
    monkeypatch.setattr(colmap, "nvidia_driver_present", lambda: True)
    colmap._HELP_CACHE.clear()
    monkeypatch.setattr(colmap.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr=""))
    assert not colmap.has_cuda("missing-colmap")

    colmap._HELP_CACHE.clear()
    monkeypatch.setattr(colmap.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout="COLMAP build without CUDA", stderr=""))
    assert not colmap.has_cuda("cpu-colmap")

    colmap._HELP_CACHE.clear()
    monkeypatch.setattr(colmap.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout="COLMAP 3.8 with CUDA", stderr=""))
    assert colmap.has_cuda("gpu-colmap")
    colmap._HELP_CACHE.clear()


def test_prepare_images_refreshes_reused_name(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    other = tmp_path / "other"
    for folder in (old, new, other):
        folder.mkdir()
    (old / "same.jpg").write_bytes(b"old photo")
    (new / "same.jpg").write_bytes(b"new photo")
    (other / "other.jpg").write_bytes(b"another photo")
    chunk = Chunk(cameras=[Camera(path=str(old / "same.jpg")),
                           Camera(path=str(other / "other.jpg"))])
    workdir = str(tmp_path / "work")
    colmap.prepare_images(chunk, workdir)
    assert (tmp_path / "work" / "images" / "same.jpg").read_bytes() == b"old photo"

    chunk.cameras[0].path = str(new / "same.jpg")
    colmap.prepare_images(chunk, workdir)
    assert (tmp_path / "work" / "images" / "same.jpg").read_bytes() == b"new photo"
