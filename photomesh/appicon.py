"""PhotoMesh Studio application icon: original artwork drawn with QPainter
(a triangulated mesh peak over terrain with a camera lens; no third-party
or Agisoft assets). Used for the window / taskbar / splash, and written as
a multi-size .ico (photomesh/resources/photomesh.ico) for the shortcut."""
from __future__ import annotations

import os
import struct
import sys

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
APP_USER_MODEL_ID = "PhotoMesh.PhotoMeshStudio"
ICO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "photomesh.ico")


def draw(p, n):
    """Draw the icon into painter p on an n x n canvas."""
    from PySide6.QtCore import Qt, QPointF, QRectF
    from PySide6.QtGui import QColor, QPen, QBrush, QLinearGradient, QPolygonF, QPainter, QPainterPath
    p.setRenderHint(QPainter.Antialiasing)
    k = n / 64.0
    p.save(); p.scale(k, k)
    g = QLinearGradient(0, 0, 0, 64)
    g.setColorAt(0, QColor(38, 110, 170)); g.setColorAt(1, QColor(18, 52, 92))
    p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 12, 12)
    clip = QPainterPath(); clip.addRoundedRect(QRectF(2, 2, 60, 60), 12, 12)
    p.setClipPath(clip)
    # terrain
    ground = QPolygonF([QPointF(4, 50), QPointF(18, 42), QPointF(30, 46), QPointF(46, 38), QPointF(60, 44),
                        QPointF(60, 60), QPointF(4, 60)])
    p.setBrush(QBrush(QColor(46, 150, 110))); p.drawPolygon(ground)
    # triangulated peak (the reconstructed mesh)
    V = [QPointF(10, 47), QPointF(22, 26), QPointF(32, 12), QPointF(42, 24), QPointF(54, 45), QPointF(32, 40),
         QPointF(20, 44), QPointF(43, 42)]
    F = [(0, 1, 6), (1, 5, 6), (1, 2, 5), (2, 3, 5), (3, 7, 5), (3, 4, 7)]
    shades = [QColor(232, 240, 248), QColor(200, 216, 232), QColor(246, 250, 255), QColor(214, 228, 242),
              QColor(190, 206, 226), QColor(226, 236, 246)]
    pen = QPen(QColor(20, 50, 85), max(1.2, 1.6 / max(k, 0.25) * 0.5)); pen.setJoinStyle(Qt.RoundJoin)
    for f, c in zip(F, shades):
        p.setBrush(QBrush(c)); p.setPen(pen)
        p.drawPolygon(QPolygonF([V[i] for i in f]))
    if n >= 24:                                   # vertices (hidden at tiny sizes)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(255, 160, 40)))
        for v in V[:6]:
            p.drawEllipse(v, 1.6, 1.6)
    # camera lens (top right)
    p.setPen(QPen(QColor(255, 255, 255), 2.4)); p.setBrush(QBrush(QColor(255, 160, 40)))
    p.drawEllipse(QPointF(50, 14), 7, 7)
    p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(18, 52, 92)))
    p.drawEllipse(QPointF(50, 14), 3, 3)
    p.restore()


def image(n):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    img = QImage(n, n, QImage.Format_ARGB32_Premultiplied); img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        draw(p, n)
    finally:
        p.end()
    return img


_ICON = None


def app_icon():
    """QIcon with every size drawn as a vector (crisp at 100-200 % scaling)."""
    global _ICON
    if _ICON is None:
        from PySide6.QtGui import QIcon, QPixmap
        ic = QIcon()
        for s in ICO_SIZES:
            ic.addPixmap(QPixmap.fromImage(image(s)))
        _ICON = ic
    return _ICON


def png_bytes(n):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.WriteOnly)
    image(n).save(buf, "PNG"); buf.close()
    return bytes(ba.data())


def ico_bytes(sizes=ICO_SIZES):
    """Multi-size Windows .ico with PNG entries (Vista+)."""
    imgs = [(s, png_bytes(s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(imgs))
    off = 6 + 16 * len(imgs)
    data = b""
    for s, b in imgs:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(b), off + len(data))
        data += b
    return out + data


def write_ico(path=ICO_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(ico_bytes())
    return path


def set_app_user_model_id(app_id=APP_USER_MODEL_ID):
    """Windows: own taskbar group + our icon instead of python.exe's. Returns True if set."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id)) == 0
    except Exception:  # noqa: BLE001
        return False


def install(app, window=None):
    """Window/taskbar icon on the application (all windows/dialogs) and the main window."""
    ic = app_icon()
    app.setWindowIcon(ic)
    if window is not None:
        window.setWindowIcon(ic)
    return ic
