"""Horizontal mesh sections exported as elevation-bearing DXF 3D polylines.

Levels are integer multiples of the requested interval in WORLD Z. Multiples
of five intervals use layer major; four intermediate levels use layer minor.
Both CAD layers are explicitly defined. No
rasterization, smoothing or re-triangulation is performed. Shared edges are
intersected in the same lower-to-higher Z direction, so common endpoints
match exactly without a spatial tolerance that could merge nearby contours.
At a vertex on the plane we use the half-open rule z_low < level <= z_high;
coplanar triangle interiors and zero-length sections are omitted.

A sweep of sorted triangle height bounds visits only active triangles at
each level. Geometry is sliced in batches and one level is stitched/written
at a time. Junctions in non-manifold sections split into separate polylines.
"""
from __future__ import annotations

import math
import os

import numpy as np

from .export import atomic_write, _DXF_TAIL, _rows

MAX_LEVELS = 10_000
MAX_LEVEL_SEGMENTS = 2_000_000
MAX_TOTAL_SEGMENTS = 20_000_000
_BATCH = 100_000
MINOR_LAYER = "minor"
MAJOR_LAYER = "major"
INDEX_EVERY = 5


class ContourCancelled(Exception):
    """The caller cancelled before the output file was committed."""


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise ContourCancelled("Contour export cancelled")


def _prepare(vertices, faces, interval, offset, cancelled):
    interval = float(interval)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Contour interval must be a finite number greater than zero.")
    V = np.asarray(vertices, dtype=np.float64)
    F = np.asarray(faces)
    if V.ndim != 2 or V.shape[1] != 3 or not len(V) or not np.isfinite(V).all():
        raise ValueError("The mesh must contain finite XYZ vertices.")
    if F.ndim != 2 or F.shape[1] != 3 or not len(F) or not np.issubdtype(F.dtype, np.integer):
        raise ValueError("Contour export requires triangular mesh faces.")
    F = F.astype(np.int64, copy=False)
    if F.min() < 0 or F.max() >= len(V):
        raise ValueError("The mesh contains an invalid face vertex index.")
    off = np.zeros(3) if offset is None else np.asarray(offset, dtype=np.float64)
    if off.shape != (3,) or not np.isfinite(off).all():
        raise ValueError("The coordinate offset must contain three finite values.")
    with np.errstate(over="ignore"):
        world_bounds = np.array([V.min(axis=0), V.max(axis=0)]) + off
    if not np.isfinite(world_bounds).all():
        raise ValueError("World coordinates exceed the supported numeric range.")
    z = V[:, 2] + off[2]
    low, high = np.empty(len(F)), np.empty(len(F))
    for start in range(0, len(F), _BATCH):
        _check_cancel(cancelled)
        stop = start + _BATCH
        tri_z = z[F[start:stop]]
        low[start:stop] = tri_z.min(axis=1)
        high[start:stop] = tri_z.max(axis=1)
    live = np.flatnonzero(low < high)
    if not len(live):
        raise ValueError("The mesh is flat: no contour polylines can be generated.")
    zmin, zmax = float(low[live].min()), float(high[live].max())
    if interval < 8 * np.spacing(max(abs(zmin), abs(zmax), 1.0)):
        raise ValueError("Contour interval is too small for the elevation coordinate precision.")
    first, last = math.floor(zmin / interval) + 1, math.floor(zmax / interval)
    count = last - first + 1
    if count <= 0:
        raise ValueError("No contour level crosses the mesh. Choose a smaller interval.")
    if count > MAX_LEVELS:
        raise ValueError(f"This interval creates {count:,} levels (limit {MAX_LEVELS:,}). "
                         "Increase the interval or export a smaller mesh.")
    _check_cancel(cancelled)
    starts = live[np.argsort(low[live], kind="stable")]
    ends = live[np.argsort(high[live], kind="stable")]
    return V, F, off, z, starts, low[starts], ends, high[ends], first, count, interval


