"""Dockable panes: Workspace, Reference, Markers, Photos, Console, Properties, Jobs."""
from __future__ import annotations

import collections
import contextlib
import io
import os

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QPixmap, QImageReader, QColor, QBrush
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem, QListWidget,
    QListWidgetItem, QWidget, QVBoxLayout, QPlainTextEdit, QLineEdit, QMenu,
    QHeaderView, QLabel, QListView, QAbstractItemView)

ROLE = Qt.UserRole


def _fmt(v, nd=6):
    return "" if v is None else f"{v:.{nd}f}"


@contextlib.contextmanager
def bulk_fill(table):
    """Fill a QTableWidget fast. With QHeaderView.ResizeToContents every
    setItem() re-measured the whole column, so refilling the 641-camera
    Reference table took ~2 minutes (O(rows^2)) and froze the window after
    File > Save. Fill with resizing/painting/signals off, size columns once."""
    hdr = table.horizontalHeader()
    table.setUpdatesEnabled(False)
    blocked = table.blockSignals(True)
    sorting = table.isSortingEnabled(); table.setSortingEnabled(False)
    hdr.setSectionResizeMode(QHeaderView.Interactive)
    try:
        yield
    finally:
        table.setSortingEnabled(sorting)
        table.blockSignals(blocked)
        table.resizeColumnsToContents()
        table.setUpdatesEnabled(True)


# Workspace rows that mirror a 3D-view layer (row kind -> layer key)
ROW_LAYER = {"cameras": "cameras", "markers": "markers", "tie": "tie", "cloud": "cloud", "model": "mesh",
             "tiled": "tiled", "ortho": "ortho", "dem": "dem", "shapes": "shapes"}
MAX_SHAPE_ROWS = 400        # per layer; larger layers list the first ones (all are drawn)



def _outdated(prod):
    """Label suffix for a product made from an earlier model (see MainWindow._model_replaced)."""
    return " - outdated, rebuild" if isinstance(prod, dict) and prod.get("stale") else ""


