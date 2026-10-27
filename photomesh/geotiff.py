"""Own GeoTIFF writer / reader (no GDAL, tifffile or imagecodecs needed).

Writer: tiled TIFF or BigTIFF (automatic above ~3.9 GB), any number of
overview levels (reduced-resolution IFDs, NewSubfileType=1, which QGIS/GDAL
use for fast display), compression None / LZW / Deflate / JPEG, horizontal
predictor for 8-bit data, RGB(A) uint8 or single-band float32 (DEM),
alpha as an unassociated extra sample or a GDAL_NODATA value.
Georeferencing: ModelPixelScale + ModelTiepoint in REAL world coordinates
(PixelIsArea; the tiepoint is the upper-left corner of the upper-left pixel)
and a GeoKeyDirectory (EPSG code when the CRS has one, otherwise a
user-defined / local metre system with the CRS name as citation).

Codecs: Deflate = zlib (stdlib). LZW = libtiff through Pillow: each tile is
encoded as a one-strip TIFF in memory and its strip copied into the tiled
file. JPEG = Pillow's JPEG encoder, one complete JFIF stream per tile,
RGB without colour transform (Photometric RGB), which libtiff/GDAL decode.
Tile data are written as they arrive; the IFDs go at the end of the file
and the header is patched last (file written as <path>.part then renamed).
"""
from __future__ import annotations

import io
import math
import os
import struct
import threading
import zlib

import numpy as np

COMPRESSION = {"None": 1, "LZW": 5, "JPEG": 7, "Deflate": 8}
COMPRESSION_NAME = {v: k for k, v in COMPRESSION.items()}
COMPRESSION_NAME[32946] = "Deflate"
BIGTIFF_LIMIT = 3_900_000_000

# TIFF field types
_SHORT, _LONG, _ASCII, _DOUBLE, _LONG8, _RATIONAL = 3, 4, 2, 12, 16, 5
_TYPE_FMT = {1: "B", 2: "s", 3: "H", 4: "I", 5: "II", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f",
             12: "d", 16: "Q", 17: "q", 18: "Q"}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}

_PIL_LOCK = threading.Lock()


# ------------------------------------------------------------------ geokeys
def parse_epsg(crs):
    """EPSG code from 'EPSG:32639', a WKT with a final AUTHORITY["EPSG","32639"]
    on the root, or None for local / unknown systems."""
    if not crs:
        return None
    s = str(crs).strip()
    import re
    m = re.match(r"^\s*EPSG\s*:\s*(\d+)\s*$", s, re.I)
    if m:
        return int(m.group(1))
    if s.upper().startswith("LOCAL_CS") or s.upper().startswith("LOCALCS"):
        return None
    m = re.search(r'AUTHORITY\["EPSG","(\d+)"\]\]\s*$', s)
    if m and (s.upper().startswith("PROJCS") or s.upper().startswith("GEOGCS")):
        return int(m.group(1))
    return None


def is_geographic(crs):
    s = str(crs or "").upper().strip()
    if s.startswith("GEOGCS") or s.startswith("GEOGCRS"):
        return True
    return parse_epsg(crs) in (4326, 4258, 4269, 4019)


def crs_name(crs):
    import re
    s = str(crs or "")
    m = re.match(r'\s*[A-Z_]+\["([^"]+)"', s)
    return m.group(1) if m else (s or "Local Coordinates (m)")


def geokeys(crs):
    """(GeoKeyDirectory shorts, ascii params)."""
    epsg = parse_epsg(crs)
    keys = []      # (key, location, count, value)
    ascii_ = ""
    if epsg and is_geographic(crs):
        keys += [(1024, 0, 1, 2), (1025, 0, 1, 1), (2048, 0, 1, epsg)]
    elif epsg:
        keys += [(1024, 0, 1, 1), (1025, 0, 1, 1), (3072, 0, 1, epsg)]
    else:
        name = crs_name(crs) + "|"
        ascii_ = name
        keys += [(1024, 0, 1, 1), (1025, 0, 1, 1), (1026, 34737, len(name), 0),
                 (3072, 0, 1, 32767), (3076, 0, 1, 9001)]
    keys.sort()
    d = [1, 1, 0, len(keys)]
    for k in keys:
        d += list(k)
    return d, ascii_


