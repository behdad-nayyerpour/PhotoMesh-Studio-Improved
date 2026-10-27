"""Smoke test + screenshot.

    QT_QPA_PLATFORM=offscreen python tools/screenshot.py [images_dir | project.pmsx]
(With offscreen the 3D view is disabled; run under a real/virtual X display to
capture the OpenGL viewport.)
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def main():
    from photomesh.theme import apply_dark_theme
    from photomesh.mainwindow import MainWindow
    app = QApplication(sys.argv)
    app.setOrganizationName("PhotoMesh"); app.setApplicationName("PhotoMesh Studio")
    apply_dark_theme(app)
    w = MainWindow(); w.show()
    out = os.environ.get("PM_SHOT", os.path.join(ROOT, "screenshot.png"))
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg.endswith(".pmsx"):
        w.project.modified = False
        w.open_project(arg)
    elif arg:
        w.add_photos(sorted(glob.glob(os.path.join(arg, "*.[jJ][pP][gG]"))))
    print("top-level menus:", [a.text().replace("&", "") for a in w.menuBar().actions()], flush=True)
    print("actions:", len({id(a) for a in w.actions.values()}), flush=True)

    def shot():
        w.grab().save(out)
        print("saved", out, flush=True)
        w.project.modified = False
        app.quit()
    QTimer.singleShot(1500, shot)
    app.exec()


if __name__ == "__main__":
    main()