class WorkspacePane(QTreeWidget):
    """Metashape-style project tree:

    Workspace (N chunks, M images)
      Chunk 1 (M images, K markers, T tie points) [R]
        Images (a/n aligned)            [camera groups per sensor if > 1]
        Markers (K)
        Tie Points (T points)
        Depth Maps (n, Q quality, F filtering)
        Point Cloud (P points, Q quality)
        3D Model (F faces, ...)

    Only products that exist get a row. Rows of the active chunk that are also
    3D-view layers carry a visibility checkbox (synced with the layer toggles);
    double-click shows the item."""
    cameraActivated = Signal(int, int)      # chunk index, camera index
    chunkActivated = Signal(int)
    contextAction = Signal(str, int)        # action, chunk index
    selectionInfo = Signal(dict)
    layerToggled = Signal(str, bool)        # layer key, visible
    showRequested = Signal(str, int)        # row kind, chunk index
    markerActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIconSize(QSize(16, 16))
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemDoubleClicked.connect(self._dbl)
        self.itemSelectionChanged.connect(self._sel)
        self.itemChanged.connect(self._changed)
        self.vis = {}
        self._layer_items = {}
        self._filling = False
        self.shape_provider = None      # callable(chunk index) -> shape set (shapes.py) or None

    @staticmethod
    def _icon(key):
        from .icons import icon
        return icon(key)

    def _item(self, parent, text, role, icon=None, layer=None, bold=False, tip=None):
        it = QTreeWidgetItem([text])
        it.setData(0, ROLE, role)
        if icon:
            it.setIcon(0, self._icon(icon))
        if bold:
            f = it.font(0); f.setBold(True); it.setFont(0, f)
        if tip:
            it.setToolTip(0, tip)
        if layer is not None:
            it.setCheckState(0, Qt.Checked if self.vis.get(layer, True) else Qt.Unchecked)
            self._layer_items[layer] = it
        (parent.addChild if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem)(it)
        return it

    def refresh(self, project):
        from . import procmeta as pm
        expanded = {self._key(i) for i in self._iter() if i.isExpanded()}
        first = not expanded
        self._filling = True
        self.setUpdatesEnabled(False)
        try:
            self.clear(); self._layer_items = {}
            n_img = sum(len(c.cameras) for c in project.chunks)
            root = self._item(self, f"Workspace ({len(project.chunks)} chunks, {n_img:,} images)", ("workspace",),
                              "workspace")
            for ci, ch in enumerate(project.chunks):
                active = ci == project.active
                parts = [f"{len(ch.cameras):,} images"]
                if ch.markers:
                    parts.append(f"{len(ch.markers):,} markers")
                if ch.tie_point_count:
                    parts.append(f"{ch.tie_point_count:,} tie points")
                geo = " [R]" if ch.georef else ""
                crs = (ch.georef or {}).get("crs") or ch.crs
                it = self._item(root, f"{ch.name} ({', '.join(parts)}){geo}", ("chunk", ci), "chunk", bold=active,
                                tip=("Georeferenced: " + crs) if ch.georef else "Not georeferenced (local frame)")
                lay = (lambda k: k) if active else (lambda k: None)
                al = ch.aligned_count()
                imgs = self._item(it, f"Images ({al:,}/{len(ch.cameras):,} aligned)", ("cameras", ci), "folder",
                                  layer=lay("cameras"))
                groups = pm.camera_groups(ch)
                if len(groups) > 1:
                    parents = {g[3]: self._item(imgs, f"{g[0]} ({g[1]:,}/{g[2]:,} aligned)", ("group", ci, g[3]),
                                                "folder") for g in groups}
                else:
                    parents = None
                aligned_run = bool(ch.tie_point_count)
                for k, c in enumerate(ch.cameras):
                    par = parents[(c.width, c.height) if c.width and c.height else None] if parents else imgs
                    na = aligned_run and not c.aligned
                    ci_ = QTreeWidgetItem([c.label + ("  (NA)" if na else "")])
                    ci_.setData(0, ROLE, ("camera", ci, k))
                    ci_.setIcon(0, self._icon("photo" if c.enabled else "photo_off"))
                    ci_.setCheckState(0, Qt.Checked if c.enabled else Qt.Unchecked)
                    if na:
                        ci_.setForeground(0, QBrush(QColor(200, 120, 120)))
                        ci_.setToolTip(0, "Not aligned")
                    elif not c.enabled:
                        ci_.setForeground(0, QBrush(QColor(130, 130, 130)))
                        ci_.setToolTip(0, "Disabled")
                    par.addChild(ci_)
                if ch.markers:
                    nctl = sum(1 for m in ch.markers if m.get("enabled", True))
                    mk = self._item(it, f"Markers ({len(ch.markers):,})", ("markers", ci), "markers",
                                    layer=lay("markers"),
                                    tip=f"{nctl} control, {len(ch.markers) - nctl} check")
                    for m in ch.markers:
                        ctl = m.get("enabled", True)
                        r = self._item(mk, m["label"] + ("" if ctl else "  (check)"), ("marker", ci, m["label"]),
                                       "markers")
                        if not ctl:
                            r.setForeground(0, QBrush(QColor(120, 210, 240)))
                if ch.tie_point_count:
                    self._item(it, f"Tie Points ({pm.summary(ch, 'tie')})", ("tie", ci), "tie", layer=lay("tie"))
                dm = (ch.meta or {}).get("depth_maps")
                if dm:
                    self._item(it, f"Depth Maps ({pm.summary(ch, 'depth')})", ("depth", ci), "depth")
                if ch.point_cloud_ply:
                    self._item(it, f"Point Cloud ({pm.summary(ch, 'cloud')})", ("cloud", ci), "cloud",
                               layer=lay("cloud"))
                if ch.model_ply:
                    self._item(it, f"3D Model ({pm.summary(ch, 'model')})", ("model", ci), "mesh",
                               layer=lay("mesh"))
                if getattr(ch, "tiled_model", None):
                    self._item(it, f"Tiled Model ({pm.summary(ch, 'tiled')})" + _outdated(ch.tiled_model), ("tiled", ci), "mesh",
                               layer=lay("tiled"))
                if getattr(ch, "dem", None):
                    self._item(it, f"DEM ({pm.summary(ch, 'dem')})" + _outdated(ch.dem), ("dem", ci), "dem", layer=lay("dem"),
                               tip="Digital elevation model - double-click to show it in the Ortho view")
                if getattr(ch, "orthomosaic", None):
                    o = ch.orthomosaic
                    tip = (f"Created {o.get('created', '?')}; surface {o.get('surface')}, blending {o.get('blending')}"
                           f", hole filling {'on' if o.get('hole_filling') else 'off'}, ghosting filter "
                           f"{'on' if o.get('ghosting') else 'off'}")
                    self._item(it, f"Orthomosaic ({pm.summary(ch, 'ortho')})" + _outdated(ch.orthomosaic), ("ortho", ci), "ortho",
                               layer=lay("ortho"), tip=tip)
                sset = self.shape_provider(ci) if (self.shape_provider and getattr(ch, "shapes", None)) else None
                if sset and sset.get("shapes"):
                    self._shape_rows(it, ci, ch, sset, lay("shapes"))
                it.setExpanded(True)
            root.setExpanded(True)
            if not first:
                for i in self._iter():
                    if self._key(i) in expanded:
                        i.setExpanded(True)
        finally:
            self.setUpdatesEnabled(True)
            self._filling = False

    def _shape_rows(self, parent, ci, ch, sset, layer):
        from .shapes import shape_name
        from PySide6.QtGui import QPixmap, QIcon
        n = len(sset["shapes"])
        top = self._item(parent, f"Shapes ({n:,})", ("shapes", ci), "shapes", layer=layer,
                         tip="Imported shapes - File > Import > Import Shapes...")
        hidden = set((ch.shapes or {}).get("hidden") or [])
        by = {}
        for k, s in enumerate(sset["shapes"]):
            by.setdefault(s["layer"], []).append(k)
        for li, L in enumerate(sset["layers"]):
            idx = by.get(li, [])
            r = QTreeWidgetItem([f"{L['name']} ({len(idx):,})"])
            r.setData(0, ROLE, ("shape_layer", ci, L["name"]))
            pm = QPixmap(14, 14); pm.fill(QColor(*[int(v) for v in L.get("color", (255, 140, 0))[:3]]))
            r.setIcon(0, QIcon(pm))
            r.setCheckState(0, Qt.Unchecked if L["name"] in hidden else Qt.Checked)
            src = L.get("source") or ""
            r.setToolTip(0, f"{L.get('format', '')} {src}".strip())
            top.addChild(r)
            for k in idx[:MAX_SHAPE_ROWS]:
                s = sset["shapes"][k]
                c = QTreeWidgetItem([shape_name(s, k)])
                c.setData(0, ROLE, ("shape", ci, k))
                c.setIcon(0, self._icon("shapes"))
                r.addChild(c)
            if len(idx) > MAX_SHAPE_ROWS:
                more = QTreeWidgetItem([f"... {len(idx) - MAX_SHAPE_ROWS:,} more"])
                more.setDisabled(True); r.addChild(more)

    def _iter(self):
        stack = [self.topLevelItem(k) for k in range(self.topLevelItemCount())]
        while stack:
            it = stack.pop()
            yield it
            stack += [it.child(k) for k in range(it.childCount())]

    @staticmethod
    def _key(it):
        d = it.data(0, ROLE)
        return tuple(d) if d else None

    def set_layer_states(self, states):
        """Mirror layer visibility (from menu/toolbar) in the row checkboxes."""
        self.vis.update(states)
        self._filling = True
        try:
            for key, on in states.items():
                it = self._layer_items.get(key)
                if it is not None:
                    it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        finally:
            self._filling = False

    def _changed(self, item, _col=0):
        if self._filling:
            return
        d = item.data(0, ROLE)
        if not d:
            return
        on = item.checkState(0) == Qt.Checked
        if d[0] in ROW_LAYER and self._layer_items.get(ROW_LAYER[d[0]]) is item:
            self.vis[ROW_LAYER[d[0]]] = on
            self.layerToggled.emit(ROW_LAYER[d[0]], on)
        elif d[0] == "camera":
            self.contextAction.emit(f"set_camera:{d[2]}:{int(on)}", d[1])
        elif d[0] == "shape_layer":
            self.contextAction.emit(f"shape_layer:{int(on)}:{d[2]}", d[1])

    def _dbl(self, item, _):
        d = item.data(0, ROLE)
        if not d:
            return
        if d[0] == "camera":
            self.cameraActivated.emit(d[1], d[2])
        elif d[0] == "chunk":
            self.chunkActivated.emit(d[1])
        elif d[0] == "marker":
            self.markerActivated.emit(d[2])
        elif d[0] in ("tie", "cloud", "model", "markers", "cameras", "depth", "tiled", "ortho", "dem", "shapes"):
            self.showRequested.emit(d[0], d[1])
        elif d[0] == "shape":
            self.contextAction.emit(f"show_shape:{d[2]}", d[1])
        elif d[0] == "shape_layer":                   # double-click a layer: its style window
            self.contextAction.emit(f"shape_style:layer:{d[2]}", d[1])

    def selected_roles(self):
        return [tuple(i.data(0, ROLE)) for i in self.selectedItems() if i.data(0, ROLE)]

    def _sel(self):
        items = self.selectedItems()
        if items and items[0].data(0, ROLE):
            d = items[0].data(0, ROLE)
            if d[0] == "workspace":
                return
            self.selectionInfo.emit({"kind": d[0], "chunk": d[1], "index": d[2] if len(d) > 2 else None})

    def _menu(self, pos):
        item = self.itemAt(pos)
        d = item.data(0, ROLE) if item else None
        ci = d[1] if d and len(d) > 1 else -1
        m = QMenu(self)
        for name in ("Add Chunk", "Set as Active Chunk", "Rename Chunk...", "Duplicate Chunk", "Remove Chunk"):
            a = m.addAction(name)
            a.setEnabled(name == "Add Chunk" or ci >= 0)
        if d and d[0] == "camera":
            m.addSeparator(); m.addAction("Open Photo"); m.addAction("Enable/Disable Camera")
        if d and d[0] in ("camera", "cameras", "group", "chunk"):
            m.addSeparator(); m.addAction("Change Image Paths...")
        if d and d[0] in ("tie", "cloud", "model", "tiled", "ortho", "dem", "shapes"):
            m.addSeparator(); m.addAction("Show")
        if d and d[0] in ("marker", "markers"):
            m.addSeparator(); m.addAction("Add Marker")
            if d[0] == "marker":
                m.addAction("Rename Marker..."); m.addAction("Remove Marker")
            m.addAction("Filter Photos by Markers"); m.addAction("Reset Filter")
        if d and d[0] in ("shapes", "shape_layer", "shape"):
            m.addSeparator()
            m.addAction({"shapes": "Shape Settings...", "shape_layer": "Layer Style...",
                         "shape": "Shape Style..."}[d[0]])
            m.addAction("Export Shapes...")
            m.addAction("Remove Layer" if d[0] == "shape_layer" else "Remove Shapes")
            if d[0] == "shape":
                m.addAction("Measure Shape...")
        a = m.exec(self.viewport().mapToGlobal(pos))
        if a:
            if a.text() == "Open Photo":
                self.cameraActivated.emit(d[1], d[2])
            elif a.text() == "Enable/Disable Camera":
                self.contextAction.emit("toggle_camera:%d" % d[2], ci)
            elif a.text() == "Show":
                self.showRequested.emit(d[0], d[1])
            elif a.text() == "Remove Layer":
                self.contextAction.emit(f"remove_shape_layer:{d[2]}", ci)
            elif a.text() == "Layer Style..." and d[0] == "shape_layer":
                self.contextAction.emit(f"shape_style:layer:{d[2]}", ci)
            elif a.text() == "Shape Style...":
                self.contextAction.emit(f"shape_style:shape:{d[2]}", ci)
            elif a.text() == "Shape Settings...":
                self.contextAction.emit("shape_style:all:", ci)
            elif a.text() == "Measure Shape...":
                self.contextAction.emit(f"measure_shape:{d[2]}", ci)
            elif a.text() in ("Rename Marker...", "Remove Marker", "Filter Photos by Markers", "Reset Filter",
                              "Add Marker"):
                labels = [r[2] for r in self.selected_roles() if r[0] == "marker"] or \
                    ([d[2]] if d[0] == "marker" else [])
                if a.text() == "Filter Photos by Markers" and d[0] == "markers" and not labels:
                    labels = ["*"]
                self.contextAction.emit("marker:" + a.text() + ":" + "\x1f".join(labels), ci)
            else:
                self.contextAction.emit(a.text(), ci)


