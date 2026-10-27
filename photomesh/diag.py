"""Diagnostics: a per-session log file plus a hang/crash watchdog.

Log folder: %LOCALAPPDATA%\\PhotoMeshStudio\\logs on Windows (~/.local/state/
PhotoMeshStudio/logs elsewhere; override with PHOTOMESH_LOG_DIR).
Help > Open Log Folder opens it, so a user can send the newest log.

* Python logging, warnings, uncaught exceptions and Qt messages go to the log.
* pythonw has no stdout/stderr (None); they are redirected to the log, so
  print()/tracebacks are not lost.
* faulthandler writes a native traceback of every thread on a hard crash.
* Hang watchdog: faulthandler.dump_traceback_later(HANG_S, repeat=True) runs
  in a C thread (works even while the GUI thread holds the GIL inside C code)
  and is re-armed by a 1 s Qt timer on the GUI thread. If the GUI thread stops
  processing events for HANG_S seconds, the stack of every thread is written
  to the log (and again every HANG_S seconds while the hang lasts).
"""
from __future__ import annotations

import datetime
import faulthandler
import glob
import logging
import os
import platform
import sys
import time

HANG_S = 20
KEEP_LOGS = 30
log = logging.getLogger("photomesh")
_state = {"file": None, "path": None, "timer": None, "armed": False, "handler": None}


def set_error_handler(fn):
    """fn(kind, info, thread_name): called for uncaught exceptions ("exception",
    exc_info), exceptions in threads ("thread", exc_info) and Qt critical/fatal
    messages ("qt", (level, text)). Used by report_ui to build error reports.
    None restores the plain message box."""
    _state["handler"] = fn


def _dispatch(kind, info, thread_name):
    h = _state.get("handler")
    if h is None:
        return False
    try:
        h(kind, info, thread_name)
        return True
    except Exception:  # noqa: BLE001 - the handler must never take the app down
        log.error("error report handler failed", exc_info=True)
        return False


def log_dir():
    d = os.environ.get("PHOTOMESH_LOG_DIR")
    if not d:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
            d = os.path.join(base, "PhotoMeshStudio", "logs")
        else:
            d = os.path.join(os.path.expanduser("~/.local/state"), "PhotoMeshStudio", "logs")
    return d


def log_path():
    return _state["path"]


def _prune(d):
    logs = sorted(glob.glob(os.path.join(d, "photomesh_*.log")), key=os.path.getmtime)
    for p in logs[:-KEEP_LOGS]:
        try:
            os.remove(p)
        except OSError:
            pass


def setup(log_system=True):
    """Open the session log (idempotent). Returns its path (None if impossible).
    log_system=False skips the system-info line (it imports NumPy / Pillow and
    queries the OS, ~0.2 s or more on Windows); main() logs it with
    log_system_info() once the splash is visible."""
    if _state["file"] is not None:
        return _state["path"]
    d = log_dir()
    try:
        os.makedirs(d, exist_ok=True)
        _prune(d)
        name = f"photomesh_{datetime.datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log"
        path = os.path.join(d, name)
        f = open(path, "a", encoding="utf-8", buffering=1, errors="backslashreplace")
    except OSError:
        return None
    _state.update(file=f, path=path)
    h = logging.StreamHandler(f)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    try:
        faulthandler.enable(file=f, all_threads=True)
    except Exception:
        pass
    sys.excepthook = _excepthook
    try:
        import threading
        threading.excepthook = _thread_excepthook
    except Exception:
        pass
    log.info("PhotoMesh Studio session start, log %s", path)
    if log_system:
        log_system_info()
    return path


def log_system_info():
    try:
        log.info("system: %s", system_info())
    except Exception:  # noqa: BLE001 - diagnostics must never stop the app
        log.error("system info failed", exc_info=True)