def _slice_segments(V, F, z, ids, level, cancelled):
    """Yield Nx2x2 XY segment batches, with canonical edge interpolation."""
    for start in range(0, len(ids), _BATCH):
        _check_cancel(cancelled)
        tri = F[ids[start:start + _BATCH]]
        below = z[tri] < level
        crossed = below != below[:, [1, 2, 0]]
        # A valid, non-flat triangle section always crosses two edges.
        valid = crossed.sum(axis=1) == 2
        tri, crossed = tri[valid], crossed[valid]
        a = tri[crossed]
        b = tri[:, [1, 2, 0]][crossed]
        reverse = z[a] > z[b]
        lo, hi = np.where(reverse, b, a), np.where(reverse, a, b)
        t = (level - z[lo]) / (z[hi] - z[lo])
        xy = V[lo, :2] + t[:, None] * (V[hi, :2] - V[lo, :2])
        # Preserve an exact on-plane vertex when several edges meet there.
        xy[t == 1] = V[hi[t == 1], :2]
        xy[t == 0] = V[lo[t == 0], :2]
        yield xy.reshape(-1, 2, 2)


def _stitch(batches, cancelled):
    """Return (XY paths with closed flags, unique segment count, junctions)."""
    node_ids, points, edges = {}, [], set()

    def node(p):
        key = (float(p[0]), float(p[1]))
        idx = node_ids.get(key)
        if idx is None:
            idx = len(points)
            node_ids[key] = idx
            points.append(key)
        return idx

    for batch in batches:
        for i, segment in enumerate(batch):
            if i % 4096 == 0:
                _check_cancel(cancelled)
            a, b = node(segment[0]), node(segment[1])
            if a != b:
                edges.add((min(a, b), max(a, b)))
                if len(edges) > MAX_LEVEL_SEGMENTS:
                    raise ValueError("A contour level has too many segments. Simplify the mesh.")
    adjacent = [[] for _ in points]
    for i, (a, b) in enumerate(sorted(edges)):
        if i % 4096 == 0:
            _check_cancel(cancelled)
        adjacent[a].append(b)
        adjacent[b].append(a)
    left = set(edges)
    paths = []

    def walk(a, b):
        path, prev, cur = [a], a, b
        left.remove((min(a, b), max(a, b)))
        while True:
            if len(path) % 4096 == 0:
                _check_cancel(cancelled)
            path.append(cur)
            if cur == a or len(adjacent[cur]) != 2:
                break
            nxt = adjacent[cur][0] if adjacent[cur][0] != prev else adjacent[cur][1]
            edge = (min(cur, nxt), max(cur, nxt))
            if edge not in left:
                break
            left.remove(edge)
            prev, cur = cur, nxt
        closed = path[-1] == path[0]
        if closed:
            path.pop()
        paths.append((np.asarray([points[p] for p in path]), closed))

    # Stop at endpoints/junctions rather than guessing how to cross a saddle.
    for a, neighbours in enumerate(adjacent):
        if len(neighbours) != 2:
            for b in neighbours:
                if (min(a, b), max(a, b)) in left:
                    walk(a, b)
    # Remaining edges belong to closed loops.
    for a, b in sorted(edges):
        if (a, b) in left:
            walk(a, b)
    return paths, len(edges), sum(len(n) > 2 for n in adjacent)


def _contour_dxf_header():
    """Declare real CAD layers, including an empty major layer for short ranges."""
    text = ("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n0\nENDSEC\n"
            "0\nSECTION\n2\nTABLES\n"
            "0\nTABLE\n2\nLTYPE\n70\n1\n"
            "0\nLTYPE\n2\nCONTINUOUS\n70\n0\n3\nSolid line\n72\n65\n73\n0\n40\n0\n"
            "0\nENDTAB\n0\nTABLE\n2\nLAYER\n70\n3\n")
    for name, colour in (("0", 7), (MINOR_LAYER, 8), (MAJOR_LAYER, 7)):
        text += f"0\nLAYER\n2\n{name}\n70\n0\n62\n{colour}\n6\nCONTINUOUS\n"
    return text + "0\nENDTAB\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"


