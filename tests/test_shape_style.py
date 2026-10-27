"""Shape style settings (per layer / per shape, saved in the project) and the
transparent view (surfaces see-through, photo / ortho dimmed, hidden parts dashed)."""
import os

import numpy as np
import pytest

from photomesh import shape_style as SS
from photomesh import shapes as S
from photomesh import colmap_io as cio

from test_shapes import _gui_chunk, _site_files, _rows, OFF


def _set():
    ss = S.new_set()
    a = S.add_layer(ss, "A", color=(10, 20, 30), width=3.0); b = S.add_layer(ss, "B", color=(200, 0, 0))
    ss["shapes"].append(S.make_shape(a, "point", [[[0, 0, 0]]], {"id": 7, "kind": "gcp"}, "P1"))
    ss["shapes"].append(S.make_shape(a, "polyline", [[[0, 0, 0], [10, 0, 0]]], {"id": 8}, "", color=(0, 255, 0)))
    ss["shapes"].append(S.make_shape(b, "polygon", [S._close([[0, 0, 0], [4, 0, 0], [4, 4, 0]])], {"id": 9}, "poly"))
    return ss


def test_resolve_precedence_defaults_layer_shape():
    ss = _set()
    st = SS.resolve(ss, 0, {})
    assert st["line_color"] == [10, 20, 30] and st["line_width"] == 3.0          # layer colour / imported width
    assert st["fill_color"] == st["label_color"] == [10, 20, 30] and st["on_top"] and st["point_symbol"] == "ring"
    assert SS.resolve(ss, 1, {})["line_color"] == [0, 255, 0]                     # colour from the file
    info = SS.set_layer_style({}, "A", {"line_color": [1, 2, 3], "line_style": "dashed", "fill_opacity": 3})
    assert info["styles"]["A"] == {"line_color": [1, 2, 3], "line_style": "dashed", "fill_opacity": 1.0}
    assert SS.resolve(ss, 1, info)["line_color"] == [1, 2, 3]                   # explicit layer colour wins
    full = SS.layer_style(info, "A")
    info = SS.set_shape_style(info, 0, dict(full, point_symbol="square", label_size=14), full)
    assert info["shape_styles"]["0"] == {"point_symbol": "square", "label_size": 14.0}
    st = SS.resolve(ss, 0, info)
    assert st["point_symbol"] == "square" and st["line_style"] == "dashed" and st["line_color"] == [1, 2, 3]
    assert SS.resolve(ss, 2, info)["line_style"] == "solid"                      # other layer untouched
    hl = SS.resolve(ss, 0, info, highlight=True)
    assert hl["line_color"] == [255, 255, 255] and hl["line_width"] == st["line_width"] + 2
    assert SS.set_shape_style(info, 0, None)["shape_styles"] == {}
    assert SS.set_layer_style(info, "A", None)["styles"] == {}
    assert SS.clean({"line_width": 99, "point_symbol": "star", "bogus": 1, "label_color": (300, -1, 5)}) == \
        {"line_width": 20.0, "point_symbol": "ring", "label_color": [255, 0, 5]}


def test_label_fields_and_attribute_list():
    ss = _set()
    assert SS.label_text(ss["shapes"][0], 0, "") == "P1"
    assert SS.label_text(ss["shapes"][0], 0, "id") == "7"
    assert SS.label_text(ss["shapes"][1], 1, "kind") == ""
    assert SS.label_text(ss["shapes"][2], 2, SS.LABEL_FIELD_NAME) == S.shape_name(ss["shapes"][2], 2)
    assert SS.attribute_fields(ss, 0) == ["id", "kind"] and SS.attribute_fields(ss) == ["id", "kind"]


def test_dash_segments_and_layer_removal_remap():
    D = SS.dash_segments(np.array([[0.0, 0, 0], [10.0, 0, 0]]), 1.0, 1.0)
    assert len(D) == 10 and np.allclose(D[0], [0, 0, 0]) and np.allclose(D[1], [1, 0, 0])
    assert np.allclose(np.linalg.norm(D[1::2] - D[0::2], axis=1), 1.0)
    ss = _set()
    info = {"styles": {"A": {"line_width": 5.0}, "B": {"line_width": 6.0}}, "shape_styles": {"2": {"label": False}}}
    out = SS.remap_after_layer_removal(info, ss, 0)
    assert out["shape_styles"] == {"0": {"label": False}} and list(out["styles"]) == ["B"]


