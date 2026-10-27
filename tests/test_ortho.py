"""Build DEM / Build Orthomosaic / Export (GeoTIFF, JPG/PNG + world files),
the Ortho tab (pyramid tiles, cursor coordinates) - synthetic scene with a
known albedo (tests/texsynth.py) placed at a large georeference offset."""
import json
import os
import shutil
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texsynth as ts                                          # noqa: E402
from photomesh import ortho, geotiff as gt                     # noqa: E402

QUIET = lambda m: None                                         # noqa: E731
OFF = [26700.0, 23000.0, 1300.0]
LOCAL_CRS = 'LOCAL_CS["Local Coordinates (m)",LOCAL_DATUM["Local Datum",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]]]'
GEO = {"offset": OFF, "crs": LOCAL_CRS}


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    return ts.make_scene(str(tmp_path_factory.mktemp("orthoscene")))


@pytest.fixture(scope="module")
def ortho_geo(scene, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("ortho") / "o")
    return ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                        out_dir=out, georef=GEO, pixel_size=0.02, block=512), log=QUIET)


def _reference(info, H, W):
    jj, ii = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    X, Y = ortho.pixel_to_world(info, jj.ravel(), ii.ravel())
    x, y = X - info["offset"][0], Y - info["offset"][1]
    return ts.albedo(np.column_stack([x, y, ts.height(x, y)])).reshape(H, W, 3)


def _best_shift(a, ref, ok, axis):
    errs = []
    for s in (-2, -1, 0, 1, 2):
        errs.append(np.abs(np.roll(a, s, axis=axis) - ref)[ok].mean())
    e = np.array(errs)
    k = int(np.argmin(e))
    # parabola through the minimum -> sub-pixel offset
    if 0 < k < 4:
        d = (e[k - 1] - e[k + 1]) / (2 * (e[k - 1] - 2 * e[k] + e[k + 1]))
    else:
        d = 0.0
    return (k - 2) + d, e


def test_ortho_georef_pixels_within_one_pixel(ortho_geo):
    info = ortho_geo
    assert info["georef"] and info["pixel_size"] == 0.02
    with gt.TiffReader(info["path"]) as r:
        a = r.read_level(0)
        # GeoTIFF tags: REAL world coordinates of the upper-left corner (offset added)
        x0, y1, sx, sy = r.geo
        assert (x0, y1) == pytest.approx(tuple(info["origin"]), abs=1e-9)
        assert x0 == pytest.approx(OFF[0] + 0.0, abs=0.021) and y1 == pytest.approx(OFF[1] + 20.0, abs=0.021)
        assert sx == sy == 0.02 and len(r.levels) >= 3 and r.alpha
    # origin snapped to the pixel grid in world coordinates
    assert abs(info["origin"][0] / 0.02 - round(info["origin"][0] / 0.02)) < 1e-6
    H, W = a.shape[:2]
    assert (W, H) == (info["width"], info["height"]) and W >= 990 and H >= 990
    ok = a[..., 3] > 0
    assert ok.mean() > 0.97
    ref = _reference(info, H, W)
    # away from the raised block (its walls lean in the photos) and the scene border
    yy, xx = np.mgrid[0:H, 0:W]
    X, Y = ortho.pixel_to_world(info, xx + 0.5, yy + 0.5)
    x, y = X - OFF[0], Y - OFF[1]
    m = ok & ((np.abs(x - 10) > 3.5) | (np.abs(y - 10) > 3.5)) & (x > 1) & (x < 19) & (y > 1) & (y < 19)
    img = a[..., :3].astype(float)
    dx, ex = _best_shift(img, ref, m, 1)
    dy, ey = _best_shift(img, ref, m, 0)
    assert abs(dx) < 1.0 and abs(dy) < 1.0, (dx, dy, ex, ey)
    fx, fy = x % 1, y % 1
    inner = m & (np.minimum(fx, 1 - fx) > 0.1) & (np.minimum(fy, 1 - fy) > 0.1)
    assert np.median(np.abs(img - ref)[inner]) < 12
    # height grid = world Z (offset added)
    with gt.TiffReader(info["height_path"]) as r:
        h = r.read_level(0)[..., 0]
    v = h[h != ortho.NODATA_DEM]
    assert v.min() > OFF[2] - 1 and v.max() < OFF[2] + 3


