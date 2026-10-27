"""Busy indication while a heavy task runs: wait cursor (QApplication
override cursor, counted so nested tasks restore it exactly once) and a
'Working: <task>...' label with a small indeterminate progress bar in the
status bar. The GUI keeps running (tasks run in threads / processes);
end() is always called from finished / finally, also on error or cancel."""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar


class BusyIndicator:
    def __init__(self, statusbar=None):
        self.titles = []
        self.label = QLabel(); self.label.hide()
        self.bar = QProgressBar(); self.bar.setRange(0, 0); self.bar.setTextVisible(False)
        self.bar.setFixedSize(64, 12); self.bar.hide()
        if statusbar is not None:
            statusbar.addPermanentWidget(self.label)
            statusbar.addPermanentWidget(self.bar)

    @property
    def active(self):
        return bool(self.titles)

    def begin(self, title):
        self.titles.append(str(title))
        if len(self.titles) == 1:
            QApplication.setOverrideCursor(Qt.BusyCursor)       # arrow + hourglass: UI stays clickable
        self._show()

    def end(self):
        if not self.titles:
            return
        self.titles.pop()
        if not self.titles:
            QApplication.restoreOverrideCursor()
        self._show()

    def end_all(self):
        while self.titles:
            self.end()

    def _show(self):
        on = bool(self.titles)
        if on:
            self.label.setText(f"Working: {self.titles[-1]}...")
        self.label.setVisible(on); self.bar.setVisible(on)

    @contextmanager
    def __call__(self, title):
        self.begin(title)
        try:
            yield self
        finally:
            self.end()