def workspace_panel(tree, actions):
    """The Workspace dock content: a small pane toolbar (PANE_ICON px icons) above the tree."""
    from PySide6.QtWidgets import QToolBar
    from .icons import PANE_ICON
    w = QWidget()
    lay = QVBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
    tb = QToolBar(); tb.setIconSize(QSize(PANE_ICON, PANE_ICON))
    tb.setStyleSheet("QToolBar{spacing:1px;padding:0px;border:0px;} QToolButton{padding:1px;margin:0px;}")
    for a in actions:
        if a is None:
            tb.addSeparator()
        else:
            tb.addAction(a)
    lay.addWidget(tb); lay.addWidget(tree)
    w.toolbar = tb
    return w


class ReferencePane(QTableWidget):
    COLS = ["Cameras", "Longitude", "Latitude", "Altitude (m)", "Accuracy (m)",
            "X est", "Y est", "Z est", "Aligned"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)

    def refresh(self, chunk):
        with bulk_fill(self):
            self.clearContents()
            self.setRowCount(len(chunk.cameras))
            for r, c in enumerate(chunk.cameras):
                est = c.est or [None, None, None]
                vals = [c.label, _fmt(c.lon), _fmt(c.lat), _fmt(c.alt, 3),
                        "10.000" if c.lat is not None else "", _fmt(est[0], 4),
                        _fmt(est[1], 4), _fmt(est[2], 4), "yes" if c.aligned else "no"]
                for k, v in enumerate(vals):
                    self.setItem(r, k, QTableWidgetItem(v))