def test_overlay_fill_symbols_label_style_and_depth(qapp):
    from PySide6.QtGui import QImage, QPainter, QColor
    from photomesh.overlay3d import Overlay
    ov = Overlay(); ov.legend = False
    mvp = np.diag([0.01, 0.01, 0.001, 1.0])
    ov.shape_fills = [([np.array([[-50, -30, 0], [50, -30, 0], [50, 30, 0], [-50, 30, 0], [-50, -30, 0.0]])],
                       QColor(0, 0, 255, 128))]
    ov.shape_points = [(np.array([[60.0, 0, 0], [80.0, 0, 0]]),
                        {"color": QColor(255, 0, 0), "size": 12, "symbol": "square", "on_top": False, "width": 2})]
    ov.shape_labels = [(np.array([[-80.0, 30, 0]]), ["L"], QColor(0, 255, 0),
                        {"label_size": 14, "label_bg": False, "label_bg_color": [0, 0, 0]})]
    depth = lambda xy: np.where(xy[:, 0] > 170, 0.0, 1.0)                  # surface in front of the 2nd point
    items = ov.items(mvp, 200, 100, depth_fn=depth)
    pts = [i for i in items if i[0] == "shape_pt"]
    assert [i[4] for i in pts] == [False, True]                             # depth-tested style: 2nd hidden
    ov.transparent = True
    assert [i[4] for i in ov.items(mvp, 200, 100, depth_fn=depth) if i[0] == "shape_pt"] == [False, False]
    ov.transparent = False
    img = QImage(200, 100, QImage.Format_RGB32); img.fill(QColor(255, 255, 255))
    p = QPainter(img); rects = ov.paint(p, items, 200, 100); p.end()
    c = img.pixelColor(100, 50)
    assert c.blue() > 200 and c.red() < 160                                  # translucent blue fill
    s = img.pixelColor(160, 50 - 5)                                          # square outline (filled) at x=160
    assert s.red() > 150 and s.green() < 120, s.name()
    (_k, t, r), = rects
    assert t == "L" and r.height() > 18                                      # 14 pt label box, no background
    bg = img.pixelColor(int(r.left()) + 1, int(r.top()) + 1)
    assert bg.red() > 200 and bg.green() > 200


def test_project_hidden_parts_for_the_transparent_view():
    o3d = pytest.importorskip("open3d")
    cam = cio.Camera(1, 1000, 800, np.array([1000.0, 1000.0, 500.0, 400.0]))
    sc = o3d.t.geometry.RaycastingScene()
    V = np.array([[-1, -1, 5], [1, -1, 5], [1, 1, 5], [-1, 1, 5]], np.float32)
    sc.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(np.array([[0, 1, 2], [0, 2, 3]], np.uint32)))
    vis, hid = S.project_polylines([np.array([[-4.0, 0, 10], [4.0, 0, 10]])], cam, np.eye(3), np.zeros(3),
                                   scene=sc, hidden=True)
    assert len(vis) == 2 and len(hid) == 1
    assert np.all(np.abs(hid[0][:, 0] - 500) <= 201)    # behind the wall (x +-1 at z 5 -> +-2 at z 10)
    uv, ok, uvh, okh = S.project_points(np.array([[0.0, 0, 10], [3.0, 0, 10]]), cam, np.eye(3), np.zeros(3),
                                        scene=sc, hidden=True)
    assert list(ok) == [1] and list(okh) == [0] and np.allclose(uvh[0], [500, 400])
    F = S.project_ring_fill(np.array([[-1.0, -1, 10], [1, -1, 10], [1, 1, 10], [-1.0, -1, 10]]), cam, np.eye(3),
                            np.zeros(3))
    assert np.allclose(F[1], [600, 300])
    assert S.project_ring_fill(np.array([[0, 0, -1.0], [1, 0, 10], [0, 1, 10]]), cam, np.eye(3), np.zeros(3)) is None


def _gl_opts(item):
    return item._GLGraphicsItem__glOpts


