"""Project data folder: everything a project computes lives in <name>.files
next to <name>.pmsx (one sub-folder per chunk), so the project folder can be
copied/moved as a whole. Photos are referenced, never copied.

Save As to a new location copies the chunks' working folders into the new
data folder (free disk space checked first, progress + cancel). The copy is
written straight into its final, new folder - never renamed afterwards: on
Windows a folder whose files are still open (OneDrive / Dropbox sync, virus
scanner, search indexer) cannot be renamed ("Access is denied", issue #2).
Until the copy is complete the folder holds INCOMPLETE_MARK; on cancel/error
it is removed again and the project keeps using its old data. The originals
are kept, except for the pre-0.1.7 temporary folder of unsaved projects,
which is moved.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time

from .project import data_folder, is_under, CHUNK_PATHS, PRODUCT_PATHS, CAMERA_PATHS

MB = 1024 * 1024
LINK_DIRS = ("images",)        # COLMAP photo links (hard/sym links to the photos): re-linked, not duplicated
SPACE_MARGIN = 256 * MB
DEPTH_REL = ("dense/stereo/depth_maps", "dense/stereo/normal_maps", "dense/stereo/consistency_graphs")
PARTIAL_RE = re.compile(r"^chunk\d+[x\d_]*\.partial$", re.I)      # 0.1.7 Save As leftovers
WORKDIR_ENTRIES = {"dense", "sparse", "sparse_txt", "database.db", "coarse.db", "image_root.txt", "image_list.txt",
                   "images", "tie_points.ply", "texture", "tiled", "orthomosaic", "dem", "model.ply",
                   "model_poisson.ply", "cache", "shapes", "mesh.json"}
INCOMPLETE_MARK = "PHOTOMESH_COPY_INCOMPLETE.txt"   # inside a data copy that is not finished yet
RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0, 3.0)            # transient Windows locks (sync client, AV, indexer)


def _retry(fn, *args):
    """fn(*args), retried a few times on PermissionError (Windows file briefly
    open by OneDrive, a virus scanner or the indexer)."""
    for d in RETRY_DELAYS:
        try:
            return fn(*args)
        except PermissionError:
            time.sleep(d)
    return fn(*args)


def remove_tree(path):
    """Remove a folder this module created; read-only files and brief locks
    are handled. Returns True when it is gone."""
    def onexc(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass
        try:
            _retry(func, p)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if not os.path.lexists(path):
        return True
    for d in (0,) + RETRY_DELAYS:
        time.sleep(d)
        try:
            shutil.rmtree(path, onexc=onexc)
        except TypeError:                                     # Python < 3.12
            shutil.rmtree(path, onerror=lambda f, p, e: onexc(f, p, e))
        except OSError:
            pass
        if not os.path.lexists(path):
            return True
    return False


def is_incomplete(folder):
    return os.path.isfile(os.path.join(folder, INCOMPLETE_MARK))


def legacy_temp_root():
    """Where 0.1.6 and older put the data of unsaved projects."""
    return os.path.join(tempfile.gettempdir(), "photomesh_unsaved")


def is_temporary(path):
    return bool(path) and is_under(path, legacy_temp_root())


def _rel(root, base):
    r = os.path.relpath(root, base).replace("\\", "/")
    return "" if r == "." else r


def _in_rel(rel, prefixes):
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def breakdown(path, exclude=None):
    """Sizes below a working folder: {"depth": depth/normal maps and consistency
    graphs, "photo_copies": plain copies of photos in images/, "other": the
    rest (results, models, databases), "files": count}. Photo links count 0."""
    out = {"depth": 0, "photo_copies": 0, "other": 0, "files": 0}
    for root, dirs, files in os.walk(path):
        if exclude:
            dirs[:] = [d for d in dirs if not is_under(os.path.join(root, d), exclude)]
        rel = _rel(root, path)
        top = rel.split("/")[0]
        depth = _in_rel(rel, DEPTH_REL)
        for f in files:
            p = os.path.join(root, f)
            out["files"] += 1
            try:
                if os.path.islink(p):
                    continue
                st = os.stat(p)
            except OSError:
                continue
            if depth:
                out["depth"] += st.st_size
            elif top in LINK_DIRS:
                if st.st_nlink <= 1:
                    out["photo_copies"] += st.st_size
            else:
                out["other"] += st.st_size
    return out


def tree_size(path, exclude=None):
    """(bytes, files) below path; photo links count as 0 bytes (they are
    re-linked, not copied)."""
    b = breakdown(path, exclude)
    return b["depth"] + b["photo_copies"] + b["other"], b["files"]


def item_bytes(it):
    """Bytes an item will copy (depth maps excluded with skip_depth)."""
    return it["bytes"] - (it.get("parts", {}).get("depth", 0) if it.get("skip_depth") else 0)


def free_space(folder):
    """Free bytes on the drive that holds folder (nearest existing parent)."""
    p = os.path.abspath(folder)
    while p and not os.path.exists(p):
        q = os.path.dirname(p)
        if q == p:
            break
        p = q
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def unique_target(root, name):
    """root/name, or root/name_2, _3 ... when that exists and is not empty
    (an unfinished copy of an earlier attempt, marked INCOMPLETE_MARK, is reused)."""
    d = os.path.join(root, name)
    k = 2
    while os.path.exists(d) and (not os.path.isdir(d) or (os.listdir(d) and not is_incomplete(d))):
        d = os.path.join(root, f"{name}_{k}"); k += 1
    return d


def photo_index(chunk):
    """{file name (lower case): [photo paths]} of the chunk's photos, to
    re-link plain copies in images/ to the originals."""
    idx = {}
    for c in getattr(chunk, "cameras", []) or []:
        if c.path:                                  # the COLMAP name when known (exact), else the file name
            idx.setdefault((getattr(c, "colmap_name", "") or os.path.basename(c.path)).lower(), []).append(c.path)
    return idx


def plan(project, new_path, save_as=True):
    """Working folders to bring into the data folder of new_path:
    [{chunk, src, dst, bytes, files, move}]. Save As: every working folder
    not already inside the new project's folder; Save: only those outside
    the project folder (old absolute-path projects / temporary folder)."""
    base = os.path.dirname(os.path.abspath(new_path))
    root = data_folder(new_path)
    items, taken = [], set()
    for i, ch in enumerate(project.chunks):
        wd = ch.workdir
        if not wd or not os.path.isdir(wd):
            continue
        inside = is_under(wd, root) if save_as else is_under(wd, base)
        if inside:
            continue
        name = f"chunk{i}"
        dst = unique_target(root, name)
        while os.path.normcase(dst) in taken:
            name += "x"; dst = unique_target(root, name)
        taken.add(os.path.normcase(dst))
        parts = breakdown(wd, exclude=dst)
        size = parts["depth"] + parts["photo_copies"] + parts["other"]
        items.append({"chunk": ch, "src": os.path.abspath(wd), "dst": dst, "bytes": size, "files": parts["files"],
                      "parts": parts, "skip_depth": False, "photos": photo_index(ch),
                      "move": is_temporary(wd)})
    return items


def check_space(items):
    """(ok, needed, free) for copying items (moves on the same drive need nothing)."""
    need = sum(item_bytes(it) for it in items)
    free = None
    if items:
        free = free_space(items[0]["dst"])
    ok = free is None or not need or free >= need + SPACE_MARGIN
    return ok, need, free


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _link_photo(s, d, photos):
    """images/ entry s -> d as a link: hard link to the original photo (same
    size), else to s itself, else a symlink to the original. True if linked."""
    try:
        size = os.path.getsize(s)
    except OSError:
        size = -1
    orig = [p for p in (photos or {}).get(os.path.basename(s).lower(), [])
            if os.path.isfile(p) and os.path.getsize(p) == size]
    if len(orig) > 1:                               # same name and size: cannot tell which photo it is
        orig = [p for p in orig if _same_file(p, s)]
    for c in orig + [os.path.realpath(s)]:
        try:
            os.link(c, d); return True
        except OSError:
            pass
    for c in orig:
        try:
            os.symlink(c, d); return True
        except OSError:
            pass
    return False


def copy_tree(src, dst, report=None, cancelled=None, done0=0, total=0, skip=(), photos=None):
    """Copy folder src to dst: files under LINK_DIRS (photo links or plain
    photo copies) become links to the original photos where possible (no
    duplicate photo bytes); symlinks are recreated; folders in skip (relative,
    '/') are created empty. Returns bytes copied. Raises InterruptedError on cancel."""
    done = done0
    me = os.path.normcase(os.path.abspath(dst))
    for root, dirs, files in os.walk(src):
        # never walk into the copy itself (dst inside src: Save As into a sub-folder of the
        # old working folder would otherwise copy the copy again and again)
        dirs[:] = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(root, d))) != me
                   and not is_under(os.path.join(root, d), dst)]
        rel = os.path.relpath(root, src)
        top = rel.replace("\\", "/").split("/")[0]
        out = os.path.normpath(os.path.join(dst, rel))
        os.makedirs(out, exist_ok=True)
        if skip and _in_rel(_rel(root, src), skip):
            dirs[:] = []
            continue
        for f in files:
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            s = os.path.join(root, f); d = os.path.join(out, f)
            if os.path.islink(s):
                try:
                    os.symlink(os.readlink(s), d); continue
                except OSError:
                    pass                                    # Windows without symlink rights: copy the target
            if top in LINK_DIRS and _link_photo(s, d, photos):
                continue
            _retry(shutil.copy2, s, d)
            try:
                done += os.path.getsize(d)
            except OSError:
                pass
            if report is not None:
                report(done // MB, max(1, total // MB), os.path.join(rel, f) if rel != "." else f)
    return done - done0


def remap(obj, old, new):
    """Replace the path prefix old -> new in every string below obj (dict/list)."""
    o = os.path.normcase(os.path.normpath(old))

    def fix(v):
        if isinstance(v, str) and v and os.path.isabs(v):
            n = os.path.normcase(os.path.normpath(v))
            if n == o or n.startswith(o.rstrip("\\/") + os.sep):
                return os.path.normpath(os.path.join(new, os.path.relpath(os.path.normpath(v), os.path.normpath(old))))
            return v
        if isinstance(v, dict):
            for k in list(v):
                v[k] = fix(v[k])
        elif isinstance(v, list):
            for k in range(len(v)):
                v[k] = fix(v[k])
        return v
    return fix(obj)


def rebase_chunk(ch, old, new):
    """Point every path of the chunk that lies in old (working folder) to new."""
    for k in CHUNK_PATHS:
        setattr(ch, k, remap(getattr(ch, k), old, new))
    for name, _keys in PRODUCT_PATHS:
        remap(getattr(ch, name), old, new)
    remap(ch.meta, old, new)
    for c in ch.cameras:
        for k in CAMERA_PATHS[1:]:                # thumbnails / descriptors (photos are referenced as they are)
            setattr(c, k, remap(getattr(c, k), old, new))
    fix_image_root(new, old)


def fix_image_root(wd, old):
    """COLMAP's image folder (wd/image_root.txt) pointing into the old
    working folder (photo links) -> the same folder inside wd."""
    f = os.path.join(wd, "image_root.txt")
    if not os.path.exists(f):
        return
    try:
        cur = open(f, encoding="utf-8").read().strip()
    except OSError:
        return
    if cur and os.path.isabs(cur) and is_under(cur, old):
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(remap(cur, old, wd))


def execute(items, report=None, cancelled=None):
    """Copy (or move) every planned working folder; all-or-nothing: on error /
    cancel the new copies are removed and the project is unchanged.
    Returns the list of finished items. Each copy goes straight into its new
    target folder (no rename at the end, see the module notes)."""
    total = sum(item_bytes(it) for it in items)
    done, started = 0, []
    try:
        for it in items:
            dst = it["dst"]
            if os.path.lexists(dst):
                if not (os.path.isdir(dst) and (not os.listdir(dst) or is_incomplete(dst))):
                    raise FileExistsError(f"target folder exists and is not empty: {dst}")
                if not remove_tree(dst):                  # unfinished copy of an earlier attempt
                    raise PermissionError(f"cannot clear the unfinished copy {dst} (files in use?)")
            os.makedirs(dst)
            started.append(it)
            with open(os.path.join(dst, INCOMPLETE_MARK), "w", encoding="utf-8") as f:
                f.write("PhotoMesh Studio is copying project data into this folder.\n"
                        "If this file is still here, the copy did not finish and the folder can be deleted.\n")
            skip = DEPTH_REL if it.get("skip_depth") else ()
            done += copy_tree(it["src"], dst, report, cancelled, done, total, skip=skip, photos=it.get("photos"))
            if skip and it.get("parts", {}).get("depth"):
                try:
                    import json
                    with open(os.path.join(dst, "dense", "depth_maps_info.json"), "w", encoding="utf-8") as fh:
                        json.dump({"removed": True, "bytes": it["parts"]["depth"], "by": "save_as"}, fh)
                except OSError:
                    pass
        for it in items:                                  # all complete: drop the markers
            _retry(os.remove, os.path.join(it["dst"], INCOMPLETE_MARK))
    except BaseException as e:
        left = [it["dst"] for it in started if not remove_tree(it["dst"])]
        if left:
            e.add_note("Could not remove the unfinished copy (delete it later): " + "; ".join(left))
        raise
    return list(items)


def apply(finished):
    """After execute(): repoint the chunks and remove moved (temporary) sources."""
    out = []
    for it in finished:
        rebase_chunk(it["chunk"], it["src"], it["dst"])
        moved = False
        if it.get("move") and is_temporary(it["src"]):
            moved = remove_tree(it["src"])
        out.append((it["chunk"], it["src"], it["dst"], moved))
    return out


def fmt_size(n):
    for u, f in (("GB", 1024 ** 3), ("MB", MB), ("KB", 1024)):
        if n >= f:
            return f"{n / f:.1f} {u}"
    return f"{n} B"


# ------------------------------------------------------------------ stale copies
def looks_like_workdir(folder):
    """A folder with PhotoMesh working-folder content (and no project file)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    low = {n.lower() for n in names}
    return bool(low & WORKDIR_ENTRIES) and not any(n.endswith(".pmsx") for n in low)


