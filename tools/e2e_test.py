"""End-to-end GUI run: add photos, Align Photos (Fast), optionally dense + mesh, save, screenshot.
    python tools/e2e_test.py <images_dir> <out_project.pmsx> [accuracy] [dense]
Requires COLMAP (PATH, Preferences or tools/colmap). PM_GPU=1 enables GPU."""
import glob, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PySide6.QtCore import QTimer, QSettings, QEventLoop
from PySide6.QtWidgets import QApplication, QMessageBox


def wait(w):
    if w.runner is not None and getattr(w.runner, "busy", False):
        loop = QEventLoop()
        w.runner.finished.connect(lambda *a: QTimer.singleShot(0, loop.quit))
        loop.exec()


def main():
    from photomesh.theme import apply_dark_theme
    from photomesh import mainwindow, dialogs
    app = QApplication(sys.argv)
    app.setOrganizationName("PhotoMesh"); app.setApplicationName("PhotoMesh Studio")
    QSettings().setValue("gpu/use_gpu", os.environ.get("PM_GPU", "0") == "1")
    apply_dark_theme(app)
    acc = sys.argv[3] if len(sys.argv) > 3 else "Medium"
    dense = len(sys.argv) > 4 and sys.argv[4] == "dense"
    a0, p0, m0 = (dialogs.AlignPhotosDialog.settings, dialogs.BuildPointCloudDialog.settings,
                  dialogs.BuildModelDialog.settings)
    for cls in (dialogs.AlignPhotosDialog, dialogs.BuildPointCloudDialog, dialogs.BuildModelDialog):
        cls.exec = lambda self: True
    dialogs.ProgressDialog.exec = lambda self: None
    dialogs.AlignPhotosDialog.settings = lambda self: dict(a0(self), accuracy=acc)
    dialogs.BuildPointCloudDialog.settings = lambda self: dict(p0(self), quality="Medium")
    dialogs.BuildModelDialog.settings = lambda self: dict(m0(self), source="Point cloud", face_count="Medium")
    QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING:", a[2], flush=True))
    w = mainwindow.MainWindow(); w.resize(1600, 950); w.show()
    out = os.path.abspath(sys.argv[2])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    w.project.path = out   # results go to <project>.files
    w.add_photos(sorted(glob.glob(os.path.join(sys.argv[1], "*.[jJ][pP][gG]"))))
    w.align_photos(); wait(w)
    ch = w.chunk
    print(f"aligned {ch.aligned_count()}/{len(ch.cameras)}, tie points {ch.tie_point_count}", flush=True)
    if dense:
        w.build_point_cloud(); wait(w)
        print(f"point cloud {ch.point_count}", flush=True)
        w.build_model(); wait(w)
        print(f"model faces {ch.face_count}", flush=True)
    w.project.save(out)
    print("saved", out, flush=True)
    w.refresh_all(); w.reload_view()
    w.docks["Photos"].raise_()
    def shot():
        w.grab().save(os.path.join(ROOT, "screenshot.png")); print("saved screenshot", flush=True)
        w.project.modified = False; app.quit()
    QTimer.singleShot(2000, shot)
    app.exec()


if __name__ == "__main__":
    main()
