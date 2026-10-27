"""Error dialog and error-report plumbing (see report.py).

ErrorDialog: one dialog for every failure - message, Details (the report),
Copy report, Open report folder, Report to GitHub (opens the browser with a
prefilled issue; the app itself never sends anything).

ErrorReporter: builds and saves a report for uncaught exceptions (GUI thread,
Python threads, QThread), Qt critical/fatal messages, and failed steps, and
shows the dialog on the GUI thread.
"""
from __future__ import annotations

import html
import logging
import os
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QStyle, QVBoxLayout)

from . import report as rp

log = logging.getLogger("photomesh")
SHELL_URL_LIMIT = 2000     # longer URLs go through a small local HTML page (Windows ShellExecute limits)


def open_url(url):
    """QDesktopServices.openUrl for an already percent-encoded URL."""
    return QDesktopServices.openUrl(QUrl.fromEncoded(url.encode("ascii")))


def open_issue(url, directory, rid):
    """Open the prefilled GitHub issue in the default browser. Long URLs are
    opened through <rid>_github.html (redirect page, also explains what to do
    offline), so no OS URL-length limit cuts them. Returns (ok, how)."""
    if len(url) <= SHELL_URL_LIMIT:
        return bool(open_url(url)), "direct"
    page = os.path.join(directory, rid + "_github.html")
    u = html.escape(url, quote=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="1; url={u}"><title>PhotoMesh Studio - report to GitHub</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:46em;margin:3em auto;line-height:1.5}}</style></head>
