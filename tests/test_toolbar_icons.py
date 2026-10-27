"""Toolbar icons 30 % larger, drawn crisply per device pixel ratio, no
QStyle bitmaps, no toolbar overflow at 1920 px / 150 %; Photo tab marker
flags follow Show Markers / Show Marker Labels."""
import pytest
from PySide6.QtCore import QSize


def _opaque_bbox(img):
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 40:
                xs.append(x); ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def test_icon_sizes_are_30_percent_larger():
    from photomesh import icons, toolbuttons
    assert icons.TOOLBAR_ICON == round(16 * 1.3) and icons.PANE_ICON == round(16 * 1.3)
    assert icons.SELECT_ICON == round(16 * 1.3) and icons.MODE_ICON == round(20 * 1.3)
    assert toolbuttons.ARROW_W == round(16 * 1.3)


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_icons_are_rendered_at_device_pixels_not_upscaled(qapp, dpr):
    from photomesh.icons import layer_icon, render_image
    for key in ("mesh", "file_save", "undo", "redo", "preferences", "transparency", "markers"):
        pm = layer_icon(key).pixmap(QSize(21, 21), dpr)
        assert pm.width() == round(21 * dpr) and abs(pm.devicePixelRatio() - dpr) < 1e-6, key
        # pixel-identical to drawing the vector directly at that size (no resampling)
        assert pm.toImage().convertToFormat(render_image(key, 1).format()) == render_image(key, round(21 * dpr)), key


def test_icons_fill_the_box_and_have_contrast(qapp):
    from PySide6.QtGui import QColor
    from photomesh.icons import render_image
    bg = QColor(58, 58, 61)                                    # toolbar background (theme)
    for key in ("file_new", "file_open", "file_save", "undo", "redo", "preferences", "tie", "cloud", "mesh",
                "grid", "cameras", "markers", "zoom_in", "navigation"):
        img = render_image(key, 32)
        x0, y0, x1, y1 = _opaque_bbox(img)
        assert max(x1 - x0, y1 - y0) >= 20 and min(x1 - x0, y1 - y0) >= 12, key   # uses most of the box
        best = 0
        for y in range(32):
            for x in range(32):
                c = img.pixelColor(x, y)
                if c.alpha() > 200:
                    l = 0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue()
                    best = max(best, abs(l - (0.3 * bg.red() + 0.59 * bg.green() + 0.11 * bg.blue())))
        assert best > 70, key


def test_thin_strokes_are_kept_legible_when_small(qapp):
    from photomesh.icons import render_image
    # grid has 1.5-unit lines: at 21 px they would be < 1 px without the minimum width
    img = render_image("grid", 21, outline=False)
    alpha = sum(img.pixelColor(x, y).alpha() for y in range(21) for x in range(21))
    assert alpha / 255.0 > 21 * 21 * 0.12


def test_disabled_icon_is_grey(qapp):
    from PySide6.QtGui import QIcon
    from photomesh.icons import layer_icon
    img = layer_icon("file_save").pixmap(QSize(21, 21), 1.0, QIcon.Disabled).toImage()
    for y in range(21):
        for x in range(21):
            c = img.pixelColor(x, y)
            if c.alpha() > 30:
                assert abs(c.red() - c.green()) <= 2 and abs(c.green() - c.blue()) <= 2


def test_toolbar_uses_only_painter_icons(window):
    from photomesh.icons import PainterIconEngine, TOOLBAR_ICON
    tb = window.main_toolbar
    for a in tb.actions():
        if not a.isSeparator() and not a.icon().isNull():
            # crisp: every toolbar action icon is redrawn at the device pixel size
            pm = a.icon().pixmap(QSize(TOOLBAR_ICON, TOOLBAR_ICON), 1.5)
            assert pm.width() == round(TOOLBAR_ICON * 1.5), a.text()
    for n in ("New", "Open...", "Save", "Undo", "Redo", "Preferences..."):
        assert QSize(21, 21) in window.actions[n].icon().availableSizes(), n   # ours, not a QStyle bitmap
    for b in window.mode_buttons:
        assert b.height() <= 40
    assert isinstance(PainterIconEngine("mesh").clone(), PainterIconEngine)


