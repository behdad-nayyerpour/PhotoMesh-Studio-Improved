"""Shapes: vector layers (points, polylines, polygons) in the project.

Own pure-python readers / writers (no GDAL / pyshp needed):
  * ESRI Shapefile  .shp + .shx + .dbf (+ .prj, .cpg): Point/Multipoint/PolyLine/
    Polygon in the plain, M and Z variants (MultiPatch rings read as polygons)
  * DXF (ASCII R12 ... 2018): POINT, LINE, LWPOLYLINE, POLYLINE (2D / 3D,
    polyface meshes as faces), 3DFACE, CIRCLE, ARC; layers + ACI / true colours
  * KML / KMZ (WGS 84): Point, LineString, LinearRing, Polygon (+ holes),
    MultiGeometry; Folder names -> layers; ExtendedData -> attributes
  * GeoJSON (RFC 7946 and the old "crs" member), CSV / TXT point lists
Writers: SHP (one file per geometry type), DXF R12, GeoJSON.

Coordinates of a shape set are REAL world coordinates of the project frame
(local + georef offset; for a WGS 84 project the metric UTM frame used by the
georeferencing). Views convert to the model frame with Frame.to_local.
"""
from __future__ import annotations

import colorsys
import csv
import io
import json
import math
import os
import re
import struct
import time
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

FORMAT = "photomesh-shapes/1"
PALETTE = [(255, 210, 40), (60, 200, 255), (255, 90, 90), (120, 230, 120), (255, 140, 0), (200, 120, 255),
           (255, 255, 255), (0, 220, 180), (255, 105, 180), (180, 180, 60)]
SHP_TYPES = {0: "null", 1: "point", 3: "polyline", 5: "polygon", 8: "multipoint",
             11: "point", 13: "polyline", 15: "polygon", 18: "multipoint",
             21: "point", 23: "polyline", 25: "polygon", 28: "multipoint", 31: "multipatch"}


class ShapeError(Exception):
    pass


# =========================================================== CRS helpers
class SrcCRS:
    """Coordinate system of a shape file: 'project' (same as the project),
    'local', 'wgs84', 'utm' (zone, south) or 'proj' (any CRS pyproj knows,
    only when pyproj happens to be installed)."""

    def __init__(self, kind="project", zone=0, south=False, name="", wkt=""):
        self.kind, self.zone, self.south, self.name, self.wkt = kind, int(zone or 0), bool(south), name, wkt

    def __repr__(self):
        return f"SrcCRS({self.describe()})"

    def describe(self):
        if self.kind == "project":
            return "Project coordinate system"
        if self.kind == "wgs84":
            return "WGS 84 (EPSG::4326)"
        if self.kind == "utm":
            return f"WGS 84 / UTM zone {self.zone}{'S' if self.south else 'N'} (EPSG::{self.epsg})"
        if self.kind == "local":
            return self.name or "Local Coordinates (m)"
        return self.name or self.wkt[:60]

    @property
    def epsg(self):
        if self.kind == "wgs84":
            return 4326
        if self.kind == "utm":
            return (32700 if self.south else 32600) + self.zone
        return None

    @classmethod
    def parse(cls, text):
        """From a .prj WKT (OGC or ESRI flavour), 'EPSG:xxxx', a name, or ''."""
        t = (text or "").strip()
        if not t:
            return cls("project")
        u = t.upper().replace("_", " ")
        m = re.search(r"EPSG[:\s\"',]*(\d{4,5})\D*$", u) or re.fullmatch(r"\s*(\d{4,5})\s*", u)
        code = int(m.group(1)) if m else None
        if u.startswith(("LOCAL CS", "LOCALCS")) or u in ("LOCAL", "XYZ") or "LOCAL COORD" in u:
            n = re.search(r'LOCAL.CS\["([^"]+)"', t)
            return cls("local", name=n.group(1) if n else "Local Coordinates (m)", wkt=t)
        mu = re.search(r"UTM ZONE\s*(\d{1,2})\s*([NS])", u)
        if mu and ("WGS 84" in u or "WGS 1984" in u or "WGS84" in u):
            return cls("utm", int(mu.group(1)), mu.group(2) == "S", wkt=t)
        if code and (32601 <= code <= 32660 or 32701 <= code <= 32760):
            return cls("utm", code % 100, code > 32700, wkt=t)
        if code in (4326, 4979):
            return cls("wgs84", wkt=t)
        if u.startswith("GEOGCS") and ("WGS 1984" in u or "WGS 84" in u or "WGS84" in u) and "PROJCS" not in u:
            return cls("wgs84", wkt=t)
        if u.startswith(("PROJCS", "GEOGCS", "PROJCRS", "GEOGCRS", "COMPD")) or code:
            simple = re.fullmatch(r"\s*(EPSG:)?\d+\s*", t, re.I)
            return cls("proj", name=_wkt_root_name(t) or t[:60], wkt=f"EPSG:{code}" if simple else t)
        return cls("project", name=t)


def _wkt_root_name(t):
    m = re.match(r'\s*[A-Z_]+\["([^"]+)"', t or "")
    return m.group(1) if m else ""


class Frame:
    """The project's world frame: kind local | utm (WGS 84 projects use the
    UTM zone of the georeferencing) and the offset (world = local + offset)."""

    def __init__(self, kind="local", zone=0, south=False, offset=None, crs_text="", georeferenced=False):
        self.kind, self.zone, self.south = kind, int(zone or 0), bool(south)
        self.offset = np.asarray(offset if offset is not None else [0.0, 0.0, 0.0], float)
        self.crs_text, self.georeferenced = crs_text, georeferenced

    @classmethod
    def from_chunk(cls, ch):
        from .georef import CRS
        gr = getattr(ch, "georef", None) or {}
        crs_text = gr.get("crs") or getattr(ch, "crs", "") or ""
        kind, zone, south = gr.get("crs_kind") or "", 0, False
        try:
            c = CRS.parse(getattr(ch, "crs", "") or crs_text)
            kind = kind or c.kind
            zone, south = c.zone, c.south
        except Exception:  # noqa: BLE001
            kind = kind or "local"
        if kind == "wgs84":
            z = gr.get("zone")
            if z:
                zone, south = (int(z[0]), bool(z[1])) if isinstance(z, (list, tuple)) else (int(z), False)
            kind = "utm"
        off = gr.get("offset")
        return cls(kind or "local", zone, south, off if off is not None else [0, 0, 0], crs_text, bool(gr))

    def describe(self):
        if self.kind == "utm":
            return f"WGS 84 / UTM zone {self.zone}{'S' if self.south else 'N'}"
        return self.crs_text or "Local Coordinates (m)"

    @property
    def epsg(self):
        return ((32700 if self.south else 32600) + self.zone) if self.kind == "utm" and self.zone else None

    def to_local(self, P):
        return np.asarray(P, float) - self.offset

    def to_world(self, P):
        return np.asarray(P, float) + self.offset

    def can_transform(self, src):
        if src.kind == "project":
            return True
        if src.kind == "local":
            return self.kind == "local"
        if self.kind == "local":
            return False
        return src.kind in ("wgs84", "utm") or _have_pyproj()

    def from_src(self, P, src):
        """(n,3) coordinates in `src` -> world coordinates of this frame."""
        P = np.array(P, float, copy=True)
        if not len(P) or src.kind == "project" or (src.kind == "local" and self.kind == "local"):
            return P
        if self.kind == "local":
            raise ShapeError(f"The shapes are in {src.describe()} but the project uses a local coordinate "
                             "system, so they cannot be transformed (georeference the project in WGS 84 / UTM "
                             "first, or import them as project coordinates)")
        if src.kind == "utm" and (src.zone, src.south) == (self.zone, self.south):
            return P
        from .georef import utm_forward, utm_inverse
        if src.kind == "wgs84":
            lon, lat = P[:, 0], P[:, 1]
        elif src.kind == "utm":
            lat, lon = utm_inverse(P[:, 0], P[:, 1], src.zone, src.south)
        else:
            lon, lat = _pyproj_to_wgs84(P, src)
        E, N = utm_forward(lat, lon, self.zone, self.south)
        return np.column_stack([E, N, P[:, 2]])

    def to_dst(self, P, dst):
        """World coordinates of this frame -> `dst` (for export)."""
        P = np.array(P, float, copy=True)
        if not len(P) or dst.kind == "project" or (dst.kind == "local" and self.kind == "local"):
            return P
        if self.kind == "local":
            raise ShapeError("Shapes of a local project cannot be exported in another coordinate system")
        if dst.kind == "utm" and (dst.zone, dst.south) == (self.zone, self.south):
            return P
        from .georef import utm_forward, utm_inverse
        lat, lon = utm_inverse(P[:, 0], P[:, 1], self.zone, self.south)
        if dst.kind == "wgs84":
            return np.column_stack([lon, lat, P[:, 2]])
        if dst.kind == "utm":
            E, N = utm_forward(lat, lon, dst.zone, dst.south)
            return np.column_stack([E, N, P[:, 2]])
        raise ShapeError(f"Cannot export to {dst.describe()}")

    def prj(self):
        """WKT for .prj files (None for local systems)."""
        if self.kind == "utm" and self.zone:
            return utm_wkt(self.zone, self.south)
        s = (self.crs_text or "").strip()
        return s if s.upper().startswith(("PROJCS", "GEOGCS")) else None


def utm_wkt(zone, south=False):
    code = (32700 if south else 32600) + zone
    return ('PROJCS["WGS 84 / UTM zone %d%s",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
            '298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
            'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
            'AUTHORITY["EPSG","4326"]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
            'PARAMETER["central_meridian",%d],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
            'PARAMETER["false_northing",%d],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],'
            'AXIS["Northing",NORTH],AUTHORITY["EPSG","%d"]]'
            % (zone, "S" if south else "N", zone * 6 - 183, 10000000 if south else 0, code))


WGS84_WKT = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
             'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]')


def _have_pyproj():
    try:
        import pyproj  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _pyproj_to_wgs84(P, src):
    try:
        from pyproj import CRS as PC, Transformer
    except Exception as e:  # noqa: BLE001
        raise ShapeError(f"Coordinate system '{src.describe()}' is not supported without pyproj "
                         "(supported: WGS 84, WGS 84 / UTM, project / local)") from e
    tr = Transformer.from_crs(PC.from_user_input(src.wkt), PC.from_epsg(4326), always_xy=True)
    lon, lat = tr.transform(P[:, 0], P[:, 1])
    return np.asarray(lon), np.asarray(lat)