def world_file_lines(x0, y1, px, py=None):
    """World file (.tfw/.jgw/.pgw): pixel size, rotation, rotation, -pixel size,
    X and Y of the CENTRE of the upper-left pixel."""
    py = px if py is None else py
    return [f"{px:.12f}", "0.000000000000", "0.000000000000", f"{-py:.12f}",
            f"{x0 + px / 2:.6f}", f"{y1 - py / 2:.6f}"]


def write_world_file(path, x0, y1, px, py=None):
    with open(path, "w", encoding="ascii", newline="\r\n") as f:
        f.write("\n".join(world_file_lines(x0, y1, px, py)) + "\n")
    return path


def world_file_ext(path):
    ext = os.path.splitext(path)[1].lower()
    return {".tif": ".tfw", ".tiff": ".tfw", ".jpg": ".jgw", ".jpeg": ".jgw", ".png": ".pgw"}.get(ext, ".wld")


def prj_text(crs):
    """WKT for a .prj file - only when the CRS is a real WKT (local systems get no .prj)."""
    s = str(crs or "").strip()
    if s.upper().startswith(("PROJCS", "GEOGCS")):
        return s
    return None


# ------------------------------------------------------------------ codecs
def _predict(a):
    """Horizontal differencing (TIFF predictor 2) for uint8 (h, w, spp)."""
    d = a.copy()
    d[:, 1:] = a[:, 1:] - a[:, :-1]
    return d


def _unpredict(a):
    return np.cumsum(a, axis=1, dtype=a.dtype)


def _lzw_encode_pillow(arr):
    from PIL import Image, TiffImagePlugin
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[..., 0]
    if arr.dtype == np.float32:
        im = Image.fromarray(arr, "F")
    else:
        im = Image.fromarray(arr)
    with _PIL_LOCK:
        TiffImagePlugin.STRIP_SIZE = 1 << 31
        b = io.BytesIO()
        im.save(b, "TIFF", compression="tiff_lzw")
    raw = b.getvalue()
    b.seek(0)
    with Image.open(b) as r:
        offs, cnts = r.tag_v2[273], r.tag_v2[279]
    if len(offs) != 1:
        raise RuntimeError("LZW encoder produced several strips")
    return raw[offs[0]:offs[0] + cnts[0]]


def lzw_decode(data):
    """TIFF LZW (MSB-first, early change) decoder."""
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, nbits, code_len = 0, 0, 9
    prev = None
    for byte in data:
        bits = ((bits & 0xFFFFFF) << 8) | byte; nbits += 8
        while nbits >= code_len:
            nbits -= code_len
            code = (bits >> nbits) & ((1 << code_len) - 1)
            if code == 256:
                table = table[:258]; code_len = 9; prev = None; continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]; table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]; table.append(entry)
            out += entry
            prev = entry
            n = len(table) + 1
            code_len = 12 if n >= 2048 else 11 if n >= 1024 else 10 if n >= 512 else 9
    return bytes(out)


def encode_tile(arr, compression, predictor, quality=90):
    """Compressed bytes of one full tile (h, w, spp)."""
    if compression == 7:
        from PIL import Image
        b = io.BytesIO()
        im = Image.fromarray(arr[..., :3] if arr.ndim == 3 else arr)
        im.save(b, "JPEG", quality=int(quality), keep_rgb=arr.ndim == 3, subsampling=0)
        return b.getvalue()
    a = _predict(arr) if predictor == 2 else arr
    if compression == 8:
        return zlib.compress(np.ascontiguousarray(a).tobytes(), 6)
    if compression == 5:
        return _lzw_encode_pillow(np.ascontiguousarray(a))
    return np.ascontiguousarray(a).tobytes()


def decode_tile(data, comp, pred, tw, th, spp, dtype, photometric=2, jpegtables=None):
    if comp in (8, 32946):
        raw = zlib.decompress(data)
    elif comp == 5:
        raw = lzw_decode(data)
    elif comp == 1:
        raw = data
    elif comp in (7, 6):
        from PIL import Image
        if jpegtables:
            data = jpegtables[:-2] + data[2:]
        with Image.open(io.BytesIO(data)) as im:
            a = np.asarray(im.convert("RGB") if spp == 3 else im)
        return a if a.ndim == 3 else a[..., None]
    else:
        raise ValueError(f"unsupported TIFF compression {comp}")
    n = tw * th * spp
    a = np.frombuffer(raw, np.dtype(dtype).newbyteorder("<"), count=n).reshape(th, tw, spp)
    return _unpredict(a) if pred == 2 else a


