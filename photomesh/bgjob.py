"""Run work off the GUI thread.

BackgroundJob(fn): fn(report, cancelled) runs in a worker thread.
  report(done, total, text="", item=None) - throttled progress (Qt signal, queued
  to the GUI thread); items passed to report() are delivered in batches via
  `batch` so the GUI can apply partial results (e.g. thumbnails) as they come.
  cancelled() -> True once cancel() was called.
run_modal(...) shows a cancellable progress dialog and keeps the event loop
running until the job ends (the window never stops responding)."""
from __future__ import annotations

import logging
import threading
import time
import traceback

from PySide6.QtCore import QObject, Signal, QEventLoop, QTimer, Qt

log = logging.getLogger("photomesh")


# most recent worker exception (name, error, traceback, time): the error dialog
# of the step that ran the job puts its traceback into the report
LAST_ERROR = {}


def recent_error(max_age=30.0):
    if LAST_ERROR and time.time() - LAST_ERROR.get("time", 0) <= max_age:
        return dict(LAST_ERROR)
    return {}


class BackgroundJob(QObject):
    progress = Signal(int, int, str)
    batch = Signal(list)
    done = Signal(object, object)          # result, error text (None if ok)

    def __init__(self, fn, name="job", interval=0.15, parent=None):
        super().__init__(parent)
        self.fn, self.name, self.interval = fn, name, interval
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._pending = []
        self._last = 0.0
        self.finished = False
        self.result = None
        self.error = None
        self.traceback = ""
        self.thread = None

    def cancel(self):
        self._cancel.set()

    def cancelled(self):
        return self._cancel.is_set()

    def _report(self, done, total, text="", item=None):
        flush = None
        with self._lock:
            if item is not None:
                self._pending.append(item)
            now = time.perf_counter()
            if now - self._last >= self.interval or done >= total:
                self._last = now
                flush, self._pending = self._pending, []
        if flush is not None:
            if flush:
                self.batch.emit(flush)
            self.progress.emit(int(done), int(total), str(text))

    def _run(self):
        try:
            self.result = self.fn(self._report, self.cancelled)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            notes = [str(n) for n in getattr(e, "__notes__", ())]
            if notes:
                self.error += "\n" + "\n".join(notes)
            self.traceback = traceback.format_exc()
            LAST_ERROR.update(name=self.name, error=self.error, traceback=self.traceback, time=time.time())
            log.error("%s failed:\n%s", self.name, self.traceback)
        with self._lock:
            rest, self._pending = self._pending, []
        if rest:
            self.batch.emit(rest)
        self.finished = True
        self.done.emit(self.result, self.error)

    def start(self):
        self.thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self.thread.start()
        return self

    def wait(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout)
        return self.finished


def run_modal(parent, title, label, fn, total, min_duration_ms=400):
    """Run fn(report, cancelled) in a thread behind a cancellable progress
    dialog. Returns (result, cancelled, error)."""
    from PySide6.QtWidgets import QProgressDialog, QApplication
    job = BackgroundJob(fn, name=title)
    dlg = QProgressDialog(label, "Cancel", 0, max(1, total), parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(min_duration_ms)
    dlg.setAutoClose(False); dlg.setAutoReset(False)
    dlg.canceled.connect(job.cancel)
    loop = QEventLoop()

    def prog(d, n, text):
        dlg.setMaximum(max(1, n)); dlg.setValue(min(d, n))
        if text:
            dlg.setLabelText(f"{label}\n{text}")
    job.progress.connect(prog)
    job.done.connect(lambda *_: loop.quit())
    busy = getattr(parent, "busy_indicator", None)       # wait cursor + 'Working: ...' in the status bar
    if busy is not None:
        busy.begin(title)
    try:
        job.start()
        if not job.finished:
            loop.exec()
        QApplication.processEvents()
    finally:
        if busy is not None:
            busy.end()
    cancelled = job.cancelled()
    dlg.canceled.disconnect(job.cancel)      # QProgressDialog emits canceled() when closed
    dlg.close(); dlg.deleteLater()
    return job.result, cancelled, job.error