# ========================================================= data model
def new_set():
    return {"format": FORMAT, "layers": [], "shapes": []}


def add_layer(sset, name, color=None, source="", fmt="", crs="", width=2.0):
    names = {l["name"] for l in sset["layers"]}
    base, k = name or "Layer", 2
    name = base
    while name in names:
        name = f"{base} ({k})"; k += 1
    col = list(color) if color is not None else list(PALETTE[len(sset["layers"]) % len(PALETTE)])
    sset["layers"].append({"name": name, "color": [int(c) for c in col[:3]], "visible": True, "width": float(width),
                           "source": source, "format": fmt, "crs": crs})
    return len(sset["layers"]) - 1


def make_shape(layer, kind, parts, attrs=None, label="", holes=None, has_z=True, color=None):
    parts = [np.asarray(p, float).reshape(-1, 3) for p in parts if len(p)]
    s = {"layer": layer, "type": kind, "parts": parts, "attrs": dict(attrs or {}), "label": str(label or ""),
         "has_z": bool(has_z)}
    if kind == "polygon":
        s["holes"] = [bool(h) for h in holes] if holes is not None else [False] * len(parts)
    if color is not None:
        s["color"] = [int(c) for c in color[:3]]
    return s


def merge(dst, src):
    """Append the layers and shapes of `src` to `dst` (layer names made unique)."""
    remap = {}
    for i, l in enumerate(src["layers"]):
        j = add_layer(dst, l["name"], l["color"], l.get("source", ""), l.get("format", ""), l.get("crs", ""),
                      l.get("width", 2.0))
        dst["layers"][j]["visible"] = l.get("visible", True)
        remap[i] = j
    for s in src["shapes"]:
        dst["shapes"].append(dict(s, layer=remap[s["layer"]]))
    return dst


def remove_layer(sset, li):
    sset["shapes"] = [dict(s, layer=s["layer"] - (s["layer"] > li)) for s in sset["shapes"] if s["layer"] != li]
    del sset["layers"][li]
    return sset


def all_vertices(sset, shapes=None):
    ps = [p for s in (shapes if shapes is not None else sset["shapes"]) for p in s["parts"]]
    return np.vstack(ps) if ps else np.zeros((0, 3))


def bounds(sset, shapes=None):
    V = all_vertices(sset, shapes)
    if not len(V):
        return None
    return V.min(0), V.max(0)


def shape_name(s, i):
    return s.get("label") or f"{s['type'].capitalize()} {i + 1}"


def to_json(sset):
    def enc(s):
        d = {k: v for k, v in s.items() if k not in ("parts", "dparts")}
        d["parts"] = [np.round(p, 6).tolist() for p in s["parts"]]
        if s.get("dparts") is not None:
            d["dparts"] = [np.round(p, 4).tolist() for p in s["dparts"]]
        return d
    out = {k: v for k, v in sset.items() if k != "shapes"}
    out["shapes"] = [enc(s) for s in sset["shapes"]]
    return out


def from_json(d):
    if str(d.get("format", "")).split("/")[0] != FORMAT.split("/")[0]:
        raise ShapeError("not a PhotoMesh shapes file")
    for s in d["shapes"]:
        s["parts"] = [np.asarray(p, float).reshape(-1, 3) for p in s["parts"]]
        if s.get("dparts") is not None:
            s["dparts"] = [np.asarray(p, float).reshape(-1, 3) for p in s["dparts"]]
    return d


def save(sset, path):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_json(sset), f, separators=(",", ":"), default=_jdefault)
    os.replace(tmp, path)


def _jdefault(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (bytes, bytearray)):
        return o.decode("latin-1")
    return str(o)


def load(path):
    with open(path, encoding="utf-8") as f:
        return from_json(json.load(f))


def _close(r):
    r = np.asarray(r, float).reshape(-1, 3)
    if len(r) >= 3 and not np.allclose(r[0], r[-1]):
        r = np.vstack([r, r[:1]])
    return r


def _label_of(attrs):
    for k in ("label", "Label", "LABEL", "name", "Name", "NAME", "id", "Id", "ID", "title"):
        if attrs.get(k) not in (None, ""):
            return str(attrs[k])
    return ""


def _ring_signed_area(P):
    x, y = P[:, 0], P[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


# =========================================================== readers
def detect_format(path):
    e = os.path.splitext(path)[1].lower()
    return {".shp": "SHP", ".dxf": "DXF", ".kml": "KML", ".kmz": "KMZ", ".geojson": "GeoJSON",
            ".json": "GeoJSON", ".csv": "CSV", ".txt": "CSV"}.get(e)


def read_any(path, layer_name=None):
    """-> (shape set in the file's own coordinates, SrcCRS of the file, notes)."""
    fmt = detect_format(path)
    if fmt is None:
        raise ShapeError(f"Unknown shape format: {os.path.basename(path)}")
    fn = {"SHP": read_shp, "DXF": read_dxf, "KML": read_kml, "KMZ": read_kml, "GeoJSON": read_geojson,
          "CSV": read_csv}[fmt]
    sset, crs, notes = fn(path, layer_name)
    for l in sset["layers"]:
        l["source"], l["format"], l["crs"] = os.path.abspath(path), fmt, crs.describe()
    return sset, crs, notes


# ---- ESRI Shapefile
def _dbf_read(path, encoding=None):
    if not os.path.exists(path):
        return None
    data = open(path, "rb").read()
    if len(data) < 32:
        return None
    n, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields, off = [], 32
    while off + 32 <= hlen and data[off] != 0x0D:
        fd = data[off:off + 32]
        fields.append((fd[:11].split(b"\0")[0].decode("latin-1").strip(), chr(fd[11]), fd[16], fd[17]))
        off += 32
    if encoding is None:
        encoding = "utf-8"
        cpg = os.path.splitext(path)[0] + ".cpg"
        if os.path.exists(cpg):
            e = open(cpg, encoding="latin-1").read().strip()
            encoding = {"utf8": "utf-8", "1252": "cp1252", "ansi 1252": "cp1252"}.get(e.lower(), e) or "utf-8"
    recs = []
    for i in range(n):
        r = data[hlen + i * rlen: hlen + (i + 1) * rlen]
        if not r:
            break
        rec, p = {}, 1
        for name, t, ln, dec in fields:
            raw = r[p:p + ln]; p += ln
            try:
                s = raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                s = raw.decode("cp1252", errors="replace")
            s = s.strip().strip("\0")
            if t in "NF":
                try:
                    v = (float(s) if (dec or "." in s or "e" in s.lower()) else int(s)) if s.strip("*") else None
                except ValueError:
                    v = None
            elif t == "L":
                v = None if s in ("", "?") else s.upper() in ("Y", "T")
            elif t == "D":
                v = f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
            else:
                v = s
            rec[name] = v
        recs.append(rec if r[:1] != b"*" else None)
    return recs


def read_shp(path, layer_name=None):
    base = os.path.splitext(path)[0]
    data = open(path, "rb").read()
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise ShapeError(f"{os.path.basename(path)}: not an ESRI shapefile")
    ftype = struct.unpack("<i", data[32:36])[0]
    recs = _dbf_read(base + ".dbf") or []
    prj = open(base + ".prj", encoding="latin-1").read() if os.path.exists(base + ".prj") else ""
    crs = SrcCRS.parse(prj)
    sset = new_set()
    li = add_layer(sset, layer_name or os.path.basename(base))
    notes, off, k, skipped = [], 100, 0, 0
    while off + 8 <= len(data):
        _num, clen = struct.unpack(">ii", data[off:off + 8])
        c = data[off + 8: off + 8 + 2 * clen]
        off += 8 + 2 * clen
        attrs = recs[k] if k < len(recs) and recs[k] is not None else {}
        k += 1
        if len(c) < 4:
            continue
        st = struct.unpack("<i", c[:4])[0]
        kind = SHP_TYPES.get(st)
        hasz = st in (11, 13, 15, 18, 31)
        if kind in (None, "null"):
            skipped += kind is None
            continue
        label = _label_of(attrs)
        if kind == "point":
            x, y = struct.unpack("<2d", c[4:20])
            z = struct.unpack("<d", c[20:28])[0] if hasz else 0.0
            sset["shapes"].append(make_shape(li, "point", [[[x, y, z]]], attrs, label, has_z=hasz))
        elif kind == "multipoint":
            npnt = struct.unpack("<i", c[36:40])[0]
            xy = np.frombuffer(c, "<f8", 2 * npnt, 40).reshape(-1, 2)
            z = np.frombuffer(c, "<f8", npnt, 40 + 16 * npnt + 16) if hasz else np.zeros(npnt)
            P = np.column_stack([xy, z])
            sset["shapes"].append(make_shape(li, "point", [P[i:i + 1] for i in range(npnt)], attrs, label,
                                             has_z=hasz))
        else:
            nparts, npnt = struct.unpack("<ii", c[36:44])
            parts = list(struct.unpack(f"<{nparts}i", c[44:44 + 4 * nparts]))
            o = 44 + 4 * nparts
            ptypes = None
            if st == 31:
                ptypes = list(struct.unpack(f"<{nparts}i", c[o:o + 4 * nparts])); o += 4 * nparts
            xy = np.frombuffer(c, "<f8", 2 * npnt, o).reshape(-1, 2)
            o += 16 * npnt
            z = np.frombuffer(c, "<f8", npnt, o + 16) if hasz and len(c) >= o + 16 + 8 * npnt else np.zeros(npnt)
            P = np.column_stack([xy, z])
            idx = parts + [npnt]
            rings = [P[idx[i]:idx[i + 1]] for i in range(nparts) if idx[i + 1] > idx[i]]
            if kind == "polyline":
                sset["shapes"].append(make_shape(li, "polyline", rings, attrs, label, has_z=hasz))
            else:
                if kind == "multipatch":
                    notes.append("MultiPatch parts read as polygon rings")
                    holes = [t == 3 for t in (ptypes or [])][:len(rings)]
                else:   # outer rings clockwise (negative signed area), holes counter-clockwise
                    holes = [_ring_signed_area(r) > 0 for r in rings]
                    if holes and all(holes):          # writer ignored the orientation rule
                        holes = [False] * len(rings)
                sset["shapes"].append(make_shape(li, "polygon", [_close(r) for r in rings], attrs, label, holes,
                                                 has_z=hasz))
    if skipped:
        notes.append(f"{skipped} record(s) of unsupported type skipped")
    if not prj:
        notes.append("no .prj - coordinates are assumed to be in the project coordinate system")
    notes.insert(0, f"Shapefile type {SHP_TYPES.get(ftype, ftype)} ({ftype}), {len(sset['shapes'])} shapes, "
                    f"{len(recs)} attribute records")
    return sset, crs, notes


# ---- DXF
def aci_rgb(i):
    """AutoCAD Color Index -> RGB (standard palette, computed)."""
    i = int(i)
    std = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255), 5: (0, 0, 255),
           6: (255, 0, 255), 7: (255, 255, 255), 8: (128, 128, 128), 9: (192, 192, 192)}
    if i in std:
        return std[i]
    if 250 <= i <= 255:
        g = int(round(51 + (i - 250) * (255 - 51) / 5))
        return (g, g, g)
    if 10 <= i <= 249:
        hue = ((i - 10) // 10) * 15.0
        v = (i - 10) % 10
        val = [1.0, 1.0, 0.8, 0.8, 0.6, 0.6, 0.5, 0.5, 0.3, 0.3][v]
        sat = 1.0 if v % 2 == 0 else 0.5
        r, g, b = colorsys.hsv_to_rgb(hue / 360.0, sat, val)
        return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
    return (255, 255, 255)


_ACI = None


def rgb_aci(rgb):
    global _ACI
    if _ACI is None:
        _ACI = np.array([aci_rgb(i) for i in range(1, 256)], float)
    d = ((_ACI - np.asarray(rgb[:3], float)) ** 2).sum(1)
    return int(np.argmin(d)) + 1


def _dxf_pairs(path):
    raw = open(path, "rb").read()
    if raw.startswith(b"AutoCAD Binary DXF"):
        raise ShapeError("Binary DXF is not supported - save it as ASCII DXF")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines) - 1:
        try:
            out.append((int(lines[i].strip()), lines[i + 1].strip()))
            i += 2
        except ValueError:
            i += 1                   # resynchronise on a broken line
    return out


