#!/usr/bin/env python3
"""PhotoMesh Studio - entry point."""
import multiprocessing
import sys


def main():
    # session log + crash/hang watchdog first, so even import errors are recorded
    # (the slower system-info line is written once the splash is up)
    from photomesh import diag
    diag.setup(log_system=False)
    from PySide6.QtWidgets import QApplication
    from photomesh import APP_NAME, appicon

    from PySide6.QtCore import Qt, QCoreApplication
    appicon.set_app_user_model_id()      # Windows: own taskbar button + icon (before any window)
    # one GL share group: a recreated / second GL view can use the shader programs
    # and buffers built in the first one (pyqtgraph compiles its shaders once)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setOrganizationName("PhotoMesh")
    app.setApplicationName(APP_NAME)
    appicon.install(app)
    # splash first, then the heavy imports (NumPy, OpenGL / pyqtgraph, main window)
    from photomesh import splash as splash_mod
    splash = splash_mod.show()
    from photomesh.theme import apply_dark_theme
    apply_dark_theme(app)
    splash.step("Loading numerical libraries")
    import numpy  # noqa: F401
    diag.log_system_info()
    splash.step("Loading 3D view (OpenGL)")
    import photomesh.views  # noqa: F401
    splash.step("Loading main window")
    from photomesh.mainwindow import MainWindow
    splash.step("Creating main window")
    w = MainWindow()
    appicon.install(app, w)
    project = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".pmsx") else None
    w.show()
    splash.finish(w)
    diag.start_watchdog(app)
    from photomesh import report_ui
    report_ui.install(w)          # crashes / failed steps -> error report + dialog
    if project:
        w.open_project(project)
    from PySide6.QtCore import QSettings, QTimer
    from photomesh import APP_VERSION, shortcut_icon
    QTimer.singleShot(3000, lambda: shortcut_icon.maybe_fix(QSettings(), APP_VERSION))
    rc = app.exec()
    diag.stop_watchdog()
    diag.log.info("exit code %s", rc)
    sys.exit(rc)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # child processes on Windows
    main()
