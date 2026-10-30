"""Processing settings dialogs, progress dialog and preferences."""
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QComboBox, QSpinBox, QCheckBox, QDialogButtonBox,
    QVBoxLayout, QGroupBox, QLabel, QProgressBar, QPlainTextEdit, QPushButton,
    QHBoxLayout, QLineEdit, QFileDialog, QTabWidget, QWidget)

from . import colmap as cm


def _combo(items, current=None):
    c = QComboBox()
    c.addItems(items)
    if current:
        c.setCurrentText(current)
    return c


class _SettingsDialog(QDialog):
    """Base: 'General' group + collapsible 'Advanced' group, OK/Cancel."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        self.general = QGroupBox("General")
        self.gform = QFormLayout(self.general)
        self.advanced = QGroupBox("Advanced")
        self.advanced.setCheckable(True)
        self.advanced.setChecked(True)
        self.aform = QFormLayout(self.advanced)
        lay.addWidget(self.general)
        lay.addWidget(self.advanced)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AlignPhotosDialog(_SettingsDialog):
    def __init__(self, parent=None):
        super().__init__("Align Photos", parent)
        self.accuracy = _combo(cm.ACCURACY, "High")
        self.generic = QCheckBox("Generic preselection")
        self.generic.setChecked(True)
        self.reference = QCheckBox("Reference preselection")
        self.ref_mode = _combo(["Source", "Estimated", "Sequential"], "Sequential")
        self.reset = QCheckBox("Reset current alignment")
        self.mode = _combo(["Fast", "Exhaustive", "Sequential"], "Fast")
        self.mode.setToolTip("Fast: fastpipe pair preselection (GPS + global descriptor + sequence)\n"
                             "with coarse-to-fine alignment. Exhaustive: match all pairs.")
        self.c2f = QCheckBox("Coarse-to-fine (align downscaled first)")
        self.c2f.setChecked(True)
        self.mode.currentTextChanged.connect(lambda t: self.c2f.setEnabled(t == "Fast"))
        self.gform.addRow("Matching mode:", self.mode)
        self.mapper = _combo(["Auto", "Global", "Incremental", "Hierarchical"], "Auto")
        self.mapper.setToolTip("Camera pose estimation:\n"
                               "Auto: Global for 100+ photos (if GLOMAP is bundled), else Incremental\n"
                               "Global: GLOMAP - solves all cameras at once, fastest on large sets\n"
                               "Incremental: COLMAP, adds photos one by one (most robust, slow on large sets)\n"
                               "Hierarchical: COLMAP, splits the scene and solves parts in parallel")
        self.gform.addRow("Camera estimation:", self.mapper)
        self.gform.addRow(self.c2f)
        self.gform.addRow("Accuracy:", self.accuracy)
        self.gform.addRow(self.generic)
        row = QHBoxLayout()
        row.addWidget(self.reference)
        row.addWidget(self.ref_mode)
        self.gform.addRow(row)
        self.gform.addRow(self.reset)
        self.key_limit = QSpinBox(); self.key_limit.setRange(0, 1_000_000); self.key_limit.setValue(40000)
        self.tie_limit = QSpinBox(); self.tie_limit.setRange(0, 1_000_000); self.tie_limit.setValue(4000)
        self.exclude_stationary = QCheckBox("Exclude stationary tie points")
        self.exclude_stationary.setChecked(True)
        self.guided = QCheckBox("Guided image matching")
        self.adaptive = QCheckBox("Adaptive camera model fitting")
        self.single_camera = QCheckBox("Share one camera model per physical camera (lens + image size)")
        self.single_camera.setChecked(True)
        self.aform.addRow("Key point limit:", self.key_limit)
        self.aform.addRow("Tie point limit:", self.tie_limit)
        for w in (self.exclude_stationary, self.guided, self.adaptive, self.single_camera):
            self.aform.addRow(w)
        note = QLabel("<small>0 = no limit. Tie point limit and stationary-point "
                      "exclusion are informational only (no COLMAP equivalent).</small>")
        note.setWordWrap(True)
        self.aform.addRow(note)

    def settings(self):
        return {"accuracy": self.accuracy.currentText(),
                "generic": self.generic.isChecked(),
                "mode": self.mode.currentText(),
                "mapper": self.mapper.currentText(),
                "coarse_to_fine": self.c2f.isChecked(),
                "reference": True,
                "sequential": self.reference.isChecked() and self.ref_mode.currentText() == "Sequential",
                "reset": self.reset.isChecked(),
                "key_point_limit": self.key_limit.value(),
                "tie_point_limit": self.tie_limit.value(),
                "guided_matching": self.guided.isChecked(),
                "adaptive_fitting": self.adaptive.isChecked(),
                "single_camera": self.single_camera.isChecked(),
                "use_gpu": QSettings().value("gpu/use_gpu", True, bool) and _gpu_ok()}


class BuildPointCloudDialog(_SettingsDialog):
    def __init__(self, parent=None):
        super().__init__("Build Point Cloud", parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        self.source = _combo(["Depth maps"])
        self.quality = _combo(cm.QUALITY, "Medium")
        self.gform.addRow("Source data:", self.source)
        self.gform.addRow("Quality:", self.quality)
        self.filtering = _combo(cm.DEPTH_FILTERING, "Mild")
        self.colors = QCheckBox("Calculate point colors"); self.colors.setChecked(True)
        self.confidence = QCheckBox("Calculate point confidence"); self.confidence.setChecked(True)
        self.reuse = QCheckBox("Reuse depth maps")
        self.keep_depth = QCheckBox("Keep depth maps")
        self.keep_depth.setToolTip("Keep the depth maps after the point cloud is built (needed for 'Reuse "
                                   "depth maps'). They are large: at Ultra high about 16 bytes per photo "
                                   "pixel, e.g. ~200 GB for 640 photos of 20 MPix.")
        self.aform.addRow("Depth filtering:", self.filtering)
        for w in (self.colors, self.confidence, self.reuse, self.keep_depth):
            self.aform.addRow(w)
        self.sources = QSpinBox(); self.sources.setRange(2, 30); self.sources.setValue(8)
        self.step = _combo(["1 (slow, finest)", "2 (fast, default)"], "2 (fast, default)")
        self.iters = QSpinBox(); self.iters.setRange(1, 10); self.iters.setValue(3)
        self.aform.addRow("Neighbour photos per depth map:", self.sources)
        self.aform.addRow("Matching window step:", self.step)
        self.aform.addRow("PatchMatch iterations:", self.iters)
        self.outl = QGroupBox("Outlier filtering"); f = QFormLayout(self.outl)
        self.min_conf = QSpinBox(); self.min_conf.setRange(0, 50); self.min_conf.setValue(3)
        self.sor = QCheckBox("Statistical outlier removal"); self.sor.setChecked(True)
        self.radius = QCheckBox("Radius outlier removal"); self.radius.setChecked(True)
        self.elev = QCheckBox("Remove points far above/below the terrain"); self.elev.setChecked(True)
        f.addRow("Minimum confidence (photos):", self.min_conf)
        for w in (self.sor, self.radius, self.elev):
            f.addRow(w)
        self.layout().insertWidget(2, self.outl)

    def settings(self):
        return {"quality": self.quality.currentText(),
                "depth_filtering": self.filtering.currentText(),
                "colors": self.colors.isChecked(),
                "reuse": self.reuse.isChecked(),
                "keep_depth_maps": self.keep_depth.isChecked(),
                "max_sources": self.sources.value(),
                "window_step": 1 if self.step.currentIndex() == 0 else 2,
                "num_iterations": self.iters.value(),
                "filters": {"min_confidence": self.min_conf.value() if self.confidence.isChecked() else 0,
                            "statistical": self.sor.isChecked(), "radius": self.radius.isChecked(),
                            "elevation": self.elev.isChecked()}}


class BuildModelDialog(_SettingsDialog):
    def __init__(self, parent=None):
        super().__init__("Build Model", parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        self.source = _combo(["Point cloud", "Depth maps"], "Point cloud")
        self.surface = _combo(["Arbitrary (3D)", "Height field (2.5D)"])
        self.face_count = _combo(["High", "Medium", "Low", "Custom"], "Medium")
        self.custom = QSpinBox(); self.custom.setRange(1000, 200_000_000); self.custom.setValue(1_000_000)
        self.custom.setSingleStep(100000)
        self.gform.addRow("Source data:", self.source)
        self.gform.addRow("Surface type:", self.surface)
        self.gform.addRow("Face count:", self.face_count)
        self.gform.addRow("Custom face count:", self.custom)
        self.interp = _combo(["Disabled", "Enabled (default)", "Extrapolated"], "Enabled (default)")
        self.vcolors = QCheckBox("Calculate vertex colors"); self.vcolors.setChecked(True)
        self.voxel = QDoubleSpinBox(); self.voxel.setDecimals(3); self.voxel.setRange(0, 1000)
        self.voxel.setSpecialValueText("Auto (fit memory)")
        self.max_pts = QSpinBox(); self.max_pts.setRange(100_000, 100_000_000); self.max_pts.setValue(12_000_000)
        self.max_pts.setSingleStep(1_000_000)
        self.small = QDoubleSpinBox(); self.small.setRange(0, 50); self.small.setValue(1.0); self.small.setSuffix(" % of largest")
        self.long_edge = QDoubleSpinBox(); self.long_edge.setRange(0, 100); self.long_edge.setValue(8.0)
        self.long_edge.setSuffix(" x median edge"); self.long_edge.setSpecialValueText("Off")
        self.aform.addRow("Interpolation:", self.interp)
        self.aform.addRow(self.vcolors)
        self.aform.addRow("Voxel downsample:", self.voxel)
        self.aform.addRow("Max points for meshing:", self.max_pts)
        self.aform.addRow("Remove components smaller than:", self.small)
        self.aform.addRow("Remove faces with edges longer than:", self.long_edge)

    def settings(self):
        return {"source": self.source.currentText(),
                "surface": self.surface.currentText(),
                "face_count": self.face_count.currentText(),
                "custom_faces": self.custom.value(),
                "interpolation": ["Disabled", "Enabled", "Extrapolated"][self.interp.currentIndex()],
                "voxel": self.voxel.value() or None, "max_points": self.max_pts.value(),
                "min_component": self.small.value() / 100.0, "long_edge_factor": self.long_edge.value()}


class FilterPointCloudDialog(_SettingsDialog):
    """Tools > Point Cloud > Filter Point Cloud / Filter by Confidence."""
    def __init__(self, parent=None, confidence_only=False):
        super().__init__("Filter by Confidence" if confidence_only else "Filter Point Cloud", parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        self.min_conf = QSpinBox(); self.min_conf.setRange(0, 255); self.min_conf.setValue(3)
        self.max_conf = QSpinBox(); self.max_conf.setRange(1, 65535); self.max_conf.setValue(65535)
        self.gform.addRow("Minimum confidence:", self.min_conf)
        self.gform.addRow("Maximum confidence:", self.max_conf)
        self.sor = QCheckBox("Statistical outlier removal"); self.sor.setChecked(not confidence_only)
        self.k = QSpinBox(); self.k.setRange(4, 100); self.k.setValue(16)
        self.std = QDoubleSpinBox(); self.std.setRange(0.5, 10); self.std.setValue(2.0)
        self.radius = QCheckBox("Radius outlier removal"); self.radius.setChecked(not confidence_only)
        self.rmin = QSpinBox(); self.rmin.setRange(1, 100); self.rmin.setValue(4)
        self.rval = QDoubleSpinBox(); self.rval.setDecimals(4); self.rval.setRange(0, 1e6)
        self.rval.setSpecialValueText("Auto (3x point spacing)")
        self.elev = QCheckBox("Remove points far above/below the terrain")
        for w in (self.sor,):
            self.aform.addRow(w)
        self.aform.addRow("Neighbours (k):", self.k); self.aform.addRow("Std ratio:", self.std)
        self.aform.addRow(self.radius)
        self.aform.addRow("Radius:", self.rval); self.aform.addRow("Min neighbours:", self.rmin)
        self.aform.addRow(self.elev)
        self.advanced.setVisible(not confidence_only)

    def settings(self):
        return {"min_confidence": self.min_conf.value(), "max_confidence": self.max_conf.value(),
                "statistical": self.sor.isChecked(), "sor_k": self.k.value(), "sor_std": self.std.value(),
                "radius": self.radius.isChecked(), "radius_min": self.rmin.value(),
                "radius_value": self.rval.value() or None, "elevation": self.elev.isChecked()}


class GradualSelectionDialog(QDialog):
    """Select (and delete) tie points by reprojection error, reconstruction
    uncertainty, image count or projection accuracy."""
    def __init__(self, metrics, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        from .pcfilter import CRITERIA, gradual_select
        self.setWindowTitle("Gradual Selection")
        self.metrics = metrics
        self._sel = gradual_select
        lay = QVBoxLayout(self); f = QFormLayout()
        self.crit = _combo([c for c in CRITERIA if c in metrics])
        self.level = QDoubleSpinBox(); self.level.setDecimals(3); self.level.setRange(0, 1e9)
        self.info = QLabel()
        f.addRow("Criterion:", self.crit); f.addRow("Level:", self.level); f.addRow(self.info)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Delete selected")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject); lay.addWidget(bb)
        self.crit.currentTextChanged.connect(self._crit); self.level.valueChanged.connect(self._update)
        self._crit(self.crit.currentText())

    def _crit(self, c):
        import numpy as np
        v = self.metrics[c]
        self.level.setValue(3 if c == "Image count" else float(np.percentile(v, 90)) if len(v) else 0)
        self._update()

    def selection(self):
        return self._sel(self.metrics, self.crit.currentText(), self.level.value())

    def _update(self, *_):
        s = self.selection()
        self.info.setText(f"{int(s.sum()):,} of {len(s):,} tie points selected")


class OptimizeCamerasDialog(_SettingsDialog):
    """Tools > Optimize Cameras: georeference with markers / camera GPS and
    run the GCP-constrained bundle adjustment."""
    def __init__(self, parent=None, settings=None):
        super().__init__("Optimize Cameras", parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        from .georef import DEFAULTS
        s = dict(DEFAULTS); s.update(settings or {})
        self.use_markers = QCheckBox("Use control markers (XYZ + placements on 2 or more aligned photos)")
        self.use_markers.setChecked(True)
        self.use_markers.setToolTip("At least 3 non-collinear controls are needed to georeference. "
                                   "Without sufficient reference data, local bundle adjustment is offered.")
        self.use_cameras = QCheckBox("Use camera GPS (geographic/UTM coordinate systems)")
        self.use_cameras.setChecked(bool(s.get("use_cameras", False)))
        self.bundle = QCheckBox("Bundle adjustment (otherwise similarity transform only)"); self.bundle.setChecked(True)
        self.intr = QCheckBox("Fit focal length, principal point and k1"); self.intr.setChecked(True)
        for w in (self.use_markers, self.use_cameras, self.bundle, self.intr):
            self.gform.addRow(w)
        def dspin(v, dec=3, mx=1000):
            w = QDoubleSpinBox(); w.setDecimals(dec); w.setRange(0.0001, mx); w.setValue(v); return w
        self.cam_acc = dspin(s["camera_accuracy"]); self.cam_acc_z = dspin(s["camera_accuracy_z"])
        self.mk_acc = dspin(s["marker_accuracy"], 4); self.mk_acc_z = dspin(s["marker_accuracy_z"], 4)
        self.proj_acc = dspin(s["projection_accuracy"], 2, 50); self.tie_acc = dspin(s["tie_accuracy"], 2, 50)
        self.ties = QSpinBox(); self.ties.setRange(1000, 1_000_000); self.ties.setValue(40000); self.ties.setSingleStep(10000)
        self.aform.addRow("Camera accuracy XY / Z (m):", _pair(self.cam_acc, self.cam_acc_z))
        self.aform.addRow("Marker accuracy XY / Z (m):", _pair(self.mk_acc, self.mk_acc_z))
        self.aform.addRow("Marker projection accuracy (px):", self.proj_acc)
        self.aform.addRow("Tie point accuracy (px):", self.tie_acc)
        self.aform.addRow("Tie points used in adjustment:", self.ties)

    def settings(self):
        return {"use_markers": self.use_markers.isChecked(), "use_cameras": self.use_cameras.isChecked(),
                "bundle": self.bundle.isChecked(), "fit_intrinsics": self.intr.isChecked(),
                "tie_points": self.ties.value(),
                "accuracies": {"camera_accuracy": self.cam_acc.value(), "camera_accuracy_z": self.cam_acc_z.value(),
                               "marker_accuracy": self.mk_acc.value(), "marker_accuracy_z": self.mk_acc_z.value(),
                               "projection_accuracy": self.proj_acc.value(), "tie_accuracy": self.tie_acc.value()}}


def _pair(a, b):
    w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.addWidget(a); l.addWidget(b)
    return w


class BuildTextureDialog(_SettingsDialog):
    """Workflow > Build Texture (Metashape-like options, see texture.py)."""
    def __init__(self, parent=None, has_uv=False):
        super().__init__("Build Texture", parent)
        st = QSettings()
        self.ttype = _combo(["Diffuse map"])
        self.source = _combo(["Images"])
        self.mapping = _combo(["Generic", "Adaptive orthophoto", "Orthophoto"] + (["Keep uv"] if has_uv else []),
                              st.value("texture/mapping", "Generic", str))
        self.blending = _combo(["Mosaic (default)", "Average", "Max intensity", "Min intensity", "Disabled"],
                               st.value("texture/blending", "Mosaic (default)", str))
        self.size = _combo(["1024", "2048", "4096", "8192", "16384"], st.value("texture/size", "8192", str))
        self.size.setEditable(True)
        self.count = QSpinBox(); self.count.setRange(1, 64); self.count.setValue(st.value("texture/count", 1, int))
        row = QWidget(); hl = QHBoxLayout(row); hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.size, 1); hl.addWidget(QLabel("x")); hl.addWidget(self.count)
        self.gform.addRow("Texture type:", self.ttype)
        self.gform.addRow("Source data:", self.source)
        self.gform.addRow("Mapping mode:", self.mapping)
        self.gform.addRow("Blending mode:", self.blending)
        self.gform.addRow("Texture size/count:", row)
        self.holes = QCheckBox("Enable hole filling"); self.holes.setChecked(st.value("texture/holes", True, bool))
        self.ghost = QCheckBox("Enable ghosting filter"); self.ghost.setChecked(st.value("texture/ghost", True, bool))
        self.res = _combo(["Working resolution (2640 px, fast)", "Full resolution (slow)"],
                          st.value("texture/res", "Working resolution (2640 px, fast)", str))
        self.fmt = _combo(["JPEG", "PNG"], st.value("texture/format", "JPEG", str))
        self.aform.addRow(self.holes); self.aform.addRow(self.ghost)
        self.aform.addRow("Photo resolution:", self.res)
        self.aform.addRow("Texture file format:", self.fmt)

    def settings(self):
        st = QSettings()
        for k, w in (("mapping", self.mapping), ("blending", self.blending), ("size", self.size),
                     ("res", self.res), ("format", self.fmt)):
            st.setValue(f"texture/{k}", w.currentText())
        st.setValue("texture/count", self.count.value())
        st.setValue("texture/holes", self.holes.isChecked()); st.setValue("texture/ghost", self.ghost.isChecked())
        try:
            size = max(256, min(32768, int(self.size.currentText())))
        except ValueError:
            size = 8192
        return {"mapping": self.mapping.currentText(), "blending": self.blending.currentText().replace(" (default)", ""),
                "size": size, "count": self.count.value(), "hole_filling": self.holes.isChecked(),
                "ghosting": self.ghost.isChecked(), "max_res": 0 if self.res.currentIndex() == 1 else 2640,
                "format": "png" if self.fmt.currentText() == "PNG" else "jpg"}


class BuildTiledModelDialog(_SettingsDialog):
    """Workflow > Build Tiled Model (see tiles.py)."""
    def __init__(self, parent=None, sources=("Model",), default_pixel=0.0, unit="m"):
        super().__init__("Build Tiled Model", parent)
        from PySide6.QtWidgets import QDoubleSpinBox
        st = QSettings()
        self.source = _combo(list(sources), st.value("tiles/source", sources[0], str))
        self.pixel = QDoubleSpinBox(); self.pixel.setDecimals(4); self.pixel.setRange(0, 1e6)
        self.pixel.setSpecialValueText("Auto (texture resolution)"); self.pixel.setValue(0)
        self.pixel.setSuffix(f" {unit}")
        self.tile_faces = QSpinBox(); self.tile_faces.setRange(2000, 1_000_000); self.tile_faces.setSingleStep(10000)
        self.tile_faces.setValue(st.value("tiles/tile_faces", 60000, int))
        self.max_tex = _combo(["512", "1024", "2048", "4096"], st.value("tiles/max_tex", "2048", str))
        self.fmt = _combo(["Cesium 3D Tiles 1.0 (b3dm)", "Cesium 3D Tiles 1.1 (glb)"],
                          st.value("tiles/format", "Cesium 3D Tiles 1.0 (b3dm)", str))
        self.gform.addRow("Source data:", self.source)
        self.gform.addRow("Pixel size:", self.pixel)
        self.gform.addRow("Tile format:", self.fmt)
        self.aform.addRow("Faces / points per tile:", self.tile_faces)
        self.aform.addRow("Max tile texture size:", self.max_tex)

    def settings(self):
        st = QSettings()
        st.setValue("tiles/source", self.source.currentText()); st.setValue("tiles/tile_faces", self.tile_faces.value())
        st.setValue("tiles/max_tex", self.max_tex.currentText()); st.setValue("tiles/format", self.fmt.currentText())
        return {"source": self.source.currentText(), "pixel_size": self.pixel.value(),
                "tile_faces": self.tile_faces.value(), "tile_points": self.tile_faces.value() * 4,
                "max_tex": int(self.max_tex.currentText()),
                "format": "glb" if "glb" in self.fmt.currentText() else "b3dm"}


class ProgressDialog(QDialog):
    """Modal-ish processing progress window with log output and Cancel."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(520, 320)
        lay = QVBoxLayout(self)
        self.stage = QLabel("Starting...")
        self.stage.setWordWrap(True)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000)       # 0.1 % steps
        self.bar.setFormat("%p%")
        from .progress import WorkProgress
        from PySide6.QtCore import QTimer
        self._meter = WorkProgress()
        self._stage_text = ""
        self.stage_bar = QProgressBar(); self.stage_bar.setRange(0, 0)
        self.timing = QLabel("Estimating remaining time..."); self.timing.setWordWrap(True)
        self._timer = QTimer(self); self._timer.setInterval(1000)
        self._timer.timeout.connect(lambda: self.timing.setText(self._stage_text + "\n" + self._meter.text()))
        self._timer.start()
        self.finished.connect(lambda *_: self._timer.stop())
        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.cancel_btn = QPushButton("Cancel")
        lay.addWidget(self.stage)
        lay.addWidget(self.bar)
        self.bar.setFormat("Overall (weighted estimate): %p%")
        lay.addWidget(self.stage_bar)
        lay.addWidget(self.timing)
        lay.addWidget(self.log, 1)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.running = True
        self._label, self._detail, self._pct = "", "", 0.0

    def reject(self):
        if self.running:
            self.cancel_btn.click()
            self.stage.setText("Cancelling - waiting for the current operation to stop...")
        else:
            super().reject()

    def closeEvent(self, event):
        if self.running:
            self.reject(); event.ignore()
        else:
            event.accept()

    def _show(self):
        txt = f"{self._label}...  {self._pct:.1f}%"
        if self._detail:
            txt += f"\n{self._detail}"
        self.stage.setText(txt)

    def set_progress(self, pct, label):
        """Step progress (int %); never moves the bar backwards (a retried
        step adds weight to the total)."""
        if label != self._label:
            self._label, self._detail = label, ""
        self._pct = max(self._pct, float(pct)) if pct < 100 else 100.0
        self.bar.setValue(int(round(self._pct * 10)))
        self._show()

    def set_stage_progress(self, fraction, text):
        self._stage_text = text
        if fraction < 0:
            self._meter.reset()
            self.stage_bar.setRange(0, 0)
        else:
            self._meter.update(fraction * 1000, 1000)
            self.stage_bar.setRange(0, 1000)
            self.stage_bar.setValue(min(1000, int(fraction * 1000)))
        self.timing.setText(text + "\n" + self._meter.text())

    def set_detail(self, pct, text):
        """Fine progress inside a step: e.g. depth-map pass, view and ETA."""
        self._pct = max(self._pct, float(pct))
        self._detail = text
        self.bar.setValue(int(round(self._pct * 10)))
        self._show()

    def append(self, line):
        self.log.appendPlainText(line)


class PreferencesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(460)
        s = QSettings()
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        gen = QWidget(); gf = QFormLayout(gen)
        self.theme = _combo(["Dark", "Light"], s.value("ui/theme", "Dark", str))
        gf.addRow("Theme:", self.theme)
        self.default_view = _combo(["Tie points", "Point cloud", "Model"], s.value("ui/default_view", "Point cloud", str))
        gf.addRow("Default view:", self.default_view)
        self.qt_dialogs = QCheckBox("Use built-in file dialogs (if the Windows Open/Add dialogs hang)")
        self.qt_dialogs.setChecked(s.value("ui/qt_file_dialogs", False, bool))
        gf.addRow(self.qt_dialogs)
        tabs.addTab(gen, "General")
        gpu = QWidget(); gl = QFormLayout(gpu)
        self.use_gpu = QCheckBox("Use GPU for feature extraction/matching")
        self.use_gpu.setChecked(s.value("gpu/use_gpu", True, bool))
        if not _gpu_ok():
            self.use_gpu.setToolTip("No NVIDIA driver found: COLMAP runs on the CPU "
                                    "(alignment works, slower; dense point cloud needs CUDA)")
        gl.addRow(self.use_gpu)
        tabs.addTab(gpu, "GPU")
        adv = QWidget(); af = QFormLayout(adv)
        row = QHBoxLayout()
        self.colmap = QLineEdit(s.value("colmap/path", "", str))
        self.colmap.setPlaceholderText("auto-detect from PATH")
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse)
        row.addWidget(self.colmap); row.addWidget(b)
        af.addRow("COLMAP executable:", row)
        found = cm.find_colmap()
        af.addRow("Detected:", QLabel(found or "<i>not found</i>"))
        tabs.addTab(adv, "Advanced")
        lay.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "COLMAP executable")
        if p:
            self.colmap.setText(p)

    def _save(self):
        s = QSettings()
        s.setValue("ui/theme", self.theme.currentText())
        s.setValue("ui/default_view", self.default_view.currentText())
        s.setValue("gpu/use_gpu", self.use_gpu.isChecked())
        s.setValue("ui/qt_file_dialogs", self.qt_dialogs.isChecked())
        from .mainwindow import apply_dialog_setting
        apply_dialog_setting()
        s.setValue("colmap/path", self.colmap.text().strip())
        self.accept()


