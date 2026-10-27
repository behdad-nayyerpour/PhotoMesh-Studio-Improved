"""Import / export / draping / measuring / photo projection of shapes (shapes.py)
and the GUI integration (File > Import > Import Shapes..., Workspace, views)."""
import json
import math
import os
import struct
import zipfile

import numpy as np
import pytest

from photomesh import shapes as S
from photomesh import colmap_io as cio
from photomesh import geotiff as gt

OFF = np.array([26700.0, 23000.0, 1300.0])          # like the real project
QUIET = lambda *_a, **_k: None                        # noqa: E731


def _sample_set():
    ss = S.new_set()
    a = S.add_layer(ss, "GCP", (255, 220, 0)); b = S.add_layer(ss, "Site", (255, 60, 60))
    for i, (x, y, z) in enumerate([(26542.098, 23371.444, 1321.471), (26600.5, 23100.25, 1310.0)]):
        ss["shapes"].append(S.make_shape(a, "point", [[[x, y, z]]], {"label": f"P{i + 1}", "err": 0.01 * i,
                                                                      "n": i, "note": "Ünïcode"}, f"P{i + 1}"))
    outer = S._close([[26500, 23000, 1300], [26600, 23000, 1301], [26600, 23100, 1302], [26500, 23100, 1303]])
    hole = S._close([[26520, 23020, 1300], [26540, 23020, 1300], [26540, 23040, 1300], [26520, 23040, 1300]])
    ss["shapes"].append(S.make_shape(b, "polygon", [outer, hole], {"name": "site"}, "site", [False, True]))
    ss["shapes"].append(S.make_shape(b, "polyline", [[[26500, 23000, 1300], [26530, 23040, 1305],
                                                       [26560, 23000, 1310]]], {"name": "road"}, "road"))
    return ss


# ------------------------------------------------------------------ formats
def test_shp_roundtrip_types_holes_attributes_prj(tmp_path):
    ss = _sample_set()
    fr = S.Frame("utm", 38, False, OFF)
    files = S.export(ss, str(tmp_path / "t.shp"), "SHP", fr, log=QUIET)
    names = sorted(os.path.basename(f) for f in files)
    assert "t_points.shp" in names and "t_polygons.prj" in names and "t_lines.dbf" in names
    pts, crs, notes = S.read_shp(str(tmp_path / "t_points.shp"))
    assert crs.kind == "utm" and crs.zone == 38 and not crs.south
    p = pts["shapes"]
    assert [s["label"] for s in p] == ["P1", "P2"]
    assert np.allclose(p[0]["parts"][0], [[26542.098, 23371.444, 1321.471]], atol=1e-6)
    assert p[1]["attrs"]["note"] == "Ünïcode" and abs(float(p[1]["attrs"]["err"]) - 0.01) < 1e-9
    assert int(p[1]["attrs"]["n"]) == 1 and p[0]["has_z"]
    pol, _c, _n = S.read_shp(str(tmp_path / "t_polygons.shp"))
    s = pol["shapes"][0]
    assert s["type"] == "polygon" and s["holes"] == [False, True]
    assert abs(S.measure(s)["Area 2D"] - (100 * 100 - 20 * 20)) < 1e-6
    # orientation rules of the format: outer ring clockwise, hole counter-clockwise
    assert S._ring_signed_area(s["parts"][0]) < 0 < S._ring_signed_area(s["parts"][1])
    lin, _c, _n = S.read_shp(str(tmp_path / "t_lines.shp"))
    assert np.allclose(lin["shapes"][0]["parts"][0][:, 2], [1300, 1305, 1310])