def stale_copies(root):
    """Unfinished copies of our own Save As inside a data folder root
    (<name>.files): 0.1.7's 'chunkN.partial' folders with working-folder
    content, and folders marked INCOMPLETE_MARK. [(path, bytes)]."""
    out = []
    if not root or not os.path.isdir(root) or not root.lower().rstrip("\\/").endswith(".files"):
        return out
    for e in sorted(os.listdir(root)):
        p = os.path.join(root, e)
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        if is_incomplete(p) or (PARTIAL_RE.match(e) and looks_like_workdir(p)):
            out.append((p, tree_size(p)[0]))
    return out


# ------------------------------------------------------------------ synced folders
def sync_roots():
    """[(service, folder)] of cloud-synced folders on this computer."""
    out = []
    home = os.path.expanduser("~")
    for var, name in (("OneDriveCommercial", "OneDrive"), ("OneDriveConsumer", "OneDrive"), ("OneDrive", "OneDrive")):
        v = os.environ.get(var)
        if v:
            out.append((name, v))
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            info = os.path.join(base, "Dropbox", "info.json")
            try:
                import json
                d = json.load(open(info, encoding="utf-8"))
                for v in d.values():
                    if isinstance(v, dict) and v.get("path"):
                        out.append(("Dropbox", v["path"]))
            except (OSError, ValueError):
                pass
    for sub, name in (("Dropbox", "Dropbox"), ("Google Drive", "Google Drive"), ("My Drive", "Google Drive"),
                      ("iCloudDrive", "iCloud Drive")):
        out.append((name, os.path.join(home, sub)))
    return out


