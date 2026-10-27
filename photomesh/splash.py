"""Startup splash screen: shown right after QApplication is created, before
the heavy imports (NumPy, OpenGL / pyqtgraph, the main window), with
progress messages. Qt-only imports so it appears within a fraction of a second.

A plain frameless widget, not QSplashScreen: in Qt 6 QSplashScreen's Show
event calls waitForWidgetMapped(), which cannot succeed from inside the Show
event and so always blocks for its full 1 s timeout before the window is even
mapped (the splash appeared ~1 s late and startup took 1 s longer)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QLinearGradient, QBrush
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_NAME, APP_VERSION

W, H = 440, 240


def _pixmap(dpr=1.0):
    from . import appicon
    pm = QPixmap(int(W * dpr), int(H * dpr)); pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(32, 34, 38))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0, QColor(40, 44, 52)); g.setColorAt(1, QColor(26, 28, 32))
    p.fillRect(QRectF(0, 0, W, H), QBrush(g))
    p.setPen(QColor(70, 76, 88)); p.drawRect(QRectF(0.5, 0.5, W - 1, H - 1))
    p.save(); p.translate(28, 34); appicon.draw(p, 96); p.restore()
    p.setPen(QColor(235, 238, 242))
    f = QFont(); f.setPointSizeF(20); f.setBold(True); p.setFont(f)
    p.drawText(QRectF(140, 40, W - 150, 36), Qt.AlignLeft | Qt.AlignVCenter, APP_NAME)
    f.setPointSizeF(10); f.setBold(False); p.setFont(f)
    p.setPen(QColor(160, 168, 180))
    p.drawText(QRectF(140, 78, W - 150, 22), Qt.AlignLeft | Qt.AlignVCenter, f"Version {APP_VERSION}")
    p.drawText(QRectF(140, 100, W - 150, 22), Qt.AlignLeft | Qt.AlignVCenter, "Offline photogrammetry")
    p.end()
    return pm


class Splash(QWidget):
    def __init__(self):
        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        app = QApplication.instance()
        screen = app.primaryScreen() if app else None
        dpr = screen.devicePixelRatio() if screen else 1.0
        self._pm = _pixmap(dpr)
        self._msg = ""
        self.messages = []
        self.setFixedSize(W, H)
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.center().x() - W // 2, g.center().y() - H // 2)

    def message(self):
        return self._msg

    def showMessage(self, text, *args):          # noqa: N802 - QSplashScreen-compatible name
        self._msg = text
        self.repaint()

    def paintEvent(self, ev):                    # noqa: N802 (Qt name)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pm)
        p.setPen(QColor(200, 206, 214))
        p.drawText(self.rect().adjusted(14, 0, -14, -12), Qt.AlignLeft | Qt.AlignBottom, self._msg)
        p.end()

    def step(self, text):
        """Show a progress message and paint it now (the GUI loop is not running yet)."""
        self.messages.append(text)
        self.showMessage(text + "...")
        QApplication.processEvents()

    def finish(self, main_window=None):
        """Close once the main window is up (it is shown just before this call)."""
        QApplication.processEvents()
        self.close()


def show():
    s = Splash()
    s.show()
    s.raise_()
    s.step("Starting")
    return s