<body><h2>Opening GitHub...</h2>
<p>Your error report is being filled into a new GitHub issue. Check it, attach <b>{html.escape(rid)}.zip</b>
if you like, and press <b>Submit</b>. Nothing is sent until you do.</p>
<p><a href="{u}">Click here if GitHub does not open.</a></p>
<p>No internet? The report is saved on this computer in this folder; open this page again later or send the
.zip from any computer.</p>
<p dir="rtl">گزارش خطا در یک Issue جدید در GitHub قرار می‌گیرد؛ آن را بررسی کنید و Submit را بزنید. بدون اینترنت؟
گزارش روی همین کامپیوتر ذخیره شده است و می‌توانید بعداً آن را بفرستید.</p>
<script>setTimeout(function(){{location.replace({_js(url)});}},150);</script>
</body></html>""")
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(page))), "page"


def _esc(s):
    """Escape for QLabel rich text (Qt does not decode &#x27;, so quotes stay as they are)."""
    return html.escape(str(s), quote=False)


def _js(s):
    import json
    return json.dumps(s).replace("</", "<\\/")


class ErrorDialog(QDialog):
    """Metashape-style error dialog with the report actions."""

    def __init__(self, report, md_path, zip_path, parent=None, session_log=None, manual=False):
        super().__init__(parent)
        self.report, self.md_path, self.zip_path = report, md_path, zip_path
        self.session_log, self.manual = session_log, manual
        self.setObjectName("error_dialog")
        self.setWindowTitle(("Report a Problem" if manual else report["title"] or "Error") + " - PhotoMesh Studio")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(self.style().standardIcon(
            QStyle.SP_MessageBoxInformation if manual else QStyle.SP_MessageBoxCritical).pixmap(40, 40))
        icon.setAlignment(Qt.AlignTop)
        top.addWidget(icon)
        txt = QVBoxLayout()
        head = QLabel(f"<b>{_esc(report['title'] or 'Error')}</b>" if not manual else
                      "<b>Describe the problem and send it as a GitHub issue.</b>")
        head.setWordWrap(True)
        txt.addWidget(head)
        msg = report["message"] or ""
        e = report.get("exit")
        if e and e.get("hex") and e["hex"] not in msg:
            msg += f"\nExit code {e['text']}"
        self.message = QLabel(_esc(msg.strip()).replace("\n", "<br>"))
        self.message.setWordWrap(True); self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.message.setObjectName("error_message")
        if msg.strip():
            txt.addWidget(self.message)
        self.saved = QLabel()
        self.saved.setWordWrap(True); self.saved.setObjectName("report_saved")
        self.saved.setStyleSheet("color: palette(mid);")
        txt.addWidget(self.saved)
        top.addLayout(txt, 1)
        lay.addLayout(top)
        lay.addWidget(QLabel("What were you doing? (optional, added to the report)"))
        self.description = QPlainTextEdit(); self.description.setObjectName("report_description")
        self.description.setPlaceholderText("e.g. Build Point Cloud, quality High, 640 drone photos ...")
        self.description.setFixedHeight(56 if not manual else 130)
        lay.addWidget(self.description)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setObjectName("report_details")
        f = QFont("Consolas" if sys.platform == "win32" else "Monospace"); f.setStyleHint(QFont.Monospace)
        f.setPointSize(9); self.details.setFont(f)
        self.details.setMinimumHeight(260); self.details.setVisible(False)
        lay.addWidget(self.details, 1)
        self.note = QLabel(
            "<b>Report to GitHub</b> opens your web browser with the report filled in - nothing is sent until you "
            "press <i>Submit</i> there. No internet now? The report stays saved; you can send it later.")
        self.note.setWordWrap(True); self.note.setObjectName("report_note")
        lay.addWidget(self.note)
        row = QHBoxLayout()
        self.details_btn = QPushButton("Details \u25b8"); self.details_btn.setCheckable(True)
        self.copy_btn = QPushButton("Copy report")
        self.folder_btn = QPushButton("Open report folder")
        self.github_btn = QPushButton("Report to GitHub")
        self.github_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowUp))
        self.close_btn = QPushButton("Close"); self.close_btn.setDefault(True)
        for b, name in ((self.details_btn, "details"), (self.copy_btn, "copy"), (self.folder_btn, "folder"),
                        (self.github_btn, "github"), (self.close_btn, "close")):
            b.setObjectName(f"report_{name}_button")
        row.addWidget(self.details_btn); row.addWidget(self.copy_btn); row.addWidget(self.folder_btn)
        row.addStretch(1); row.addWidget(self.github_btn); row.addWidget(self.close_btn)
        lay.addLayout(row)
        if not rp.issues_url():
            self.github_btn.setVisible(False)
            self.note.setText("The report is saved on this computer; send the .zip to your support contact.")
        self.details_btn.toggled.connect(self._toggle_details)
        self.copy_btn.clicked.connect(self.copy_report)
        self.folder_btn.clicked.connect(self.open_folder)
        self.github_btn.clicked.connect(self.report_to_github)
        self.close_btn.clicked.connect(self.accept)
        self.last_url = None
        self._show_saved()
        if manual:
            self.description.setFocus()

    # ---------------------------------------------------------------- actions
    def _show_saved(self, extra=""):
        where = rp.display_dir(os.path.dirname(self.md_path)) if self.md_path else "(could not save)"
        name = os.path.basename(self.zip_path or self.md_path or "")
        self.saved.setText(f"Report saved: {_esc(name)} in {_esc(where)}" + extra)

    def _sync(self):
        """Put the description into the report and re-save it."""
        d = self.description.toPlainText().strip()
        an = rp.Anonymizer()
        d = an.text(d)
        if d != self.report.get("description", ""):
            self.report["description"] = d
            try:
                self.md_path, self.zip_path = rp.save_report(self.report, os.path.dirname(self.md_path),
                                                             self.session_log)
            except Exception:  # noqa: BLE001
                log.error("could not re-save report", exc_info=True)
        if self.details.isVisible():
            self.details.setPlainText(rp.render_markdown(self.report))

    def text(self):
        self._sync()
        return rp.render_markdown(self.report)

    def _toggle_details(self, on):
        self.details_btn.setText("Details \u25be" if on else "Details \u25b8")
        if on:
            self.details.setPlainText(self.text())
        self.details.setVisible(on)
        self.adjustSize()

    def copy_report(self):
        QGuiApplication.clipboard().setText(self.text())
        self._show_saved(" - copied to the clipboard.")

    def open_folder(self):
        self._sync()
        d = os.path.dirname(self.md_path) if self.md_path else rp.report_dir()
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def report_to_github(self):
        self._sync()
        url = rp.issue_url(self.report, zip_name=os.path.basename(self.zip_path or ""))
        self.last_url = url
        if not url:
            return
        try:
            ok, _how = open_issue(url, os.path.dirname(self.md_path), self.report["id"])
        except Exception:  # noqa: BLE001
            log.error("could not open the browser", exc_info=True); ok = False
        if ok:
            self.note.setText(
                "Your browser opened GitHub with the report filled in. Check it"
                + (f", drag <b>{_esc(os.path.basename(self.zip_path))}</b> in" if self.zip_path else "")
                + " and press <i>Submit</i>. If the page does not load (no internet), nothing was sent - "
                  "the report stays saved and you can send it later.")
        else:
            QMessageBox.information(
                self, "Report to GitHub",
                "No web browser could be opened.\n\nThe report is saved on this computer:\n"
                f"{self.zip_path or self.md_path}\n\nYou can send it later: on a computer with internet open\n"
                f"{rp.issues_url()}\nand drag the .zip into the new issue (or use Copy report and paste it).")


