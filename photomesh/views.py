"""Central views: Model (3D), Ortho and Photo."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import Qt, Signal, QEvent, QPointF
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QWheelEvent, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QGraphicsView,
                               QGraphicsScene, QStackedLayout)

try:
    import pyqtgraph as pg
    import pyqtgraph.opengl as gl
    HAVE_GL = True
except Exception:  # pragma: no cover
    HAVE_GL = False


try:  # draw markers on top of the mesh/cloud (no depth test), like flags in Metashape
    from OpenGL import GL as _GL
    _ON_TOP_GL_OPTIONS = {_GL.GL_DEPTH_TEST: False, _GL.GL_BLEND: True, _GL.GL_ALPHA_TEST: False,
                          _GL.GL_CULL_FACE: False,
                          "glBlendFunc": (_GL.GL_SRC_ALPHA, _GL.GL_ONE_MINUS_SRC_ALPHA)}
    # transparent surfaces: blended, depth-tested but no depth writes, drawn after the opaque items
    _TRANSPARENT_GL_OPTIONS = {_GL.GL_DEPTH_TEST: True, _GL.GL_BLEND: True, _GL.GL_ALPHA_TEST: False,
                               _GL.GL_CULL_FACE: False, "glDepthMask": (_GL.GL_FALSE,),
                               "glBlendFunc": (_GL.GL_SRC_ALPHA, _GL.GL_ONE_MINUS_SRC_ALPHA)}
    _OPAQUE_GL_OPTIONS = {_GL.GL_DEPTH_TEST: True, _GL.GL_BLEND: False, _GL.GL_ALPHA_TEST: False,
                          _GL.GL_CULL_FACE: False, "glDepthMask": (_GL.GL_TRUE,)}
    # depth-tested shapes: draped lines lie on the surface -> a tiny depth-range bias
    # (~0.5% of the view distance) so they are not half-hidden by z-fighting
    _DEPTH_TESTED_GL_OPTIONS = {_GL.GL_DEPTH_TEST: True, _GL.GL_BLEND: True, _GL.GL_ALPHA_TEST: False,
                                _GL.GL_CULL_FACE: False, "glDepthMask": (_GL.GL_FALSE,),
                                "glDepthRange": (0.0, 1.0 - 5e-6),
                                "glBlendFunc": (_GL.GL_SRC_ALPHA, _GL.GL_ONE_MINUS_SRC_ALPHA)}
except Exception:  # pragma: no cover
    _ON_TOP_GL_OPTIONS = _TRANSPARENT_GL_OPTIONS = _OPAQUE_GL_OPTIONS = _DEPTH_TESTED_GL_OPTIONS = None
TRANSPARENT_DEPTH_VALUE = 5          # after opaque items (0), before cameras (8) and shapes (9)


if HAVE_GL:
    class OverlayGLView(gl.GLViewWidget):
        """GLViewWidget with a final 2D pass: after all GL items are drawn, the
        owner's overlay (labels, marker dots, legend) is painted with QPainter
        with the depth test off, so geometry can never cover it."""
        overlay_cb = None

        def paintGL(self):
            try:              # GL state that items may leave changed (transparent surfaces, depth bias)
                _GL.glDepthMask(_GL.GL_TRUE); _GL.glDepthRange(0.0, 1.0)
            except Exception:  # pragma: no cover
                pass
            super().paintGL()
            try:
                _GL.glDepthMask(_GL.GL_TRUE); _GL.glDepthRange(0.0, 1.0)
            except Exception:  # pragma: no cover
                pass
            self.pass_log = ["geometry"]
            cb = self.overlay_cb
            if cb is not None:
                try:
                    cb(self)
                    self.pass_log.append("overlay")
                except Exception as e:  # pragma: no cover - never break the frame
                    print("overlay pass:", e)
else:  # pragma: no cover
    OverlayGLView = None


# COLMAP/OpenCV frames are y-down, z-forward; show them z-up in the viewer.
FLIP = np.array([1.0, -1.0, -1.0], np.float32)


# (key, label, default visibility) of every toggleable 3D-view layer
LAYERS = [
    ("tie", "Tie Points", True),
    ("cloud", "Point Cloud", True),
    ("mesh", "Model", True),
    ("tiled", "Tiled Model", True),
    ("ortho", "Orthomosaic", True),
    ("dem", "DEM", True),
    ("cameras", "Cameras", True),
    ("camera_labels", "Camera Labels", False),
    ("markers", "Markers", True),
    ("marker_labels", "Marker Labels", True),
    ("shapes", "Shapes", True),
    ("grid", "Grid", True),
    ("region", "Region", False),
    ("trackball", "Trackball", False),
]


BACKGROUND = QColor(74, 74, 80)          # dark grey viewport background
DEFAULT_FOV = 30.0                        # degrees, shown as "Perspective 30°"


def view_rotation(azimuth, elevation):
    """3x3 rotation taking world vectors to camera space for pyqtgraph's
    GLViewWidget (rotate(elev-90, x) * rotate(azim+90, -z))."""
    a = np.radians(-(azimuth + 90.0)); e = np.radians(elevation - 90.0)
    rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(e), -np.sin(e)], [0, np.sin(e), np.cos(e)]])
    return rx @ rz


class AxisGizmo(QWidget):
    """Small XYZ axis indicator drawn with QPainter (bottom-right of the 3D view)."""
    COLORS = {"X": QColor(230, 70, 70), "Y": QColor(90, 200, 90), "Z": QColor(80, 130, 240)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(70, 70)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.azimuth, self.elevation = 45.0, 30.0

    def set_angles(self, azimuth, elevation):
        if (azimuth, elevation) != (self.azimuth, self.elevation):
            self.azimuth, self.elevation = float(azimuth), float(elevation)
            self.update()

    def axes_2d(self):
        """Screen-space (dx, dy, depth) of the unit X, Y, Z axes (y down)."""
        r = view_rotation(self.azimuth, self.elevation)
        return {k: (float(v[0]), float(-v[1]), float(v[2]))
                for k, v in zip("XYZ", r.T)}

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2); L = self.width() * 0.36
        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        for k, (dx, dy, dz) in sorted(self.axes_2d().items(), key=lambda kv: kv[1][2]):
            col = QColor(self.COLORS[k])
            if dz > 0.3:                       # pointing away from the viewer: dim it
                col.setAlpha(140)
            p.setPen(QPen(col, 2)); end = QPointF(c.x() + dx * L, c.y() + dy * L)
            p.drawLine(c, end)
            p.drawText(QPointF(end.x() + dx * 7 - 3, end.y() + dy * 7 + 4), k)
        p.end()


def camera_icon_scale(centres, radius):
    """Frustum depth for the camera icons: 0.6 x the median spacing of
    neighbouring cameras, at most 3% of the scene radius. The old fixed 7% of
    the radius (~70 m on a 1 km site) pushed the frusta of a drone flight at
    ~80 m above steep terrain into the slope, so the mesh cut them."""
    C = np.asarray(centres, float).reshape(-1, 3)
    cap = radius * 0.03
    if len(C) < 2:
        return cap
    try:
        from scipy.spatial import cKDTree
        d, _ = cKDTree(C).query(C, k=2)
        nn = float(np.median(d[:, 1]))
    except Exception:  # pragma: no cover
        sub = C[:: max(1, len(C) // 300)]
        D = np.linalg.norm(sub[:, None] - sub[None], axis=2); D[D == 0] = np.inf
        nn = float(np.median(D.min(1)))
    return float(min(cap, max(nn * 0.6, radius * 0.003)))


def _with_alpha(colors, a):
    """Per-vertex colours (n,3|4) as float RGBA with alpha a."""
    C = np.asarray(colors, np.float32)
    if C.ndim == 1:
        C = np.tile(C, (1, 1))
    if C.max(initial=0) > 1.5:
        C = C / 255.0
    if C.shape[1] == 3:
        C = np.c_[C, np.ones(len(C), np.float32)]
    C = C.copy(); C[:, 3] = a
    return C


class ModelView(QWidget):
    """3D viewport: tie points, point cloud, mesh, camera frusta, grid.

    Right-click: Add Marker on the surface under the cursor (addMarkerRay
    carries the model-frame ray; the main window casts it on the surface)."""
    markerAction = Signal(str, str)
    moveMarkerRay = Signal(str, object, object)
    addMarkerRay = Signal(object, object)          # origin, direction (model frame)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._moving_marker = None
        self.marker_states = {}
        self.flip = FLIP.copy()
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self.items = {}
        self.layers = {"tie": None, "cloud": None, "mesh": None, "mesh_tex": None, "tiled": None}
        self._tex = None                 # (batches, zrange) of the textured model, see set_textured
        self._tiled = None               # (tiles dir, matrix) of the tiled model, see set_tiled
        self.tiled_mode = "textured"
        # layer visibility (View toolbar / Model > Show/Hide Items); see LAYERS
        self.show = {k: d for k, _t, d in LAYERS}
        self.data = {}                 # key -> (points, colors) as displayed (display-decimated)
        self.solid = {}                # key -> True when drawn in solid colour
        self.mesh_mode = "shaded"
        self.center = np.zeros(3)
        self.radius = 10.0
        self.gl = None
        from .overlay3d import Overlay
        self.overlay = Overlay()
        self.overlay.show.update({k: self.show.get(k, True) for k in self.overlay.show})
        self.normals_flipped = False
        self.transparent = False          # transparent-surface view (shapes under the surface visible)
        self.surface_opacity = 0.4
        from PySide6.QtGui import QGuiApplication
        offscreen = QGuiApplication.platformName() in ("offscreen", "minimal")
        if HAVE_GL and not offscreen and not os.environ.get("PHOTOMESH_NO_GL"):
            try:
                self.gl = OverlayGLView()
                self.gl.overlay_cb = self._paint_overlay
                self.gl.setContextMenuPolicy(Qt.CustomContextMenu)
                self.gl.customContextMenuRequested.connect(self._context)
                self.gl.setBackgroundColor(BACKGROUND)
                self.gl.opts["fov"] = DEFAULT_FOV
                self.grid = gl.GLGridItem(); self.grid.setSize(20, 20); self.grid.setSpacing(1, 1)
                self.grid.setColor((150, 150, 158, 55))          # faint grid
                self.gl.addItem(self.grid)
                self.axis = gl.GLAxisItem(); self.axis.setSize(1, 1, 1)
                self.gl.addItem(self.axis)
                lay.addWidget(self.gl)
                self.reset_view()
            except Exception as e:
                self.gl = None
                lay.addWidget(QLabel(f"OpenGL view unavailable: {e}"))
        elif offscreen or os.environ.get("PHOTOMESH_NO_GL"):
            lbl = QLabel("3D view disabled (no OpenGL on this display platform)")
            lbl.setAlignment(Qt.AlignCenter); lay.addWidget(lbl)
        else:
            lbl = QLabel("3D view requires pyqtgraph + PyOpenGL (pip install -r requirements.txt)")
            lbl.setAlignment(Qt.AlignCenter); lay.addWidget(lbl)
        self._build_overlays()

    # ---- overlays: projection (top-left), axis gizmo (bottom-right)
    def _build_overlays(self):
        host = self.gl if self.gl is not None else self
        if self.gl is None:
            self.setAutoFillBackground(True)
            pal = self.palette(); pal.setColor(self.backgroundRole(), BACKGROUND); self.setPalette(pal)
        css = "QLabel{color:#e6e6e6;background:transparent;font-size:8pt;}"
        self.proj_label = QLabel(host); self.proj_label.setStyleSheet(css)
        self.stale_label = QLabel(host)
        self.stale_label.setStyleSheet("QLabel{color:white;background:#923e25;padding:6px;font-weight:bold;}")
        self.stale_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.stale_label.hide()
        self.gizmo = AxisGizmo(host)
        self.proj_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.ortho = False
        self._update_projection_label()
        host.installEventFilter(self)
        self._overlay_host = host
        self._place_overlays()

    def _place_overlays(self):
        host = self._overlay_host
        self.proj_label.adjustSize()
        self.proj_label.move(8, 6)
        self.stale_label.move(8, 30)
        self.stale_label.setMaximumWidth(max(200, host.width() - 20))
        self.stale_label.raise_()
        self.gizmo.move(max(0, host.width() - self.gizmo.width() - 4),
                        max(0, host.height() - self.gizmo.height() - 4))
        for w in (self.proj_label, self.gizmo):
            w.raise_()

    def set_stale_message(self, text):
        self.stale_label.setText(text)
        self.stale_label.adjustSize()
        self.stale_label.setVisible(bool(text))
        self._place_overlays()

    def projection_text(self):
        if self.ortho:
            return "Orthographic"
        fov = self.gl.opts["fov"] if self.gl is not None else DEFAULT_FOV
        return f"Perspective {fov:g}\u00b0"

    def _update_projection_label(self):
        self.proj_label.setText(self.projection_text()); self.proj_label.adjustSize()

    def _sync_gizmo(self):
        if self.gl is not None and hasattr(self, "gizmo"):
            self.gizmo.set_angles(self.gl.opts["azimuth"], self.gl.opts["elevation"])

    def eventFilter(self, obj, ev):
        t = ev.type()
        if self._moving_marker and t == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
            self.cancel_marker_move(); return True
        if self._moving_marker and t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            ray = self.ray_at(ev.position().x(), ev.position().y())
            if ray is not None:
                label = self._moving_marker
                self.cancel_marker_move()
                self.moveMarkerRay.emit(label, ray[0], ray[1])
            return True
        if t in (QEvent.Resize, QEvent.Show):
            self._place_overlays()
        elif t in (QEvent.MouseMove, QEvent.Wheel, QEvent.MouseButtonRelease, QEvent.KeyPress):
            self._sync_gizmo()
        return False

    # ---- data
    def _remove(self, key):
        it = self.layers.get(key)
        if it is not None and self.gl is not None:
            for i in (it if isinstance(it, list) else [it]):
                self.gl.removeItem(i)
        self.layers[key] = None

    def set_points(self, key, pts, colors=None, size=2.0, fit=True):
        if pts is None:
            return
        self.data[key] = (pts, colors, size)
        if self.solid.get(key):
            colors = None
        if self.gl is None:
            return
        self._remove(key)
        pts = np.asarray(pts, np.float32) * self.flip
        if colors is None:
            colors = np.tile(np.array([[0.75, 0.75, 0.8, 1.0]], np.float32), (len(pts), 1))
        if self.transparent:
            colors = _with_alpha(colors, self.surface_opacity)
        item = gl.GLScatterPlotItem(pos=pts, color=colors, size=size, pxMode=True)
        self._style_surface(item)
        self.gl.addItem(item)
        self.layers[key] = item
        item.setVisible(self.show.get(key, True))
        if fit:
            self._fit(pts)
        if self.show.get("region"):
            self._update_region()

    def has_layer(self, key):
        if key == "mesh":
            return bool(getattr(self, "_mesh_data", None)) or self._tex is not None
        if key == "tiled":
            return self._tiled is not None
        return key in self.data

    def set_solid(self, key, solid):
        """Redraw a point layer in its colours or in solid grey, reusing the
        arrays already loaded (no file read)."""
        self.solid[key] = bool(solid)
        if key in self.data:
            pts, colors, size = self.data[key]
            self.set_points(key, pts, colors, size, fit=False)

    def set_mesh(self, verts, faces, colors=None, normals=None, fit=True):
        """Show the mesh in the current mesh mode. `colors` are the mesh's own
        vertex colours (kept, so switching Shaded -> Textured doesn't lose them)."""
        self._mesh_data = (verts, faces, colors, normals)
        if self.gl is None:
            return
        self._remove("mesh")
        from .plyio import vertex_normals
        col = colors if self.mesh_mode in ("textured", "solid") else None
        if col is not None and self.transparent:
            col = _with_alpha(col, self.surface_opacity)
        V = np.asarray(verts, np.float32) * self.flip
        md = gl.MeshData(vertexes=V, faces=np.asarray(faces, np.int32), vertexColors=col)
        # precomputed (vectorised) normals: pyqtgraph would compute them in a
        # Python loop on the GUI thread at first paint (~1 min for 1.8M faces)
        N = ((np.asarray(normals, np.float32) * self.flip) if normals is not None
             else vertex_normals(V, faces))
        # terrain is looked at from above: normals that mostly point down (reversed
        # winding, mirrored transform) are flipped; the shader is two-sided anyway
        from .lighting import orient_normals_up, headlight_program
        N, self.normals_flipped = orient_normals_up(N, z_up=True, min_fraction=0.65)   # closed objects: ~0.5
        md._vertexNormals = N
        kw = dict(meshdata=md, smooth=True, drawEdges=self.mesh_mode == "wireframe",
                  drawFaces=self.mesh_mode != "wireframe", edgeColor=(0.6, 0.8, 1, 1))
        if col is None:
            # camera-relative key + headlight fill + ambient (pyqtgraph's 'shaded' lit from behind)
            kw.update(shader=headlight_program(), color=(0.78, 0.78, 0.78, self._alpha()))
        item = gl.GLMeshItem(**kw)
        self._style_surface(item)
        self.gl.addItem(item)
        self.layers["mesh"] = item
        item.setVisible(self.show["mesh"] and not self._textured_active())
        if fit:
            self._fit(np.asarray(verts) * self.flip)
        if self.show.get("region"):
            self._update_region()

    def set_mesh_mode(self, mode):
        changed = mode != self.mesh_mode
        self.mesh_mode = mode
        if changed and getattr(self, "_mesh_data", None):
            v, f, c, n = self._mesh_data
            self.set_mesh(v, f, c, n, fit=False)
        self._apply_tex()

    # ---- textured model (texture atlas, own GL item) and tiled model (LOD streaming)
    def _textured_active(self):
        return self.mesh_mode == "textured" and self._tex is not None and self.gl is not None

    def set_textured(self, batches, zrange=(0, 1)):
        """Texture atlas of the model (glitems.load_textured_display output);
        used in Model - Textured. None removes it."""
        self._remove("mesh_tex")
        self._tex = (batches, zrange) if batches else None
        self._apply_tex()

    def _apply_tex(self):
        if self.gl is None:
            return
        self._remove("mesh_tex")
        plain = self.layers.get("mesh")
        if self._textured_active():
            from .glitems import TexturedMeshItem
            it = TexturedMeshItem(self._tex[0], zrange=self._tex[1])
            self._style_surface(it)
            it.setVisible(self.show["mesh"])
            self.gl.addItem(it); self.layers["mesh_tex"] = it
            if plain is not None and not isinstance(plain, list):
                plain.setVisible(False)
        elif plain is not None and not isinstance(plain, list):
            plain.setVisible(self.show["mesh"])

    def has_texture(self):
        return self._tex is not None

    def set_tiled(self, tiles_dir, model_to_tiles=None, fit=False):
        """Show a tiled model (tiles.build_tiles output), streamed by LOD."""
        self._remove_tiled()
        self._tiled = (tiles_dir, model_to_tiles) if tiles_dir else None
        if self.gl is None or not tiles_dir:
            return
        from .glitems import TiledModelItem
        R = np.asarray(model_to_tiles if model_to_tiles is not None else np.eye(3), float)
        M = np.eye(4); M[:3, :3] = np.diag(self.flip.astype(float)) @ R.T
        it = TiledModelItem(tiles_dir, M, mode=self.tiled_mode)
        self._style_surface(it)
        it.setVisible(self.show.get("tiled", True))
        self.gl.addItem(it); self.layers["tiled"] = it
        if fit:
            n = it.nodes[it.root]
            c = np.array([[n["bmin"][0], n["bmin"][1], n["bmin"][2]], [n["bmax"][0], n["bmax"][1], n["bmax"][2]]])
            self._fit((c @ R) * self.flip)

    def _remove_tiled(self):
        it = self.layers.get("tiled")
        if it is not None and hasattr(it, "shutdown"):
            it.shutdown()
        self._remove("tiled")

    def set_tiled_mode(self, mode):
        self.tiled_mode = mode
        it = self.layers.get("tiled")
        if it is not None:
            it.set_mode(mode)

    def tiled_item(self):
        return self.layers.get("tiled")

    def set_z_up(self, z_up):
        """Georeferenced chunks are already Z-up; COLMAP frames are y-down."""
        self.flip = np.ones(3, np.float32) if z_up else FLIP.copy()

    def _add(self, key, items):
        """Add GL items as layer `key`, honouring its visibility."""
        self._remove(key)
        items = [i for i in (items if isinstance(items, list) else [items]) if i is not None]
        for i in items:
            i.setVisible(self.show.get(key, True))
            self.gl.addItem(i)
        self.layers[key] = items or None

    # ---- transparent surfaces
    def _alpha(self):
        return float(self.surface_opacity) if self.transparent else 1.0

    def _style_surface(self, item):
        """GL state of a surface item (mesh, textured, tiled, point clouds) for
        the current transparency: blended without depth writes and drawn after
        the opaque items, or opaque with depth writes."""
        if self.transparent and _TRANSPARENT_GL_OPTIONS is not None:
            item.setGLOptions(dict(_TRANSPARENT_GL_OPTIONS)); item.setDepthValue(TRANSPARENT_DEPTH_VALUE)
        elif _OPAQUE_GL_OPTIONS is not None:
            item.setGLOptions(dict(_OPAQUE_GL_OPTIONS)); item.setDepthValue(0)
        if hasattr(item, "alpha"):
            item.alpha = self._alpha()

    def set_transparency(self, on, opacity=None):
        """Transparent view: the model, textured / tiled model and the point clouds
        are drawn with `opacity` (0.05..1); shapes are then drawn over them."""
        if opacity is not None:
            self.surface_opacity = float(min(1.0, max(0.05, opacity)))
        self.transparent = bool(on)
        self.overlay.transparent = self.transparent
        if self.gl is None:
            return
        plain = self.layers.get("mesh")
        md = getattr(self, "_mesh_data", None)
        if plain is not None and md:
            if md[2] is not None and self.mesh_mode in ("textured", "solid"):
                self.set_mesh(*md, fit=False)              # vertex colours carry the alpha
            else:
                if self.mesh_mode != "wireframe":
                    plain.setColor((0.78, 0.78, 0.78, self._alpha()))
                self._style_surface(plain)
        for key in ("mesh_tex", "tiled"):
            it = self.layers.get(key)
            if it is not None and not isinstance(it, list):
                self._style_surface(it)
        for key in ("tie", "cloud"):
            if key in self.data:
                pts, colors, size = self.data[key]
                self.set_points(key, pts, colors, size, fit=False)
        if getattr(self, "_shape_data", None) is not None:
            self.set_shapes(self._shape_data)
        self.gl.update()

    def set_markers(self, pts, labels=None, control=None):
        """Marker positions (model frame): control (enabled) markers yellow, check
        (disabled) markers cyan. Dots and labels are drawn in the final overlay
        pass (overlay3d), on top of every model; hidden ones are hollow."""
        self._marker_data = (pts, labels, control)
        self.overlay.marker_states = self.marker_states
        self._remove("markers"); self._remove("marker_labels")
        if pts is None or not len(pts):
            self.overlay.markers = None
        else:
            P = np.asarray(pts, np.float32) * self.flip
            ctl = np.ones(len(P), bool) if control is None else np.asarray(control, bool)
            self.overlay.markers = (P, None if labels is None else [str(t) for t in labels], ctl)
        self._overlay_changed()

    def view_mvp(self):
        """Row-major 4x4 MVP of the current view (display frame), or None."""
        if self.gl is None:
            return None
        region = self.gl.getViewport()
        return np.array((self.gl.projectionMatrix(region, region) * self.gl.viewMatrix()).data(),
                        float).reshape(4, 4).T

    def ray_at(self, x, y):
        """Model-frame ray through logical widget pixel (x, y)."""
        from .overlay3d import pixel_ray
        mvp = self.view_mvp()
        if mvp is None:
            return None
        o, d = pixel_ray(mvp, x, y, self.gl.width(), self.gl.height())
        f = np.asarray(self.flip, float)            # display = model * flip (flip is +-1)
        o, d = o * f, d * f
        return o, d / np.linalg.norm(d)

    def begin_marker_move(self, label):
        self._moving_marker = label
        (self.gl or self).setCursor(Qt.CrossCursor)
        (self.gl or self).setFocus()

    def cancel_marker_move(self):
        self._moving_marker = None
        (self.gl or self).unsetCursor()

    def marker_at(self, x, y, tolerance=18):
        if self.overlay.markers is None or not self.overlay.show.get("markers", True):
            return None
        for kind, label, rect in self.overlay.last.get("label_rects", []):
            if kind == "marker" and rect.contains(QPointF(x, y)):
                return label
        from .overlay3d import project
        mvp = self.view_mvp()
        if mvp is None or self.gl is None:
            return None
        points, labels, _ = self.overlay.markers
        xy, _z, valid = project(mvp, points, self.gl.width(), self.gl.height())
        distance = np.linalg.norm(xy - [x, y], axis=1)
        choices = np.flatnonzero(valid & (distance <= tolerance))
        return labels[min(choices, key=lambda k: distance[k])] if len(choices) and labels else None

    def _context(self, pos):
        from PySide6.QtWidgets import QMenu
        from .marker_style import marker_menu
        menu = QMenu(self)
        label = self.marker_at(pos.x(), pos.y())
        if label:
            marker_menu(menu, label, lambda action, lab: self.markerAction.emit(action, lab))
            menu.addSeparator()
        a = menu.addAction("Add Marker")
        a.setToolTip("New marker on the surface under the cursor, projected into the photos")
        if menu.exec(self.gl.mapToGlobal(pos)) is a:
            ray = self.ray_at(pos.x(), pos.y())
            if ray is not None:
                self.addMarkerRay.emit(ray[0], ray[1])

    def _overlay_changed(self):
        if self.gl is not None:
            self.gl.update()

    def _paint_overlay(self, glw):
        """Final pass (called from OverlayGLView.paintGL after all GL items)."""
        from PySide6.QtGui import QMatrix4x4
        from .overlay3d import project
        w, h = glw.width(), glw.height()
        if w <= 0 or h <= 0:
            return
        region = glw.getViewport()
        proj = glw.projectionMatrix(region, region)
        mvp_q = proj * glw.viewMatrix()
        mvp = np.array(mvp_q.data(), float).reshape(4, 4).T
        dist = float(glw.opts["distance"])
        near, far = dist * 0.001, dist * 1000.0
        dpr = float(glw.devicePixelRatioF())
        H_dev = int(round(h * dpr))

        def depth_at(xy):
            try:
                out = []
                for x, y in xy[:400]:
                    px, py = int(x * dpr), H_dev - 1 - int(y * dpr)
                    v = _GL.glReadPixels(px, py, 1, 1, _GL.GL_DEPTH_COMPONENT, _GL.GL_FLOAT)
                    out.append(float(np.asarray(v, float).ravel()[0]))
                out += [1.0] * (len(xy) - len(out))
                return np.array(out)
            except Exception:
                return None
        items = self.overlay.items(mvp, w, h, depth_fn=depth_at, near=near, far=far)
        self.overlay.last_depth_test = None
        try:
            _GL.glDisable(_GL.GL_DEPTH_TEST)
            self.overlay.last_depth_test = bool(_GL.glIsEnabled(_GL.GL_DEPTH_TEST))
        except Exception:
            pass
        p = QPainter(glw)
        try:
            self.overlay.paint(p, items, w, h)
        finally:
            p.end()

    def set_shapes(self, groups):
        """Imported shapes (shapes.py), layer 'shapes'. groups: list of dicts
        {color (r,g,b 0-255), width, lines: [(n,3) model-frame polylines],
        points: (m,3), labels: [str] per point, optional: dashed, on_top,
        style (shape_style point / label keys), fills: [[rings (n,3)]],
        fill_color (r,g,b), fill_opacity, anchors: (k,3) + anchor_labels}.
        Lines are GL items: on top (no depth test), or depth-tested with the
        hidden parts drawn dashed and faded on top. In the transparent view
        everything is on top. Points, fills and labels are overlay-pass items."""
        self._shape_data = groups
        self._remove("shapes")
        ov = self.overlay
        ov.shape_labels = []; ov.shape_points = []; ov.shape_fills = []
        for g in groups or []:
            c = g.get("color", (255, 140, 0))
            qc = QColor(int(c[0]), int(c[1]), int(c[2]))
            st = dict(g.get("style") or {})
            on_top = bool(g.get("on_top", st.get("on_top", True)))
            pts, labs = g.get("points"), g.get("labels")
            if pts is not None and len(pts):
                P = np.asarray(pts, np.float32).reshape(-1, 3) * self.flip
                ov.shape_points.append((P, {"color": qc, "size": float(st.get("point_size", 14.0)),
                                            "symbol": st.get("point_symbol", "ring"), "on_top": on_top,
                                            "width": float(g.get("width", 2.0))}))
                if labs and any(labs):
                    ov.shape_labels.append((P, list(labs), self._label_colour(st, qc), st))
            anc = g.get("anchors")
            if anc is not None and len(anc) and any(g.get("anchor_labels") or []):
                ov.shape_labels.append((np.asarray(anc, np.float32).reshape(-1, 3) * self.flip,
                                        list(g["anchor_labels"]), self._label_colour(st, qc), st))
            if g.get("fills") and float(g.get("fill_opacity", 0.0)) > 0:
                fc = g.get("fill_color") or c
                q = QColor(int(fc[0]), int(fc[1]), int(fc[2]), int(round(255 * float(g["fill_opacity"]))))
                for rings in g["fills"]:
                    ov.shape_fills.append(([np.asarray(R, np.float32) * self.flip for R in rings], q))
        self._overlay_changed()
        if not groups or self.gl is None:
            return
        from .shape_style import dash_segments
        dash = max(self.radius * 0.015, 1e-3)          # ~7 m on a 1 km site
        items = []
        for g in groups:
            rgb = tuple(c / 255.0 for c in g.get("color", (255, 140, 0))[:3])
            on_top = self.transparent or bool(g.get("on_top", (g.get("style") or {}).get("on_top", True)))
            solid, dashed = [], []
            for P in g.get("lines") or []:
                P = np.asarray(P, np.float32)
                if len(P) >= 2:
                    solid.append(np.repeat(P, 2, axis=0)[1:-1])     # strip -> segment pairs
                    if g.get("dashed") or not on_top:
                        dashed.append(dash_segments(P, dash))
            if not solid:
                continue
            S_ = np.concatenate(solid) * self.flip
            D_ = (np.concatenate(dashed) * self.flip).astype(np.float32) if dashed else None
            w = float(g.get("width", 2.0))

            def line(pos, alpha, opts, wid=w):
                it = gl.GLLinePlotItem(pos=np.ascontiguousarray(pos, np.float32), mode="lines", color=rgb + (alpha,),
                                       width=wid, antialias=True)
                it.setGLOptions(dict(opts) if opts else "translucent"); it.setDepthValue(9)
                it.shape_pass = "on_top" if opts is _ON_TOP_GL_OPTIONS else "depth"
                items.append(it)
            main = D_ if g.get("dashed") else S_
            if on_top:
                line(main, 1.0, _ON_TOP_GL_OPTIONS)
            else:
                line(D_, 0.45, _ON_TOP_GL_OPTIONS, max(1.0, w - 0.5))      # hidden parts: dashed, faded
                line(main, 1.0, _DEPTH_TESTED_GL_OPTIONS)                    # visible parts
        self._add("shapes", items)

    @staticmethod
    def _label_colour(st, default):
        c = st.get("label_color")
        return QColor(int(c[0]), int(c[1]), int(c[2])) if c else default

    def set_cameras(self, cams, scale=None, labels=None):
        """cams: list of (centre(3,), R(3x3) world->cam); labels optional."""
        self._camera_data = (cams, labels)
        self.overlay.cameras = None
        if cams and labels:
            self.overlay.cameras = (np.array([np.asarray(C, float) for C, _R in cams]) * self.flip,
                                    [str(t) for t in labels])
        self._overlay_changed()
        if self.gl is None:
            return
        self._remove("cameras"); self._remove("camera_labels")
        if not cams:
            return
        scale = scale or camera_icon_scale([c for c, _r in cams], self.radius)
        segs = []
        w, h, d = 0.6 * scale, 0.45 * scale, scale
        corners_c = np.array([[-w, -h, d], [w, -h, d], [w, h, d], [-w, h, d]])
        for C, R in cams:
            C = np.asarray(C) * self.flip; R = np.asarray(R) @ np.diag(self.flip)
            cw = (R.T @ corners_c.T).T + C
            for k in range(4):
                segs += [C, cw[k], cw[k], cw[(k + 1) % 4]]
        item = gl.GLLinePlotItem(pos=np.array(segs, np.float32), mode="lines",
                                 color=(0.35, 0.65, 1.0, 1.0), width=1.0, antialias=True)
        # frusta are icons: drawn after the models without depth test (were cut by the mesh)
        item.setGLOptions(_ON_TOP_GL_OPTIONS or "translucent"); item.setDepthValue(8)
        self._add("cameras", item)

    def _update_region(self):
        """Region: axis-aligned bounding box of the reconstruction (cloud, else mesh, else tie points)."""
        if self.gl is None:
            return
        src = None
        if "cloud" in self.data:
            src = np.asarray(self.data["cloud"][0])
        elif getattr(self, "_mesh_data", None):
            src = np.asarray(self._mesh_data[0])
        elif "tie" in self.data:
            src = np.asarray(self.data["tie"][0])
        if src is None or not len(src):
            self._remove("region"); return
        P = src.astype(np.float32) * self.flip
        lo, hi = np.percentile(P, 0.5, axis=0), np.percentile(P, 99.5, axis=0)
        c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], np.float32)
        edges = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
        segs = np.array([c[i] for e in edges for i in e], np.float32)
        self._add("region", gl.GLLinePlotItem(pos=segs, mode="lines", color=(1.0, 0.55, 0.2, 1.0), width=1.5))

    def _update_trackball(self):
        """Trackball: three circles around the orbit centre."""
        if self.gl is None:
            return
        t = np.linspace(0, 2 * np.pi, 97)
        r = self.radius * 0.25
        c = np.asarray(self.center, np.float32)
        rings = []
        for (i, j), col in (((0, 1), (0.3, 0.5, 1, 1)), ((1, 2), (1, 0.3, 0.3, 1)), ((0, 2), (0.3, 1, 0.3, 1))):
            P = np.tile(c, (len(t), 1)); P[:, i] += r * np.cos(t); P[:, j] += r * np.sin(t)
            rings.append(gl.GLLinePlotItem(pos=P.astype(np.float32), mode="line_strip", color=col, width=1.0))
        self._add("trackball", rings)

    def clear(self):
        self.set_markers(None)
        self._remove_tiled()
        for k in list(self.layers):
            self._remove(k)
        self._mesh_data = None
        self._tex = None; self._tiled = None
        self.data = {}

    def set_visible(self, key, on):
        on = bool(on)
        self.show[key] = on
        if key in self.overlay.show:
            self.overlay.show[key] = on
            self._overlay_changed()
        if self.gl is None:
            return
        if key == "grid":
            self.grid.setVisible(on); self.axis.setVisible(on); return
        if on and self.layers.get(key) is None:          # built on demand
            if key == "region":
                self._update_region()
            elif key == "trackball":
                self._update_trackball()
        if key == "mesh":
            tex = self.layers.get("mesh_tex")
            if tex is not None:
                tex.setVisible(on)
            plain = self.layers.get("mesh")
            if plain is not None:
                plain.setVisible(on and tex is None)
            return
        it = self.layers.get(key)
        for i in (it if isinstance(it, list) else [it]):
            if i is not None:
                i.setVisible(on)

    # ---- navigation
    def fit_raw(self, pts):
        """Fit view to points given in reconstruction coordinates."""
        self._fit(np.asarray(pts) * self.flip)

    def _fit(self, pts):
        if len(pts) == 0:
            return
        lo, hi = np.percentile(pts, 1, axis=0), np.percentile(pts, 99, axis=0)
        self.center = (lo + hi) / 2
        self.radius = float(max(np.linalg.norm(hi - lo) / 2, 1e-3))
        s = 10 ** np.floor(np.log10(self.radius))
        self.grid.setSpacing(s, s); self.grid.setSize(self.radius * 4, self.radius * 4)
        self.grid.resetTransform(); self.grid.translate(self.center[0], self.center[1], lo[2])
        self.axis.setSize(s, s, s)
        self.reset_view()
        if self.show.get("trackball"):
            self._update_trackball()

    def reset_view(self):
        if self.gl is None:
            return
        dist = self.radius / np.tan(np.radians(self.gl.opts["fov"] / 2)) * 1.1
        self.gl.setCameraPosition(pos=pg.Vector(*self.center), distance=dist,
                                  elevation=30, azimuth=45)
        self._sync_gizmo()

    def zoom(self, factor):
        if self.gl is not None:
            self.gl.opts["distance"] *= factor; self.gl.update()

    def set_predefined(self, name):
        if self.gl is None:
            return
        el, az = {"Top": (90, -90), "Bottom": (-90, -90), "Front": (0, -90), "Back": (0, 90),
                  "Left": (0, 180), "Right": (0, 0)}[name]
        self.gl.setCameraPosition(elevation=el, azimuth=az)
        self._sync_gizmo()

    def set_orthographic(self, on):
        self.ortho = bool(on)
        self._update_projection_label()
        if self.gl is None:
            return
        d = self.gl.opts["distance"]; f = self.gl.opts["fov"]
        nf = 1.0 if on else DEFAULT_FOV
        self.gl.opts["distance"] = d * np.tan(np.radians(f / 2)) / np.tan(np.radians(nf / 2))
        self.gl.opts["fov"] = nf; self.gl.update()


