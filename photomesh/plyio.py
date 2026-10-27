"""Minimal PLY loading for point clouds and meshes.

Files are read with plain file I/O (never memory-mapped: on Windows a mapped
file can't be replaced while the viewer holds it). Binary vertex/triangle
blocks are read with numpy directly; anything else falls back to plyfile."""
import numpy as np

_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
              "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
              "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def read_header(fh):
    """Parse a PLY header from an open binary file (left positioned at the data).
    Returns (header_bytes, endian '<'/'>' or None for ascii, elements) with
    elements = [(name, count, [(prop, dtype) | ('list', prop, count_dtype, item_dtype)])],
    or None if this isn't a parsable PLY."""
    head = b""
    while True:                                   # COLMAP on Windows writes CRLF headers
        line = fh.readline()
        if not line or len(head) > 1 << 20:
            return None
        head += line
        if line.rstrip(b"\r\n") == b"end_header":
            break
    if not head.startswith(b"ply"):
        return None
    lines = head.decode("ascii", "replace").replace("\r", "").split("\n")
    fmt = next((l.split()[1] for l in lines if l.startswith("format ")), "")
    end = {"binary_little_endian": "<", "binary_big_endian": ">"}.get(fmt)
    elems = []
    for l in lines:
        t = l.split()
        if t[:1] == ["element"]:
            elems.append((t[1], int(t[2]), []))
        elif t[:1] == ["property"] and elems:
            if t[1] == "list" and len(t) >= 5 and t[2] in _PLY_TYPES and t[3] in _PLY_TYPES:
                elems[-1][2].append(("list", t[4], (end or "<") + _PLY_TYPES[t[2]], (end or "<") + _PLY_TYPES[t[3]]))
            elif t[1] in _PLY_TYPES:
                elems[-1][2].append((t[2], (end or "<") + _PLY_TYPES[t[1]]))
            else:
                elems[-1][2].append(None)
    return head, end, elems


def vertex_layout(fh):
    """(header_bytes, vertex_dtype, vertex_count) if the file is binary and its
    first element is 'vertex' with scalar properties only, else None."""
    h = read_header(fh)
    if h is None or h[1] is None or not h[2]:
        return None
    name, n, props = h[2][0]
    if name != "vertex" or any(p is None or p[0] == "list" for p in props):
        return None
    return h[0], np.dtype(props), n


def read_fast(path):
    """{'vertex': structured array, 'face': (m,3) int32 or None} for binary
    PLYs with a scalar vertex element first and (optionally) a triangle face
    element after it; None if the layout needs the general reader."""
    with open(path, "rb") as fh:
        lay = vertex_layout(fh)
        if lay is None:
            return None
        head, dt, n = lay
        v = np.fromfile(fh, dtype=dt, count=n)
        if len(v) != n:
            raise ValueError(f"{path}: truncated vertex data")
        fh.seek(0)
        elems = read_header(fh)[2]
        fh.seek(len(head) + dt.itemsize * n)
        faces = None
        if len(elems) > 1:
            name, m, props = elems[1]
            if name != "face" or len(props) != 1 or props[0] is None or props[0][0] != "list":
                return None
            fdt = np.dtype([("n", props[0][2]), ("i", props[0][3], (3,))])
            f = np.fromfile(fh, dtype=fdt, count=m)
            if len(f) != m or (f["n"] != 3).any():
                return None                        # not all triangles: general reader
            faces = f["i"].astype(np.int32)
    return {"vertex": v, "face": faces}


def ply_counts(path):
    """(vertex_count, face_count) from the header only (no data read)."""
    with open(path, "rb") as fh:
        h = read_header(fh)
    if h is None:
        raise ValueError(f"{path}: not a PLY file")
    counts = {name: n for name, n, _ in h[2]}
    if "vertex" not in counts:
        raise ValueError(f"{path}: PLY has no vertex element")
    return counts["vertex"], counts.get("face", 0)


def vertex_normals(verts, faces):
    """Area-weighted smooth vertex normals, vectorised (pyqtgraph's
    MeshData.vertexNormals loops in Python: ~1 min for a 1.8M-face mesh,
    which froze the window on project open)."""
    v = np.asarray(verts, np.float32)
    f = np.asarray(faces, np.int64)
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    n = np.zeros((len(v), 3), np.float64)
    for k in range(3):
        idx = f[:, k]
        for c in range(3):
            n[:, c] += np.bincount(idx, weights=fn[:, c], minlength=len(v))
    norm = np.linalg.norm(n, axis=1, keepdims=True)
    return (n / np.where(norm > 0, norm, 1)).astype(np.float32)


def load_ply(path, max_points=2_000_000):
    """Return dict(points=Nx3 float32, colors=Nx4 float32 or None, faces=Mx3 int or None)."""
    fast = read_fast(path)
    if fast is not None:
        v, faces = fast["vertex"], fast["face"]
    else:
        from plyfile import PlyData
        with open(path, "rb") as fh:          # no memory map: never keep the file mapped/locked
            ply = PlyData.read(fh, mmap=False)
        v = ply["vertex"].data
        faces = None
        if "face" in [e.name for e in ply.elements]:
            f = ply["face"].data
            key = "vertex_indices" if "vertex_indices" in f.dtype.names else f.dtype.names[0]
            try:
                faces = np.vstack(f[key]).astype(np.int32)[:, :3]
            except Exception:
                faces = None
    total = len(v)
    if faces is None and max_points and total > max_points:
        # display decimation: uniform random subset, chosen before any copies
        keep = np.random.default_rng(0).random(total) < (max_points / total)
        v = v[keep]
    pts = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float32)
    names = v.dtype.names
    cols = None
    if all(n in names for n in ("red", "green", "blue")):
        cols = np.empty((len(v), 4), np.float32)
        cols[:, 0], cols[:, 1], cols[:, 2] = v["red"], v["green"], v["blue"]
        cols[:, :3] /= 255.0
        cols[:, 3] = 1.0
    return {"points": pts, "colors": cols, "faces": faces, "total": total}


def save_ply(path, points, colors=None, faces=None):
    from plyfile import PlyData, PlyElement
    n = len(points)
    dt = [("x", "f4"), ("y", "f4"), ("z", "f4")]
    if colors is not None:
        dt += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    arr = np.empty(n, dtype=dt)
    arr["x"], arr["y"], arr["z"] = points[:, 0], points[:, 1], points[:, 2]
    if colors is not None:
        c = (np.clip(colors[:, :3], 0, 1) * 255).astype(np.uint8)
        arr["red"], arr["green"], arr["blue"] = c[:, 0], c[:, 1], c[:, 2]
    els = [PlyElement.describe(arr, "vertex")]
    if faces is not None:
        fa = np.empty(len(faces), dtype=[("vertex_indices", "i4", (3,))])
        fa["vertex_indices"] = faces
        els.append(PlyElement.describe(fa, "face"))
    PlyData(els, text=False).write(path)