@pytest.mark.parametrize("blending,seams", [("Average", False), ("Disabled", False), ("Mosaic", True)])
def test_blending_modes(scene, tmp_path, blending, seams):
    info = ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                        out_dir=str(tmp_path / "o"), georef=GEO, pixel_size=0.04,
                                        blending=blending, refine_seamlines=seams, ghosting=False), log=QUIET)
    with gt.TiffReader(info["path"]) as r:
        a = r.read_level(0)
    ok = a[..., 3] > 0
    ref = _reference(info, *a.shape[:2])
    assert ok.mean() > 0.95 and np.median(np.abs(a[..., :3].astype(float) - ref)[ok]) < 25
    assert info["blending"] == blending and info["refine_seamlines"] == seams


def test_not_georeferenced(scene, tmp_path):
    info = ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"],
                                        out_dir=str(tmp_path / "o"), georef={}), log=QUIET)
    assert not info["georef"] and info["offset"] == [0, 0, 0] and info["crs"] == ""
    assert info["pixel_size"] == pytest.approx(ortho.nice(info["source_gsd"]))
    # the projection frame of this aerial block is the local frame (cameras look down -z)
    assert np.allclose(info["rotation"], np.eye(3), atol=0.05)
    assert -0.2 < info["bounds"][0] < 0.2 and 19.8 < info["bounds"][3] < 20.2
    files = ortho.export_raster(info, str(tmp_path / "e" / "o.tif") if os.makedirs(tmp_path / "e") is None else "",
                                log=QUIET)
    tfw = open(files[1]).read().split()
    assert float(tfw[4]) == pytest.approx(info["origin"][0] + info["pixel_size"] / 2, abs=1e-6)
    assert not any(f.endswith(".prj") for f in files)


def test_export_geotiff_jpg_png_world_files(ortho_geo, tmp_path):
    info = ortho_geo
    px = info["pixel_size"]; x0, y1 = info["origin"]
    d = tmp_path / "exp"; d.mkdir()
    # GeoTIFF LZW + alpha
    files = ortho.export_raster(info, str(d / "o_lzw.tif"), compression="LZW", log=QUIET)
    assert files[0].endswith("o_lzw.tif") and os.path.exists(str(d / "o_lzw.tfw"))
    lines = open(str(d / "o_lzw.tfw")).read().split()
    assert [float(v) for v in lines] == pytest.approx([px, 0, 0, -px, x0 + px / 2, y1 - px / 2], abs=1e-9)
    assert not os.path.exists(str(d / "o_lzw.prj"))                  # local CRS: no .prj
    with gt.TiffReader(files[0]) as r, gt.TiffReader(info["path"]) as s:
        assert r.compression == "LZW" and r.geo == pytest.approx(s.geo) and r.bands == 4
        assert np.array_equal(r.read_level(0), s.read_level(0))
        assert len(r.levels) >= 3
    from PIL import Image
    with Image.open(files[0]) as im:                                 # libtiff can read it too
        im.load(); assert im.size == (info["width"], info["height"])
    # JPEG-compressed GeoTIFF: RGB + nodata 0, valid pixels never 0
    f2 = ortho.export_raster(info, str(d / "o_jpeg.tif"), compression="JPEG", log=QUIET)[0]
    with gt.TiffReader(f2) as r:
        assert r.bands == 3 and r.nodata == 0 and r.compression == "JPEG"
        a = r.read_level(0); assert a.shape[:2] == (info["height"], info["width"])
    # Deflate, half resolution, forced BigTIFF
    f3 = ortho.export_raster(info, str(d / "o_half.tif"), compression="Deflate", scale=2, bigtiff="yes",
                             log=QUIET)[0]
    with gt.TiffReader(f3) as r:
        assert r.big and r.geo[2] == pytest.approx(2 * px) and r.width == (info["width"] + 1) // 2
    # JPG + .jgw, PNG + .pgw (alpha), split tiles
    fj = ortho.export_raster(info, str(d / "o.jpg"), log=QUIET)
    assert os.path.exists(str(d / "o.jgw")) and Image.open(fj[0]).mode == "RGB"
    fp = ortho.export_raster(info, str(d / "o.png"), log=QUIET)
    assert os.path.exists(str(d / "o.pgw")) and Image.open(fp[0]).mode == "RGBA"
    fs = ortho.export_raster(info, str(d / "t.jpg"), split=512, log=QUIET)
    tiles = [f for f in fs if f.endswith(".jpg")]
    assert len(tiles) == 4
    w = open(str(d / "t_001_001.jgw")).read().split()
    assert float(w[4]) == pytest.approx(x0 + 512 * px + px / 2) and float(w[5]) == pytest.approx(y1 - 512 * px - px / 2)
    # Rhino / AutoCAD placement help with the real corners
    note = open(str(d / "o_placement.txt"), encoding="utf-8").read()
    assert f"{x0:.4f}" in note and "Picture" in note and "IMAGEATTACH" in note
    script = open(str(d / "o_rhino_place.py"), encoding="utf-8").read()
    assert "AddPictureFrame" in script and repr(x0) in script
    compile(script, "rhino.py", "exec")