def _polyline_path(lines):
    """One QPainterPath for many pixel polylines, built vectorised (pyqtgraph
    arrayToQPath with per-polyline breaks) instead of one lineTo per vertex."""
    from PySide6.QtGui import QPainterPath
    Ls = [np.asarray(L, np.float64).reshape(-1, 2) for L in lines if len(L) >= 2]
    if not Ls:
        return QPainterPath()
    X = np.concatenate(Ls)
    keep = np.isfinite(X).all(axis=1)
    conn = np.ones(len(X), bool)
    conn[np.cumsum([len(L) for L in Ls]) - 1] = False      # no segment between polylines
    if not keep.all():                                       # never draw through a NaN
        conn &= keep & np.r_[keep[1:], False]
        X, conn = X[keep], conn[keep]
    try:
        from pyqtgraph.functions import arrayToQPath
        return arrayToQPath(X[:, 0], X[:, 1], connect=conn, finiteCheck=False)
    except Exception:                                        # pragma: no cover - fallback
        path = QPainterPath(); new = True
        for (u, v), c in zip(X, conn):
            if new:
                path.moveTo(float(u), float(v))
            else:
                path.lineTo(float(u), float(v))
            new = not c
        return path


def _label_item(text, parent, ls, colour, r):
    """Label child of a photo symbol: style {size, color, bg, bg_color}."""
    from PySide6.QtGui import QPen, QBrush
    from PySide6.QtWidgets import QGraphicsSimpleTextItem, QGraphicsRectItem
    t = QGraphicsSimpleTextItem(text, parent)
    f = t.font(); f.setBold(True)
    if ls.get("size"):
        f.setPointSizeF(float(ls["size"]))
    t.setFont(f)
    lc = ls.get("color")
    tc = QColor(int(lc[0]), int(lc[1]), int(lc[2])) if lc else QColor(colour)
    t.setBrush(QBrush(tc))
    br = t.boundingRect()
    t.setPos(r + 3, -r - br.height())
    if ls.get("bg"):
        b = ls.get("bg_color") or (20, 20, 24)
        box = QGraphicsRectItem(br.adjusted(-3, -1, 3, 1), parent)
        box.setPos(t.pos()); box.setPen(QPen(Qt.NoPen))
        box.setBrush(QBrush(QColor(int(b[0]), int(b[1]), int(b[2]), 190)))
        t.setZValue(1); box.setZValue(0)
    else:
        t.setPen(QPen(QColor(0, 0, 0, 200), 0.6))
    return t


