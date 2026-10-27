"""MainWindow mix-in: manual ground control (Metashape-like marker editing).

- Add Marker at the cursor in the Photo tab (pinned there; no active marker
  needed) or on the 3D surface in the Model view (ray cast); the new marker's
  3D position comes from the surface (mesh, else DEM, else tie points) and it
  is projected into every photo that sees it as unpinned (blue) placements,
  ready for Refine Markers / Optimize Cameras.
- Add Marker in the Ortho tab (GCP on the orthomosaic / DEM): world X, Y from
  the raster's geotransform, Z from the DEM / ortho height grid, mapped into
  the model frame and projected into the photos the same way.
- Markers table cells (label, X, Y, Z, accuracy) are editable with validation;
  every marker edit is undoable (Edit > Undo / Redo).
- Rename / Remove Marker, Filter Photos by Markers / Reset Filter.
"""
from __future__ import annotations

import copy
import math
import os

import numpy as np
from PySide6.QtWidgets import QInputDialog, QMessageBox

from . import georef as g

UNDO_DEPTH = 50


def ray_mesh(V, F, o, d, chunk=400_000):
    """Nearest hit (t > 0) of ray o + t d with triangles V[F] (Moller-Trumbore,
    vectorised, two-sided). Returns t or None."""
    V = np.asarray(V, np.float64); F = np.asarray(F, np.int64)
    o = np.asarray(o, float); d = np.asarray(d, float) / np.linalg.norm(d)
    best = None
    for s in range(0, len(F), chunk):
        f = F[s:s + chunk]
        a = V[f[:, 0]]; e1 = V[f[:, 1]] - a; e2 = V[f[:, 2]] - a
        p = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o - a
        u = np.einsum("ij,ij->i", tv, p) * inv
        q = np.cross(tv, e1)
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        if hit.any():
            tm = float(t[hit].min())
            best = tm if best is None else min(best, tm)
    return best


def parse_coordinate(text):
    """'' -> None (cleared); number (also with a decimal comma) -> float; else ValueError."""
    s = str(text).strip().replace(" ", "")
    if not s:
        return None
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    v = float(s)
    if not math.isfinite(v):
        raise ValueError("not a finite number")
    return v


def parse_accuracy(text):
    """'' -> None (project default); 'a' -> [a, a]; 'a/b' -> [a, b] (xy / z, metres, > 0)."""
    s = str(text).strip()
    if not s:
        return None
    parts = [p for p in s.replace(";", "/").split("/") if p.strip()]
    if not 1 <= len(parts) <= 2:
        raise ValueError("use 'xy' or 'xy/z' (metres)")
    vals = [parse_coordinate(p) for p in parts]
    if any(v is None or v <= 0 for v in vals):
        raise ValueError("accuracy must be > 0")
    return [vals[0], vals[-1]]