def test_surface_gl_state_for_transparency(qapp):
    from OpenGL import GL
    from pyqtgraph.opengl.GLGraphicsItem import GLGraphicsItem
    from photomesh.views import ModelView, TRANSPARENT_DEPTH_VALUE, _with_alpha
    mv = ModelView()
    it = GLGraphicsItem(); it.alpha = 1.0
    mv.transparent, mv.surface_opacity = True, 0.35
    mv._style_surface(it)
    o = _gl_opts(it)
    assert o[GL.GL_BLEND] is True and o[GL.GL_DEPTH_TEST] is True and o["glDepthMask"] == (GL.GL_FALSE,)
    assert it.depthValue() == TRANSPARENT_DEPTH_VALUE and it.alpha == pytest.approx(0.35)
    assert 0 < TRANSPARENT_DEPTH_VALUE < 8                    # after opaque items, before cameras / shapes
    mv.transparent = False; mv._style_surface(it)
    o = _gl_opts(it)
    assert o[GL.GL_BLEND] is False and o["glDepthMask"] == (GL.GL_TRUE,) and it.depthValue() == 0 and it.alpha == 1
    C = _with_alpha(np.array([[255, 0, 0], [0, 255, 0]], np.uint8), 0.4)
    assert C.shape == (2, 4) and np.allclose(C[:, 3], 0.4) and C[0, 0] == 1.0


def test_textured_shader_has_alpha_uniform():
    from photomesh import glitems
    assert "uniform float u_alpha" in glitems.FRAG and "gl_FragColor.a = u_alpha" in glitems.FRAG
    assert glitems._BaseItem.alpha == 1.0


def _with_shapes(window, tmp_path):
    ch = _gui_chunk(window, tmp_path)
    gcp, site, _local = _site_files(tmp_path)
    window.import_shapes(gcp, {"layer": "GCP", "z_mode": "keep"})
    window.import_shapes(site, {"layer": "Site", "z_mode": "drape_missing", "surface": "DEM"})
    return ch


def test_gui_layer_style_applied_saved_reloaded(window, no_dialogs, tmp_path):
    ch = _with_shapes(window, tmp_path)
    window.project.modified = False
    window.set_layer_style("Site", {"line_color": [0, 0, 255], "line_width": 4, "line_style": "dashed",
                                    "fill_color": [255, 255, 0], "fill_opacity": 0.5, "label_field": "name",
                                    "on_top": False})
    window.set_layer_style("GCP", {"point_symbol": "diamond", "point_size": 20, "label_size": 12,
                                   "label_bg": False, "label_color": [255, 0, 255]})
    assert window.project.modified
    g = [g for g in window.model_view._shape_data if g["lines"]][0]
    assert g["color"] == (0, 0, 255) and g["width"] == 4 and g["dashed"] and not g["on_top"]
    assert g["fills"] and g["fill_opacity"] == 0.5 and g["anchor_labels"] == ["site"]
    ov = window.model_view.overlay
    assert ov.shape_fills and ov.shape_fills[0][1].alpha() == 128
    assert ov.shape_points[0][1]["symbol"] == "diamond" and ov.shape_points[0][1]["size"] == 20
    lab = [e for e in ov.shape_labels if "G1" in e[1]][0]
    assert lab[2].name() == "#ff00ff" and lab[3]["label_bg"] is False and lab[3]["label_size"] == 12
    o = {d["type"]: d for d in window.ortho_view.shapes}
    assert o["polygon"]["dashed"] and o["polygon"]["fill_color"] == (255, 255, 0) and o["point"]["symbol"] == "diamond"
    assert o["polygon"]["label"] == "site" and o["polygon"]["anchor"] is not None
    window.open_photo(0)
    items = {("pt" if it["points"] else "ln"): it for it in window.photo_view.shapes}
    assert items["ln"]["dashed"] and items["ln"]["width"] == 4 and items["ln"]["fills"]
    assert items["pt"]["symbol"] == "diamond" and items["pt"]["label_style"]["color"] == (255, 0, 255)
    pv = window.photo_view
    fills = [it for it in pv._shape_items if it.zValue() == 3]
    assert fills and all(pv.view.scene().sceneRect().contains(f.boundingRect()) for f in fills)   # clipped to the photo
    # per-shape override (shape 0 = G1) and removal
    window.set_shape_style(0, dict(window.layer_style("GCP"), line_color=[0, 255, 0]))
    assert ch.shapes["shape_styles"] == {"0": {"line_color": [0, 255, 0]}}
    assert window.shape_style(0, highlight=False)["line_color"] == [0, 255, 0]
    assert window.shape_style(1, highlight=False)["line_color"] != [0, 255, 0]
    # saved with the project and back after a reload; a re-import keeps them
    pm = str(tmp_path / "styled.pmsx")
    window.project.save(pm)
    from photomesh.project import Project
    p2 = Project.load(pm)
    assert p2.chunk.shapes["styles"]["Site"]["line_style"] == "dashed"
    assert p2.chunk.shapes["shape_styles"]["0"]["line_color"] == [0, 255, 0]
    window.project = p2; window.refresh_all(); window.reload_view(); window.wait_view_loaded()
    window.update_shape_views()
    o = {d["type"]: d for d in window.ortho_view.shapes}
    assert o["polygon"]["dashed"] and o["polygon"]["color"] == (0, 0, 255)
    _gcp, _site, local = _site_files(tmp_path)
    window.import_shapes(local, {"layer": "loc", "z_mode": "keep"}, far_action="offset")
    assert window.chunk.shapes["styles"]["Site"]["line_style"] == "dashed"
    window.remove_shape_layer("GCP")                  # shape indices shift: the override goes with G1
    assert "GCP" not in window.chunk.shapes["styles"] and not window.chunk.shapes.get("shape_styles")