def test_cancel_leaves_no_partial_output(scene, tmp_path):
    seen = []

    def prog(i, n):
        seen.append(i)
    out = str(tmp_path / "o")
    with pytest.raises(ortho.Cancelled):
        ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"], out_dir=out,
                                     georef=GEO, pixel_size=0.02, block=512), log=QUIET, progress=prog,
                                cancel=lambda: bool(seen) and seen[-1] >= 30)
    assert not any(n.endswith(".part") for n in os.listdir(out))
    assert not os.path.exists(os.path.join(out, "orthomosaic.tif"))
    # export cancel too
    info = ortho.build_orthomosaic(dict(mesh=scene["mesh"], model=scene["model"], photos=scene["photos"], out_dir=out,
                                        georef=GEO, pixel_size=0.04), log=QUIET)
    with pytest.raises(ortho.Cancelled):
        ortho.export_raster(info, str(tmp_path / "x.tif"), cancel=lambda: True, log=QUIET)
    assert not [n for n in os.listdir(tmp_path) if n.startswith("x.")]


def _cloud(tmp_path, n=200_000):
    from photomesh.plyio import save_ply
    rng = np.random.default_rng(5)
    x, y = rng.uniform(0, 20, n), rng.uniform(0, 20, n)
    keep = ~((np.abs(x - 5) < 1) & (np.abs(y - 5) < 1))           # a hole to interpolate
    x, y = x[keep], y[keep]
    P = np.column_stack([x, y, ts.height(x, y)]).astype(np.float32)
    p = str(tmp_path / "cloud.ply")
    save_ply(p, P, np.full((len(P), 3), 0.5))
    return p