def _dxf_entities(pairs):
    """-> (layer colours {name: rgb}, [(type, [(code, value)])] of the ENTITIES section)."""
    layer_col, ents, sec, cur, i = {}, [], None, None, 0
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0 and v == "SECTION":
            sec = pairs[i + 1][1] if i + 1 < len(pairs) else None
            i += 2; continue
        if c == 0 and v in ("ENDSEC", "EOF"):
            if cur is not None:
                ents.append(cur)
            cur, sec = None, None; i += 1; continue
        if sec == "TABLES" and c == 0 and v == "LAYER":
            name, col, tc, j = None, 7, None, i + 1
            while j < len(pairs) and pairs[j][0] != 0:
                cc, vv = pairs[j]
                try:
                    if cc == 2:
                        name = vv
                    elif cc == 62:
                        col = abs(int(vv))
                    elif cc == 420:
                        tc = int(vv)
                except ValueError:
                    pass
                j += 1
            if name is not None:
                layer_col[name] = ((tc >> 16) & 255, (tc >> 8) & 255, tc & 255) if tc is not None else aci_rgb(col)
            i = j; continue
        if sec == "ENTITIES":
            if c == 0:
                if cur is not None:
                    ents.append(cur)
                cur = (v, [])
            elif cur is not None:
                cur[1].append((c, v))
        i += 1
    return layer_col, ents


def _gv(g, code, default=None, cast=float):
    for cc, vv in g:
        if cc == code:
            try:
                return cast(vv)
            except ValueError:
                return default
    return default


def read_dxf(path, layer_name=None):
    layer_col, ents = _dxf_entities(_dxf_pairs(path))
    sset = new_set()
    lidx = {}
    counts, skipped = {}, {}

    def layer_of(name):
        name = name or "0"
        if name not in lidx:            # one shape layer per DXF layer, with its colour
            lidx[name] = add_layer(sset, f"{layer_name}: {name}" if layer_name else name, color=layer_col.get(name))
        return lidx[name]

    def common(g, etype):
        lay = _gv(g, 8, "0", str)
        tc = _gv(g, 420, None, int)
        rgb = ((tc >> 16) & 255, (tc >> 8) & 255, tc & 255) if tc is not None else None
        aci = _gv(g, 62, None, int)
        if rgb is None and aci not in (None, 0, 256):
            rgb = aci_rgb(abs(aci))
        attrs = {"layer": lay, "entity": etype}
        h = _gv(g, 5, None, str)
        if h:
            attrs["handle"] = h
        return layer_of(lay), attrs, rgb

    def add(li, kind, parts, attrs, rgb, hasz, holes=None):
        sset["shapes"].append(make_shape(li, kind, parts, attrs, "", holes, has_z=hasz, color=rgb))

    k = 0
    texts = []
    while k < len(ents):
        etype, g = ents[k]
        k += 1
        if etype in ("VERTEX", "SEQEND"):
            continue
        if etype in ("TEXT", "MTEXT"):
            texts.append((_gv(g, 10, 0.0), _gv(g, 20, 0.0), _gv(g, 40, 1.0), _gv(g, 1, "", str), _gv(g, 8, "0", str)))
            continue
        if etype not in ("POINT", "LINE", "LWPOLYLINE", "POLYLINE", "3DFACE", "CIRCLE", "ARC"):
            skipped[etype] = skipped.get(etype, 0) + 1
            continue
        li, attrs, rgb = common(g, etype)
        counts[etype] = counts.get(etype, 0) + 1
        if etype == "POINT":
            z = _gv(g, 30, 0.0)
            add(li, "point", [[[_gv(g, 10, 0.0), _gv(g, 20, 0.0), z]]], attrs, rgb, z != 0.0)
        elif etype == "LINE":
            P = [[_gv(g, 10, 0.0), _gv(g, 20, 0.0), _gv(g, 30, 0.0)], [_gv(g, 11, 0.0), _gv(g, 21, 0.0), _gv(g, 31, 0.0)]]
            add(li, "polyline", [P], attrs, rgb, P[0][2] != 0.0 or P[1][2] != 0.0)
        elif etype == "LWPOLYLINE":
            elev = _gv(g, 38, 0.0)
            xs = [float(v) for c, v in g if c == 10]
            ys = [float(v) for c, v in g if c == 20]
            n = min(len(xs), len(ys))
            P = np.column_stack([xs[:n], ys[:n], np.full(n, elev)])
            _add_line(sset, li, P, bool(_gv(g, 70, 0, int) & 1), attrs, rgb, elev != 0.0)
        elif etype == "POLYLINE":
            flags = _gv(g, 70, 0, int)
            verts = []
            while k < len(ents) and ents[k][0] == "VERTEX":
                vg = ents[k][1]
                verts.append(((_gv(vg, 10, 0.0), _gv(vg, 20, 0.0), _gv(vg, 30, 0.0)), _gv(vg, 70, 0, int),
                              [_gv(vg, c_, 0, int) for c_ in (71, 72, 73, 74)]))
                k += 1
            if k < len(ents) and ents[k][0] == "SEQEND":
                k += 1
            if flags & 64:      # polyface mesh: coordinate vertices (flag 64|128) + face records (flag 128)
                coords = [v[0] for v in verts if v[1] & 64]
                for v in verts:
                    if (v[1] & 128) and not (v[1] & 64):
                        ring = [coords[abs(x) - 1] for x in v[2] if x and abs(x) - 1 < len(coords)]
                        if len(ring) >= 3:
                            add(li, "polygon", [_close(ring)], dict(attrs, entity="POLYFACE"), rgb, True)
                continue
            if flags & 16:      # polygon mesh (M x N grid): keep the vertices as a polyline
                skipped["POLYLINE mesh"] = skipped.get("POLYLINE mesh", 0) + 1
            P = np.array([v[0] for v in verts], float).reshape(-1, 3)
            elev = _gv(g, 30, 0.0)
            if not flags & 8 and len(P) and not np.any(P[:, 2]):
                P[:, 2] = elev
            _add_line(sset, li, P, bool(flags & 1), attrs, rgb, bool(flags & 8) or bool(np.any(P[:, 2])))
        elif etype == "3DFACE":
            P = np.array([[_gv(g, 10 + j, 0.0), _gv(g, 20 + j, 0.0), _gv(g, 30 + j, 0.0)] for j in range(4)])
            if np.allclose(P[3], P[2]):
                P = P[:3]
            add(li, "polygon", [_close(P)], attrs, rgb, True)
        else:   # CIRCLE / ARC -> polygon / polyline (64 segments per full circle)
            cx, cy, cz, r = _gv(g, 10, 0.0), _gv(g, 20, 0.0), _gv(g, 30, 0.0), _gv(g, 40, 0.0)
            a0, a1 = (0.0, 360.0) if etype == "CIRCLE" else (_gv(g, 50, 0.0), _gv(g, 51, 360.0))
            if a1 <= a0:
                a1 += 360.0
            n = max(8, int(64 * (a1 - a0) / 360))
            t = np.radians(np.linspace(a0, a1, n + 1))
            P = np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t), np.full(len(t), cz)])
            if etype == "CIRCLE":
                add(li, "polygon", [_close(P[:-1])], attrs, rgb, cz != 0.0)
            else:
                add(li, "polyline", [P], attrs, rgb, cz != 0.0)
    if texts:     # TEXT next to a POINT (same layer) is its label
        pts = [(i, sh) for i, sh in enumerate(sset["shapes"]) if sh["type"] == "point"]
        if pts:
            XY = np.array([sh["parts"][0][0, :2] for _i, sh in pts])
            for tx, ty, th, txt, tl in texts:
                d = np.hypot(XY[:, 0] - tx, XY[:, 1] - ty)
                j = int(np.argmin(d))
                sh = pts[j][1]
                if d[j] <= 3.0 * max(th, 1e-6) and not sh["label"] and sh["attrs"].get("layer") == tl:
                    sh["label"] = txt; sh["attrs"]["label"] = txt
        counts["TEXT (labels)"] = len(texts)
    notes = ["DXF entities: " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none")]
    if skipped:
        notes.append("skipped (not supported): " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())))
    notes.append("DXF has no coordinate system - assumed to be the project coordinate system")
    if not sset["layers"]:
        add_layer(sset, layer_name or os.path.splitext(os.path.basename(path))[0])
    return sset, SrcCRS("project"), notes