def _gpu_ok():
    from .colmap import nvidia_driver_present
    return nvidia_driver_present()


def _dspin(lo, hi, val, dec=4, suffix="", special=None):
    from PySide6.QtWidgets import QDoubleSpinBox
    s = QDoubleSpinBox(); s.setDecimals(dec); s.setRange(lo, hi); s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    if special:
        s.setSpecialValueText(special)
    return s


class _RegionBox(QGroupBox):
    """Optional region (world X/Y bounds) - Metashape's 'Region' setup in 2D."""
    def __init__(self, bounds=None, unit="m"):
        super().__init__("Region")
        self.setCheckable(True); self.setChecked(False)
        f = QFormLayout(self)
        b = bounds or [0, 0, 0, 0]
        self.spins = [_dspin(-1e9, 1e9, float(v), 3, f" {unit}") for v in b]
        f.addRow("X min / Y min:", _pair(self.spins[0], self.spins[1]))
        f.addRow("X max / Y max:", _pair(self.spins[2], self.spins[3]))
        self.setToolTip("Limit the output to these real-world X/Y bounds (project coordinate system)")

    def region(self):
        if not self.isChecked():
            return None
        v = [s.value() for s in self.spins]
        return v if v[2] > v[0] and v[3] > v[1] else None


class BuildDEMDialog(_SettingsDialog):
    """Workflow > Build DEM (from the point cloud or the mesh, see ortho.py)."""
    def __init__(self, parent=None, sources=("Point cloud",), crs="", unit="m", bounds=None):
        super().__init__("Build DEM", parent)
        st = QSettings()
        self.proj = QLabel(crs or "Local Coordinates (m)"); self.proj.setWordWrap(True)
        self.source = _combo(list(sources), st.value("dem/source", sources[0], str))
        self.interp = _combo(["Enabled (default)", "Disabled", "Extrapolated"],
                             st.value("dem/interp", "Enabled (default)", str))
        self.pixel = _dspin(0, 1e6, 0.0, 4, f" {unit}", "Auto (from point density)")
        self.gform.addRow("Projection:", self.proj)
        self.gform.addRow("Source data:", self.source)
        self.gform.addRow("Interpolation:", self.interp)
        self.aform.addRow("Resolution:", self.pixel)
        self.region_box = _RegionBox(bounds, unit)
        self.layout().insertWidget(2, self.region_box)

    def settings(self):
        st = QSettings()
        st.setValue("dem/source", self.source.currentText()); st.setValue("dem/interp", self.interp.currentText())
        return {"source": self.source.currentText(), "interpolation": self.interp.currentText().replace(" (default)", ""),
                "pixel_size": self.pixel.value(), "region": self.region_box.region()}