def test_dem_from_cloud_world_heights(tmp_path):
    cloud = _cloud(tmp_path)
    info = ortho.build_dem(dict(cloud=cloud, georef=GEO, out_dir=str(tmp_path / "dem")), log=QUIET)
    assert info["pixel_size"] > 0 and info["georef"]
    with gt.TiffReader(info["path"]) as r:
        z = r.read_level(0)[..., 0]
        assert r.dtype == np.float32 and r.nodata == ortho.NODATA_DEM
        x0, y1, px, _ = r.geo
    assert x0 == pytest.approx(info["origin"][0]) and x0 > OFF[0] - 1
    H, W = z.shape
    jj, ii = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    x, y = x0 + jj * px - OFF[0], y1 - ii * px - OFF[1]
    ref = ts.height(x, y) + OFF[2]
    ok = z != ortho.NODATA_DEM
    flat = ok & ((np.abs(x - 10) > 3) | (np.abs(y - 10) > 3))      # away from the block walls
    assert np.median(np.abs(z - ref)[flat]) < 0.03
    hole = (np.abs(x - 5) < 0.7) & (np.abs(y - 5) < 0.7)
    assert ok[hole].all() and np.abs(z - ref)[hole].max() < 0.3       # interpolated
    # Disabled interpolation keeps the hole empty
    info2 = ortho.build_dem(dict(cloud=cloud, georef=GEO, out_dir=str(tmp_path / "dem2"), interpolation="Disabled",
                                 pixel_size=info["pixel_size"]), log=QUIET)
    with gt.TiffReader(info2["path"]) as r:
        assert (r.read_level(0)[..., 0][hole] == ortho.NODATA_DEM).mean() > 0.8
    # float32 GeoTIFF export
    f = ortho.export_raster(info, str(tmp_path / "dem_out.tif"), kind="dem", compression="LZW", log=QUIET)
    with gt.TiffReader(f[0]) as r:
        assert r.dtype == np.float32 and np.array_equal(r.read_level(0)[..., 0], z)
    assert os.path.exists(str(tmp_path / "dem_out.tfw"))


def test_ortho_on_dem_surface(scene, tmp_path):
    dem = ortho.build_dem(dict(mesh=scene["mesh"], source="Mesh", georef=GEO, out_dir=str(tmp_path / "dem"),
                               pixel_size=0.05, interpolation="Extrapolated"), log=QUIET)
    # mesh vertices are sparse (25 cm): a DEM from vertices needs interpolation -> Extrapolated fills all
    info = ortho.build_orthomosaic(dict(dem=dem["path"], surface="DEM", model=scene["model"], photos=scene["photos"],
                                        out_dir=str(tmp_path / "o"), georef=GEO, pixel_size=0.04), log=QUIET)
    with gt.TiffReader(info["path"]) as r:
        a = r.read_level(0)
    assert info["surface"] == "DEM" and (a[..., 3] > 0).mean() > 0.9