class MarkersPane(QTableWidget):
    """Markers (ground control / check points) with reference and errors.

    Double-click a Markers / X / Y / Z / Accuracy cell to edit it (validated by
    the main window; Edit > Undo reverts). Double-click another cell opens the
    marker's photo. Right-click: Add / Rename / Remove Marker, Filter Photos
    by Markers, Reset Filter. The checkbox makes the marker a control (checked)
    or check (unchecked) point."""
    COLS = ["Markers", "X/Easting", "Y/Northing", "Z/Altitude", "Accuracy (m)",
            "Error (m)", "Projections", "Error (pix)"]
    EDITABLE = (0, 1, 2, 3, 4)
    markerActivated = Signal(str)
    markerToggled = Signal(str, bool)
    markerEdited = Signal(str, int, str)        # label, column, new text
    markerAction = Signal(str, list)            # menu action, selected labels

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self._busy = False
        self.pending = {}
        self.itemChanged.connect(self._changed)
        self.cellDoubleClicked.connect(self._dbl)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def _dbl(self, r, c):
        if c not in self.EDITABLE and self.item(r, 0):
            self.markerActivated.emit(self.item(r, 0).data(ROLE) or self.item(r, 0).text())

    def refresh(self, chunk):
        self._busy = True
        try:
            with bulk_fill(self):
                self._fill(chunk)
        finally:
            self._busy = False

    def _fill(self, chunk):
        ms = getattr(chunk, "markers", None) or []
        rows = {r.get("label"): r for r in (getattr(chunk, "georef", {}) or {}).get("rows", [])}
        self.clearContents()
        self.setRowCount(len(ms))
        for r, m in enumerate(ms):
            pend = (self.pending or {}).get(m["label"])
            ref = pend or m.get("ref") or [None, None, None]
            acc = m.get("acc")
            e = rows.get(m["label"], {})
            proj = m.get("projections", {})
            pinned = sum(1 for v in proj.values() if len(v) < 3 or v[2])
            vals = [m["label"], _fmt(ref[0], 4), _fmt(ref[1], 4), _fmt(ref[2], 4),
                    "" if not acc else (f"{acc[0]:.3f}" if acc[0] == acc[1] else f"{acc[0]:.3f}/{acc[1]:.3f}"),
                    _fmt(e.get("total"), 4), f"{pinned}/{len(proj)}", _fmt(e.get("pix"), 3)]
            for k, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(ROLE, m["label"])            # label before any edit
                if k not in self.EDITABLE:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if k == 0:
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(Qt.Checked if m.get("enabled", True) else Qt.Unchecked)
                    it.setToolTip("checked = control point, unchecked = check point; double-click to rename")
                    it.setData(ROLE + 1, bool(m.get("enabled", True)))
                elif k in (1, 2, 3, 4):
                    it.setToolTip("Double-click to edit" + (" (incomplete: enter X, Y and Z)" if pend else ""))
                    if pend:
                        f = it.font(); f.setItalic(True); it.setFont(f)
                        it.setForeground(QBrush(QColor(230, 160, 60)))
                elif k == 6:
                    it.setToolTip(f"{pinned} pinned / {len(proj)} placed (unpinned ones are blue flags)")
                self.setItem(r, k, it)

    def _changed(self, it):
        if self._busy:
            return
        label = it.data(ROLE) or it.text()
        if it.column() == 0:
            on = it.checkState() == Qt.Checked
            if it.data(ROLE + 1) is not None and bool(it.data(ROLE + 1)) != on:
                self._busy = True; it.setData(ROLE + 1, on); self._busy = False
                self.markerToggled.emit(label, on)
                return
            if it.text() != label:
                self.markerEdited.emit(label, 0, it.text())
            return
        if it.column() in self.EDITABLE:
            self.markerEdited.emit(label, it.column(), it.text())

    def current_label(self):
        r = self.currentRow()
        it = self.item(r, 0) if r >= 0 else None
        return (it.data(ROLE) or it.text()) if it else None

    def selected_labels(self):
        rows = sorted({i.row() for i in self.selectedIndexes()})
        out = []
        for r in rows:
            it = self.item(r, 0)
            if it:
                out.append(it.data(ROLE) or it.text())
        return out

    def _menu(self, pos):
        it = self.itemAt(pos)
        if it is not None and it.row() not in {i.row() for i in self.selectedIndexes()}:
            self.selectRow(it.row())
        labels = self.selected_labels()
        m = QMenu(self)
        m.addAction("Add Marker")
        for name in ("Rename Marker...", "Remove Markers", "-", "Show Marker", "Filter Photos by Markers",
                     "Reset Filter"):
            if name == "-":
                m.addSeparator(); continue
            a = m.addAction(name)
            a.setEnabled(bool(labels) or name == "Reset Filter")
        a = m.exec(self.viewport().mapToGlobal(pos))
        if a:
            self.markerAction.emit(a.text(), labels)