def system_info():
    info = {"python": sys.version.split()[0], "executable": sys.executable, "platform": platform.platform(),
            "machine": platform.machine(), "cpus": os.cpu_count(), "cwd": os.getcwd(),
            "app": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fs_encoding": sys.getfilesystemencoding(), "locale": _locale()}
    try:
        import PySide6
        from PySide6 import QtCore
        info["pyside6"] = PySide6.__version__; info["qt"] = QtCore.qVersion()
    except Exception as e:
        info["pyside6"] = f"error: {e}"
    try:
        import PIL
        info["pillow"] = PIL.__version__
    except Exception as e:
        info["pillow"] = f"MISSING: {e}"
    try:
        import numpy
        info["numpy"] = numpy.__version__
    except Exception as e:
        info["numpy"] = f"MISSING: {e}"
    try:
        from .colmap import nvidia_driver_present
        info["nvidia_driver"] = nvidia_driver_present()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                            ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            m = MS(); m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            info["ram_gb"] = round(m.total / 2 ** 30, 1); info["ram_free_gb"] = round(m.avail / 2 ** 30, 1)
        except Exception:
            pass
    return info


def _locale():
    try:
        import locale
        return locale.getlocale()
    except Exception:
        return None


def _thread_excepthook(a):
    if a.exc_type is SystemExit:
        return
    name = a.thread.name if a.thread is not None else "?"
    log.error("Uncaught exception in thread %s", name, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    _dispatch("thread", (a.exc_type, a.exc_value, a.exc_traceback), name)


def _excepthook(etype, value, tb):
    """Uncaught exceptions, also in Qt slots and QThread.run (PySide6 calls
    sys.excepthook in the thread that raised)."""
    import threading
    log.error("Uncaught exception", exc_info=(etype, value, tb))
    th = threading.current_thread()
    if _dispatch("exception" if th is threading.main_thread() else "thread", (etype, value, tb), th.name):
        return
    if th is not threading.main_thread():
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "PhotoMesh Studio",
                                 f"Unexpected error:\n{value}\n\nDetails were written to the log file:\n{log_path()}")
    except Exception:
        pass


def _qt_message(mode, ctx, msg):
    lvl = {0: logging.DEBUG, 1: logging.WARNING, 2: logging.ERROR, 3: logging.CRITICAL, 4: logging.INFO}
    try:
        m = int(mode.value) if hasattr(mode, "value") else int(mode)
    except Exception:
        m = 4
    try:
        log.log(lvl.get(m, logging.INFO), "Qt: %s", msg)
    except Exception:
        pass
    if m in (2, 3):                                  # QtCriticalMsg, QtFatalMsg
        import threading
        _dispatch("qt", ("fatal" if m == 3 else "critical", str(msg)), threading.current_thread().name)


def _rearm():
    f = _state["file"]
    if f is None:
        return
    try:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(HANG_S, repeat=True, file=f)
        _state["armed"] = True
    except Exception:
        pass


def start_watchdog(app=None, hang_s=None):
    """Arm the hang watchdog (needs a running Qt event loop to re-arm it)."""
    global HANG_S
    if hang_s:
        HANG_S = hang_s
    if _state["file"] is None:
        return
    from PySide6.QtCore import QTimer, qInstallMessageHandler
    try:
        qInstallMessageHandler(_qt_message)
    except Exception:
        pass
    t = QTimer(); t.setInterval(1000); t.timeout.connect(_rearm); t.start()
    _state["timer"] = t
    _rearm()
    log.info("hang watchdog armed: a traceback of all threads is logged if the UI is blocked for %d s", HANG_S)


def stop_watchdog():
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    if _state["timer"] is not None:
        _state["timer"].stop(); _state["timer"] = None
    _state["armed"] = False


class Timed:
    """with Timed("Add Photos: header scan", n=641): ...  -> logs start / end + duration."""
    def __init__(self, what, **kw):
        self.what, self.kw = what, kw

    def __enter__(self):
        self.t = time.perf_counter()
        log.info("%s: start %s", self.what, self.kw or "")
        return self

    def __exit__(self, et, ev, tb):
        dt = time.perf_counter() - self.t
        if et is None:
            log.info("%s: done in %.2f s", self.what, dt)
        else:
            log.error("%s: failed after %.2f s", self.what, dt, exc_info=(et, ev, tb))
        return False