class BuildOrthomosaicDialog(_SettingsDialog):
    """Workflow > Build Orthomosaic (Metashape-like options, see ortho.py)."""
    def __init__(self, parent=None, surfaces=("Mesh",), crs="", unit="m", gsd=0.0, bounds=None, plan=None):
        super().__init__("Build Orthomosaic", parent)
        from PySide6.QtWidgets import QRadioButton
        st = QSettings()
        self.proj = QLabel((crs or "Local Coordinates (m)") + "  -  Top XY (planar)"); self.proj.setWordWrap(True)
        self.surface = _combo(list(surfaces), st.value("ortho/surface", surfaces[0], str))
        if self.surface.currentText() not in surfaces:
            self.surface.setCurrentIndex(0)
        self.blending = _combo(["Mosaic (default)", "Average", "Disabled"],
                               st.value("ortho/blending", "Mosaic (default)", str))
        self.holes = QCheckBox("Enable hole filling"); self.holes.setChecked(st.value("ortho/holes", True, bool))
        self.ghost = QCheckBox("Enable ghosting filter"); self.ghost.setChecked(st.value("ortho/ghost", True, bool))
        self.seams = QCheckBox("Refine seamlines"); self.seams.setChecked(st.value("ortho/seams", False, bool))
        self.r_pix = QRadioButton("Pixel size:"); self.r_max = QRadioButton("Max. dimension:")
        self.r_pix.setChecked(True)
        self.gsd = float(gsd or 0)
        self.pixel = _dspin(0, 1e6, self.gsd, 4, f" {unit}", "Auto (photo GSD)")
        self.maxdim = QSpinBox(); self.maxdim.setRange(256, 1_000_000); self.maxdim.setValue(st.value("ortho/maxdim", 16384, int))
        self.maxdim.setSuffix(" px")
        self.gform.addRow("Projection:", self.proj)
        self.gform.addRow("Surface:", self.surface)
        self.gform.addRow("Blending mode:", self.blending)
        self.gform.addRow(self.r_pix, self.pixel)
        self.gform.addRow(self.r_max, self.maxdim)
        if self.gsd:
            self.gform.addRow("", QLabel(f"Default = estimated photo GSD ({self.gsd:.4g} {unit}/pix)"))
        # Metashape-like "Total size (pix)" read-out, checked before the build starts
        self.plan, self.unit = plan, unit
        self.size_label = QLabel(""); self.size_label.setWordWrap(True)
        if plan:
            self.gform.addRow("Total size (pix):", self.size_label)
        for sig in (self.pixel.valueChanged, self.maxdim.valueChanged, self.r_pix.toggled):
            sig.connect(self._update_size)
        self.aform.addRow(self.holes); self.aform.addRow(self.ghost); self.aform.addRow(self.seams)
        self.res = _combo(["Full resolution photos (best)", "Half resolution photos (faster)"],
                          st.value("ortho/res", "Full resolution photos (best)", str))
        self.aform.addRow("Photo resolution:", self.res)
        self.region_box = _RegionBox(bounds, unit)
        self.layout().insertWidget(2, self.region_box)
        self.region_box.toggled.connect(self._update_size)
        for sp in self.region_box.spins:
            sp.valueChanged.connect(self._update_size)
        self._update_size()

    def estimated_size(self):
        """(W, H, pixel size) of the result for the current settings, or None."""
        if not self.plan:
            return None
        from .ortho import ortho_extent, raster_size
        try:
            b, off = self.plan["bounds"], self.plan["off"]
            reg = self.region_box.region()
            if reg:
                b = ortho_extent(b[:2], b[2:], None, reg, off[:2])[:4]
            if self.r_max.isChecked():
                px, W, H = raster_size(*b, 1e-12, self.maxdim.value(), off[:2])
            else:
                px = self.pixel.value() or self.gsd
                if px <= 0:
                    return None
                px, W, H = raster_size(*b, px, 0, off[:2])
            return W, H, px
        except Exception:  # noqa: BLE001 - e.g. a region outside the surface
            return None

    def _update_size(self, *_):
        from .ortho import ORTHO_DEFAULTS
        e = self.estimated_size()
        if e is None:
            self.size_label.setText("-"); return
        W, H, px = e
        big = W * H > ORTHO_DEFAULTS["max_pixels"]
        txt = f"~ {W:,} x {H:,}  ({W * H / 1e6:,.0f} Mpx)"
        if big:
            txt += " - too large, increase the pixel size"
        elif self.gsd and px < self.gsd / 1.5:
            txt += f" - finer than the photos ({self.gsd:.4g} {self.unit}/pix)"
        self.size_label.setText(txt)
        self.size_label.setStyleSheet("color: #d04040;" if big else "")

    def settings(self):
        st = QSettings()
        for k, w in (("surface", self.surface), ("blending", self.blending), ("res", self.res)):
            st.setValue(f"ortho/{k}", w.currentText())
        st.setValue("ortho/holes", self.holes.isChecked()); st.setValue("ortho/ghost", self.ghost.isChecked())
        st.setValue("ortho/seams", self.seams.isChecked()); st.setValue("ortho/maxdim", self.maxdim.value())
        use_max = self.r_max.isChecked()
        return {"surface": self.surface.currentText(), "blending": self.blending.currentText().replace(" (default)", ""),
                "hole_filling": self.holes.isChecked(), "ghosting": self.ghost.isChecked(),
                "refine_seamlines": self.seams.isChecked(),
                "pixel_size": 0.0 if use_max else self.pixel.value(),
                "max_dimension": self.maxdim.value() if use_max else 0,
                "max_res": 2640 if self.res.currentIndex() == 1 else 0, "region": self.region_box.region()}


