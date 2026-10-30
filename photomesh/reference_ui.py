"""MainWindow mix-in: markers / ground control, Optimize Cameras, Refine
Markers, point cloud filtering and tie point gradual selection."""
from __future__ import annotations

import json
import os

import numpy as np
from PySide6.QtWidgets import QFileDialog, QMessageBox

from . import colmap as cm
from . import georef as g


GEOREF_STALE = "built before the last georeferencing (Optimize Cameras)"


def products_after_georef(ch, M, log=print):
    """Optimize Cameras moved the model, point cloud and mesh into a new frame by
    the similarity M (4x4). Lengths the texture measured in the old frame are
    scaled (else e.g. Build Orthomosaic defaults to a pixel size in the old
    units - issue #3), and rasters / tiles built in the old frame are marked
    outdated (they stay on disk until rebuilt). Returns the scale."""
    try:
        A = np.asarray(M, float)[:3, :3]
        s = float(abs(np.linalg.det(A)) ** (1.0 / 3.0))
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(s) or s <= 0:
        return 1.0
    t = ch.texture or {}
    if t and abs(s - 1.0) > 1e-9:
        for k in ("source_gsd", "texel_size"):
            if t.get(k):
                t[k] = float(t[k]) * s
        p = os.path.join(t.get("dir") or "", "info.json")
        if t.get("dir") and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    info = json.load(f)
                for k in ("source_gsd", "texel_size"):
                    if info.get(k):
                        info[k] = float(info[k]) * s
                with open(p + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(info, f, indent=1)
                os.replace(p + ".tmp", p)
            except (OSError, ValueError) as e:
                log(f"Optimize Cameras: could not update {p}: {e}")
    notes = []
    for name, d in (("orthomosaic", ch.orthomosaic), ("DEM", ch.dem), ("tiled model", ch.tiled_model)):
        if d and d.get("format") != "imported" and not d.get("stale"):
            d["stale"] = GEOREF_STALE; notes.append(name)
    if notes:
        log("Optimize Cameras: the " + ", ".join(notes) + " belonged to the previous frame - rebuild "
            + ("it" if len(notes) == 1 else "them"))
    return s


class ReferenceMixin:
    # ------------------------------------------------------------ helpers
    def _model_dir(self, ch=None):
        ch = ch or self.chunk
        wd = ch.workdir or (self.project.default_workdir(ch) if self.project.path else "")
        return os.path.join(wd, "sparse", "0") if wd else ""

    def _sparse_model(self):
        """COLMAP model of the active chunk (cached by file time)."""
        from . import colmap_io as cio
        d = self._model_dir()
        f = os.path.join(d, "images.bin")
        if not os.path.exists(f):
            return None
        key = (d, os.path.getmtime(f), os.path.getmtime(os.path.join(d, "points3D.bin")))
        if getattr(self, "_model_cache", (None,))[0] != key:
            self._model_cache = (key, cio.read_model(d))
        return self._model_cache[1]

    def _marker_objs(self):
        return [g.Marker(**m) for m in (self.chunk.markers or [])]

    def _snap(self):
        if hasattr(self, "_markers_snapshot"):
            self._markers_snapshot()              # Edit > Undo restores the previous markers

    def _invalidate_marker_error_rows(self, markers):
        """Old residuals must not be presented as measurements of edited placements."""
        before = {m["label"]: m for m in self.chunk.markers or []}
        changed = {m.label for m in markers if m.label not in before or
                   before[m.label].get("ref") != m.ref or
                   before[m.label].get("projections", {}) != m.projections}
        if changed and self.chunk.georef:
            self.chunk.georef["rows"] = [row for row in self.chunk.georef.get("rows", [])
                                        if row["label"] not in changed]
            self.chunk.georef["control_rmse"] = self.chunk.georef["check_rmse"] = None

    def _store_markers(self, ms):
        self._invalidate_marker_error_rows(ms)
        self.chunk.markers = [m.to_dict() for m in ms]
        self.set_modified()
        if hasattr(self, "_refresh_markers_pane"):
            self._refresh_markers_pane()
        else:
            self.markers_pane.refresh(self.chunk)
        self._update_view_markers()

    def _marker_model_xyz(self, m, model, names):
        """Marker position in the model frame: reference (if georeferenced),
        else triangulated from its projections."""
        # Display the surveyed feature's observed estimate, not its reference target.
        # Moving its photo/Ortho placement must not edit its survey XYZ.
        X, _n = g.triangulate_marker(model, m.projections, names)
        if X is not None:
            return X
        gr = self.chunk.georef or {}
        if gr.get("offset") is not None and m.ref is not None:
            try:
                crs = g.CRS.parse(self.chunk.crs)
                X = crs.to_metric(np.atleast_2d(np.asarray(m.ref, float)), gr.get("zone"))[0]
                return X - np.asarray(gr["offset"], float)
            except Exception:
                pass
        X, _n = g.triangulate_marker(model, m.projections, names)
        return X

    def _marker_positions(self):
        """Model-frame positions of the active chunk's markers (list of xyz).
        Safe to call from the view-loader thread. Results are cached per
        marker (projections/reference/model), so after an edit only that
        marker is re-triangulated instead of all of them (~2 s for 98)."""
        import json as _json
        try:
            model = self._sparse_model()
        except Exception:
            model = None
        if model is None:
            return None
        key0 = (self._model_cache[0], _json.dumps(self.chunk.georef.get("offset") if self.chunk.georef else None),
                self.chunk.crs)
        cache = getattr(self, "_marker_xyz_cache", None)
        if cache is None or cache[0] != key0:
            cache = self._marker_xyz_cache = (key0, {})
        names = None
        pts = []                       # (label, xyz, enabled=control)
        for m in self._marker_objs():
            k = (m.label, _json.dumps(m.ref), _json.dumps(m.projections, sort_keys=True))
            if k not in cache[1]:
                names = names or g.name_index(model)
                X = self._marker_model_xyz(m, model, names)
                cache[1][k] = None if X is None else np.asarray(X, float)
            if cache[1][k] is not None:
                pts.append((m.label, cache[1][k], bool(m.enabled)))
        return pts

    def _show_markers_3d(self, items):
        if not items:
            self.model_view.set_markers(None); return
        from .marker_style import marker_state
        self.model_view.marker_states = {m["label"]: marker_state(m) for m in self.chunk.markers}
        self.model_view.set_markers(np.array([x for _l, x, _e in items]), labels=[l for l, _x, _e in items],
                                    control=[e for _l, _x, e in items])

    def _update_view_markers(self):
        self._show_markers_3d(self._marker_positions())
        self._marker_selection_changed()
        self._update_ortho_markers()

    def _marker_selection_changed(self):
        self._update_photo_markers()
        labels = self.markers_pane.selected_labels()
        self.ortho_view.set_active_marker(labels[0] if len(labels) == 1 else None)

    def _update_photo_markers(self):
        k = getattr(self, "current_photo", -1)
        cams = self.chunk.cameras
        if not (0 <= k < len(cams)) or self.photo_view.path is None:
            return
        stem = g._stem(cams[k].label)
        items = []
        model = None
        try:
            model = self._sparse_model()
        except Exception:
            pass
        names = g.name_index(model) if model is not None else {}
        for m in self._marker_objs():
            p = {g._stem(a): v for a, v in m.projections.items()}.get(stem)
            if p is not None:
                kind = "predicted" if m.disabled else ("pinned" if len(p) < 3 or p[2] else "placed")
                items.append((m.label, p[0], p[1], kind))
            elif model is not None and stem in names:
                X = self._marker_model_xyz(m, model, names)
                if X is not None:
                    q = g.predict_projection(model, names[stem], X)
                    if q is not None:
                        items.append((m.label, q[0], q[1], "predicted"))
        for key in ("markers", "marker_labels"):          # keep in step with the Show Markers toggles
            self.photo_view.set_visible(key, self.layer_action(key).isChecked())
        self.photo_view.set_markers(items, self.markers_pane.current_label())

    def place_marker(self, u, v):
        label = self.markers_pane.current_label()
        k = self.current_photo
        if not label or not (0 <= k < len(self.chunk.cameras)):
            return
        ms = self._marker_objs()
        self._snap()
        for m in ms:
            if m.label == label:
                stem = g._stem(self.chunk.cameras[k].label)
                m.projections = {a: b for a, b in m.projections.items() if g._stem(a) != stem}
                m.projections[stem] = [float(u), float(v), True]
        self._store_markers(ms)
        self.log(f"Marker {label} placed on {self.chunk.cameras[k].label} at ({u:.1f}, {v:.1f})")

    def remove_marker_projection(self, label):
        k = self.current_photo
        stem = g._stem(self.chunk.cameras[k].label)
        ms = self._marker_objs()
        self._snap()
        for m in ms:
            if m.label == label:
                m.projections = {a: b for a, b in m.projections.items() if g._stem(a) != stem}
        self._store_markers(ms)

    def toggle_marker(self, label, enabled):
        ms = self._marker_objs()
        self._snap()
        for m in ms:
            if m.label == label:
                m.enabled = enabled
        self.chunk.markers = [m.to_dict() for m in ms]
        self.set_modified()
        self._update_view_markers()

    def add_marker(self):
        ms = self._marker_objs()
        n = len(ms) + 1
        labels = {m.label for m in ms}
        while f"point {n}" in labels:
            n += 1
        self._snap()
        ms.append(g.Marker(label=f"point {n}"))
        self._store_markers(ms)

    # ------------------------------------------------------------ import / export
    def import_markers(self, path=None):
        path = path or QFileDialog.getOpenFileName(
            self, "Import Markers / Reference", "",
            "Markers and reference (*.xml *.txt *.csv);;All files (*)")[0]
        if not path:
            return
        try:
            new, crs = g.import_markers(path)
        except Exception as e:
            QMessageBox.warning(self, "Import Markers", f"Cannot read {path}:\n{e}"); return
        ms = g.merge_markers(self._marker_objs(), new)
        if crs is not None and (crs.kind != "local" or not self.chunk.crs):
            self.chunk.crs = crs.name if crs.kind != "local" else 'LOCAL_CS["Local Coordinates (m)"]'
        self._store_markers(ms)
        nproj = sum(len(m.projections) for m in new)
        self.log(f"Imported {len(new)} markers ({nproj} photo projections) from {path}; "
                 f"coordinate system: {crs.name if crs else 'unknown'}")
        self.docks["Markers"].raise_()

    def export_markers(self):
        rows = (self.chunk.georef or {}).get("rows")
        if not self.chunk.markers:
            QMessageBox.information(self, "Export Markers", "No markers."); return
        dst = QFileDialog.getSaveFileName(self, "Export Markers", "markers.txt",
                                          "Reference report (*.txt);;Markers XML (*.xml)")[0]
        if not dst:
            return
        if dst.lower().endswith(".xml"):
            g.write_markers_xml(dst, self._marker_objs(), self.chunk.crs or "")
        else:
            crs = g.CRS.parse(self.chunk.crs)
            if rows:
                g.write_report(dst, rows, crs)
            else:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(f"# CoordinateSystem: {crs.name}\n#Label\tX\tY\tZ\tEnabled\tProjections\n")
                    for m in self._marker_objs():
                        r = m.ref or ["", "", ""]
                        f.write(f"{m.label}\t{r[0]}\t{r[1]}\t{r[2]}\t{int(m.enabled)}\t{len(m.projections)}\n")
        self.log(f"Exported markers to {dst}")

    # ------------------------------------------------------------ optimize
    def optimize_cameras(self):
        if not self._require_saved("Optimize Cameras"):
            return None
        from .dialogs import OptimizeCamerasDialog
        ch = self.chunk
        if not ch.aligned_count() or not os.path.isdir(self._model_dir()):
            QMessageBox.information(self, "Optimize Cameras", "Align photos first."); return
        dlg = OptimizeCamerasDialog(self, ch.reference_settings)
        if not dlg.exec():
            return
        s = dlg.settings()
        ch.reference_settings = dict(s["accuracies"], use_cameras=s["use_cameras"])
        wd = self.project.default_workdir(ch)
        cfg = {"model": self._model_dir(), "markers": ch.markers or [], "crs": ch.crs or "",
               "use_markers": s["use_markers"], "use_cameras": s["use_cameras"],
               "camera_gps": {c.label: [c.lon, c.lat, c.alt] for c in ch.cameras
                              if c.lat is not None and c.lon is not None},
               "accuracies": s["accuracies"], "bundle": s["bundle"], "tie_points": s["tie_points"],
               "fit_intrinsics": s["fit_intrinsics"], "offset": (ch.georef or {}).get("offset"),
               "report": os.path.join(wd, "georef_report.txt"),
               "result": os.path.join(wd, "georef.json"),
               "transform_files": [p for p in (ch.point_cloud_ply, ch.model_ply,
                                              os.path.join(wd, "dense", "fused.ply")) if p]}
        if not ch.georef and cfg["camera_gps"] and (ch.crs or "") == "" and s["use_cameras"]:
            cfg["crs"] = "EPSG:4326"
        status = g.reference_status(self._sparse_model(), self._marker_objs(),
                                    g.CRS.parse(cfg["crs"]), cfg["camera_gps"])
        ready = ((s["use_markers"] and status["usable"] >= 3) or
                 (s["use_cameras"] and status["gps"] >= 3))
        if not ready:
            msg = (f"{status['imported']} markers imported; {status['references']} enabled with complete XYZ; "
                   f"{status['usable']} usable controls on at least two aligned photos; "
                   f"{status['gps']} camera GPS positions usable in the selected CRS.\n\n"
                   "Georeferencing needs at least 3 non-collinear controls or camera GPS positions. "
                   "Imported XYZ alone is insufficient: place each control on at least two aligned photos.\n\n"
                   "Optimize cameras in the existing frame instead? This uses tie points and enabled marker "
                   "projections, ignores reference coordinates and GPS, and does not georeference the model.")
            if not s["bundle"]:
                QMessageBox.information(self, "Optimize Cameras", msg +
                                        "\nEnable Bundle adjustment for optimization without georeferencing.")
                return
            if QMessageBox.question(self, "Optimize without georeferencing?", msg,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            cfg["local_only"] = True
        path = os.path.join(wd, "georef_task.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, default=float)
        title = "Optimizing cameras (local bundle adjustment)" if cfg.get("local_only") else "Optimizing cameras (georeferencing)"
        steps = [(title, [cm.PYTHON, "georef", path], 85)] + \
            self._export_sparse_steps(wd)

        def loaded():
            res = json.load(open(cfg["result"], encoding="utf-8"))
            if res.get("similarity") is not None:
                products_after_georef(ch, res["similarity"], self.log)
            self._invalidate_camera_products(ch)
            if not res.get("local_only"):
                ch.georef = {k: res.get(k) for k in ("offset", "similarity", "crs", "crs_kind", "zone", "rows",
                                                     "control_rmse", "check_rmse")}
                for r in ch.georef.get("rows") or []:
                    r["pix"] = ((res.get("markers") or {}).get(r["label"]) or {}).get("pix")
                    r["n"] = ((res.get("markers") or {}).get(r["label"]) or {}).get("n")
            else:
                # Existing CRS/offset still describe the same frame, but old errors no longer apply.
                if ch.georef:
                    ch.georef.update(rows=[], control_rmse=None, check_rmse=None)
                for d in (ch.orthomosaic, ch.dem, ch.tiled_model):
                    if d and d.get("format") != "imported":
                        d["stale"] = "built before camera optimization"
                self.log("Cameras optimized without georeferencing. Rebuild dense products to use the new cameras.")
            ms = self._marker_objs()
            for m in ms:
                e = (res.get("markers") or {}).get(m.label, {})
                m.est = e.get("est")
            ch.markers = [m.to_dict() for m in ms]
            self._reload_alignment(ch, wd)
            c, k = res.get("control_rmse"), res.get("check_rmse")
            msg = ("Cameras optimized in the existing frame. " if res.get("local_only") else "Georeferenced. ") + (f"Control RMSE {c['total']:.3f} m (n={c['n']}). " if c else "") + \
                (f"Check RMSE {k['total']:.3f} m (n={k['n']})." if k else "")
            self.log(msg if res.get("local_only") else msg + f" Report: {cfg['report']}")
            self.statusBar().showMessage(msg, 15000)
        def meta(dt, stages):
            from . import procmeta
            res = json.load(open(cfg["result"], encoding="utf-8"))
            procmeta.record(ch, "camera_optimization" if cfg.get("local_only") else "georeference", params={k: cfg[k] for k in ("crs", "use_markers", "use_cameras",
                                                                             "accuracies", "bundle", "tie_points",
                                                                             "fit_intrinsics")},
                            seconds=dt, stages=stages, control_rmse=res.get("control_rmse"),
                            check_rmse=res.get("check_rmse"), bundle=res.get("bundle"),
                            transformed=[] if cfg.get("local_only") else cfg["transform_files"])
            ch.meta.pop("sparse_stats", None)
        self._run("Optimize Cameras", steps, loaded, meta)

    def _invalidate_camera_products(self, ch):
        """Camera refinement invalidates camera-derived products, not imported rasters."""
        reason = "Cameras changed - rebuild this product"
        for key in ("point_cloud", "model", "texture", "depth_maps"):
            if key in ch.meta:
                ch.meta[key]["stale"] = reason
        if ch.model_ply:
            ch.meta.setdefault("model", {})["stale"] = reason
        for d in (ch.texture, ch.tiled_model, ch.orthomosaic, ch.dem):
            if d and d.get("format") != "imported":
                d["stale"] = reason
        if hasattr(self, "_update_stale_banners"):
            self._update_stale_banners()

    def _export_sparse_steps(self, wd):
        sp = os.path.join(wd, "sparse", "0")
        return [("Exporting tie points", ["model_converter", "--input_path", sp, "--output_path",
                                          os.path.join(wd, "tie_points.ply"), "--output_type", "PLY"], 8),
                ("Exporting cameras", ["model_converter", "--input_path", sp, "--output_path",
                                       os.path.join(wd, "sparse_txt"), "--output_type", "TXT"], 7)]

    def _reload_alignment(self, ch, wd):
        poses = cm.read_images_txt(os.path.join(wd, "sparse_txt", "images.txt"))
        from .photonames import camera_for_names, recover_names
        if any(c.aligned and not c.colmap_name for c in ch.cameras):
            recover_names(ch, wd)
        cams, ambiguous = camera_for_names(ch, list(poses))      # exact names, not file names
        if ambiguous:
            self._warn_ambiguous(ambiguous)
        by_cam = {id(c): poses[n] for n, c in cams.items()}
        for c in ch.cameras:
            p = by_cam.get(id(c))
            c.aligned = p is not None
            c.est = p[0].tolist() if p else None
            c.rot = p[1].tolist() if p else None
        ch.tie_points_ply = os.path.join(wd, "tie_points.ply")
        ch.tie_point_count = cm.count_points3d(os.path.join(wd, "sparse_txt", "points3D.txt"))
        self.model_view.set_z_up(bool(ch.georef))
        self.set_modified(); self.reload_view()
        self.markers_pane.refresh(ch)
        self._update_view_markers()

    # ------------------------------------------------------------ refine
    def refine_markers(self):
        if not self._require_saved("Refine Markers"):
            return None
        ch = self.chunk
        if not ch.markers or not os.path.isdir(self._model_dir()):
            QMessageBox.information(self, "Refine Markers", "Align photos and import markers first."); return
        wd = self.project.default_workdir(ch)
        cfg = {"model": self._model_dir(), "image_root": cm.image_dir(wd), "markers": [m for m in ch.markers if not m.get("disabled", False) and not m.get("locked", False)],
               "offset": (ch.georef or {}).get("offset"), "use_reference": bool(ch.georef),
               "search": 25, "output": os.path.join(wd, "refined_markers.json"),
               # model-frame positions computed here (CRS-aware: geographic references are
               # converted to metres before the offset is removed, which the task alone did not do)
               "positions": {l: np.asarray(x, float).tolist() for l, x, _e in (self._marker_positions() or [])}}
        path = os.path.join(wd, "refine_task.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, default=float)

        def loaded():
            res = json.load(open(cfg["output"], encoding="utf-8"))
            ms = self._marker_objs(); n = 0
            for m in ms:
                for stem, (u, v, _pin, _sc, how) in (res.get(m.label) or {}).items():
                    cur = {g._stem(a): a for a in m.projections}
                    if stem in cur and (len(m.projections[cur[stem]]) < 3 or m.projections[cur[stem]][2]):
                        continue                     # never move a user-pinned placement
                    if how in ("saddle", "template"):
                        m.projections.pop(cur.get(stem, stem), None)
                        m.projections[stem] = [u, v, False]; n += 1
            self._store_markers(ms)
            self.log(f"Refine Markers: {n} projections placed/refined (unpinned; pinned ones untouched)")
        self._run("Refine Markers", [("Placing markers", [cm.PYTHON, "refine_markers", path], 100)], loaded)

    # ------------------------------------------------------------ point cloud filtering
    def filter_point_cloud(self, confidence_only=False):
        if not self._require_saved("Filter Point Cloud"):
            return None
        from .dialogs import FilterPointCloudDialog
        ch = self.chunk
        if not ch.point_cloud_ply or not os.path.exists(ch.point_cloud_ply):
            QMessageBox.information(self, "Filter Point Cloud", "Build or import a point cloud first."); return
        dlg = FilterPointCloudDialog(self, confidence_only)
        if not dlg.exec():
            return
        wd = self.project.default_workdir(ch)
        out = os.path.join(wd, "dense", "point_cloud_filtered.ply")
        if os.path.abspath(out) == os.path.abspath(ch.point_cloud_ply):
            out = os.path.join(wd, "dense", "point_cloud_filtered2.ply")
        cfg = dict(dlg.settings(), input=ch.point_cloud_ply, output=out,
                   model=self._model_dir() if os.path.isdir(self._model_dir()) else "")
        if ch.georef:
            cfg["up"] = [0.0, 0.0, 1.0]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        path = os.path.join(wd, "dense", "filter_ui.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        prev = dict((ch.meta or {}).get("point_cloud") or {})

        def meta(dt, stages):
            from . import procmeta
            e = procmeta.record(ch, "point_cloud", params=dict(prev.get("params") or {}, filter=cfg),
                                seconds=prev.get("seconds"), stages=prev.get("stages"), points=ch.point_count,
                                fusion_seconds=prev.get("fusion_seconds"), filtering_seconds=dt,
                                points_fused=prev.get("points"), filtering=[], **procmeta.file_info(out))
            e["filtered_from"] = cfg["input"]
        self._run("Filter Point Cloud", [("Filtering point cloud", [cm.PYTHON, "filter_cloud", path], 100)],
                  lambda: self.load_result("cloud", out), meta)

    def gradual_selection(self):
        if not self._require_saved("Gradual Selection"):
            return None
        from .dialogs import GradualSelectionDialog
        from . import colmap_io as cio
        from . import pcfilter as pf
        ch = self.chunk
        model = self._sparse_model() if os.path.isdir(self._model_dir()) else None
        if model is None:
            QMessageBox.information(self, "Gradual Selection", "Align photos first."); return
        wd = self.project.default_workdir(ch)
        scales = None
        db = os.path.join(wd, "database.db")
        if os.path.exists(db):
            try:
                scales = pf.keypoint_scales(db, model)
            except Exception as e:
                self.log(f"Gradual Selection: keypoint scales unavailable ({e})")
        metrics = pf.tie_point_metrics(model, scales)
        dlg = GradualSelectionDialog(metrics, self)
        if not dlg.exec():
            return
        sel = dlg.selection()
        n = cio.remove_points(model, sel)
        cio.write_model(self._model_dir(), model)
        from .plyio import save_ply
        save_ply(os.path.join(wd, "tie_points.ply"), model.points.xyz, model.points.rgb / 255.0)
        ch.tie_points_ply = os.path.join(wd, "tie_points.ply"); ch.tie_point_count = len(model.points)
        self._model_cache = (None,)
        self.log(f"Gradual Selection ({dlg.crit.currentText()} > {dlg.level.value():g}): "
                 f"removed {n:,} tie points, {len(model.points):,} remain. Run Optimize Cameras to re-adjust.")
        self.set_modified(); self.reload_view()
