"""World-coordinate exports.

After georeferencing, the internal PLYs (tie points, dense cloud, mesh) and the
camera centres are stored in a *local* frame: world - offset, where the offset
(chunk.georef["offset"], e.g. [26700, 23000, 1300]) keeps float32 vertices
precise. Every export adds the offset back so the files land at the real
coordinates in Rhino / AutoCAD / GIS:

* PLY    - vertex x, y, z written as 'double' (float64); every other property
           and element (normals, colours, confidence, faces) copied unchanged.
           Binary files are streamed in chunks (27.8M-point clouds fit in a few
           hundred MB of RAM at most).
* OBJ    - vertices = local + offset, printed with 6 decimals (1 micrometre).
* DXF    - ASCII DXF R12 written by our own streaming writer (no dependency).
           Meshes as POLYFACE MESH entities (POLYLINE flag 64 + VERTEX
           records): Rhino imports each as one mesh object and AutoCAD as a
           polyface. R12 face indices are 16-bit, so the mesh is split into
           blocks of <= 32767 vertices. Clouds as POINT entities.
           Coordinates are written with %.17g (full double precision).
* GLB    - glTF positions are float32 by spec, so the vertices stay local and
           the offset goes into the root node 'translation' (converted to the
           glTF Y-up frame exactly like the vertices), so viewers that honour
           the node transform place it correctly. The offset is also stored
           in asset extras.

All files are written to a temporary file next to the target and then moved
into place with os.replace (no half-written results).
Without an offset (not georeferenced) nothing is added and the PLY export is a
plain copy, as before."""
from __future__ import annotations

import contextlib
import os
import shutil

import numpy as np

from .plyio import read_header, vertex_layout

DXF_MAX_VERTS = 32767


def georef_offset(georef):
    """float64 (3,) offset from a chunk.georef dict, or None when there is none."""
    off = (georef or {}).get("offset")
    if off is None:
        return None
    off = np.asarray(off, np.float64).reshape(3)
    return off if np.any(off != 0) else None


@contextlib.contextmanager
def atomic_write(dst, mode="wb", **kw):
    """Open a temporary file next to dst; replace dst only if the block succeeds."""
    dst = os.path.abspath(dst)
    tmp = dst + ".part"
    fh = open(tmp, mode, **kw)
    try:
        yield fh
        fh.flush(); os.fsync(fh.fileno()); fh.close()
        from .georef import replace_file
        replace_file(tmp, dst)
    except BaseException:
        fh.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _world_header(head, offset):
    """Header text with vertex x/y/z promoted to double and an offset comment."""
    lines = head.decode("ascii", "replace").replace("\r", "").split("\n")
    out, elem = [], None
    for l in lines:
        t = l.split()
        if t[:1] == ["element"]:
            elem = t[1]
        if t[:1] == ["property"] and elem == "vertex" and len(t) == 3 and t[2] in ("x", "y", "z"):
            l = f"property double {t[2]}"
        out.append(l)
        if t[:1] == ["format"]:
            out.append("comment PhotoMesh Studio export: world coordinates (local + offset "
                       + " ".join(f"{v:.3f}" for v in offset) + ")")
    while out and out[-1] == "":
        out.pop()
    return ("\n".join(out) + "\n").encode("ascii")


def _world_dtype(dt):
    end = "<" if dt.fields["x"][0].byteorder in "<=|" else ">"
    return np.dtype([(n, end + "f8") if n in ("x", "y", "z") else (n, dt.fields[n][0]) for n in dt.names])


def export_ply_world(src, dst, offset=None, chunk=2_000_000, log=None):
    """Copy a PLY (cloud or mesh) adding `offset` to x, y, z (as double)."""
    if offset is None:
        if os.path.abspath(src) != os.path.abspath(dst):
            with atomic_write(dst) as out, open(src, "rb") as fh:
                shutil.copyfileobj(fh, out, 16 << 20)
        return dst
    off = np.asarray(offset, np.float64)
    with open(src, "rb") as fh:
        lay = vertex_layout(fh)
        if lay is None:
            return _export_ply_world_general(src, dst, off)
        head, dt, n = lay
        odt = _world_dtype(dt)
        with atomic_write(dst) as out:
            out.write(_world_header(head, off))
            done = 0
            while done < n:
                v = np.fromfile(fh, dtype=dt, count=min(chunk, n - done))
                if not len(v):
                    raise ValueError(f"{src}: truncated vertex data")
                o = np.empty(len(v), odt)
                for name in dt.names:
                    o[name] = v[name]
                for k, name in enumerate("xyz"):
                    o[name] = v[name].astype(np.float64) + off[k]
                out.write(o.tobytes())
                done += len(v)
            shutil.copyfileobj(fh, out, 16 << 20)           # faces and any other elements, verbatim
    return dst


def _export_ply_world_general(src, dst, off):
    """ascii / unusual layouts: plyfile, in memory, written as binary."""
    from plyfile import PlyData, PlyElement
    with open(src, "rb") as fh:
        ply = PlyData.read(fh, mmap=False)
    v = ply["vertex"].data
    odt = [(n, "f8") if n in ("x", "y", "z") else (n, v.dtype.fields[n][0]) for n in v.dtype.names]
    o = np.empty(len(v), odt)
    for name in v.dtype.names:
        o[name] = v[name]
    for k, name in enumerate("xyz"):
        o[name] = np.asarray(v[name], np.float64) + off[k]
    els = [PlyElement.describe(o, "vertex")] + [e for e in ply.elements if e.name != "vertex"]
    comments = list(ply.comments) + ["PhotoMesh Studio export: world coordinates (local + offset "
                                     + " ".join(f"{x:.3f}" for x in off) + ")"]
    with atomic_write(dst) as out:
        PlyData(els, text=False, comments=comments).write(out)
    return dst