class MarkerEditMixin:
    # ------------------------------------------------------------ state / undo
    def _marker_init(self):
        self._mk_undo, self._mk_redo = [], []
        self._ref_pending = {}            # label -> [x, y, z] being typed (incomplete reference)
        self._photo_filter = None         # (labels, set of photo indices) or None

    def _markers_snapshot(self):
        self._mk_undo.append((copy.deepcopy(self.chunk.markers or []), copy.deepcopy(self._ref_pending)))
        del self._mk_undo[:-UNDO_DEPTH]
        self._mk_redo.clear()

    def _restore_markers(self, state):
        self.chunk.markers, self._ref_pending = copy.deepcopy(state[0]), copy.deepcopy(state[1])
        self.set_modified()
        self._refresh_markers_pane()
        self._update_view_markers()

    def undo_markers(self):
        if not self._mk_undo:
            self.statusBar().showMessage("Nothing to undo", 3000); return False
        self._mk_redo.append((copy.deepcopy(self.chunk.markers or []), copy.deepcopy(self._ref_pending)))
        self._restore_markers(self._mk_undo.pop())
        self.log("Undo: markers restored"); return True

    def redo_markers(self):
        if not self._mk_redo:
            self.statusBar().showMessage("Nothing to redo", 3000); return False
        self._mk_undo.append((copy.deepcopy(self.chunk.markers or []), copy.deepcopy(self._ref_pending)))
        self._restore_markers(self._mk_redo.pop())
        self.log("Redo: markers restored"); return True

    def _refresh_markers_pane(self):
        self.markers_pane.pending = self._ref_pending
        self.markers_pane.refresh(self.chunk)

    def _commit_markers(self, ms, msg=None):
        """Store markers (after a snapshot was taken)."""
        self.chunk.markers = [m.to_dict() for m in ms]
        self.set_modified()
        self._refresh_markers_pane()
        self._update_view_markers()
        if hasattr(self, "_update_ortho_markers"):
            try:
                self._update_ortho_markers()
            except Exception as e:  # noqa: BLE001 - display only
                self.log(f"Ortho markers: {e}")
        self.workspace.refresh(self.project) if hasattr(self, "workspace") else None
        if msg:
            self.log(msg)

    @staticmethod
    def _new_label(ms, base="point"):
        labels = {m.label for m in ms}
        n = len(ms) + 1
        while f"{base} {n}" in labels:
            n += 1
        return f"{base} {n}"

    # ------------------------------------------------------------ geometry helpers
    def _camera_of_photo(self, k):
        """(model, image id, sx, sy): photo pixel = COLMAP pixel * (sx, sy)."""
        model = self._sparse_model()
        if model is None or not (0 <= k < len(self.chunk.cameras)):
            return None
        names = g.name_index(model)
        c = self.chunk.cameras[k]
        iid = names.get(g._stem(c.label))
        if iid is None:
            return None
        cam = model.cameras[model.images[iid].camera_id]
        sx = sy = 1.0
        if c.width and c.height and cam.width and (c.width, c.height) != (cam.width, cam.height):
            sx, sy = c.width / cam.width, c.height / cam.height
        return model, iid, sx, sy

    def photo_pixel_ray(self, k, u, v):
        """Ray (origin, unit direction) in the model frame through photo pixel (u, v)."""
        from . import colmap_io as cio
        r = self._camera_of_photo(k)
        if r is None:
            return None
        model, iid, sx, sy = r
        im = model.images[iid]; cam = model.cameras[im.camera_id]
        x, y = cio.undistort_points(cam, np.array([[u / sx, v / sy]], float))
        R = im.R(); t = np.asarray(im.tvec, float)
        C = -R.T @ t
        d = R.T @ np.array([x[0], y[0], 1.0])
        return C, d / np.linalg.norm(d)

    def _raycast_scene(self, build=True):
        """Open3D scene of the model mesh (shared with the shapes photo occlusion)."""
        occ = getattr(self, "_occl", None)
        if occ is None:
            return None
        key = self._occl_key(self.chunk)
        if key is None:
            return None
        if occ.get("key") == key and occ.get("scene") is not None:
            return occ["scene"]
        if occ.get("busy") and build and hasattr(self, "wait_occlusion"):
            self.wait_occlusion(90)                       # the shapes overlay is building it already
            if occ.get("key") == key and occ.get("scene") is not None:
                return occ["scene"]
        if not build or occ.get("busy"):
            return None
        try:
            import open3d as o3d
            from .texture import read_mesh
            V, F = read_mesh(key[0])
            sc = o3d.t.geometry.RaycastingScene()
            sc.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
        except Exception as e:  # noqa: BLE001
            self.log(f"Add Marker: no mesh ray casting ({e})"); return None
        occ.update(key=key, scene=sc)
        return sc

    def surface_hit(self, o, d):
        """First surface point along the ray (model frame): mesh, else DEM, else
        nearest tie point to the ray. Returns (X, source) or (None, reason)."""
        o = np.asarray(o, float); d = np.asarray(d, float); d = d / np.linalg.norm(d)
        sc = self._raycast_scene()
        if sc is not None:
            import open3d as o3d
            ray = o3d.core.Tensor(np.r_[o, d][None].astype(np.float32))
            t = float(sc.cast_rays(ray)["t_hit"].numpy()[0])
            if math.isfinite(t):
                return o + t * d, "model"
        md = getattr(self.model_view, "_mesh_data", None)
        if sc is None and md and md[0] is not None and len(md[1]) <= 3_000_000:
            t = ray_mesh(md[0], md[1], o, d)
            if t is not None:
                return o + t * d, "model"
        try:
            smp = self._sampler("DEM") if self.chunk.georef else None
        except Exception:  # noqa: BLE001
            smp = None
        if smp is not None and d[2] < -1e-3:
            X = self._march_height(smp, o, d)
            if X is not None:
                return X, "DEM"
        model = self._sparse_model()
        if model is not None and len(model.points.xyz):
            P = np.asarray(model.points.xyz, float)
            w = P - o
            t = w @ d
            ok = t > 0
            if ok.any():
                dist = np.linalg.norm(w[ok] - np.outer(t[ok], d), axis=1)
                ang = dist / np.maximum(t[ok], 1e-9)
                j = int(np.argmin(ang))
                if ang[j] < 0.01:                                  # within ~0.6 deg of the ray
                    return o + t[ok][j] * d, "tie points"
        return None, "no surface under the cursor (build a model, DEM or tie points)"

    def _march_height(self, smp, o, d, n=6000):
        """Ray vs height field (local frame; sampler takes world XY): march + bisection."""
        off = self._frame(self.chunk).offset
        step = max(0.25 * float(getattr(smp, "px", 1.0) or 1.0), 0.05)

        def h(p):
            z = smp(np.array([p[0] + off[0]]), np.array([p[1] + off[1]]))[0]
            return z - off[2] if np.isfinite(z) else None
        prev = None
        for k in range(n):
            p = o + d * (k * step)
            z = h(p)
            if z is None:
                prev = None; continue
            if p[2] <= z:
                if prev is None:
                    return p
                a, b = (k - 1) * step, k * step
                for _ in range(30):
                    m = (a + b) / 2; q = o + d * m; zq = h(q)
                    if zq is None:
                        break
                    a, b = (m, b) if q[2] > zq else (a, m)
                return o + d * b
            prev = p
        return None

    def predicted_projections(self, X, skip=(), occlusion=True, margin=2.0):
        """{photo stem: [u, v, False]} for every aligned photo seeing X (model frame),
        in front of the camera, inside the frame and (with a mesh) not hidden."""
        model = self._sparse_model()
        if model is None or X is None:
            return {}
        X = np.asarray(X, float)
        sc = self._raycast_scene(build=False) if occlusion else None
        by_stem = {g._stem(c.label): c for c in self.chunk.cameras}
        out = {}
        rays, keys = [], []
        for iid, im in model.images.items():
            stem = g._stem(im.name)
            if stem in skip:
                continue
            p = g.predict_projection(model, iid, X)
            if p is None:
                continue
            cam = model.cameras[im.camera_id]
            if not (margin <= p[0] < cam.width - margin and margin <= p[1] < cam.height - margin):
                continue
            c = by_stem.get(stem)
            sx = sy = 1.0
            if c is not None and c.width and c.height and (c.width, c.height) != (cam.width, cam.height):
                sx, sy = c.width / cam.width, c.height / cam.height
            out[stem] = [float(p[0] * sx), float(p[1] * sy), False]
            if sc is not None:
                R = im.R(); C = -R.T @ np.asarray(im.tvec, float)
                v = X - C; L = float(np.linalg.norm(v))
                rays.append(np.r_[C, v / L]); keys.append((stem, L))
        if sc is not None and rays:
            import open3d as o3d
            t = sc.cast_rays(o3d.core.Tensor(np.asarray(rays, np.float32)))["t_hit"].numpy()
            for (stem, L), th in zip(keys, t):
                if np.isfinite(th) and th < L - max(0.01 * L, 0.05):
                    out.pop(stem, None)                       # surface in front of the marker
        return out

    # ------------------------------------------------------------ add / pin
    def add_marker_at_photo(self, u, v, k=None, label=None):
        """Photo tab > right-click > Add Marker: new marker pinned at (u, v) on
        photo k (no active marker needed), predicted on the other photos."""
        k = self.current_photo if k is None else k
        if not (0 <= k < len(self.chunk.cameras)):
            return None
        ms = self._marker_objs()
        label = label or self._new_label(ms)
        stem = g._stem(self.chunk.cameras[k].label)
        X, src = None, None
        ray = self.photo_pixel_ray(k, u, v)
        if ray is not None:
            X, src = self.surface_hit(*ray)
        m = g.Marker(label=label, projections={stem: [float(u), float(v), True]})
        pred = self.predicted_projections(X, skip={stem}) if X is not None else {}
        m.projections.update(pred)
        self._markers_snapshot()
        ms.append(m)
        where = (f"surface point from the {src}" if X is not None else
                 "no 3D position yet (place it on a second photo)" if ray is not None else "photo not aligned")
        self._commit_markers(ms, f"Added marker {label}: pinned on {self.chunk.cameras[k].label} at "
                                 f"({u:.1f}, {v:.1f}); {where}; predicted on {len(pred)} other photos (unpinned)")
        self._select_marker_row(label)
        return label

    def add_marker_at_ray(self, o, d, label=None):
        """Model view > right-click > Add Marker: new marker at the surface point
        under the cursor, projected into all photos that see it (unpinned)."""
        X, src = self.surface_hit(o, d)
        if X is None:
            self.statusBar().showMessage(f"Add Marker: {src}", 6000); self.log(f"Add Marker: {src}")
            return None
        ms = self._marker_objs()
        label = label or self._new_label(ms)
        pred = self.predicted_projections(X)
        m = g.Marker(label=label, projections=pred)
        self._markers_snapshot()
        ms.append(m)
        self._commit_markers(ms, f"Added marker {label} on the {src} at ({X[0]:.3f}, {X[1]:.3f}, {X[2]:.3f}) "
                                 f"(model frame); projected into {len(pred)} photos (unpinned)")
        self._select_marker_row(label)
        return label

    # ------------------------------------------------------------ ortho view
    def _ortho_frame(self, ch=None):
        """(R, off) with ortho/DEM world XYZ = R @ local + off (ortho.py: R is the
        top-view rotation of a local project, identity when georeferenced)."""
        ch = ch or self.chunk
        for d in (ch.orthomosaic, ch.dem):
            if d and d.get("rotation") is not None:
                return np.asarray(d["rotation"], float), np.asarray(d.get("offset") or [0, 0, 0], float)
        off = self._world_offset() if hasattr(self, "_world_offset") else None
        return np.eye(3), (np.zeros(3) if off is None else np.asarray(off, float))

    def ortho_from_local(self, X):
        R, off = self._ortho_frame()
        return R @ np.asarray(X, float) + off

    def ortho_to_local(self, P):
        R, off = self._ortho_frame()
        return R.T @ (np.asarray(P, float) - off)

    def _ortho_height(self, X, Y):
        """World Z under (X, Y): the Ortho tab's height grid (DEM / ortho height), else
        read from the height raster directly."""
        z = self.ortho_view.z_at(X, Y) if hasattr(self, "ortho_view") else None
        if z is not None:
            return z, "DEM" if "dem" in getattr(self.ortho_view, "layers", {}) else "orthomosaic height"
        from . import geotiff as gt
        ch = self.chunk
        for d, name in ((ch.dem, "DEM"), (ch.orthomosaic, "orthomosaic height")):
            src = (d or {}).get("path") if name == "DEM" else (d or {}).get("height_path")
            if not src or not os.path.exists(src):
                continue
            try:
                with gt.TiffReader(src) as r:
                    lev = 0                                   # an overview <= 16 MPix (fast, like the Ortho tab)
                    while lev + 1 < len(r.levels) and r.levels[lev].width * r.levels[lev].height > 16_000_000:
                        lev += 1
                    a = r.read_level(lev)[..., 0]
                    x0, y1, px, py = r.geo
                    f = r.width / r.levels[lev].width
                    px, py = px * f, (py or px) * f
                    c = int(math.floor((X - x0) / px)); rr = int(math.floor((y1 - Y) / py))
                    if 0 <= rr < a.shape[0] and 0 <= c < a.shape[1]:
                        z = float(a[rr, c])
                        if math.isfinite(z) and (r.nodata is None or z != r.nodata):
                            return z, name
            except Exception as e:  # noqa: BLE001
                self.log(f"Add Marker: cannot read {src}: {e}")
        return None, ""

    def add_marker_at_ortho(self, X, Y, Z=None, label=None):
        """Ortho tab > right-click > Add Marker: a new marker (GCP) at world (X, Y)
        of the orthomosaic / DEM (its geotransform), Z from the DEM / ortho height
        grid (else the surface below), projected into every photo that sees it
        as unpinned placements - then enter its reference coordinates in the
        Reference pane, pin / refine it on photos and Optimize Cameras."""
        src = ""
        if Z is None:
            Z, src = self._ortho_height(X, Y)
        else:
            src = "DEM" if "dem" in getattr(self.ortho_view, "layers", {}) else "orthomosaic height"
        if Z is not None:
            Xl = self.ortho_to_local([X, Y, Z])
        else:
            R, off = self._ortho_frame()
            zr = [v for v in ((self.chunk.orthomosaic or {}).get("z_range") or []) if v is not None]
            top = (max(zr) if zr else off[2]) + 1000.0
            Xl, src = self.surface_hit(R.T @ (np.array([X, Y, top]) - off), R.T @ np.array([0.0, 0.0, -1.0]))
            if Xl is None:
                msg = f"Add Marker: {src}"
                self.statusBar().showMessage(msg, 6000); self.log(msg)
                return None
            Z = float(self.ortho_from_local(Xl)[2])
        pred = self.predicted_projections(Xl)
        if not pred:
            msg = (f"Add Marker: no aligned photo sees ({X:.3f}, {Y:.3f}); "
                   f"align the photos first or place the marker inside the covered area")
            self.statusBar().showMessage(msg, 8000); self.log(msg)
            return None
        ms = self._marker_objs()
        label = label or self._new_label(ms)
        self._markers_snapshot()
        ms.append(g.Marker(label=label, projections=pred))
        self._commit_markers(ms, f"Added marker {label} in the ortho view at ({X:.3f}, {Y:.3f}, {Z:.3f}) "
                                 f"(height from the {src}); projected into {len(pred)} photos (unpinned)")
        self._select_marker_row(label)
        return label

    def pin_marker(self, label, u, v, k=None):
        """Pin `label` at (u, v) on photo k (replaces its placement there)."""
        k = self.current_photo if k is None else k
        ms = self._marker_objs()
        m = next((x for x in ms if x.label == label), None)
        if m is None or not (0 <= k < len(self.chunk.cameras)):
            return False
        self._markers_snapshot()
        stem = g._stem(self.chunk.cameras[k].label)
        m.projections = {a: b for a, b in m.projections.items() if g._stem(a) != stem}
        m.projections[stem] = [float(u), float(v), True]
        self._commit_markers(ms, f"Marker {label} pinned on {self.chunk.cameras[k].label} at ({u:.1f}, {v:.1f})")
        return True

    def _select_marker_row(self, label):
        p = self.markers_pane
        for r in range(p.rowCount()):
            if p.item(r, 0) and p.item(r, 0).text() == label:
                p.blockSignals(True); p.selectRow(r); p.blockSignals(False)
                break
        self._update_photo_markers()

    # ------------------------------------------------------------ rename / remove
    def rename_marker(self, old, new=None):
        if new is None:
            new, ok = QInputDialog.getText(self, "Rename Marker", "Label:", text=old)
            if not ok:
                return False
        new = str(new).strip()
        ms = self._marker_objs()
        err = None
        if not new:
            err = "The label cannot be empty."
        elif new != old and any(m.label == new for m in ms):
            err = f"A marker named '{new}' already exists."
        if err:
            self._marker_error("Rename Marker", err); return False
        if new == old:
            return True
        self._markers_snapshot()
        for m in ms:
            if m.label == old:
                m.label = new
        if old in self._ref_pending:
            self._ref_pending[new] = self._ref_pending.pop(old)
        gr = self.chunk.georef or {}
        for r in gr.get("rows") or []:
            if r.get("label") == old:
                r["label"] = new
        self._commit_markers(ms, f"Marker '{old}' renamed to '{new}'")
        return True

    def remove_markers(self, labels, confirm=True):
        labels = [l for l in labels if l]
        if not labels:
            return 0
        if confirm and not getattr(self, "_headless", False):
            txt = labels[0] if len(labels) == 1 else f"{len(labels)} markers"
            if QMessageBox.question(self, "Remove Markers", f"Remove {txt}? (Edit > Undo restores it)") \
                    != QMessageBox.Yes:
                return 0
        ms = self._marker_objs()
        keep = [m for m in ms if m.label not in labels]
        n = len(ms) - len(keep)
        if not n:
            return 0
        self._markers_snapshot()
        for l in labels:
            self._ref_pending.pop(l, None)
        self._commit_markers(keep, f"Removed {n} marker(s): {', '.join(labels[:10])}")
        if self._photo_filter:
            self.reset_photo_filter()
        return n

    def _marker_error(self, title, msg):
        self.log(f"{title}: {msg}")
        self.statusBar().showMessage(f"{title}: {msg}", 8000)
        if not getattr(self, "_headless", False):
            QMessageBox.warning(self, title, msg)

    # ------------------------------------------------------------ table edits
    def edit_marker_cell(self, label, col, text):
        """Markers table edit: col 0 label, 1-3 X/Y/Z, 4 accuracy. Returns True if applied."""
        title = "Markers"
        try:
            if col == 0:
                return self.rename_marker(label, text)
            ms = self._marker_objs()
            m = next((x for x in ms if x.label == label), None)
            if m is None:
                return False
            if col in (1, 2, 3):
                v = parse_coordinate(text)
                cur = list(self._ref_pending.get(label) or (m.ref if m.ref is not None else [None, None, None]))
                if cur[col - 1] == v:
                    return True
                cur[col - 1] = v
                crs = g.CRS.parse(self.chunk.crs) if self.chunk.crs else None
                if v is not None and crs is not None and getattr(crs, "kind", "") == "geographic":
                    lim = 180.0 if col == 1 else 90.0 if col == 2 else None
                    if lim is not None and abs(v) > lim:
                        raise ValueError(f"{'longitude' if col == 1 else 'latitude'} must be within ±{lim:g}°")
                self._markers_snapshot()
                if all(c is not None for c in cur):
                    m.ref = [float(c) for c in cur]; self._ref_pending.pop(label, None)
                    msg = f"Marker {label}: reference set to {m.ref}"
                elif all(c is None for c in cur):
                    m.ref = None; self._ref_pending.pop(label, None)
                    msg = f"Marker {label}: reference cleared"
                else:
                    m.ref = None; self._ref_pending[label] = cur      # incomplete: not used until X, Y, Z set
                    msg = f"Marker {label}: reference incomplete ({'XYZ'[col - 1]} set) - enter X, Y and Z"
                self._commit_markers(ms, msg)
                return True
            if col == 4:
                a = parse_accuracy(text)
                if a == m.acc:
                    return True
                self._markers_snapshot()
                m.acc = a
                self._commit_markers(ms, f"Marker {label}: accuracy {a if a else 'default'}")
                return True
        except ValueError as e:
            self._marker_error(title, f"'{text}' is not valid for {['label', 'X', 'Y', 'Z', 'accuracy'][col]}: {e}")
            self._refresh_markers_pane()
            return False
        return False

    # ------------------------------------------------------------ photos filter
    def marker_photo_indices(self, labels):
        labs = set(labels)
        stems = set()
        for m in self.chunk.markers or []:
            if m["label"] in labs:
                stems |= {g._stem(p) for p in (m.get("projections") or {})}
        return {k for k, c in enumerate(self.chunk.cameras) if g._stem(c.label) in stems}

    def filter_photos_by_markers(self, labels=None):
        """Photos pane shows only the photos where the markers are placed (pinned or not)."""
        if labels is None:
            labels = self.markers_pane.selected_labels()
        labels = [l for l in labels if l]
        if not labels:
            self.statusBar().showMessage("Select one or more markers first", 4000); return set()
        idx = self.marker_photo_indices(labels)
        self._photo_filter = (labels, idx)
        self.photos.set_filter(idx)
        self.log(f"Photos filtered by markers {', '.join(labels[:10])}: {len(idx)} photos")
        if "Photos" in getattr(self, "docks", {}):
            self.docks["Photos"].raise_()
        return idx

    def reset_photo_filter(self):
        self._photo_filter = None
        self.photos.set_filter(None)

    def _marker_pane_action(self, action, labels):
        if action == "Add Marker":
            self.add_marker()
        elif action == "Rename Marker..." and labels:
            self.rename_marker(labels[0])
        elif action == "Remove Markers" and labels:
            self.remove_markers(labels)
        elif action == "Filter Photos by Markers":
            self.filter_photos_by_markers(labels)
        elif action == "Reset Filter":
            self.reset_photo_filter()
        elif action == "Show Marker" and labels:
            self._goto_marker(labels[0])