# ------------------------------------------------------------------ writer
class TiledTiffWriter:
    """levels: [(width, height)] - level 0 = full resolution, then overviews.
    dtype uint8 (bands 1/3/4) or float32 (bands 1)."""

    def __init__(self, path, levels, bands, dtype=np.uint8, tile=512, compression="Deflate",
                 alpha=True, nodata=None, geo=None, crs=None, bigtiff=None, jpeg_quality=90,
                 software="PhotoMesh Studio", description=None):
        self.path = path
        self.levels = [(int(w), int(h)) for w, h in levels]
        self.bands, self.dtype, self.tile = int(bands), np.dtype(dtype), int(tile)
        self.comp = COMPRESSION[compression] if isinstance(compression, str) else int(compression)
        if self.comp == 7 and (self.dtype != np.uint8 or self.bands not in (1, 3)):
            raise ValueError("JPEG compression needs 8-bit RGB or grey data (no alpha band)")
        self.pred = 2 if (self.dtype == np.uint8 and self.comp in (5, 8)) else 1
        self.alpha = bool(alpha) and self.bands in (2, 4)
        self.nodata, self.geo, self.crs = nodata, geo, crs
        self.quality = jpeg_quality
        self.software, self.description = software, description
        raw = sum(w * h for w, h in self.levels) * self.bands * self.dtype.itemsize
        self.big = bool(bigtiff) if bigtiff is not None else raw > BIGTIFF_LIMIT
        self.ntiles = [(math.ceil(w / self.tile), math.ceil(h / self.tile)) for w, h in self.levels]
        self.offsets = [np.zeros(nx * ny, np.uint64) for nx, ny in self.ntiles]
        self.counts = [np.zeros(nx * ny, np.uint64) for nx, ny in self.ntiles]
        self.tmp = path + ".part"
        self.fh = open(self.tmp, "wb")
        self.lock = threading.Lock()
        self.fh.write(b"\0" * 16)           # header patched in close()
        self.pos = 16
        self._empty = {}

    def _pad(self, a):
        t = self.tile
        a = np.asarray(a, self.dtype)
        if a.ndim == 2:
            a = a[..., None]
        if a.shape[0] == t and a.shape[1] == t:
            return a
        o = np.zeros((t, t, a.shape[2]), self.dtype)
        if self.nodata is not None and self.dtype != np.uint8:
            o[:] = self.nodata
        o[:a.shape[0], :a.shape[1]] = a
        return o

    def encode(self, arr):
        a = self._pad(arr)
        if a.shape[2] == 1:
            a2 = a[..., 0] if self.comp == 7 else a
        else:
            a2 = a
        return encode_tile(a2, self.comp, self.pred, self.quality)

    def write_encoded(self, level, ti, tj, data):
        """ti = tile row, tj = tile column."""
        nx = self.ntiles[level][0]
        with self.lock:
            if self.pos % 2:
                self.fh.write(b"\0"); self.pos += 1
            self.fh.write(data)
            k = ti * nx + tj
            self.offsets[level][k] = self.pos
            self.counts[level][k] = len(data)
            self.pos += len(data)
            if not self.big and self.pos > 0xFFFFFFF0:
                raise OverflowError("TIFF larger than 4 GB - use BigTIFF")

    def write_tile(self, level, ti, tj, arr):
        self.write_encoded(level, ti, tj, self.encode(arr))

    def write_block(self, level, r0, c0, arr, executor=None):
        """Write an array whose upper-left pixel is (r0, c0); both multiples of the tile size."""
        t = self.tile
        assert r0 % t == 0 and c0 % t == 0
        h, w = arr.shape[:2]
        jobs = [(ti, tj) for ti in range(math.ceil(h / t)) for tj in range(math.ceil(w / t))]
        def enc(job):
            ti, tj = job
            return job, self.encode(arr[ti * t:(ti + 1) * t, tj * t:(tj + 1) * t])
        res = executor.map(enc, jobs) if executor is not None else map(enc, jobs)
        for (ti, tj), data in res:
            self.write_encoded(level, r0 // t + ti, c0 // t + tj, data)

    def read_tile(self, level, ti, tj):
        """Decode a tile already written (used to build the overviews)."""
        k = ti * self.ntiles[level][0] + tj
        n = int(self.counts[level][k])
        t = self.tile
        if n == 0:
            z = np.zeros((t, t, self.bands), self.dtype)
            if self.nodata is not None and self.dtype != np.uint8:
                z[:] = self.nodata
            return z
        with self.lock:
            self.fh.flush()
            with open(self.tmp, "rb") as r:
                r.seek(int(self.offsets[level][k])); data = r.read(n)
        return decode_tile(data, self.comp, self.pred, t, t, self.bands, self.dtype)

    def read_region(self, level, r0, c0, h, w):
        W, H = self.levels[level]
        t = self.tile
        r1, c1 = min(r0 + h, H), min(c0 + w, W)
        out = np.zeros((r1 - r0, c1 - c0, self.bands), self.dtype)
        for ti in range(r0 // t, (r1 - 1) // t + 1):
            for tj in range(c0 // t, (c1 - 1) // t + 1):
                a = self.read_tile(level, ti, tj)
                y0, x0 = ti * t, tj * t
                ys, xs, ye, xe = max(r0, y0), max(c0, x0), min(r1, y0 + t), min(c1, x0 + t)
                out[ys - r0:ye - r0, xs - c0:xe - c0] = a[ys - y0:ye - y0, xs - x0:xe - x0]
        return out

    def _fill_missing(self):
        for lev, cnt in enumerate(self.counts):
            miss = np.flatnonzero(cnt == 0)
            if not len(miss):
                continue
            if "e" not in self._empty:
                z = np.zeros((self.tile, self.tile, self.bands), self.dtype)
                if self.nodata is not None and self.dtype != np.uint8:
                    z[:] = self.nodata
                self._empty["e"] = self.encode(z)
            nx = self.ntiles[lev][0]
            for k in miss:
                self.write_encoded(lev, int(k // nx), int(k % nx), self._empty["e"])

    def _tags(self, level, extern):
        w, h = self.levels[level]
        spp = self.bands
        bits = 8 * self.dtype.itemsize
        photometric = 2 if spp >= 3 else 1
        tags = [(254, _LONG, [0 if level == 0 else 1]), (256, _LONG, [w]), (257, _LONG, [h]),
                (258, _SHORT, [bits] * spp), (259, _SHORT, [self.comp]), (262, _SHORT, [photometric]),
                (277, _SHORT, [spp]), (284, _SHORT, [1]), (317, _SHORT, [self.pred]),
                (322, _SHORT if self.tile < 65536 else _LONG, [self.tile]),
                (323, _SHORT if self.tile < 65536 else _LONG, [self.tile]),
                (324, _LONG8 if self.big else _LONG, self.offsets[level]),
                (325, _LONG8 if self.big else _LONG, self.counts[level]),
                (339, _SHORT, [3 if self.dtype.kind == "f" else 1] * spp)]
        if spp in (2, 4):
            tags.append((338, _SHORT, [2 if self.alpha else 0]))
        if level == 0:
            tags.append((305, _ASCII, self.software))
            if self.description:
                tags.append((270, _ASCII, self.description))
            if self.geo:
                x0, y1, px, py = self.geo
                tags.append((33550, _DOUBLE, [px, py, 0.0]))
                tags.append((33922, _DOUBLE, [0.0, 0.0, 0.0, x0, y1, 0.0]))
                d, asc = geokeys(self.crs)
                tags.append((34735, _SHORT, d))
                if asc:
                    tags.append((34737, _ASCII, asc))
        if self.nodata is not None:
            tags.append((42113, _ASCII, repr(float(self.nodata)) if self.dtype.kind == "f" else str(int(self.nodata))))
        tags.sort(key=lambda t: t[0])
        return tags

    def close(self):
        self._fill_missing()
        fh = self.fh
        ifd_offsets = []
        entry = 20 if self.big else 12
        inline = 8 if self.big else 4
        for level in range(len(self.levels)):
            if self.pos % 2:
                fh.write(b"\0"); self.pos += 1
            tags = self._tags(level, None)
            ifd_pos = self.pos
            ifd_size = (8 if self.big else 2) + len(tags) * entry + (8 if self.big else 4)
            extra_pos = ifd_pos + ifd_size
            entries, extra = [], bytearray()
            for tag, typ, vals in tags:
                if typ == _ASCII:
                    data = (vals if isinstance(vals, bytes) else str(vals).encode("ascii", "replace")) + b"\0"
                    count = len(data)
                else:
                    fmt = _TYPE_FMT[typ]
                    vals = list(vals)
                    count = len(vals)
                    if typ == _DOUBLE:
                        data = struct.pack(f"<{count}d", *[float(v) for v in vals])
                    else:
                        data = np.asarray(vals, {"H": "<u2", "I": "<u4", "Q": "<u8"}[fmt]).tobytes()
                if len(data) <= inline:
                    val = data.ljust(inline, b"\0")
                else:
                    if (extra_pos + len(extra)) % 2:
                        extra += b"\0"
                    off = extra_pos + len(extra)
                    extra += data
                    val = struct.pack("<Q" if self.big else "<I", off)
                entries.append(struct.pack("<HHQ" if self.big else "<HHI", tag, typ, count) + val)
            nxt = 0          # patched below
            blob = (struct.pack("<Q" if self.big else "<H", len(tags)) + b"".join(entries)
                    + struct.pack("<Q" if self.big else "<I", nxt))
            fh.write(blob); fh.write(extra)
            ifd_offsets.append((ifd_pos, ifd_pos + len(blob) - (8 if self.big else 4)))
            self.pos += len(blob) + len(extra)
        # chain IFDs
        for k in range(len(ifd_offsets) - 1):
            fh.seek(ifd_offsets[k][1]); fh.write(struct.pack("<Q" if self.big else "<I", ifd_offsets[k + 1][0]))
        fh.seek(0)
        if self.big:
            fh.write(b"II" + struct.pack("<HHHQ", 43, 8, 0, ifd_offsets[0][0]))
        else:
            fh.write(b"II" + struct.pack("<HI", 42, ifd_offsets[0][0]))
        fh.flush(); os.fsync(fh.fileno()); fh.close()
        from .georef import replace_file
        replace_file(self.tmp, self.path)
        return self.path

    def abort(self):
        try:
            self.fh.close()
        finally:
            if os.path.exists(self.tmp):
                os.remove(self.tmp)


def overview_levels(w, h, min_size=256):
    levels = [(w, h)]
    while max(levels[-1]) > min_size:
        pw, ph = levels[-1]
        levels.append(((pw + 1) // 2, (ph + 1) // 2))
    return levels


# ------------------------------------------------------------------ reader
class TiffLevel:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def nx(self):
        return math.ceil(self.width / self.tw)

    @property
    def ny(self):
        return math.ceil(self.height / self.th)


class TiffReader:
    """Reads tiled or stripped TIFF/BigTIFF levels tile by tile (thread-safe).
    Uncompressed / Deflate / LZW / JPEG (via Pillow), uint8/uint16/float32."""

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.lock = threading.Lock()
        hdr = self.fh.read(16)
        if hdr[:2] != b"II":
            if hdr[:2] == b"MM":
                raise ValueError("big-endian TIFF is not supported")
            raise ValueError("not a TIFF file")
        ver = struct.unpack("<H", hdr[2:4])[0]
        self.big = ver == 43
        off = struct.unpack("<Q", hdr[8:16])[0] if self.big else struct.unpack("<I", hdr[4:8])[0]
        self.levels, self.tags0 = [], None
        self.masks = []
        seen = set()
        while off and off not in seen:
            seen.add(off)
            tags, off = self._ifd(off)
            if self.tags0 is None:
                self.tags0 = tags
            sub = tags.get(254, [0])[0]
            lev = self._level(tags)
            if sub & 4:
                self.masks.append(lev)
            elif lev is not None:
                self.levels.append(lev)
        if not self.levels:
            raise ValueError("no image in TIFF")
        t = self.tags0
        self.width, self.height = self.levels[0].width, self.levels[0].height
        self.bands = self.levels[0].spp
        self.dtype = self.levels[0].dtype
        self.geo = None
        if 33550 in t and 33922 in t:
            sx, sy = t[33550][0], t[33550][1]
            tp = t[33922]
            i, j, X, Y = tp[0], tp[1], tp[3], tp[4]
            self.geo = (X - i * sx, Y + j * sy, sx, sy)      # x0 (left), y1 (top), px, py
        nd = t.get(42113)
        try:
            self.nodata = float(nd.strip("\0 ")) if nd else None
        except ValueError:
            self.nodata = None
        self.alpha = self.bands in (2, 4) and t.get(338, [0])[0] in (1, 2)
        self.jpegtables = t.get(347)
        self.compression = COMPRESSION_NAME.get(self.levels[0].comp, str(self.levels[0].comp))
        self.geokeys = t.get(34735)
        self.geoascii = t.get(34737)

    def epsg(self):
        k = self.geokeys
        if not k:
            return None
        for i in range(4, len(k), 4):
            if k[i] in (3072, 2048) and k[i + 1] == 0 and k[i + 3] not in (0, 32767):
                return int(k[i + 3])
        return None

    def _read(self, off, n):
        with self.lock:
            self.fh.seek(off)
            return self.fh.read(n)

    def _ifd(self, off):
        if self.big:
            n = struct.unpack("<Q", self._read(off, 8))[0]
            raw = self._read(off + 8, n * 20 + 8)
            esz, cfmt, vsz = 20, "<HHQ", 8
        else:
            n = struct.unpack("<H", self._read(off, 2))[0]
            raw = self._read(off + 2, n * 12 + 4)
            esz, cfmt, vsz = 12, "<HHI", 4
        tags = {}
        for k in range(n):
            e = raw[k * esz:(k + 1) * esz]
            tag, typ, cnt = struct.unpack(cfmt, e[:esz - vsz])
            size = _TYPE_SIZE.get(typ, 1) * cnt
            vb = e[esz - vsz:]
            data = vb[:size] if size <= vsz else self._read(
                struct.unpack("<Q" if self.big else "<I", vb)[0], size)
            if typ == 2:
                tags[tag] = data.rstrip(b"\0").decode("latin-1")
            elif typ == 7 and tag == 347:
                tags[tag] = bytes(data)
            elif typ in _TYPE_FMT:
                f = _TYPE_FMT[typ]
                if typ == 5:
                    v = struct.unpack(f"<{2 * cnt}I", data)
                    tags[tag] = [v[i] / max(v[i + 1], 1) for i in range(0, len(v), 2)]
                else:
                    tags[tag] = list(struct.unpack(f"<{cnt}{f}", data))
        nxt = struct.unpack("<Q" if self.big else "<I", raw[-vsz:])[0]
        return tags, nxt

    def _level(self, t):
        if 256 not in t:
            return None
        w, h = t[256][0], t[257][0]
        spp = t.get(277, [1])[0]
        bits = t.get(258, [8])[0]
        fmt = t.get(339, [1])[0]
        dtype = np.dtype({(8, 1): np.uint8, (16, 1): np.uint16, (32, 3): np.float32, (16, 2): np.int16,
                          (32, 1): np.uint32, (32, 2): np.int32, (64, 3): np.float64, (1, 1): np.uint8}
                         .get((bits, fmt), np.uint8))
        if 322 in t:
            tw, th = t[322][0], t[323][0]
            offs, cnts = t[324], t[325]
        else:
            tw, th = w, t.get(278, [h])[0]
            offs, cnts = t[273], t[279]
        return TiffLevel(width=w, height=h, tw=tw, th=min(th, h) if 322 not in t else th, spp=spp, bits=bits,
                         dtype=dtype, comp=t.get(259, [1])[0], pred=t.get(317, [1])[0],
                         photometric=t.get(262, [1])[0], offsets=list(offs), counts=list(cnts),
                         tiled=322 in t)

    def read_tile(self, level, ti, tj):
        """(th, tw, spp) array of the tile (full tile size, padded)."""
        L = self.levels[level]
        k = ti * L.nx + tj
        th = L.th
        if not L.tiled:
            th = min(L.th, L.height - ti * L.th)
        data = self._read(L.offsets[k], L.counts[k])
        return decode_tile(data, L.comp, L.pred, L.tw, th, L.spp, L.dtype, L.photometric, self.jpegtables)

    def read_region(self, level, r0, c0, h, w):
        """Pixels [r0:r0+h, c0:c0+w] of a level (clipped to the image)."""
        L = self.levels[level]
        r1, c1 = min(r0 + h, L.height), min(c0 + w, L.width)
        out = np.zeros((max(0, r1 - r0), max(0, c1 - c0), L.spp), L.dtype)
        for ti in range(r0 // L.th, (r1 - 1) // L.th + 1 if r1 > r0 else 0):
            for tj in range(c0 // L.tw, (c1 - 1) // L.tw + 1 if c1 > c0 else 0):
                t = self.read_tile(level, ti, tj)
                y0, x0 = ti * L.th, tj * L.tw
                ys, xs = max(r0, y0), max(c0, x0)
                ye, xe = min(r1, y0 + t.shape[0]), min(c1, x0 + t.shape[1])
                if ye > ys and xe > xs:
                    out[ys - r0:ye - r0, xs - c0:xe - c0] = t[ys - y0:ye - y0, xs - x0:xe - x0]
        return out

    def read_level(self, level):
        L = self.levels[level]
        return self.read_region(level, 0, 0, L.height, L.width)

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def downsample2(a, alpha_band=None, nodata=None):
    """2x2 box filter. uint8 RGBA: alpha-weighted colour mean; float: nodata-aware mean."""
    h, w = a.shape[:2]
    if h % 2 or w % 2:
        pad = np.zeros(((h + 1) // 2 * 2, (w + 1) // 2 * 2) + a.shape[2:], a.dtype)
        if nodata is not None:
            pad[:] = nodata
        pad[:h, :w] = a
        a = pad
    if a.dtype == np.uint8:
        f = a.astype(np.float32)
        if alpha_band is not None:
            al = f[..., alpha_band:alpha_band + 1] / 255.0
            q = lambda x: x[0::2, 0::2] + x[1::2, 0::2] + x[0::2, 1::2] + x[1::2, 1::2]
            wsum = q(al)
            col = q(f * al) / np.maximum(wsum, 1e-6)
            col[..., alpha_band] = (q(f[..., alpha_band]) / 4 >= 127.5) * 255.0
            col[wsum[..., 0] <= 0] = 0
            return np.clip(col + 0.5, 0, 255).astype(np.uint8)
        q = (f[0::2, 0::2] + f[1::2, 0::2] + f[0::2, 1::2] + f[1::2, 1::2]) / 4
        return np.clip(q + 0.5, 0, 255).astype(np.uint8)
    v = [a[0::2, 0::2], a[1::2, 0::2], a[0::2, 1::2], a[1::2, 1::2]]
    st = np.stack(v, 0).astype(np.float64)
    if nodata is not None:
        ok = st != nodata
        s = np.where(ok, st, 0).sum(0); n = ok.sum(0)
        return np.where(n > 0, s / np.maximum(n, 1), nodata).astype(a.dtype)
    return st.mean(0).astype(a.dtype)


def build_overviews(writer: TiledTiffWriter, reader_fn=None, alpha_band=None, nodata=None, executor=None,
                    cancel=None, progress=None):
    """Fill levels 1.. of `writer` from level 0 by reading back blocks with
    reader_fn(level, r0, c0, h, w) (bounded memory: 2*tile x 2*tile... up to 4096 px)."""
    reader_fn = reader_fn or writer.read_region
    t = writer.tile
    B = max(t, 2048 // t * t)          # output block in the overview level
    nlev = len(writer.levels)
    for lev in range(1, nlev):
        W, H = writer.levels[lev]
        for r0 in range(0, H, B):
            for c0 in range(0, W, B):
                if cancel and cancel():
                    raise KeyboardInterrupt
                h, w = min(B, H - r0), min(B, W - c0)
                src = reader_fn(lev - 1, 2 * r0, 2 * c0, 2 * h, 2 * w)
                d = downsample2(src, alpha_band, nodata)[:h, :w]
                writer.write_block(lev, r0, c0, d, executor)
        if progress:
            progress(lev, nlev - 1)