def _add_line(sset, li, P, closed, attrs, rgb, has_z=True):
    if len(P) < 2:
        if len(P) == 1:
            sset["shapes"].append(make_shape(li, "point", [P], attrs, "", has_z=has_z, color=rgb))
        return
    if closed and len(P) >= 3:
        sset["shapes"].append(make_shape(li, "polygon", [_close(P)], attrs, "", has_z=has_z, color=rgb))
    else:
        sset["shapes"].append(make_shape(li, "polyline", [P], attrs, "", has_z=has_z, color=rgb))


# ---- KML / KMZ
def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _kml_coords(text):
    pts = []
    for tok in (text or "").split():
        v = tok.split(",")
        try:
            pts.append([float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 and v[2] else 0.0])
        except (ValueError, IndexError):
            pass
    return np.array(pts, float).reshape(-1, 3)


def _kml_color(c):
    c = (c or "").strip()
    if len(c) == 8:
        try:
            return (int(c[6:8], 16), int(c[4:6], 16), int(c[2:4], 16))
        except ValueError:
            return None
    return None


def _kml_geoms(pm):
    out = []

    def walk(el):
        for c in el:
            if c.tag in ("Point", "LineString", "LinearRing", "Polygon"):
                out.append(c)
            elif c.tag in ("MultiGeometry",):
                walk(c)
    walk(pm)
    return out


def read_kml(path, layer_name=None):
    if path.lower().endswith(".kmz"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ShapeError("KMZ contains no .kml")
            data = z.read("doc.kml" if "doc.kml" in names else names[0])
    else:
        data = open(path, "rb").read()
    root = _strip_ns(ET.fromstring(data))
    styles = {}
    for st in root.iter("Style"):
        c = st.find("LineStyle/color")
        if c is None:
            c = st.find("PolyStyle/color")
        if c is None:
            c = st.find("IconStyle/color")
        if st.get("id") and c is not None:
            styles["#" + st.get("id")] = _kml_color(c.text)
    for sm in root.iter("StyleMap"):
        for pair in sm.findall("Pair"):
            if (pair.findtext("key") or "").strip() == "normal" and sm.get("id"):
                styles["#" + sm.get("id")] = styles.get((pair.findtext("styleUrl") or "").strip())
    sset = new_set()
    stem = layer_name or os.path.splitext(os.path.basename(path))[0]
    lidx = {}
    parent = {c: p for p in root.iter() for c in p}
    nclamp = 0

    def layer_for(pm):
        p = parent.get(pm)
        while p is not None and p.tag != "Folder":
            p = parent.get(p)
        nm = (p.findtext("name") or "").strip() if p is not None else ""
        if nm not in lidx:
            lidx[nm] = add_layer(sset, (f"{layer_name}: {nm}" if layer_name else nm) if nm else stem)
        return lidx[nm]

    for pm in root.iter("Placemark"):
        li = layer_for(pm)
        label = (pm.findtext("name") or "").strip()
        attrs = {"name": label} if label else {}
        d = pm.findtext("description")
        if d and d.strip():
            attrs["description"] = d.strip()
        for dd in pm.iter("Data"):
            attrs[dd.get("name") or "data"] = (dd.findtext("value") or "").strip()
        for sd in pm.iter("SimpleData"):
            attrs[sd.get("name") or "data"] = (sd.text or "").strip()
        rgb = styles.get((pm.findtext("styleUrl") or "").strip())
        inline = pm.find("Style")
        if inline is not None and inline.find("LineStyle/color") is not None:
            rgb = _kml_color(inline.find("LineStyle/color").text) or rgb
        for geom in _kml_geoms(pm):
            mode = (geom.findtext("altitudeMode") or "clampToGround").strip()
            hasz = mode in ("absolute", "relativeToGround")
            nclamp += not hasz
            if geom.tag == "Point":
                P = _kml_coords(geom.findtext("coordinates"))
                if len(P):
                    sset["shapes"].append(make_shape(li, "point", [P[:1]], attrs, label, has_z=hasz, color=rgb))
            elif geom.tag == "LineString":
                P = _kml_coords(geom.findtext("coordinates"))
                if len(P) >= 2:
                    sset["shapes"].append(make_shape(li, "polyline", [P], attrs, label, has_z=hasz, color=rgb))
            elif geom.tag == "LinearRing":
                P = _kml_coords(geom.findtext("coordinates"))
                if len(P) >= 3:
                    sset["shapes"].append(make_shape(li, "polygon", [_close(P)], attrs, label, has_z=hasz, color=rgb))
            else:
                rings, holes = [], []
                for tag, hole in (("outerBoundaryIs", False), ("innerBoundaryIs", True)):
                    for b in geom.findall(tag):
                        P = _kml_coords(b.findtext("LinearRing/coordinates"))
                        if len(P) >= 3:
                            rings.append(_close(P)); holes.append(hole)
                if rings:
                    sset["shapes"].append(make_shape(li, "polygon", rings, attrs, label, holes, has_z=hasz, color=rgb))
    if not sset["layers"]:
        add_layer(sset, stem)
    notes = [f"KML: {len(sset['shapes'])} geometries in {len(sset['layers'])} layer(s), WGS 84"]
    if nclamp:
        notes.append(f"{nclamp} geometries are clamped to the ground (no altitude): drape them on the surface")
    return sset, SrcCRS("wgs84"), notes


# ---- GeoJSON
def read_geojson(path, layer_name=None):
    with open(path, encoding="utf-8-sig") as f:
        d = json.load(f)
    crs, explicit = SrcCRS("wgs84"), False
    c = d.get("crs") if isinstance(d, dict) else None
    if c:
        name = (c.get("properties") or {}).get("name") or ""
        m = re.search(r"EPSG:+(\d+)", name)
        if m:
            crs = SrcCRS.parse(f"EPSG:{m.group(1)}")
        elif "CRS84" in name.upper():
            crs = SrcCRS("wgs84")
        else:
            crs = SrcCRS.parse(name)
        explicit = True
    if isinstance(d, dict) and d.get("photomesh_crs"):
        crs, explicit = SrcCRS.parse(d["photomesh_crs"]), True
        if crs.kind == "proj" and crs.name.upper().startswith("LOCAL"):
            crs = SrcCRS("local", name=crs.name)
    sset = new_set()
    li = add_layer(sset, layer_name or (d.get("name") if isinstance(d, dict) and d.get("name") else
                                        os.path.splitext(os.path.basename(path))[0]))
    if d.get("type") == "FeatureCollection":
        feats = d.get("features") or []
    elif d.get("type") == "Feature":
        feats = [d]
    else:
        feats = [{"type": "Feature", "geometry": d}]
    for ft in feats:
        props = ft.get("properties") or {}
        attrs = {k: (v if isinstance(v, (int, float, str, bool)) or v is None else json.dumps(v)) for k, v in props.items()}
        label = _label_of(attrs) or str(ft.get("id") or "")
        for kind, parts, holes, hz in _gj_geom(ft.get("geometry")):
            sset["shapes"].append(make_shape(li, kind, parts, attrs, label, holes if kind == "polygon" else None,
                                             has_z=hz))
    notes = [f"GeoJSON: {len(sset['shapes'])} geometries"]
    V = all_vertices(sset)
    if not explicit and len(V) and (np.abs(V[:, 0]).max() > 180 or np.abs(V[:, 1]).max() > 90):
        crs = SrcCRS("project")
        notes.append("no 'crs' member and the coordinates are not degrees - read as project coordinates")
    else:
        notes.append(f"coordinate system: {crs.describe()}")
    return sset, crs, notes


def _gj_pt(c):
    return [float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 and c[2] is not None else 0.0]


def _gj_geom(g):
    if not g:
        return []
    t, c = g.get("type"), g.get("coordinates")

    def hz(arr):
        return any(len(p) > 2 for p in arr)
    if t == "Point":
        return [("point", [[_gj_pt(c)]], None, len(c) > 2)]
    if t == "MultiPoint":
        return [("point", [[_gj_pt(p)] for p in c], None, hz(c))]
    if t == "LineString":
        return [("polyline", [[_gj_pt(p) for p in c]], None, hz(c))]
    if t == "MultiLineString":
        return [("polyline", [[_gj_pt(p) for p in l] for l in c], None, any(hz(l) for l in c))]
    if t == "Polygon":
        return [("polygon", [_close([_gj_pt(p) for p in r]) for r in c], [k > 0 for k in range(len(c))],
                 any(hz(r) for r in c))]
    if t == "MultiPolygon":
        rings, holes = [], []
        for poly in c:
            for k, r in enumerate(poly):
                rings.append(_close([_gj_pt(p) for p in r])); holes.append(k > 0)
        return [("polygon", rings, holes, any(hz(r) for poly in c for r in poly))]
    if t == "GeometryCollection":
        return [x for gg in g.get("geometries") or [] for x in _gj_geom(gg)]
    return []


# ---- CSV / TXT points
def read_csv(path, layer_name=None):
    text = open(path, "rb").read().decode("utf-8-sig", errors="replace")
    crs_line, rows = "", []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            m = re.match(r"#\s*CoordinateSystem:\s*(.*)", s, re.I)
            if m:
                crs_line = m.group(1)
            continue
        rows.append(s)
    if not rows:
        raise ShapeError("empty file")

    def ncols(s, d):
        return len(s.split()) if d == " " else len(next(csv.reader([s], delimiter=d)))
    delim = max(("\t", ",", ";", " "), key=lambda d: ncols(rows[0], d))

    def split(s):
        return s.split() if delim == " " else [x.strip() for x in next(csv.reader([s], delimiter=delim))]

    def isnum(v):
        try:
            float(v); return True
        except (ValueError, TypeError):
            return False
    first = split(rows[0])
    has_header = sum(isnum(v) for v in first) < 2
    if has_header:
        names = [h.strip().lower() for h in first]; data = rows[1:]
    else:
        n = len(first)
        names = ["label", "x", "y", "z"][:n] + [f"f{i}" for i in range(4, n)] if not isnum(first[0]) or n >= 4 \
            else ["x", "y", "z"][:n]
        data = rows

    def col(*cands):
        for c in cands:
            if c in names:
                return names.index(c)
        return None
    ix = col("x", "easting", "east", "lon", "longitude", "x/east")
    iy = col("y", "northing", "north", "lat", "latitude", "y/north")
    iz = col("z", "alt", "altitude", "elevation", "height", "h", "z/altitude")
    il = col("label", "name", "id", "point", "#label", "pt")
    if ix is None or iy is None:
        raise ShapeError("CSV needs X and Y (or lon / lat) columns")
    geo = names[ix] in ("lon", "longitude")
    crs = SrcCRS("wgs84") if geo else (SrcCRS.parse(crs_line) if crs_line else SrcCRS("project"))
    sset = new_set()
    li = add_layer(sset, layer_name or os.path.splitext(os.path.basename(path))[0])
    for r in data:
        v = split(r)
        try:
            P = [[float(v[ix]), float(v[iy]), float(v[iz]) if iz is not None and iz < len(v) and isnum(v[iz]) else 0.0]]
        except (ValueError, IndexError):
            continue
        attrs = {names[k]: (float(x) if isnum(x) else x) for k, x in enumerate(v) if k < len(names)}
        sset["shapes"].append(make_shape(li, "point", [P], attrs, v[il] if il is not None and il < len(v) else "",
                                         has_z=iz is not None))
    return sset, crs, [f"CSV points: {len(sset['shapes'])}, columns {names}"]


# ========================================================== import pipeline
def transform_set(sset, frame, src, add_offset=False):
    """In place: file coordinates -> world coordinates of `frame`
    (add_offset: the file holds local model coordinates, world = file + offset)."""
    for s in sset["shapes"]:
        s["parts"] = [frame.from_src(p, src) + (frame.offset if add_offset else 0.0) for p in s["parts"]]
    return sset


def far_check(shape_bounds, project_bounds, offset=None):
    """Is the shape extent far from the project area? -> (far, message, hint).
    hint 'offset': adding the project offset puts the shapes on the site (local
    coordinates without offset); 'degrees': they look like lon / lat."""
    if shape_bounds is None or project_bounds is None:
        return False, "", None
    slo, shi = np.asarray(shape_bounds[0], float)[:2], np.asarray(shape_bounds[1], float)[:2]
    plo, phi = np.asarray(project_bounds[0], float)[:2], np.asarray(project_bounds[1], float)[:2]
    size = float(max(np.linalg.norm(phi - plo), 1.0))
    gap = np.maximum(0, np.maximum(plo - shi, slo - phi))
    dist = float(np.linalg.norm(gap))
    if dist <= max(2.0 * size, 1000.0):
        return False, "", None
    msg = (f"The shapes are {dist:,.0f} m away from the project area (shapes X {slo[0]:,.2f} .. {shi[0]:,.2f}, "
           f"Y {slo[1]:,.2f} .. {shi[1]:,.2f}; project X {plo[0]:,.2f} .. {phi[0]:,.2f}, "
           f"Y {plo[1]:,.2f} .. {phi[1]:,.2f}).")
    hint = None
    if offset is not None and np.any(np.asarray(offset, float)[:2]):
        o = np.asarray(offset, float)[:2]
        g2 = np.maximum(0, np.maximum(plo - (shi + o), (slo + o) - phi))
        if np.linalg.norm(g2) <= max(0.5 * size, 100.0):
            hint = "offset"
            msg += (f" Adding the project offset ({o[0]:.3f}, {o[1]:.3f}) puts them on the site - they look like "
                    "local (model) coordinates.")
    if hint is None and np.all(np.abs(np.concatenate([slo[:1], shi[:1]])) <= 180.0) and \
            np.all(np.abs(np.concatenate([slo[1:], shi[1:]])) <= 90.0):
        hint = "degrees"
        msg += " The values look like longitude / latitude in degrees - choose WGS 84 as the source system."
    if hint is None:
        msg += " Check the coordinate system of the file (another CRS or a missing offset)."
    return True, msg, hint


# ---- heights (draping)
class HeightSampler:
    """World XY -> world Z: a DEM / height GeoTIFF (bilinear) or the mesh
    (vertical rays, Embree via Open3D, model frame + offset)."""

    def __init__(self, kind, sample, px, name):
        self.kind, self._sample, self.px, self.name = kind, sample, float(px), name
        self.scene = None

    def __call__(self, X, Y):
        X, Y = np.asarray(X, float), np.asarray(Y, float)
        return self._sample(X, Y) if X.size else np.zeros(X.shape)

    @classmethod
    def from_raster(cls, path, name="DEM"):
        from . import geotiff as gt
        with gt.TiffReader(path) as r:
            z = r.read_level(0)[..., 0].astype(np.float32)
            x0, y1, px, py = r.geo
            nd = r.nodata
        if nd is not None:
            z[z == nd] = np.nan
        z[~np.isfinite(z)] = np.nan
        H, W = z.shape

        def sample(X, Y):
            c = (X - x0) / px - 0.5
            rr = (y1 - Y) / py - 0.5
            c0, r0 = np.floor(c).astype(np.int64), np.floor(rr).astype(np.int64)
            fc, fr = c - c0, rr - r0
            vals, wts = np.zeros(X.shape), np.zeros(X.shape)
            for dr, dc, w in ((0, 0, (1 - fr) * (1 - fc)), (0, 1, (1 - fr) * fc), (1, 0, fr * (1 - fc)),
                              (1, 1, fr * fc)):
                ri, ci = r0 + dr, c0 + dc
                inside = (ri >= 0) & (ri < H) & (ci >= 0) & (ci < W)
                v = z[np.clip(ri, 0, H - 1), np.clip(ci, 0, W - 1)]
                good = inside & np.isfinite(v)
                vals += np.where(good, v * w, 0.0); wts += np.where(good, w, 0.0)
            out = np.full(X.shape, np.nan)
            m = wts > 1e-6
            out[m] = vals[m] / wts[m]
            return out
        return cls("raster", sample, px, name)

    @classmethod
    def from_mesh(cls, mesh_path, offset, name="Model"):
        import open3d as o3d
        from .texture import read_mesh
        V, F = read_mesh(mesh_path)
        sc = o3d.t.geometry.RaycastingScene()
        sc.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
        top = float(V[:, 2].max()) + 10.0
        off = np.asarray(offset, float)
        e = np.linalg.norm(V[F[:, 1]] - V[F[:, 0]], axis=1) if len(F) else np.ones(1)
        px = float(np.median(e))

        def sample(X, Y):
            n = X.size
            rays = np.zeros((n, 6), np.float32)
            rays[:, 0] = X.ravel() - off[0]; rays[:, 1] = Y.ravel() - off[1]; rays[:, 2] = top; rays[:, 5] = -1
            t = sc.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy().astype(float)
            z = top - t + off[2]
            z[~np.isfinite(t)] = np.nan
            return z.reshape(X.shape)
        s = cls("mesh", sample, px, name)
        s.scene = sc
        return s


def densify(P, step, max_per_segment=5000):
    """Insert vertices so that no segment is longer than `step` (XY)."""
    P = np.asarray(P, float)
    if len(P) < 2 or step <= 0:
        return P.copy()
    d = np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1)
    n = np.clip(np.ceil(d / step).astype(int), 1, max_per_segment)
    seg = np.repeat(np.arange(len(P) - 1), n)
    t = (np.concatenate([np.arange(1, k + 1) / k for k in n]))[:, None]
    return np.vstack([P[:1], P[seg] + (P[seg + 1] - P[seg]) * t])