class PhotosPane(QListWidget):
    photoActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setIconSize(QSize(120, 90))
        self.setGridSize(QSize(140, 120))
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(4)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.itemDoubleClicked.connect(lambda it: self.photoActivated.emit(it.data(ROLE)))
        self._cache = {}

    _placeholder = None

    @classmethod
    def placeholder(cls):
        if cls._placeholder is None:
            pm = QPixmap(120, 90); pm.fill(QColor(70, 70, 76))
            from PySide6.QtGui import QPainter
            p = QPainter(pm); p.setPen(QColor(150, 150, 158)); p.drawRect(0, 0, 119, 89)
            p.drawText(pm.rect(), Qt.AlignCenter, "..."); p.end()
            cls._placeholder = QIcon(pm)
        return cls._placeholder

    def _icon(self, cam):
        """Small cached thumbnail only - the original photo (often 20+ MB) is
        never decoded here; thumbnails are made by a background job."""
        key = cam.thumb
        if not key or not os.path.exists(key):
            return self.placeholder()
        if key not in self._cache:
            self._cache[key] = QIcon(QPixmap(key))
        return self._cache[key]

    def set_thumb(self, k, cam):
        it = self.item(k)
        if it is not None and it.data(ROLE) == k:
            it.setIcon(self._icon(cam))

    filter = None                      # set of photo indices shown, or None (all)

    def set_filter(self, indices):
        """Filter Photos by Markers: show only these photo indices (None = all)."""
        self.filter = None if indices is None else set(indices)
        for r in range(self.count()):
            it = self.item(r)
            it.setHidden(self.filter is not None and it.data(ROLE) not in self.filter)

    def visible_indices(self):
        return [self.item(r).data(ROLE) for r in range(self.count()) if not self.item(r).isHidden()]

    def refresh(self, chunk):
        self.clear()
        for k, c in enumerate(chunk.cameras):
            it = QListWidgetItem(self._icon(c), c.label)
            it.setData(ROLE, k)
            tip = c.path + ("\naligned" if c.aligned else "\nnot aligned")
            if not c.enabled:
                it.setForeground(QBrush(QColor(120, 120, 120))); tip += "\n(disabled)"
            it.setToolTip(tip)
            self.addItem(it)
            if self.filter is not None and k not in self.filter:
                it.setHidden(True)


