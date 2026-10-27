"""Label / marker overlay for the 3D Model view.

Drawn with QPainter *after* every GL item of the frame (see ModelView's GL
widget paintGL), so the texture, the mesh and the tiled model can never hide a
label: text sits on a dark rounded box, marker dots are filled when visible
and hollow (with the label still fully readable) when the surface hides them.
Positions are projected on the CPU with the view's MVP, points behind the
near plane are skipped, and boxes are clamped inside the viewport. All sizes
are in logical (device-independent) pixels, so they look the same at any DPI.

The old labels were pyqtgraph GLTextItems: QPainter text painted *during*
the GL item pass at depth value 0, so any item drawn after them (the textured
model and tiled model are added later, when they finish loading) painted
over the text - the user saw marker dots but no labels."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QBrush

CONTROL = QColor(255, 214, 40)        # enabled marker: used as control point
CHECK = QColor(60, 220, 255)          # disabled marker: check point
CAMERA = QColor(150, 190, 255)
LEGEND = [(CONTROL, "Control point (enabled marker)"), (CHECK, "Check point (disabled marker)")]


def project(mvp, pts, width, height):
    """mvp: 4x4 row-major; pts (n,3) -> (xy (n,2) logical px, depth01 (n,), ok (n,) bool).
    ok is False behind the near plane / outside the depth range."""
    P = np.asarray(pts, float).reshape(-1, 3)
    if not len(P):
        return np.zeros((0, 2)), np.zeros(0), np.zeros(0, bool)
    H = np.c_[P, np.ones(len(P))] @ np.asarray(mvp, float).T
    w = H[:, 3]
    ok = w > 1e-9
    ndc = H[:, :3] / np.where(ok, w, 1.0)[:, None]
    ok &= (ndc[:, 2] >= -1.0) & (ndc[:, 2] <= 1.0)
    xy = np.c_[(ndc[:, 0] + 1) * 0.5 * width, (1 - ndc[:, 1]) * 0.5 * height]
    return xy, (ndc[:, 2] + 1) * 0.5, ok


def pixel_ray(mvp, x, y, width, height):
    """Ray (origin, unit direction) through logical pixel (x, y), in the frame
    the 4x4 row-major mvp maps from (inverse of the near/far unprojection)."""
    inv = np.linalg.inv(np.asarray(mvp, float))
    nx, ny = 2.0 * x / width - 1.0, 1.0 - 2.0 * y / height
    a = inv @ np.array([nx, ny, -1.0, 1.0]); b = inv @ np.array([nx, ny, 1.0, 1.0])
    a, b = a[:3] / a[3], b[:3] / b[3]
    d = b - a
    return a, d / np.linalg.norm(d)


def linear_depth(d01, near, far):
    """Eye distance of window depths d01 for a perspective [near, far] frustum
    (identity when near/far are unknown)."""
    d = np.asarray(d01, float)
    if not near or not far:
        return d
    zn = d * 2.0 - 1.0
    return 2.0 * near * far / np.maximum(far + near - zn * (far - near), 1e-30)


def label_rect(anchor, tw, th, width, height, pad=3.0, off=(9.0, -9.0)):
    """Box of a label placed up-right of its anchor, clamped inside the viewport."""
    w, h = tw + 2 * pad, th + 2 * pad
    x = anchor[0] + off[0]
    y = anchor[1] + off[1] - h
    if x + w > width - 1:                      # flip to the left side near the right edge
        x = anchor[0] - off[0] - w
    if y < 1:
        y = anchor[1] - off[1]
    x = min(max(x, 1.0), max(1.0, width - w - 1))
    y = min(max(y, 1.0), max(1.0, height - h - 1))
    return QRectF(x, y, w, h)


def label_font(base=None):
    f = QFont(base) if base is not None else QFont()
    f.setPointSizeF(9.0)                       # points: scaled by Qt per screen DPI, consistent size
    f.setBold(True)
    return f


def draw_symbol(painter, x, y, symbol, r, col, width=2.0, hidden=False):
    """Point symbol of a shape (screen pixels), dark halo underneath;
    hidden (behind the surface): dashed outline, no fill."""
    from PySide6.QtGui import QPolygonF
    col = QColor(col)
    pts = {"square": [(-r, -r), (r, -r), (r, r), (-r, r)],
           "diamond": [(0, -r * 1.3), (r * 1.3, 0), (0, r * 1.3), (-r * 1.3, 0)],
           "triangle": [(0, -r * 1.2), (r * 1.1, r * 0.8), (-r * 1.1, r * 0.8)]}.get(symbol)
    for halo in (True, False):
        pen = QPen(QColor(0, 0, 0, 170) if halo else col, width + (2.0 if halo else 0.0))
        if hidden and not halo:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        fill = symbol == "circle" or (pts is not None)
        if halo or hidden or not fill:
            painter.setBrush(Qt.NoBrush)
        else:
            fc = QColor(col); fc.setAlpha(150 if symbol != "circle" else 255); painter.setBrush(QBrush(fc))
        if symbol == "cross":
            painter.drawLine(QPointF(x - r, y - r), QPointF(x + r, y + r))
            painter.drawLine(QPointF(x - r, y + r), QPointF(x + r, y - r))
        elif pts is not None:
            painter.drawPolygon(QPolygonF([QPointF(x + a, y + b) for a, b in pts]))
        else:
            painter.drawEllipse(QPointF(x, y), r, r)
            if symbol == "ring" and not halo:             # short ticks, centre left clear
                for a, b in ((-r - 4, -r + 2), (r - 2, r + 4)):
                    painter.drawLine(QPointF(x + a, y), QPointF(x + b, y))
                    painter.drawLine(QPointF(x, y + a), QPointF(x, y + b))


class Overlay:
    """CPU-side description of what the overlay pass draws; filled by ModelView."""

    def __init__(self):
        self.markers = None        # (pts (n,3) display frame, labels, control bool (n,))
        self.cameras = None        # (pts (n,3), labels)
        self.shape_labels = []     # [(pts (n,3), labels, QColor[, style])]; style: label_size, label_bg, label_bg_color
        self.shape_points = []     # [(pts (n,3), style)]; style: color QColor, size, symbol, on_top
        self.shape_fills = []      # [([rings (n,3)], QColor with alpha)] polygon fills, painted under everything
        self.transparent = False   # transparent-surface mode: nothing is hidden by the surface
        self._fills = []
        self.show = {"markers": True, "marker_labels": True, "cameras": True, "camera_labels": False,
                     "shapes": True}
        self.legend = True
        self.last = {}             # diagnostics of the last pass (tests / screenshots)

    def items(self, mvp, width, height, depth_fn=None, near=None, far=None):
        """Screen-space draw list: [(kind, xy, label, colour, occluded)].
        depth_fn(xy) returns the depth-buffer values (0..1) under the points;
        both depths are linearised with near/far before comparing, because the
        perspective depth buffer is extremely non-linear (near = dist/1000)."""
        out = []
        self._fills = []
        if self.shape_fills and self.show.get("shapes"):
            from PySide6.QtGui import QPainterPath, QPolygonF
            for rings, col in self.shape_fills:
                path = QPainterPath(); path.setFillRule(Qt.OddEvenFill)
                for R in rings:
                    xy, _z, ok = project(mvp, R, width, height)
                    if len(xy) < 3 or not ok.all():           # partly behind the camera: skip the ring
                        continue
                    if (xy[:, 0].max() < 0 or xy[:, 0].min() > width or xy[:, 1].max() < 0
                            or xy[:, 1].min() > height):
                        continue
                    path.addPolygon(QPolygonF([QPointF(float(a), float(b)) for a, b in xy])); path.closeSubpath()
                if not path.isEmpty():
                    self._fills.append((path, col))

        def add(kind, pts, labels, colours, test_depth, style=None):
            xy, z, ok = project(mvp, pts, width, height)
            inside = ok & (xy[:, 0] >= -2) & (xy[:, 0] <= width + 2) & (xy[:, 1] >= -2) & (xy[:, 1] <= height + 2)
            occ = np.zeros(len(xy), bool)
            if test_depth and depth_fn is not None and inside.any():
                idx = np.flatnonzero(inside)
                d = depth_fn(xy[idx])
                if d is not None:
                    a, b = linear_depth(z[idx], near, far), linear_depth(np.asarray(d, float), near, far)
                    occ[idx] = a > b * 1.006 + 1e-6
            for k in np.flatnonzero(inside):
                c = colours[k] if isinstance(colours, (list, tuple)) else colours
                out.append((kind, (float(xy[k, 0]), float(xy[k, 1])), "" if labels is None else str(labels[k]),
                            c, bool(occ[k]), style))

        if self.cameras is not None and self.show.get("cameras") and self.show.get("camera_labels"):
            pts, labels = self.cameras
            add("camera", pts, labels, CAMERA, False)
        if self.shape_points and self.show.get("shapes"):
            for pts, st in self.shape_points:
                add("shape_pt", pts, None, st["color"], not st.get("on_top", True) and not self.transparent, st)
        if self.shape_labels and self.show.get("shapes"):
            for ent in self.shape_labels:
                pts, labels, col = ent[:3]
                add("shape", pts, labels, col, False, ent[3] if len(ent) > 3 else None)
        if self.markers is not None and self.show.get("markers"):
            pts, labels, ctl = self.markers
            cols = [CONTROL if c else CHECK for c in ctl]
            add("marker", pts, labels if self.show.get("marker_labels") else None, cols, True)
        return out

    def paint(self, painter: QPainter, items, width, height):
        """Draws the items (markers last, on top); returns the label rects drawn."""
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        font = label_font(painter.font()); painter.setFont(font)
        fm = QFontMetricsF(font)
        rects = []
        n_markers = n_hidden = 0
        for path, col in self._fills:
            painter.setPen(Qt.NoPen); painter.setBrush(QBrush(col)); painter.drawPath(path)
        for it in items:
            kind, (x, y), text, col, occ = it[:5]
            st = it[5] if len(it) > 5 else None
            if kind == "shape_pt":
                draw_symbol(painter, x, y, st.get("symbol", "ring"), float(st.get("size", 14.0)) / 2.0, col,
                            float(st.get("width", 2.0)), hidden=occ)
                continue
            if kind == "marker":
                n_markers += 1; n_hidden += occ
                r = 5.0
                painter.setPen(QPen(QColor(0, 0, 0, 200), 3.0)); painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(x, y), r, r)
                painter.setPen(QPen(col, 2.0 if occ else 1.5))
                painter.setBrush(Qt.NoBrush if occ else QBrush(col))
                painter.drawEllipse(QPointF(x, y), r, r)
            elif kind == "shape":
                pass                                     # the GL pass draws the shape points
            if text:
                lf, lfm = font, fm
                bg = QColor(20, 20, 24, 190 if not occ else 150)
                if st:                                   # shape label style
                    if abs(float(st.get("label_size", 9.0)) - 9.0) > 1e-6:
                        lf = QFont(font); lf.setPointSizeF(float(st["label_size"])); lfm = QFontMetricsF(lf)
                    if not st.get("label_bg", True):
                        bg = None
                    elif st.get("label_bg_color") is not None:
                        b = st["label_bg_color"]; bg = QColor(int(b[0]), int(b[1]), int(b[2]), 190)
                painter.setFont(lf)
                tw = lfm.horizontalAdvance(text); th = lfm.height()
                box = label_rect((x, y), tw, th, width, height)
                tc = QColor(col); tc.setAlpha(255)
                if bg is not None:
                    painter.setPen(Qt.NoPen); painter.setBrush(bg)
                    painter.drawRoundedRect(box, 3, 3)
                if lf is font and bg is not None:
                    painter.setPen(tc)
                    painter.drawText(box, Qt.AlignCenter, text)
                else:
                    # other sizes / halo text: rasterised once on the CPU and cached. The GL
                    # paint engine draws glyphs it has no cache for as stencil paths, which
                    # came out hatched / broken over the GL frame (12 pt shape labels)
                    img = self._text_image(text, lf, tc, bg is None, painter.device().devicePixelRatioF()
                                           if painter.device() is not None else 1.0)
                    painter.drawImage(QPointF(box.center().x() - img.width() / img.devicePixelRatio() / 2,
                                              box.center().y() - img.height() / img.devicePixelRatio() / 2), img)
                painter.setFont(font)
                rects.append((kind, text, box))
        if self.legend and n_markers:
            self._legend(painter, fm, height)
        painter.restore()
        self.last = {"items": len(items), "labels": len(rects), "markers": n_markers, "occluded": n_hidden,
                     "label_rects": rects}
        return rects

    def _text_image(self, text, font, colour, halo, dpr=1.0):
        from PySide6.QtGui import QImage
        cache = self.__dict__.setdefault("_text_cache", {})
        key = (text, font.toString(), colour.rgba(), halo, round(dpr, 2))
        img = cache.get(key)
        if img is None:
            fm = QFontMetricsF(font)
            w, h = fm.horizontalAdvance(text) + 4, fm.height() + 2
            img = QImage(max(1, int(w * dpr + 0.999)), max(1, int(h * dpr + 0.999)), QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr); img.fill(0)
            p = QPainter(img)
            p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing); p.setFont(font)
            r = QRectF(0, 0, w, h)
            if halo:
                p.setPen(QColor(0, 0, 0, 210))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1)):
                    p.drawText(r.translated(dx, dy), Qt.AlignCenter, text)
            p.setPen(colour); p.drawText(r, Qt.AlignCenter, text); p.end()
            if len(cache) > 2000:
                cache.clear()
            cache[key] = img
        return img

    def _legend(self, painter, fm, height):
        lines = list(LEGEND)
        if not self.show.get("marker_labels"):
            lines.append((QColor(220, 220, 220), "Labels hidden: Model > Show/Hide Items > Show Marker Labels"))
        lines.append((QColor(200, 200, 200), "hollow = hidden behind the surface"))
        h = fm.height() + 4
        w = max(fm.horizontalAdvance(t) for _c, t in lines) + 30
        box = QRectF(8, height - 12 - h * len(lines), w, h * len(lines) + 4)
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(20, 20, 24, 170))
        painter.drawRoundedRect(box, 4, 4)
        for k, (c, t) in enumerate(lines):
            cy = box.top() + 2 + h * k + h / 2
            if k < 2:
                painter.setPen(QPen(QColor(0, 0, 0), 1)); painter.setBrush(QBrush(c))
                painter.drawEllipse(QPointF(box.left() + 12, cy), 4.5, 4.5)
            elif "hollow" in t:
                painter.setPen(QPen(CONTROL, 1.5)); painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(box.left() + 12, cy), 4.5, 4.5)
            painter.setPen(c if k >= 2 else QColor(235, 235, 235))
            painter.drawText(QRectF(box.left() + 24, cy - h / 2, w - 24, h), Qt.AlignVCenter | Qt.AlignLeft, t)