def apply_heights(sset, mode="keep", sampler=None, fixed_z=0.0, only_without_z=True, step=None, shapes=None,
                  log=print):
    """Z of the shapes:
      keep  - Z from the file (shapes without Z get fixed_z)
      drape - attached to the surface (Metashape-like 'shapes are 2D'): vertex Z from
              the sampler, plus draped display polylines ('dparts', densified to ~2
              sampler pixels) that follow the terrain
      fixed - every vertex at fixed_z
    only_without_z: drape / fixed only the shapes that have no Z in the file."""
    shp = shapes if shapes is not None else sset["shapes"]
    miss_total, n = 0, 0
    for s in shp:
        target = not (only_without_z and s.get("has_z"))
        if not target:
            continue
        n += 1
        if mode == "fixed":
            for p in s["parts"]:
                p[:, 2] = fixed_z
            s["attached"] = False; s.pop("dparts", None)
        elif mode == "drape" and sampler is not None:
            st = step or max(sampler.px * 2.0, 1e-3)
            dps = []
            for p in s["parts"]:
                z = sampler(p[:, 0], p[:, 1])
                miss = ~np.isfinite(z)
                miss_total += int(miss.sum())
                p[:, 2] = np.where(miss, p[:, 2] if s.get("has_z") else fixed_z, z)
                if s["type"] != "point" and len(p) >= 2:
                    q = densify(p, st)
                    zq = sampler(q[:, 0], q[:, 1])
                    ok = np.isfinite(zq)
                    if ok.any() and not ok.all():          # gaps: interpolate along the line
                        idx = np.arange(len(q))
                        zq = np.interp(idx, idx[ok], zq[ok])
                    q[:, 2] = zq if ok.any() else q[:, 2]
                    dps.append(q)
                else:
                    dps.append(p.copy())
            s["dparts"] = dps
            s["attached"] = True
        elif mode == "keep" and not s.get("has_z") and fixed_z:
            for p in s["parts"]:
                p[:, 2] = fixed_z
    if miss_total:
        log(f"Shapes: {miss_total} vertices are outside the surface (kept Z {fixed_z:g} / their own Z)")
    return n


def display_parts(s):
    d = s.get("dparts")
    return d if d else s["parts"]


# ============================================================ measurement
def _poly_area2d(rings, holes):
    a = 0.0
    for r, h in zip(rings, holes):
        v = abs(_ring_signed_area(r))
        a += -v if h else v
    return a


