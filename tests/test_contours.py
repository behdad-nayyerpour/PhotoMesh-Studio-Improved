"""Analytic mesh sections and independent CAD parsing of contour DXF files."""
import math

import ezdxf
import numpy as np
import pytest

from photomesh import contours as C
from photomesh.plyio import save_ply


def plane(zshift=0.0):
    V = np.array([[0, 0, zshift], [4, 0, 4 + zshift],
                  [4, 3, 4 + zshift], [0, 3, zshift]], dtype=float)
    return V, np.array([[0, 1, 2], [0, 2, 3]])


def pyramid():
    V = np.array([[-2, -2, 0], [2, -2, 0], [2, 2, 0],
                  [-2, 2, 0], [0, 0, 2]], dtype=float)
    return V, np.array([[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]])


def read_contours(path):
    doc = ezdxf.readfile(path)
    assert "minor" in doc.layers and "major" in doc.layers
    assert not doc.audit().has_errors
    entities = list(doc.modelspace())
    assert entities and all(e.dxftype() == "POLYLINE" for e in entities)
    result = []
    for e in entities:
        assert e.dxf.flags & 8 and not e.dxf.flags & 64
        assert e.dxf.layer in ("minor", "major")
        assert all(v.dxf.layer == e.dxf.layer for v in e.vertices)
        assert all(v.dxf.flags & 32 for v in e.vertices)
        xyz = np.array([tuple(v.dxf.location) for v in e.vertices])
        assert len(xyz) >= 2 and np.ptp(xyz[:, 2]) == 0
        assert np.isfinite(xyz).all()
        result.append((xyz, e.is_closed))
    return result


def test_plane_is_stitched_across_diagonal_and_not_a_mesh(tmp_path):
    V, F = plane()
    out = tmp_path / "plane.dxf"
    stats = C.write_mesh_contours(out, V, F, 1)
    paths = read_contours(out)
    assert stats["polylines"] == stats["levels"] == len(paths) == 4
    assert stats["minor"] == 4 and stats["major"] == 0
    for xyz, closed in paths:
        assert not closed
        z = xyz[0, 2]
        assert z in (1, 2, 3, 4)
        assert np.allclose(xyz[:, 0], z)
        assert sorted(xyz[[0, -1], 1]) == pytest.approx([0, 3])


def test_closed_pyramid_contours_preserve_elevations(tmp_path):
    out = tmp_path / "hill.dxf"
    stats = C.write_mesh_contours(out, *pyramid(), 0.5)
    paths = read_contours(out)
    assert stats["polylines"] == 3
    assert sorted(p[0][0, 2] for p in paths) == [0.5, 1, 1.5]
    for xyz, closed in paths:
        assert closed and len(xyz) == 4
        half_width = 2 - xyz[0, 2]
        assert np.allclose(np.abs(xyz[:, :2]), half_width)


def test_separate_hills_remain_separate_and_duplicate_seams_join(tmp_path):
    V, F = pyramid()
    # Unshared face vertices simulate an unwelded input mesh.
    unwelded = V[F].reshape(-1, 3)
    faces = np.arange(len(unwelded)).reshape(-1, 3)
    vertices = np.vstack([unwelded, unwelded + [10, 0, 0]])
    faces = np.vstack([faces, faces + len(unwelded)])
    out = tmp_path / "two_hills.dxf"
    stats = C.write_mesh_contours(out, vertices, faces, 1)
    paths = read_contours(out)
    assert stats["polylines"] == len(paths) == 2
    assert all(closed for _, closed in paths)
    assert all(np.ptp(xyz[:, 0]) == 2 for xyz, _ in paths)
    assert sorted(np.mean(xyz[:, 0]) for xyz, _ in paths) == [0, 10]


