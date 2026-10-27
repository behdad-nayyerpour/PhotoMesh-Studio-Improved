"""Exact link between COLMAP image names and the chunk's photos.

COLMAP knows a photo only by its image name. When all photos are in one
folder that is the file name; photos from several folders are exposed in
<workdir>/images under unique names (IMG_001.JPG, IMG_001_1.JPG, ...; see
colmap.prepare_images). The name each camera got is stored in the project
(Camera.colmap_name), so it survives save/open, moving the project folder and
Change Image Paths, which only change Camera.path.

Projects from 0.1.9 and older have no stored names: recover_names() reads
them back from the images/ links, and photo_map() falls back to the file name
only where that is unambiguous. Photos it cannot tell apart are left out and
reported instead of being matched to the wrong camera.
"""
import os
from collections import Counter


def assign_names(chunk, mapping):
    """Store the names of a new alignment ({photo path: COLMAP name}, from
    prepare_images). Photos not in it (disabled) are not in the model."""
    for c in chunk.cameras:
        c.colmap_name = mapping.get(c.path, "")


def _key(st):
    return (st.st_dev, st.st_ino)


def recover_names(chunk, wd):
    """Fill missing Camera.colmap_name from the folder COLMAP read the images
    from (hard links / symlinks to the photos, or the photo folder itself).
    Plain copies match by size and modification time when that is unique.
    Returns the number of names recovered."""
    todo = [c for c in chunk.cameras if not getattr(c, "colmap_name", "") and c.path]
    if not todo or not wd:
        return 0
    try:
        with open(os.path.join(wd, "image_root.txt"), encoding="utf-8") as f:
            root = f.read().strip()
    except OSError:
        return 0
    root = os.path.join(wd, root) if root and not os.path.isabs(root) else root
    if not root or not os.path.isdir(root):
        return 0
    by_inode, by_stamp = {}, {}
    for c in todo:
        try:
            st = os.stat(c.path)
        except OSError:
            continue
        by_inode.setdefault(_key(st), []).append(c)
        by_stamp.setdefault((st.st_size, st.st_mtime_ns), []).append(c)
    taken = {c.colmap_name for c in chunk.cameras if getattr(c, "colmap_name", "")}
    found = {}
    for name in sorted(os.listdir(root)):
        if name in taken:
            continue
        try:
            st = os.stat(os.path.join(root, name))
        except OSError:
            continue
        cands = by_inode.get(_key(st)) or []
        if not cands:
            cands = [c for c in by_stamp.get((st.st_size, st.st_mtime_ns), [])
                     if _stem_ok(name, c.path)]
        if len(cands) == 1:
            found.setdefault(id(cands[0]), []).append((cands[0], name))
    n = 0
    for lst in found.values():
        if len(lst) == 1:                      # one name per photo, else unknown
            c, name = lst[0]; c.colmap_name = name; n += 1
    return n


def _stem_ok(name, path):
    """name is the photo's file name or a de-duplicated variant (IMG_1.JPG -> IMG_1_2.JPG)."""
    b, e = os.path.splitext(os.path.basename(path))
    n, ne = os.path.splitext(name)
    if ne.lower() != e.lower():
        return False
    if n.lower() == b.lower():
        return True
    head, _, tail = n.rpartition("_")
    return head.lower() == b.lower() and tail.isdigit()


def photo_map(chunk):
    """({COLMAP image name: photo path}, [ambiguous file names]).

    Stored names are exact. Cameras without one (older projects) are matched
    by file name only when no other photo of the chunk has that name."""
    cams = [c for c in chunk.cameras if c.path]
    out = {c.colmap_name: c.path for c in cams if getattr(c, "colmap_name", "")}
    names = Counter(os.path.basename(c.path).lower() for c in cams)
    used = {k.lower() for k in out}
    ambiguous = set()
    for c in cams:
        if getattr(c, "colmap_name", ""):
            continue
        b = os.path.basename(c.path)
        if names[b.lower()] > 1 or b.lower() in used:
            ambiguous.add(b)
        else:
            out[b] = c.path
    return out, sorted(ambiguous)


def camera_for_names(chunk, names):
    """{COLMAP name: Camera} for the names of a model (exact names first, then
    unambiguous file names), plus the ambiguous file names."""
    pm, ambiguous = photo_map(chunk)
    by_path = {}
    for c in chunk.cameras:
        by_path.setdefault(c.path, c)
    return {n: by_path[pm[n]] for n in names if n in pm}, ambiguous
