"""Ortho tab: 2D top view of the orthomosaic / DEM.

Large rasters are shown from their tiled GeoTIFF pyramid (overview levels):
only the tiles visible at the current zoom are decoded, in background
threads, and cached (LRU); while a tile loads, the matching part of a coarser
level is drawn instead, so panning / zooming never blocks the GUI.
The cursor position is shown in REAL world coordinates (offset included) with
Z from the DEM (or the orthomosaic's height grid) in a corner overlay and in
the status bar (coordinatesChanged signal); right-click > Copy Coordinates.
Without a raster the view shows the cameras and points (planar projection)."""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QFont, QImage, QPen, QBrush, QGuiApplication
from PySide6.QtWidgets import QWidget, QMenu

from . import geotiff as gt

_DEM_STOPS = np.array([[0.0, 40, 90, 180], [0.25, 60, 170, 200], [0.5, 90, 190, 90], [0.75, 230, 210, 80],
                       [1.0, 200, 80, 50]], np.float32)


def dem_colors(z, valid, zmin, zmax, px=1.0):
    """Elevation colours with a light hillshade (RGBA uint8)."""
    t = np.clip((z - zmin) / max(zmax - zmin, 1e-9), 0, 1)
    rgb = np.stack([np.interp(t, _DEM_STOPS[:, 0], _DEM_STOPS[:, k]) for k in (1, 2, 3)], -1)
    zz = np.where(valid, z, np.nanmean(z[valid]) if valid.any() else 0)
    gy, gx = np.gradient(zz.astype(np.float32), px)
    shade = np.clip(0.75 + 0.25 * (-gx - gy) / np.sqrt(1 + gx * gx + gy * gy), 0.45, 1.1)
    rgb = np.clip(rgb * shade[..., None], 0, 255)
    out = np.zeros(z.shape + (4,), np.uint8)
    out[..., :3] = rgb.astype(np.uint8); out[..., 3] = np.where(valid, 255, 0)
    return out


def format_coords(X, Y, Z=None, geographic=False, unit="m"):
    if geographic:
        s = f"Lon: {X:.8f}\u00b0  Lat: {Y:.8f}\u00b0"
    else:
        u = f" {unit}" if unit else ""
        s = f"X: {X:,.3f}{u}  Y: {Y:,.3f}{u}"
    if Z is not None and np.isfinite(Z):
        s += f"  Z: {Z:,.3f} m"
    return s


class RasterLayer:
    def __init__(self, kind, info):
        self.kind, self.info = kind, info
        self.reader = gt.TiffReader(info["path"])
        g = self.reader.geo
        self.x0, self.y1, self.px, self.py = g if g else (info["origin"][0], info["origin"][1],
                                                           info["pixel_size"], info["pixel_size"])
        self.W, self.H = self.reader.width, self.reader.height
        self.nlev = len(self.reader.levels)
        self.visible = True
        zr = info.get("z_range") or [0, 1]
        self.zmin, self.zmax = (zr[0] if zr[0] is not None else 0), (zr[1] if zr[1] is not None else 1)
        self.nodata = self.reader.nodata

    def level_px(self, lev):
        from .ortho import level_pixel_size
        return level_pixel_size({"format": self.info.get("format", ""), "pixel_size": self.px, "width": self.W},
                                self.reader.levels[lev].width, lev)

    def tile_image(self, lev, ti, tj):
        a = self.reader.read_tile(lev, ti, tj)
        if self.kind == "dem" or a.dtype != np.uint8:
            z = a[..., 0].astype(np.float32)
            valid = np.isfinite(z) & ((z != self.nodata) if self.nodata is not None else True)
            rgba = dem_colors(z, valid, self.zmin, self.zmax, self.level_px(lev))
        elif a.shape[2] == 4:
            rgba = np.ascontiguousarray(a)
        elif a.shape[2] == 3:
            rgba = np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], 2)
            if self.nodata is not None:
                rgba[(a == int(self.nodata)).all(2), 3] = 0
        else:
            g = a[..., 0]
            rgba = np.stack([g, g, g, np.full_like(g, 255)], -1)
        rgba = np.ascontiguousarray(rgba)
        img = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4, QImage.Format_RGBA8888)
        return img.copy()

    def close(self):
        try:
            self.reader.close()
        except Exception:  # noqa: BLE001
            pass