class ExportRasterDialog(_SettingsDialog):
    """File > Export > Export Orthomosaic / Export DEM - raster options."""
    def __init__(self, parent=None, kind="ortho", info=None):
        super().__init__("Export DEM" if kind == "dem" else "Export Orthomosaic", parent)
        st = QSettings()
        info = info or {}
        self.kind = kind
        px = info.get("pixel_size") or 0
        self.fmt = _combo(["GeoTIFF (*.tif)"] if kind == "dem" else
                          ["GeoTIFF (*.tif)", "JPEG + world file (*.jpg)", "PNG + world file (*.png)"],
                          st.value(f"export_{kind}/fmt", "GeoTIFF (*.tif)", str))
        self.comp = _combo(["LZW", "Deflate", "None"] + ([] if kind == "dem" else ["JPEG"]),
                           st.value(f"export_{kind}/comp", "LZW", str))
        self.quality = QSpinBox(); self.quality.setRange(10, 100); self.quality.setValue(st.value(f"export_{kind}/q", 90, int))
        self.alpha = QCheckBox("Write alpha channel (otherwise nodata = 0)")
        self.alpha.setChecked(st.value(f"export_{kind}/alpha", True, bool))
        self.ovr = QCheckBox("Write tile overviews"); self.ovr.setChecked(st.value(f"export_{kind}/ovr", True, bool))
        self.big = _combo(["Auto (BigTIFF above 4 GB)", "Yes", "No"])
        levels = int(info.get("levels") or 1)
        self.scale = _combo([f"1/{2 ** k}  ({px * 2 ** k:.4g} m/pix, {info.get('width', 0) // 2 ** k} x "
                             f"{info.get('height', 0) // 2 ** k} px)" for k in range(max(1, min(levels, 6)))])
        self.maxdim = QSpinBox(); self.maxdim.setRange(0, 1_000_000); self.maxdim.setValue(0)
        self.maxdim.setSpecialValueText("No limit"); self.maxdim.setSuffix(" px")
        self.split = QSpinBox(); self.split.setRange(0, 200_000); self.split.setSingleStep(4096)
        self.split.setValue(st.value(f"export_{kind}/split", 0, int)); self.split.setSpecialValueText("No (one file)")
        self.split.setSuffix(" px")
        self.gform.addRow("Format:", self.fmt)
        self.gform.addRow("Resolution:", self.scale)
        self.gform.addRow("Max. dimension:", self.maxdim)
        self.gform.addRow("Split in blocks:", self.split)
        self.aform.addRow("TIFF compression:", self.comp)
        self.aform.addRow("JPEG quality:", self.quality)
        if kind != "dem":
            self.aform.addRow(self.alpha)
        self.aform.addRow(self.ovr)
        self.aform.addRow("Write BigTIFF:", self.big)
        note = QLabel("Coordinates are written in the real world system (offset included): GeoTIFF tags + "
                      "world file (.tfw / .jgw / .pgw)" + ("; a Rhino placement script and notes are written "
                                                          "next to the file." if kind != "dem" else "."))
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)

    def ext(self):
        t = self.fmt.currentText()
        return "jpg" if "JPEG" in t else "png" if "PNG" in t else "tif"

    def settings(self):
        st = QSettings()
        k = self.kind
        st.setValue(f"export_{k}/fmt", self.fmt.currentText()); st.setValue(f"export_{k}/comp", self.comp.currentText())
        st.setValue(f"export_{k}/q", self.quality.value()); st.setValue(f"export_{k}/alpha", self.alpha.isChecked())
        st.setValue(f"export_{k}/ovr", self.ovr.isChecked()); st.setValue(f"export_{k}/split", self.split.value())
        return {"fmt": self.ext(), "compression": self.comp.currentText(), "jpeg_quality": self.quality.value(),
                "alpha": self.alpha.isChecked() if k != "dem" else False, "overviews": self.ovr.isChecked(),
                "bigtiff": {0: "auto", 1: "yes", 2: "no"}[self.big.currentIndex()],
                "scale": 2 ** self.scale.currentIndex(), "max_dimension": self.maxdim.value(),
                "split": self.split.value()}


