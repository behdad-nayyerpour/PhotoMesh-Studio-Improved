"""Busy indication while a heavy task runs: wait cursor (QApplication
override cursor, counted so nested tasks restore it exactly once) and a
'Working: <task>...' label with a small indeterminate progress bar in the
status bar. The GUI keeps running (tasks run in threads / processes);
end() is always called from finished / finally, also on error or cancel."""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import Qt, QTimer
from .progress import WorkProgress
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar


class BusyIndicator:
    def __init__(self, statusbar=None):
        self.titles = []
        self.tasks = {}
        self.label = QLabel(); self.label.hide()
        self.bar = QProgressBar(); self.bar.setRange(0, 0); self.bar.setTextVisible(False)
        self.bar.setFixedSize(64, 12); self.bar.hide()
        self.timer = QTimer(self.label)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._show)
        self.label.setMaximumWidth(650)
        if statusbar is not None:
            statusbar.addPermanentWidget(self.label)
            statusbar.addPermanentWidget(self.bar)

    @property
    def active(self):
        return bool(self.titles or self.tasks)

    def begin(self, title):
        title = str(title)
        self.titles.append(title)
        self.tasks.setdefault(title, [0, WorkProgress(), ""])[0] += 1
        self.timer.start()
        if len(self.titles) == 1:
            QApplication.setOverrideCursor(Qt.BusyCursor)       # arrow + hourglass: UI stays clickable
        self._show()

    def end(self):
        if not self.titles:
            return
        title = self.titles.pop()
        self.tasks[title][0] -= 1
        if self.tasks[title][0] <= 0:
            self.tasks.pop(title, None)
        if not self.titles:
            QApplication.restoreOverrideCursor()
        self._show()

    def end_all(self):
        while self.titles:
            self.end()
        self.tasks.clear()
        self._show()

    def _show(self):
        on = bool(self.titles or self.tasks)
        if on:
            title = next(reversed(self.tasks))
            _count, meter, detail = self.tasks[title]
            self.label.setText(f"Working: {detail or title} - {meter.text()}")
            self.label.setToolTip("\n".join(f"{name}: {data[2] or data[1].text()}" for name, data in self.tasks.items()))
        else:
            self.timer.stop()
        self.label.setVisible(on); self.bar.setVisible(on)
        host = self.label.window()
        host.setCursor(Qt.BusyCursor) if on else host.unsetCursor()

    def background(self, key, title=None, done=None, total=None, text=""):
        """Nonmodal tasks stay visible in the shared status area."""
        key = str(key)
        if title is None:
            self.tasks.pop(key, None)
        else:
            if key not in self.tasks:
                self.tasks[key] = [0, WorkProgress(), ""]
            if done is not None:
                self.tasks[key][1].update(done, total or 0)
            self.tasks[key][2] = str(title) + (": " + text if text else "")
            self.timer.start()
        if not self.titles:
            host = self.label.window()
            host.setCursor(Qt.BusyCursor) if self.tasks else host.unsetCursor()
        self._show()

    @contextmanager
    def __call__(self, title):
        self.begin(title)
        try:
            yield self
        finally:
            self.end()