def synced_folder(path):
    """Name of the sync service whose folder holds path, else None."""
    if not path:
        return None
    for name, root in sync_roots():
        if root and is_under(path, root):
            return name
    parts = [x.lower() for x in re.split(r"[\\/]+", os.path.abspath(path))]
    for x in parts[:-1]:
        if x.startswith("onedrive - ") or x == "onedrive":
            return "OneDrive"
        if x in ("my drive", "google drive"):
            return "Google Drive"
        if x == "dropbox" or x.startswith("dropbox ("):
            return "Dropbox"
    return None


# ------------------------------------------------------------------ clean up
CLEANUP_PARTS = (
    # key, label, relative paths, checked by default, note
    ("depth", "Depth maps", DEPTH_REL, True,
     "only used by 'Reuse depth maps'; Build Point Cloud recomputes them"),
    ("undistorted", "Undistorted photos (dense workspace)", ("dense/images",), False,
     "only used to rebuild the point cloud; recreated by Build Point Cloud"),
    ("database", "Feature matching database", ("database.db", "coarse.db"), False,
     "only used to align again; Align Photos recomputes it"),
)


def _path_size(p):
    if os.path.isdir(p) and not os.path.islink(p):
        return tree_size(p)[0]
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def protected_paths(project):
    """Finished products and photos: never deleted by the clean-up."""
    keep = []
    for ch in project.chunks:
        for k in CHUNK_PATHS:
            v = getattr(ch, k, "")
            if isinstance(v, str) and v:
                keep.append(v)
        for name, keys in PRODUCT_PATHS:
            d = getattr(ch, name, None) or {}
            for k in keys:
                v = d.get(k) if isinstance(d, dict) else None
                if isinstance(v, str) and v:
                    keep.append(v)
        keep += [c.path for c in ch.cameras if c.path]
    return keep


