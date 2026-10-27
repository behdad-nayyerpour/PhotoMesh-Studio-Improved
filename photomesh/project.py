"""Project data model, saved as JSON (*.pmsx)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# path fields of a chunk (stored relative to the .pmsx when below its folder)
CHUNK_PATHS = ("workdir", "tie_points_ply", "point_cloud_ply", "model_ply")
PRODUCT_PATHS = (("texture", ("dir", "obj", "glb", "uv_npz")), ("tiled_model", ("dir", "tileset", "index", "zip")),
                 ("orthomosaic", ("dir", "path", "height_path")), ("dem", ("dir", "path")), ("shapes", ("path",)))
CAMERA_PATHS = ("path", "thumb", "desc")


class ProjectNotSaved(RuntimeError):
    """Processing needs a saved project: its data goes to <name>.files next to the .pmsx."""


def data_folder(project_path):
    """<name>.files next to <name>.pmsx (all processing data of the project)."""
    return os.path.splitext(os.path.abspath(project_path))[0] + ".files"


def is_under(path, folder):
    try:
        a = os.path.normcase(os.path.abspath(path)); b = os.path.normcase(os.path.abspath(folder))
        return os.path.commonpath([a, b]) == b
    except ValueError:              # other drive
        return False


def to_relative(path, base):
    """Path relative to base (forward slashes) when it lies below base, else unchanged."""
    if not path or not os.path.isabs(path) or not is_under(path, base):
        return path
    return os.path.relpath(os.path.abspath(path), base).replace(os.sep, "/")


def _resolve(v, base):
    return os.path.normpath(os.path.join(base, v)) if v and not os.path.isabs(v) else v


@dataclass
class Camera:
    path: str
    label: str = ""
    enabled: bool = True
    aligned: bool = False
    # source reference (from EXIF GPS), degrees / metres
    lon: Optional[float] = None
    lat: Optional[float] = None
    alt: Optional[float] = None
    # estimated camera centre in the reconstruction frame
    est: Optional[list] = None
    # rotation matrix rows (world->camera), for drawing frusta
    rot: Optional[list] = None
    # cached analysis (fastpipe): thumbnail, global descriptor, image size
    thumb: str = ""
    desc: str = ""
    width: int = 0
    height: int = 0
    # image name in the COLMAP model (photonames.py): exact link camera <-> model
    colmap_name: str = ""

    def info(self):
        return {"lat": self.lat, "lon": self.lon, "alt": self.alt, "desc": self.desc,
                "width": self.width, "height": self.height}

    def __post_init__(self):
        if not self.label:
            self.label = os.path.basename(self.path)


@dataclass
class Chunk:
    name: str = "Chunk 1"
    cameras: list = field(default_factory=list)
    workdir: str = ""
    tie_points_ply: str = ""
    tie_point_count: int = 0
    point_cloud_ply: str = ""
    point_count: int = 0
    model_ply: str = ""
    face_count: int = 0
    log: list = field(default_factory=list)
    # ground control / reference (see georef.py)
    markers: list = field(default_factory=list)      # georef.Marker dicts
    crs: str = ""                                     # marker coordinate system (WKT/EPSG/name)
    reference_settings: dict = field(default_factory=dict)   # accuracies (georef.DEFAULTS keys)
    georef: dict = field(default_factory=dict)        # offset, similarity, RMSE of last georeferencing
    meta: dict = field(default_factory=dict)          # processing metadata per step (see procmeta.py)
    texture: dict = field(default_factory=dict)       # Build Texture result (texture.py info: dir, obj, pages...)
    tiled_model: dict = field(default_factory=dict)   # Build Tiled Model result (tiles.py: dir, levels, pixel size)
    orthomosaic: dict = field(default_factory=dict)   # Build Orthomosaic result (ortho.py info: path, pixel size, bounds...)
    dem: dict = field(default_factory=dict)           # Build DEM result (ortho.py info: path, pixel size, bounds...)
    shapes: dict = field(default_factory=dict)        # imported shapes: path (shapes.json), count, sources, hidden layers

    def add_photos(self, paths, infos=None):
        """Add images; infos = {path: fastpipe.analyze_image() result}."""
        header_given = infos is not None
        infos = infos or {}
        known = {c.path for c in self.cameras}
        added = []
        for p in paths:
            p = os.path.abspath(p)
            if p in known or os.path.splitext(p)[1].lower() not in IMAGE_EXTS:
                continue
            cam = Camera(path=p)
            inf = infos.get(p)
            if inf and "error" not in inf:
                cam.lon, cam.lat, cam.alt = inf.get("lon"), inf.get("lat"), inf.get("alt")
                cam.thumb, cam.desc = inf.get("thumb", ""), inf.get("desc", "")
                cam.width, cam.height = inf.get("width", 0), inf.get("height", 0)
            elif not header_given:
                read_exif_gps(cam)          # API use without pre-read headers
            self.cameras.append(cam)
            known.add(p)
            added.append(cam)
        return added

    def aligned_count(self):
        return sum(1 for c in self.cameras if c.aligned)


class Project:
    def __init__(self):
        self.path: str = ""
        self.chunks: list[Chunk] = [Chunk()]
        self.active: int = 0
        self.modified = False
        self.relocated: list = []      # (chunk, field, old, new) rebased by load()

    @property
    def chunk(self) -> Chunk:
        if not self.chunks:
            self.chunks.append(Chunk())
            self.active = 0
        return self.chunks[min(self.active, len(self.chunks) - 1)]

    def to_dict(self, base=None):
        """base = folder of the .pmsx: paths below it are written relative
        (portable project folder); photos/products elsewhere stay absolute."""
        chunks = [asdict(c) for c in self.chunks]
        if base:
            for cd in chunks:
                for k in CHUNK_PATHS:
                    cd[k] = to_relative(cd.get(k), base)
                for name, keys in PRODUCT_PATHS:
                    d = cd.get(name) or {}
                    for k in keys:
                        if isinstance(d.get(k), str):
                            d[k] = to_relative(d[k], base)
                for c in cd.get("cameras", []):
                    for k in CAMERA_PATHS:
                        c[k] = to_relative(c.get(k), base)
        return {"format": "photomesh-studio/1", "active": self.active, "chunks": chunks}

    def save(self, path):
        """Atomic: write <path>.tmp, then replace, so a crash/kill mid-save never
        leaves a truncated project file. Paths below the project folder are
        stored relative to it."""
        data = json.dumps(self.to_dict(os.path.dirname(os.path.abspath(path))), indent=1, separators=(",", ": "))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush(); os.fsync(f.fileno())
        for k in range(20):
            try:
                os.replace(tmp, path); break
            except PermissionError:          # Windows: file briefly open by AV/indexer
                if k == 19:
                    raise
                import time; time.sleep(0.25)
        self.path = path
        self.modified = False

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        prj = cls()
        prj.chunks = []
        base = os.path.dirname(os.path.abspath(path))
        known = set(Chunk.__dataclass_fields__)
        cam_known = set(Camera.__dataclass_fields__)
        for cd in data.get("chunks") or []:
            cams = []
            for c in cd.pop("cameras", []):
                for k in CAMERA_PATHS:          # relative = below the project folder
                    if isinstance(c.get(k), str):
                        c[k] = _resolve(c[k], base)
                cams.append(Camera(**{k: v for k, v in c.items() if k in cam_known}))
            cd = {k: v for k, v in cd.items() if k in known}
            # paths saved relative (0.1.7+, benchmark.py) are relative to the project file
            for k in CHUNK_PATHS:
                cd[k] = _resolve(cd.get(k), base)
            ch = Chunk(cameras=cams, **cd)
            # project folder copied/moved (other drive or computer): find products below the new location
            from .relink import rebase_path
            for k in CHUNK_PATHS:
                v = getattr(ch, k)
                if v and not os.path.exists(v):
                    r = rebase_path(v, base)
                    if r:
                        setattr(ch, k, r); prj.relocated.append((ch.name, k, v, r))
            for name, keys in PRODUCT_PATHS:
                prod = getattr(ch, name)
                if not isinstance(prod, dict):
                    prod = {}; setattr(ch, name, prod)
                for k in keys:
                    v = prod.get(k)
                    if not isinstance(v, str):
                        continue
                    if v and not os.path.isabs(v):
                        prod[k] = v = _resolve(v, base)
                    if v and not os.path.exists(v):
                        r = rebase_path(v, base)
                        if r:
                            prod[k] = r; prj.relocated.append((ch.name, k, v, r))
            for c in ch.cameras:
                for k in ("thumb", "desc"):
                    v = getattr(c, k)
                    if v and not os.path.exists(v):
                        r = rebase_path(v, base)
                        if r:
                            setattr(c, k, r)
            prj.chunks.append(ch)
        if not prj.chunks:
            prj.chunks.append(Chunk())
        a = data.get("active", 0)
        # the active chunk is read from the root of the file (0.1.9 and older read it
        # from the last product dict, so it always came back as chunk 0)
        prj.active = a if isinstance(a, int) and not isinstance(a, bool) and 0 <= a < len(prj.chunks) else 0
        prj.path = path
        return prj

    @property
    def data_dir(self):
        """<name>.files next to the .pmsx ("" while unsaved)."""
        return data_folder(self.path) if self.path else ""

    def default_workdir(self, chunk: Chunk):
        """Folder where processing results of a chunk are stored: always inside
        the project's data folder <name>.files (no temporary folder; an
        unsaved project must be saved before processing)."""
        if chunk.workdir:
            return chunk.workdir
        if not self.path:
            raise ProjectNotSaved("Save the project before processing (its data is stored next to the .pmsx).")
        idx = self.chunks.index(chunk) if chunk in self.chunks else 0
        chunk.workdir = os.path.join(self.data_dir, f"chunk{idx}")
        return chunk.workdir

    def external_workdirs(self, path=None):
        """Chunks whose working folder lies outside the folder of the .pmsx
        (old absolute-path projects, the pre-0.1.7 temporary folder)."""
        base = os.path.dirname(os.path.abspath(path or self.path))
        return [c for c in self.chunks if c.workdir and not is_under(c.workdir, base)]


def _rat(v):
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return 0.0


def read_exif_gps(cam: Camera):
    """Fill lon/lat/alt from EXIF GPS tags if Pillow is available."""
    try:
        from PIL import Image
        with Image.open(cam.path) as im:
            exif = im.getexif()
            gps = exif.get_ifd(0x8825) if exif else None
        if not gps:
            return
        def dms(vals, ref):
            d = _rat(vals[0]) + _rat(vals[1]) / 60 + _rat(vals[2]) / 3600
            return -d if ref in ("S", "W") else d
        if 2 in gps and 4 in gps:
            cam.lat = dms(gps[2], gps.get(1, "N"))
            cam.lon = dms(gps[4], gps.get(3, "E"))
        if 6 in gps:
            a = _rat(gps[6])
            cam.alt = -a if gps.get(5, 0) == 1 else a
    except Exception:
        pass