def test_world_grid_fractional_offset_and_large_xy(tmp_path):
    V, F = plane()
    off = np.array([600000.125, 4000000.25, 100.125])
    out = tmp_path / "world.dxf"
    C.write_mesh_contours(out, V, F, 0.75, off)
    paths = read_contours(out)
    assert sorted(xyz[0, 2] for xyz, _ in paths) == [100.5, 101.25, 102, 102.75, 103.5]
    for xyz, _ in paths:
        assert np.allclose(xyz[:, 0] - off[0], xyz[:, 2] - off[2], atol=1e-9, rtol=0)
        assert sorted(xyz[[0, -1], 1] - off[1]) == pytest.approx([0, 3], abs=1e-9)


def test_negative_elevation_levels(tmp_path):
    out = tmp_path / "below_zero.dxf"
    C.write_mesh_contours(out, *plane(-2), 1)
    assert sorted(xyz[0, 2] for xyz, _ in read_contours(out)) == [-1, 0, 1, 2]


def test_flat_plateau_has_outer_contour_without_internal_diagonal(tmp_path):
    V = np.array([[-2, -2, 0], [2, -2, 0], [2, 2, 0], [-2, 2, 0],
                  [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)
    F = []
    for i in range(4):
        j = (i + 1) % 4
        F += [[i, j, 4 + j], [i, 4 + j, 4 + i]]
    F += [[4, 5, 6], [4, 6, 7]]
    out = tmp_path / "plateau.dxf"
    C.write_mesh_contours(out, V, np.array(F), 1)
    paths = read_contours(out)
    assert len(paths) == 1
    xyz, closed = paths[0]
    assert closed and len(xyz) == 4
    assert np.allclose(np.abs(xyz[:, :2]), 1)


def test_saddle_splits_at_junction_instead_of_connecting_arbitrarily(tmp_path):
    V = np.array([[-1, -1, 1], [1, -1, -1], [1, 1, 1], [-1, 1, -1], [0, 0, 0]], float)
    F = np.array([[4, i, (i + 1) % 4] for i in range(4)])
    out = tmp_path / "saddle.dxf"
    stats = C.write_mesh_contours(out, V, F, 1)
    paths = read_contours(out)
    assert stats["junctions"] == 1 and len(paths) == 4
    assert all(not closed and len(xyz) == 2 for xyz, closed in paths)
    assert all(np.all(xyz[:, 2] == 0) for xyz, _ in paths)


@pytest.mark.parametrize("interval", [0, -1, math.inf, math.nan])
def test_invalid_interval_writes_nothing(tmp_path, interval):
    out = tmp_path / "bad.dxf"
    with pytest.raises(ValueError, match="interval"):
        C.write_mesh_contours(out, *plane(), interval)
    assert not out.exists() and not (tmp_path / "bad.dxf.part").exists()


def test_no_intersection_preserves_existing_destination(tmp_path):
    out = tmp_path / "old.dxf"
    out.write_bytes(b"existing")
    V, F = plane(0.25)
    with pytest.raises(ValueError, match="No contour"):
        C.write_mesh_contours(out, V, F, 100)
    assert out.read_bytes() == b"existing"


def test_flat_mesh_and_excessive_level_count_are_rejected(tmp_path):
    V, F = plane()
    V[:, 2] = 2
    with pytest.raises(ValueError, match="flat"):
        C.write_mesh_contours(tmp_path / "flat.dxf", V, F, 1)
    with pytest.raises(ValueError, match="levels"):
        C.write_mesh_contours(tmp_path / "huge.dxf", *plane(), 0.000001)


def test_cancellation_removes_partial_and_preserves_old_file(tmp_path):
    out = tmp_path / "cancel.dxf"
    out.write_bytes(b"old complete result")
    stop = False
    def report(done, total, text):
        nonlocal stop
        if done:
            stop = True
    with pytest.raises(C.ContourCancelled):
        C.write_mesh_contours(out, *plane(), 1, report=report, cancelled=lambda: stop)
    assert out.read_bytes() == b"old complete result"
    assert not (tmp_path / "cancel.dxf.part").exists()


def test_face_limits_and_bad_coordinates_fail_without_partial_output(tmp_path, monkeypatch):
    V, F = plane()
    with pytest.raises(ValueError, match="index"):
        C.write_mesh_contours(tmp_path / "index.dxf", V, F + 5, 1)
    V[0, 0] = math.nan
    with pytest.raises(ValueError, match="finite"):
        C.write_mesh_contours(tmp_path / "nan.dxf", V, F, 1)
    monkeypatch.setattr(C, "MAX_TOTAL_SEGMENTS", 1)
    out = tmp_path / "limited.dxf"
    with pytest.raises(ValueError, match="segments"):
        C.write_mesh_contours(out, *plane(), 1)
    assert not out.exists() and not (tmp_path / "limited.dxf.part").exists()


def test_ply_input_is_not_changed(tmp_path):
    V, F = plane()
    src = tmp_path / "model.ply"
    save_ply(str(src), V.astype(np.float32), faces=F)
    before = src.read_bytes()
    out = tmp_path / "from_ply.dxf"
    stats = C.export_mesh_contours(src, out, 1)
    assert stats["polylines"] == 4 and len(read_contours(out)) == 4
    assert src.read_bytes() == before


def test_export_menu_asks_interval_and_uses_contour_route(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QInputDialog
    src, out = tmp_path / "model.ply", tmp_path / "ui.dxf"
    V, F = plane()
    save_ply(str(src), V.astype(np.float32), faces=F)
    window.chunk.model_ply = str(src)
    asked = []
    def choose(*args):
        assert "Mesh contours DXF" in args[-1]
        return str(out), "Mesh contours DXF (*.dxf)"
    def interval(*args):
        asked.append(args)
        return 1.0, True
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(choose))
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(interval))
    window.export_model()
    assert len(asked) == 1 and len(read_contours(out)) == 4


def test_interval_dialog_cancel_does_not_export(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    src, out = tmp_path / "model.ply", tmp_path / "cancel_ui.dxf"
    V, F = plane()
    save_ply(str(src), V.astype(np.float32), faces=F)
    window.chunk.model_ply = str(src)
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a: (1.0, False)))
    assert window.export_model_to(str(out), "contours") is None
    assert not out.exists()