def cleanup_candidates(project):
    """[{key, label, note, checked, chunk, paths, bytes}] of intermediate data
    that can be removed, per chunk, plus unfinished copies in the data folder."""
    out = []
    keep = protected_paths(project)
    for i, ch in enumerate(project.chunks):
        wd = ch.workdir
        if not wd or not os.path.isdir(wd):
            continue
        for key, label, rels, checked, note in CLEANUP_PARTS:
            paths = [os.path.join(wd, *r.split("/")) for r in rels]
            paths = [p for p in paths if os.path.lexists(p)
                     and not any(is_under(k, p) for k in keep)]          # never a product or a photo
            size = sum(_path_size(p) for p in paths)
            if paths and size:
                out.append({"key": key, "label": label, "note": note, "checked": checked, "chunk": ch.name,
                            "paths": paths, "bytes": size})
    if project.path:
        for p, size in stale_copies(data_folder(project.path)):
            if not any(is_under(k, p) for k in keep):
                out.append({"key": "stale", "label": f"Unfinished copy {os.path.basename(p)}",
                            "note": "left by an interrupted or failed Save As; not used", "checked": True,
                            "chunk": "", "paths": [p], "bytes": size})
    return out


def delete_parts(parts, project, report=None, cancelled=None):
    """Delete the paths of the selected cleanup parts (re-checked against the
    protected products/photos). Returns (freed bytes, [paths not removed])."""
    keep = protected_paths(project)
    freed, left = 0, []
    total = max(1, len(parts))
    for k, part in enumerate(parts):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        for p in part["paths"]:
            if any(is_under(x, p) for x in keep):
                left.append(p); continue
            size = _path_size(p)
            if os.path.isdir(p) and not os.path.islink(p):
                ok = remove_tree(p)
                if ok and part["key"] == "depth":
                    os.makedirs(p, exist_ok=True)          # COLMAP workspace layout stays valid
            else:
                try:
                    _retry(os.remove, p); ok = True
                except OSError:
                    ok = False
            if ok:
                freed += size
            else:
                left.append(p)
        if report is not None:
            report(k + 1, total, part["label"])
    return freed, left