# ------------------------------------------------------------------ DXF
def _dxf_head(layer):
    # minimal R12 file: HEADER ($ACADVER) + ENTITIES; layers are created on use
    return "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"


_DXF_TAIL = "0\nENDSEC\n0\nEOF\n"


def _rows(fh, fmt, arr, chunk=100_000):
    for s in range(0, len(arr), chunk):
        blk = arr[s:s + chunk]
        fh.write((fmt * len(blk)) % tuple(blk.ravel().tolist()))


def _face_blocks(F, limit=DXF_MAX_VERTS, start=16384):
    """Split faces (in order) into blocks that reference <= limit distinct vertices."""
    i, m, step = 0, len(F), start
    while i < m:
        s = min(step, m - i)
        while True:
            u = np.unique(F[i:i + s])
            if len(u) <= limit or s == 1:
                break
            s //= 2
        yield F[i:i + s], u
        i += s
        step = s if s < step else min(step * 2, 65536)


def write_dxf_mesh(dst, V, F, offset=None, layer="PhotoMesh_Model", log=None):
    """Mesh as POLYFACE MESH entities (ASCII DXF R12), world = V + offset."""
    P = np.asarray(V, np.float64) + (np.asarray(offset, np.float64) if offset is not None else 0)
    F = np.asarray(F, np.int64)
    vfmt = "0\nVERTEX\n8\n" + layer + "\n10\n%.17g\n20\n%.17g\n30\n%.17g\n70\n192\n"
    ffmt = "0\nVERTEX\n8\n" + layer + "\n10\n0\n20\n0\n30\n0\n70\n128\n71\n%d\n72\n%d\n73\n%d\n"
    nblk = 0
    with atomic_write(dst, "w", encoding="ascii", newline="\n") as fh:
        fh.write(_dxf_head(layer))
        for blk, u in _face_blocks(F):
            local = np.searchsorted(u, blk) + 1                      # 1-based, positive = visible edges
            fh.write(f"0\nPOLYLINE\n8\n{layer}\n66\n1\n10\n0\n20\n0\n30\n0\n70\n64\n71\n{len(u)}\n72\n{len(blk)}\n")
            _rows(fh, vfmt, P[u])
            _rows(fh, ffmt, local)
            fh.write(f"0\nSEQEND\n8\n{layer}\n")
            nblk += 1
        fh.write(_DXF_TAIL)
    if log:
        log(f"DXF: {len(F):,} faces in {nblk} polyface mesh(es)")
    return dst


def export_dxf_mesh(src, dst, offset=None, log=None):
    from .texture import read_mesh
    V, F = read_mesh(src)
    return write_dxf_mesh(dst, V, F, offset, log=log)


def export_dxf_points(src, dst, offset=None, layer="PhotoMesh_Points", chunk=1_000_000, log=None):
    """Point cloud as POINT entities (streamed for binary PLYs)."""
    off = np.asarray(offset, np.float64) if offset is not None else np.zeros(3)
    fmt = "0\nPOINT\n8\n" + layer + "\n10\n%.17g\n20\n%.17g\n30\n%.17g\n"
    n_out = 0
    with atomic_write(dst, "w", encoding="ascii", newline="\n") as out:
        out.write(_dxf_head(layer))
        with open(src, "rb") as fh:
            lay = vertex_layout(fh)
            if lay is not None:
                _h, dt, n = lay
                while n_out < n:
                    v = np.fromfile(fh, dtype=dt, count=min(chunk, n - n_out))
                    if not len(v):
                        break
                    P = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64) + off
                    _rows(out, fmt, P); n_out += len(v)
            else:
                from .plyio import load_ply
                P = load_ply(src, max_points=0)["points"].astype(np.float64) + off
                _rows(out, fmt, P); n_out = len(P)
        out.write(_DXF_TAIL)
    if log:
        log(f"DXF: {n_out:,} points")
    return dst


def read_dxf_entities(path, limit=None):
    """Tiny DXF reader for tests: list of (type, {code: [values]})."""
    ents, cur = [], None
    with open(path, encoding="ascii") as fh:
        it = iter(fh)
        for code in it:
            code = code.strip(); val = next(it).rstrip("\n")
            if code == "0":
                if cur is not None:
                    ents.append(cur)
                    if limit and len(ents) >= limit:
                        break
                cur = (val, {})
            elif cur is not None:
                cur[1].setdefault(int(code), []).append(val)
    if cur is not None and not (limit and len(ents) >= limit):
        ents.append(cur)
    return ents


def polyface_meshes(path):
    """[(V (n,3), F (m,3) 0-based)] from our own DXF polyface output."""
    out, V, F, inside = [], [], [], False
    for t, g in read_dxf_entities(path):
        if t == "POLYLINE":
            V, F, inside = [], [], True
        elif t == "VERTEX" and inside:
            fl = int(g[70][0])
            if fl & 64:
                V.append([float(g[10][0]), float(g[20][0]), float(g[30][0])])
            else:
                F.append([abs(int(g[k][0])) - 1 for k in (71, 72, 73)])
        elif t == "SEQEND" and inside:
            out.append((np.asarray(V, np.float64), np.asarray(F, np.int64))); inside = False
    return out


# ------------------------------------------------------------------ cameras
def write_cameras_csv(dst, cameras, offset=None):
    off = np.asarray(offset, np.float64) if offset is not None else np.zeros(3)
    with atomic_write(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("label,x,y,z\n")
        for c in cameras:
            if c.est:
                p = np.asarray(c.est, np.float64) + off
                f.write(f"{c.label},{p[0]:.6f},{p[1]:.6f},{p[2]:.6f}\n")
    return dst