def test_batch_boundaries_reversed_faces_and_duplicate_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_BATCH", 1)
    V, F = plane()
    F = np.vstack([F[::-1, ::-1], F])
    out = tmp_path / "batched.dxf"
    stats = C.write_mesh_contours(out, V, F, 1)
    paths = read_contours(out)
    assert len(paths) == stats["polylines"] == 4
    assert stats["segments"] == 7
    assert all(not closed for _, closed in paths)


def test_interval_below_world_coordinate_precision_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="precision"):
        C.write_mesh_contours(tmp_path / "precision.dxf", *plane(), 1, offset=[0, 0, 1e16])


def test_four_minor_then_one_major_in_separate_cad_layers(tmp_path):
    V, F = plane()
    V[:, 2] *= 3
    out = tmp_path / "indexed.dxf"
    stats = C.write_mesh_contours(out, V, F, 1)
    read_contours(out)
    polylines = list(ezdxf.readfile(out).modelspace().query("POLYLINE"))
    assert [p.dxf.layer for p in polylines] == [
        "minor", "minor", "minor", "minor", "major",
        "minor", "minor", "minor", "minor", "major", "minor", "minor"]
    assert stats["major"] == 2 and stats["minor"] == 10
    for p in polylines:
        assert all(v.dxf.layer == p.dxf.layer for v in p.vertices)


@pytest.mark.parametrize("interval,zoffset", [(0.2, 0.13), (1.0, -6.0)])
def test_major_levels_are_world_multiples_not_relative_to_mesh_start(tmp_path, interval, zoffset):
    V, F = plane()
    V[:, 2] *= 3
    out = tmp_path / "world_indexed.dxf"
    stats = C.write_mesh_contours(out, V, F, interval, offset=[10, 20, zoffset])
    paths = read_contours(out)
    assert stats["major"] >= 2 and len(paths) == stats["polylines"]
    for p in ezdxf.readfile(out).modelspace().query("POLYLINE"):
        z = p.vertices[0].dxf.location.z
        index = round(z / interval)
        assert z == pytest.approx(index * interval)
        assert p.dxf.layer == ("major" if index % 5 == 0 else "minor")
