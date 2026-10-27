"""Original layer icons for the 3D-view toolbar, drawn with QPainter at runtime
(no bundled third-party artwork)."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF, QRectF, QSize
from PySide6.QtGui import (QIcon, QIconEngine, QImage, QPixmap, QPainter, QPainterPath, QColor, QPen, QBrush,
                           QPolygonF, QFont)

_SIZE = 32          # design grid: every icon is drawn in 32 x 32 units and scaled as a vector

# Toolbar icon sizes in logical (device-independent) px. They were 16 / 20 / 16 / 16;
# all were made 30 % larger (16 * 1.3 = 20.8 -> 21, 20 * 1.3 = 26). At 150 % Windows
# scaling 21 logical px is drawn as a crisp 32 x 32 device-pixel icon.
TOOLBAR_ICON = 21           # main toolbar buttons
MODE_ICON = 26              # split display-mode buttons (Tie Points / Colors / Shaded)
SELECT_ICON = 21            # navigation / selection split button
PANE_ICON = 21              # pane toolbars (Workspace)
MIN_STROKE_PX = 1.25        # thinnest line, in device pixels, after scaling


def _canvas():
    pm = QPixmap(_SIZE, _SIZE)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def _dots(p, pts, col, r):
    p.setPen(Qt.NoPen); p.setBrush(QBrush(col))
    for x, y in pts:
        p.drawEllipse(QPointF(x, y), r, r)


class _IconPainter(QPainter):
    """QPainter that keeps strokes at least MIN_STROKE_PX device pixels wide
    when the 32-unit design is drawn small, so icons stay legible."""
    min_width = 0.0

    def setPen(self, pen):                 # noqa: N802 (Qt name)
        if isinstance(pen, QPen) and pen.style() != Qt.NoPen and 0 < pen.widthF() < self.min_width:
            pen = QPen(pen); pen.setWidthF(self.min_width)
        super().setPen(pen)


def render_image(key, device_px, outline=True) -> QImage:
    """Draw icon `key` as a vector at exactly device_px x device_px pixels
    (never by upscaling a small bitmap). `outline` adds a soft dark halo
    around the shapes for contrast on both dark and light toolbars."""
    n = max(1, int(device_px))
    img = QImage(n, n, QImage.Format_ARGB32_Premultiplied); img.fill(Qt.transparent)
    p = _IconPainter(img)
    p.setRenderHint(QPainter.Antialiasing); p.setRenderHint(QPainter.TextAntialiasing)
    k = n / _SIZE
    p.min_width = MIN_STROKE_PX / k
    p.scale(k, k)
    try:
        _draw(key, p)
    finally:
        p.end()
    if not outline:
        return img
    mask = QImage(img); mp = QPainter(mask)
    mp.setCompositionMode(QPainter.CompositionMode_SourceIn); mp.fillRect(mask.rect(), QColor(0, 0, 0)); mp.end()
    out = QImage(n, n, QImage.Format_ARGB32_Premultiplied); out.fill(Qt.transparent)
    op = QPainter(out)
    d = max(1.0, n / 32.0)
    op.setOpacity(0.22)
    for dx, dy in ((-d, 0), (d, 0), (0, -d), (0, d), (-d, -d), (d, d), (-d, d), (d, -d)):
        op.drawImage(QPointF(dx, dy), mask)
    op.setOpacity(1.0); op.drawImage(0, 0, img); op.end()
    return out


def _disabled(img: QImage) -> QImage:
    g = img.convertToFormat(QImage.Format_ARGB32)
    for y in range(g.height()):
        for x in range(g.width()):
            c = g.pixelColor(x, y)
            if c.alpha():
                v = int(0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue())
                g.setPixelColor(x, y, QColor(v, v, v, int(c.alpha() * 0.45)))
    return g


class PainterIconEngine(QIconEngine):
    """Icon engine that redraws the painter icon at every requested size and
    device pixel ratio, so toolbar icons are crisp at 100 %, 150 %, 200 %."""
    _cache = {}

    def __init__(self, key):
        super().__init__()
        self.name = key

    def clone(self):
        return PainterIconEngine(self.name)

    def key(self):                          # noqa: D401 (QIconEngine API)
        return "photomesh_painter"

    def _image(self, device_px, mode):
        ck = (self.name, int(device_px), mode == QIcon.Disabled)
        im = self._cache.get(ck)
        if im is None:
            im = render_image(self.name, device_px)
            if mode == QIcon.Disabled:
                im = _disabled(im)
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[ck] = im
        return im

    def scaledPixmap(self, size, mode, state, scale):          # noqa: N802
        side = min(size.width(), size.height())
        dev = max(1, round(side * scale))
        pm = QPixmap.fromImage(self._image(dev, mode))
        pm.setDevicePixelRatio(dev / side if side else 1.0)
        return pm

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def paint(self, painter, rect, mode, state):
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        side = min(rect.width(), rect.height())
        im = self._image(max(1, round(side * dpr)), mode)
        x = rect.x() + (rect.width() - side) / 2; y = rect.y() + (rect.height() - side) / 2
        painter.save(); painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(QRectF(x, y, side, side), im); painter.restore()

    def actualSize(self, size, mode, state):                    # noqa: N802
        s = min(size.width(), size.height())
        return QSize(s, s)

    def availableSizes(self, mode=QIcon.Normal, state=QIcon.Off):  # noqa: N802
        return [QSize(s, s) for s in (16, 21, 24, 26, 32, 48)]


def _draw(key, p):
    S = _SIZE
    if key == "tie":                       # sparse dots
        _dots(p, [(7, 9), (16, 6), (25, 10), (10, 18), (21, 17), (6, 26), (16, 25), (26, 25)],
              QColor(120, 200, 255), 2.7)
    elif key == "cloud":                   # dense coloured dots
        cols = [QColor(220, 120, 80), QColor(120, 200, 110), QColor(110, 150, 230), QColor(230, 210, 110)]
        k = 0
        for y in range(5, 29, 4):
            for x in range(5 + (y // 4) % 2 * 2, 29, 4):
                _dots(p, [(x, y)], cols[k % 4], 1.6); k += 1
    elif key == "mesh":                    # triangulated surface
        pts = [QPointF(4, 24), QPointF(12, 10), QPointF(20, 22), QPointF(28, 8), QPointF(28, 26), QPointF(4, 8)]
        p.setBrush(QBrush(QColor(150, 160, 175)))
        p.setPen(QPen(QColor(230, 235, 245), 1.4))
        for tri in ((5, 1, 0), (1, 2, 0), (1, 3, 2), (3, 4, 2)):
            p.drawPolygon(QPolygonF([pts[i] for i in tri]))
    elif key in ("cameras", "camera_labels"):   # frustum
        p.setPen(QPen(QColor(90, 165, 255), 1.8)); p.setBrush(Qt.NoBrush)
        apex, quad = QPointF(16, 27), [QPointF(5, 7), QPointF(27, 7), QPointF(23, 15), QPointF(9, 15)]
        p.drawPolygon(QPolygonF(quad))
        for q in quad[2:]:
            p.drawLine(apex, q)
        if key == "camera_labels":
            _label(p, "A", QColor(150, 190, 255))
    elif key in ("markers", "marker_labels"):    # flag on a pole + target
        p.setPen(QPen(QColor(240, 240, 240), 1.6)); p.drawLine(QPointF(10, 28), QPointF(10, 5))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(255, 210, 40)))
        p.drawPolygon(QPolygonF([QPointF(10, 5), QPointF(24, 9), QPointF(10, 14)]))
        p.setBrush(QBrush(QColor(60, 220, 255))); p.drawEllipse(QPointF(10, 28), 3, 2)
        if key == "marker_labels":
            _label(p, "A", QColor(255, 230, 120))
    elif key == "grid":                    # perspective base plane
        p.setPen(QPen(QColor(185, 188, 205), 1.5))
        for k in range(5):
            t = k / 4
            p.drawLine(QPointF(10 + 12 * t, 8), QPointF(2 + 28 * t, 27))
            y = 8 + 19 * t
            half = 6 + 8 * t
            p.drawLine(QPointF(16 - half, y), QPointF(16 + half, y))
    elif key == "region":                  # bounding box
        p.setPen(QPen(QColor(255, 150, 60), 1.6)); p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(5, 11, 16, 16)); p.drawRect(QRectF(11, 5, 16, 16))
        for a, b in (((5, 11), (11, 5)), ((21, 11), (27, 5)), ((5, 27), (11, 21)), ((21, 27), (27, 21))):
            p.drawLine(QPointF(*a), QPointF(*b))
    elif key == "trackball":               # three orbit rings
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(90, 130, 255), 1.6)); p.drawEllipse(QPointF(16, 16), 12, 12)
        p.setPen(QPen(QColor(255, 90, 90), 1.6)); p.drawEllipse(QPointF(16, 16), 12, 5)
        p.setPen(QPen(QColor(90, 230, 110), 1.6)); p.drawEllipse(QPointF(16, 16), 5, 12)
    elif key == "ortho":                   # top-view photo tile: fields, a road, a north tick
        p.setPen(QPen(QColor(30, 40, 30), 1.2)); p.setBrush(QBrush(QColor(120, 160, 90)))
        p.drawRect(QRectF(4, 4, 24, 24))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(175, 150, 95))); p.drawRect(QRectF(4, 4, 11, 10))
        p.setBrush(QBrush(QColor(90, 130, 70))); p.drawRect(QRectF(17, 17, 11, 11))
        p.setBrush(QBrush(QColor(200, 185, 150))); p.drawRect(QRectF(19, 5, 7, 8))
        p.setPen(QPen(QColor(230, 225, 210), 2.4)); p.drawLine(QPointF(4, 22), QPointF(28, 12))
        p.setPen(QPen(QColor(250, 250, 250), 1.2)); p.setBrush(Qt.NoBrush); p.drawRect(QRectF(4, 4, 24, 24))
    elif key == "shapes":                  # vector overlay: polygon outline, a polyline and vertices
        poly = [QPointF(5, 9), QPointF(19, 5), QPointF(27, 15), QPointF(17, 27), QPointF(6, 21)]
        p.setBrush(QBrush(QColor(255, 140, 60, 70)))
        p.setPen(QPen(QColor(255, 150, 70), 1.8))
        p.drawPolygon(QPolygonF(poly))
        p.setPen(QPen(QColor(90, 220, 140), 1.6, Qt.DashLine))
        p.drawPolyline(QPolygonF([QPointF(3, 28), QPointF(13, 16), QPointF(29, 24)]))
        _dots(p, [(q.x(), q.y()) for q in poly], QColor(255, 235, 120), 2.0)
    elif key == "transparency":            # see-through surface: a dashed shape behind a translucent sheet
        p.setPen(QPen(QColor(255, 150, 70), 2.0, Qt.DashLine)); p.setBrush(Qt.NoBrush)
        p.drawPolygon(QPolygonF([QPointF(4, 20), QPointF(14, 6), QPointF(27, 12), QPointF(22, 28)]))
        p.setPen(QPen(QColor(220, 228, 240), 1.4)); p.setBrush(QBrush(QColor(170, 185, 205, 110)))
        p.drawPolygon(QPolygonF([QPointF(9, 9), QPointF(30, 13), QPointF(24, 30), QPointF(3, 26)]))
    elif key == "dem":                     # elevation colours + contour lines
        for k, col in enumerate((QColor(40, 90, 180), QColor(60, 170, 200), QColor(90, 190, 90),
                                 QColor(230, 210, 80), QColor(200, 80, 50))):
            p.setPen(Qt.NoPen); p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(16, 18), 13 - 2.4 * k, 11 - 2.0 * k)
        p.setPen(QPen(QColor(30, 30, 30, 160), 1.0)); p.setBrush(Qt.NoBrush)
        for k in range(1, 5):
            p.drawEllipse(QPointF(16, 18), 13 - 2.4 * k, 11 - 2.0 * k)
    elif key == "chunk":                   # stacked sheets
        for k, col in enumerate((QColor(90, 130, 190), QColor(120, 165, 225), QColor(170, 205, 250))):
            p.setPen(QPen(QColor(40, 60, 90), 1.2)); p.setBrush(QBrush(col))
            p.drawRect(QRectF(4 + 3 * k, 4 + 4 * k, 20, 15))
    elif key == "workspace":
        p.setPen(QPen(QColor(120, 90, 30), 1.2)); p.setBrush(QBrush(QColor(235, 190, 80)))
        p.drawRect(QRectF(3, 10, 26, 17)); p.drawRect(QRectF(3, 6, 11, 5))
        p.setBrush(QBrush(QColor(120, 165, 225))); p.drawRect(QRectF(9, 14, 14, 9))
    elif key == "folder":
        p.setPen(QPen(QColor(150, 110, 30), 1.2)); p.setBrush(QBrush(QColor(240, 200, 95)))
        p.drawPolygon(QPolygonF([QPointF(3, 8), QPointF(12, 8), QPointF(15, 11), QPointF(29, 11),
                                 QPointF(29, 26), QPointF(3, 26)]))
    elif key == "photo":                   # small landscape picture
        p.setPen(QPen(QColor(70, 80, 95), 1.4)); p.setBrush(QBrush(QColor(200, 225, 250)))
        p.drawRect(QRectF(3, 6, 26, 20))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(90, 160, 90)))
        p.drawPolygon(QPolygonF([QPointF(4, 25), QPointF(13, 14), QPointF(19, 21), QPointF(23, 17), QPointF(28, 25)]))
        p.setBrush(QBrush(QColor(250, 200, 60))); p.drawEllipse(QPointF(23, 11), 2.5, 2.5)
    elif key == "photo_off":
        _draw("photo", p)
        p.setPen(QPen(QColor(220, 60, 60), 3)); p.drawLine(QPointF(5, 27), QPointF(27, 5))
    elif key == "depth":                   # depth gradient
        for k in range(6):
            p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(40 + 35 * k, 60 + 25 * k, 200 - 25 * k)))
            p.drawRect(QRectF(4, 4 + 4 * k, 24, 4))
        p.setPen(QPen(QColor(220, 220, 230), 1)); p.setBrush(Qt.NoBrush); p.drawRect(QRectF(4, 4, 24, 24))
    elif key == "add_chunk":
        _draw("chunk", p); _plus(p)
    elif key == "add_photos":
        _draw("photo", p); _plus(p)
    elif key == "add_folder":
        _draw("folder", p); _plus(p)
    elif key == "enable":
        p.setPen(QPen(QColor(120, 120, 130), 1.6)); p.setBrush(Qt.NoBrush); p.drawEllipse(QPointF(16, 16), 11, 11)
        p.setPen(QPen(QColor(120, 120, 130), 2.4)); p.drawLine(QPointF(8.5, 23.5), QPointF(23.5, 8.5))
    elif key == "remove":
        p.setPen(QPen(QColor(200, 70, 70), 3)); p.drawLine(QPointF(8, 8), QPointF(24, 24)); p.drawLine(QPointF(24, 8), QPointF(8, 24))
    elif key in ("navigation", "rect_sel", "circle_sel", "free_sel"):
        if key == "navigation":            # arrow cursor
            p.setPen(QPen(QColor(30, 30, 30), 1.2)); p.setBrush(QBrush(QColor(245, 245, 245)))
            p.drawPolygon(QPolygonF([QPointF(9, 4), QPointF(9, 26), QPointF(14, 21), QPointF(18, 29),
                                     QPointF(21, 28), QPointF(17, 20), QPointF(24, 20)]))
        else:
            pen = QPen(QColor(90, 165, 255), 1.8); pen.setStyle(Qt.DashLine); p.setPen(pen); p.setBrush(Qt.NoBrush)
            if key == "rect_sel":
                p.drawRect(QRectF(5, 7, 22, 18))
            elif key == "circle_sel":
                p.drawEllipse(QPointF(16, 16), 11, 11)
            else:
                p.drawPolygon(QPolygonF([QPointF(5, 12), QPointF(14, 5), QPointF(27, 10), QPointF(22, 26), QPointF(9, 24)]))
    elif key in ("mode_shaded", "mode_solid", "mode_textured", "mode_wireframe"):
        pts = [QPointF(4, 24), QPointF(12, 10), QPointF(20, 22), QPointF(28, 8), QPointF(28, 26), QPointF(4, 8)]
        fill = {"mode_shaded": QColor(165, 170, 180), "mode_solid": QColor(120, 150, 200),
                "mode_textured": QColor(170, 120, 80), "mode_wireframe": None}[key]
        p.setBrush(QBrush(fill) if fill else Qt.NoBrush)
        p.setPen(QPen(QColor(90, 200, 255) if fill is None else QColor(235, 238, 245), 1.4))
        for k, tri in enumerate(((5, 1, 0), (1, 2, 0), (1, 3, 2), (3, 4, 2))):
            if key == "mode_textured":
                p.setBrush(QBrush([QColor(170, 120, 80), QColor(120, 160, 90), QColor(190, 160, 120), QColor(110, 130, 90)][k]))
            elif key == "mode_shaded":
                p.setBrush(QBrush(QColor(110 + 30 * k, 115 + 30 * k, 125 + 30 * k)))
            p.drawPolygon(QPolygonF([pts[i] for i in tri]))
    elif key in ("cloud_colors", "cloud_solid"):
        cols = [QColor(220, 120, 80), QColor(120, 200, 110), QColor(110, 150, 230), QColor(230, 210, 110)]
        k = 0
        for y in range(5, 29, 4):
            for x in range(5 + (y // 4) % 2 * 2, 29, 4):
                _dots(p, [(x, y)], cols[k % 4] if key == "cloud_colors" else QColor(200, 200, 212), 1.6); k += 1
    elif key == "show_items":              # eye
        p.setPen(QPen(QColor(220, 220, 230), 1.6)); p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(3, 9, 26, 14))
        p.setBrush(QBrush(QColor(90, 165, 255))); p.drawEllipse(QPointF(16, 16), 4.5, 4.5)
    elif key == "zoom_in" or key == "zoom_out":
        p.setPen(QPen(QColor(220, 220, 230), 2.2)); p.setBrush(Qt.NoBrush); p.drawEllipse(QPointF(13, 13), 8, 8)
        p.drawLine(QPointF(19, 19), QPointF(28, 28)); p.drawLine(QPointF(9, 13), QPointF(17, 13))
        if key == "zoom_in":
            p.drawLine(QPointF(13, 9), QPointF(13, 17))
    elif key == "reset_view":
        p.setPen(QPen(QColor(90, 165, 255), 2.2)); p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(6, 6, 20, 20)); p.drawLine(QPointF(16, 2), QPointF(16, 30)); p.drawLine(QPointF(2, 16), QPointF(30, 16))
    elif key == "align":                   # two photos + link
        p.setPen(QPen(QColor(70, 80, 95), 1.2)); p.setBrush(QBrush(QColor(200, 225, 250)))
        p.drawRect(QRectF(3, 5, 14, 11)); p.drawRect(QRectF(15, 16, 14, 11))
        p.setPen(QPen(QColor(90, 200, 120), 2)); p.drawLine(QPointF(10, 16), QPointF(22, 16))
    elif key == "file_new":                # sheet with folded corner
        p.setPen(QPen(QColor(60, 70, 85), 1.6)); p.setBrush(QBrush(QColor(245, 247, 250)))
        p.drawPolygon(QPolygonF([QPointF(7, 3), QPointF(19, 3), QPointF(26, 10), QPointF(26, 29), QPointF(7, 29)]))
        p.setBrush(QBrush(QColor(195, 205, 220)))
        p.drawPolygon(QPolygonF([QPointF(19, 3), QPointF(19, 10), QPointF(26, 10)]))
        p.setPen(QPen(QColor(120, 150, 200), 1.6))
        for y in (15, 19, 23):
            p.drawLine(QPointF(11, y), QPointF(22, y))
    elif key == "file_open":               # open folder
        p.setPen(QPen(QColor(140, 100, 25), 1.6)); p.setBrush(QBrush(QColor(215, 170, 60)))
        p.drawPolygon(QPolygonF([QPointF(3, 7), QPointF(12, 7), QPointF(15, 10), QPointF(26, 10),
                                 QPointF(26, 14), QPointF(3, 27)]))
        p.setBrush(QBrush(QColor(250, 210, 100)))
        p.drawPolygon(QPolygonF([QPointF(8, 14), QPointF(30, 14), QPointF(25, 27), QPointF(3, 27)]))
    elif key == "file_save":               # floppy disk
        p.setPen(QPen(QColor(25, 45, 85), 1.6)); p.setBrush(QBrush(QColor(60, 115, 200)))
        p.drawPolygon(QPolygonF([QPointF(4, 4), QPointF(24, 4), QPointF(28, 8), QPointF(28, 28), QPointF(4, 28)]))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(235, 240, 248))); p.drawRect(QRectF(9, 4.8, 13, 8))
        p.setBrush(QBrush(QColor(25, 45, 85))); p.drawRect(QRectF(17, 6, 3, 5.5))
        p.setBrush(QBrush(QColor(250, 250, 252))); p.drawRect(QRectF(8, 17, 16, 10))
        p.setPen(QPen(QColor(150, 160, 175), 1.2))
        p.drawLine(QPointF(11, 21), QPointF(21, 21)); p.drawLine(QPointF(11, 24), QPointF(21, 24))
    elif key in ("undo", "redo"):          # curved arrow
        if key == "redo":
            p.translate(32, 0); p.scale(-1, 1)
        path = QPainterPath(QPointF(9, 12))
        path.cubicTo(QPointF(17, 5), QPointF(29, 9), QPointF(27, 19))
        path.cubicTo(QPointF(26, 25), QPointF(20, 27), QPointF(14, 27))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(20, 30, 45), 5.2, Qt.SolidLine, Qt.RoundCap)); p.drawPath(path)
        p.setPen(QPen(QColor(110, 185, 255), 3.0, Qt.SolidLine, Qt.RoundCap)); p.drawPath(path)
        p.setPen(QPen(QColor(20, 30, 45), 1.2)); p.setBrush(QBrush(QColor(110, 185, 255)))
        p.drawPolygon(QPolygonF([QPointF(3, 13), QPointF(13, 4), QPointF(14, 17)]))
    elif key == "preferences":             # gear
        p.setPen(QPen(QColor(40, 45, 55), 1.4)); p.setBrush(QBrush(QColor(200, 205, 215)))
        poly = QPolygonF()
        for i in range(48):
            a = i / 48 * 2 * math.pi
            r = 13 if (i // 3) % 2 == 0 else 10
            poly.append(QPointF(16 + r * math.cos(a), 16 + r * math.sin(a)))
        p.drawPolygon(poly)
        p.setBrush(QBrush(QColor(58, 58, 61))); p.drawEllipse(QPointF(16, 16), 4.5, 4.5)
    else:
        p.setPen(QPen(QColor(200, 200, 200), 2)); p.drawEllipse(QPointF(16, 16), 10, 10)


def _plus(p):
    p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(40, 170, 70))); p.drawEllipse(QPointF(24, 24), 7.5, 7.5)
    p.setPen(QPen(QColor(255, 255, 255), 2.4)); p.drawLine(QPointF(20, 24), QPointF(28, 24)); p.drawLine(QPointF(24, 20), QPointF(24, 28))


def _label(p, text, col):
    f = QFont(); f.setBold(True); f.setPixelSize(13)
    p.setFont(f)
    p.setPen(QPen(QColor(20, 20, 25), 3))
    r = QRectF(17, 15, 15, 17)
    p.setBrush(QBrush(QColor(20, 20, 25))); p.drawRoundedRect(r, 3, 3)
    p.setPen(col); p.drawText(r, Qt.AlignCenter, text)


_CACHE = {}


def layer_icon(key) -> QIcon:
    """Resolution-independent icon: redrawn as a vector at whatever size and
    device pixel ratio it is shown (16 px tree rows, 21/26 px toolbars, HiDPI)."""
    if key in _CACHE:
        return _CACHE[key]
    icon = QIcon(PainterIconEngine(key))
    _CACHE[key] = icon
    return icon


icon = layer_icon