def test_geotiff_codecs_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    H, W = 700, 900
    img = rng.integers(0, 255, (H, W, 4), dtype=np.uint8)
    img[..., 3] = 255; img[:50, :50, 3] = 0
    for comp in ("None", "Deflate", "LZW"):
        p = str(tmp_path / f"{comp}.tif")
        w = gt.TiledTiffWriter(p, gt.overview_levels(W, H), 4, compression=comp, geo=(OFF[0], OFF[1], 0.5, 0.5),
                               crs="EPSG:32639")
        w.write_block(0, 0, 0, img); gt.build_overviews(w, alpha_band=3); w.close()
        with gt.TiffReader(p) as r:
            assert np.array_equal(r.read_level(0), img) and r.epsg() == 32639 and r.geo == (OFF[0], OFF[1], 0.5, 0.5)
            assert r.read_level(1).shape[:2] == ((H + 1) // 2, (W + 1) // 2)
    assert gt.parse_epsg(LOCAL_CRS) is None and gt.prj_text(LOCAL_CRS) is None
    assert gt.parse_epsg("EPSG:4326") == 4326 and gt.is_geographic("EPSG:4326")


def test_pixel_world_mapping_and_cursor_readout(qtbot, ortho_geo):
    """Ortho tab: screen -> REAL world coordinates (offset included) and Z from the height grid."""
    from photomesh.orthoview import OrthoView, format_coords
    v = OrthoView(); qtbot.addWidget(v); v.resize(800, 600); v.show()
    v.set_frame(OFF, geographic=False, unit="m")
    v.set_raster("ortho", ortho_geo)
    qtbot.waitUntil(lambda: v.zgrid is not None, timeout=10000)
    info = ortho_geo
    # pixel -> world (offset included) and back
    X, Y = ortho.pixel_to_world(info, 100.25, 40.5)
    assert X == pytest.approx(info["origin"][0] + 100.25 * 0.02) and X > OFF[0]
    assert Y == pytest.approx(info["origin"][1] - 40.5 * 0.02) and Y > OFF[1]
    c, r = ortho.world_to_pixel(info, X, Y)
    assert (c, r) == pytest.approx((100.25, 40.5))
    # widget: zoom 1:1 on a known pixel and read the coordinates under the cursor
    v.scale = 1 / 0.02
    v.center = tuple(ortho.pixel_to_world(info, 500, 500))
    Xs, Ys, Zs = v.coordinates_at(v.width() / 2 + 10, v.height_px() / 2 - 20)
    assert Xs == pytest.approx(v.center[0] + 10 * 0.02) and Ys == pytest.approx(v.center[1] + 20 * 0.02)
    xl, yl = Xs - OFF[0], Ys - OFF[1]
    assert Zs == pytest.approx(float(ts.height(xl, yl)) + OFF[2], abs=0.1)
    txt = format_coords(Xs, Ys, Zs)
    assert txt.startswith(f"X: {Xs:,.3f} m") and "Z:" in txt
    assert format_coords(51.5, 35.7, None, geographic=True).startswith("Lon: 51.50000000")
    from PySide6.QtCore import QPointF
    v._update_coords(QPointF(v.width() / 2, v.height_px() / 2))
    assert v.coord_text.startswith(f"X: {v.center[0]:,.3f}")
    clip = v.copy_coordinates(Xs, Ys, Zs)
    from PySide6.QtGui import QGuiApplication
    assert QGuiApplication.clipboard().text() == clip and clip.startswith(f"{Xs:.3f}\t")
    # tiles load in the background; painting never blocks
    v.repaint()
    qtbot.waitUntil(lambda: v.pending_count() == 0, timeout=10000)
    v.repaint()
    assert len(v.cache) > 0
    v.zoom(0.5); v.zoom(4); v.fit()


# ------------------------------------------------------------------ GUI
def _setup_chunk(window, scene, tmp_path, georef=True):
    from photomesh.plyio import ply_counts
    ch = window.chunk
    ch.add_photos(sorted(scene["photos"].values()))
    wd = str(tmp_path / "work"); ch.workdir = wd
    shutil.copytree(scene["model"], os.path.join(wd, "sparse", "0"))
    shutil.copy(scene["mesh"], os.path.join(wd, "model.ply"))
    ch.model_ply = os.path.join(wd, "model.ply"); ch.face_count = ply_counts(ch.model_ply)[1]
    if georef:
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


def test_gui_build_export_import_orthomosaic(window, no_dialogs, monkeypatch, scene, tmp_path):
    window.project.path = str(tmp_path / "proj.pmsx")
    from photomesh import dialogs, procmeta as pm
    monkeypatch.setattr(dialogs.ProgressDialog, "exec", lambda self: None)
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "exec", lambda self: 1)
    s0 = dialogs.BuildOrthomosaicDialog.settings
    monkeypatch.setattr(dialogs.BuildOrthomosaicDialog, "settings", lambda self: dict(s0(self), pixel_size=0.04))
    monkeypatch.setattr(dialogs.BuildDEMDialog, "exec", lambda self: 1)
    ch = _setup_chunk(window, scene, tmp_path)
    window.actions["Build Orthomosaic..."].trigger(); _wait(window)
    o = ch.orthomosaic
    assert o.get("pixel_size") == 0.04, window.chunk.log
    assert window.tabs.currentWidget() is window.ortho_view and window.ortho_view.has_raster("ortho")
    assert "4 cm/pix" in pm.summary(ch, "ortho")
    rows = dict(pm.properties(ch, "ortho"))
    assert rows["Blending mode"] == "Mosaic" and rows["Resolution"] == "4 cm/pix" and "Date created" in rows
    labels = []
    def walk(it):
        for i in range(it.childCount()):
            labels.append(it.child(i).text(0)); walk(it.child(i))
    walk(window.workspace.invisibleRootItem())
    assert any(l.startswith("Orthomosaic (") for l in labels), labels
    # layer toggle (toolbar / menu / workspace checkbox)
    a = window.layer_action("ortho")
    from PySide6.QtWidgets import QToolBar
    tb = [b for b in window.findChildren(QToolBar) if b.objectName() == "toolbar"][0]
    assert a in tb.actions() and any(s.width() == 16 for s in a.icon().availableSizes())
    a.setChecked(False); assert window.ortho_view.layers["ortho"].visible is False
    a.setChecked(True); assert window.ortho_view.layers["ortho"].visible is True
    # export from the GUI path (background thread)
    files = window.export_raster_to("ortho", str(tmp_path / "exp.tif"), fmt="tif", compression="Deflate")
    with gt.TiffReader(files[0]) as r:
        assert r.geo[0] == pytest.approx(o["origin"][0]) and r.geo[1] > OFF[1] + 19
    # DEM from the mesh (no cloud in this chunk)
    window.actions["Build DEM..."].trigger(); _wait(window)
    assert ch.dem.get("width"), window.chunk.log
    assert window.ortho_view.has_raster("dem")
    # save / load keeps both; Import Orthomosaic of a moved folder
    window.project.path = str(tmp_path / "p.pmsx"); window.project.save(window.project.path)
    from photomesh.project import Project
    c2 = Project.load(window.project.path).chunks[0]
    assert c2.orthomosaic["width"] == o["width"] and c2.dem["width"] == ch.dem["width"]
    moved = str(tmp_path / "moved_ortho"); shutil.copytree(o["dir"], moved)
    ch.orthomosaic = {}
    assert window.import_raster("ortho", moved, quiet=True)
    assert ch.orthomosaic["path"].startswith(os.path.abspath(moved)) and ch.meta["orthomosaic"]["source"] == "imported"
    assert window.import_raster("ortho", files[0], quiet=True) and ch.orthomosaic["format"] == "imported"
    assert not window.import_raster("ortho", str(tmp_path / "p.pmsx"), quiet=True)
    assert no_dialogs == []


