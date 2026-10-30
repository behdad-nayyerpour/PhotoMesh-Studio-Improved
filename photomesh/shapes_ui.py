"""Import / show / export of shapes in the main window (File > Import > Import
Shapes..., File > Export > Export Shapes..., Tools > Shapes, Workspace
"Shapes (N)", overlays in the Model, Ortho and Photo views)."""
from __future__ import annotations

import copy
import os
import re
import threading
import time

import numpy as np
from PySide6.QtCore import Signal, QObject
from PySide6.QtWidgets import QFileDialog, QMessageBox, QDialog, QPushButton

from . import shapes as S

IMPORT_FILTER = ("Shapes (*.shp *.dxf *.kml *.kmz *.geojson *.json *.csv *.txt);;ESRI Shapefile (*.shp);;"
                 "AutoCAD DXF (*.dxf);;KML / KMZ (*.kml *.kmz);;GeoJSON (*.geojson *.json);;CSV points (*.csv *.txt)")


class _Relay(QObject):
    occlusionReady = Signal(int, object)
    photoOcclusionReady = Signal(object, object)


class ShapesMixin:
    """Mixed into MainWindow; needs project, chunk, model_view, ortho_view,
    photo_view, workspace, properties, log(), set_modified(), _sparse_model()."""

    # ------------------------------------------------------------ data
    def _shapes_init(self):
        self._shape_cache = {}           # chunk id -> (key, sset)
        self._shapes_ver = 0
        self._photo_shape_cache = {}
        self._sampler_cache = {}
        self._occl = {"key": None, "scene": None, "busy": False}
        self._shape_relay = _Relay()
        self._shape_relay.occlusionReady.connect(self._occlusion_ready)
        self._shape_relay.photoOcclusionReady.connect(self._photo_occlusion_ready)
        self.selected_shape = None
        self.workspace.shape_provider = lambda ci: self._shape_set(self.project.chunks[ci])
        self.ortho_view.shapeClicked.connect(self._shape_clicked)

    def _shapes_file(self, ch):
        """Shape data of a chunk: <project>.files/shapes/<chunk>.json (the
        project's data folder; older projects keep their stored path).
        Unsaved projects: raises ProjectNotSaved (callers ask to save first)."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ch.name or "chunk")
        if not self.project.path:
            from .project import ProjectNotSaved
            raise ProjectNotSaved("Save the project before importing shapes.")
        return os.path.join(self.project.data_dir, "shapes", f"{safe}.json")

    def _shape_set(self, ch=None):
        """The chunk's shapes (loaded from its shapes.json, cached by file time)."""
        ch = ch or self.chunk
        info = ch.shapes or {}
        p = info.get("path")
        if not p or not os.path.exists(p):
            return None
        key = (p, os.path.getmtime(p))
        c = self._shape_cache.get(id(ch))
        if c is None or c[0] != key:
            try:
                sset = S.load(p)
            except Exception as e:  # noqa: BLE001
                self.log(f"Cannot read shapes {p}: {e}")
                return None
            c = self._shape_cache[id(ch)] = (key, sset)
            self._shapes_ver += 1
        sset = c[1]
        hidden = set(info.get("hidden") or [])
        for L in sset["layers"]:
            L["visible"] = L["name"] not in hidden
        return sset

    def _store_shapes(self, ch, sset):
        path = (ch.shapes or {}).get("path") or self._shapes_file(ch)
        S.save(sset, path)
        old = dict(ch.shapes or {})
        srcs = list(dict.fromkeys([L.get("source") for L in sset["layers"] if L.get("source")]))
        ch.shapes = {"path": path, "count": len(sset["shapes"]), "layers": len(sset["layers"]), "sources": srcs,
                     "hidden": [h for h in (old.get("hidden") or []) if h in {L["name"] for L in sset["layers"]}],
                     "modified": time.strftime("%Y-%m-%d %H:%M:%S")}
        names = {L["name"] for L in sset["layers"]}
        if old.get("styles"):                  # display styles survive re-imports (kept per layer name)
            ch.shapes["styles"] = {k: v for k, v in old["styles"].items() if k in names}
        if old.get("shape_styles"):
            n = len(sset["shapes"])
            ch.shapes["shape_styles"] = {k: v for k, v in old["shape_styles"].items() if int(k) < n}
        self._shape_cache.pop(id(ch), None)
        self._shapes_ver += 1
        self._photo_shape_cache.clear()

    def _frame(self, ch=None):
        return S.Frame.from_chunk(ch or self.chunk)

    def _project_bounds_world(self, ch=None):
        """World XY extent of the project (orthomosaic / DEM, else camera centres)."""
        ch = ch or self.chunk
        for d in (ch.orthomosaic, ch.dem):
            b = (d or {}).get("bounds")
            if b:
                return np.array([b[0], b[1]], float), np.array([b[2], b[3]], float)
        off = self._frame(ch).offset
        P = [np.asarray(c.est, float) + off for c in ch.cameras if c.est]
        if not P:
            return None
        P = np.array(P)
        return P.min(0), P.max(0)

    def _surfaces(self, ch=None):
        """Available draping surfaces: [(name, kind, path)] (DEM first)."""
        ch = ch or self.chunk
        out = []
        if (ch.dem or {}).get("path") and os.path.exists(ch.dem["path"]):
            out.append(("DEM", "raster", ch.dem["path"]))
        hp = (ch.orthomosaic or {}).get("height_path")
        if hp and os.path.exists(hp):
            out.append(("Orthomosaic height", "raster", hp))
        if ch.model_ply and os.path.exists(ch.model_ply) and ch.georef:     # Z-up frame needed for vertical rays
            out.append(("Model", "mesh", ch.model_ply))
        return out

    def _sampler(self, name=None, ch=None):
        ch = ch or self.chunk
        surf = self._surfaces(ch)
        if not surf:
            return None
        s = next((x for x in surf if x[0] == name), surf[0])
        key = (s[2], os.path.getmtime(s[2]), tuple(self._frame(ch).offset))
        if key not in self._sampler_cache:
            if s[1] == "raster":
                smp = S.HeightSampler.from_raster(s[2], s[0])
            else:
                smp = S.HeightSampler.from_mesh(s[2], self._frame(ch).offset, s[0])
            self._sampler_cache = {key: smp}          # keep one (DEMs / meshes are big)
        return self._sampler_cache[key]

    # ------------------------------------------------------------ import
    def import_shapes(self, path=None, options=None, far_action=None):
        """File > Import > Import Shapes... (SHP, DXF, KML/KMZ, GeoJSON, CSV).
        options: ImportShapesDialog.settings() keys (no dialog when given);
        far_action: 'offset' | 'wgs84' | 'anyway' | 'cancel' answers the
        far-away warning without asking (scripts / tests)."""
        if not self._require_saved("Import Shapes"):
            return None
        ch = self.chunk
        if path is None:
            from PySide6.QtCore import QSettings
            st = QSettings()
            path, _ = QFileDialog.getOpenFileName(self, "Import Shapes", st.value("paths/shapes", "", str),
                                                  IMPORT_FILTER)
            if not path:
                return None
            st.setValue("paths/shapes", os.path.dirname(path))
        sset, src, notes = S.read_any(path)
        if not sset["shapes"]:
            msg = f"No shapes found in {os.path.basename(path)}" + ("\n" + "\n".join(notes) if notes else "")
            if options is None:
                QMessageBox.warning(self, "Import Shapes", msg)
            self.log("Import Shapes: " + msg.replace("\n", "; "))
            return None
        frame = self._frame(ch)
        cur = self._shape_set(ch)
        surf = self._surfaces(ch)
        counts = {}
        for s in sset["shapes"]:
            counts[s["type"]] = counts.get(s["type"], 0) + 1
        stats = ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in counts.items())
        if options is None:
            from .dialogs import ImportShapesDialog
            dlg = ImportShapesDialog(self, path, src.describe() if src.kind != "project" else "",
                                     frame.describe() + (f", offset {tuple(np.round(frame.offset, 3))}"
                                                         if np.any(frame.offset) else ""),
                                     [x[0] for x in surf], bool(cur and cur["shapes"]), notes, stats)
            if dlg.exec() != QDialog.Accepted:
                return None
            options = dlg.settings()
        opt = {"layer": "", "src": "file", "zone": 0, "south": False, "custom": "", "z_mode": "drape_missing",
               "surface": surf[0][0] if surf else "", "fixed_z": 0.0, "replace": False}
        opt.update(options or {})
        add_offset = False
        sk = opt["src"]
        if sk == "project":
            src = S.SrcCRS("project")
        elif sk == "local":
            src, add_offset = S.SrcCRS("project"), True
        elif sk == "wgs84":
            src = S.SrcCRS("wgs84")
        elif sk == "utm":
            src = S.SrcCRS("utm", int(opt["zone"]), bool(opt["south"]))
        elif sk == "custom":
            src = S.SrcCRS.parse(opt["custom"])
        if not frame.can_transform(src):
            msg = (f"The file is in {src.describe()}, but the project uses {frame.describe()} - the shapes cannot "
                   "be transformed. Import them as project coordinates (Coordinate system: Project coordinate "
                   "system) if the numbers are already in the project system.")
            if not opt.get("_headless"):
                QMessageBox.warning(self, "Import Shapes", msg)
            self.log("Import Shapes: " + msg)
            return None
        work = copy.deepcopy(sset)            # transform a copy (retry with other settings)
        S.transform_set(work, frame, src, add_offset)
        # far away from the project?  (forgotten offset / another coordinate system)
        far, msg, hint = S.far_check(S.bounds(work), self._project_bounds_world(ch), frame.offset)
        if far:
            act = far_action or self._ask_far(msg, hint)
            self.log("Import Shapes: " + msg + f" -> {act}")
            if act == "cancel":
                return None
            if act == "offset":
                work = copy.deepcopy(sset); S.transform_set(work, frame, src, True)
            elif act == "wgs84":
                if not frame.can_transform(S.SrcCRS("wgs84")):
                    self.log("Import Shapes: WGS 84 cannot be transformed into a local project"); return None
                work = copy.deepcopy(sset); S.transform_set(work, frame, S.SrcCRS("wgs84"))
        # heights
        zm = opt["z_mode"]
        smp = None
        if zm in ("drape_missing", "drape_all"):
            smp = self._sampler(opt.get("surface"), ch)
            if smp is None:
                self.log("Import Shapes: no DEM / model to attach the shapes to - keeping Z from the file")
                zm = "keep"
        mode = {"drape_missing": "drape", "drape_all": "drape", "keep": "keep", "fixed": "fixed"}[zm]
        n = S.apply_heights(work, mode, smp, float(opt.get("fixed_z") or 0.0), only_without_z=zm != "drape_all",
                            log=self.log)
        name = opt.get("layer") or ""
        if name and len(work["layers"]) == 1:
            work["layers"][0]["name"] = name
        elif name:
            for L in work["layers"]:
                L["name"] = f"{name} / {L['name']}"
        for L in work["layers"]:
            L["crs"] = src.describe() if src.kind != "project" else frame.describe()
            L["imported"] = time.strftime("%Y-%m-%d %H:%M:%S")
            L["z_mode"] = zm + (f" ({smp.name})" if smp is not None and mode == "drape" else "")
        base = S.new_set() if (opt.get("replace") or cur is None) else cur
        first_new = len(base["shapes"])
        S.merge(base, work)
        self._store_shapes(ch, base)
        what = ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in counts.items())
        self.log(f"Imported {len(work['shapes'])} shapes ({what}) from {path} - "
                 f"{src.describe()} -> {frame.describe()}"
                 + (" (+ offset)" if add_offset or (far and far_action == 'offset') else "")
                 + (f"; {n} attached to {smp.name}" if mode == "drape" and smp is not None else "")
                 + (f"; Z = {opt['fixed_z']:g}" if mode == "fixed" else ""))
        for t in notes:
            self.log("  " + t)
        self.set_modified()
        self.set_layer_visible("shapes", True)
        self.update_shape_views()
        self._start_occlusion_scene()
        return {"shapes": len(work["shapes"]), "first": first_new, "far": far, "hint": hint, "notes": notes,
                "src": src.describe()}

    def _ask_far(self, msg, hint):
        box = QMessageBox(QMessageBox.Warning, "Import Shapes", msg + "\n\nWhat do you want to do?", parent=self)
        b_off = box.addButton("Add Project Offset", QMessageBox.AcceptRole) if hint == "offset" else None
        b_geo = box.addButton("Treat as WGS 84 (lon/lat)", QMessageBox.AcceptRole) if hint == "degrees" else None
        b_any = box.addButton("Import Anyway", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        c = box.clickedButton()
        return "offset" if c is b_off and c else "wgs84" if c is b_geo and c else "anyway" if c is b_any else "cancel"

    # ------------------------------------------------------------ export / remove
    def export_shapes(self, path=None, options=None):
        """File > Export > Export Shapes... - SHP / DXF / GeoJSON in world coordinates."""
        ch = self.chunk
        sset = self._shape_set(ch)
        if not sset or not sset["shapes"]:
            if options is None:
                QMessageBox.information(self, "Export Shapes", "No shapes - use File > Import > Import Shapes...")
            return None
        frame = self._frame(ch)
        systems = [("Project coordinate system (" + frame.describe() + ")", S.SrcCRS("project"))]
        if frame.kind == "utm":
            systems.append(("WGS 84 (EPSG::4326)", S.SrcCRS("wgs84")))
        counts = {}
        for s in sset["shapes"]:
            counts[s["layer"]] = counts.get(s["layer"], 0) + 1
        layers = [(L["name"], counts.get(i, 0), L.get("visible", True)) for i, L in enumerate(sset["layers"])]
        if options is None:
            from .dialogs import ExportShapesDialog
            dlg = ExportShapesDialog(self, layers, [n for n, _c in systems])
            if dlg.exec() != QDialog.Accepted:
                return None
            options = dlg.settings()
        fmt = (options.get("fmt") or "shp").lower()
        if path is None:
            ext = {"shp": "Shapefile (*.shp)", "dxf": "DXF (*.dxf)", "geojson": "GeoJSON (*.geojson)"}[fmt]
            path, _ = QFileDialog.getSaveFileName(self, "Export Shapes", f"shapes.{fmt}", ext)
            if not path:
                return None
        if not os.path.splitext(path)[1]:
            path += "." + fmt
        dst = systems[min(int(options.get("crs_index", 0)), len(systems) - 1)][1]
        names = options.get("layers")
        li = None if names is None else {i for i, L in enumerate(sset["layers"]) if L["name"] in names}
        files = S.export(sset, path, fmt, frame, dst, li, log=self.log)
        self.statusBar().showMessage(f"Exported shapes: {', '.join(os.path.basename(f) for f in files)}", 6000)
        return files

    def remove_shapes(self, confirm=True):
        """Tools > Shapes > Remove Shapes (the shapes leave the project; the
        imported source files are not touched)."""
        ch = self.chunk
        if not ch.shapes:
            self.statusBar().showMessage("No shapes", 3000); return False
        if confirm and QMessageBox.question(self, "Remove Shapes",
                                            f"Remove all {ch.shapes.get('count', 0)} shapes from the chunk?") \
                != QMessageBox.Yes:
            return False
        self.log(f"Removed {ch.shapes.get('count', 0)} shapes")
        ch.shapes = {}
        self._shape_cache.pop(id(ch), None); self._shapes_ver += 1; self._photo_shape_cache.clear()
        self.selected_shape = None
        self.set_modified(); self.update_shape_views()
        return True

    def remove_shape_layer(self, name):
        ch = self.chunk
        sset = self._shape_set(ch)
        if not sset:
            return
        li = next((i for i, L in enumerate(sset["layers"]) if L["name"] == name), None)
        if li is None:
            return
        from . import shape_style as SS
        before = copy.deepcopy(sset)
        S.remove_layer(sset, li)
        if not sset["layers"]:
            self.remove_shapes(confirm=False); return
        ch.shapes = SS.remap_after_layer_removal(ch.shapes, before, li)
        self._store_shapes(ch, sset)
        self.log(f"Removed shape layer {name}")
        self.selected_shape = None
        self.set_modified(); self.update_shape_views()

    def set_shape_layer_visible(self, name, on):
        ch = self.chunk
        hid = set((ch.shapes or {}).get("hidden") or [])
        (hid.discard if on else hid.add)(name)
        ch.shapes = dict(ch.shapes or {}, hidden=sorted(hid))
        self.project.modified = True; self._update_title()
        self._shapes_ver += 1
        self.update_shape_views()

    # ------------------------------------------------------------ measurement / properties
    def shape_metrics(self, index, ch=None):
        ch = ch or self.chunk
        sset = self._shape_set(ch)
        if not sset or not (0 <= index < len(sset["shapes"])):
            return None
        key = (self._shapes_ver, id(ch), index)
        c = getattr(self, "_metric_cache", {})
        if key not in c:
            try:
                smp = self._sampler(None, ch)
            except Exception as e:  # noqa: BLE001
                self.log(f"Shapes: surface for measurements: {e}"); smp = None
            c = {k: v for k, v in c.items() if k[0] == self._shapes_ver}
            c[key] = S.measure(sset["shapes"][index], smp)
            self._metric_cache = c
        return self._metric_cache[key]

    def _shape_rows(self, ch, index):
        sset = self._shape_set(ch)
        s = sset["shapes"][index]
        L = sset["layers"][s["layer"]]
        rows = [("Shape", S.shape_name(s, index)), ("Layer", L["name"]), ("Source", L.get("source", "")),
                ("Coordinate system", self._frame(ch).describe()),
                ("Attached to surface", "Yes" if s.get("attached") else "No"),
                ("Z in file", "Yes" if s.get("has_z") else "No")]
        for k, v in (self.shape_metrics(index, ch) or {}).items():
            if isinstance(v, float):
                u = " m\u00b2" if k.startswith("Area") else " m\u00b3" if k.startswith("Volume") else " m"
                v = f"{v:,.3f}{u}"
            rows.append((k, v))
        for k, v in (s.get("attrs") or {}).items():
            rows.append((f"[{k}]", v))
        return rows

    def measure_shape(self, index=None):
        """Tools > Shapes > Measure Shape... - metrics of the selected shape."""
        if index is None:
            index = self.selected_shape
        sset = self._shape_set()
        if index is None or not sset or not (0 <= index < len(sset["shapes"])):
            self.statusBar().showMessage("Select a shape (Workspace or Ortho view) first", 4000); return None
        rows = self._shape_rows(self.chunk, index)
        txt = "\n".join(f"{k}: {v}" for k, v in rows)
        self.log("Measure Shape:\n" + txt)
        if not getattr(self, "_headless", False):
            QMessageBox.information(self, "Measure Shape", txt)
        return dict(rows)

    def _shape_properties(self, info, ch):
        kind = info["kind"]
        sset = self._shape_set(ch)
        if not sset:
            return [("Shapes", "none")]
        if kind == "shape":
            self.select_shape(info["index"], from_view=False)
            return self._shape_rows(ch, info["index"])
        if kind == "shape_layer":
            li = next((i for i, L in enumerate(sset["layers"]) if L["name"] == info["index"]), None)
            if li is None:
                return []
            L = sset["layers"][li]
            shp = [s for s in sset["shapes"] if s["layer"] == li]
            cnt = {}
            for s in shp:
                cnt[s["type"]] = cnt.get(s["type"], 0) + 1
            return [("Layer", L["name"]), ("Shapes", ", ".join(f"{v} {k}" for k, v in cnt.items())),
                    ("Source", L.get("source", "")), ("Format", L.get("format", "")),
                    ("File coordinate system", L.get("crs", "")), ("Heights", L.get("z_mode", "")),
                    ("Imported", L.get("imported", "")), ("Visible", "Yes" if L.get("visible", True) else "No"),
                    ("Vertices", f"{sum(len(p) for s in shp for p in s['parts']):,}")]
        b = S.bounds(sset)
        rows = [("Shapes", f"{len(sset['shapes']):,}"), ("Layers", len(sset["layers"])),
                ("Data file", (ch.shapes or {}).get("path", "")),
                ("Sources", "; ".join((ch.shapes or {}).get("sources") or []))]
        if b is not None:
            rows += [("X range", f"{b[0][0]:,.3f} .. {b[1][0]:,.3f}"), ("Y range", f"{b[0][1]:,.3f} .. {b[1][1]:,.3f}"),
                     ("Z range", f"{b[0][2]:,.3f} .. {b[1][2]:,.3f}")]
        return rows

    def select_shape(self, index, from_view=True):
        self.selected_shape = index
        self.ortho_view.select_shape(index)
        self._photo_shape_cache.clear()
        self.update_shape_views(photo_only=True)
        self.update_shape_views(model_only=True)

    def _shape_clicked(self, index):
        if index < 0:
            self.selected_shape = None; self.update_shape_views(photo_only=True); return
        self.selected_shape = index
        rows = self._shape_rows(self.chunk, index)
        self.properties.show_props({k: ("" if v is None else v) for k, v in rows})
        self.statusBar().showMessage(f"Shape: {rows[0][1]} ({rows[1][1]})", 4000)

    # ------------------------------------------------------------ views
    def _visible_shapes(self, ch=None):
        sset = self._shape_set(ch)
        if not sset:
            return None, []
        vis = [i for i, s in enumerate(sset["shapes"]) if sset["layers"][s["layer"]].get("visible", True)]
        return sset, vis

    @staticmethod
    def _shape_color(sset, s):
        return s.get("color") or sset["layers"][s["layer"]]["color"]

    def shape_style(self, index, ch=None, highlight=None):
        """Resolved display style of shape `index` (shape_style.resolve)."""
        from . import shape_style as SS
        ch = ch or self.chunk
        sset = self._shape_set(ch)
        hl = (index == self.selected_shape) if highlight is None else highlight
        return SS.resolve(sset, index, ch.shapes, highlight=hl)

    def _shape_label(self, s, i, st):
        from . import shape_style as SS
        return SS.label_text(s, i, st["label_field"]) if st["label"] else ""

    @staticmethod
    def _anchor(s, parts):
        """Label position of a polyline (middle vertex) / polygon (ring centroid)."""
        P = np.asarray(parts[0], float)
        if s["type"] == "polygon" and len(P) >= 3:
            return P[:-1].mean(0) if np.allclose(P[0], P[-1]) else P.mean(0)
        return P[len(P) // 2]

    def update_shape_views(self, photo_only=False, model_only=False):
        """Push the (visible) shapes into the Model view, the Ortho view and the
        open photo, each drawn with its style (layer style < per-shape override)."""
        ch = self.chunk
        sset, vis = self._visible_shapes(ch)
        frame = self._frame(ch)
        styles = {i: self.shape_style(i, ch) for i in vis} if sset else {}
        if not photo_only:
            groups = {}
            for i in vis:
                s = sset["shapes"][i]
                st = styles[i]
                col = tuple(st["line_color"])
                key = (col, st["line_width"], st["line_style"], st["on_top"], st["point_symbol"], st["point_size"],
                       tuple(st["fill_color"]), st["fill_opacity"], st["label_size"], tuple(st["label_color"]),
                       st["label_bg"], tuple(st["label_bg_color"]))
                g = groups.setdefault(key, {"color": col, "width": st["line_width"],
                                            "dashed": st["line_style"] == "dashed", "on_top": st["on_top"],
                                            "style": st, "lines": [], "points": [], "labels": [],
                                            "fills": [], "fill_color": tuple(st["fill_color"]),
                                            "fill_opacity": st["fill_opacity"], "anchors": [], "anchor_labels": []})
                parts = [frame.to_local(p) for p in S.display_parts(s)]
                if not parts:
                    continue
                lab = self._shape_label(s, i, st)
                if s["type"] == "point":
                    g["points"] += [p[0] for p in parts]
                    g["labels"] += [lab] * len(parts)
                else:
                    g["lines"] += [S._close(p) if s["type"] == "polygon" else p for p in parts]
                    if s["type"] == "polygon":
                        g["fills"].append([S._close(p) for p in parts])
                    if lab:
                        g["anchors"].append(self._anchor(s, parts)); g["anchor_labels"].append(lab)
            gl = []
            for g in groups.values():
                g["points"] = np.array(g["points"]) if g["points"] else None
                g["anchors"] = np.array(g["anchors"]) if g["anchors"] else None
                gl.append(g)
            self.model_view.set_shapes(gl)
            if not model_only:
                items = []
                if sset:
                    for i in vis:
                        s = sset["shapes"][i]
                        st = self.shape_style(i, ch, highlight=False)     # the Ortho view highlights itself
                        lab = self._shape_label(s, i, st)
                        items.append({"index": i, "type": s["type"], "color": tuple(st["line_color"]),
                                      "width": st["line_width"],
                                      "label": lab, "dashed": st["line_style"] == "dashed",
                                      "fill_color": tuple(st["fill_color"]), "fill_opacity": st["fill_opacity"],
                                      "symbol": st["point_symbol"], "point_size": st["point_size"],
                                      "label_style": self._label_style(st),
                                      "anchor": (self._anchor(s, s["parts"]) if lab and s["type"] != "point"
                                                 and s["parts"] else None),
                                      "parts": s["parts"], "holes": s.get("holes")})
                self.ortho_view.set_shapes(items, self.selected_shape)
                self.ortho_view.set_visible("shapes", self.layer_action("shapes").isChecked())
        if model_only:
            return
        try:
            self._update_photo_shapes()
        except Exception as e:  # noqa: BLE001
            self.log(f"Shapes overlay on the photo: {e}")

    @staticmethod
    def _label_style(st):
        return {"size": st["label_size"], "color": tuple(st["label_color"]), "bg": st["label_bg"],
                "bg_color": tuple(st["label_bg_color"])}

    def _photo_prep(self, ch, sset, vis):
        """Per shapes version (not per photo): local-frame parts, resolved styles,
        labels, label anchors and the concatenated polylines for
        S.project_batch - so a photo only does vectorised projection work."""
        key = (self._shapes_ver, id(ch), tuple(vis), self.selected_shape)
        c = getattr(self, "_photo_prep_cache", None)
        if c is not None and c[0] == key:
            return c[1]
        old = c[1] if c is not None and c[0][:3] == key[:3] else None
        frame = self._frame(ch)
        if old is not None:                  # only the selection changed: reuse geometry
            prep = dict(old)
            prep["styles"] = dict(old["styles"])
            for i in {old["selected"], self.selected_shape} - {None}:
                if i in prep["styles"]:
                    prep["styles"][i] = self.shape_style(i, ch)
            prep["selected"] = self.selected_shape
            self._photo_prep_cache = (key, prep)
            return prep
        from . import shape_style as SS
        shapes = sset["shapes"]
        line_idx, line_parts, line_closed = [], [], []
        pt_idx, pt_P = [], []
        styles, labels, anchors, parts_of = {}, {}, {}, {}
        for i in vis:
            sh = shapes[i]
            parts = [frame.to_local(p) for p in S.display_parts(sh)]
            if not parts:
                continue
            st = SS.resolve(sset, i, ch.shapes, highlight=(i == self.selected_shape))
            styles[i] = st
            lab = self._shape_label(sh, i, st)
            labels[i] = lab
            if sh["type"] == "point":
                P = np.vstack(parts)
                pt_idx += [i] * len(P); pt_P.append(P)
            else:
                line_idx.append(i); line_parts.append(parts); line_closed.append(sh["type"] == "polygon")
                parts_of[i] = parts
                if lab:
                    anchors[i] = self._anchor(sh, parts)
        lines = S.prepare_polylines(line_parts, line_closed)
        lines["shape"] = np.asarray(line_idx, np.int64)
        prep = {"styles": styles, "labels": labels, "lines": lines, "parts": parts_of,
                "pt_idx": np.asarray(pt_idx, np.int64), "pt_P": np.vstack(pt_P) if pt_P else np.zeros((0, 3)),
                "an_idx": np.asarray(list(anchors), np.int64),
                "an_P": np.array(list(anchors.values())) if anchors else np.zeros((0, 3)),
                "type": {i: shapes[i]["type"] for i in styles}, "selected": self.selected_shape}
        self._photo_prep_cache = (key, prep)
        return prep

    def photo_shape_items(self, k, ch=None, occlusion=True):
        """Shapes projected into photo k (pixel polylines / points, the parts the
        surface hides separately, polygon fills), cached per (photo, shapes
        version, selection, occlusion). occlusion=False skips the mesh test
        (fast first draw; the occluded version follows from a background thread).

        All shapes are projected together (S.project_batch): one transform of
        all vertices, frame culling of whole segments, one ray cast for the
        occlusion - instead of a Python loop with a ray cast per shape."""
        ch = ch or self.chunk
        sset, vis = self._visible_shapes(ch)
        if not sset or not vis:
            return []
        from . import georef as g
        model = self._sparse_model()
        if model is None:
            return []
        cam_ = ch.cameras[k]
        names = getattr(self, "_names_cache", (None, {}))
        if names[0] is not model:
            names = self._names_cache = (model, g.name_index(model))
        iid = names[1].get(g._stem(cam_.label))
        if iid is None:
            return []
        scene = self._occl["scene"] if occlusion and self._occl["key"] == self._occl_key(ch) else None
        key = (self._shapes_ver, id(ch), k, self.selected_shape, scene is not None, tuple(vis[:3]), len(vis))
        if key in self._photo_shape_cache:
            return self._photo_shape_cache[key]
        prep = self._photo_prep(ch, sset, vis)
        im = model.images[iid]; cam = model.cameras[im.camera_id]
        boxes = getattr(self, "_box_cache", {})
        bk = (id(model), im.camera_id)
        if bk not in boxes:
            boxes[bk] = S.frame_box(cam); self._box_cache = boxes
        box = boxes[bk]
        R, t = im.R(), np.asarray(im.tvec, float)
        sx = sy = 1.0
        if cam_.width and cam_.height and cam.width and (cam_.width, cam_.height) != (cam.width, cam.height):
            sx, sy = cam_.width / cam.width, cam_.height / cam.height     # photo resampled for COLMAP
        sc_ = np.array([sx, sy])
        L = prep["lines"]
        vis_runs, hid_runs = S.project_batch(L, cam, R, t, box, scene=scene, hidden=True)
        res = {}

        def item(i):
            it = res.get(i)
            if it is None:
                st = prep["styles"][i]
                sel = i == self.selected_shape
                it = res[i] = {"index": i, "color": tuple(st["line_color"]),
                               "width": st["line_width"] - (2.0 if sel else 0.0),
                               "selected": sel, "dashed": st["line_style"] == "dashed", "symbol": st["point_symbol"],
                               "point_size": st["point_size"], "label_style": self._label_style(st),
                               "fill_color": tuple(st["fill_color"]), "fill_opacity": st["fill_opacity"],
                               "lines": [], "points": [], "hidden_lines": [], "hidden_points": [], "fills": [],
                               "anchors": []}
            return it
        shape_of = L["shape"]
        for j, P in vis_runs:
            item(int(shape_of[j]))["lines"].append(P * sc_)
        for j, P in hid_runs:
            item(int(shape_of[j]))["hidden_lines"].append(P * sc_)
        if len(prep["pt_P"]):
            uv, ok, uvh, okh = S.project_points(prep["pt_P"], cam, R, t, box, scene=scene, hidden=True)
            pidx = prep["pt_idx"]
            for (u, v), n in zip(uv, ok):
                i = int(pidx[n]); item(i)["points"].append((u * sx, v * sy, prep["labels"].get(i, "")))
            for (u, v), n in zip(uvh, okh):
                item(int(pidx[n]))["hidden_points"].append((u * sx, v * sy))
        # labels at the anchor of drawn polylines / polygons, fills of drawn polygons
        if len(prep["an_P"]) and res:
            a_uv, a_ok = S.project_points(prep["an_P"], cam, R, t, box)
            for (u, v), n in zip(a_uv, a_ok):
                i = int(prep["an_idx"][n])
                if i in res:
                    res[i]["anchors"] = [(u * sx, v * sy, prep["labels"][i])]
        for i, it in res.items():
            st = prep["styles"][i]
            if prep["type"][i] == "polygon" and st["fill_opacity"] > 0 and (it["lines"] or it["hidden_lines"]):
                for p_ in prep["parts"][i]:
                    F = S.project_ring_fill(p_, cam, R, t, box)
                    if F is not None:
                        it["fills"].append(F * sc_)
        out = [res[i] for i in sorted(res) if res[i]["lines"] or res[i]["points"] or res[i]["hidden_lines"]
               or res[i]["hidden_points"]]
        if len(self._photo_shape_cache) > 64:
            self._photo_shape_cache.clear()
        self._photo_shape_cache[key] = out
        return out

    # ------------------------------------------------------------ styles
    def shape_layer_names(self, ch=None):
        sset = self._shape_set(ch)
        return [L["name"] for L in sset["layers"]] if sset else []

    def layer_style(self, name, ch=None):
        from . import shape_style as SS
        return SS.layer_style((ch or self.chunk).shapes, name)

    def set_layer_style(self, name, style, commit=True):
        """Store / apply a layer style (None = defaults); commit marks the
        project modified (the style is saved with the project)."""
        from . import shape_style as SS
        ch = self.chunk
        if not ch.shapes:
            return
        ch.shapes = SS.set_layer_style(ch.shapes, name, style)
        self._styles_changed(commit)

    def set_shape_style(self, index, style, commit=True):
        """Per-shape override (None removes it -> layer style)."""
        from . import shape_style as SS
        ch = self.chunk
        sset = self._shape_set(ch)
        if not sset or not (0 <= index < len(sset["shapes"])):
            return
        base = self.layer_style(sset["layers"][sset["shapes"][index]["layer"]]["name"])
        ch.shapes = SS.set_shape_style(ch.shapes, index, style, base)
        self._styles_changed(commit)

    def _styles_changed(self, commit):
        self._shapes_ver += 1
        self._photo_shape_cache.clear()
        if commit:
            self.set_modified()
        self.update_shape_views()

    def open_shape_style(self, layer=None, shape=None, modal=True):
        """Tools > Shapes > Shape Settings... / Workspace layer > Layer Style... /
        double-click on a layer: the style dialog (live preview, Apply / OK /
        Cancel, Reset Defaults). Returns the dialog (modal=False: shown, for scripts)."""
        names = self.shape_layer_names()
        if not names:
            self.statusBar().showMessage("No shapes - use File > Import > Import Shapes...", 4000); return None
        from .shape_style_ui import ShapeStyleDialog
        if shape is None and self.selected_shape is not None and layer is None:
            shape = self.selected_shape
        if layer is None:
            sset = self._shape_set()
            layer = (sset["layers"][sset["shapes"][shape]["layer"]]["name"]
                     if shape is not None and 0 <= shape < len(sset["shapes"]) else names[0])
        dlg = ShapeStyleDialog(self, layer, shape)
        if modal:
            dlg.exec()
        else:
            dlg.show()
        return dlg

    # ------------------------------------------------------------ transparent view
    def set_transparency(self, on=None, opacity=None, save=True):
        """Transparent view (toolbar button next to Show Shapes, Ctrl+Shift+T):
        the model / textured / tiled model / point clouds are drawn with the
        opacity (default 40%), the photo and the orthomosaic are dimmed, and
        shapes (and their parts hidden by the surface, dashed) stay visible."""
        from PySide6.QtCore import QSettings
        a = getattr(self, "transparency_action", None)
        if on is None:
            on = a.isChecked() if a is not None else not self.model_view.transparent
        if opacity is None:
            opacity = self.model_view.surface_opacity
        opacity = float(min(1.0, max(0.05, opacity)))
        if a is not None and a.isChecked() != bool(on):
            a.blockSignals(True); a.setChecked(bool(on)); a.blockSignals(False)
        sl = getattr(self, "transparency_slider", None)
        if sl is not None and sl.value() != int(round(opacity * 100)):
            sl.blockSignals(True); sl.setValue(int(round(opacity * 100))); sl.blockSignals(False)
        lab = getattr(self, "transparency_value", None)
        if lab is not None:
            lab.setText(f"{int(round(opacity * 100))}%")
        self.model_view.set_transparency(on, opacity)
        self.photo_view.set_transparency(on, opacity)
        self.ortho_view.set_transparency(on, opacity)
        if save:
            st = QSettings(); st.setValue("view/transparent", bool(on)); st.setValue("view/transparent_opacity", opacity)
        return bool(on)

    def _update_photo_shapes(self):
        """Draw the shapes on the open photo. Fast first draw without the mesh
        occlusion test unless it is cached; the occluded version is computed
        in a background thread (ray casts) and replaces it when ready."""
        k = getattr(self, "current_photo", -1)
        if self.photo_view.path is None or not (0 <= k < len(self.chunk.cameras)):
            return
        if not self.layer_action("shapes").isChecked() or not self.chunk.shapes:
            self.photo_view.set_shapes([]); return
        t0 = time.perf_counter()
        ch = self.chunk
        scene_ready = self._occl["scene"] is not None and self._occl["key"] == self._occl_key(ch)
        sync = not scene_ready or getattr(self, "_headless_occlusion", False)
        cached = None
        if scene_ready and not sync:
            sset, vis = self._visible_shapes(ch)
            ck = (self._shapes_ver, id(ch), k, self.selected_shape, True, tuple(vis[:3]), len(vis))
            cached = self._photo_shape_cache.get(ck)
        items = cached if cached is not None else self.photo_shape_items(k, occlusion=sync)
        self.photo_view.set_shapes(items)
        dt = time.perf_counter() - t0
        self._photo_shapes_timing = dt
        if dt > 0.3:
            self.log(f"Shapes on {self.chunk.cameras[k].label}: {dt:.2f} s")
        if scene_ready and cached is None and not sync:
            self._start_photo_occlusion(k)

    def _start_photo_occlusion(self, k):
        """Occluded shapes for photo k in a background thread (one batched ray
        cast); the result is drawn if photo / shapes / selection are unchanged."""
        ch = self.chunk
        tag = (k, self._shapes_ver, self.selected_shape, id(ch))
        self._photo_occl_tag = tag
        if getattr(self, "_photo_occl_busy", None) == tag:
            return
        self._photo_occl_busy = tag
        self.busy_indicator.background("photo-occlusion", "Checking photo visibility")

        def work():
            try:
                res = self.photo_shape_items(k, ch, occlusion=True)
            except Exception as e:  # noqa: BLE001
                res = e
            try:
                self._shape_relay.photoOcclusionReady.emit(tag, res)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="photo-shape-occlusion", daemon=True).start()

    def _photo_occlusion_ready(self, tag, res):
        if getattr(self, "_photo_occl_busy", None) == tag:
            self._photo_occl_busy = None
            self.busy_indicator.background("photo-occlusion")
        if isinstance(res, Exception):
            self.log(f"Shapes overlay on the photo (occlusion): {res}"); return
        cur = (getattr(self, "current_photo", -1), self._shapes_ver, self.selected_shape, id(self.chunk))
        if tag != cur:                       # another photo / shapes / selection meanwhile
            return
        if self.photo_view.path is None or not self.layer_action("shapes").isChecked():
            return
        self.photo_view.set_shapes(res)

    def wait_photo_occlusion(self, timeout=30.0):
        """Scripts / tests: wait for the background occlusion pass of the photo."""
        from PySide6.QtWidgets import QApplication
        t0 = time.time()
        while getattr(self, "_photo_occl_busy", None) is not None and time.time() - t0 < timeout:
            QApplication.processEvents(); time.sleep(0.01)
        QApplication.processEvents()
        return getattr(self, "_photo_occl_busy", None) is None

    # mesh occlusion for the photo overlay (Open3D scene, built in the background)
    def _occl_key(self, ch):
        p = ch.model_ply
        return (p, os.path.getmtime(p)) if p and os.path.exists(p) else None

    def _start_occlusion_scene(self):
        ch = self.chunk
        key = self._occl_key(ch)
        if key is None or not ch.shapes or self._occl["key"] == key or self._occl["busy"]:
            return
        self._occl["busy"] = True
        self.busy_indicator.background("model-occlusion", "Preparing model visibility")
        gen = self._shapes_ver

        def work():
            try:
                import open3d as o3d
                from .texture import read_mesh
                V, F = read_mesh(key[0])
                sc = o3d.t.geometry.RaycastingScene()
                sc.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
                res = (key, sc)
            except Exception as e:  # noqa: BLE001
                res = (key, e)
            try:
                self._shape_relay.occlusionReady.emit(gen, res)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="shape-occlusion", daemon=True).start()

    def _occlusion_ready(self, _gen, res):
        self._occl["busy"] = False
        self.busy_indicator.background("model-occlusion")
        key, sc = res
        if isinstance(sc, Exception):
            self.log(f"Shapes: no occlusion test on photos ({sc})"); return
        self._occl.update(key=key, scene=sc)
        self._photo_shape_cache.clear()
        try:
            self._update_photo_shapes()
        except Exception as e:  # noqa: BLE001
            self.log(f"Shapes overlay on the photo: {e}")

    def wait_occlusion(self, timeout=120.0):
        from PySide6.QtWidgets import QApplication
        t0 = time.time()
        while self._occl["busy"] and time.time() - t0 < timeout:
            QApplication.processEvents(); time.sleep(0.02)
        return self._occl["scene"] is not None