def _len2d(P):
    return float(np.sum(np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1))) if len(P) > 1 else 0.0


def _len3d(P):
    return float(np.sum(np.linalg.norm(np.diff(P, axis=0), axis=1))) if len(P) > 1 else 0.0


def inside_polygon(X, Y, rings):
    """Even-odd rule over all rings (holes included)."""
    inside = np.zeros(np.shape(X), bool)
    for r in rings:
        x, y = r[:, 0], r[:, 1]
        for i in range(len(r) - 1):
            x1, y1, x2, y2 = x[i], y[i], x[i + 1], y[i + 1]
            if y1 == y2:
                continue
            inside ^= ((y1 > Y) != (y2 > Y)) & (X < (x2 - x1) * (Y - y1) / (y2 - y1) + x1)
    return inside


def measure(s, sampler=None, max_cells=4_000_000):
    """Metrics of one shape (world units, metres for metric systems). With a
    sampler (DEM / mesh height): draped 3D lengths, the 3D surface area and the
    volume above / below the best-fit plane through the polygon vertices."""
    P = s["parts"]
    D = display_parts(s)
    out = {"Type": s["type"].capitalize(), "Vertices": int(sum(len(p) for p in P)), "Parts": len(P)}
    if s["type"] == "point":
        p = P[0][0]
        out.update({"X": float(p[0]), "Y": float(p[1]), "Z": float(p[2])})
        if sampler is not None:
            z = float(sampler(np.array([p[0]]), np.array([p[1]]))[0])
            if np.isfinite(z):
                out[f"Surface Z ({sampler.name})"] = z
        return out
    if s["type"] == "polyline":
        out["Length 2D"] = sum(_len2d(p) for p in P)
        out["Length 3D"] = sum(_len3d(p) for p in D)
        zs = np.concatenate([p[:, 2] for p in D])
        out["Z min"], out["Z max"] = float(zs.min()), float(zs.max())
        return out
    holes = s.get("holes") or [False] * len(P)
    out["Area 2D"] = _poly_area2d(P, holes)
    out["Perimeter 2D"] = sum(_len2d(p) for p, h in zip(P, holes) if not h)
    out["Perimeter 3D"] = sum(_len3d(p) for p, h in zip(D, holes) if not h)
    if sampler is None:
        return out
    V = np.vstack(P)
    lo, hi = V[:, :2].min(0), V[:, :2].max(0)
    area_bb = float(np.prod(np.maximum(hi - lo, 1e-9)))
    step = max(sampler.px, math.sqrt(area_bb / max_cells))
    xs = np.arange(lo[0] + step / 2, hi[0], step)
    ys = np.arange(lo[1] + step / 2, hi[1], step)
    if not (len(xs) and len(ys)):
        return out
    GX, GY = np.meshgrid(xs, ys)
    ins = inside_polygon(GX, GY, P)
    Z = sampler(GX, GY)
    ok = ins & np.isfinite(Z)
    if not ok.any():
        out["Volume"] = "no surface inside the polygon"
        return out
    zy, zx = np.gradient(Z, step)
    g = np.sqrt(1 + np.nan_to_num(zx) ** 2 + np.nan_to_num(zy) ** 2)
    out["Area 3D (surface)"] = float(np.sum(g[ok]) * step * step)
    B = np.vstack([(p[:-1] if len(p) > 3 else p) for p, h in zip(P, holes) if not h])
    zb = sampler(B[:, 0], B[:, 1])
    zb = np.where(np.isfinite(zb), zb, B[:, 2])
    A = np.column_stack([B[:, 0] - lo[0], B[:, 1] - lo[1], np.ones(len(B))])
    coef, *_ = np.linalg.lstsq(A, zb, rcond=None)
    plane = coef[0] * (GX - lo[0]) + coef[1] * (GY - lo[1]) + coef[2]
    dz = (Z - plane)[ok]
    cell = step * step
    out["Volume above plane (cut)"] = float(np.sum(dz[dz > 0]) * cell)
    out["Volume below plane (fill)"] = float(-np.sum(dz[dz < 0]) * cell)
    out["Volume net"] = float(np.sum(dz) * cell)
    out["Volume grid"] = f"{step:.3f} m cells, {int(ok.sum()):,} cells, {sampler.name}"
    cov = ok.sum() / max(int(ins.sum()), 1)
    if cov < 0.98:
        out["Surface coverage"] = f"{100 * cov:.1f} % of the polygon has surface"
    return out


# =========================================================== writers
def export(sset, dst, fmt=None, frame=None, dst_crs=None, layers=None, log=print):
    """Write shapes (world coordinates, or `dst_crs`) as SHP / DXF / GeoJSON. Returns the files."""
    fmt = (fmt or detect_format(dst) or "SHP").upper()
    shapes = [s for s in sset["shapes"] if layers is None or s["layer"] in layers]
    if frame is not None and dst_crs is not None and dst_crs.kind != "project":
        shapes = [dict(s, parts=[frame.to_dst(p, dst_crs) for p in s["parts"]]) for s in shapes]
    if fmt == "SHP":
        files = write_shp(shapes, dst, sset["layers"], _prj_for(frame, dst_crs))
    elif fmt == "DXF":
        files = write_dxf(shapes, dst, sset["layers"])
    elif fmt in ("GEOJSON", "JSON"):
        files = write_geojson(shapes, dst, sset["layers"], frame, dst_crs)
    else:
        raise ShapeError(f"Unsupported export format {fmt}")
    log(f"Exported {len(shapes)} shapes -> " + ", ".join(os.path.basename(f) for f in files))
    return files


def _prj_for(frame, dst_crs):
    if dst_crs is not None and dst_crs.kind == "wgs84":
        return WGS84_WKT
    if dst_crs is not None and dst_crs.kind == "utm":
        return utm_wkt(dst_crs.zone, dst_crs.south)
    return frame.prj() if frame is not None else None


def _shape_attrs(s, layers):
    a = {"layer": layers[s["layer"]]["name"] if 0 <= s["layer"] < len(layers) else "", "label": s.get("label", "")}
    for k, v in (s.get("attrs") or {}).items():
        if str(k).lower() not in ("layer", "label"):
            a[k] = v
    return a


def _dbf_clean(v):
    """NaN / inf have no dBASE representation -> empty (null) field."""
    if isinstance(v, (float, np.floating)) and not math.isfinite(float(v)):
        return None
    return v


def _dbf_write(path, records):
    names = []
    for r in records:
        for k in r:
            if k not in names:
                names.append(k)
    fields, used = [], set()
    for k in names:
        nm = re.sub(r"[^A-Za-z0-9_]", "_", str(k))[:10] or "F"
        base, j = nm, 1
        while nm.upper() in used:
            nm = f"{base[:8]}{j}"; j += 1
        used.add(nm.upper())
        vals = [_dbf_clean(r.get(k)) for r in records]
        if any(isinstance(v, bool) for v in vals) and all(isinstance(v, bool) or v is None for v in vals):
            fields.append((k, nm, "L", 1, 0))
        elif any(v is not None for v in vals) and all(v is None or (isinstance(v, (int, float, np.number)) and
                                                                    not isinstance(v, bool)) for v in vals):
            isint = all(v is None or (float(v).is_integer() and abs(v) < 1e15) for v in vals)
            big = any(v is not None and abs(v) >= 1e9 for v in vals)
            # N(9,0) / N(18,0) are integers for GDAL / QGIS (wider integer fields become Real)
            fields.append((k, nm, "N", (18 if big else 9) if isint else 24, 0 if isint else 8))
        else:
            ln = max([len(str(v).encode("utf-8")) for v in vals if v is not None] + [1])
            fields.append((k, nm, "C", min(254, ln), 0))
    rlen = 1 + sum(f[3] for f in fields)
    hlen = 32 + 32 * len(fields) + 1
    t = time.localtime()
    out = bytearray(struct.pack("<BBBBIHH20x", 3, t.tm_year - 1900, t.tm_mon, t.tm_mday, len(records), hlen, rlen))
    for _k, nm, ty, ln, dec in fields:
        out += struct.pack("<11sc4xBB14x", nm.encode("ascii"), ty.encode(), ln, dec)
    out += b"\x0D"
    for r in records:
        out += b" "
        for k, _nm, ty, ln, dec in fields:
            v = _dbf_clean(r.get(k))
            if ty == "N":
                s = "" if v is None else (f"{int(v)}" if dec == 0 else f"{float(v):.{dec}f}")
                b = s.rjust(ln)[:ln].encode("ascii")
            elif ty == "L":
                b = b"?" if v is None else (b"T" if v else b"F")
            else:
                b = ("" if v is None else str(v)).encode("utf-8")[:ln]
                while b:
                    try:
                        b.decode("utf-8"); break
                    except UnicodeDecodeError:       # do not cut a multi-byte character
                        b = b[:-1]
                b = b.ljust(ln, b" ")
            out += b
    out += b"\x1A"
    with open(path, "wb") as f:
        f.write(out)


def _shp_header(ftype, flen_words, bb, zr):
    return (struct.pack(">7i", 9994, 0, 0, 0, 0, 0, flen_words) +
            struct.pack("<2i8d", 1000, ftype, bb[0], bb[1], bb[2], bb[3], zr[0], zr[1], 0.0, 0.0))


def write_shp(shapes, dst, layers, prj=None):
    """One .shp/.shx/.dbf/.cpg(/.prj) per geometry type (a shapefile holds one type):
    <name>.shp for a single type, else <name>_points / _lines / _polygons."""
    base = os.path.splitext(dst)[0]
    groups = {"point": [], "polyline": [], "polygon": []}
    for s in shapes:
        groups[s["type"]].append(s)
    used = [k for k, v in groups.items() if v] or ["point"]
    files = []
    suffix = {"point": "points", "polyline": "lines", "polygon": "polygons"}
    for kind in used:
        b = base if len(used) == 1 else f"{base}_{suffix[kind]}"
        files += _write_shp_one(groups[kind], b, kind, layers, prj)
    return files