class ImportShapesDialog(_SettingsDialog):
    """File > Import > Import Shapes... - layer, source coordinate system and heights."""
    SRC = ["From file (.prj / KML / GeoJSON)", "Project coordinate system (world)",
           "Project local coordinates (add offset)", "WGS 84 (EPSG::4326)", "WGS 84 / UTM zone", "Custom (WKT / EPSG)"]
    SRC_KEYS = ["file", "project", "local", "wgs84", "utm", "custom"]
    ZMODE = ["Attach to surface (shapes without Z)", "Attach all shapes to surface", "Keep Z from file",
             "Fixed Z for shapes without Z"]
    ZKEYS = ["drape_missing", "drape_all", "keep", "fixed"]

    def __init__(self, parent=None, path="", detected="", project_crs="", surfaces=(), has_shapes=False,
                 notes=(), stats=""):
        super().__init__("Import Shapes", parent)
        import os
        st = QSettings()
        self.layer = QLineEdit(os.path.splitext(os.path.basename(path))[0])
        self.src = _combo(self.SRC)
        self.src.setCurrentIndex(0)
        self.zone = QSpinBox(); self.zone.setRange(1, 60); self.zone.setValue(st.value("shapes/zone", 38, int))
        self.south = QCheckBox("Southern hemisphere")
        self.custom = QLineEdit(); self.custom.setPlaceholderText("EPSG:32638 or PROJCS[...] WKT")
        self.zmode = _combo(self.ZMODE); self.zmode.setCurrentIndex(st.value("shapes/zmode", 0, int))
        self.surface = _combo([s for s in surfaces] or ["(none)"])
        self.fixed = QLineEdit(st.value("shapes/fixed_z", "0", str))
        self.replace = QCheckBox("Replace existing shapes"); self.replace.setEnabled(has_shapes)
        f = QLabel(f"<b>{os.path.basename(path)}</b> - {stats}<br>File coordinate system: {detected or 'not given'}"
                   f"<br>Project: {project_crs}")
        f.setWordWrap(True)
        self.layout().insertWidget(0, f)
        self.gform.addRow("Layer name:", self.layer)
        self.gform.addRow("Coordinate system:", self.src)
        utm = QWidget(); h = QHBoxLayout(utm); h.setContentsMargins(0, 0, 0, 0); h.addWidget(self.zone)
        h.addWidget(self.south)
        self.gform.addRow("UTM zone:", utm)
        self.gform.addRow("Custom CRS:", self.custom)
        self.aform.addRow("Heights:", self.zmode)
        self.aform.addRow("Surface:", self.surface)
        self.aform.addRow("Fixed Z (m):", self.fixed)
        self.aform.addRow(self.replace)
        if notes:
            n = QLabel("\n".join(notes[:6])); n.setWordWrap(True); n.setStyleSheet("color: gray")
            self.layout().insertWidget(3, n)

    def settings(self):
        st = QSettings()
        st.setValue("shapes/zone", self.zone.value()); st.setValue("shapes/zmode", self.zmode.currentIndex())
        st.setValue("shapes/fixed_z", self.fixed.text())
        try:
            fz = float(self.fixed.text().replace(",", "."))
        except ValueError:
            fz = 0.0
        return {"layer": self.layer.text().strip(), "src": self.SRC_KEYS[self.src.currentIndex()],
                "zone": self.zone.value(), "south": self.south.isChecked(), "custom": self.custom.text().strip(),
                "z_mode": self.ZKEYS[self.zmode.currentIndex()], "surface": self.surface.currentText(),
                "fixed_z": fz, "replace": self.replace.isChecked()}