def write_mesh_contours(dst, vertices, faces, interval, offset=None, report=None, cancelled=None):
    """Write a DXF atomically; return counts. Cancellation preserves any old dst."""
    _check_cancel(cancelled)
    if report:
        report(0, 1, "Preparing mesh contours...")
    V, F, off, z, starts, lows, ends, highs, first, count, step = _prepare(
        vertices, faces, interval, offset, cancelled)
    stats = dict(polylines=0, levels=0, vertices=0, segments=0, junctions=0, interval=step,
                 minor=0, major=0)
    active, si, ei = set(), 0, 0
    with atomic_write(dst, "w", encoding="ascii", newline="\n") as out:
        out.write(_contour_dxf_header())
        out.write(f"999\nPhotoMesh mesh contours; interval={step:.17g}; world Z; major every {INDEX_EVERY} intervals; no smoothing\n")
        for i in range(count):
            _check_cancel(cancelled)
            index = first + i
            level = index * step
            # Index from world zero, not from the first contour that happens to
            # cross this mesh. Integer modulo is stable at fractional intervals.
            layer = MAJOR_LAYER if index % INDEX_EVERY == 0 else MINOR_LAYER
            vfmt = "0\nVERTEX\n8\n" + layer + "\n10\n%.17g\n20\n%.17g\n30\n%.17g\n70\n32\n"
            ns = int(np.searchsorted(lows, level, side="left"))  # low < level
            ne = int(np.searchsorted(highs, level, side="left")) # high < level expires
            active.update(starts[si:ns].tolist())
            active.difference_update(ends[ei:ne].tolist())
            si, ei = ns, ne
            if len(active) > MAX_LEVEL_SEGMENTS:
                raise ValueError("A contour level crosses too many triangles. Simplify the mesh.")
            ids = np.sort(np.fromiter(active, dtype=np.int64, count=len(active)))
            paths, segments, junctions = _stitch(
                _slice_segments(V, F, z, ids, level, cancelled), cancelled)
            stats["segments"] += segments
            stats["junctions"] += junctions
            if stats["segments"] > MAX_TOTAL_SEGMENTS:
                raise ValueError("Too many contour segments. Increase the interval or simplify the mesh.")
            if paths:
                stats["levels"] += 1
            for xy, closed in paths:
                _check_cancel(cancelled)
                out.write(f"0\nPOLYLINE\n8\n{layer}\n66\n1\n10\n0\n20\n0\n30\n0\n70\n{9 if closed else 8}\n")
                for start in range(0, len(xy), 10_000):
                    _check_cancel(cancelled)
                    block = xy[start:start + 10_000]
                    xyz = np.empty((len(block), 3))
                    xyz[:, :2] = block + off[:2]
                    xyz[:, 2] = level
                    _rows(out, vfmt, xyz)
                out.write(f"0\nSEQEND\n8\n{layer}\n")
                stats["polylines"] += 1
                stats[layer] += 1
                stats["vertices"] += len(xy)
            if report:
                report(i + 1, count, f"Elevation {level:.6g}; {stats['polylines']:,} polylines")
        if not stats["polylines"]:
            raise ValueError("No contour polylines were generated. Check the mesh and interval.")
        out.write(_DXF_TAIL)
        _check_cancel(cancelled)
    return stats


def export_mesh_contours(src, dst, interval, offset=None, report=None, cancelled=None):
    """Read the existing mesh, then section its triangles (never a raster DEM)."""
    from .texture import read_mesh
    _check_cancel(cancelled)
    if report:
        report(0, 1, "Reading mesh...")
    V, F = read_mesh(os.fspath(src))
    return write_mesh_contours(dst, V, F, interval, offset, report, cancelled)