def test_gui_style_dialog_live_apply_cancel_reset(window, no_dialogs, tmp_path):
    from PySide6.QtWidgets import QDialogButtonBox
    ch = _with_shapes(window, tmp_path)
    window.project.modified = False
    dlg = window.open_shape_style(layer="Site", modal=False)
    assert dlg.layer() == "Site" and not dlg.target_is_shape()
    fields = [dlg.label_field.itemData(i) for i in range(dlg.label_field.count())]
    assert fields[:2] == ["", "(name)"] and "name" in fields
    dlg.line_width.setValue(6.5)                                 # live preview, not committed
    assert window.ortho_view.shapes[-1]["width"] == 6.5 and not window.project.modified
    dlg.reject()                                                 # Cancel restores
    assert window.ortho_view.shapes[-1]["width"] != 6.5 and not (ch.shapes.get("styles") or {}).get("Site")
    dlg = window.open_shape_style(layer="Site", modal=False)
    dlg.line_color.set_color([0, 200, 0], emit=True)
    dlg.line_style.setCurrentIndex(1); dlg.fill_opacity.setValue(60); dlg.on_top.setCurrentIndex(1)
    dlg.buttons.button(QDialogButtonBox.Apply).click()
    assert window.project.modified
    assert ch.shapes["styles"]["Site"]["line_color"] == [0, 200, 0] and ch.shapes["styles"]["Site"]["fill_opacity"] == 0.6
    dlg.buttons.button(QDialogButtonBox.RestoreDefaults).click()
    assert not ch.shapes["styles"].get("Site")                  # reset previews defaults
    dlg.reject()                                                 # ... cancel goes back to the applied style
    assert ch.shapes["styles"]["Site"]["line_style"] == "dashed"
    # per shape: select G2 (index 1), override its symbol; OK commits
    window.select_shape(1)
    dlg = window.open_shape_style(modal=False)
    assert dlg.layer() == "GCP" and dlg.target_is_shape() and "G2" in dlg.shape_label.text()
    dlg.point_symbol.setCurrentIndex(dlg.point_symbol.findData("cross"))
    dlg.label_on.setChecked(False)
    dlg.buttons.button(QDialogButtonBox.Ok).click()
    assert ch.shapes["shape_styles"]["1"] == {"point_symbol": "cross", "label": False}
    assert window.shape_style(0, highlight=False)["point_symbol"] == "ring"
    # entry points: Tools menu, Workspace context menu / double-click on a layer
    assert "Tools/Shape Settings..." in window.actions or "Shape Settings..." in window.actions
    seen = []
    window.workspace.contextAction.connect(lambda a, ci: seen.append(a))
    layer_item = next(it for it in window.workspace._iter()
                      if it.data(0, 256) and tuple(it.data(0, 256))[0] == "shape_layer")
    import photomesh.shape_style_ui as UI
    opened = []
    orig = UI.ShapeStyleDialog.exec
    UI.ShapeStyleDialog.exec = lambda self: opened.append(self.layer()) or 0
    try:
        window.workspace._dbl(layer_item, 0)
        window._workspace_action("shape_style:shape:2", 0)
        window.actions["Shape Settings..."].trigger()
    finally:
        UI.ShapeStyleDialog.exec = orig
    assert seen and seen[0].startswith("shape_style:layer:")
    assert opened[0] == tuple(layer_item.data(0, 256))[2] and opened[1] == "GCP" and len(opened) == 3