# ---------------------------------------------------------------- reporter
class ErrorReporter(QObject):
    """Builds/saves reports and shows ErrorDialog on the GUI thread."""
    request = Signal(object)

    def __init__(self, window=None):
        super().__init__(window)
        self.window = window
        self._busy = threading.local()
        self._qt_seen = set()
        self.request.connect(self._show, Qt.QueuedConnection)
        self.dialog = None
        self.last = None            # (report, md, zip) of the most recent report

    # data sources ------------------------------------------------------------
    def _project(self):
        return getattr(self.window, "project", None)

    def _console(self, mark=0, n=rp.LOG_LINES):
        c = getattr(self.window, "console", None)
        if c is not None and hasattr(c, "lines_since"):
            return c.lines_since(mark, n)
        return []

    def _session_log(self):
        try:
            from . import diag
            return diag.log_path()
        except Exception:
            return None

    def build(self, title, message="", *, mark=None, log_lines=None, **kw):
        """Build + save a report. Returns (report, md_path, zip_path)."""
        if log_lines is None:
            log_lines = self._console(mark or 0) if mark is not None else []
            if not log_lines:
                sl = self._session_log()
                log_lines = rp.tail_lines(sl) if sl else self._console(0)
        r = rp.build_report(title, message, project=self._project(), log_lines=log_lines, **kw)
        md = zp = None
        try:
            md, zp = rp.save_report(r, session_log=self._session_log())
            log.info("error report saved: %s", zp)
        except Exception:  # noqa: BLE001 - still show the dialog
            log.error("could not save the error report", exc_info=True)
        self.last = (r, md, zp)
        return r, md, zp

    # entry points ------------------------------------------------------------
    def show_error(self, title, message="", *, manual=False, **kw):
        """GUI thread: build, save and show the dialog (modal). Returns the dialog."""
        if getattr(self._busy, "on", False):
            QMessageBox.critical(self.window, title, message)
            return None
        self._busy.on = True
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                r, md, zp = self.build(title, message, **kw)
            finally:
                QApplication.restoreOverrideCursor()
            return self._open(r, md, zp, manual)
        except Exception:  # noqa: BLE001 - fall back to a plain box
            log.error("error dialog failed", exc_info=True)
            QMessageBox.critical(self.window, title, message)
            return None
        finally:
            self._busy.on = False

    def _open(self, r, md, zp, manual=False):
        if self.dialog is not None and self.dialog.isVisible():
            sb = getattr(self.window, "statusBar", None)
            if sb:
                sb().showMessage(f"Another error was reported: {r['title']} ({r['id']})", 15000)
            return None
        dlg = ErrorDialog(r, md, zp, self.window, session_log=self._session_log(), manual=manual)
        self.dialog = dlg
        dlg.exec()
        return dlg

    def _show(self, payload):
        r, md, zp = payload
        self._open(r, md, zp)

    def handle(self, kind, info, thread_name=""):
        """diag error handler: any thread."""
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            if kind == "qt":
                level, text = info
                key = text[:200]
                if key in self._qt_seen or len(self._qt_seen) >= 5:
                    return
                self._qt_seen.add(key)
                r, md, zp = self.build(f"Qt {level} message", text, kind=f"qt-{level}")
                return                       # saved silently (Qt warnings are not user errors)
            etype, value, tb = info
            tbtext = "".join(traceback.format_exception(etype, value, tb))
            where = "" if kind == "exception" else f" in thread '{thread_name}'"
            title = "Out of memory" if issubclass(etype, MemoryError) else "Unexpected error" + where
            msg = f"{etype.__name__}: {value}"
            if threading.current_thread() is threading.main_thread():
                self._busy.on = False             # show_error has its own re-entry guard
                self.show_error(title, msg, kind=kind, traceback_text=tbtext)
            else:
                payload = self.build(title, msg, kind=kind, traceback_text=tbtext)
                self.request.emit(payload)
        finally:
            self._busy.on = False


def install(window):
    """main.py: route uncaught exceptions / Qt critical messages to reports."""
    from . import diag
    rep = getattr(window, "reporter", None) or ErrorReporter(window)
    window.reporter = rep
    diag.set_error_handler(rep.handle)
    if sys.excepthook is not diag._excepthook:
        sys.excepthook = diag._excepthook
    threading.excepthook = diag._thread_excepthook
    return rep