try:
    from PySide6.QtWidgets import QGraphicsItem as _QGI

    class _SymbolItem(_QGI):
        """Shape point symbol in a photo, constant screen size (ignores the zoom)."""
        def __init__(self, symbol, r, colour, width, hidden):
            super().__init__()
            self.symbol, self.r, self.colour, self.width, self.hidden = symbol, r, QColor(colour), width, hidden
            self.setFlag(_QGI.ItemIgnoresTransformations, True)

        def boundingRect(self):
            from PySide6.QtCore import QRectF
            m = self.r * 1.4 + self.width + 6
            return QRectF(-m, -m, 2 * m, 2 * m)

        def paint(self, p, _opt, _w=None):
            if self.symbol == "none":
                return
            from .overlay3d import draw_symbol
            p.setRenderHint(QPainter.Antialiasing)
            draw_symbol(p, 0.0, 0.0, self.symbol, self.r, self.colour, self.width, hidden=self.hidden)
except Exception:  # pragma: no cover
    _SymbolItem = None


class _ZoomView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(26, 26, 32))

    def wheelEvent(self, e: QWheelEvent):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)


class PhotoView(QWidget):
    """Shows a single photo with pan/zoom and marker overlay.

    Right-click: place the active marker here / remove a marker near the
    cursor. Pinned markers are green flags, predicted (not yet placed) ones blue."""
    placeMarker = Signal(float, float)      # photo pixel coordinates
    removeMarker = Signal(str)
    addMarker = Signal(float, float)        # new marker pinned at this pixel
    markerAction = Signal(str, str)
    pinMarker = Signal(str, float, float)   # pin an existing marker here

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self.view = _ZoomView()
        self.caption = QLabel("Double-click a photo in the Photos pane to open it.")
        lay.addWidget(self.view, 1); lay.addWidget(self.caption)
        self.path = None
        self.shapes = []           # projected shapes (see set_shapes)
        self._shape_items = []
        self._moving_marker = None
        self.markers = []          # (label, u, v, kind) kind: pinned | placed | predicted
        self.active_marker = None
        # Show Markers / Show Marker Labels, the same toggles as the Model and Ortho views
        self.vis = {"markers": True, "marker_labels": True}
        self._overlay = []
        self._pix = None
        self.transparent = False   # transparent view: photo dimmed, hidden shape parts dashed
        self.surface_opacity = 0.4
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context)
        self.view.viewport().installEventFilter(self)
        self.view.installEventFilter(self)

    def eventFilter(self, obj, ev):
        """Ctrl+click: pin the active marker here (or add a new marker if none is active)."""
        if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Escape and self._moving_marker:
            self.cancel_marker_move(); return True
        if (self._moving_marker and ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton
                and self.path is not None):
            point = self.view.mapToScene(ev.position().toPoint())
            label = self._moving_marker
            self.cancel_marker_move()
            self.pinMarker.emit(label, point.x(), point.y())
            return True
        if (ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton
                and ev.modifiers() & Qt.ControlModifier and self.path is not None):
            p = self.view.mapToScene(ev.position().toPoint())
            self.ctrl_click(p.x(), p.y())
            return True
        return False

    def begin_marker_move(self, label):
        self._moving_marker = label
        self.view.viewport().setCursor(Qt.CrossCursor)
        self.view.setFocus()

    def cancel_marker_move(self):
        self._moving_marker = None
        self.view.viewport().unsetCursor()

    def ctrl_click(self, x, y):
        if self.active_marker:
            self.pinMarker.emit(self.active_marker, float(x), float(y))
        else:
            self.addMarker.emit(float(x), float(y))

    def clear(self):
        """Drop the displayed photo and overlays when the project changes."""
        self.cancel_marker_move()
        sc = self.view.scene()
        sc.clear(); sc.setSceneRect(0, 0, 0, 0)
        self.view.resetTransform()
        self.path = None; self._pix = None
        self._overlay = []; self._shape_items = []
        self.shapes = []; self.markers = []; self.active_marker = None
        self.caption.setText("Double-click a photo in the Photos pane to open it.")

    def open(self, path, caption=""):
        pm = QPixmap(path)
        sc = self.view.scene(); sc.clear(); self._overlay = []; self._shape_items = []; self._pix = None
        self.shapes = []
        if pm.isNull():
            self.caption.setText(f"Cannot open {path}"); return
        self._pix = sc.addPixmap(pm); sc.setSceneRect(pm.rect())
        self._pix.setOpacity(self.surface_opacity if self.transparent else 1.0)
        self.view.resetTransform()
        self.view.fitInView(sc.sceneRect(), Qt.KeepAspectRatio)
        self.path = path
        self.caption.setText(caption or path)
        self._draw_markers()

    def set_markers(self, markers, active=None):
        self.markers = list(markers or []); self.active_marker = active
        self._draw_markers()

    def set_visible(self, key, on):
        """Show Markers hides the flags, Show Marker Labels only their text."""
        if key in self.vis and self.vis[key] != bool(on):
            self.vis[key] = bool(on)
            self._draw_markers()

    def set_transparency(self, on, opacity=None):
        """Transparent view: the photo is drawn with `opacity` over the dark
        background and shape parts hidden by the surface are drawn dashed."""
        if opacity is not None:
            self.surface_opacity = float(min(1.0, max(0.05, opacity)))
        self.transparent = bool(on)
        if self._pix is not None:
            self._pix.setOpacity(self.surface_opacity if self.transparent else 1.0)
        self.set_shapes(self.shapes)

    def set_shapes(self, items):
        """Shapes projected into this photo (pixel coordinates, (0.5,0.5) = centre of
        the top-left pixel). items: list of dicts {color, width, lines: [(n,2)],
        points: [(u, v, label)], selected} plus optional style keys: dashed,
        fills [(n,2) polygons], fill_color, fill_opacity, symbol, point_size,
        label_style {size, color, bg, bg_color}, anchors [(u, v, label)],
        hidden_lines, hidden_points [(u, v)] (drawn dashed in the transparent view)."""
        from PySide6.QtGui import QPen, QBrush, QPainterPath, QPolygonF
        from PySide6.QtWidgets import QGraphicsItem
        sc = self.view.scene()
        for it in self._shape_items:
            sc.removeItem(it)
        self._shape_items = []
        self.shapes = list(items or [])
        if self.path is None:
            return
        # lines and hidden lines: one path per pen (colour, width, dashed, selected)
        # instead of two items per shape; thousands of shapes become a few items
        line_groups, hidden_groups = {}, {}
        for g in self.shapes:
            key = (tuple(int(v) for v in g.get("color", (255, 140, 0))[:3]),
                   float(g.get("width", 2.0)) + (2.0 if g.get("selected") else 0.0),
                   bool(g.get("dashed")), bool(g.get("selected")))
            if g.get("lines"):
                line_groups.setdefault(key, []).extend(g["lines"])
            if self.transparent and g.get("hidden_lines"):
                hidden_groups.setdefault(key, []).extend(g["hidden_lines"])
        for (rgb, w, dashed, sel), lines in line_groups.items():
            path = _polyline_path(lines)
            if path.isEmpty():
                continue
            pen = QPen(QColor(*rgb), w); pen.setCosmetic(True)
            halo = QPen(QColor(0, 0, 0, 150), w + 2.0); halo.setCosmetic(True)
            if dashed:
                pen.setStyle(Qt.DashLine); halo.setStyle(Qt.DashLine)
            h = sc.addPath(path, halo); h.setZValue(4.5 if sel else 4)       # selected above the rest
            it = sc.addPath(path, pen); it.setZValue(5.5 if sel else 5)
            self._shape_items += [h, it]
        for (rgb, w, dashed, sel), lines in hidden_groups.items():
            hp = _polyline_path(lines)
            if not hp.isEmpty():
                hc = QColor(*rgb); hc.setAlpha(200)
                hpen = QPen(hc, max(1.0, w - 0.5), Qt.DashLine); hpen.setCosmetic(True)
                it = sc.addPath(hp, hpen); it.setZValue(5); self._shape_items.append(it)
        frame_path = QPainterPath(); frame_path.addRect(sc.sceneRect())
        for g in self.shapes:
            c = QColor(*[int(v) for v in g.get("color", (255, 140, 0))[:3]])
            w = float(g.get("width", 2.0)) + (2.0 if g.get("selected") else 0.0)
            fills = g.get("fills") or []
            fo = float(g.get("fill_opacity", 0.0))
            if fills and fo > 0:
                fc = g.get("fill_color") or g.get("color", (255, 140, 0))
                fpath = _polyline_path(fills); fpath.setFillRule(Qt.OddEvenFill)
                fpath = fpath.intersected(frame_path)          # never paint outside the photo
                if not fpath.isEmpty():
                    it = sc.addPath(fpath, QPen(Qt.NoPen), QBrush(QColor(int(fc[0]), int(fc[1]), int(fc[2]),
                                                                        int(round(255 * fo)))))
                    it.setZValue(3); self._shape_items.append(it)
            if not (g.get("points") or g.get("anchors") or (self.transparent and g.get("hidden_points"))):
                continue
            ls = g.get("label_style") or {}
            sym, r = g.get("symbol", "ring"), float(g.get("point_size", 14.0)) / 2.0
            pts = [(u, v, lab, False) for u, v, lab in (g.get("points") or [])]
            if self.transparent:
                pts += [(u, v, "", True) for u, v in (g.get("hidden_points") or [])]
            for u, v, lab, hidden in pts:
                # constant screen size, the target stays visible (ring); the label is a
                # child so its offset is in screen pixels at any zoom
                e = _SymbolItem(sym, r, c, w, hidden)
                e.setPos(float(u), float(v)); e.setZValue(5)
                sc.addItem(e)
                self._shape_items.append(e)
                if lab:
                    _label_item(str(lab), e, ls, c, r)
            for u, v, lab in g.get("anchors") or []:
                if not lab:
                    continue
                a = _SymbolItem("none", 0.0, c, w, False)
                a.setPos(float(u), float(v)); a.setZValue(6); sc.addItem(a)
                self._shape_items.append(a)
                _label_item(str(lab), a, ls, c, 0.0)

    def _draw_markers(self):
        from PySide6.QtGui import QPen, QBrush
        from PySide6.QtWidgets import QGraphicsItem
        sc = self.view.scene()
        for it in self._overlay:
            sc.removeItem(it)
        self._overlay = []
        if self.path is None or not self.vis.get("markers", True):
            return
        labels = self.vis.get("marker_labels", True)
        # Metashape-like flags: pinned green, placed but not pinned blue,
        # predicted (computed, not stored) grey and dashed
        from PySide6.QtGui import QPolygonF
        col = {"pinned": QColor(40, 220, 60), "placed": QColor(60, 140, 255), "predicted": QColor(190, 190, 190)}
        for label, u, v, kind in self.markers:
            c = col.get(kind, col["predicted"])
            pen = QPen(c, 3.5 if label == self.active_marker else 3); pen.setCosmetic(True)
            if kind == "predicted":
                pen.setStyle(Qt.DashLine)
            halo = QPen(QColor(0, 0, 0, 170), pen.widthF() + 2); halo.setCosmetic(True)
            r = 9
            items = []
            # pixel centre of the top-left pixel is (0.5, 0.5) in photo coordinates;
            # the flag is drawn at constant screen size (ignores the zoom)
            for pn, z in ((halo, 6), (pen, 7)):
                parts = [sc.addEllipse(-r, -r, 2 * r, 2 * r, pn),
                         sc.addLine(-2 * r, 0, -4, 0, pn), sc.addLine(4, 0, 2 * r, 0, pn),
                         sc.addLine(0, -2 * r, 0, -4, pn), sc.addLine(0, 4, 0, 2 * r, pn),
                         sc.addLine(0, -2 * r, 0, -2 * r - 14, pn)]                 # flag pole
                for it in parts:
                    it.setPos(u, v); it.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); it.setZValue(z)
                items += parts
            flag = sc.addPolygon(QPolygonF([QPointF(0, -2 * r - 14), QPointF(11, -2 * r - 10), QPointF(0, -2 * r - 6)]),
                                 pen, QBrush(c if kind != "predicted" else QColor(0, 0, 0, 0)))
            flag.setPos(u, v); flag.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); flag.setZValue(7)
            self._overlay += items + [flag]
            if not labels:
                continue
            txt = str(label) + {"pinned": "", "predicted": "  (predicted)"}.get(kind, "  (not pinned)")
            t = sc.addSimpleText(txt)
            f = t.font(); f.setBold(True); f.setPointSizeF(max(10.0, f.pointSizeF())); t.setFont(f)
            t.setBrush(QBrush(c)); t.setPen(QPen(QColor(0, 0, 0, 200), 0.5))
            t.setPos(u, v); t.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); t.setZValue(8)
            t.setTransform(t.transform().translate(14, -2 * r - 16))
            self._overlay.append(t)

    def _context(self, pos):
        from PySide6.QtWidgets import QMenu
        if self.path is None:
            return
        p = self.view.mapToScene(pos)
        menu = QMenu(self)
        a = menu.addAction("Add Marker")
        a.setToolTip("New marker pinned here, predicted on the other photos (Ctrl+click)")
        a.triggered.connect(lambda: self.addMarker.emit(p.x(), p.y()))
        if self.active_marker:
            a = menu.addAction(f"Place Marker '{self.active_marker}' Here")
            a.triggered.connect(lambda: self.placeMarker.emit(p.x(), p.y()))
        near = self.markers_near(p.x(), p.y())
        if near:
            menu.addSeparator()
        from .marker_style import marker_menu
        for m in near:
            marker_menu(menu, m[0], lambda action, lab: self.markerAction.emit(action, lab))
            if m[3] != "predicted":
                a = menu.addAction(f"Remove Marker '{m[0]}' from this Photo")
                a.triggered.connect(lambda _=False, lab=m[0]: self.removeMarker.emit(lab))
        menu.exec(self.view.mapToGlobal(pos))

    def markers_near(self, x, y, px=40):
        """Flags within `px` screen pixels of photo point (x, y), at any zoom
        (none while Show Markers is off: hidden flags can't be picked)."""
        if not self.vis.get("markers", True):
            return []
        s = max(abs(self.view.transform().m11()), 1e-9)
        return [m for m in self.markers if (m[1] - x) ** 2 + (m[2] - y) ** 2 < (px / s) ** 2]

    def zoom(self, f):
        self.view.scale(f, f)


from .orthoview import OrthoView  # noqa: E402,F401  (Ortho tab, see orthoview.py)
