"""Toolbar buttons for grouped modes (Metashape-like split buttons).

A ModeButton is a QToolButton in MenuButtonPopup mode: the left part runs the
current mode, the right part (a clearly visible, >= 14 px wide arrow area)
opens the list of modes. The button shows the current mode's icon and a short
text label, and follows mode changes made anywhere (menus, toolbar, code)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QPointF
from PySide6.QtGui import QPainter, QColor, QPolygonF
from PySide6.QtWidgets import QToolButton, QMenu, QStyle, QStyleOptionToolButton

ARROW_W = 21          # width of the dropdown-arrow area, px (was 16; +30 %)


class ModeButton(QToolButton):
    def __init__(self, parent, entries, tip, icon_size=26, show_text=True):
        """entries: [(QAction, short_label)]; the first one is the initial mode."""
        super().__init__(parent)
        self.current = None
        self._labels = {}
        self._show_text = show_text
        self.setObjectName("modeButton")
        self._labels = {a: lab for a, lab in entries}
        self._tip = tip
        self._show_text = show_text
        menu = QMenu(self)
        for a, _lab in entries:
            menu.addAction(a)
            a.triggered.connect(lambda _=False, act=a: self.set_current(act))
        self.setMenu(menu)
        self.setPopupMode(QToolButton.MenuButtonPopup)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if show_text else Qt.ToolButtonIconOnly)
        self.setAutoRaise(True)
        # an explicit style sheet so the toolbar's compact one doesn't squeeze the arrow area
        self.setStyleSheet(
            f"QToolButton#modeButton{{padding:3px {ARROW_W + 4}px 3px 4px;margin:0px 1px;border:1px solid transparent;"
            f"border-radius:2px;}}"
            f"QToolButton#modeButton:hover{{border:1px solid #8a8a8a;background:rgba(127,127,127,40);}}"
            f"QToolButton#modeButton::menu-button{{width:{ARROW_W}px;border-left:1px solid rgba(127,127,127,90);}}"
            f"QToolButton#modeButton::menu-arrow{{image:none;width:0px;height:0px;}}")
        self.current = None
        self.set_current(entries[0][0])

    def set_current(self, action):
        if action not in self._labels:
            return
        self.current = action
        self.setDefaultAction(action)
        # setDefaultAction copies the long action text; show the short label instead
        self.setText(self._labels[action] if self._show_text else "")
        self.setToolTip(f"{self._tip}: {action.text()}  (click the arrow for other modes)")

    def actionEvent(self, ev):
        super().actionEvent(ev)
        if getattr(self, "current", None) is not None:          # keep the short label if the action changes
            self.setText(self._labels.get(self.current, "") if self._show_text else "")

    def labels(self):
        return [self._labels[a] for a in self._labels]

    def paintEvent(self, ev):
        super().paintEvent(ev)
        # draw our own, always-visible arrow in the menu-button area
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        x0 = self.width() - ARROW_W; cx = x0 + ARROW_W / 2; cy = self.height() / 2 + 1
        col = self.palette().buttonText().color() if self.isEnabled() else QColor(140, 140, 140)
        p.setPen(Qt.NoPen); p.setBrush(col)
        p.drawPolygon(QPolygonF([QPointF(cx - 5, cy - 2.5), QPointF(cx + 5, cy - 2.5), QPointF(cx, cy + 3.5)]))
        p.end()
