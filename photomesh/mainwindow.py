"""Main application window."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

import numpy as np
from PySide6.QtCore import Qt, QSettings, QStandardPaths, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QFileDialog, QMessageBox, QStyle,
    QToolBar, QInputDialog, QProgressDialog, QApplication, QLabel, QProgressBar)

from . import APP_NAME, APP_VERSION, PROJECT_EXT
from . import colmap as cm
from .dialogs import (AlignPhotosDialog, BuildPointCloudDialog, BuildModelDialog,
                      BuildTextureDialog, BuildTiledModelDialog, ProgressDialog, PreferencesDialog,
                      BuildDEMDialog, BuildOrthomosaicDialog, ExportRasterDialog)
from .menus import MENUS
from .panes import (WorkspacePane, ReferencePane, MarkersPane, PhotosPane, ConsolePane,
                    PropertiesPane, JobsPane, workspace_panel)
from . import procmeta
from .plyio import load_ply, save_ply, ply_counts, vertex_normals
from .project import Project, Chunk, IMAGE_EXTS, data_folder
from .views import ModelView, PhotoView, OrthoView, LAYERS
from .icons import layer_icon
from . import diag
from .bgjob import BackgroundJob, run_modal

_log = logging.getLogger("photomesh")

# layer key -> Model > Show/Hide Items action
LAYER_ACTIONS = {"tie": "Show Tie Points", "cloud": "Show Point Cloud", "mesh": "Show Model",
                 "tiled": "Show Tiled Model", "ortho": "Show Orthomosaic", "dem": "Show DEM",
                 "cameras": "Show Cameras", "camera_labels": "Show Camera Labels",
                 "markers": "Show Markers", "marker_labels": "Show Marker Labels", "shapes": "Show Shapes",
                 "grid": "Show Grid", "region": "Show Region", "trackball": "Show Trackball"}
from .reference_ui import ReferenceMixin
from .shapes_ui import ShapesMixin
from .markers_ui import MarkerEditMixin

SHORTCUTS = {"New": QKeySequence.New, "Open...": QKeySequence.Open, "Save": QKeySequence.Save,
             "Save As...": QKeySequence.SaveAs, "Exit": QKeySequence.Quit,
             "Undo": QKeySequence.Undo, "Redo": QKeySequence.Redo,
             "Zoom In": "Ctrl++", "Zoom Out": "Ctrl+-", "Reset View": "0",
             "Full Screen": "F11", "Contents": "F1", "Open Next Photo": "Page Down",
             "Open Previous Photo": "Page Up"}

PROJECT_FILTER = f"{APP_NAME} project (*{PROJECT_EXT})"



def window_title(path, modified=False):
    """'PhotoMesh Studio 0.1.7 - <project>' (Untitled while unsaved; * = unsaved changes)."""
    name = os.path.splitext(path.replace("\\", "/").rsplit("/", 1)[-1])[0] if path else "Untitled"
    return f"{APP_NAME} {APP_VERSION} - {name}{'*' if modified else ''}"

class MainWindow(ReferenceMixin, MarkerEditMixin, ShapesMixin, QMainWindow):
    _viewData = Signal(int, object)          # generation, loaded arrays (from the loader thread)
    _texData = Signal(int, object)           # generation, textured model display data (or the error)
    _viewProgress = Signal(int, str)

    def __init__(self):
        super().__init__()
        self._view_gen = 0
        self._view_thread = None
        self.view_loading = False
        self.project = Project()
        self.actions: dict[str, QAction] = {}
        self.current_photo = -1
        self.runner = None
        self.resize(1500, 920)
        self._build_central()
        self._build_docks()
        self._shapes_init()
        self._marker_init()
        self._build_menus()
        self._build_toolbar()
        self.statusBar().showMessage("Ready")
        self.view_progress = QProgressBar(); self.view_progress.setRange(0, 0)
        self.view_progress.setMaximumWidth(260); self.view_progress.setTextVisible(True)
        self.view_progress.hide()
        self.statusBar().addPermanentWidget(self.view_progress)
        self._viewData.connect(self._apply_view_data)
        self._texData.connect(self._apply_texture)
        self._viewProgress.connect(self._show_view_progress)
        self.ortho_view.addMarkerRequested.connect(lambda X, Y, Z: self.add_marker_at_ortho(X, Y, Z))
        self.ortho_view.coordinatesChanged.connect(lambda t: self.statusBar().showMessage(t) if t else
                                                   self.statusBar().clearMessage())
        from PySide6.QtWidgets import QToolButton
        self.thumb_progress = QProgressBar(); self.thumb_progress.setMaximumWidth(220)
        self.thumb_progress.setFormat("Thumbnails %v/%m"); self.thumb_progress.hide()
        self.thumb_cancel = QToolButton(); self.thumb_cancel.setText("\u2715"); self.thumb_cancel.setAutoRaise(True)
        self.thumb_cancel.setToolTip("Stop creating thumbnails (they are made again when needed)")
        self.thumb_cancel.clicked.connect(self.cancel_thumbnails); self.thumb_cancel.hide()
        self.statusBar().addPermanentWidget(self.thumb_progress)
        self.statusBar().addPermanentWidget(self.thumb_cancel)
        self._thumb_job = None
        apply_dialog_setting()
        from .busy import BusyIndicator
        self.busy_indicator = BusyIndicator(self.statusBar())
        self.colmap_label = QLabel()
        self.statusBar().addPermanentWidget(self.colmap_label)
        self._update_colmap_label()
        self._start_resource_monitor()
        self.refresh_all()
        self.log(f"{APP_NAME} {APP_VERSION} started. COLMAP: {cm.find_colmap() or 'not found'}")

    # ------------------------------------------------------------------ UI
    def _build_central(self):
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.model_view = ModelView()
        self.ortho_view = OrthoView()
        self.photo_view = PhotoView()
        self.tabs.addTab(self.model_view, "Model")
        self.tabs.addTab(self.ortho_view, "Ortho")
        self.tabs.addTab(self.photo_view, "Photo")
        self.setCentralWidget(self.tabs)

    def _dock(self, title, widget, area, name):
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self):
        self.workspace = WorkspacePane()
        self.reference = ReferencePane()
        self.markers_pane = MarkersPane()
        self.photos = PhotosPane()
        self.console = ConsolePane({"app": QApplication.instance(), "window": self,
                                    "project": self.project, "np": np})
        self.properties = PropertiesPane()
        self.jobs = JobsPane()
        from .report_ui import ErrorReporter
        self.reporter = ErrorReporter(self)
        L, B = Qt.LeftDockWidgetArea, Qt.BottomDockWidgetArea
        self.docks = {
            "Workspace": self._dock("Workspace", self._workspace_panel(), L, "ws"),
            "Reference": self._dock("Reference", self.reference, L, "ref"),
            "Markers": self._dock("Markers", self.markers_pane, L, "markers"),
            "Properties": self._dock("Properties", self.properties, L, "props"),
            "Photos": self._dock("Photos", self.photos, B, "photos"),
            "Console": self._dock("Console", self.console, B, "console"),
            "Jobs": self._dock("Jobs", self.jobs, B, "jobs"),
        }
        # Properties below the Workspace tree (Metashape layout), Reference/Markers tabbed with Workspace
        self.splitDockWidget(self.docks["Workspace"], self.docks["Properties"], Qt.Vertical)
        self.tabifyDockWidget(self.docks["Workspace"], self.docks["Reference"])
        self.tabifyDockWidget(self.docks["Reference"], self.docks["Markers"])
        self.docks["Workspace"].raise_()
        self.tabifyDockWidget(self.docks["Photos"], self.docks["Console"])
        self.tabifyDockWidget(self.docks["Console"], self.docks["Jobs"])
        self.docks["Photos"].raise_()
        self.docks["Jobs"].hide()
        self.resizeDocks([self.docks["Workspace"]], [330], Qt.Horizontal)
        self.resizeDocks([self.docks["Workspace"], self.docks["Properties"]], [420, 220], Qt.Vertical)
        self.resizeDocks([self.docks["Photos"]], [230], Qt.Vertical)
        self.workspace.cameraActivated.connect(lambda ci, k: (self.set_active(ci), self.open_photo(k)))
        self.workspace.chunkActivated.connect(self.set_active)
        self.workspace.contextAction.connect(self._workspace_action)
        self.workspace.selectionInfo.connect(self._show_properties)
        self.workspace.layerToggled.connect(self.set_layer_visible)
        self.workspace.showRequested.connect(self._show_requested)
        self.workspace.markerActivated.connect(self._goto_marker)
        self.markers_pane.markerActivated.connect(self._goto_marker)
        self.photos.photoActivated.connect(self.open_photo)
        self.markers_pane.markerToggled.connect(self.toggle_marker)
        self.markers_pane.itemSelectionChanged.connect(self._update_photo_markers)
        self.photo_view.placeMarker.connect(self.place_marker)
        self.photo_view.removeMarker.connect(self.remove_marker_projection)
        self.photo_view.addMarker.connect(lambda u, v: self.add_marker_at_photo(u, v))
        self.photo_view.pinMarker.connect(lambda lab, u, v: self.pin_marker(lab, u, v))
        self.markers_pane.markerEdited.connect(self._marker_edited)
        self.markers_pane.markerAction.connect(self._marker_pane_action)
        self.model_view.addMarkerRay.connect(lambda o, d: self.add_marker_at_ray(o, d))

    def _workspace_panel(self):
        def mk(text, icon, fn):
            a = QAction(layer_icon(icon), text, self)
            a.setToolTip(text); a.triggered.connect(lambda _=False: self._safe(text, fn)())
            return a
        self.ws_actions = {
            "add_chunk": mk("Add Chunk", "add_chunk", lambda: self._workspace_action("Add Chunk", -1)),
            "add_photos": mk("Add Photos", "add_photos", lambda: self.add_photos()),
            "add_folder": mk("Add Folder", "add_folder", lambda: self.add_folder()),
            "enable": mk("Enable/Disable selected cameras", "enable", self._ws_toggle_selected),
            "remove": mk("Remove selected items", "remove", self._ws_remove_selected),
        }
        a = self.ws_actions
        self.workspace_widget = workspace_panel(self.workspace, [a["add_chunk"], a["add_photos"], a["add_folder"],
                                                                 None, a["enable"], a["remove"]])
        return self.workspace_widget

    def _ws_toggle_selected(self):
        sel = [r for r in self.workspace.selected_roles() if r[0] == "camera"]
        if not sel:
            self.statusBar().showMessage("Select cameras in the Workspace first", 4000); return
        for _kind, ci, k in sel:
            c = self.project.chunks[ci].cameras[k]; c.enabled = not c.enabled
        self.set_modified()

    def _ws_remove_selected(self):
        roles = self.workspace.selected_roles()
        cams = sorted(((r[1], r[2]) for r in roles if r[0] == "camera"), reverse=True)
        chunks = sorted({r[1] for r in roles if r[0] == "chunk"}, reverse=True)
        if not cams and not chunks:
            self.statusBar().showMessage("Select cameras or a chunk to remove", 4000); return
        what = f"{len(cams)} camera(s)" if cams else f"{len(chunks)} chunk(s)"
        if QMessageBox.question(self, "Remove", f"Remove {what} from the project?") != QMessageBox.Yes:
            return
        for ci, k in cams:
            del self.project.chunks[ci].cameras[k]
        for ci in chunks:
            if len(self.project.chunks) > 1:
                del self.project.chunks[ci]
        self.project.active = max(0, min(self.project.active, len(self.project.chunks) - 1))
        self.set_modified(); self.reload_view()

    def _show_requested(self, kind, ci):
        if ci != self.project.active:
            self.set_active(ci)
        if kind == "tie":
            self.show_layer("tie")
        elif kind in ("cloud", "depth"):
            self.show_layer("cloud", solid=self.model_view.solid.get("cloud", False))
        elif kind == "model":
            self.show_model_mode(self.model_view.mesh_mode)
        elif kind == "tiled":
            self.show_tiled_mode(self.model_view.tiled_mode)
        elif kind in ("ortho", "dem"):
            self.show_ortho(kind)
        elif kind in ("markers", "cameras"):
            self.set_layer_visible(kind, True)
            self.tabs.setCurrentWidget(self.model_view)
        elif kind == "shapes":
            self.set_layer_visible("shapes", True)
            if self.tabs.currentWidget() is self.photo_view and self.photo_view.path:
                return
            self.tabs.setCurrentWidget(self.ortho_view if self.ortho_view.has_raster() else self.model_view)

    def _goto_marker(self, label):
        """Open the first photo where the marker is placed (pinned first)."""
        ms = [m for m in (self.chunk.markers or []) if m["label"] == label]
        if not ms:
            return
        for r in range(self.markers_pane.rowCount()):
            if self.markers_pane.item(r, 0) and self.markers_pane.item(r, 0).text() == label:
                self.markers_pane.selectRow(r); break
        proj = ms[0].get("projections") or {}
        order = sorted(proj, key=lambda n: not (len(proj[n]) < 3 or proj[n][2]))
        stems = {os.path.splitext(c.label)[0].lower(): k for k, c in enumerate(self.chunk.cameras)}
        for n in order:
            k = stems.get(os.path.splitext(n)[0].lower())
            if k is not None:
                self.open_photo(k); return
        self.statusBar().showMessage(f"Marker {label} has no projections", 4000)

    def _handlers(self):
        h = {
            "New": self.new_project, "Open...": self.open_project, "Append...": self.append_project,
            "Save": self.save_project, "Save As...": self.save_project_as, "Exit": self.close,
            "Export Point Cloud...": lambda: self.export_ply("cloud"),
            "Export Model...": self.export_model,
            "Export Tiled Model...": self.export_tiled_model,
            "Build Tiled Model...": self.build_tiled_model,
            "Tiled Model - Textured": lambda: self.show_tiled_mode("textured"),
            "Tiled Model - Solid": lambda: self.show_tiled_mode("solid"),
            "Tiled Model - Wireframe": lambda: self.show_tiled_mode("wireframe"),
            "Tiled Model - Elevation": lambda: self.show_tiled_mode("elevation"),
            "Export Cameras...": self.export_cameras, "Export Reference": self.export_reference,
            "Generate Report...": self.generate_report,
            "Import Point Cloud...": lambda: self.import_ply("cloud"),
            "Import Model...": lambda: self.import_ply("model"),
            "Import Texture...": self.import_texture,
            "Import Tiled Model...": self.import_tiled_model,
            "Build Orthomosaic...": self.build_orthomosaic, "Build DEM...": self.build_dem,
            "Export Orthomosaic...": lambda: self.export_raster("ortho"),
            "Export DEM...": lambda: self.export_raster("dem"),
            "Import Orthomosaic...": lambda: self.import_raster("ortho"),
            "Import DEM...": lambda: self.import_raster("dem"),
            "Orthomosaic": lambda: self.show_ortho("ortho"),
            "DEM - Elevation": lambda: self.show_ortho("dem"),
            "Zoom In": lambda: self.zoom(1 / 1.25), "Zoom Out": lambda: self.zoom(1.25),
            "Reset View": self.reset_view, "Capture View": self.capture_view,
            "Full Screen": self.toggle_full_screen,
            "Add Photos...": self.add_photos, "Add Folder...": self.add_folder,
            "Align Photos...": self.align_photos, "Build Point Cloud...": self.build_point_cloud,
            "Build Model...": self.build_model, "Build Texture...": self.build_texture,
            "Merge Chunks...": self.merge_chunks,
            "Import Markers...": self.import_markers,
            "Import Shapes...": lambda: self.import_shapes(), "Export Shapes...": lambda: self.export_shapes(),
            "Remove Shapes": lambda: self.remove_shapes(), "Measure Shape...": lambda: self.measure_shape(),
            "Shape Settings...": lambda: self.open_shape_style(),
            "Transparent View": lambda on=True: self.set_transparency(bool(on)),
            "Export Markers...": self.export_markers,
            "Optimize Cameras...": self.optimize_cameras, "Refine Markers": self.refine_markers,
            "Add Marker": self.add_marker,
            "Undo": self.undo_markers, "Redo": self.redo_markers,
            "Filter Point Cloud...": lambda: self.filter_point_cloud(False),
            "Filter by Confidence...": lambda: self.filter_point_cloud(True),
            "Gradual Selection...": self.gradual_selection,
            "Show All": lambda: self.set_all_layers(True), "Hide All": lambda: self.set_all_layers(False),
            "Show Labels": lambda on=True: (self.set_layer_visible("marker_labels", on),
                                            self.set_layer_visible("camera_labels", on)),
            "Change Image Paths...": self.change_image_paths,
            "Clean Up Intermediate Data...": self.clean_up_data,
            "Open Log Folder": self.open_log_folder,
            "Report a Problem...": self.report_problem,
            "Perspective/Orthographic": self.model_view.set_orthographic,
            "Model - Shaded": lambda: self.show_model_mode("shaded"),
            "Model - Solid": lambda: self.show_model_mode("solid"),
            "Model - Textured": lambda: self.show_model_mode("textured"),
            "Model - Wireframe": lambda: self.show_model_mode("wireframe"),
            "Tie Points - Colors": lambda: self.show_layer("tie"),
            "Point Cloud - Colors": lambda: self.show_layer("cloud"),
            "Point Cloud - Solid": lambda: self.show_layer("cloud", solid=True),
            "Open Next Photo": lambda: self.step_photo(1),
            "Open Previous Photo": lambda: self.step_photo(-1),
            "Rotate Right": lambda: self.rotate_photo(90), "Rotate Left": lambda: self.rotate_photo(-90),
            "View Model Statistics...": self.model_statistics,
            "Run Script...": self.run_script, "Preferences...": self.preferences,
            "Contents": self.help_contents,
            "About PhotoMesh Studio...": self.about,
        }
        for key, name in LAYER_ACTIONS.items():
            h[name] = (lambda k: lambda on=True: self._layer_toggled(k, on))(key)
        for v in ("Top", "Bottom", "Front", "Back", "Left", "Right"):
            h[v] = (lambda vv: lambda: self.model_view.set_predefined(vv))(v)
        return h

    def _build_menus(self):
        handlers = self._handlers()
        self.unhandled_actions = []
        mb = self.menuBar()
        for title, items in MENUS:
            self._fill_menu(mb.addMenu(title), items, handlers, title.strip("&"))
        # panes / toolbar toggles reflect dock visibility
        for name, dock in self.docks.items():
            a = self.actions.get(f"View/{name}")
            if a:
                a.setChecked(dock.isVisible())
                a.toggled.connect(dock.setVisible)
                dock.visibilityChanged.connect(lambda vis, a=a, d=dock: a.setChecked(not d.isHidden()))
        # layer toggles: restore last state, mirror them in View > Layers
        st = QSettings()
        view_menu = next(a.menu() for a in mb.actions() if a.text().strip("&") == "View")
        layers_menu = view_menu.addMenu("Layers")
        for key, label, default in LAYERS:
            a = self.actions[f"Model/{LAYER_ACTIONS[key]}"]
            a.setIcon(layer_icon(key))
            a.setToolTip(f"Show/hide {label.lower()}  (Model > Show/Hide Items > {LAYER_ACTIONS[key]})")
            on = st.value(f"view/show_{key}", default, bool)
            a.blockSignals(True); a.setChecked(on); a.blockSignals(False)
            self.model_view.set_visible(key, on)
            layers_menu.addAction(a)
        self.workspace.vis = {k: self.layer_action(k).isChecked() for k in LAYER_ACTIONS}
        layers_menu.addSeparator()
        layers_menu.addAction(self.actions["Model/Show All"]); layers_menu.addAction(self.actions["Model/Hide All"])

    def _fill_menu(self, menu, items, handlers, top):
        for it in items:
            if it == "-":
                menu.addSeparator()
            elif isinstance(it, tuple):
                self._fill_menu(menu.addMenu(it[0]), it[1], handlers, top)
            else:
                checkable = it.endswith("*")
                name = it.rstrip("*")
                a = QAction(name, self)
                a.setCheckable(checkable)
                if name in SHORTCUTS and top in ("File", "Edit", "View", "Workflow", "Help", "Photo"):
                    a.setShortcut(QKeySequence(SHORTCUTS[name]))
                fn = handlers.get(name)
                if top in ("Photo", "Ortho") and name == "Show Shapes":
                    # mirrors Model > Show/Hide Items > Show Shapes (one layer for all views)
                    fn = lambda on=True: self.set_layer_visible("shapes", on)
                    self._shape_mirrors = getattr(self, "_shape_mirrors", []) + [a]
                if fn is not None:
                    fn = self._safe(name, fn)
                    if checkable:
                        a.toggled.connect(fn)
                    else:
                        a.triggered.connect(lambda _=False, f=fn: f())
                elif top == "View" and name in self.docks or name == "Toolbar":
                    pass
                else:
                    # every menu entry must do something (0.1.12: no "not implemented" placeholders)
                    _log.warning("menu item without a handler: %s > %s", top, name)
                    self.unhandled_actions.append(f"{top}/{name}")
                    a.setEnabled(False)
                menu.addAction(a)
                self.actions.setdefault(f"{top}/{name}", a)
                self.actions.setdefault(name, a)

    def _build_toolbar(self):
        """One compact row of 16 px icon buttons (Metashape-like). Grouped modes
        (selection tools, point cloud / model display mode, extra show/hide
        items) are buttons with a dropdown arrow; the last used entry becomes
        the button's action."""
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QToolButton, QMenu
        tb = QToolBar("Toolbar"); tb.setObjectName("toolbar")
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        from .icons import TOOLBAR_ICON, MODE_ICON
        tb.setIconSize(QSize(TOOLBAR_ICON, TOOLBAR_ICON))
        tb.setStyleSheet("QToolBar{spacing:1px;padding:1px;} QToolButton{padding:2px;margin:0px;}")
        self.addToolBar(tb)
        self.main_toolbar = tb
        st = self.style()

        def act(name, icon, tip=None):
            a = self.actions[name]
            a.setIcon(icon if not isinstance(icon, (str, QStyle.StandardPixmap)) else
                      (layer_icon(icon) if isinstance(icon, str) else st.standardIcon(icon)))
            a.setToolTip(tip or name.split("/")[-1].rstrip("."))
            return a

        def add(name, icon, tip=None):
            tb.addAction(act(name, icon, tip))

        def drop(names_icons, tip, instant=False, icon=None):
            btn = QToolButton(tb)
            menu = QMenu(btn)
            for n, ic in names_icons:
                if n is None:
                    menu.addSeparator(); continue
                menu.addAction(act(n, ic) if ic is not None else self.actions[n])
            btn.setMenu(menu)
            btn.setToolTip(tip)
            if instant:
                btn.setPopupMode(QToolButton.InstantPopup)
                btn.setIcon(layer_icon(icon))
            else:
                btn.setPopupMode(QToolButton.MenuButtonPopup)
                btn.setDefaultAction(self.actions[names_icons[0][0]])
                menu.triggered.connect(lambda a, b=btn: (b.setDefaultAction(a), b.setToolTip(a.toolTip())))
            tb.addWidget(btn)
            return btn

        add("New", "file_new"); add("Open...", "file_open"); add("Save", "file_save")
        tb.addSeparator()
        add("Undo", "undo"); add("Redo", "redo")
        tb.addSeparator()
        add("Zoom In", "zoom_in"); add("Zoom Out", "zoom_out"); add("Reset View", "reset_view")
        tb.addSeparator()
        add("Add Photos...", "add_photos", "Add Photos")
        add("Align Photos...", "align", "Align Photos")
        add("Build Point Cloud...", "cloud", "Build Point Cloud")
        add("Build Model...", "mesh", "Build Model")
        tb.addSeparator()
        # grouped display modes: larger split buttons (MODE_ICON px icon, short label, wide arrow)
        from .toolbuttons import ModeButton

        def mode(entries, tip):
            btn = ModeButton(tb, [(act(n, ic), lab) for n, ic, lab in entries], tip, icon_size=MODE_ICON)
            tb.addWidget(btn)
            return btn
        self.tie_mode_button = mode([("Tie Points - Colors", "tie", "Tie Points")], "Tie points display mode")
        self.cloud_mode_button = mode([("Point Cloud - Colors", "cloud_colors", "Colors"),
                                       ("Point Cloud - Solid", "cloud_solid", "Solid")], "Point cloud display mode")
        self.model_mode_button = mode([("Model - Shaded", "mode_shaded", "Shaded"),
                                       ("Model - Solid", "mode_solid", "Solid"),
                                       ("Model - Textured", "mode_textured", "Textured"),
                                       ("Model - Wireframe", "mode_wireframe", "Wireframe")], "Model display mode")
        self.mode_buttons = [self.tie_mode_button, self.cloud_mode_button, self.model_mode_button]
        tb.addSeparator()
        for key in ("tie", "cloud", "mesh", "ortho", "dem", "cameras", "markers", "shapes", "region", "grid"):
            tb.addAction(self.layer_action(key))
            if key == "shapes":
                tb.addWidget(self._transparency_button(tb))
        extra = [(f"Model/{LAYER_ACTIONS[k]}", None) for k, _l, _d in LAYERS]
        self.show_items_button = drop(extra + [(None, None), ("Model/Show All", None), ("Model/Hide All", None)],
                                      "Show/Hide Items", instant=True, icon="show_items")
        tb.addSeparator()
        add("Preferences...", "preferences")
        ta = self.actions["View/Toolbar"]
        ta.setChecked(True); ta.toggled.connect(tb.setVisible)

    def _transparency_button(self, tb):
        """Checkable 'Transparent View' button with an opacity slider in its
        dropdown (default 40%); the state is restored at start-up."""
        from PySide6.QtWidgets import QToolButton, QMenu, QWidgetAction, QSlider, QLabel, QWidget, QHBoxLayout
        a = self.actions["Tools/Transparent View"] if "Tools/Transparent View" in self.actions else \
            self.actions["Transparent View"]
        a.setIcon(layer_icon("transparency"))
        a.setShortcut(QKeySequence("Ctrl+Shift+T")); a.setShortcutContext(Qt.ApplicationShortcut)
        a.setToolTip("Transparent view (Ctrl+Shift+T): model, point clouds, photo and orthomosaic semi-transparent, "
                     "so shapes behind or under the surface are visible. Arrow: opacity.")
        self.transparency_action = a
        btn = QToolButton(tb); btn.setDefaultAction(a); btn.setPopupMode(QToolButton.MenuButtonPopup)
        btn.setObjectName("transparency_button")
        menu = QMenu(btn)
        box = QWidget(menu); h = QHBoxLayout(box); h.setContentsMargins(8, 4, 8, 4)
        h.addWidget(QLabel("Surface opacity:"))
        sl = QSlider(Qt.Horizontal); sl.setRange(5, 100); sl.setMinimumWidth(140)
        sl.setToolTip("Opacity of the model, point clouds, photo and orthomosaic in the transparent view")
        val = QLabel("40%"); val.setMinimumWidth(34)
        h.addWidget(sl, 1); h.addWidget(val)
        wa = QWidgetAction(menu); wa.setDefaultWidget(box); menu.addAction(wa)
        btn.setMenu(menu)
        self.transparency_slider, self.transparency_value, self.transparency_button = sl, val, btn
        st = QSettings()
        op = st.value("view/transparent_opacity", 0.4, float)
        sl.setValue(int(round(op * 100)))
        sl.valueChanged.connect(lambda v: self.set_transparency(a.isChecked(), v / 100.0))
        on = st.value("view/transparent", False, bool)
        self.set_transparency(on, op, save=False)
        return btn

    # ------------------------------------------------------------- helpers
    def _safe(self, name, fn):
        """Wrap a command so an exception is reported instead of crashing."""
        def wrapper(*args):
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001
                import traceback
                self.log(f"Error in '{name}': {e}\n{traceback.format_exc()}")
                _log.error("command %r failed", name, exc_info=True)
                self.statusBar().showMessage(f"{name} failed: {e}", 8000)
                self.show_error(f"{name} failed", f"{type(e).__name__}: {e}", step=name,
                                traceback_text=traceback.format_exc())
        return wrapper

    def show_error(self, title, message="", *, worker=True, **kw):
        """The error dialog (with a saved error report) for any failure.
        worker: add the traceback of a worker-thread exception from the last
        30 s (run_modal / BackgroundJob only pass the error text on)."""
        if worker and not kw.get("traceback_text"):
            from .bgjob import recent_error
            r = recent_error()
            if r:
                kw["traceback_text"] = f"[worker '{r['name']}']\n{r['traceback']}"
        kw.setdefault("mark", getattr(self, "_step_mark", None))
        return self.reporter.show_error(title, message, **kw)

    def report_problem(self):
        """Help > Report a Problem...: a report of the current session on demand."""
        from . import diag, report as rp
        sl = diag.log_path()
        lines = rp.tail_lines(sl) if sl else self.console.lines_since(0, rp.LOG_LINES)
        return self.reporter.show_error("Problem report", "", kind="manual", manual=True, log_lines=lines)

    def log(self, text):
        self.console.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")
        _log.info("%s", text)

    def _start_resource_monitor(self):
        """CPU / GPU load in the status bar (background thread, ~1.5 s; GPU hidden without NVIDIA NVML)."""
        from .statusmon import ResourceMonitor
        self.cpu_label = QLabel("CPU  --"); self.gpu_label = QLabel(); self.gpu_label.hide()
        for lab in (self.cpu_label, self.gpu_label):
            lab.setToolTip("System load (updated every 1.5 s)")
            self.statusBar().addPermanentWidget(lab)
        self.monitor = ResourceMonitor(parent=self)
        self.monitor.sampled.connect(self._show_resources)
        stop = self.monitor._stop
        self.destroyed.connect(lambda *_: stop.set())
        self.monitor.start()

    def _show_resources(self, d):
        from .statusmon import format_cpu, format_gpu
        self.cpu_label.setText(format_cpu(d.get("cpu")))
        if d.get("gpu") is None:
            self.gpu_label.hide()
        else:
            self.gpu_label.setText(format_gpu(d["gpu"], d.get("gpu_mem")))
            if d.get("gpu_name"):
                self.gpu_label.setToolTip(f"{d['gpu_name']}: GPU load and memory (NVIDIA NVML)")
            self.gpu_label.show()

    def _update_colmap_label(self):
        exe = cm.find_colmap()
        self.colmap_label.setText(f"COLMAP: {'found' if exe else 'not found'}"
                                  + (f" ({cm.gpu_status()})" if exe else ""))
        self.colmap_label.setToolTip(exe or "Install COLMAP and add it to PATH, or set it in Tools > Preferences")

    @property
    def chunk(self) -> Chunk:
        return self.project.chunk

    def refresh_all(self):
        self.workspace.refresh(self.project)
        self.reference.refresh(self.chunk)
        self.markers_pane.refresh(self.chunk)
        self.photos.refresh(self.chunk)
        self._update_title()

    def _update_title(self):
        self.setWindowTitle(window_title(self.project.path, self.project.modified))

    def reload_view(self):
        """Show the active chunk's results in the 3D and ortho views.

        Cameras appear at once; the PLYs (tie points, a display-decimated
        point cloud, the mesh + its normals) and marker positions are loaded
        in a background thread with a progress bar in the status bar, so the
        window stays responsive while a 28M-point project opens."""
        self.model_view.clear()
        ch = self.chunk
        self.model_view.set_z_up(bool(ch.georef))
        cams = [(c.est, c.rot) for c in ch.cameras if c.aligned and c.est and c.rot]
        if cams and self.model_view.gl is not None:
            self.model_view.fit_raw(np.array([c[0] for c in cams], np.float32))
        self.model_view.set_cameras(cams, labels=[c.label for c in ch.cameras if c.aligned and c.est and c.rot])
        self.ortho_view.set_data(None, [c.est for c in ch.cameras if c.est])
        self.load_ortho_view()
        self.selected_shape = None
        try:
            self.update_shape_views()
            self._start_occlusion_scene()
        except Exception as e:  # noqa: BLE001
            self.log(f"Shapes: {e}")
        self._view_gen += 1
        gen = self._view_gen
        jobs = [(key, path) for key, path in (("tie", ch.tie_points_ply), ("cloud", ch.point_cloud_ply),
                                              ("mesh", ch.model_ply)) if path and os.path.exists(path)]
        want_markers = bool(ch.markers)
        want_stats = bool(ch.tie_point_count) and "sparse_stats" not in (ch.meta or {}) and \
            os.path.exists(os.path.join(self._model_dir(ch), "points3D.bin"))
        if not jobs and not want_markers and not want_stats:
            self.view_loading = False; self.view_progress.hide()
            return
        self.view_loading = True
        self._show_view_progress(gen, "Loading view...")
        import threading

        def emit(sig, *a):
            try:
                sig.emit(*a)
            except RuntimeError:                 # window already destroyed
                pass

        def work():
            out = {"errors": []}
            for key, path in jobs:
                emit(self._viewProgress, gen, {"tie": "Loading tie points...", "cloud": "Loading point cloud...",
                                              "mesh": "Loading model..."}[key])
                try:
                    d = load_ply(path)
                    if key == "mesh" and d["faces"] is not None:
                        d["normals"] = vertex_normals(d["points"], d["faces"])
                    out[key] = d
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"Cannot read {path}: {e}")
                if gen != self._view_gen:
                    return                       # superseded by a newer reload
            if want_stats:
                emit(self._viewProgress, gen, "Tie point statistics...")
                try:
                    model = self._sparse_model()
                    if model is not None:
                        out["sparse_stats"] = procmeta.sparse_stats(model)
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"Tie point statistics: {e}")
            if want_markers:
                emit(self._viewProgress, gen, "Locating markers...")
                try:
                    out["markers"] = self._marker_positions()
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"Markers: {e}")
            emit(self._viewData, gen, out)

        self._view_thread = threading.Thread(target=work, name="view-loader", daemon=True)
        self._view_thread.start()

    def _show_view_progress(self, gen, text):
        if gen == self._view_gen:
            self.view_progress.setFormat(text); self.view_progress.show()
            self.statusBar().showMessage(text)

    def _apply_view_data(self, gen, out):
        if gen != self._view_gen:
            return
        for e in out["errors"]:
            self.log(e)
        pts = None
        for key in ("tie", "cloud"):
            d = out.get(key)
            if d is not None:
                self.model_view.set_points(key, d["points"], d["colors"], 2.0 if key == "cloud" else 3.0)
                pts = d["points"]
        d = out.get("mesh")
        if d is not None and d["faces"] is not None:
            self.model_view.set_mesh(d["points"], d["faces"], d["colors"], d.get("normals"))
        ch = self.chunk
        cams = [c.est for c in ch.cameras if c.aligned and c.est and c.rot]
        if cams and self.model_view.gl is not None:
            centres = np.array(cams, np.float32)
            self.model_view.fit_raw(centres if pts is None else np.vstack([pts, centres]))
            al = [c for c in ch.cameras if c.aligned and c.est and c.rot]
            self.model_view.set_cameras([(c.est, c.rot) for c in al], labels=[c.label for c in al])
        self.ortho_view.set_data(pts, [c.est for c in ch.cameras if c.est])
        if "markers" in out:
            self._show_markers_3d(out["markers"])
        if out.get("sparse_stats"):
            # derived data (recomputable from the sparse model): kept with the
            # project on the next save, but opening a project doesn't make it "modified"
            ch.meta = dict(ch.meta or {}, sparse_stats=out["sparse_stats"])
            self.workspace.refresh(self.project); self._update_title()
            self._update_photo_markers()
        if ch.texture:
            self.load_texture_view()
        if ch.tiled_model:
            self.load_tiled_view()
        self.view_loading = False
        self.view_progress.hide()
        self.statusBar().showMessage("Ready", 2000)

    def wait_view_loaded(self, timeout=60.0):
        """Block (processing events) until the background view load is done."""
        t0 = time.time()
        while self.view_loading and time.time() - t0 < timeout:
            QApplication.processEvents(); time.sleep(0.01)
        return not self.view_loading

    def set_modified(self):
        self.project.modified = True
        self.refresh_all()

    def set_active(self, ci):
        if 0 <= ci < len(self.project.chunks) and ci != self.project.active:
            self.project.active = ci
            self.refresh_all(); self.reload_view()

    # ------------------------------------------------------- layer visibility
    def layer_action(self, key):
        return self.actions[f"Model/{LAYER_ACTIONS[key]}"]

    def _layer_toggled(self, key, on):
        """Single sink for every way of toggling a layer (menu, toolbar, Workspace checkbox)."""
        on = bool(on)
        if key in ("ortho", "dem", "markers", "marker_labels", "shapes"):
            self.ortho_view.set_visible(key, on)
        if key in ("markers", "marker_labels") and getattr(self, "photo_view", None) is not None:
            self.photo_view.set_visible(key, on)
        if key == "shapes":
            for m in getattr(self, "_shape_mirrors", []):
                if m.isChecked() != on:
                    m.blockSignals(True); m.setChecked(on); m.blockSignals(False)
            try:
                self._update_photo_shapes()
            except Exception as e:  # noqa: BLE001
                self.log(f"Shapes overlay on the photo: {e}")
        if key not in ("ortho", "dem"):
            self.model_view.set_visible(key, on)
        QSettings().setValue(f"view/show_{key}", on)
        self.workspace.set_layer_states({key: on})

    def set_layer_visible(self, key, on):
        a = self.layer_action(key)
        if a.isChecked() != bool(on):
            a.setChecked(bool(on))                # emits toggled -> _layer_toggled
        else:
            self._layer_toggled(key, on)

    def layer_states(self):
        return {k: self.layer_action(k).isChecked() for k in LAYER_ACTIONS}

    def set_all_layers(self, on):
        for k in LAYER_ACTIONS:
            self.set_layer_visible(k, on)

    def _has_layer(self, key):
        ch = self.chunk
        path = {"tie": ch.tie_points_ply, "cloud": ch.point_cloud_ply, "mesh": ch.model_ply,
                "tiled": (ch.tiled_model or {}).get("index"), "ortho": (ch.orthomosaic or {}).get("path"),
                "dem": (ch.dem or {}).get("path"), "shapes": (ch.shapes or {}).get("path")}[key]
        if key in ("ortho", "dem"):
            return bool(path and os.path.exists(path))
        return bool(path and os.path.exists(path)) or self.model_view.has_layer(key)

    def _solo(self, key):
        """Like Metashape's view-mode buttons: show one data layer, hide the others."""
        for k in ("tie", "cloud", "mesh", "tiled"):
            self.set_layer_visible(k, k == key)
        self.tabs.setCurrentWidget(self.model_view)

    def show_layer(self, key, solid=False):
        """Tie Points - Colors / Point Cloud - Colors|Solid. Reuses the arrays
        loaded (display-decimated) by the background view loader; never reads
        the PLY again on the GUI thread."""
        self._sync_mode_button("Tie Points - Colors" if key == "tie" else
                               f"Point Cloud - {'Solid' if solid else 'Colors'}")
        if not self._has_layer(key):
            self.statusBar().showMessage("Nothing to show - build it first", 4000); return
        self.model_view.set_solid(key, solid)
        self._solo(key)

    def _sync_mode_button(self, action_name):
        a = self.actions.get(action_name)
        for b in getattr(self, "mode_buttons", []):
            b.set_current(a)

    def show_model_mode(self, mode):
        """Model - Shaded/Solid/Textured/Wireframe: show the mesh only."""
        self._sync_mode_button(f"Model - {mode.capitalize()}")
        self.model_view.set_mesh_mode(mode)
        if not self._has_layer("mesh"):
            self.statusBar().showMessage("No model - build it first", 4000); return
        self._solo("mesh")

    def _show_properties(self, info):
        ch = self.project.chunks[info["chunk"]]
        kind = info["kind"]
        if kind == "camera":
            c = ch.cameras[info["index"]]
            rows = [("Label", c.label), ("Path", c.path), ("Size", f"{c.width} x {c.height}"),
                    ("Enabled", c.enabled), ("Aligned", c.aligned), ("Longitude", c.lon),
                    ("Latitude", c.lat), ("Altitude", c.alt), ("Estimated", c.est)]
        elif kind == "marker":
            m = next((m for m in ch.markers or [] if m["label"] == info["index"]), {})
            row = next((r for r in (ch.georef or {}).get("rows") or [] if r.get("label") == info["index"]), {})
            rows = [("Label", m.get("label")), ("Type", "control" if m.get("enabled", True) else "check"),
                    ("Reference", m.get("ref")), ("Projections", len(m.get("projections") or {})),
                    ("Error (m)", procmeta.fmt(row.get("total"))), ("Error (pix)", procmeta.fmt(row.get("pix")))]
        elif kind in ("shapes", "shape_layer", "shape"):
            rows = self._shape_properties(info, ch)
        else:
            rows = procmeta.properties(ch, {"group": "cameras"}.get(kind, kind)) or \
                procmeta.properties(ch, "chunk")
        self.properties.show_props({k: ("" if v is None else v) for k, v in rows})

    def _marker_edited(self, label, col, text):
        # applied after the edit signal returns (the table is refilled)
        QTimer.singleShot(0, lambda: self.edit_marker_cell(label, col, text))

    def _workspace_action(self, action, ci):
        if action == "Add Chunk":
            self.project.chunks.append(Chunk(name=f"Chunk {len(self.project.chunks) + 1}"))
            self.project.active = len(self.project.chunks) - 1
        elif action == "Set as Active Chunk":
            self.set_active(ci)
        elif action == "Rename Chunk...":
            name, ok = QInputDialog.getText(self, "Rename Chunk", "Name:", text=self.project.chunks[ci].name)
            if ok and name:
                self.project.chunks[ci].name = name
        elif action == "Duplicate Chunk":
            import copy
            c = copy.deepcopy(self.project.chunks[ci]); c.name += " (copy)"
            self.project.chunks.insert(ci + 1, c)
        elif action == "Remove Chunk":
            del self.project.chunks[ci]
            self.project.active = max(0, min(self.project.active, len(self.project.chunks) - 1))
        elif action == "Change Image Paths...":
            self.change_image_paths(ci); return
        elif action.startswith("marker:"):
            if ci != self.project.active:
                self.set_active(ci)
            _t, act, labs = action.split(":", 2)
            labels = [l for l in labs.split("\x1f") if l]
            if labels == ["*"]:
                labels = [m["label"] for m in self.chunk.markers or []]
            self._marker_pane_action({"Remove Marker": "Remove Markers"}.get(act, act), labels)
            return
        elif action.startswith("set_camera:"):
            _t, k, on = action.split(":")
            self.project.chunks[ci].cameras[int(k)].enabled = bool(int(on))
            self.set_modified(); return
        elif action.startswith(("shape_layer:", "show_shape:", "remove_shape_layer:", "measure_shape:",
                                "shape_style:")) or \
                action in ("Export Shapes...", "Remove Shapes"):
            if ci != self.project.active:
                self.set_active(ci)
            if action.startswith("shape_layer:"):
                _t, on, name = action.split(":", 2)
                self.set_shape_layer_visible(name, bool(int(on)))
                self.workspace.refresh(self.project)
            elif action.startswith("show_shape:"):
                self.select_shape(int(action.split(":", 1)[1]))
                self._show_requested("shapes", ci)
            elif action.startswith("remove_shape_layer:"):
                self.remove_shape_layer(action.split(":", 1)[1])
            elif action.startswith("measure_shape:"):
                self.measure_shape(int(action.split(":", 1)[1]))
            elif action.startswith("shape_style:"):
                _t, what, arg = action.split(":", 2)
                if what == "layer":
                    self.open_shape_style(layer=arg, shape=None)
                elif what == "shape":
                    self.select_shape(int(arg))
                    self.open_shape_style(shape=int(arg))
                else:
                    self.open_shape_style()
            elif action == "Export Shapes...":
                self.export_shapes()
            else:
                self.remove_shapes()
            return
        elif action.startswith("toggle_camera:"):
            cam = self.project.chunks[ci].cameras[int(action.split(":")[1])]
            cam.enabled = not cam.enabled
        self.set_modified(); self.reload_view()

    # ---------------------------------------------------------------- File
    def _confirm_discard(self):
        if not self.project.modified:
            return True
        r = QMessageBox.question(self, APP_NAME, "Save changes to the current project?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    def new_project(self):
        if self._confirm_discard():
            self.project = Project(); self.console.ns["project"] = self.project
            self.model_view.clear(); self.refresh_all(); self.reload_view()

    def open_project(self, path=None):
        if not self._confirm_discard():
            return
        path = path or QFileDialog.getOpenFileName(self, "Open", "", PROJECT_FILTER)[0]
        if path:
            try:
                self.project = Project.load(path)
            except Exception as e:
                import traceback
                self.show_error("Open", f"Cannot open project:\n{e}", step="Open project",
                                traceback_text=traceback.format_exc(), worker=False); return
            self.console.ns["project"] = self.project
            if self.project.relocated:
                self.project.modified = True
                for name, k, old, new in self.project.relocated:
                    self.log(f"{name}: {k} moved: {old} -> {new}")
                self.log("Project folder was moved: product paths updated (Save to keep them).")
            self._backfill_meta()
            self._check_photo_names()
            self._report_missing_photos()
            self.start_thumbnails([c for c in self.chunk.cameras if os.path.exists(c.path)])
            self.refresh_all(); self.reload_view()
            self.log(f"Opened {path}")

    def _check_photo_names(self):
        """Projects from 0.1.9 and older do not record the COLMAP name of each
        photo: recover it from the working folder, and warn when photos that
        share a file name cannot be told apart."""
        from .photonames import photo_map, recover_names
        for ch in self.project.chunks:
            if any(c.aligned and not c.colmap_name for c in ch.cameras):
                if recover_names(ch, ch.workdir):
                    self.project.modified = True
                amb = photo_map(ch)[1]
                if amb:
                    self._warn_ambiguous(amb)

    def _report_missing_photos(self, ask=True):
        """Photos not found at their saved paths: offer Change Image Paths right away."""
        from .relink import missing_cameras
        n = sum(len(missing_cameras(ch)) for ch in self.project.chunks)
        if n:
            msg = (f"{n} photo(s) not found at their saved paths (moved disk/computer?). "
                   f"Use Tools > Change Image Paths... to point the project at the photos' new folder.")
            self.log(msg); self.statusBar().showMessage(msg, 20000)
            if ask:
                r = QMessageBox.question(
                    self, "Missing Photos",
                    f"{n} photo(s) of this project were not found at their saved paths "
                    f"(the photos were moved, or the project was opened on another computer).\n\n"
                    f"Locate the folder that contains them now?\n"
                    f"(You can do this later with Tools > Change Image Paths...)",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if r == QMessageBox.Yes:
                    self.change_image_paths()
        return n

    def change_image_paths(self, ci=None):
        """Relink photos after moving them (other drive letter / computer): pick
        the folder that now contains them; files are matched by name (searching
        subfolders) and the longest identical trailing path."""
        from .relink import relink_chunk, missing_cameras, index_folder
        chunks = [self.project.chunks[ci]] if isinstance(ci, int) and 0 <= ci < len(self.project.chunks) \
            else self.project.chunks
        n_missing = sum(len(missing_cameras(ch)) for ch in chunks)
        n_all = sum(len(ch.cameras) for ch in chunks)
        if not n_all:
            QMessageBox.information(self, "Change Image Paths", "The chunk has no photos."); return
        only_missing = True
        if n_missing == 0:
            r = QMessageBox.question(self, "Change Image Paths",
                                     "All photos are found at their current paths.\n"
                                     "Point them at copies in another folder anyway?",
                                     QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            only_missing = False
        folder = QFileDialog.getExistingDirectory(self, "Folder that contains the photos (subfolders are searched)")
        if not folder:
            return
        idx = index_folder(folder)
        done, nf = 0, []
        for ch in chunks:
            a, b = relink_chunk(ch, folder, only_missing=only_missing, idx=idx)
            done += a; nf += b
        self.log(f"Change Image Paths: {done} photo(s) relinked to {folder}; {len(nf)} not found"
                 + (f" (e.g. {', '.join(nf[:5])})" if nf else ""))
        if done:
            self.set_modified(); self.refresh_all()
        QMessageBox.information(self, "Change Image Paths",
                                f"{done} photo(s) relinked.\n{len(nf)} not found"
                                + (f":\n{', '.join(nf[:10])}{' ...' if len(nf) > 10 else ''}" if nf else "."))
        return done, nf

    def _backfill_meta(self):
        """Projects processed before metadata was recorded: rebuild it from the
        logs next to the project. Kept in memory and saved with the next Save."""
        for ch in self.project.chunks:
            added = procmeta.backfill(ch, self.project.path)
            if added:
                self.project.modified = True
                self.log(f"{ch.name}: processing metadata reconstructed from logs ({', '.join(added)}); "
                         f"values not in the logs are shown as 'unknown'. Save to keep it in the project.")

    def append_project(self):
        path = QFileDialog.getOpenFileName(self, "Append", "", PROJECT_FILTER)[0]
        if path:
            self.project.chunks += Project.load(path).chunks
            self.set_modified()

    def save_project(self):
        if not self.project.path:
            return self.save_project_as()
        return self._save_to(self.project.path)

    def _require_saved(self, title):
        """Processing writes into <name>.files next to the .pmsx, so an unsaved
        project is saved first (Save As right here); Cancel = don't start."""
        if self.project.path:
            return True
        r = QMessageBox.question(
            self, title,
            f"Save the project before running {title}.\n\n"
            f"All processing results are stored in the project's data folder next to the "
            f"project file (<name>.files). Save the project now?",
            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save)
        if r != QMessageBox.Save:
            self.statusBar().showMessage(f"{title} not started: the project is not saved", 6000)
            return False
        return bool(self.save_project_as()) and bool(self.project.path)

    def _offer_stale_cleanup(self, path):
        """Unfinished copies of an earlier Save As in <name>.files of path
        (0.1.7 'chunkN.partial', or marked incomplete): ask, then delete them."""
        from . import datafolder as dfo
        root = data_folder(path)
        asked = self.__dict__.setdefault("_stale_asked", set())
        if os.path.normcase(root) in asked:
            return
        stale = dfo.stale_copies(root)
        if not stale:
            return
        asked.add(os.path.normcase(root))
        size = sum(b for _p, b in stale)
        names = "\n".join(f"  {p} ({dfo.fmt_size(b)})" for p, b in stale[:6])
        r = QMessageBox.question(self, "Project Data",
                                 f"Found {len(stale)} unfinished copy(ies) of project data left by an earlier, "
                                 f"interrupted or failed Save As ({dfo.fmt_size(size)}):\n{names}\n\n"
                                 f"They are not used by any project. Delete them to free the space?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            self.log("Unfinished data copies kept: " + "; ".join(p for p, _b in stale))
            return
        left = [p for p, _b in stale if not dfo.remove_tree(p)]
        freed = sum(b for p, b in stale if p not in left)
        self.log(f"Removed unfinished data copies ({dfo.fmt_size(freed)})"
                 + (f"; could not remove: {'; '.join(left)}" if left else ""))
        if left:
            QMessageBox.warning(self, "Project Data", "Could not remove (files in use?):\n" + "\n".join(left))

    def _bring_data(self, path, save_as):
        """Copy the chunks' working folders into <name>.files of `path` when
        they are elsewhere (Save As to a new place, old absolute-path or
        temporary data). Returns False to abort the save."""
        from . import datafolder as dfo, choice
        self._offer_stale_cleanup(path)
        items = dfo.plan(self.project, path, save_as=save_as)
        if save_as:                       # never-created working folders: new results go to the new data folder
            for ch in self.project.chunks:
                if ch.workdir and not os.path.isdir(ch.workdir):
                    ch.workdir = ""
        if not items:
            return True
        total = sum(it["bytes"] for it in items)
        depth = sum(it["parts"]["depth"] for it in items)
        copies = sum(it["parts"]["photo_copies"] for it in items)
        other = total - depth - copies
        src = "\n".join(f"  {it['chunk'].name}: {it['src']}" for it in items[:6])
        root = data_folder(path)
        lines = [f"  Results, models, databases: {dfo.fmt_size(other)}"]
        if depth:
            lines.append(f"  Depth maps: {dfo.fmt_size(depth)} (can be recomputed by Build Point Cloud)")
        if copies:
            lines.append(f"  Photo copies in images\\: {dfo.fmt_size(copies)} (linked to the original photos "
                         f"where possible)")
        if save_as:
            text = (f"Copy the project data to the new project's data folder?\n\n{src}\n  -> {root}\n\n"
                    + "\n".join(lines) + "\n\nThe original files are kept.")
        else:
            text = (f"This project's data is stored outside its folder:\n{src}\n\n"
                    f"Copy it into {root} so the project folder is self-contained?\n\n" + "\n".join(lines))
        if any(it["move"] for it in items):
            text += "\n\n(Data in the temporary folder of an unsaved project is moved, not copied.)"
        keep_label = "Don't copy (keep using the data where it is)"
        if depth:
            options = [("results", f"Copy results only ({dfo.fmt_size(total - depth)})"),
                       ("all", f"Copy everything ({dfo.fmt_size(total)})"), ("keep", keep_label), ("cancel", "Cancel")]
            default = "results"
        else:
            options = [("all", f"Copy ({dfo.fmt_size(total)})"), ("keep", keep_label), ("cancel", "Cancel")]
            default = "all"
        r = choice.ask_choice(self, "Project Data", text, options, default=default)
        if r in (None, "cancel"):
            return False
        if r == "keep":
            self.log("Project data left at: " + "; ".join(it["src"] for it in items))
            return True
        for it in items:
            it["skip_depth"] = r == "results"
        size = sum(dfo.item_bytes(it) for it in items)
        ok, need, free = dfo.check_space(items)
        if not ok:
            QMessageBox.warning(self, "Project Data",
                                f"Not enough free disk space for the project data:\n"
                                f"needed {dfo.fmt_size(need)}, free {dfo.fmt_size(free or 0)} on the target drive.\n\n"
                                f"Nothing was copied; the project was not saved.")
            return False
        res, cancelled, err = run_modal(self, "Copying project data", f"Copying {dfo.fmt_size(size)}...",
                                        lambda report, cancel: dfo.execute(items, report, cancel),
                                        max(1, size // dfo.MB))
        if cancelled:
            self.statusBar().showMessage("Save cancelled: nothing was copied", 8000)
            self.log("Save As cancelled while copying the project data; the unfinished copy was removed.")
            return False
        if err is not None:
            self.show_error("Save", f"Copying the project data failed; the project file was not saved "
                                    f"and still uses its current data:\n{err}\n\n"
                                    f"If the target is in a OneDrive/Dropbox folder, pausing syncing during "
                                    f"the copy may help.",
                            step="Save project data", worker=False)
            return False
        for ch, a, b, moved in dfo.apply(res or []):
            self.log(f"{ch.name}: data {'moved' if moved else 'copied'} {a} -> {b}"
                     + (" (without depth maps)" if r == "results" and depth else ""))
        return True

    def _warn_synced(self, path):
        """Project inside a OneDrive/Dropbox/Google Drive folder: warn once per
        folder; returns False when the user wants another folder."""
        from . import datafolder as dfo
        name = dfo.synced_folder(path)
        warned = self.__dict__.setdefault("_sync_warned", set())
        key = os.path.normcase(os.path.dirname(os.path.abspath(path)))
        if not name or key in warned:
            return True
        r = QMessageBox.question(
            self, "Synced Folder",
            f"This folder is synced by {name}:\n{os.path.dirname(path)}\n\n"
            f"Photogrammetry projects hold tens to hundreds of GB of data. {name} would upload all of it, "
            f"and while it syncs it can lock files (saving or processing may fail with 'Access is denied').\n\n"
            f"A local folder that is not synced (e.g. C:\\PhotoMesh\\Projects) is recommended.\n\n"
            f"Save here anyway?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.No:
            self.statusBar().showMessage("Choose a local (not synced) folder for the project", 8000)
            return False
        warned.add(key)
        self.log(f"Project saved in a {name} folder: {os.path.dirname(path)}")
        return True

    def _save_to(self, path, save_as=False):
        """Write the project file (metadata, ~1 MB JSON, well under 1 s) with
        paths relative to its folder. Data outside the project folder is
        offered to be copied into <name>.files first (see _bring_data)."""
        import time
        if not self._bring_data(path, save_as):
            return False
        t0 = time.perf_counter()
        try:
            self.project.save(path)
        except Exception as e:
            import traceback
            self.show_error("Save", f"Cannot save project:\n{e}", step="Save project",
                            traceback_text=traceback.format_exc(), worker=False); return False
        self._update_title()
        self.log(f"Saved {path} ({time.perf_counter() - t0:.2f} s)")
        return True

    def save_project_as(self):
        path = QFileDialog.getSaveFileName(self, "Save As", self.project.path or "", PROJECT_FILTER)[0]
        if not path:
            return False
        if not path.endswith(PROJECT_EXT):
            path += PROJECT_EXT
        if not self._warn_synced(path):
            return False
        same = bool(self.project.path) and os.path.normcase(os.path.abspath(path)) == \
            os.path.normcase(os.path.abspath(self.project.path))
        if not self._save_to(path, save_as=not same):
            return False
        self.workspace.refresh(self.project)
        return True

    def _world_offset(self):
        from .export import georef_offset
        return georef_offset(self.chunk.georef)

    def _run_export(self, title, fn):
        """Run a (possibly long) export off the GUI thread behind a progress dialog."""
        res, _cancelled, err = run_modal(self, title, "Exporting...", lambda report, cancelled: fn(), 0)
        if err is not None:
            raise err if isinstance(err, BaseException) else RuntimeError(str(err))
        return res

    def export_ply(self, what):
        src = (self.chunk.point_cloud_ply or self.chunk.tie_points_ply) if what == "cloud" else self.chunk.model_ply
        if not src or not os.path.exists(src):
            QMessageBox.information(self, "Export", "Nothing to export yet."); return
        if what != "cloud":
            return self.export_model()
        dst, f = QFileDialog.getSaveFileName(self, "Export Point Cloud", os.path.splitext(os.path.basename(src))[0],
                                             "Stanford PLY (*.ply);;AutoCAD DXF (*.dxf)")
        if dst:
            self.export_cloud_to(dst, "dxf" if "DXF" in f or dst.lower().endswith(".dxf") else "ply")

    def export_cloud_to(self, dst, kind="ply", src=None):
        """Point cloud (dense, else tie points) in world coordinates (local + georef offset)."""
        from . import export as ex
        src = src or self.chunk.point_cloud_ply or self.chunk.tie_points_ply
        off = self._world_offset()
        ext = ".dxf" if kind == "dxf" else ".ply"
        if not dst.lower().endswith(ext):
            dst += ext
        t0 = time.perf_counter()
        if kind == "dxf":
            n = ply_counts(src)[0]
            if n > 2_000_000:
                self.log(f"Warning: DXF with {n:,} POINT entities will be very large (~{n * 70 / 1e9:.1f} GB) "
                         "and slow in CAD; PLY is recommended for point clouds")
            self._run_export("Export Point Cloud", lambda: ex.export_dxf_points(src, dst, off))
        else:
            self._run_export("Export Point Cloud", lambda: ex.export_ply_world(src, dst, off))
        self.log(f"Exported {dst} ({time.perf_counter() - t0:.1f} s)"
                 + (f", world coordinates (offset {', '.join(f'{v:.1f}' for v in off)})" if off is not None else ""))
        return dst

    def import_ply(self, what):
        src = QFileDialog.getOpenFileName(self, "Import", "", "PLY (*.ply)")[0]
        if src:
            self.load_result(what, src)

    def load_result(self, what, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Expected result not found: {path}")
        nv, nf = ply_counts(path)                # header only; the view loads in the background
        if what == "cloud":
            self.chunk.point_cloud_ply, self.chunk.point_count = path, nv
        elif what == "tie":
            self.chunk.tie_points_ply, self.chunk.tie_point_count = path, nv
        else:
            if path != self.chunk.model_ply or nf != self.chunk.face_count:
                self.chunk.texture = {}          # the old atlas belongs to the previous mesh
            self.chunk.model_ply = path
            self.chunk.face_count = nf
        self.set_modified(); self.reload_view()
        self.tabs.setCurrentWidget(self.model_view)

    def export_cameras(self):
        aligned = [c for c in self.chunk.cameras if c.est]
        if not aligned:
            QMessageBox.information(self, "Export Cameras", "No aligned cameras."); return
        dst = QFileDialog.getSaveFileName(self, "Export Cameras", "cameras.csv", "CSV (*.csv)")[0]
        if dst:
            from .export import write_cameras_csv
            write_cameras_csv(dst, aligned, self._world_offset())     # c.est is local: add the offset
            self.log(f"Exported {dst}")

    def export_reference(self):
        dst = QFileDialog.getSaveFileName(self, "Export Reference", "reference.csv", "CSV (*.csv)")[0]
        if dst:
            with open(dst, "w", encoding="utf-8") as f:
                f.write("label,longitude,latitude,altitude\n")
                for c in self.chunk.cameras:
                    f.write(f"{c.label},{c.lon or ''},{c.lat or ''},{c.alt or ''}\n")

    def generate_report(self):
        dst = QFileDialog.getSaveFileName(self, "Generate Report", "report.html", "HTML (*.html)")[0]
        if not dst:
            return
        ch = self.chunk
        img = os.path.splitext(dst)[0] + "_view.png"
        self.model_view.grab().save(img)
        rows = "".join(f"<tr><td>{c.label}</td><td>{'yes' if c.aligned else 'no'}</td></tr>" for c in ch.cameras)
        html = (f"<html><body style='font-family:sans-serif'><h1>{APP_NAME} processing report</h1>"
                f"<h2>{ch.name}</h2><ul><li>Cameras: {len(ch.cameras)} ({ch.aligned_count()} aligned)</li>"
                f"<li>Tie points: {ch.tie_point_count:,}</li><li>Point cloud: {ch.point_count:,}</li>"
                f"<li>Model faces: {ch.face_count:,}</li></ul><img src='{os.path.basename(img)}' width=800>"
                f"<h3>Cameras</h3><table border=1 cellpadding=3><tr><th>Label</th><th>Aligned</th></tr>{rows}</table>"
                f"<h3>Processing log</h3><pre>{chr(10).join(ch.log)}</pre></body></html>")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(html)
        self.log(f"Report written to {dst}")

    # ---------------------------------------------------------------- View
    def reset_view(self):
        if self.tabs.currentWidget() is self.ortho_view:
            self.ortho_view.fit()
        else:
            self.model_view.reset_view()

    def zoom(self, f):
        if self.tabs.currentWidget() is self.ortho_view:
            self.ortho_view.zoom(f)
        elif self.tabs.currentWidget() is self.photo_view:
            self.photo_view.zoom(1 / f)
        else:
            self.model_view.zoom(f)

    def capture_view(self):
        dst = QFileDialog.getSaveFileName(self, "Capture View", "capture.png", "PNG (*.png)")[0]
        if dst:
            self.tabs.currentWidget().grab().save(dst)

    def toggle_full_screen(self, on):
        self.showFullScreen() if on else self.showNormal()

    # -------------------------------------------------------------- Photos
    def open_photo(self, k):
        cams = self.chunk.cameras
        if 0 <= k < len(cams):
            self.current_photo = k
            c = cams[k]
            self.photo_view.open(c.path, f"{c.label}   ({k + 1}/{len(cams)})   "
                                          f"{'aligned' if c.aligned else 'not aligned'}")
            self.tabs.setCurrentWidget(self.photo_view)
            try:
                self._update_photo_markers()
            except Exception as e:
                self.log(f"Markers overlay: {e}")
            try:
                self._update_photo_shapes()
            except Exception as e:  # noqa: BLE001
                self.log(f"Shapes overlay on the photo: {e}")

    def step_photo(self, d):
        if self.chunk.cameras:
            self.open_photo((self.current_photo + d) % len(self.chunk.cameras))

    def rotate_photo(self, deg):
        if self.tabs.currentWidget() is self.photo_view:
            self.photo_view.view.rotate(deg)

    # ------------------------------------------------------------ Workflow
    def add_photos(self, paths=None):
        """Add photos without blocking the window: sizes + EXIF GPS are read from
        the file headers in a background thread (cancellable progress dialog);
        thumbnails/descriptors are created afterwards in the background."""
        if paths is None:
            exts = " ".join(f"*{e} *{e.upper()}" for e in sorted(IMAGE_EXTS))
            paths, _ = QFileDialog.getOpenFileNames(self, "Add Photos", self._last_photo_dir(), f"Images ({exts})")
        if not paths:
            return []
        from .fastpipe import map_images, read_header
        known = {c.path for c in self.chunk.cameras}
        new = [p for p in dict.fromkeys(os.path.abspath(p) for p in paths)
               if p not in known and os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        if not new:
            self.log("Add Photos: nothing new to add"); return []
        QSettings().setValue("paths/last_photo_dir", os.path.dirname(new[0]))
        t0 = time.perf_counter()
        _log.info("Add Photos: %d files, first %s", len(new), new[0])

        def work(report, cancelled):
            return map_images(read_header, new, workers=8,
                              progress=lambda i, n, p, r: report(i, n, os.path.basename(p)), cancel=cancelled)
        infos, cancelled, err = run_modal(self, "Add Photos", f"Reading {len(new)} photos (size, EXIF/GPS)...",
                                          work, len(new))
        if err:
            self.show_error("Add Photos", f"Could not read the photos:\n{err}", step="Add Photos: read headers"); return []
        if cancelled:
            self.log(f"Add Photos: cancelled - nothing added ({len(infos or {})} of {len(new)} were read)")
            return []
        t1 = time.perf_counter()
        _log.info("Add Photos: header scan %.2f s", t1 - t0)
        with diag.Timed("Add Photos: add cameras", n=len(new)):
            added = self.chunk.add_photos(new, infos)
        bad = [p for p, i in infos.items() if "error" in i]
        for p in bad[:20]:
            _log.warning("unreadable photo %s: %s", p, infos[p]["error"])
        self.log(f"Added {len(added)} photos in {time.perf_counter() - t0:.2f} s"
                 + (f" ({len(bad)} unreadable, e.g. {os.path.basename(bad[0])}: {infos[bad[0]]['error']})"
                    if bad else ""))
        with diag.Timed("Add Photos: refresh panes", cameras=len(self.chunk.cameras)):
            self.set_modified()
        self.start_thumbnails(added)
        return added

    def _last_photo_dir(self):
        d = QSettings().value("paths/last_photo_dir", "", str)
        return d if d and os.path.isdir(d) else ""

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add Folder", self._last_photo_dir())
        if not d:
            return
        try:
            with os.scandir(d) as it:
                files = sorted(e.path for e in it
                               if e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS)
        except OSError as e:
            QMessageBox.warning(self, "Add Folder", f"Cannot read folder:\n{d}\n{e}"); return
        if not files:
            QMessageBox.information(self, "Add Folder", f"No photos ({', '.join(sorted(IMAGE_EXTS))}) in\n{d}")
            return
        self.add_photos(files)

    # ---- thumbnails (background, never on the GUI thread)
    def _thumb_cache(self):
        return os.path.join(QStandardPaths.writableLocation(QStandardPaths.CacheLocation) or
                            os.path.expanduser("~/.cache/photomesh"), "thumbs")

    def start_thumbnails(self, cams):
        """Create missing thumbnails + global descriptors in a background thread;
        the Photos pane updates as they arrive."""
        todo = [c for c in cams if not (c.thumb and os.path.exists(c.thumb) and c.desc and os.path.exists(c.desc))]
        if not todo:
            return None
        self.cancel_thumbnails()
        from .fastpipe import map_images, analyze_image
        cache = self._thumb_cache()
        paths = [c.path for c in todo]

        def work(report, cancelled):
            return map_images(lambda p: analyze_image(p, cache), paths, workers=max(1, min(6, (os.cpu_count() or 2) - 1)),
                              progress=lambda i, n, p, r: report(i, n, "", r), cancel=cancelled)
        job = BackgroundJob(work, name="thumbnails")
        job.batch.connect(self._apply_thumbs)
        job.progress.connect(lambda d, n, _t: (self.thumb_progress.setMaximum(n), self.thumb_progress.setValue(d)))
        job.done.connect(lambda r, e, j=job: self._thumbs_done(j, r, e))
        self._thumb_job = job
        self.thumb_progress.setRange(0, len(paths)); self.thumb_progress.setValue(0)
        self.thumb_progress.show(); self.thumb_cancel.show()
        _log.info("thumbnails: %d photos -> %s", len(paths), cache)
        return job.start()

    def cancel_thumbnails(self):
        j = self._thumb_job
        if j is not None and not j.finished:
            j.cancel()

    def _apply_thumbs(self, results):
        by_path = {}
        for ch in self.project.chunks:
            for k, c in enumerate(ch.cameras):
                by_path.setdefault(c.path, []).append((ch, k, c))
        for r in results:
            if not r or "error" in r:
                continue
            for ch, k, c in by_path.get(r["path"], []):
                c.thumb, c.desc = r["thumb"], r["desc"]
                if not c.width:
                    c.width, c.height = r["width"], r["height"]
                if ch is self.chunk:
                    self.photos.set_thumb(k, c)

    def _thumbs_done(self, job, result, err):
        if job is not self._thumb_job:
            return
        self.thumb_progress.hide(); self.thumb_cancel.hide()
        bad = sum(1 for v in (result or {}).values() if "error" in v)
        _log.info("thumbnails finished: %d done, %d unreadable, cancelled=%s, error=%s",
                  len(result or {}), bad, job.cancelled(), err)

    def open_log_folder(self):
        d = diag.log_dir()
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))
        self.statusBar().showMessage(f"Log folder: {d}  (current log: {diag.log_path() or 'not available'})", 15000)

    def _need_colmap(self):
        exe = cm.find_colmap()
        if not exe:
            QMessageBox.warning(self, "COLMAP not found",
                "This command uses COLMAP as its reconstruction engine, but no 'colmap' "
                "executable was found.\n\nWindows: download COLMAP (CUDA build) from\n"
                "https://github.com/colmap/colmap/releases, unzip it (e.g. C:\\COLMAP) and add "
                "that folder to PATH, or set the path in Tools > Preferences > Advanced.")
            self.statusBar().showMessage("COLMAP not found", 6000)
        return exe

    def _run(self, title, steps, on_success, meta=None, params=None, on_fail=None):
        """meta: callable(seconds, stage_seconds) run after on_success, to record
        processing metadata (procmeta.record) for the step. params: the step's
        settings (dialog values), for the error report if it fails. on_fail: run
        when the step fails or is cancelled (e.g. show the previous result again)."""
        exe = cm.find_colmap()
        if self.runner is not None and getattr(self.runner, "busy", False):
            QMessageBox.information(self, title, "Another processing task is running."); return
        dlg = ProgressDialog(title, self)
        self.runner = cm.PipelineRunner(exe, steps, self)
        self.runner.progress.connect(dlg.set_progress)
        self.runner.detail.connect(dlg.set_detail)
        self.runner.log.connect(dlg.append)
        self.runner.log.connect(self.console.write)
        dlg.cancel_btn.clicked.connect(self.runner.cancel)
        t0 = time.perf_counter()
        stage_t = []                                   # [(label, start)]

        def on_progress(_pct, label):
            if label and (not stage_t or stage_t[-1][0] != label) and label != "Finished":
                stage_t.append((label, time.perf_counter()))
        self.runner.progress.connect(on_progress)
        job = f"{title} - {self.chunk.name}"
        self.jobs.add_job(f"[running] {job}")
        mark = self.console.mark()
        load_tb = []
        def done(ok, msg):
            dt = time.perf_counter() - t0
            self.jobs.add_job(f"[{'done' if ok else 'failed'}] {job}  ({dt:.1f} s)")
            self.chunk.log.append(f"{title}: {'OK' if ok else msg} ({dt:.1f} s)")
            if ok:
                try:
                    on_success()
                except Exception as e:
                    import traceback
                    ok, msg = False, f"Could not load results: {e}"
                    load_tb.append(traceback.format_exc())
            if ok and meta is not None:
                t_end = time.perf_counter()
                stages = {}
                for k, (lab, ts) in enumerate(stage_t):
                    nxt = stage_t[k + 1][1] if k + 1 < len(stage_t) else t_end
                    stages[lab] = round(stages.get(lab, 0) + nxt - ts, 2)
                try:
                    meta(round(dt, 2), stages)
                    self.refresh_all()
                except Exception as e:  # noqa: BLE001 - metadata must never fail a step
                    self.log(f"{title}: could not record processing metadata: {e}")
            dlg.cancel_btn.setText("Close")
            dlg.cancel_btn.clicked.disconnect(); dlg.cancel_btn.clicked.connect(dlg.accept)
            if ok:
                dlg.accept()
                self.statusBar().showMessage(f"{title} finished in {dt:.1f} s", 8000)
            self.log(f"{title}: {'finished' if ok else msg} ({dt:.1f} s)")
            if not ok and on_fail is not None:
                try:
                    on_fail()
                except Exception as e:  # noqa: BLE001 - must not hide the error dialog
                    self.log(f"{title}: {e}")
            if not ok:
                dlg.stage.setText(msg)
                if msg == "Cancelled by user":
                    QMessageBox.information(self, title, msg)
                else:
                    f = (self.runner.failure if self.runner is not None else None) or {}
                    step = f"{title} > {f['step']}" if f.get("step") else title
                    dlg.accept()
                    self.show_error(f"{title} failed", msg, step=step, params=params, command=f.get("command"),
                                    exit_code=f.get("exit_code"), crashed=bool(f.get("crashed")),
                                    traceback_text=(load_tb[0] if load_tb else f.get("traceback", "")),
                                    mark=mark, worker=False)
        self.busy_indicator.begin(title)
        ended = []

        def end_busy(*_):                    # wait cursor restored first (also on error / cancel)
            if not ended:
                ended.append(1); self.busy_indicator.end()
        self.runner.finished.connect(end_busy)
        self.runner.finished.connect(done)
        self.runner.busy = True
        self.runner.finished.connect(lambda *_: setattr(self.runner, "busy", False))
        try:
            self.runner.start()
        except BaseException:
            end_busy(); raise
        dlg.exec()

    def align_photos(self):
        if not self._require_saved("Align Photos"):
            return None
        ch = self.chunk
        if len([c for c in ch.cameras if c.enabled]) < 2:
            QMessageBox.information(self, "Align Photos", "Add at least two photos first."); return
        exe = self._need_colmap()
        if not exe:
            return
        dlg = AlignPhotosDialog(self)
        if not dlg.exec():
            return
        s = dlg.settings()
        wd = self.project.default_workdir(ch)
        try:
            mapping = cm.prepare_images(ch, wd)
        except OSError as e:
            QMessageBox.critical(self, "Align Photos", f"Cannot prepare working folder {wd}:\n{e}"); return
        enabled = [c for c in ch.cameras if c.enabled]
        if s["mode"] == "Fast" and any(not (c.desc and os.path.exists(c.desc)) for c in enabled):
            from .fastpipe import analyze_images
            self.cancel_thumbnails()
            paths = [c.path for c in enabled]
            cache = os.path.join(wd, "cache")
            infos, cancelled, err = run_modal(
                self, "Align Photos", "Preparing photos (thumbnails / global descriptors)...",
                lambda report, cancelled: analyze_images(paths, cache, progress=lambda i, n, *_: report(i, n),
                                                         cancel=cancelled), len(paths))
            if cancelled or err:
                if err:
                    self.show_error("Align Photos", f"Could not read the photos:\n{err}",
                                    step="Align Photos: prepare photos", params=s)
                return
            for c in enabled:
                i = infos.get(c.path, {})
                c.desc, c.width, c.height = i.get("desc", ""), i.get("width", 0), i.get("height", 0)
        names = [mapping[c.path] for c in enabled]
        steps = cm.align_steps(exe, ch, wd, s, names=names, infos=[c.info() for c in enabled])
        self.log(f"Align Photos: mode={s['mode']}, accuracy={s['accuracy']}, workdir={wd}")

        def loaded():
            poses = cm.read_images_txt(os.path.join(wd, "sparse_txt", "images.txt"))
            from .photonames import assign_names
            assign_names(ch, mapping)              # exact camera <-> model link, saved in the project
            for c in ch.cameras:
                p = poses.get(mapping.get(c.path, ""))
                c.aligned = p is not None
                c.est = p[0].tolist() if p else None
                c.rot = p[1].tolist() if p else None
            ch.tie_points_ply = os.path.join(wd, "tie_points.ply")
            ch.tie_point_count = cm.count_points3d(os.path.join(wd, "sparse_txt", "points3D.txt"))
            self.set_modified(); self.reload_view()
            self.tabs.setCurrentWidget(self.model_view)
            self._report_unaligned([c for c in enabled if not c.aligned], len(enabled))
        def meta(dt, stages):
            procmeta.record(ch, "alignment", params=s, seconds=dt, stages=stages,
                            tie_points=ch.tie_point_count, registered=ch.aligned_count(),
                            matching_seconds=round(sum(v for k, v in stages.items() if "atching" in k), 2) or None,
                            alignment_seconds=round(sum(v for k, v in stages.items() if k.startswith(
                                ("Estimating", "Triangulating", "Registering", "Optimizing", "Reconstruct",
                                 "Mapping", "Bundle"))), 2) or None)
            ch.meta.pop("sparse_stats", None)          # recomputed by the view loader
        self._run("Align Photos", steps, loaded, meta, params=s)

    def _report_unaligned(self, missing, total):
        """List photos that could not be aligned (Console + status bar) so the
        user can check them - e.g. take-off/landing shots or photos with no
        overlap with the rest of the survey."""
        if not missing:
            return
        names = [c.label for c in missing]
        self.log(f"Align Photos: {len(missing)} of {total} photos could not be aligned "
                 f"(no reliable overlap with the aligned photos): " + ", ".join(names))
        self.statusBar().showMessage(f"{total - len(missing)}/{total} photos aligned - "
                                     f"{len(missing)} not aligned (see Console)", 15000)

    def build_point_cloud(self):
        if not self._require_saved("Build Point Cloud"):
            return None
        ch = self.chunk
        if not ch.aligned_count():
            QMessageBox.information(self, "Build Point Cloud", "Align photos first."); return
        exe = self._need_colmap()
        if not exe:
            return
        if not cm.has_cuda(exe):
            QMessageBox.warning(self, "Build Point Cloud",
                "Your COLMAP build has no CUDA support. COLMAP's dense reconstruction "
                "(depth maps) needs an NVIDIA GPU and the CUDA build of COLMAP.\n\n"
                "Download 'COLMAP-x.y-windows-cuda.zip' from github.com/colmap/colmap/releases.")
            return
        dlg = BuildPointCloudDialog(self)
        if not dlg.exec():
            return
        from .dense import dense_steps
        wd = self.project.default_workdir(ch)
        s = dlg.settings()
        if ch.georef:
            s["filters"]["up"] = [0.0, 0.0, 1.0]
        try:
            steps = dense_steps(exe, wd, s, cm.image_dir(wd))
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Build Point Cloud", str(e)); return
        self.log(f"Build Point Cloud: quality={s['quality']}, depth filtering={s['depth_filtering']}, "
                 f"{s['max_sources']} neighbour photos, workdir={wd}")
        def meta(dt, stages):
            import glob as _g
            depth_s = sum(v for k, v in stages.items() if "depth map" in k.lower())
            p = {k: s[k] for k in ("quality", "depth_filtering", "max_sources", "window_step", "num_iterations")
                 if k in s}
            count = len(_g.glob(os.path.join(wd, "dense", "stereo", "depth_maps", "*.geometric.bin")))
            removed = {}
            if not count:
                try:
                    import json as _j
                    removed = _j.load(open(os.path.join(wd, "dense", "depth_maps_info.json"), encoding="utf-8"))
                    count = int(removed.get("maps") or 0)
                except (OSError, ValueError):
                    removed = {}
            procmeta.record(ch, "depth_maps", params=p, seconds=round(depth_s, 2) or None, stages=stages,
                            count=count or None, kept=not removed.get("removed", False))
            procmeta.record(ch, "point_cloud", params=dict(p, filters=s.get("filters")), seconds=dt, stages=stages,
                            points=ch.point_count,
                            fusion_seconds=round(sum(v for k, v in stages.items() if "fusion" in k.lower()), 2) or None,
                            filtering_seconds=round(sum(v for k, v in stages.items() if "ilter" in k), 2) or None,
                            filtering=[], **procmeta.file_info(ch.point_cloud_ply))
        self._run("Build Point Cloud", steps,
                  lambda: self.load_result("cloud", os.path.join(wd, "dense", "point_cloud.ply")), meta, params=s)

    def build_model(self):
        if not self._require_saved("Build Model"):
            return None
        ch = self.chunk
        if not ch.aligned_count():
            QMessageBox.information(self, "Build Model", "Align photos first."); return
        exe = self._need_colmap()
        if not exe:
            return
        dlg = BuildModelDialog(self)
        if not dlg.exec():
            return
        s = dlg.settings()
        if s["source"] == "Depth maps" and not cm.has_cuda(exe):
            QMessageBox.warning(self, "Build Model", "'Depth maps' source needs a CUDA build of COLMAP."); return
        if s["source"] == "Point cloud" and not (ch.point_cloud_ply and os.path.exists(ch.point_cloud_ply)):
            QMessageBox.information(self, "Build Model", "Build the point cloud first or choose another source."); return
        from .mesh_steps import mesh_steps
        wd = self.project.default_workdir(ch)
        cams = [c.est for c in ch.cameras if c.aligned and c.est]
        try:
            steps, out = mesh_steps(exe, wd, s, ch.point_cloud_ply,
                                    cam_center=np.mean(cams, 0).tolist() if cams else None)
        except RuntimeError as e:
            QMessageBox.information(self, "Build Model", str(e)); return
        def meta(dt, stages):
            from .plyio import ply_counts
            nv, nf = ply_counts(out)
            procmeta.record(ch, "model", params=dict(s), seconds=dt, stages=stages, faces=nf, vertices=nv,
                            **procmeta.file_info(out))
        def loaded():
            self.load_result("model", out)
            self._model_replaced(ch)
        self._run("Build Model", steps, loaded, meta, params=s)

    STALE = "built from an earlier model"

    def _model_replaced(self, ch):
        """A new model replaced the old one: its texture no longer applies, and
        products made from the old model are marked outdated (they stay on disk
        and in the project until rebuilt) so they are not taken for results of
        the new geometry."""
        notes = []
        if ch.texture:
            ch.texture = {}; notes.append("texture")
        dem_stale = False
        if ch.dem and ch.dem.get("source") == "Mesh":
            ch.dem["stale"] = self.STALE; notes.append("DEM"); dem_stale = True
        if ch.tiled_model and ch.tiled_model.get("source", "Model") != "Point cloud":
            ch.tiled_model["stale"] = self.STALE; notes.append("tiled model")
        o = ch.orthomosaic
        if o and (o.get("surface", "Mesh") == "Mesh" or dem_stale):
            o["stale"] = self.STALE; notes.append("orthomosaic")
        if notes:
            self.log("Build Model: the previous " + ", ".join(notes) + " belonged to the old model - "
                     "rebuild " + ("it" if len(notes) == 1 else "them") + " for the new model")
            self.set_modified(); self.refresh_all()

    # ------------------------------------------------------------ texture / tiled model
    def _photo_map(self, ch):
        """COLMAP image name -> photo path (exact, see photonames.py). Photos of
        older projects that share a file name and cannot be told apart are left
        out with a warning instead of being given another photo's camera."""
        from .photonames import photo_map, recover_names
        if any(c.aligned and not c.colmap_name for c in ch.cameras):
            if recover_names(ch, ch.workdir):
                self.set_modified()
        pm, ambiguous = photo_map(ch)
        if ambiguous:
            self._warn_ambiguous(ambiguous)
        return pm

    def _warn_ambiguous(self, names):
        msg = (f"{len(names)} photo name(s) are used by more than one photo (e.g. {names[0]}) and this "
               "project (made with an older version) does not record which photo is which camera: "
               "those photos are left out. Run Align Photos again to fix it.")
        self.log("Warning: " + msg)
        self.statusBar().showMessage(msg, 15000)

    def build_texture(self):
        if not self._require_saved("Build Texture"):
            return None
        ch = self.chunk
        if not (ch.model_ply and os.path.exists(ch.model_ply)):
            QMessageBox.information(self, "Build Texture", "Build the model first."); return
        mdir = self._model_dir(ch)
        if not os.path.exists(os.path.join(mdir, "images.bin")):
            QMessageBox.information(self, "Build Texture", "Align photos first (no camera model found)."); return
        old_uv = (ch.texture or {}).get("uv_npz")
        has_uv = bool(old_uv and os.path.exists(old_uv) and (ch.texture or {}).get("faces") == ch.face_count)
        dlg = BuildTextureDialog(self, has_uv=has_uv)
        if not dlg.exec():
            return
        s = dlg.settings()
        wd = self.project.default_workdir(ch)
        out = os.path.join(wd, "texture")
        cfg = dict(s, mesh=ch.model_ply, model=mdir, photos=self._photo_map(ch), out_dir=out,
                   georef=bool(ch.georef), result=os.path.join(wd, "texture_result.json"))
        if s["mapping"] == "Keep uv":
            cfg["uv_npz"] = old_uv
        cfg_path = os.path.join(wd, "build_texture.json")
        os.makedirs(wd, exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        steps = [("Building texture", [cm.PYTHON, "build_texture", cfg_path], 1)]
        res = {}

        def loaded():
            with open(cfg["result"], encoding="utf-8") as f:
                res.update(json.load(f))
            res.update({k: s[k] for k in ("hole_filling", "ghosting")})
            ch.texture = res
            self.set_modified()
            self.load_texture_view()
            self.show_model_mode("textured")

        def meta(dt, stages):
            procmeta.record(ch, "texture", params=dict(s), seconds=dt, stages=res.get("stages") or stages,
                            size=res.get("size"), count=res.get("count"))
        self._run("Build Texture", steps, loaded, meta, params=s)

    def load_texture_view(self):
        """Load the textured model display in the background (Model - Textured)."""
        ch = self.chunk
        t = ch.texture or {}
        if not t.get("dir") or not ch.model_ply or self.model_view.gl is None:
            return
        if t.get("faces") != ch.face_count:
            self.log("Texture does not match the current model (rebuild the texture)"); return
        import threading
        gen = self._view_gen
        flip = None if ch.georef else np.asarray(self.model_view.flip, float)
        mesh, d = ch.model_ply, t["dir"]

        def work():
            try:
                from .glitems import load_textured_display
                data = load_textured_display(mesh, d, flip)
            except Exception as e:  # noqa: BLE001
                data = e
            try:
                self._texData.emit(gen, data)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="texture-loader", daemon=True).start()

    def _apply_texture(self, gen, data):
        if gen != self._view_gen:
            return
        if isinstance(data, Exception):
            self.log(f"Cannot show texture: {data}"); return
        self.model_view.set_textured(*data)
        self.statusBar().showMessage("Textured model loaded", 3000)

    def import_texture(self, folder=None, quiet=False):
        """File > Import > Import Texture...: attach an existing Build Texture output
        folder (info.json + uv.npz + texture_N pages) to the current model without
        rebuilding it. The folder must belong to this exact mesh (same face count)."""
        ch = self.chunk
        if not folder:
            if not (ch.model_ply and os.path.exists(ch.model_ply)):
                QMessageBox.information(self, "Import Texture", "Build or import the model first."); return False
            folder = QFileDialog.getExistingDirectory(self, "Import Texture - folder with info.json and texture_0.jpg",
                                                      os.path.dirname(ch.model_ply or ""))
            if not folder:
                return False
        err = None
        try:
            with open(os.path.join(folder, "info.json"), encoding="utf-8") as f:
                info = json.load(f)
            pages = info.get("pages") or []
            missing = [n for n in pages + ["uv.npz"] if not os.path.exists(os.path.join(folder, n))]
            if not pages or missing:
                err = "Incomplete texture folder (missing: " + ", ".join(missing or ["pages"]) + ")."
            elif info.get("faces") != ch.face_count:
                err = (f"This texture was built for a model with {info.get('faces'):,} faces, but the current model "
                       f"has {ch.face_count:,} faces. Build Texture again for the current model.")
        except (OSError, ValueError) as e:
            err = f"Not a PhotoMesh texture folder ({e})."
        if err:
            if not quiet:
                QMessageBox.warning(self, "Import Texture", err)
            self.log(f"Import Texture: {err}"); return False
        folder = os.path.abspath(folder)
        info["dir"] = folder
        info["uv_npz"] = os.path.join(folder, "uv.npz")
        for k, n in (("obj", "model_textured.obj"), ("glb", "model_textured.glb")):
            info[k] = os.path.join(folder, n) if os.path.exists(os.path.join(folder, n)) else None
        ch.texture = info
        procmeta.record(ch, "texture", params={k: info.get(k) for k in ("mapping", "blending", "hole_filling",
                                                                          "ghosting", "size", "count")},
                        seconds=info.get("seconds"), stages=info.get("stages"),
                        size=info.get("size"), count=info.get("count"))
        ch.meta["texture"]["source"] = "imported"
        self.set_modified()
        self.load_texture_view()
        self.show_model_mode("textured")
        self.log(f"Imported texture {info.get('size')}x{info.get('size')} x{info.get('count')} from {folder}")
        return True

    def import_tiled_model(self, folder=None, quiet=False):
        """File > Import > Import Tiled Model...: attach an existing Build Tiled Model
        output folder (tileset.json + photomesh_tiles.json + tiles/)."""
        ch = self.chunk
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Import Tiled Model - folder with tileset.json",
                                                      os.path.dirname(ch.model_ply or ""))
            if not folder:
                return False
        folder = os.path.abspath(folder)
        idx_path = os.path.join(folder, "photomesh_tiles.json")
        try:
            with open(idx_path, encoding="utf-8") as f:
                idx = json.load(f)
            if not str(idx.get("format", "")).startswith("photomesh-tiles"):
                raise ValueError("unknown index format")
        except (OSError, ValueError) as e:
            msg = f"Not a PhotoMesh tiled model folder ({e})."
            if not quiet:
                QMessageBox.warning(self, "Import Tiled Model", msg)
            self.log(f"Import Tiled Model: {msg}"); return False
        if bool(idx.get("georeferenced")) != bool(ch.georef):
            self.log("Import Tiled Model: warning - georeferencing of the tiles and the project differ")
        nbytes = 0
        for dp, _dn, fn in os.walk(folder):
            nbytes += sum(os.path.getsize(os.path.join(dp, n)) for n in fn)
        tdir = os.path.join(folder, "tiles")
        fmt = "glb" if os.path.isdir(tdir) and any(n.endswith(".glb") for n in os.listdir(tdir)) else "b3dm"
        res = {"dir": folder, "tileset": os.path.join(folder, "tileset.json"), "index": idx_path,
               "levels": idx.get("levels"), "tiles": idx.get("tiles"), "pixel_size": idx.get("pixel_size"),
               "georef": bool(idx.get("georeferenced")), "source": idx.get("source"), "format": fmt,
               "bytes": nbytes, "faces": idx.get("faces"), "points": idx.get("points"),
               "rotation_model_to_tiles": idx.get("rotation_model_to_tiles")}
        secs, stages = None, None                # timing from the build's *_result.json next to the folder
        parent = os.path.dirname(folder)
        for n in sorted(os.listdir(parent)):
            if n.endswith("result.json"):
                try:
                    with open(os.path.join(parent, n), encoding="utf-8") as f:
                        r = json.load(f)
                    if os.path.basename(os.path.normpath(str(r.get("dir", "")).replace("\\", "/"))) == \
                            os.path.basename(folder):
                        secs, stages = r.get("seconds"), r.get("stages"); break
                except (OSError, ValueError, AttributeError):
                    pass
        res["seconds"], res["stages"] = secs, stages
        ch.tiled_model = res
        procmeta.record(ch, "tiled_model", params={"source": res["source"], "format": fmt},
                        seconds=secs, stages=stages, levels=res["levels"], tiles=res["tiles"])
        ch.meta["tiled_model"]["source"] = "imported"
        self.set_modified()
        self.load_tiled_view()
        self.show_tiled_mode("textured")
        self.log(f"Imported tiled model ({res['levels']} levels, {res['tiles']} tiles) from {folder}")
        return True

    def _model_to_tiles(self, t):
        R = t.get("rotation_model_to_tiles")
        return np.asarray(R, float) if R is not None else np.eye(3)

    def build_tiled_model(self):
        if not self._require_saved("Build Tiled Model"):
            return None
        ch = self.chunk
        have_mesh = bool(ch.model_ply and os.path.exists(ch.model_ply))
        have_cloud = bool(ch.point_cloud_ply and os.path.exists(ch.point_cloud_ply))
        if not (have_mesh or have_cloud):
            QMessageBox.information(self, "Build Tiled Model", "Build the model or the point cloud first."); return
        sources = (["Model"] if have_mesh else []) + (["Point cloud"] if have_cloud else [])
        dlg = BuildTiledModelDialog(self, sources, unit="m" if ch.georef else "units")
        if not dlg.exec():
            return
        s = dlg.settings()
        wd = self.project.default_workdir(ch)
        tex = ch.texture or {}
        tex_dir = tex.get("dir") if tex.get("faces") == ch.face_count and tex.get("dir") else None
        cfg = dict(s, out_dir=os.path.join(wd, "tiled"), mesh=ch.model_ply, cloud=ch.point_cloud_ply,
                   texture_dir=tex_dir, model=self._model_dir(ch), georef=ch.georef or {},
                   result=os.path.join(wd, "tiled_result.json"))
        if ch.georef:
            cfg["up"] = [0.0, 0.0, 1.0]
        cfg_path = os.path.join(wd, "build_tiles.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        self.model_view.set_tiled(None)          # release the tiles that are about to be replaced
        steps = [("Building tiled model", [cm.PYTHON, "build_tiles", cfg_path], 1)]
        res = {}

        def loaded():
            with open(cfg["result"], encoding="utf-8") as f:
                res.update(json.load(f))
            with open(res["index"], encoding="utf-8") as f:
                res["rotation_model_to_tiles"] = json.load(f).get("rotation_model_to_tiles")
            res["textured"] = bool(tex_dir)
            ch.tiled_model = res
            self.set_modified()
            self.load_tiled_view()
            self.show_tiled_mode("textured")

        def meta(dt, stages):
            procmeta.record(ch, "tiled_model", params=dict(s), seconds=dt, stages=res.get("stages") or stages,
                            levels=res.get("levels"), tiles=res.get("tiles"))
        self._run("Build Tiled Model", steps, loaded, meta, params=s)

    def load_tiled_view(self):
        t = self.chunk.tiled_model or {}
        idx = t.get("index")
        if not idx or not os.path.exists(idx):
            self.model_view.set_tiled(None); return
        try:
            self.model_view.set_tiled(t["dir"], self._model_to_tiles(t))
        except Exception as e:  # noqa: BLE001
            self.log(f"Cannot show tiled model: {e}")

    def show_tiled_mode(self, mode):
        """Tiled Model - Textured/Solid/Wireframe/Elevation."""
        self._sync_mode_button(f"Tiled Model - {mode.capitalize()}")
        self.model_view.set_tiled_mode(mode)
        if not self._has_layer("tiled"):
            self.statusBar().showMessage("No tiled model - build it first", 4000); return
        self._solo("tiled")

    def export_model(self):
        """Export Model: OBJ (+MTL + texture pages), binary glTF, or PLY."""
        ch = self.chunk
        src = ch.model_ply
        if not src or not os.path.exists(src):
            QMessageBox.information(self, "Export Model", "Nothing to export yet."); return
        t = ch.texture or {}
        textured = bool(t.get("dir") and t.get("faces") == ch.face_count)
        filt = ("Wavefront OBJ with texture (*.obj);;Binary glTF with texture (*.glb);;"
                "Stanford PLY with texture (*.ply);;" if textured else "") + "Stanford PLY (*.ply);;AutoCAD DXF (*.dxf)"
        base = os.path.splitext(os.path.basename(src))[0]
        dst, f = QFileDialog.getSaveFileName(self, "Export Model", base, filt)
        if dst:
            self.export_model_to(dst, "dxf" if "DXF" in f else "obj" if "OBJ" in f else "glb" if "glTF" in f else
                                 "ply_tex" if "PLY with" in f else "ply")

    def export_model_to(self, dst, kind):
        """World coordinates (local + georef offset) for PLY / OBJ / DXF; GLB keeps
        float32 local vertices with the offset in the root node translation."""
        from . import export as ex
        ch = self.chunk
        t = ch.texture or {}
        off = self._world_offset()
        src = ch.model_ply
        t0 = time.perf_counter()
        if kind == "dxf":
            if not dst.lower().endswith(".dxf"):
                dst += ".dxf"
            self._run_export("Export Model", lambda: ex.export_dxf_mesh(src, dst, off))
        elif kind == "ply" or not t.get("dir"):
            if not dst.lower().endswith(".ply"):
                dst += ".ply"
            self._run_export("Export Model", lambda: ex.export_ply_world(src, dst, off))
        elif kind == "ply_tex":
            if not dst.lower().endswith(".ply"):
                dst += ".ply"
            from .texture import export_ply_textured
            self._run_export("Export Model", lambda: export_ply_textured(src, t["dir"], dst, offset=off))
        elif kind == "obj":
            if not dst.lower().endswith(".obj"):
                dst += ".obj"
            from .texture import export_obj
            self._run_export("Export Model", lambda: export_obj(src, t["dir"], dst, offset=off))
        else:
            if not dst.lower().endswith(".glb"):
                dst += ".glb"
            from .texture import export_glb
            self._run_export("Export Model", lambda: export_glb(src, t["dir"], dst, z_up=True, offset=off))
        self.log(f"Exported {dst} ({time.perf_counter() - t0:.1f} s)"
                 + (f", world coordinates (offset {', '.join(f'{v:.1f}' for v in off)})" if off is not None else ""))
        return dst

    def export_tiled_model(self):
        t = self.chunk.tiled_model or {}
        if not t.get("dir") or not os.path.exists(t["dir"]):
            QMessageBox.information(self, "Export Tiled Model", "Build the tiled model first."); return
        dst, f = QFileDialog.getSaveFileName(self, "Export Tiled Model", "tiled_model",
                                             "Zip archive (*.zip);;Folder (*)")
        if dst:
            self.export_tiled_to(dst, "zip" if "zip" in f.lower() else "folder")

    def export_tiled_to(self, dst, kind):
        from .tiles import zip_tiles
        src = self.chunk.tiled_model["dir"]
        if kind == "zip":
            if not dst.lower().endswith(".zip"):
                dst += ".zip"
            zip_tiles(src, dst)
        else:
            if os.path.exists(dst) and os.listdir(dst):
                dst = os.path.join(dst, os.path.basename(src.rstrip("/\\")))
            shutil.copytree(src, dst, dirs_exist_ok=True)
        self.log(f"Exported tiled model to {dst}")
        return dst

    # ------------------------------------------------------------ DEM / orthomosaic
    def _world_bounds_guess(self, ch):
        """Default region (world X/Y) for the dialogs: existing ortho/DEM, else the cameras."""
        for d in (ch.orthomosaic, ch.dem):
            if d and d.get("bounds"):
                return list(d["bounds"])
        cams = np.array([c.est for c in ch.cameras if c.est], float)
        if not len(cams):
            return None
        off = self._world_offset()
        off = np.zeros(3) if off is None else off
        lo, hi = cams[:, :2].min(0) + off[:2], cams[:, :2].max(0) + off[:2]
        return [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]

    def _crs_text(self, ch):
        return (ch.georef or {}).get("crs") or ch.crs or ""

    def load_ortho_view(self):
        """Orthomosaic + DEM layers of the active chunk in the Ortho tab (tiles load in the background)."""
        ch = self.chunk
        from .geotiff import is_geographic
        crs = self._crs_text(ch)
        geo = bool(ch.georef)
        for d in (ch.orthomosaic, ch.dem):       # an imported georeferenced raster in a local project
            if not geo and d and d.get("georef"):
                geo, crs = True, crs or d.get("crs") or ""
        self.ortho_view.set_frame(self._world_offset(), geographic=geo and is_geographic(crs),
                                  unit="m" if geo else "")
        for kind, d in (("dem", ch.dem), ("ortho", ch.orthomosaic)):
            ok = bool(d and d.get("path") and os.path.exists(d["path"]))
            try:
                cur = self.ortho_view.layers.get(kind)
                if not ok:
                    if cur is not None:
                        self.ortho_view.set_raster(kind, None)
                elif cur is None or cur.info.get("path") != d["path"]:
                    self.ortho_view.set_raster(kind, d)
            except Exception as e:  # noqa: BLE001
                self.log(f"Cannot show {'DEM' if kind == 'dem' else 'orthomosaic'}: {e}")
        for k in ("ortho", "dem", "markers", "marker_labels"):
            self.ortho_view.set_visible(k, self.layer_action(k).isChecked())
        self._update_ortho_markers()

    def _update_ortho_markers(self):
        """Markers in the Ortho tab (world XY): reference if georeferenced, else the
        estimate, else the position triangulated from the photos (e.g. a marker
        just added in the ortho view), mapped into the ortho's frame."""
        ch = self.chunk
        mk = []
        off = self._world_offset()
        tri = None
        for m in ch.markers or []:
            ref = m.get("ref")
            if ref is not None and len(ref) >= 2 and ref[0] is not None and ch.georef:
                mk.append((m["label"], float(ref[0]), float(ref[1])))
            elif m.get("est"):
                e = np.asarray(m["est"], float) + (off if off is not None else 0)
                mk.append((m["label"], float(e[0]), float(e[1])))
            elif m.get("projections"):
                if tri is None:
                    try:
                        tri = {lab: x for lab, x, _e in (self._marker_positions() or [])}
                    except Exception:  # noqa: BLE001
                        tri = {}
                X = tri.get(m["label"])
                if X is not None:
                    P = self.ortho_from_local(X)
                    mk.append((m["label"], float(P[0]), float(P[1])))
        self.ortho_view.set_markers(mk)

    def show_ortho(self, kind):
        """Model > Orthomosaic / DEM (and the Workspace rows): show the layer in the Ortho tab."""
        if not self._has_layer(kind):
            self.statusBar().showMessage(("No orthomosaic" if kind == "ortho" else "No DEM") + " - build it first", 4000)
            return
        self.set_layer_visible(kind, True)
        self.load_ortho_view()
        self.tabs.setCurrentWidget(self.ortho_view)

    def build_dem(self):
        if not self._require_saved("Build DEM"):
            return None
        ch = self.chunk
        have_cloud = bool(ch.point_cloud_ply and os.path.exists(ch.point_cloud_ply))
        have_mesh = bool(ch.model_ply and os.path.exists(ch.model_ply))
        if not (have_cloud or have_mesh):
            QMessageBox.information(self, "Build DEM", "Build the point cloud (or the model) first."); return
        sources = (["Point cloud"] if have_cloud else []) + (["Mesh"] if have_mesh else [])
        dlg = BuildDEMDialog(self, sources, crs=self._crs_text(ch), unit="m" if ch.georef else "units",
                             bounds=self._world_bounds_guess(ch))
        if not dlg.exec():
            return
        s = dlg.settings()
        wd = self.project.default_workdir(ch)
        os.makedirs(wd, exist_ok=True)
        cfg = dict(s, cloud=ch.point_cloud_ply, mesh=ch.model_ply, model=self._model_dir(ch),
                   photos=self._photo_map(ch) if not ch.georef else None, georef=ch.georef or {},
                   crs=self._crs_text(ch), out_dir=os.path.join(wd, "dem"), result=os.path.join(wd, "dem_result.json"))
        cfg_path = os.path.join(wd, "build_dem.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        self.ortho_view.set_raster("dem", None)           # release the file that is about to be replaced
        steps = [("Building DEM", [cm.PYTHON, "build_dem", cfg_path], 1)]
        res = {}

        def loaded():
            with open(cfg["result"], encoding="utf-8") as f:
                res.update(json.load(f))
            ch.dem = res
            self.set_modified()
            self.show_ortho("dem")

        def meta(dt, stages):
            procmeta.record(ch, "dem", params=dict(s), seconds=dt, stages=res.get("stages") or stages,
                            width=res.get("width"), height=res.get("height"), pixel_size=res.get("pixel_size"))
        self._run("Build DEM", steps, loaded, meta, params=s, on_fail=self.load_ortho_view)

    def build_orthomosaic(self):
        if not self._require_saved("Build Orthomosaic"):
            return None
        ch = self.chunk
        mdir = self._model_dir(ch)
        if not os.path.exists(os.path.join(mdir, "images.bin")):
            QMessageBox.information(self, "Build Orthomosaic", "Align photos first (no camera model found)."); return
        have_mesh = bool(ch.model_ply and os.path.exists(ch.model_ply))
        have_dem = bool(ch.dem and ch.dem.get("path") and os.path.exists(ch.dem["path"]))
        if not (have_mesh or have_dem):
            QMessageBox.information(self, "Build Orthomosaic", "Build the model (mesh) or a DEM first."); return
        surfaces = (["Mesh"] if have_mesh else []) + (["DEM"] if have_dem else [])
        from .ortho import estimate_gsd
        try:
            gsd = estimate_gsd(mdir, ch.tie_points_ply, bool(ch.georef), ch.texture)
        except Exception:  # noqa: BLE001
            gsd = 0.0
        plan = self._ortho_plan(ch, mdir, have_mesh, have_dem)
        dlg = BuildOrthomosaicDialog(self, surfaces, crs=self._crs_text(ch), unit="m" if ch.georef else "units",
                                     gsd=gsd, bounds=self._world_bounds_guess(ch), plan=plan)
        if not dlg.exec():
            return
        s = dlg.settings()
        s = self._check_ortho_size(s, plan, gsd, "m" if ch.georef else "units")
        if s is None:
            return
        wd = self.project.default_workdir(ch)
        os.makedirs(wd, exist_ok=True)
        cfg = dict(s, mesh=ch.model_ply, dem=(ch.dem or {}).get("path"), model=mdir, photos=self._photo_map(ch),
                   georef=ch.georef or {}, crs=self._crs_text(ch), out_dir=os.path.join(wd, "orthomosaic"),
                   result=os.path.join(wd, "ortho_result.json"))
        cfg_path = os.path.join(wd, "build_ortho.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        self.ortho_view.set_raster("ortho", None)
        steps = [("Building orthomosaic", [cm.PYTHON, "build_ortho", cfg_path], 1)]
        res = {}

        def loaded():
            with open(cfg["result"], encoding="utf-8") as f:
                res.update(json.load(f))
            ch.orthomosaic = res
            self.set_modified()
            self.show_ortho("ortho")

        def meta(dt, stages):
            procmeta.record(ch, "orthomosaic", params=dict(s), seconds=dt, stages=res.get("stages") or stages,
                            width=res.get("width"), height=res.get("height"), pixel_size=res.get("pixel_size"))
        # a failed / cancelled rebuild leaves the previous orthomosaic in place: show it again
        self._run("Build Orthomosaic", steps, loaded, meta, params=s, on_fail=self.load_ortho_view)

    def _ortho_plan(self, ch, mdir, have_mesh, have_dem):
        """Estimated orthomosaic extent for the dialog's size read-out (None if unknown)."""
        from .ortho import plan_orthomosaic
        try:
            if have_mesh:
                return plan_orthomosaic(mdir, ch.model_ply, ch.georef or None)
            if have_dem and ch.dem.get("bounds"):
                b, off = ch.dem["bounds"], np.asarray((ch.georef or {}).get("offset") or [0, 0, 0], float)
                return {"bounds": (b[0] - off[0], b[1] - off[1], b[2] - off[0], b[3] - off[1]), "off": off,
                        "clipped": False}
        except Exception as e:  # noqa: BLE001 - only the size read-out is missing
            self.log(f"Build Orthomosaic: no size estimate ({e})")
        return None

    def _check_ortho_size(self, s, plan, gsd, unit):
        """Refuse an orthomosaic that cannot be built before starting it, and offer
        a pixel size that works (issue #3). Returns the settings or None."""
        from .ortho import ORTHO_DEFAULTS, ortho_extent, raster_size, suggest_pixel_size, too_big_message
        if not plan:
            return s
        try:
            b = plan["bounds"]
            if s.get("region"):
                off = plan["off"]
                b = ortho_extent(b[:2], b[2:], None, s["region"], off[:2])[:4]
            px = float(s.get("pixel_size") or 0) or float(gsd or 0)
            if px <= 0 and not s.get("max_dimension"):
                return s
            px, W, H = raster_size(*b, px or 1e-12, s.get("max_dimension") or 0, plan["off"][:2])
        except Exception:  # noqa: BLE001 - the task checks again
            return s
        limit = float(ORTHO_DEFAULTS["max_pixels"])
        if W * H <= limit:
            return s
        sug = suggest_pixel_size(*b, limit, gsd)
        from .choice import ask_choice
        k = ask_choice(self, "Build Orthomosaic", too_big_message(W, H, px, gsd, unit, sug),
                       [("use", f"Use {sug:.4g} {unit}"), ("cancel", "Cancel")], default="use")
        if k != "use":
            return None
        self.log(f"Build Orthomosaic: pixel size {px:.4g} -> {sug:.4g} {unit} ({W:,} x {H:,} px would not fit)")
        return dict(s, pixel_size=sug, max_dimension=0)

    def import_raster(self, kind, path=None, quiet=False):
        """File > Import > Import Orthomosaic... / Import DEM...: a PhotoMesh output folder
        (info.json) or any georeferenced GeoTIFF (ModelPixelScale + ModelTiepoint)."""
        from .ortho import read_info
        title = "Import Orthomosaic" if kind == "ortho" else "Import DEM"
        ch = self.chunk
        if not path:
            path = QFileDialog.getOpenFileName(self, f"{title} - GeoTIFF (or info.json of an output folder)", "",
                                               "GeoTIFF / info.json (*.tif *.tiff info.json)")[0]
            if not path:
                return False
        if os.path.basename(path).lower() == "info.json":
            path = os.path.dirname(path)
        try:
            info = read_info(path)
            fmt = str(info.get("format", ""))
            if kind == "ortho" and fmt.startswith("photomesh-dem") or kind == "dem" and fmt.startswith("photomesh-ortho"):
                raise ValueError("this folder holds a " + ("DEM" if kind == "ortho" else "orthomosaic"))
        except (OSError, ValueError, KeyError) as e:
            if not quiet:
                QMessageBox.warning(self, title, f"Cannot import: {e}")
            self.log(f"{title}: {e}"); return False
        info.setdefault("georef", bool(ch.georef))
        info.setdefault("crs", self._crs_text(ch))
        if kind == "ortho":
            ch.orthomosaic = info
        else:
            ch.dem = info
        procmeta.record(ch, "orthomosaic" if kind == "ortho" else "dem", params=info.get("settings") or {},
                        seconds=info.get("seconds"), stages=info.get("stages"), width=info.get("width"),
                        height=info.get("height"), pixel_size=info.get("pixel_size"))
        ch.meta["orthomosaic" if kind == "ortho" else "dem"]["source"] = "imported"
        self.set_modified()
        self.show_ortho(kind)
        self.log(f"{title}: {info['width']} x {info['height']} px, {info['pixel_size']:.4g} m/pix from {path}")
        return True

    def export_raster(self, kind):
        ch = self.chunk
        info = ch.orthomosaic if kind == "ortho" else ch.dem
        title = "Export Orthomosaic" if kind == "ortho" else "Export DEM"
        if not (info and info.get("path") and os.path.exists(info["path"])):
            QMessageBox.information(self, title, "Build the " + ("orthomosaic" if kind == "ortho" else "DEM") + " first.")
            return
        dlg = ExportRasterDialog(self, kind, info)
        if not dlg.exec():
            return
        s = dlg.settings()
        ext = s["fmt"]
        filt = {"tif": "GeoTIFF (*.tif)", "jpg": "JPEG + world file (*.jpg)", "png": "PNG + world file (*.png)"}[ext]
        dst = QFileDialog.getSaveFileName(self, title, ("orthomosaic" if kind == "ortho" else "dem") + "." + ext, filt)[0]
        if dst:
            return self.export_raster_to(kind, dst, **s)

    def export_raster_to(self, kind, dst, **opts):
        """Export the orthomosaic / DEM (background thread, progress + Cancel)."""
        from .ortho import export_raster, Cancelled
        ch = self.chunk
        info = dict(ch.orthomosaic if kind == "ortho" else ch.dem)
        ext = opts.get("fmt") or os.path.splitext(dst)[1].lstrip(".").lower() or "tif"
        if not os.path.splitext(dst)[1]:
            dst += "." + ext
        title = "Export Orthomosaic" if kind == "ortho" else "Export DEM"
        t0 = time.perf_counter()

        def fn(report, cancelled):
            return export_raster(info, dst, kind=kind, log=lambda m: None,
                                 progress=lambda i, n: report(i, n, ""), cancel=cancelled, **opts)
        files, cancelled, err = run_modal(self, title, "Exporting...", fn, 100)
        if cancelled or isinstance(err, Cancelled):
            self.log(f"{title}: cancelled"); return None
        if err is not None:
            raise err if isinstance(err, BaseException) else RuntimeError(str(err))
        self.log(f"{title}: {', '.join(os.path.basename(f) for f in files[:6])}{' ...' if len(files) > 6 else ''} "
                 f"in {time.perf_counter() - t0:.1f} s (real world coordinates; world file written)")
        return files

    def merge_chunks(self):
        if len(self.project.chunks) < 2:
            QMessageBox.information(self, "Merge Chunks", "Need at least two chunks."); return
        merged = Chunk(name="Merged Chunk")
        for c in self.project.chunks:
            merged.add_photos([cam.path for cam in c.cameras],
                              {cam.path: {"lon": cam.lon, "lat": cam.lat, "alt": cam.alt, "thumb": cam.thumb,
                                          "desc": cam.desc, "width": cam.width, "height": cam.height}
                               for cam in c.cameras})
        self.project.chunks.append(merged)
        self.log("Merged photos of all chunks into a new chunk (re-align to reconstruct).")
        self.set_modified()

    # --------------------------------------------------------------- Tools
    def clean_up_data(self):
        """Tools > Clean Up Intermediate Data...: sizes per part, checkboxes,
        confirmation; finished products and photos are never deleted."""
        from . import datafolder as dfo
        from .cleanup_ui import CleanupDialog
        parts = dfo.cleanup_candidates(self.project)
        if not parts:
            QMessageBox.information(self, "Clean Up Intermediate Data",
                                    "There is no intermediate data to remove."); return None
        dlg = CleanupDialog(parts, self)
        if not dlg.exec():
            return None
        chosen = dlg.selected()
        if not chosen:
            return None
        size = sum(p["bytes"] for p in chosen)
        r = QMessageBox.question(self, "Clean Up Intermediate Data",
                                 f"Delete {dfo.fmt_size(size)} of intermediate data?\n\n"
                                 + "\n".join(f"  {p['chunk'] + ': ' if p['chunk'] else ''}{p['label']} "
                                              f"({dfo.fmt_size(p['bytes'])})" for p in chosen)
                                 + "\n\nFinished products (point cloud, model, texture, tiles, orthomosaic, DEM) "
                                   "and photos are not touched.",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return None
        res, cancelled, err = run_modal(self, "Clean Up Intermediate Data", "Deleting...",
                                        lambda report, cancel: dfo.delete_parts(chosen, self.project, report, cancel),
                                        len(chosen))
        if err is not None:
            self.show_error("Clean Up Intermediate Data", f"Deleting failed:\n{err}", step="Clean up", worker=False)
            return None
        freed, left = res or (0, [])
        self.log(f"Clean up: freed {dfo.fmt_size(freed)}" + (f"; not removed: {'; '.join(left)}" if left else ""))
        self.statusBar().showMessage(f"Freed {dfo.fmt_size(freed)}", 8000)
        if left:
            QMessageBox.warning(self, "Clean Up Intermediate Data",
                                "Could not remove (files in use?):\n" + "\n".join(left[:10]))
        return freed

    def model_statistics(self):
        ch = self.chunk
        QMessageBox.information(self, "Model Statistics",
            f"Cameras: {len(ch.cameras)} ({ch.aligned_count()} aligned)\nTie points: {ch.tie_point_count:,}\n"
            f"Point cloud: {ch.point_count:,}\nModel faces: {ch.face_count:,}")

    def run_script(self):
        path = QFileDialog.getOpenFileName(self, "Run Script", "", "Python (*.py)")[0]
        if path:
            with open(path, encoding="utf-8") as f:
                code = f.read()
            try:
                exec(compile(code, path, "exec"), dict(self.console.ns))
                self.log(f"Script {path} finished")
            except Exception as e:
                self.log(f"Script error: {e}")

    def preferences(self):
        if PreferencesDialog(self).exec():
            self._update_colmap_label()

    # ---------------------------------------------------------------- Help
    def help_contents(self):
        readme = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
        QDesktopServices.openUrl(QUrl.fromLocalFile(readme))

    def about(self):
        QMessageBox.about(self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br>Open-source photogrammetry front-end prototype.<br>"
            "Reconstruction engine: COLMAP (BSD), GLOMAP (BSD). Speed pipeline: fastpipe.<br>"
            "Runs fully offline: no network access at run time.<br>"
            "UI built with PySide6 / pyqtgraph.")

    def closeEvent(self, e):
        if self._confirm_discard():
            if getattr(self, "monitor", None) is not None:
                self.monitor.stop(0.5)
            self._view_gen += 1                  # cancel any background view load
            self.cancel_thumbnails()
            if self._thumb_job is not None:
                self._thumb_job.wait(5)
            e.accept()
        else:
            e.ignore()


def apply_dialog_setting():
    """Tools > Preferences > 'Use built-in file dialogs' (or PHOTOMESH_QT_DIALOGS=1):
    use Qt's own file dialogs instead of the Windows shell dialogs, which can hang
    on some systems (broken shell extensions, offline network/cloud drives)."""
    on = QSettings().value("ui/qt_file_dialogs", False, bool) or bool(os.environ.get("PHOTOMESH_QT_DIALOGS"))
    QApplication.instance().setAttribute(Qt.AA_DontUseNativeDialogs, on)
    return on