class ExportShapesDialog(_SettingsDialog):
    """File > Export > Export Shapes... - format, coordinate system, layers."""
    FMTS = ["Shapefile (*.shp)", "AutoCAD DXF R12 (*.dxf)", "GeoJSON (*.geojson)"]

    def __init__(self, parent=None, layers=(), systems=()):
        super().__init__("Export Shapes", parent)
        st = QSettings()
        self.fmt = _combo(self.FMTS, st.value("shapes/export_fmt", self.FMTS[0], str))
        self.crs = _combo(list(systems) or ["Project coordinate system"])
        self.gform.addRow("Format:", self.fmt)
        self.gform.addRow("Coordinate system:", self.crs)
        self.checks = []
        for name, n, vis in layers:
            c = QCheckBox(f"{name} ({n})"); c.setChecked(True); c.layer_name = name
            self.aform.addRow(c); self.checks.append(c)
        note = QLabel("Coordinates are written in the real world system (offset included). Shapefiles get a .prj "
                      "for georeferenced projects; mixed geometry types are split into _points / _lines / "
                      "_polygons files.")
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)

    def ext(self):
        t = self.fmt.currentText()
        return "dxf" if "DXF" in t else "geojson" if "GeoJSON" in t else "shp"

    def settings(self):
        QSettings().setValue("shapes/export_fmt", self.fmt.currentText())
        return {"fmt": self.ext(), "crs_index": self.crs.currentIndex(), "crs": self.crs.currentText(),
                "layers": [c.layer_name for c in self.checks if c.isChecked()]}