_FIT_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
from PySide6.QtWidgets import QApplication, QToolButton
app = QApplication([]); app.setOrganizationName("PhotoMeshFitTest"); app.setApplicationName("PhotoMeshFitTest")
from photomesh.theme import apply_dark_theme
apply_dark_theme(app)                                       # as main.py
from photomesh.mainwindow import MainWindow
w = MainWindow(); w.resize(1280, 800); w.show()
for _ in range(10):
    app.processEvents()
tb = w.main_toolbar
ext = [b for b in tb.findChildren(QToolButton) if b.objectName() == "qt_toolbar_ext_button"]
print("FIT" + json.dumps({"hint": tb.sizeHint().width(), "width": tb.width(), "dpr": w.devicePixelRatioF(),
                          "overflow": bool(ext and ext[0].isVisible())}), flush=True)
os._exit(0)
"""


def test_main_toolbar_fits_1920px_at_150_percent(tmp_path):
    """1920 px at 150 % scaling = 1280 logical px: the themed main toolbar
    (as started by main.py) must fit without the overflow (>>) button."""
    import json, os, subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="1.5")
    if os.name == "nt":
        env.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    sc = tmp_path / "fit.py"; sc.write_text(_FIT_SCRIPT, encoding="utf-8")
    out = subprocess.run([sys.executable, str(sc), root], env=env, capture_output=True, text=True, timeout=120)
    line = [l for l in out.stdout.splitlines() if l.startswith("FIT")]
    assert line, out.stdout + out.stderr
    r = json.loads(line[0][3:])
    assert abs(r["dpr"] - 1.5) < 1e-6
    assert r["hint"] <= 1280 - 16 and not r["overflow"], r


def _photo_with_markers(window):
    from PySide6.QtGui import QImage, QColor
    import tempfile, os
    d = tempfile.mkdtemp()
    p = os.path.join(d, "p.png")
    im = QImage(200, 150, QImage.Format_RGB32); im.fill(QColor(90, 90, 90)); im.save(p)
    pv = window.photo_view
    pv.open(p, "p")
    pv.set_markers([("A1", 50, 40, "pinned"), ("B2", 120, 90, "predicted"), ("C3", 150, 30, "placed")], "A1")
    return pv


def _texts(pv):
    from PySide6.QtWidgets import QGraphicsSimpleTextItem
    return [it.text() for it in pv._overlay if isinstance(it, QGraphicsSimpleTextItem)]


def test_photo_markers_follow_show_markers_and_labels(window):
    pv = _photo_with_markers(window)
    assert window.layer_action("markers").isChecked() and window.layer_action("marker_labels").isChecked()
    n_all = len(pv._overlay)
    assert n_all and len(_texts(pv)) == 3
    scene_items = set(pv.view.scene().items())
    assert all(it in scene_items for it in pv._overlay)
    # Show Marker Labels off: flags stay, labels go
    window.set_layer_visible("marker_labels", False)
    assert pv.vis["marker_labels"] is False and _texts(pv) == [] and 0 < len(pv._overlay) < n_all
    assert window.ortho_view.vis["marker_labels"] is False
    # Show Markers off: nothing at all, and hidden flags can't be picked
    window.set_layer_visible("markers", False)
    assert pv.vis["markers"] is False and pv._overlay == []
    assert pv.markers_near(50, 40) == []
    assert window.ortho_view.vis["markers"] is False and window.model_view.show["markers"] is False
    # markers set while hidden stay hidden (e.g. switching photos)
    pv.set_markers([("A1", 50, 40, "pinned")], "A1")
    assert pv._overlay == []
    # both back on: everything is redrawn, labels included
    window.set_layer_visible("markers", True); window.set_layer_visible("marker_labels", True)
    pv.set_markers([("A1", 50, 40, "pinned"), ("B2", 120, 90, "predicted"), ("C3", 150, 30, "placed")], "A1")
    assert len(pv._overlay) == n_all and len(_texts(pv)) == 3
    assert pv.markers_near(50, 40)


def test_photo_markers_hidden_from_the_toolbar_toggle(window):
    """The toolbar / menu QAction is the same sink as the Workspace checkbox."""
    pv = _photo_with_markers(window)
    window.layer_action("markers").trigger()                  # toolbar button click
    assert pv._overlay == []
    window.layer_action("markers").trigger()
    assert pv._overlay and len(_texts(pv)) == 3