def test_gui_transparency_toggle_persists(window, no_dialogs, tmp_path):
    from PySide6.QtCore import QSettings
    _with_shapes(window, tmp_path)
    window.open_photo(0)
    a = window.transparency_action
    assert a.isCheckable() and "Transparent" in a.toolTip() and not a.icon().isNull()
    assert a.shortcut().toString() == "Ctrl+Shift+T"
    tb = [w for w in window.main_toolbar.findChildren(type(window.transparency_button))
          if w.objectName() == "transparency_button"]
    assert tb and tb[0].menu() is not None
    a.trigger()                                                         # on
    assert window.model_view.transparent and window.photo_view.transparent and window.ortho_view.transparent
    from PySide6.QtGui import QImage, QPainter, QColor
    ov = window.ortho_view; ov.resize(300, 200)
    img = QImage(300, 200, QImage.Format_RGB32); img.fill(QColor(255, 255, 255))
    p = QPainter(img); ov._paint(p); p.end()                      # dimmed towards the dark background
    assert ov.transparent
    assert window.photo_view._pix.opacity() == pytest.approx(0.4)
    assert window.model_view.overlay.transparent
    window.transparency_slider.setValue(25)
    assert window.photo_view._pix.opacity() == pytest.approx(0.25) and window.model_view.surface_opacity == 0.25
    st = QSettings()
    assert st.value("view/transparent", False, bool) and st.value("view/transparent_opacity", 0, float) == 0.25
    assert window.transparency_value.text() == "25%"
    # hidden parts are kept in the photo items and drawn only in the transparent view
    assert all("hidden_lines" in it for it in window.photo_view.shapes)
    a.trigger()                                                         # off
    assert not window.model_view.transparent and window.photo_view._pix.opacity() == 1.0
    assert not QSettings().value("view/transparent", True, bool)
    window.set_transparency(True, 0.5)                                  # restored at start-up
    from photomesh.mainwindow import MainWindow
    w2 = MainWindow()
    try:
        assert w2.transparency_action.isChecked() and w2.model_view.surface_opacity == 0.5
        assert w2.transparency_slider.value() == 50
    finally:
        w2.set_transparency(False); w2.project.modified = False; w2.close()
    window.set_transparency(False)


@pytest.mark.opengl
@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM", "") in ("offscreen", "minimal") or
                    not os.environ.get("PHOTOMESH_GL_TESTS"), reason="needs a real OpenGL display")
def test_gl_transparent_surface_shows_shape_below(qapp):
    """A red depth-tested line 0.5 below an opaque plane is hidden (only its
    faded dashed copy shows); with the transparent view the plane is blended
    and the line drawn over it."""
    import time
    from PySide6.QtWidgets import QWidget
    from photomesh.views import ModelView
    mv = ModelView(); mv.set_z_up(True); mv.resize(300, 300); QWidget.show(mv)
    mv.overlay.legend = False
    V = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float); F = np.array([[0, 1, 2], [0, 2, 3]])
    mv.set_mesh(V, F)
    mv.set_shapes([{"color": (255, 0, 0), "width": 6, "on_top": False, "lines": [np.array([[-0.8, 0, -0.5],
                                                                                           [0.8, 0, -0.5]])],
                    "points": None, "labels": []}])
    mv.gl.setCameraPosition(elevation=90, azimuth=0, distance=4)

    def frame():
        for _ in range(15):
            qapp.processEvents(); time.sleep(0.02)
        mv.gl.update()
        for _ in range(5):
            qapp.processEvents(); time.sleep(0.02)
        return mv.gl.grabFramebuffer()

    def red_line(img):                       # the line runs vertically through the centre
        w, h = img.width(), img.height()
        rows = range(int(h * 0.3), int(h * 0.7))
        strong = lambda c: c.red() > 200 and c.green() < 70 and c.blue() < 70
        return sum(1 for y in rows if any(strong(img.pixelColor(x, y)) for x in range(w // 2 - 6, w // 2 + 6))) / len(rows)
    opaque = red_line(frame())
    mv.set_transparency(True, 0.4)
    see_through = red_line(frame())
    assert see_through > 0.9 and opaque < 0.3, (opaque, see_through)
    assert mv.gl.pass_log == ["geometry", "overlay"]