class OrthoView(QWidget):
    tileReady = Signal(int, object, object)        # generation, key, QImage
    heightReady = Signal(int, object)
    coordinatesChanged = Signal(str)
    shapeClicked = Signal(int)                     # index into the chunk's shape set, -1 = nothing hit
    addMarkerRequested = Signal(float, float, object)   # world X, Y, Z (None = not loaded / no height)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pts = None
        self.cams = []
        self.markers = []                 # (label, X, Y) world
        self.layers = {}                  # kind -> RasterLayer
        self.vis = {"ortho": True, "dem": True, "markers": True, "marker_labels": True, "shapes": True}
        self.shapes = []                  # dicts: index, type, color, width, label, parts [(n,2) world XY], holes
        self.selected_shape = None
        self.transparent = False          # transparent view: rasters drawn with surface_opacity
        self.surface_opacity = 0.4
        self.cache = OrderedDict()
        self.max_tiles = 600
        self.pending = set()
        self.pool = ThreadPoolExecutor(3, thread_name_prefix="ortho-tiles")
        self.gen = 0
        self.center = None                # world (X, Y)
        self.scale = 1.0                  # screen px per world unit
        self.zgrid = None                 # (array, x0, y1, px, nodata)
        self.geographic = False
        self.unit = "m"
        self.offset = np.zeros(3)
        self.coord_text = ""
        self.last_world = None
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.StrongFocus)
        self.tileReady.connect(self._tile_ready)
        self.heightReady.connect(self._height_ready)
        self._status_timer = QTimer(self); self._status_timer.setSingleShot(True); self._status_timer.setInterval(40)
        self._status_timer.timeout.connect(lambda: self.coordinatesChanged.emit(self.coord_text))

    # ------------------------------------------------------------------ data
    def set_data(self, pts=None, cams=None):
        if pts is not None:
            self.pts = np.asarray(pts)[::max(1, len(pts) // 50000)]
        if cams is not None:
            self.cams = cams
        self.update()

    def set_frame(self, offset=None, geographic=False, unit="m"):
        """Offset added to local coordinates (cameras/points) to get world coordinates."""
        self.offset = np.zeros(3) if offset is None else np.asarray(offset, float)
        self.geographic, self.unit = bool(geographic), unit

    def set_markers(self, markers):
        self.markers = list(markers or []); self.update()

    def set_shapes(self, shapes, selected=None):
        """Imported shapes in world XY (same frame as the rasters / markers)."""
        self.shapes = [dict(d, parts=[np.asarray(P, float)[:, :2] for P in d["parts"]]) for d in (shapes or [])]
        self.selected_shape = selected
        self.update()

    def select_shape(self, index):
        self.selected_shape = index; self.update()

    def shape_at(self, sx, sy, tol=6.0):
        """Index of the shape under a widget position (points/lines within tol px,
        polygons also by containment), or None."""
        if not self.shapes or self.center is None or not self.vis.get("shapes", True):
            return None
        X, Y = self.to_world(sx, sy)
        t = tol / max(self.scale, 1e-12)
        best, bd = None, None
        for d in self.shapes:
            for k, P in enumerate(d["parts"]):
                if not len(P):
                    continue
                if d["type"] == "point" or len(P) == 1:
                    dist = float(np.min(np.hypot(P[:, 0] - X, P[:, 1] - Y)))
                else:
                    A, B = P[:-1], P[1:]
                    AB = B - A
                    L2 = np.maximum((AB ** 2).sum(1), 1e-18)
                    u = np.clip(((X - A[:, 0]) * AB[:, 0] + (Y - A[:, 1]) * AB[:, 1]) / L2, 0, 1)
                    dist = float(np.min(np.hypot(A[:, 0] + u * AB[:, 0] - X, A[:, 1] + u * AB[:, 1] - Y)))
                    if d["type"] == "polygon" and dist > t and not (d.get("holes") or [False] * 99)[k]:
                        from .shapes import inside_polygon
                        if inside_polygon(np.array(X), np.array(Y), [P]):
                            dist = t * 0.99          # inside: weaker than an edge hit
                if dist <= t and (bd is None or dist < bd):
                    best, bd = d["index"], dist
        return best

    def set_raster(self, kind, info):
        """kind 'ortho' | 'dem'; info dict with 'path' (None removes the layer)."""
        old = self.layers.pop(kind, None)
        self.gen += 1
        self.cache.clear(); self.pending.clear()
        if old is not None:
            old.close()
        if info and info.get("path"):
            lay = RasterLayer(kind, info)
            lay.visible = self.vis.get(kind, True)
            self.layers[kind] = lay
            if self.center is None or len(self.layers) == 1:
                self.fit()
        self._load_height()
        self.update()

    def clear_rasters(self):
        for k in list(self.layers):
            self.set_raster(k, None)
        self.zgrid = None

    def has_raster(self, kind=None):
        return bool(self.layers) if kind is None else kind in self.layers

    def set_visible(self, kind, on):
        self.vis[kind] = bool(on)
        if kind in self.layers:
            self.layers[kind].visible = bool(on)
        self.update()

    def _load_height(self):
        """Z read-out source: the DEM if present, else the orthomosaic's height grid."""
        src = None
        if "dem" in self.layers:
            src = self.layers["dem"].info.get("path")
        elif "ortho" in self.layers:
            src = self.layers["ortho"].info.get("height_path")
        self.zgrid = None
        if not src:
            return
        gen = self.gen

        def work():
            try:
                with gt.TiffReader(src) as r:
                    lev = 0
                    while lev + 1 < len(r.levels) and r.levels[lev].width * r.levels[lev].height > 16_000_000:
                        lev += 1
                    a = r.read_level(lev)[..., 0].astype(np.float32)
                    x0, y1, px, _ = r.geo
                    f = r.width / r.levels[lev].width
                    res = (a, x0, y1, px * f, r.nodata)
            except Exception as e:  # noqa: BLE001
                res = e
            try:
                self.heightReady.emit(gen, res)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="ortho-height", daemon=True).start()

    def _height_ready(self, gen, res):
        if gen == self.gen and not isinstance(res, Exception):
            self.zgrid = res
            self._update_coords()

    def z_at(self, X, Y):
        h = self.zgrid
        if h is None:
            return None
        a, x0, y1, px, nd = h
        c = int(math.floor((X - x0) / px)); r = int(math.floor((y1 - Y) / px))
        if 0 <= r < a.shape[0] and 0 <= c < a.shape[1]:
            z = float(a[r, c])
            if np.isfinite(z) and (nd is None or z != nd):
                return z
        return None

    # ------------------------------------------------------------------ transform
    def _bounds(self):
        bs = []
        for lay in self.layers.values():
            bs.append((lay.x0, lay.y1 - lay.H * lay.py, lay.x0 + lay.W * lay.px, lay.y1))
        if not bs:
            xy = []
            if self.pts is not None and len(self.pts):
                xy.append(self.pts[:, :2] + self.offset[:2])
            if self.cams:
                xy.append(np.array([c[:2] for c in self.cams], float) + self.offset[:2])
            for d in self.shapes:
                xy += [P[:, :2] for P in d["parts"] if len(P)]
            if not xy:
                return None
            xy = np.vstack(xy)
            lo, hi = xy.min(0), xy.max(0)
            return lo[0], lo[1], hi[0], hi[1]
        b = np.array(bs)
        return b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()

    def fit(self):
        b = self._bounds()
        if b is None:
            return
        self.center = (float(b[0] + b[2]) / 2, float(b[1] + b[3]) / 2)
        w, h = max(b[2] - b[0], 1e-6), max(b[3] - b[1], 1e-6)
        self.scale = 0.92 * min(max(self.width(), 50) / w, max(self.height_px(), 50) / h)
        self.update()

    reset_view = fit

    def height_px(self):
        return QWidget.height(self)

    def to_screen(self, X, Y):
        cx, cy = self.center
        return (self.width() / 2 + (X - cx) * self.scale, self.height_px() / 2 - (Y - cy) * self.scale)

    def to_world(self, sx, sy):
        cx, cy = self.center if self.center else (0.0, 0.0)
        return (cx + (sx - self.width() / 2) / self.scale, cy - (sy - self.height_px() / 2) / self.scale)

    def zoom(self, f, anchor=None):
        if self.center is None:
            return
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height_px() / 2)
        X, Y = self.to_world(anchor.x(), anchor.y())
        self.scale = float(np.clip(self.scale / f, 1e-6, 1e6))
        # keep the world point under the anchor fixed
        cx = X - (anchor.x() - self.width() / 2) / self.scale
        cy = Y + (anchor.y() - self.height_px() / 2) / self.scale
        self.center = (cx, cy)
        self.update()

    # ------------------------------------------------------------------ tiles
    def _request(self, kind, lay, lev, ti, tj):
        key = (kind, lev, ti, tj)
        if key in self.pending:
            return
        self.pending.add(key)
        gen = self.gen

        def work():
            try:
                img = lay.tile_image(lev, ti, tj)
            except Exception as e:  # noqa: BLE001
                img = e
            try:
                self.tileReady.emit(gen, key, img)
            except RuntimeError:
                pass
        self.pool.submit(work)

    def _tile_ready(self, gen, key, img):
        self.pending.discard(key)
        if gen != self.gen or isinstance(img, Exception):
            return
        self.cache[key] = img
        while len(self.cache) > self.max_tiles:
            self.cache.popitem(last=False)
        self.update()

    def pending_count(self):
        return len(self.pending)

    def _draw_layer(self, p, kind, lay):
        need = 1.0 / max(self.scale * lay.px, 1e-12)          # level-0 pixels per screen pixel
        lev = int(np.clip(math.floor(math.log2(max(need, 1e-9))), 0, lay.nlev - 1)) if need > 1 else 0
        wx0, wy1 = self.to_world(0, 0)
        wx1, wy0 = self.to_world(self.width(), self.height_px())
        # coarsest level: always requested (background while finer tiles load)
        top = lay.nlev - 1
        Lt = lay.reader.levels[top]
        for ti in range(Lt.ny):
            for tj in range(Lt.nx):
                if (kind, top, ti, tj) not in self.cache:
                    self._request(kind, lay, top, ti, tj)
        L = lay.reader.levels[lev]
        lpx = lay.level_px(lev)
        tws, ths = L.tw * lpx, L.th * lpx
        c0 = max(0, int((wx0 - lay.x0) // tws)); c1 = min(L.nx - 1, int((wx1 - lay.x0) // tws))
        r0 = max(0, int((lay.y1 - wy1) // ths)); r1 = min(L.ny - 1, int((lay.y1 - wy0) // ths))
        p.setRenderHint(QPainter.SmoothPixmapTransform, bool(self.scale * lpx < 2.5))
        for ti in range(r0, r1 + 1):
            for tj in range(c0, c1 + 1):
                X0 = lay.x0 + tj * tws; Y1 = lay.y1 - ti * ths
                sx0, sy0 = self.to_screen(X0, Y1)
                sx1, sy1 = self.to_screen(X0 + tws, Y1 - ths)
                target = QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0)
                img = self.cache.get((kind, lev, ti, tj))
                if img is not None:
                    self.cache.move_to_end((kind, lev, ti, tj))
                    p.drawImage(target, img)
                    continue
                self._request(kind, lay, lev, ti, tj)
                # fallback: part of a coarser cached tile
                for l2 in range(lev + 1, lay.nlev):
                    f = 2 ** (l2 - lev)
                    L2 = lay.reader.levels[l2]
                    pi, pj = (ti * L.th) // (f * L2.th), (tj * L.tw) // (f * L2.tw)
                    im2 = self.cache.get((kind, l2, pi, pj))
                    if im2 is None:
                        continue
                    sx = (tj * L.tw / f) - pj * L2.tw; sy = (ti * L.th / f) - pi * L2.th
                    p.drawImage(target, im2, QRectF(sx, sy, L.tw / f, L.th / f))
                    break

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _):
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            if p.isActive():
                p.end()

    def _paint(self, p):
        p.fillRect(self.rect(), QColor(26, 26, 32))
        if self.center is None:
            self.fit()
        if self.center is None:
            p.setPen(QColor(150, 150, 160)); p.setFont(QFont("Sans", 9))
            p.drawText(10, 20, "Ortho view - build an orthomosaic or DEM (Workflow menu)")
            return
        drew = False
        for kind in ("dem", "ortho"):
            lay = self.layers.get(kind)
            if lay is not None and lay.visible:
                self._draw_layer(p, kind, lay); drew = True
        if drew and self.transparent:
            # dim the composited rasters towards the background (the DEM under the
            # orthomosaic must not show through, as it would with per-layer opacity)
            p.fillRect(self.rect(), QColor(26, 26, 32, int(round(255 * (1.0 - self.surface_opacity)))))
        if not drew:
            p.setPen(QColor(150, 150, 160)); p.setFont(QFont("Sans", 9))
            msg = ("Ortho view (planar projection) - Workflow > Build Orthomosaic / Build DEM"
                   if not self.layers else "Orthomosaic / DEM hidden (toolbar layer toggles)")
            p.drawText(10, 20, msg)
            if self.pts is not None and len(self.pts):
                p.setPen(QColor(170, 170, 180))
                step = max(1, len(self.pts) // 20000)
                for q in self.pts[::step]:
                    x, y = self.to_screen(q[0] + self.offset[0], q[1] + self.offset[1])
                    p.drawPoint(int(x), int(y))
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(80, 160, 255)); p.setBrush(QColor(80, 160, 255))
        if not self.layers:
            for c in self.cams:
                x, y = self.to_screen(c[0] + self.offset[0], c[1] + self.offset[1])
                p.drawEllipse(QPointF(x, y), 3, 3)
        if self.vis.get("shapes", True) and self.shapes:
            self._draw_shapes(p)
        if self.vis.get("markers", True) and self.markers:
            p.setFont(QFont("Sans", 8))
            for lab, X, Y in self.markers:
                x, y = self.to_screen(X, Y)
                if -20 < x < self.width() + 20 and -20 < y < self.height_px() + 20:
                    p.setPen(QPen(QColor(255, 220, 40), 1.5)); p.setBrush(Qt.NoBrush)
                    p.drawLine(QPointF(x - 6, y), QPointF(x + 6, y)); p.drawLine(QPointF(x, y - 6), QPointF(x, y + 6))
                    p.drawEllipse(QPointF(x, y), 4, 4)
                    if self.vis.get("marker_labels", True):
                        p.setPen(QColor(255, 235, 120)); p.drawText(QPointF(x + 6, y - 6), str(lab))
        self._draw_overlay(p)

    def set_transparency(self, on, opacity=None):
        if opacity is not None:
            self.surface_opacity = float(min(1.0, max(0.05, opacity)))
        self.transparent = bool(on); self.update()

    def _draw_shapes(self, p):
        """Shapes with their style (shape_style): line colour / width / dash,
        polygon fill colour and opacity, point symbol and size, labels."""
        from PySide6.QtGui import QPainterPath, QPolygonF
        from .overlay3d import draw_symbol
        W, H = self.width(), self.height_px()
        labels = []
        for d in self.shapes:
            sel = d["index"] == self.selected_shape
            c = QColor(*[int(v) for v in d.get("color", (255, 140, 0))[:3]])
            w = float(d.get("width", 2.0)) + (2.0 if sel else 0.0)
            pen = QPen(QColor(255, 255, 255) if sel else c, w)
            halo = QPen(QColor(0, 0, 0, 150), w + 2.0)
            if d.get("dashed"):
                pen.setStyle(Qt.DashLine); halo.setStyle(Qt.DashLine)
            lab = d.get("label")
            if d["type"] == "point":
                r = float(d.get("point_size", 10.0)) / 2.0
                for P in d["parts"]:
                    for X, Y in P:
                        x, y = self.to_screen(X, Y)
                        if -20 < x < W + 20 and -20 < y < H + 20:
                            draw_symbol(p, x, y, d.get("symbol", "circle"), r, QColor(255, 255, 255) if sel else c, w)
                            if lab:
                                labels.append((x + r + 2, y + r + 10, str(lab), d, c))
                continue
            path = QPainterPath()
            for P in d["parts"]:
                if len(P) < 2:
                    continue
                sx = W / 2 + (P[:, 0] - self.center[0]) * self.scale
                sy = H / 2 - (P[:, 1] - self.center[1]) * self.scale
                poly = QPolygonF([QPointF(a, b) for a, b in zip(sx, sy)])
                if d["type"] == "polygon":
                    path.addPolygon(poly); path.closeSubpath()
                else:
                    path.moveTo(poly[0]); [path.lineTo(q) for q in list(poly)[1:]]
            if path.isEmpty():
                continue
            p.setBrush(Qt.NoBrush)
            p.setPen(halo); p.drawPath(path)
            if d["type"] == "polygon":
                path.setFillRule(Qt.OddEvenFill)
                fc = d.get("fill_color") or d.get("color", (255, 140, 0))
                fo = float(d.get("fill_opacity", 0.16))
                p.setBrush(QColor(int(fc[0]), int(fc[1]), int(fc[2]), int(round(255 * min(1.0, fo + (0.12 if sel else 0))))))
            p.setPen(pen); p.drawPath(path)
            p.setBrush(Qt.NoBrush)
            if lab and d.get("anchor") is not None:
                x, y = self.to_screen(*d["anchor"][:2])
                labels.append((x + 4, y - 4, str(lab), d, c))
        for x, y, text, d, c in labels:                       # labels last: over every outline
            ls = d.get("label_style") or {}
            f = QFont("Sans"); f.setPointSizeF(float(ls.get("size") or 8.0)); f.setBold(bool(ls)); p.setFont(f)
            lc = ls.get("color")
            tc = QColor(int(lc[0]), int(lc[1]), int(lc[2])) if lc else c
            fm = p.fontMetrics()
            if ls.get("bg"):
                b = ls.get("bg_color") or (20, 20, 24)
                p.setPen(Qt.NoPen); p.setBrush(QColor(int(b[0]), int(b[1]), int(b[2]), 190))
                p.drawRoundedRect(QRectF(x - 3, y - fm.ascent() - 2, fm.horizontalAdvance(text) + 6, fm.height() + 3), 3, 3)
                p.setBrush(Qt.NoBrush)
            p.setPen(tc); p.drawText(QPointF(x, y), text)

    def _draw_overlay(self, p):
        # scale bar + coordinates (bottom-left / bottom-right)
        p.setFont(QFont("Sans", 9))
        if self.coord_text:
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(self.coord_text) + 14
            r = QRectF(self.width() - w - 8, self.height_px() - 28, w, 22)
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 160)); p.drawRoundedRect(r, 4, 4)
            p.setPen(QColor(235, 235, 240)); p.drawText(r, Qt.AlignCenter, self.coord_text)
        if self.scale > 0:
            target = 120 / self.scale
            e = 10 ** math.floor(math.log10(target))
            L = max(k * e for k in (1, 2, 5) if k * e <= target) if target >= e else e
            px = L * self.scale
            x0, y0 = 14, self.height_px() - 16
            txt = (f"{L:g} {self.unit}" if L >= 1 or self.unit != "m" else f"{L * 100:g} cm")
            tw = p.fontMetrics().horizontalAdvance(txt)
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 140))         # readable on bright imagery
            p.drawRoundedRect(QRectF(x0 - 6, y0 - 22, max(px, tw + 8) + 12, 30), 4, 4)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(240, 240, 240), 2)); p.drawLine(QPointF(x0, y0), QPointF(x0 + px, y0))
            p.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 2)); p.drawLine(QPointF(x0 + px, y0 - 4), QPointF(x0 + px, y0 + 2))
            p.drawText(QPointF(x0 + 4, y0 - 6), txt)

    # ------------------------------------------------------------------ mouse
    def _update_coords(self, pos=None):
        if pos is not None:
            self.last_world = self.to_world(pos.x(), pos.y())
        if self.last_world is None or self.center is None:
            return
        X, Y = self.last_world
        Z = self.z_at(X, Y)
        self.coord_text = format_coords(X, Y, Z, self.geographic, self.unit)
        self.update()
        if not self._status_timer.isActive():
            self._status_timer.start()

    def coordinates_at(self, sx, sy):
        """(X, Y, Z|None) world coordinates under a widget position."""
        X, Y = self.to_world(sx, sy)
        return X, Y, self.z_at(X, Y)

    def mousePressEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag = (e.position(), self.center)
        self._press = e.position() if e.button() == Qt.LeftButton else None

    def mouseMoveEvent(self, e):
        if self._drag is not None and self.center is not None:
            p0, c0 = self._drag
            d = e.position() - p0
            self.center = (c0[0] - d.x() / self.scale, c0[1] + d.y() / self.scale)
            self.update()
        self._update_coords(e.position())

    def mouseReleaseEvent(self, e):
        self._drag = None
        p0 = getattr(self, "_press", None)
        self._press = None
        if p0 is not None and e.button() == Qt.LeftButton and self.shapes:
            d = e.position() - p0
            if abs(d.x()) + abs(d.y()) <= 4:                       # a click, not a pan
                hit = self.shape_at(e.position().x(), e.position().y())
                self.selected_shape = hit; self.update()
                self.shapeClicked.emit(-1 if hit is None else int(hit))

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d:
            self.zoom(0.8 if d > 0 else 1.25, e.position())
            self._update_coords(e.position())

    def leaveEvent(self, e):
        self.coord_text = ""; self.update()
        self.coordinatesChanged.emit("")

    def contextMenuEvent(self, e):
        m = QMenu(self)
        X, Y, Z = self.coordinates_at(e.pos().x(), e.pos().y())
        if self.layers or self.pts is not None:
            m.addAction("Add Marker").triggered.connect(lambda: self.addMarkerRequested.emit(X, Y, Z))
            m.addSeparator()
        a = m.addAction("Copy Coordinates")
        a.triggered.connect(lambda: self.copy_coordinates(X, Y, Z))
        m.addAction("Reset View").triggered.connect(self.fit)
        m.exec(e.globalPos())

    def copy_coordinates(self, X, Y, Z=None):
        txt = f"{X:.3f}\t{Y:.3f}" + (f"\t{Z:.3f}" if Z is not None else "")
        if self.geographic:
            txt = f"{X:.8f}\t{Y:.8f}" + (f"\t{Z:.3f}" if Z is not None else "")
        QGuiApplication.clipboard().setText(txt)
        return txt

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()

    def closeEvent(self, e):
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)
