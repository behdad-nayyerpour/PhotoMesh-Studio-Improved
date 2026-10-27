"""Moving projects between computers: relink photos and rebase product paths.

* Photos: "Change Image Paths..." (Tools menu / Workspace context menu on
  Images or a photo) looks for each photo by file name under a folder you pick
  (recursively) and keeps the best match (longest identical trailing path).
  Nothing is written next to the photos.
* Products (working folder, tie points, point cloud, model, thumbnails): when a
  saved absolute path no longer exists, Project.load looks for the same
  trailing path below the folder the .pmsx is in now (e.g. the whole
  project folder was copied from C:\\Users\\A\\bench to D:\\bench).
"""
from __future__ import annotations

import os
import re

from .project import IMAGE_EXTS


def split_any(p):
    """Path components for both Windows and POSIX separators (projects move between OSes)."""
    return [x for x in re.split(r"[\\/]+", p or "") if x and not re.fullmatch(r"[A-Za-z]:", x)]


def rebase_path(path, base, max_parts=8):
    """If `path` doesn't exist, return base/<trailing components of path> that
    exists (longest match first), else None. Existing paths are returned as is."""
    if not path:
        return None
    if os.path.exists(path):
        return path
    parts = split_any(path)
    for k in range(min(len(parts), max_parts), 0, -1):
        c = os.path.join(base, *parts[-k:])
        if os.path.exists(c):
            return os.path.normpath(c)
    return None


def index_folder(folder):
    """{lowercase file name: [full paths]} of the images below folder."""
    idx = {}
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                idx.setdefault(f.lower(), []).append(os.path.join(root, f))
    return idx


def _trail_score(old, new):
    a = [x.lower() for x in split_any(old)][::-1]
    b = [x.lower() for x in split_any(new)][::-1]
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def best_match(old_path, idx):
    cands = idx.get(os.path.basename(old_path.replace("\\", "/")).lower())
    if not cands:
        return None
    return max(cands, key=lambda c: (_trail_score(old_path, c), -len(c)))


def missing_cameras(chunk):
    return [k for k, c in enumerate(chunk.cameras) if not os.path.exists(c.path)]


def relink_chunk(chunk, folder, only_missing=True, idx=None):
    """Point photos at their copies below `folder`. Returns (relinked, not_found)
    where not_found lists the labels that had no match (left unchanged)."""
    idx = index_folder(folder) if idx is None else idx
    n, nf = 0, []
    for c in chunk.cameras:
        if only_missing and os.path.exists(c.path):
            continue
        m = best_match(c.path, idx)
        if m is None:
            nf.append(c.label); continue
        if os.path.normcase(os.path.abspath(m)) != os.path.normcase(os.path.abspath(c.path)):
            c.path = m; n += 1
    if n:
        _update_image_root(chunk)
    return n, nf


def _update_image_root(chunk):
    """COLMAP's image folder for an already processed chunk (workdir/image_root.txt)."""
    wd = chunk.workdir
    f = os.path.join(wd, "image_root.txt") if wd else ""
    if not f or not os.path.exists(f):
        return
    dirs = {os.path.dirname(os.path.abspath(c.path)) for c in chunk.cameras if c.enabled}
    try:
        cur = open(f, encoding="utf-8").read().strip()
    except OSError:
        return
    if cur and not os.path.isabs(cur):
        cur = os.path.join(wd, cur)             # "images" (photo links inside the working folder)
    if len(dirs) == 1 and not os.path.isdir(cur):
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(dirs.pop())