def _write_shp2d(base, stype, recs):
    """Minimal 2D shapefile writer (types 1 / 3 / 5) for reader tests; recs = [[part (n,2), ...], ...]."""
    body = b""; shx = b""
    off = 50
    allp = np.vstack([np.vstack(r) for r in recs])
    bb = (allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
    for i, parts in enumerate(recs, 1):
        if stype == 1:
            c = struct.pack("<idd", 1, *parts[0][0])
        else:
            P = np.vstack(parts)
            idx = np.cumsum([0] + [len(p) for p in parts[:-1]])
            c = struct.pack("<i4d2i", stype, P[:, 0].min(), P[:, 1].min(), P[:, 0].max(), P[:, 1].max(),
                            len(parts), len(P))
            c += struct.pack(f"<{len(parts)}i", *idx) + P.astype("<f8").tobytes()
        body += struct.pack(">2i", i, len(c) // 2) + c
        shx += struct.pack(">2i", off, len(c) // 2)
        off += 4 + len(c) // 2

    def hdr(n):
        return struct.pack(">7i", 9994, 0, 0, 0, 0, 0, n) + struct.pack("<2i4d4d", 1000, stype, *bb, 0, 0, 0, 0)
    open(base + ".shp", "wb").write(hdr(50 + len(body) // 2) + body)
    open(base + ".shx", "wb").write(hdr(50 + len(shx) // 2) + shx)


def test_shp_2d_types(tmp_path):
    b = str(tmp_path / "p2")
    _write_shp2d(b, 1, [[np.array([[10.0, 20.0]])], [np.array([[11.0, 21.0]])]])
    ss, crs, notes = S.read_shp(b + ".shp")
    assert crs.kind == "project" and len(ss["shapes"]) == 2 and not ss["shapes"][0]["has_z"]
    assert np.allclose(ss["shapes"][1]["parts"][0][0, :2], [11, 21])
    b = str(tmp_path / "l2")
    _write_shp2d(b, 3, [[np.array([[0.0, 0], [3, 4]]), np.array([[10.0, 0], [10, 5]])]])
    ss, _c, _n = S.read_shp(b + ".shp")
    s = ss["shapes"][0]
    assert s["type"] == "polyline" and len(s["parts"]) == 2 and S.measure(s)["Length 2D"] == pytest.approx(10.0)
    b = str(tmp_path / "g2")       # outer CW + hole CCW
    _write_shp2d(b, 5, [[np.array([[0.0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]),
                         np.array([[2.0, 2], [4, 2], [4, 4], [2, 4], [2, 2]])]])
    ss, _c, _n = S.read_shp(b + ".shp")
    s = ss["shapes"][0]
    assert s["holes"] == [False, True] and S.measure(s)["Area 2D"] == pytest.approx(96.0)


def _dxf(path, entities, layers=(("ROADS", 3), ("GCP", 1))):
    g = [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1015"), (0, "ENDSEC"),
         (0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER")]
    for n, c in layers:
        g += [(0, "LAYER"), (2, n), (70, 0), (62, c)]
    g += [(0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")] + entities + [(0, "ENDSEC"), (0, "EOF")]
    with open(path, "w") as f:
        for c, v in g:
            f.write(f"{c:>3}\n{v}\n")


def test_dxf_2000_entities_layers_colours(tmp_path):
    ents = [(0, "POINT"), (8, "GCP"), (10, 26542.098), (20, 23371.444), (30, 1321.471),
            (0, "TEXT"), (8, "GCP"), (10, 26542.4), (20, 23371.7), (30, 1321.471), (40, 0.5), (1, "GCP-1"),
            (0, "LINE"), (8, "ROADS"), (10, 0), (20, 0), (30, 0), (11, 30), (21, 40), (31, 0),
            (0, "LWPOLYLINE"), (8, "ROADS"), (90, 4), (70, 1), (38, 12.5),
            (10, 0), (20, 0), (10, 10), (20, 0), (10, 10), (20, 10), (10, 0), (20, 10),
            (0, "POLYLINE"), (8, "ROADS"), (66, 1), (70, 8),
            (0, "VERTEX"), (8, "ROADS"), (10, 1), (20, 2), (30, 3), (70, 32),
            (0, "VERTEX"), (8, "ROADS"), (10, 4), (20, 6), (30, 3), (70, 32), (0, "SEQEND"),
            (0, "3DFACE"), (8, "0"), (62, 5), (10, 0), (20, 0), (30, 1), (11, 1), (21, 0), (31, 1),
            (12, 1), (22, 1), (32, 1), (13, 1), (23, 1), (33, 1),
            (0, "CIRCLE"), (8, "0"), (10, 5), (20, 5), (30, 0), (40, 2),
            (0, "HATCH"), (8, "0")]
    p = str(tmp_path / "a.dxf"); _dxf(p, ents)
    ss, crs, notes = S.read_dxf(p)
    assert crs.kind == "project"
    names = [L["name"] for L in ss["layers"]]
    assert {"GCP", "ROADS", "0"} <= set(names)
    col = {L["name"]: tuple(L["color"]) for L in ss["layers"]}
    assert col["GCP"] == S.aci_rgb(1) == (255, 0, 0) and col["ROADS"] == S.aci_rgb(3)
    kinds = [(ss["layers"][s["layer"]]["name"], s["type"]) for s in ss["shapes"]]
    assert ("GCP", "point") in kinds and kinds.count(("ROADS", "polyline")) == 2 and ("ROADS", "polygon") in kinds
    pt = next(s for s in ss["shapes"] if s["type"] == "point")
    assert pt["label"] == "GCP-1" and np.allclose(pt["parts"][0][0], [26542.098, 23371.444, 1321.471])
    lw = next(s for s in ss["shapes"] if s["type"] == "polygon" and ss["layers"][s["layer"]]["name"] == "ROADS")
    assert np.allclose(lw["parts"][0][:, 2], 12.5) and S.measure(lw)["Area 2D"] == pytest.approx(100.0)
    face = [s for s in ss["shapes"] if ss["layers"][s["layer"]]["name"] == "0" and s["type"] == "polygon"]
    assert len(face) == 2                                     # 3DFACE + CIRCLE (as polygon)
    circ = max(face, key=lambda s: len(s["parts"][0]))
    assert S.measure(circ)["Area 2D"] == pytest.approx(math.pi * 4, rel=0.01)
    assert any("HATCH" in n for n in notes)


def test_dxf_roundtrip_r12(tmp_path):
    ss = _sample_set()
    S.export(ss, str(tmp_path / "o.dxf"), "DXF", log=QUIET)
    txt = open(tmp_path / "o.dxf").read()
    assert "AC1009" in txt and "GCP" in txt
    r, _c, notes = S.read_dxf(str(tmp_path / "o.dxf"))
    pts = [s for s in r["shapes"] if s["type"] == "point"]
    assert [s["label"] for s in pts] == ["P1", "P2"]
    assert np.allclose(pts[0]["parts"][0][0], [26542.098, 23371.444, 1321.471], atol=1e-6)
    polys = [s for s in r["shapes"] if s["type"] == "polygon"]
    # R12 has no holes: outer + hole come back as two closed polylines
    assert sorted(round(S.measure(s)["Area 2D"]) for s in polys) == [400, 10000]
    col = {L["name"]: tuple(L["color"]) for L in r["layers"]}
    assert col["GCP"] == S.aci_rgb(S.rgb_aci((255, 220, 0)))


def test_dxf_binary_rejected(tmp_path):
    p = tmp_path / "b.dxf"; p.write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\0" * 20)
    with pytest.raises(S.ShapeError):
        S.read_dxf(str(p))


KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Style id="r"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>
<Folder><name>Survey</name>
<Placemark><name>GCP 7</name><ExtendedData><Data name="acc"><value>0.02</value></Data></ExtendedData>
<Point><altitudeMode>absolute</altitudeMode><coordinates>44.5,40.2,1000</coordinates></Point></Placemark>
<Placemark><name>edge</name><styleUrl>#r</styleUrl>
<LineString><coordinates>44.5,40.2 44.501,40.2 44.501,40.201</coordinates></LineString></Placemark>
<Placemark><name>field</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
44.5,40.2 44.502,40.2 44.502,40.202 44.5,40.202 44.5,40.2</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>44.5005,40.2005 44.501,40.2005 44.501,40.201 44.5005,40.201 44.5005,40.2005
</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>
</Folder></Document></kml>"""


def test_kml_kmz_to_utm_project(tmp_path):
    from photomesh.georef import utm_forward
    p = tmp_path / "a.kml"; p.write_text(KML, encoding="utf-8")
    k = tmp_path / "a.kmz"
    with zipfile.ZipFile(k, "w") as z:
        z.writestr("doc.kml", KML)
    for path in (p, k):
        ss, crs, notes = S.read_any(str(path))
        assert crs.kind == "wgs84" and len(ss["shapes"]) == 3
        assert ss["layers"][0]["name"] == "Survey"
        pt = next(s for s in ss["shapes"] if s["type"] == "point")
        assert pt["label"] == "GCP 7" and pt["has_z"] and pt["attrs"]["acc"] == "0.02"
        ln = next(s for s in ss["shapes"] if s["type"] == "polyline")
        assert not ln["has_z"] and tuple(ln.get("color", ())) == (255, 0, 0)
        fr = S.Frame("utm", 38, False, [500000, 4400000, 0])
        S.transform_set(ss, fr, crs)
        E, N = utm_forward(np.array([40.2]), np.array([44.5]), 38, False)
        assert np.allclose(pt["parts"][0][0], [E[0], N[0], 1000], atol=1e-6)
        pol = next(s for s in ss["shapes"] if s["type"] == "polygon")
        a = S.measure(pol)["Area 2D"]
        assert 160 * 220 * 0.8 < a < 175 * 225 * 1.0      # ~0.002 deg x 0.002 deg minus the hole
    # a local project cannot take WGS 84 shapes
    fl = S.Frame("local", offset=OFF)
    assert not fl.can_transform(S.SrcCRS("wgs84"))
    with pytest.raises(S.ShapeError):
        fl.from_src(np.zeros((1, 3)), S.SrcCRS("wgs84"))


def test_geojson_roundtrip_world_and_wgs84(tmp_path):
    ss = _sample_set()
    fr = S.Frame("utm", 38, False, OFF)
    S.export(ss, str(tmp_path / "w.geojson"), "GeoJSON", fr, log=QUIET)
    d = json.load(open(tmp_path / "w.geojson"))
    assert "32638" in json.dumps(d.get("crs"))
    r, crs, _n = S.read_any(str(tmp_path / "w.geojson"))
    assert crs.kind == "utm" and crs.zone == 38 and len(r["shapes"]) == 4
    pol = next(s for s in r["shapes"] if s["type"] == "polygon")
    assert pol["holes"] == [False, True]
    S.export(ss, str(tmp_path / "g.geojson"), "GeoJSON", fr, S.SrcCRS("wgs84"), log=QUIET)
    r2, crs2, _n = S.read_any(str(tmp_path / "g.geojson"))
    assert crs2.kind == "wgs84"
    lon = r2["shapes"][0]["parts"][0][0][0]
    assert 40 < lon < 46                                       # zone 38 -> 42..48 E (west of the zone edge ok)
    S.transform_set(r2, fr, crs2)
    assert np.allclose(r2["shapes"][0]["parts"][0][0], [26542.098, 23371.444, 1321.471], atol=1e-3)
    # local project: photomesh_crs member round trip
    fl = S.Frame("local", offset=OFF, crs_text="Local Coordinates (m)")
    S.export(ss, str(tmp_path / "l.geojson"), "GeoJSON", fl, log=QUIET)
    r3, crs3, _n = S.read_any(str(tmp_path / "l.geojson"))
    assert crs3.kind in ("local", "project") and fl.can_transform(crs3)


def test_csv_points(tmp_path):
    p = tmp_path / "gcp.txt"
    p.write_text("# CoordinateSystem: Local Coordinates (m)\n#Label,X,Y,Z\n1,26542.098,23371.444,1321.471\n"
                 "2,26600.5,23100.25,1310\n", encoding="utf-8")
    ss, crs, notes = S.read_any(str(p))
    assert [s["label"] for s in ss["shapes"]] == ["1", "2"] and crs.kind in ("local", "project")
    assert np.allclose(ss["shapes"][1]["parts"][0][0], [26600.5, 23100.25, 1310])
    q = tmp_path / "ll.csv"
    q.write_text("name;lon;lat\nA;44.5;40.2\n", encoding="utf-8")
    ss, crs, _n = S.read_any(str(q))
    assert crs.kind == "wgs84" and not ss["shapes"][0]["has_z"]


def test_crs_parsing():
    assert S.SrcCRS.parse("EPSG:32638").zone == 38
    c = S.SrcCRS.parse('PROJCS["WGS_1984_UTM_Zone_38N",GEOGCS["GCS_WGS_1984"]]')
    assert c.kind == "utm" and c.zone == 38 and not c.south
    assert S.SrcCRS.parse("EPSG:4326").kind == "wgs84"
    assert S.SrcCRS.parse('LOCAL_CS["Local Coordinates (m)"]').kind == "local"
    assert S.SrcCRS.parse("WGS 84 / UTM zone 33S").south


# ------------------------------------------------------------------ coordinates
def test_offset_local_world():
    fr = S.Frame("local", offset=OFF)
    W = np.array([[26542.098, 23371.444, 1321.471]])
    assert np.allclose(fr.to_local(W), W - OFF) and np.allclose(fr.to_world(fr.to_local(W)), W)
    ss = S.new_set(); li = S.add_layer(ss, "x")
    ss["shapes"].append(S.make_shape(li, "point", [W - OFF]))
    S.transform_set(ss, fr, S.SrcCRS("project"), add_offset=True)       # file held model coordinates
    assert np.allclose(ss["shapes"][0]["parts"][0], W)


def test_far_away_warning_hints():
    proj = (OFF[:2] - 200, OFF[:2] + 200)
    far, msg, hint = S.far_check((np.array([10, 20]), np.array([60, 80])), proj, OFF)
    assert far and hint == "offset" and "offset" in msg
    far, msg, hint = S.far_check((np.array([44.5, 40.2]), np.array([44.6, 40.3])), proj, np.zeros(3))
    assert far and hint == "degrees" and "WGS 84" in msg
    far, msg, hint = S.far_check((np.array([5e5, 4.4e6]), np.array([5.1e5, 4.41e6])), proj, OFF)
    assert far and hint is None and "coordinate system" in msg
    assert not S.far_check((OFF[:2] - 100, OFF[:2] + 50), proj, OFF)[0]
    assert not S.far_check((OFF[:2] + 600, OFF[:2] + 700), proj, OFF)[0]      # nearby (within 1 km)


# ------------------------------------------------------------------ draping
def _dem(path, f, x0=26400.0, y1=23200.0, px=0.5, W=400, H=400):
    xs = x0 + (np.arange(W) + 0.5) * px
    ys = y1 - (np.arange(H) + 0.5) * px
    GX, GY = np.meshgrid(xs, ys)
    z = f(GX, GY).astype(np.float32)
    z[:5, :5] = -32767.0
    w = gt.TiledTiffWriter(path, [(W, H)], 1, np.float32, 256, "Deflate", nodata=-32767.0, geo=(x0, y1, px, px),
                           crs='LOCAL_CS["Local Coordinates (m)"]')
    w.write_block(0, 0, 0, z[..., None]); w.close()
    return path


def plane(X, Y):
    return 1300.0 + 0.1 * (X - 26400.0) + 0.05 * (Y - 23000.0)


def test_drape_on_dem_and_fixed(tmp_path):
    smp = S.HeightSampler.from_raster(_dem(str(tmp_path / "d.tif"), plane))
    assert smp(np.array([26450.3]), np.array([23100.7]))[0] == pytest.approx(plane(26450.3, 23100.7), abs=1e-3)
    assert np.isnan(smp(np.array([26400.5]), np.array([23199.5]))[0])          # nodata corner
    assert np.isnan(smp(np.array([0.0]), np.array([0.0]))[0])                   # outside
    ss = S.new_set(); li = S.add_layer(ss, "x")
    line = np.array([[26410.0, 23010.0, 0], [26590.0, 23190.0, 0]])
    ss["shapes"].append(S.make_shape(li, "polyline", [line], has_z=False))
    ss["shapes"].append(S.make_shape(li, "point", [[[26500.0, 23100.0, 5.0]]], has_z=True))
    n = S.apply_heights(ss, "drape", smp, log=QUIET)
    assert n == 1                                                              # only the shape without Z
    s = ss["shapes"][0]
    assert s["attached"] and np.allclose(s["parts"][0][:, 2], plane(line[:, 0], line[:, 1]), atol=1e-3)
    d = s["dparts"][0]
    assert len(d) > 100 and np.allclose(d[:, 2], plane(d[:, 0], d[:, 1]), atol=1e-3)
    assert ss["shapes"][1]["parts"][0][0, 2] == 5.0
    S.apply_heights(ss, "drape", smp, only_without_z=False, log=QUIET)
    assert ss["shapes"][1]["parts"][0][0, 2] == pytest.approx(plane(26500, 23100), abs=1e-3)
    S.apply_heights(ss, "fixed", None, fixed_z=1234.5, only_without_z=False, log=QUIET)
    assert np.all(ss["shapes"][0]["parts"][0][:, 2] == 1234.5) and "dparts" not in ss["shapes"][0]


def test_drape_on_mesh(tmp_path):
    pytest.importorskip("open3d")
    from photomesh.plyio import save_ply
    # model frame mesh (local = world - offset) of the plane z = 0.2 x
    g = np.linspace(-50, 50, 21)
    X, Y = np.meshgrid(g, g)
    V = np.column_stack([X.ravel(), Y.ravel(), 0.2 * X.ravel()]).astype(np.float32)
    F = []
    for r in range(20):
        for c in range(20):
            a = r * 21 + c
            F += [[a, a + 1, a + 22], [a, a + 22, a + 21]]
    p = str(tmp_path / "m.ply"); save_ply(p, V, None, np.array(F, np.int32))
    smp = S.HeightSampler.from_mesh(p, OFF)
    z = smp(np.array([OFF[0] + 10.0, OFF[0] + 500]), np.array([OFF[1] - 5.0, OFF[1]]))
    assert z[0] == pytest.approx(OFF[2] + 2.0, abs=1e-4) and np.isnan(z[1])
    assert smp.scene is not None


# ------------------------------------------------------------------ measurement
def test_measure_area_perimeter_volume():
    bump_r, bump_h = 4.0, 2.0

    def surf(X, Y):
        z = plane(X, Y)
        return z + np.where(np.hypot(X - 26500, Y - 23100) < bump_r, bump_h, 0.0)
    smp = S.HeightSampler("raster", surf, 0.05, "DEM")
    sq = S._close([[26480, 23080, 0], [26520, 23080, 0], [26520, 23120, 0], [26480, 23120, 0]])
    s = S.make_shape(0, "polygon", [sq], has_z=False)
    S.apply_heights({"shapes": [s]}, "drape", smp, log=QUIET)
    m = S.measure(s, smp)
    assert m["Area 2D"] == pytest.approx(1600.0) and m["Perimeter 2D"] == pytest.approx(160.0)
    assert m["Perimeter 3D"] > 160.0
    slope = math.sqrt(1 + 0.1 ** 2 + 0.05 ** 2)
    a3 = m["Area 3D (surface)"]                    # sloped plane + the bump's side wall
    assert 1600 * slope * 0.995 < a3 < 1600 * slope + 1.3 * 2 * math.pi * bump_r * bump_h
    assert m["Volume above plane (cut)"] == pytest.approx(math.pi * bump_r ** 2 * bump_h, rel=0.03)
    assert m["Volume below plane (fill)"] < 1.0
    ln = S.make_shape(0, "polyline", [np.array([[0, 0, 0], [3, 4, 12.0]])])
    ml = S.measure(ln)
    assert ml["Length 2D"] == pytest.approx(5.0) and ml["Length 3D"] == pytest.approx(13.0)
    assert S.measure(S.make_shape(0, "point", [[[1, 2, 3]]]))["Z"] == 3.0


# ------------------------------------------------------------------ photo projection
def _cam(model=1, params=None, W=1000, H=800):
    if params is None:
        params = [1000.0, 1000.0, 500.0, 400.0]
    return cio.Camera(model, W, H, np.array(params, float))


def test_known_point_projects_to_known_pixel():
    """Synthetic pinhole camera at the origin looking down +Z: X = (1, 2, 10)
    -> u = f*x/z + cx = 600, v = 600. Then a rotated / translated camera."""
    cam = _cam()
    R, t = np.eye(3), np.zeros(3)
    uv, ok = S.project_points(np.array([[1.0, 2.0, 10.0]]), cam, R, t)
    assert list(ok) == [0] and np.allclose(uv[0], [600.0, 600.0])
    # camera at C looking at the world point; pixel of the principal point
    from photomesh.colmap_io import qvec_to_rot
    q = np.array([0.9, 0.1, -0.2, 0.3]); q /= np.linalg.norm(q)
    R = qvec_to_rot(q); C = np.array([5.0, -3.0, 2.0])
    t = -R @ C
    X = C + R.T @ np.array([0.0, 0.0, 7.0])                 # on the optical axis, 7 m away
    uv, ok = S.project_points(X[None], cam, R, t)
    assert np.allclose(uv[0], [500.0, 400.0], atol=1e-9)
    # pixel (0.5, 0.5) convention: the centre of the top-left pixel
    Xc = np.array([(0.5 - 500) / 1000 * 10, (0.5 - 400) / 1000 * 10, 10.0])
    uv, ok = S.project_points(Xc[None], cam, np.eye(3), np.zeros(3))
    assert np.allclose(uv[0], [0.5, 0.5])


def test_projection_matches_colmap_with_distortion():
    cam = _cam(4, [1000, 1010, 505, 398, -0.12, 0.03, 0.001, -0.0005])     # OPENCV
    P = np.array([[0.3, -0.2, 4.0], [-1.0, 0.7, 5.0], [1.6, 1.2, 4.5]])
    uv, ok = S.project_points(P, cam, np.eye(3), np.zeros(3))
    ref, _z = cio.project(cam, np.eye(3), np.zeros(3), P[ok])
    assert np.allclose(uv, ref, atol=1e-9)
    # a long straight 3D line is densified so that its image bends with the lens:
    # every output vertex is the distorted image of a point of the 3D segment
    A, B = np.array([-1.5, -1.0, 4.0]), np.array([1.5, -1.0, 4.0])
    lines = S.project_polylines([np.array([A, B])], cam, np.eye(3), np.zeros(3), px_step=5)
    L = lines[0]
    assert len(L) > 100
    xs = np.linspace(-1.5, 1.5, 2001)
    Q = np.column_stack([xs, np.full_like(xs, -1.0), np.full_like(xs, 4.0)])
    ok = (np.abs(Q[:, 0] / 4) < 10)
    curve, _z = cio.project(cam, np.eye(3), np.zeros(3), Q[ok])
    d = np.min(np.linalg.norm(L[:, None, :] - curve[None], axis=2), axis=1)
    assert d.max() < 1.0
    mid = L[len(L) // 2]
    straight = (L[0] + L[-1]) / 2
    assert abs(mid[1] - straight[1]) > 3.0              # barrel distortion bends the line


def test_clip_behind_camera_and_frame():
    cam = _cam()
    # from behind the camera (z = -5) to in front (z = 10): only the visible part, no wrap-around
    lines = S.project_polylines([np.array([[0.1, 0.1, -5.0], [0.1, 0.1, 10.0]])], cam, np.eye(3), np.zeros(3))
    for L in lines:
        assert np.all(np.isfinite(L))
        assert np.all((L[:, 0] > -100) & (L[:, 0] < 1100) & (L[:, 1] > -100) & (L[:, 1] < 900))
    # entirely behind -> nothing
    assert S.project_polylines([np.array([[0, 0, -1.0], [1, 1, -2.0]])], cam, np.eye(3), np.zeros(3)) == []
    uv, ok = S.project_points(np.array([[0, 0, -3.0], [100, 0, 1.0]]), cam, np.eye(3), np.zeros(3))
    assert len(ok) == 0
    # a line much wider than the frame is cut at the frame (small margin)
    lines = S.project_polylines([np.array([[-50.0, 0, 10], [50.0, 0, 10]])], cam, np.eye(3), np.zeros(3))
    L = np.vstack(lines)
    assert L[:, 0].min() > -40 and L[:, 0].max() < 1040 and np.allclose(L[:, 1], 400.0)
    # polygon closed automatically
    sq = np.array([[-0.5, -0.5, 5], [0.5, -0.5, 5], [0.5, 0.5, 5], [-0.5, 0.5, 5]])
    L = S.project_polylines([sq], cam, np.eye(3), np.zeros(3), closed=True)[0]
    assert np.allclose(L[0], L[-1])


def test_occlusion_by_mesh():
    o3d = pytest.importorskip("open3d")
    cam = _cam()
    sc = o3d.t.geometry.RaycastingScene()
    V = np.array([[-1, -1, 5], [1, -1, 5], [1, 1, 5], [-1, 1, 5]], np.float32)      # wall at z = 5
    sc.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(np.array([[0, 1, 2], [0, 2, 3]], np.uint32)))
    P = np.array([[0.0, 0.0, 10.0], [3.0, 0.0, 10.0], [0.2, 0.1, 4.0]])   # hidden, beside the wall, in front
    uv, ok = S.project_points(P, cam, np.eye(3), np.zeros(3), scene=sc)
    assert list(ok) == [1, 2]
    lines = S.project_polylines([np.array([[-4.0, 0, 10], [4.0, 0, 10]])], cam, np.eye(3), np.zeros(3), scene=sc)
    assert len(lines) == 2                                  # the middle is behind the wall


def test_full_opencv_rational_and_fisheye_distortion():
    p = [800, 800, 400, 300, 0.1, 0.01, 0.001, 0.002, 0.001, 0.05, 0.002, 0.0]
    cam = cio.Camera(6, 800, 600, np.array(p, float))
    x, y = np.array([0.3, -0.25]), np.array([0.2, 0.1])
    xd, yd = cio.distort(cam, x, y)
    r2 = x * x + y * y
    rad = (1 + 0.1 * r2 + 0.01 * r2 ** 2 + 0.001 * r2 ** 3) / (1 + 0.05 * r2 + 0.002 * r2 ** 2)
    ex = x * rad + 2 * 0.001 * x * y + 0.002 * (r2 + 2 * x * x)
    assert np.allclose(xd, ex)
    uv = np.column_stack([800 * xd + 400, 800 * yd + 300])
    xu, yu = cio.undistort_points(cam, uv, 30)
    assert np.allclose(xu, x, atol=1e-8) and np.allclose(yu, y, atol=1e-8)
    fe = cio.Camera(5, 800, 600, np.array([500, 500, 400, 300, 0.05, 0.01, 0, 0], float))
    xd, yd = cio.distort(fe, np.array([0.5]), np.array([0.0]))
    th = math.atan(0.5)
    assert xd[0] == pytest.approx(th * (1 + 0.05 * th ** 2 + 0.01 * th ** 4))


# ------------------------------------------------------------------ GUI
def _gui_chunk(window, tmp_path):
    """Georeferenced chunk (local CRS, big offset) with a DEM, one photo and a
    COLMAP model whose camera looks straight down at the site."""
    window.project.path = str(tmp_path / "proj.pmsx")      # processing needs a saved project
    from PIL import Image
    from photomesh.project import Camera
    ch = window.chunk
    wd = tmp_path / "work"; (wd / "sparse" / "0").mkdir(parents=True)
    ch.workdir = str(wd)
    photo = str(tmp_path / "IMG_0001.JPG")
    Image.new("RGB", (1000, 800), (90, 110, 90)).save(photo)
    # camera 60 m above local (100, 100, 0), looking down (-Z), image x = +X, image y = -Y
    C = np.array([100.0, 100.0, 60.0])
    R = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])
    t = -R @ C
    cam = cio.Camera(1, 1000, 800, np.array([1000.0, 1000.0, 500.0, 400.0]))
    img = cio.Image("IMG_0001.JPG", 1, cio.rot_to_qvec(R), t, np.zeros((0, 2)), np.zeros(0, np.int64))
    pts = cio.Points(np.array([1], np.uint64), np.array([[100.0, 100, 0]]), np.zeros((1, 3), np.uint8),
                     np.zeros(1), np.array([0, 0]), np.zeros(0, np.uint32), np.zeros(0, np.uint32))
    cio.write_model(str(wd / "sparse" / "0"), cio.Model({1: cam}, {1: img}, pts))
    ch.cameras = [Camera(path=photo, label="IMG_0001.JPG", aligned=True, est=C.tolist(), rot=R.tolist(),
                         width=1000, height=800)]
    ch.tie_point_count = 1
    ch.georef = {"offset": OFF.tolist(), "crs": "Local Coordinates (m)", "crs_kind": "local"}
    ch.crs = "Local Coordinates (m)"
    dem = _dem(str(tmp_path / "dem.tif"), lambda X, Y: np.full_like(X, OFF[2]), x0=OFF[0], y1=OFF[1] + 200,
               px=0.5, W=400, H=400)
    ch.dem = {"path": dem, "bounds": [OFF[0], OFF[1], OFF[0] + 200, OFF[1] + 200], "pixel_size": 0.5}
    window.refresh_all(); window.reload_view(); window.wait_view_loaded()
    return ch


def _site_files(tmp_path):
    """World-coordinate SHP (GCP points + site polygon, no Z) like a surveyor would deliver."""
    ss = S.new_set()
    g = S.add_layer(ss, "GCP"); p = S.add_layer(ss, "Site")
    gcp = [("G1", 100.0, 100.0), ("G2", 120.0, 90.0), ("G3", 80.0, 115.0)]
    for lab, x, y in gcp:
        ss["shapes"].append(S.make_shape(g, "point", [[[OFF[0] + x, OFF[1] + y, OFF[2]]]], {"label": lab}, lab))
    ring = S._close([[OFF[0] + 70, OFF[1] + 80, 0], [OFF[0] + 130, OFF[1] + 80, 0], [OFF[0] + 130, OFF[1] + 120, 0],
                     [OFF[0] + 70, OFF[1] + 120, 0]])
    ss["shapes"].append(S.make_shape(p, "polygon", [ring], {"name": "site"}, "site", has_z=False))
    S.write_shp([s for s in ss["shapes"] if s["type"] == "point"], str(tmp_path / "gcp.shp"), ss["layers"])
    S.write_shp([s for s in ss["shapes"] if s["type"] == "polygon"], str(tmp_path / "site.shp"), ss["layers"])
    # local (model) coordinates of the same points, as a file without the offset
    loc = [S.make_shape(0, "point", [[[x, y, 0.0]]], {"label": lab}, lab) for lab, x, y in gcp]
    S.write_shp(loc, str(tmp_path / "local.shp"), [ss["layers"][0]])
    return str(tmp_path / "gcp.shp"), str(tmp_path / "site.shp"), str(tmp_path / "local.shp")


def _rows(window):
    out = []
    for it in window.workspace._iter():
        d = it.data(0, 256)
        out.append((it.text(0), tuple(d) if d else None))
    return out


def test_gui_import_show_save_load_export_remove(window, no_dialogs, tmp_path):
    ch = _gui_chunk(window, tmp_path)
    gcp, site, local = _site_files(tmp_path)
    r = window.import_shapes(gcp, {"layer": "GCP", "z_mode": "keep"})
    assert r and r["shapes"] == 3 and not r["far"]
    r = window.import_shapes(site, {"layer": "Site", "z_mode": "drape_missing", "surface": "DEM"})
    assert r["shapes"] == 1
    sset = window._shape_set()
    assert [L["name"] for L in sset["layers"]] == ["GCP", "Site"]
    pol = sset["shapes"][3]
    assert pol["attached"] and np.allclose(pol["parts"][0][:, 2], OFF[2])      # draped on the DEM
    texts = [t for t, _d in _rows(window)]
    assert "Shapes (4)" in texts and "GCP (3)" in texts and "Site (1)" in texts and "G2" in texts
    # stored in the project folder? (unsaved project -> working folder); views got them
    assert os.path.exists(ch.shapes["path"]) and ch.shapes["count"] == 4
    assert len(window.ortho_view.shapes) == 4
    assert window.model_view._shape_data and sum(len(g["lines"]) for g in window.model_view._shape_data) == 1
    lp = [g for g in window.model_view._shape_data if g["points"] is not None][0]["points"]
    assert np.allclose(lp[0], [100, 100, 0])                                   # model frame = world - offset
    # photo overlay: G1 is right below the camera -> principal point
    window.open_photo(0)
    items = window.photo_view.shapes
    pts = [p for it in items for p in it["points"]]
    g1 = next(p for p in pts if p[2] == "G1")
    assert g1[0] == pytest.approx(500.0, abs=1e-6) and g1[1] == pytest.approx(400.0, abs=1e-6)
    g2 = next(p for p in pts if p[2] == "G2")          # +20 m X, -10 m Y at 60 m -> (833.3, 566.7)
    assert g2[0] == pytest.approx(500 + 1000 * 20 / 60, abs=1e-6) and g2[1] == pytest.approx(400 + 1000 * 10 / 60, abs=1e-6)
    lines = [L for it in items for L in it["lines"]]
    assert lines and all(np.all((L[:, 0] > -40) & (L[:, 0] < 1040)) for L in lines)
    assert window.photo_view._shape_items
    # layer visibility: hide the GCP layer -> gone from the photo / ortho; toggle the whole layer
    window._workspace_action("shape_layer:0:GCP", 0)
    assert not [p for it in window.photo_view.shapes for p in it["points"]]
    assert len(window.ortho_view.shapes) == 1
    window._workspace_action("shape_layer:1:GCP", 0)
    window.set_layer_visible("shapes", False)
    assert window.photo_view.shapes == [] and not window.ortho_view.vis["shapes"]
    window.set_layer_visible("shapes", True)
    assert window.photo_view.shapes
    # properties / measurement
    window._show_properties({"kind": "shape", "chunk": 0, "index": 3})
    m = window.measure_shape(3)
    assert m["Area 2D"].startswith("2,400.000") and "Volume net" in m
    # ortho click selection
    ov = window.ortho_view; ov.resize(600, 500); ov.fit()
    x, y = ov.to_screen(OFF[0] + 120, OFF[1] + 90)
    assert ov.shape_at(x, y) == 1
    # save / load
    pm = str(tmp_path / "p.pmsx")
    window.project.save(pm)
    from photomesh.project import Project
    p2 = Project.load(pm)
    assert p2.chunk.shapes["count"] == 4 and p2.chunk.shapes["path"] == ch.shapes["path"]
    window.project = p2; window.refresh_all(); window.reload_view(); window.wait_view_loaded()
    assert len(window.ortho_view.shapes) == 4 and "Shapes (4)" in [t for t, _d in _rows(window)]
    # export in world coordinates, re-read
    for fmt in ("shp", "dxf", "geojson"):
        files = window.export_shapes(str(tmp_path / f"out.{fmt}"), {"fmt": fmt})
        main = [f for f in files if f.endswith((".shp", ".dxf", ".geojson"))]
        got = sum(len(S.read_any(f)[0]["shapes"]) for f in main)
        assert got == 4
        pts = [s for f in main for s in S.read_any(f)[0]["shapes"] if s["type"] == "point"]
        assert np.allclose(sorted(p["parts"][0][0, 0] for p in pts), sorted(OFF[0] + v for v in (100, 120, 80)))
    # far-away file (local coordinates, offset forgotten) -> warning, 'Add Project Offset'
    r = window.import_shapes(local, {"layer": "loc", "z_mode": "keep"}, far_action="offset")
    assert r["far"] and r["hint"] == "offset"
    s = window._shape_set()["shapes"][-1]
    assert np.allclose(s["parts"][0][0, :2], OFF[:2] + [80, 115])
    assert window.import_shapes(local, {"layer": "x"}, far_action="cancel") is None
    # replace / remove layer / remove all
    window.remove_shape_layer("loc")
    assert window.chunk.shapes["count"] == 4
    assert window.remove_shapes(confirm=False) and not window.chunk.shapes
    assert window.ortho_view.shapes == [] and window.photo_view.shapes == []
    assert not any(t.startswith("Shapes (") for t, _d in _rows(window))


def test_gui_menus_and_toolbar(window):
    for name in ("File/Import Shapes...", "File/Export Shapes...", "Tools/Remove Shapes", "Tools/Measure Shape...",
                 "Model/Show Shapes"):
        assert name in window.actions, name
    a = window.layer_action("shapes")
    assert a.isCheckable() and not a.icon().isNull()
    tb = [x for x in window.findChildren(type(window.addToolBar("x")))]
    assert any(a in t.actions() for t in tb)
    window.set_layer_visible("shapes", False)
    assert all(not m.isChecked() for m in window._shape_mirrors)
    window.set_layer_visible("shapes", True)
    assert all(m.isChecked() for m in window._shape_mirrors)


def test_kml_into_local_project_warns(window, no_dialogs, tmp_path):
    ch = window.chunk
    ch.georef = {"offset": OFF.tolist(), "crs": "Local Coordinates (m)", "crs_kind": "local"}
    ch.crs = "Local Coordinates (m)"
    p = tmp_path / "a.kml"; p.write_text(KML, encoding="utf-8")
    assert window.import_shapes(str(p), {"src": "file", "_headless": True}) is None
    assert not ch.shapes


DATA = os.path.join(os.path.dirname(__file__), "data", "shapes")


def test_reads_files_written_by_gdal_and_ezdxf():
    """Reference files written by GDAL/OGR (fiona) and ezdxf (R2018), not by us."""
    ss, crs, _n = S.read_any(os.path.join(DATA, "fi.shp"))
    s = ss["shapes"][0]
    assert crs.kind == "utm" and crs.zone == 38 and s["holes"] == [False, True]
    assert s["attrs"]["name"] == "Ä" and int(s["attrs"]["id"]) == 7 and S.measure(s)["Area 2D"] == pytest.approx(96)
    ss, crs, _n = S.read_any(os.path.join(DATA, "fi.geojson"))
    assert crs.kind == "wgs84" and ss["shapes"][0]["type"] == "polyline"
    ss, crs, _n = S.read_any(os.path.join(DATA, "ez.dxf"))
    kinds = sorted(s["type"] for s in ss["shapes"])
    assert kinds == ["point", "polygon", "polyline", "polyline"]
    pt = next(s for s in ss["shapes"] if s["type"] == "point")
    assert pt["label"] == "T1" and ss["layers"][pt["layer"]]["name"] == "PTS"
    assert tuple(ss["layers"][pt["layer"]]["color"]) == S.aci_rgb(2)


def test_shp_2d_written_when_no_z(tmp_path):
    s = S.make_shape(0, "polygon", [S._close([[0, 0, 0], [5, 0, 0], [5, 5, 0]])], has_z=False)
    S.write_shp([s], str(tmp_path / "f.shp"), [{"name": "L"}])
    assert struct.unpack("<i", open(tmp_path / "f.shp", "rb").read()[32:36])[0] == 5
    r, _c, _n = S.read_shp(str(tmp_path / "f.shp"))
    assert not r["shapes"][0]["has_z"]


def test_dbf_nan_becomes_null(tmp_path):
    s = [S.make_shape(0, "point", [[[1, 2, 3]]], {"e": float("nan")}), S.make_shape(0, "point", [[[1, 2, 3]]], {"e": 0.5})]
    S.write_shp(s, str(tmp_path / "n.shp"), [{"name": "L"}])
    assert b"nan" not in open(tmp_path / "n.dbf", "rb").read()
    r, _c, _n = S.read_shp(str(tmp_path / "n.shp"))
    assert r["shapes"][0]["attrs"]["e"] is None and float(r["shapes"][1]["attrs"]["e"]) == 0.5