def _write_shp_one(shapes, base, kind, layers, prj):
    multi = kind == "point" and any(len(s["parts"]) > 1 for s in shapes)
    # Z types when any shape has a height (from the file or attached to the surface), else 2D types
    hz = any(s.get("has_z", True) or s.get("attached") for s in shapes) or not shapes
    if hz:
        ftype = {"point": 18 if multi else 11, "polyline": 13, "polygon": 15}[kind]
    else:
        ftype = {"point": 8 if multi else 1, "polyline": 3, "polygon": 5}[kind]
    allv = [p for s in shapes for p in s["parts"]]
    V = np.vstack(allv) if allv else np.zeros((1, 3))
    bb = (V[:, 0].min(), V[:, 1].min(), V[:, 0].max(), V[:, 1].max())
    zr = (V[:, 2].min(), V[:, 2].max()) if hz else (0.0, 0.0)
    body, offs, lens = bytearray(), [], []
    for i, s in enumerate(shapes):
        if kind == "point" and not multi:
            p = s["parts"][0][0]
            c = struct.pack("<i4d", 11, p[0], p[1], p[2], 0.0) if hz else struct.pack("<i2d", 1, p[0], p[1])
        else:
            parts = [np.asarray(p, float) for p in s["parts"]]
            if kind == "polygon":
                holes = s.get("holes") or [False] * len(parts)
                parts = [(lambda q: q if (_ring_signed_area(q) < 0) != bool(h) else q[::-1])(_close(p))
                         for p, h in zip(parts, holes)]                  # outer CW, holes CCW
            P = np.vstack(parts)
            n = len(P)
            sbb = (P[:, 0].min(), P[:, 1].min(), P[:, 0].max(), P[:, 1].max())
            zpart = struct.pack("<2d", P[:, 2].min(), P[:, 2].max()) + P[:, 2].astype("<f8").tobytes()
            mpart = struct.pack("<2d", 0.0, 0.0) + np.zeros(n, "<f8").tobytes()
            if not hz:
                zpart = mpart = b""
            if multi:
                c = struct.pack("<i4di", ftype, *sbb, n) + P[:, :2].astype("<f8").tobytes() + zpart + mpart
            else:
                starts = np.cumsum([0] + [len(p) for p in parts[:-1]]).astype("<i4")
                c = (struct.pack("<i4dii", ftype, *sbb, len(parts), n) + starts.tobytes() +
                     P[:, :2].astype("<f8").tobytes() + zpart + mpart)
        offs.append((100 + len(body)) // 2)
        lens.append(len(c) // 2)
        body += struct.pack(">ii", i + 1, len(c) // 2) + c
    with open(base + ".shp", "wb") as f:
        f.write(_shp_header(ftype, (100 + len(body)) // 2, bb, zr) + bytes(body))
    with open(base + ".shx", "wb") as f:
        f.write(_shp_header(ftype, (100 + 8 * len(shapes)) // 2, bb, zr) +
                b"".join(struct.pack(">ii", o, l) for o, l in zip(offs, lens)))
    _dbf_write(base + ".dbf", [_shape_attrs(s, layers) for s in shapes])
    with open(base + ".cpg", "w", encoding="ascii") as f:
        f.write("UTF-8")
    files = [base + e for e in (".shp", ".shx", ".dbf", ".cpg")]
    if prj:
        with open(base + ".prj", "w", encoding="ascii") as f:
            f.write(prj)
        files.append(base + ".prj")
    return files


def write_dxf(shapes, dst, layers):
    """DXF R12 (AC1009; read by AutoCAD, Rhino, QGIS, BricsCAD): POINT and 3D
    POLYLINE (closed for polygons; every ring is its own polyline); layer colours
    as the nearest ACI."""
    def lname(i):
        n = layers[i]["name"] if 0 <= i < len(layers) else "0"
        return re.sub(r'[<>/\\":;?*|=`,]', "_", n)[:255] or "0"
    used = sorted({s["layer"] for s in shapes})
    o = io.StringIO()

    def w(c, v):
        o.write(f"{c}\n{v}\n")
    V = all_vertices(None, shapes) if shapes else np.zeros((1, 3))
    w(0, "SECTION"); w(2, "HEADER"); w(9, "$ACADVER"); w(1, "AC1009")
    w(9, "$EXTMIN"); w(10, f"{V[:, 0].min():.4f}"); w(20, f"{V[:, 1].min():.4f}"); w(30, f"{V[:, 2].min():.4f}")
    w(9, "$EXTMAX"); w(10, f"{V[:, 0].max():.4f}"); w(20, f"{V[:, 1].max():.4f}"); w(30, f"{V[:, 2].max():.4f}")
    w(0, "ENDSEC")
    w(0, "SECTION"); w(2, "TABLES"); w(0, "TABLE"); w(2, "LAYER"); w(70, len(used) + 1)
    w(0, "LAYER"); w(2, "0"); w(70, 0); w(62, 7); w(6, "CONTINUOUS")
    done = {"0"}
    for i in used:
        if lname(i) in done:
            continue
        done.add(lname(i))
        w(0, "LAYER"); w(2, lname(i)); w(70, 0); w(62, rgb_aci(layers[i]["color"])); w(6, "CONTINUOUS")
    w(0, "ENDTAB"); w(0, "ENDSEC")
    w(0, "SECTION"); w(2, "ENTITIES")

    def f6(v):
        return f"{v:.6f}"
    ext = float(max(np.ptp(V[:, 0]), np.ptp(V[:, 1]), 1e-6))
    th = float(min(max(ext / 300.0, 0.05), 5.0))          # label text height
    for s in shapes:
        ln = lname(s["layer"])
        col = rgb_aci(s["color"]) if s.get("color") else None
        for p in s["parts"]:
            p = np.asarray(p, float)
            if s["type"] == "point":
                w(0, "POINT"); w(8, ln)
                if col:
                    w(62, col)
                w(10, f6(p[0, 0])); w(20, f6(p[0, 1])); w(30, f6(p[0, 2]))
                if s.get("label"):          # point label as TEXT next to the point (CAD users want it)
                    w(0, "TEXT"); w(8, ln); w(10, f6(p[0, 0] + th * 0.6)); w(20, f6(p[0, 1] + th * 0.6))
                    w(30, f6(p[0, 2])); w(40, f"{th:.3f}"); w(1, re.sub(r"[\r\n]", " ", s["label"]))
                continue
            closed = s["type"] == "polygon"
            if closed and len(p) > 3 and np.allclose(p[0], p[-1]):
                p = p[:-1]
            w(0, "POLYLINE"); w(8, ln)
            if col:
                w(62, col)
            w(66, 1); w(10, "0.0"); w(20, "0.0"); w(30, "0.0"); w(70, 8 | (1 if closed else 0))
            for q in p:
                w(0, "VERTEX"); w(8, ln); w(10, f6(q[0])); w(20, f6(q[1])); w(30, f6(q[2])); w(70, 32)
            w(0, "SEQEND"); w(8, ln)
    w(0, "ENDSEC"); w(0, "EOF")
    with open(dst, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write(o.getvalue())
    return [dst]


def write_geojson(shapes, dst, layers, frame=None, dst_crs=None):
    geo = dst_crs is not None and dst_crs.kind == "wgs84"
    nd = 9 if geo else 4

    def c(p):
        return [[round(float(x), nd) for x in q] for q in p]
    feats = []
    for s in shapes:
        P = [np.asarray(p, float) for p in s["parts"]]
        if s["type"] == "point":
            g = ({"type": "Point", "coordinates": c(P[0])[0]} if len(P) == 1 else
                 {"type": "MultiPoint", "coordinates": [c(p)[0] for p in P]})
        elif s["type"] == "polyline":
            g = ({"type": "LineString", "coordinates": c(P[0])} if len(P) == 1 else
                 {"type": "MultiLineString", "coordinates": [c(p) for p in P]})
        else:
            holes = s.get("holes") or [False] * len(P)
            polys = []
            for p, h in zip(P, holes):
                p = _close(p)
                p = p if (_ring_signed_area(p) > 0) != bool(h) else p[::-1]      # RFC 7946: outer CCW
                if h and polys:
                    polys[-1].append(c(p))
                else:
                    polys.append([c(p)])
            g = {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else \
                {"type": "MultiPolygon", "coordinates": polys}
        props = {k: (v.item() if isinstance(v, np.generic) else v) for k, v in _shape_attrs(s, layers).items()}
        feats.append({"type": "Feature", "properties": props, "geometry": g})
    d = {"type": "FeatureCollection", "name": os.path.splitext(os.path.basename(dst))[0], "features": feats}
    epsg = dst_crs.epsg if dst_crs is not None and dst_crs.kind != "project" else (frame.epsg if frame else None)
    if epsg and epsg != 4326:
        d["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"}}
    elif not epsg:
        # local coordinates are not WGS 84: say so (PhotoMesh reads it back as project coordinates)
        d["photomesh_crs"] = (frame.crs_text if frame is not None and frame.crs_text else "Local Coordinates (m)")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, default=_jdefault)
    return [dst]


# ====================================================== photo projection
def frame_box(cam, margin=0.03, n=48):
    """Normalised (undistorted) bounds of the photo frame: the border pixels
    undistorted with the camera model, plus a small margin."""
    from . import colmap_io as cio
    W, H = cam.width, cam.height
    t = np.linspace(0, 1, n)
    border = np.vstack([np.column_stack([t * W, np.zeros(n)]), np.column_stack([t * W, np.full(n, H)]),
                        np.column_stack([np.zeros(n), t * H]), np.column_stack([np.full(n, W), t * H])])
    x, y = cio.undistort_points(cam, border, iters=20)
    lo = np.array([x.min(), y.min()]); hi = np.array([x.max(), y.max()])
    m = (hi - lo) * margin
    return lo - m, hi + m


def prepare_polylines(shapes_parts, closed=None):
    """Concatenate the 3D polylines of many shapes for project_batch():
    shapes_parts[j] = list of (k, 3) parts of shape j; closed[j] closes its
    rings. Done once per shapes version; every photo then reuses it."""
    V, sa, sp, pshape = [], [], [], []
    base = 0; npart = 0
    for j, parts in enumerate(shapes_parts):
        cl = bool(closed[j]) if closed is not None else False
        for P in parts:
            P = np.asarray(P, float).reshape(-1, 3)
            if cl and len(P) >= 3 and not np.allclose(P[0], P[-1]):
                P = np.vstack([P, P[:1]])
            if len(P) < 2:
                continue
            V.append(P)
            sa.append(np.arange(base, base + len(P) - 1))
            sp.append(np.full(len(P) - 1, npart))
            pshape.append(j)
            base += len(P); npart += 1
    if not V:
        z = np.zeros(0, np.int64)
        return {"V": np.zeros((0, 3)), "sa": z, "seg_part": z, "part_shape": z}
    return {"V": np.vstack(V), "sa": np.concatenate(sa), "seg_part": np.concatenate(sp),
            "part_shape": np.asarray(pshape, np.int64)}


def project_batch(prep, cam, R, t, box=None, px_step=6.0, near=0.05, scene=None, occl_tol=0.004,
                  hidden=False):
    """Vectorised projection of all polylines of prepare_polylines() into one
    photo: near-plane clipping, frame culling of whole segments (exact: a
    segment in front of the camera projects inside the box of its end
    points), densification (~px_step px) before the lens distortion, one
    batched ray cast for the occlusion (scene), and the split into visible
    (and hidden) runs - no Python loop over shapes, segments or samples.
    Returns [(shape index, (k, 2) pixels)] or (visible, hidden)."""
    from . import colmap_io as cio
    empty = ([], []) if hidden else []
    if box is None:
        box = frame_box(cam)
    lo, hi = box
    V = prep["V"]
    if not len(prep["sa"]):
        return empty
    fx, fy, cx, cy = cam.K()
    f = max(fx, fy)
    R = np.asarray(R, float); t = np.asarray(t, float)
    Xc = V @ R.T + t
    sa = prep["sa"]
    A, B = Xc[sa], Xc[sa + 1]
    za, zb = A[:, 2], B[:, 2]
    keep = (za >= near) | (zb >= near)
    cut_a = keep & (za < near); cut_b = keep & (zb < near)
    s_ = np.where(cut_a | cut_b, (near - za) / np.where(zb != za, zb - za, 1e-12), 0.0)
    A = np.where(cut_a[:, None], A + (B - A) * s_[:, None], A)
    B = np.where(cut_b[:, None], A + (B - A) * s_[:, None], B)
    zA = np.where(keep, A[:, 2], 1.0); zB = np.where(keep, B[:, 2], 1.0)
    na = A[:, :2] / zA[:, None]; nb = B[:, :2] / zB[:, None]
    inside = keep & ~((np.maximum(na[:, 0], nb[:, 0]) < lo[0]) | (np.minimum(na[:, 0], nb[:, 0]) > hi[0]) |
                      (np.maximum(na[:, 1], nb[:, 1]) < lo[1]) | (np.minimum(na[:, 1], nb[:, 1]) > hi[1]))
    ks = np.flatnonzero(inside)
    if not len(ks):
        return empty
    A, B, na, nb = A[ks], B[ks], na[ks], nb[ks]
    part = prep["seg_part"][ks]
    L = np.linalg.norm(nb - na, axis=1) * f
    n = np.clip(np.ceil(L / px_step).astype(np.int64), 1, 2000)
    cnt = n + 1
    seg = np.repeat(np.arange(len(ks)), cnt)
    first = np.r_[0, np.cumsum(cnt)[:-1]]
    u = (np.arange(len(seg)) - first[seg]) / n[seg]
    S = A[seg] + (B[seg] - A[seg]) * u[:, None]               # 3D samples, camera frame
    x, y = S[:, 0] / S[:, 2], S[:, 1] / S[:, 2]
    ok = (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1])
    hid = np.zeros(len(S), bool)
    if scene is not None and ok.any():
        occ = np.zeros(len(S), bool)
        occ[ok] = _occluded(scene, S[ok], R, t, occl_tol)
        hid = ok & occ
        ok &= ~occ
    # breaks: first sample of a part, after a near-plane cut, after culled segments
    newpart = np.r_[True, part[1:] != part[:-1]]
    gap = np.r_[True, np.diff(ks) > 1]
    prev_cut = np.r_[True, cut_b[ks][:-1]] | cut_a[ks]
    brk = np.zeros(len(S), bool)
    brk[first[newpart | gap | prev_cut]] = True
    m = ok | hid
    uv = np.zeros((len(S), 2))
    if m.any():
        xd, yd = cio.distort(cam, x[m], y[m])
        uv[m, 0] = fx * xd + cx; uv[m, 1] = fy * yd + cy
    owner = prep["part_shape"][part[seg]]
    vis = _runs(uv, ok, brk, owner)
    if hidden:
        return vis, (_runs(uv, hid, brk, owner) if hid.any() else [])
    return vis


def _runs(uv, ok, brk, owner):
    """[(owner, (k, 2) polyline)] of the runs of `ok` samples, broken at brk
    and at gaps; consecutive duplicate points dropped; runs < 2 points dropped."""
    i = np.flatnonzero(ok)
    if len(i) < 2:
        return []
    start = np.ones(len(i), bool)
    start[1:] = (i[1:] != i[:-1] + 1) | brk[i[1:]]
    d = uv[i]
    same = np.zeros(len(i), bool)
    same[1:] = ~start[1:] & (np.abs(d[1:] - d[:-1]) <= 1e-8 + 1e-5 * np.abs(d[1:])).all(1)
    keep = ~same
    i, st, d = i[keep], start[keep], d[keep]
    b = np.flatnonzero(st)
    lens = np.diff(np.r_[b, len(i)])
    good = lens >= 2
    if not good.any():
        return []
    own = owner[i[b]]
    pieces = np.split(d, b[1:])
    return [(int(own[k]), pieces[k]) for k in np.flatnonzero(good)]


def project_polylines(parts, cam, R, t, box=None, px_step=6.0, near=0.05, scene=None, occl_tol=0.004,
                      closed=False, hidden=False):
    """Project 3D polylines (model frame) into a photo with the COLMAP camera
    (pose R, t world->camera, distortion of the camera model).

    Segments are clipped against the near plane (behind the camera) and the
    photo frame in normalised undistorted coordinates, where 3D lines stay
    straight; the visible pieces are densified (~px_step pixels) before the
    lens distortion is applied, so long lines bend like the image does.
    scene: Open3D RaycastingScene of the mesh -> parts hidden by the surface
    are left out. Returns a list of (k, 2) pixel polylines; hidden=True returns
    (visible, hidden) lists, hidden = the parts inside the frame that the
    surface covers (drawn dashed in the transparent view).
    (One-shape form of project_batch.)"""
    prep = prepare_polylines([parts], [closed])
    r = project_batch(prep, cam, R, t, box, px_step, near, scene, occl_tol, hidden=hidden)
    if hidden:
        return [p for _o, p in r[0]], [p for _o, p in r[1]]
    return [p for _o, p in r]


def project_ring_fill(P, cam, R, t, box=None, near=0.05):
    """Polygon ring -> pixel polygon for a fill, or None when a vertex is behind
    the camera. Vertices outside the photo are clamped to 1.5x the frame box in
    normalised coordinates before the distortion (which diverges outside)."""
    from . import colmap_io as cio
    if box is None:
        box = frame_box(cam)
    lo, hi = np.asarray(box[0], float), np.asarray(box[1], float)
    c, h = (lo + hi) / 2, (hi - lo) / 2 * 1.5
    Xc = np.asarray(P, float) @ np.asarray(R, float).T + np.asarray(t, float)
    if len(Xc) < 3 or (Xc[:, 2] <= near).any():
        return None
    x = np.clip(Xc[:, 0] / Xc[:, 2], c[0] - h[0], c[0] + h[0])
    y = np.clip(Xc[:, 1] / Xc[:, 2], c[1] - h[1], c[1] + h[1])
    if (x.max() < lo[0]) or (x.min() > hi[0]) or (y.max() < lo[1]) or (y.min() > hi[1]):
        return None
    fx, fy, cx, cy = cam.K()
    xd, yd = cio.distort(cam, x, y)
    return np.column_stack([fx * xd + cx, fy * yd + cy])


def project_points(P, cam, R, t, box=None, near=0.05, scene=None, occl_tol=0.004, hidden=False):
    """-> (k, 2) pixels and the indices of the visible points (hidden=True:
    plus the pixels and indices of the points in the frame that the surface hides)."""
    from . import colmap_io as cio
    if box is None:
        box = frame_box(cam)
    lo, hi = box
    P = np.asarray(P, float).reshape(-1, 3)
    Xc = P @ np.asarray(R, float).T + np.asarray(t, float)
    z = Xc[:, 2]
    zz = np.where(z > near, z, 1.0)
    x, y = Xc[:, 0] / zz, Xc[:, 1] / zz
    ok = (z > near) & (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1])
    hid = np.zeros(len(P), bool)
    if scene is not None and ok.any():
        occ = np.zeros(len(P), bool)
        occ[ok] = _occluded(scene, Xc[ok], R, t, occl_tol)
        hid = ok & occ
        ok &= ~occ
    fx, fy, cx, cy = cam.K()

    def px(m):
        xd, yd = cio.distort(cam, x[m], y[m])
        return np.column_stack([fx * xd + cx, fy * yd + cy])
    if hidden:
        return px(ok), np.flatnonzero(ok), px(hid), np.flatnonzero(hid)
    return px(ok), np.flatnonzero(ok)


def _occluded(scene, Xc, R, t, tol):
    """Camera-frame points hidden by the mesh (a hit clearly before the point)."""
    import open3d as o3d
    R = np.asarray(R, float); t = np.asarray(t, float)
    C = -R.T @ t
    Xw = (Xc - t) @ R                      # camera -> model frame
    d = Xw - C
    dist = np.linalg.norm(d, axis=1)
    rays = np.empty((len(Xw), 6), np.float32)
    rays[:, :3] = C; rays[:, 3:] = d / np.maximum(dist, 1e-12)[:, None]
    th = np.empty(len(Xw))
    for a in range(0, len(Xw), 65536):                   # chunks: the GUI thread gets the GIL between them
        th[a:a + 65536] = scene.cast_rays(o3d.core.Tensor(rays[a:a + 65536]))["t_hit"].numpy()
    return np.isfinite(th) & (th < dist * (1.0 - tol) - 0.05)