def test_overview_export_pixel_grid_is_exact_power_of_two(ortho_geo, tmp_path):
    """Reduced exports keep the exact 2**k pixel grid from the same origin (PhotoMesh
    overviews pad odd edges), so there is no sub-pixel drift towards the far edge
    (real case: 25,053 px -> 1,566 px at 1/16 is 0.7168 m/pix, not 25053/1566*px)."""
    info = ortho_geo
    with gt.TiffReader(info["path"]) as r:
        lev = len(r.levels) - 1
    files = ortho.export_raster(info, str(tmp_path / "o.tif"), scale=2 ** lev, log=QUIET)
    tfw = [float(v) for v in open(str(tmp_path / "o.tfw")).read().split()]
    px = info["pixel_size"] * 2 ** lev
    assert abs(tfw[0] - px) < 1e-9 and abs(tfw[3] + px) < 1e-9
    assert abs(tfw[4] - (info["origin"][0] + px / 2)) < 1e-6
    assert abs(tfw[5] - (info["origin"][1] - px / 2)) < 1e-6
    with gt.TiffReader(files[0]) as r:
        assert abs(r.geo[2] - px) < 1e-9
    # odd sizes: exact for PhotoMesh rasters, GDAL convention (same extent) for foreign ones
    own = {"format": "photomesh-ortho/1", "pixel_size": 0.0448, "width": 25053}
    assert abs(ortho.level_pixel_size(own, 1566, 4) - 0.0448 * 16) < 1e-12
    foreign = dict(own, format="imported")
    assert abs(ortho.level_pixel_size(foreign, 1566, 4) - 0.0448 * 25053 / 1566) < 1e-12
    # sample check of the overview content: 2x2 mean of level 0 == level 1 pixel
    with gt.TiffReader(info["path"]) as r:
        a0 = r.read_region(0, 0, 0, 64, 64).astype(float)
        a1 = r.read_region(1, 0, 0, 32, 32).astype(float)
    full = (a0[..., 3] == 255).reshape(32, 2, 32, 2).all(axis=(1, 3))
    m = a0[..., :3].reshape(32, 2, 32, 2, 3).mean(axis=(1, 3))
    if full.any():
        assert np.abs(m[full] - a1[..., :3][full]).max() <= 1.0