class ConsolePane(QWidget):
    """Log output plus a Python command line with access to the app."""

    def __init__(self, namespace: dict, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(2, 2, 2, 2)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(10000)
        f = self.out.font(); f.setFamily("Monospace"); f.setStyleHint(f.StyleHint.Monospace)
        self.out.setFont(f)
        self.inp = QLineEdit(); self.inp.setPlaceholderText(">>> Python (objects: app, window, project)")
        self.inp.returnPressed.connect(self._run)
        lay.addWidget(self.out, 1); lay.addWidget(self.inp)
        self.ns = namespace
        # plain-text history for error reports (readable from any thread)
        self.history = collections.deque(maxlen=20000)
        self.written = 0

    def mark(self):
        """Position in the history; lines_since(mark) gives what came after."""
        return self.written

    def lines_since(self, mark=0, n=None):
        lines = list(self.history)
        first = self.written - len(lines)
        lines = lines[max(0, mark - first):]
        return lines[-n:] if n else lines

    def write(self, text):
        for line in str(text).splitlines() or [""]:
            self.history.append(line)
            self.written += 1
        self.out.appendPlainText(text)
        sb = self.out.verticalScrollBar(); sb.setValue(sb.maximum())

    def _run(self):
        code = self.inp.text(); self.inp.clear()
        self.write(">>> " + code)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    r = eval(code, self.ns)
                    if r is not None:
                        print(repr(r))
                except SyntaxError:
                    exec(code, self.ns)
        except Exception as e:
            buf.write(f"{type(e).__name__}: {e}\n")
        if buf.getvalue():
            self.write(buf.getvalue().rstrip())


class PropertiesPane(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(["Property", "Value"])
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

    def show_props(self, props: dict):
        self.setUpdatesEnabled(False)
        self.setRowCount(len(props))
        for r, (k, v) in enumerate(props.items()):
            self.setItem(r, 0, QTableWidgetItem(str(k)))
            self.setItem(r, 1, QTableWidgetItem(str(v)))
        self.setUpdatesEnabled(True)


class JobsPane(QListWidget):
    def add_job(self, text):
        self.addItem(text); self.scrollToBottom()
