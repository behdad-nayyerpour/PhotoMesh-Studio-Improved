"""COLMAP integration: locate the binary, build pipelines, run them with QProcess.

Option names changed between COLMAP releases (e.g. SiftExtraction.* ->
FeatureExtraction.*), so we read `colmap <command> -h` once and only pass
options the installed build understands.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import numpy as np
from PySide6.QtCore import QObject, QProcess, Signal, QSettings

_HELP_CACHE: dict = {}


APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundled_candidates():
    """COLMAP shipped next to the app: <app>/tools/colmap or <app>/../tools/colmap."""
    for root in (APP_ROOT, os.path.dirname(APP_ROOT)):
        d = os.path.join(root, "tools", "colmap")
        for name in ("COLMAP.bat", os.path.join("bin", "colmap.exe"), os.path.join("bin", "colmap"), "colmap"):
            yield os.path.join(d, name)


def _resolve(path: str) -> str:
    """Prefer bin/colmap.exe over the COLMAP.bat wrapper on Windows and put
    the bundled lib folder (DLLs, Qt plugins) on PATH for child processes."""
    path = os.path.abspath(path)
    base = os.path.dirname(path)
    if path.lower().endswith(".bat"):
        exe = os.path.join(base, "bin", "colmap.exe")
        if os.path.isfile(exe):
            path, base = exe, os.path.join(base, "bin")
    lib = os.path.join(os.path.dirname(base), "lib")
    if sys.platform.startswith("win") and os.path.isdir(lib):
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if lib not in parts:
            os.environ["PATH"] = lib + os.pathsep + os.environ.get("PATH", "")
        plugins = os.path.join(lib, "plugins")
        if os.path.isdir(plugins):
            os.environ.setdefault("QT_PLUGIN_PATH", plugins)
    return path


def find_colmap() -> str | None:
    """Tools > Preferences path, then the COLMAP shipped next to the app
    (tools/colmap - the tested version, also in the portable package), then PATH."""
    custom = QSettings().value("colmap/path", "", str)
    if custom and os.path.isfile(custom):
        return _resolve(custom)
    for c in _bundled_candidates():
        if os.path.isfile(c) and (os.name == "nt" or os.access(c, os.X_OK)):
            return _resolve(c)
    for name in ("colmap", "COLMAP", "colmap.bat", "COLMAP.bat", "colmap.exe"):
        p = shutil.which(name)
        if p:
            return _resolve(p)
    cands = []
    if sys.platform.startswith("win"):
        cands += [r"C:\COLMAP\COLMAP.bat", r"C:\Program Files\COLMAP\COLMAP.bat",
                  r"C:\Program Files\COLMAP\bin\colmap.exe"]
    for c in cands:
        if os.path.isfile(c):
            return _resolve(c)
    return None


# ------------------------------------------------------------------ GLOMAP
GLOMAP = "@glomap"          # first element of a step's args -> run glomap.exe, not colmap


def find_glomap() -> str | None:
    """GLOMAP (global SfM, github.com/colmap/glomap) bundled in <app>/../tools/glomap."""
    custom = QSettings().value("glomap/path", "", str)
    if custom and os.path.isfile(custom):
        return os.path.abspath(custom)
    for root in (APP_ROOT, os.path.dirname(APP_ROOT)):
        for name in (os.path.join("bin", "glomap.exe"), os.path.join("bin", "glomap"), "glomap.exe"):
            c = os.path.join(root, "tools", "glomap", name)
            if os.path.isfile(c):
                return os.path.abspath(c)
    p = shutil.which("glomap")
    return os.path.abspath(p) if p else None


PYTHON = "@python"   # same sentinel as dense.PYTHON


def python_exe():
    """Interpreter for photomesh.tasks child processes (console python.exe,
    not pythonw.exe, so stdout can be piped)."""
    exe = sys.executable
    d, b = os.path.split(exe)
    if b.lower() == "pythonw.exe" and os.path.exists(os.path.join(d, "python.exe")):
        exe = os.path.join(d, "python.exe")
    return exe


def task_env():
    """Environment for child python processes: our package on PYTHONPATH."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def resolve_cmd(exe, args):
    """(program, args) for a step: GLOMAP steps run glomap, PYTHON steps our
    photomesh.tasks module, the rest colmap."""
    if args and args[0] == PYTHON:
        return python_exe(), ["-m", "photomesh.tasks", *[str(a) for a in args[1:]]]
    if args and args[0] == GLOMAP:
        g = find_glomap()
        if not g:
            raise RuntimeError("GLOMAP not found (expected in tools\\glomap\\bin)")
        return g, list(args[1:])
    return exe, list(args)


class Try(list):
    """Step args whose failure is not fatal: on a non-zero exit the runner
    inserts fallback() steps (or continues if fallback is None). Before
    fallback() runs, the runner sets .error (e.g. "exit code -1073740791"),
    .exit_code (int or None) and .output_tail (last console lines)."""
    def __init__(self, args, fallback=None, meter=None):
        super().__init__(args)
        self.fallback = fallback
        self.meter = meter
        self.error = ""
        self.exit_code = None
        self.output_tail = []


class Cmd(list):
    """Plain step args with a progress meter (see progress.py)."""
    def __init__(self, args, meter=None):
        super().__init__(args)
        self.meter = meter


def nvidia_driver_present() -> bool:
    """True if an NVIDIA display driver (the CUDA driver API) is installed.
    Without it the CUDA build of COLMAP can only run its CPU code paths."""
    if os.environ.get("PHOTOMESH_FORCE_NO_GPU"):
        return False
    if os.name == "nt":
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        return os.path.exists(os.path.join(sysdir, "nvcuda.dll"))
    if os.path.exists("/proc/driver/nvidia/version"):
        return True
    import ctypes.util
    return ctypes.util.find_library("cuda") is not None


def gpu_status(exe=None) -> str:
    if not nvidia_driver_present():
        return "CPU only (no NVIDIA driver)"
    if exe and not has_cuda(exe):
        return "CPU only (COLMAP without CUDA)"
    return "CUDA"


def has_cuda(exe: str) -> bool:
    """CUDA usable: an NVIDIA driver is installed and COLMAP is a CUDA build
    (COLMAP prints '... without CUDA' in its banner for CPU-only builds)."""
    if not nvidia_driver_present():
        return False
    key = (exe, "__banner__")
    if key not in _HELP_CACHE:
        try:
            out = subprocess.run([exe, "help"], capture_output=True, text=True, timeout=30)
            _HELP_CACHE[key] = out.stdout + out.stderr
        except Exception:
            _HELP_CACHE[key] = ""
    banner = _HELP_CACHE[key].strip()
    return bool(banner) and "without cuda" not in banner.lower()


def colmap_options(exe: str, command: str) -> set:
    key = (exe, command)
    if key not in _HELP_CACHE:
        try:
            out = subprocess.run([exe, command, "-h"], capture_output=True,
                                 text=True, timeout=30)
            text = out.stdout + out.stderr
        except Exception:
            text = ""
        _HELP_CACHE[key] = set(re.findall(r"--([A-Za-z0-9_.]+)", text))
    return _HELP_CACHE[key]


def opt(exe, command, candidates, value):
    """Return ['--name', value] for the first candidate option supported."""
    supported = colmap_options(exe, command)
    for c in candidates:
        if c in supported:
            return [f"--{c}", str(value)]
    if not supported:  # help unavailable: fall back to the legacy name
        return [f"--{candidates[-1]}", str(value)]
    return []


def image_max_dim(paths) -> int:
    from PySide6.QtGui import QImageReader
    best = 0
    for p in paths[:5]:
        s = QImageReader(p).size()
        best = max(best, s.width(), s.height())
    return best or 3200


# --------------------------------------------------------------- pipelines
ACCURACY = ["Highest", "High", "Medium", "Low", "Lowest"]
ACCURACY_DOWNSCALE = {"Highest": 1, "High": 1, "Medium": 2, "Low": 4, "Lowest": 8}
QUALITY = ["Ultra high", "High", "Medium", "Low", "Lowest"]
QUALITY_DOWNSCALE = {"Ultra high": 1, "High": 2, "Medium": 4, "Low": 8, "Lowest": 16}
DEPTH_FILTERING = ["Disabled", "Mild", "Moderate", "Aggressive"]
DEPTH_MIN_PIXELS = {"Disabled": 2, "Mild": 3, "Moderate": 5, "Aggressive": 8}
FACE_COUNT_DEPTH = {"High": 11, "Medium": 10, "Low": 9}


def align_steps(exe, chunk, wd, s, names=None, infos=None):
    """Steps for Align Photos. s = settings dict from AlignPhotosDialog.

    s["mode"]: "Fast" (fastpipe: preselected pairs + coarse-to-fine, default),
               "Exhaustive" or "Sequential" (plain COLMAP matchers).
    """
    if s.get("mode", "Fast") == "Fast" and names is not None:
        from .fastpipe import fast_align_steps
        return fast_align_steps(exe, wd, names, infos, s)
    s = dict(s, sequential=s.get("mode") == "Sequential" or s.get("sequential", False))
    img_dir = image_dir(wd)
    db = os.path.join(wd, "database.db")
    sparse = os.path.join(wd, "sparse")
    gpu = "1" if s.get("use_gpu", True) else "0"
    maxdim = image_max_dim([c.path for c in chunk.cameras if c.enabled])
    max_size = max(256, maxdim // ACCURACY_DOWNSCALE[s["accuracy"]])
    fe = ["feature_extractor", "--database_path", db, "--image_path", img_dir,
          "--image_list_path", image_list(wd),
          "--ImageReader.single_camera", "0"]   # identical cameras merged below
    fe += opt(exe, "feature_extractor", ["FeatureExtraction.max_image_size",
                                          "SiftExtraction.max_image_size"], max_size)
    if s["key_point_limit"] > 0:
        fe += opt(exe, "feature_extractor", ["SiftExtraction.max_num_features"],
                  s["key_point_limit"])
    fe += opt(exe, "feature_extractor", ["FeatureExtraction.use_gpu",
                                          "SiftExtraction.use_gpu"], gpu)
    matcher = "sequential_matcher" if s.get("sequential") else "exhaustive_matcher"
    m = [matcher, "--database_path", db]
    m += opt(exe, matcher, ["FeatureMatching.use_gpu", "SiftMatching.use_gpu"], gpu)
    if s.get("guided_matching"):
        m += opt(exe, matcher, ["FeatureMatching.guided_matching",
                                "SiftMatching.guided_matching"], "1")
    from .fastpipe import mapper_steps, camera_groups_step
    n_img = len([c for c in chunk.cameras if getattr(c, "enabled", True)])
    if os.path.exists(db):
        os.remove(db)
    os.makedirs(sparse, exist_ok=True)
    os.makedirs(os.path.join(wd, "sparse_txt"), exist_ok=True)
    return [
        ("Detecting points", fe, 30),
        *([camera_groups_step(db)] if s.get("single_camera") else []),
        ("Matching points", m, 30),
        *mapper_steps(exe, db, img_dir, sparse, s, n_img, image_list(wd)),
        ("Exporting tie points", ["model_converter", "--input_path",
         os.path.join(sparse, "0"), "--output_path",
         os.path.join(wd, "tie_points.ply"), "--output_type", "PLY"], 2),
        ("Exporting cameras", ["model_converter", "--input_path",
         os.path.join(sparse, "0"), "--output_path", os.path.join(wd, "sparse_txt"),
         "--output_type", "TXT"], 3),
    ]


def point_cloud_path(wd):
    """Fused dense cloud; must live at dense/fused.ply (+ .vis) for delaunay_mesher."""
    return os.path.join(wd, "dense", "fused.ply")


def point_cloud_steps(exe, chunk, wd, s):
    dense = os.path.join(wd, "dense")
    maxdim = image_max_dim([c.path for c in chunk.cameras if c.enabled])
    max_size = max(256, maxdim // QUALITY_DOWNSCALE[s["quality"]])
    pm = ["patch_match_stereo", "--workspace_path", dense]
    pm += opt(exe, "patch_match_stereo", ["PatchMatchStereo.max_image_size"], max_size)
    pm += opt(exe, "patch_match_stereo", ["PatchMatchStereo.geom_consistency"], "true")
    fu = ["stereo_fusion", "--workspace_path", dense,
          "--output_path", point_cloud_path(wd)]
    fu += opt(exe, "stereo_fusion", ["StereoFusion.min_num_pixels"],
              DEPTH_MIN_PIXELS[s["depth_filtering"]])
    fu += opt(exe, "stereo_fusion", ["StereoFusion.max_image_size"], max_size)
    return [
        ("Undistorting images", ["image_undistorter", "--image_path",
         image_dir(wd), "--input_path", os.path.join(wd, "sparse", "0"),
         "--output_path", dense, "--output_type", "COLMAP",
         "--max_image_size", str(max_size)], 10),
        ("Building depth maps", pm, 75),
        ("Building point cloud", fu, 15),
    ]


def model_output(wd, s):
    return os.path.join(wd, "model_poisson.ply" if s["source"] == "Point cloud" else "model_delaunay.ply")


def model_steps(exe, chunk, wd, s):
    dense = os.path.join(wd, "dense")
    src = s["source"]
    out = os.path.join(wd, "model_poisson.ply" if src == "Point cloud" else "model_delaunay.ply")
    if os.path.exists(out):
        os.remove(out)   # never show a stale mesh if meshing fails
    steps = []
    if src == "Point cloud":
        cloud = getattr(chunk, "point_cloud_ply", "") or point_cloud_path(wd)
        cmd = ["poisson_mesher", "--input_path", cloud,
               "--output_path", out]
        depth = FACE_COUNT_DEPTH.get(s["face_count"], 10)
        cmd += opt(exe, "poisson_mesher", ["PoissonMeshing.depth"], depth)
        cmd += opt(exe, "poisson_mesher", ["PoissonMeshing.trim"], 7 if s["interpolation"] == "Enabled" else 10)
        steps.append(("Generating mesh (Poisson)", cmd, 100))
    else:
        if not os.path.isdir(os.path.join(dense, "sparse")):
            steps.append(("Undistorting images", ["image_undistorter", "--image_path",
                          image_dir(wd), "--input_path",
                          os.path.join(wd, "sparse", "0"), "--output_path", dense,
                          "--output_type", "COLMAP"], 20))
        itype = "dense" if src == "Depth maps" else "sparse"
        steps.append(("Generating mesh (Delaunay)", ["delaunay_mesher", "--input_path",
                      dense if itype == "dense" else os.path.join(dense, "sparse"), "--input_type", itype, "--output_path", out], 80))
    return steps


def image_dir(wd):
    """Folder COLMAP reads images from (see prepare_images)."""
    f = os.path.join(wd, "image_root.txt")
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            r = fh.read().strip()
        if r:
            return r if os.path.isabs(r) else os.path.normpath(os.path.join(wd, r))
    return os.path.join(wd, "images")


def image_list(wd):
    return os.path.join(wd, "image_list.txt")


def prepare_images(chunk, wd):
    """Tell COLMAP which photos to use. Returns {photo path: COLMAP image name}.

    If all enabled photos live in one folder (the normal case) COLMAP reads
    them in place - nothing is copied or written next to the photos, and an
    image list restricts processing to the enabled ones. Otherwise photos are
    exposed in <wd>/images via hard links (same volume), symlinks, or copies.
    """
    os.makedirs(wd, exist_ok=True)
    cams = [c for c in chunk.cameras if c.enabled]
    dirs = {os.path.dirname(os.path.abspath(c.path)) for c in cams}
    names = [os.path.basename(c.path) for c in cams]
    if len(dirs) == 1 and len(set(n.lower() for n in names)) == len(names):
        root = dirs.pop()
        mapping = {c.path: os.path.basename(c.path) for c in cams}
    else:
        root = os.path.join(wd, "images")
        os.makedirs(root, exist_ok=True)
        manifest_path = os.path.join(wd, ".image_stage.json")
        try:
            with open(manifest_path, encoding="utf-8") as f:
                previous = json.load(f)
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}

        def stamp(path):
            st = os.stat(path)
            return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]

        mapping, used, copied = {}, set(), {}
        for c in cams:
            base, ext = os.path.splitext(os.path.basename(c.path))
            name, i = base + ext, 1
            while name.lower() in used:
                name = f"{base}_{i}{ext}"; i += 1
            used.add(name.lower()); mapping[c.path] = name
            dst = os.path.join(root, name)
            source = os.path.normcase(os.path.abspath(c.path))
            source_stamp = stamp(c.path)
            if os.path.lexists(dst):
                try:
                    if os.path.samefile(c.path, dst):
                        continue  # a link follows changes to the source automatically
                    cached = previous.get(name)
                    if cached == [source, source_stamp, stamp(dst)]:
                        copied[name] = cached
                        continue  # unchanged copy; avoid decoding/copying it again
                except OSError:
                    pass
                os.remove(dst)
            try:
                os.link(c.path, dst)          # hard link: no admin rights needed on NTFS
            except OSError:
                try:
                    os.symlink(c.path, dst)
                except OSError:
                    shutil.copy2(c.path, dst)
                    copied[name] = [source, source_stamp, stamp(dst)]
        tmp_manifest = manifest_path + ".part"
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(copied, f)
        os.replace(tmp_manifest, manifest_path)
    with open(os.path.join(wd, "image_root.txt"), "w", encoding="utf-8") as f:
        f.write("images" if root == os.path.join(wd, "images") else root)   # relative: portable data folder
    with open(image_list(wd), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(mapping.values())) + "\n")
    return mapping


def qvec_to_rot(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * w * z, 2 * x * z + 2 * w * y],
        [2 * x * y + 2 * w * z, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * w * x],
        [2 * x * z - 2 * w * y, 2 * y * z + 2 * w * x, 1 - 2 * x * x - 2 * y * y]])


def read_images_txt(path):
    """Return {image_name: (centre(3,), R(3x3))} from COLMAP images.txt."""
    res = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = [l for l in f if not l.startswith("#")]
    for i in range(0, len(lines), 2):
        parts = lines[i].split()
        if len(parts) < 10:
            continue
        q = list(map(float, parts[1:5]))
        t = np.array(list(map(float, parts[5:8])))
        R = qvec_to_rot(q)
        res[parts[9]] = (-R.T @ t, R)
    return res


def count_points3d(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return sum(1 for l in f if l.strip() and not l.startswith("#"))
    except Exception:
        return 0


# ----------------------------------------------------------------- runner
class PipelineRunner(QObject):
    progress = Signal(int, str)   # percent, stage label (constant per step)
    stageProgress = Signal(float, str)
    detail = Signal(float, str)   # fine percent, e.g. "Depth maps: geometric pass 12/640 - about 40 min left"
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, exe, steps, parent=None):
        super().__init__(parent)
        self.exe = exe
        self.steps = steps
        self.total = sum(w for _, _, w in steps) or 1
        self.done_weight = 0
        self.idx = -1
        self.proc = None
        self.cancelled = False
        self._ended = False
        self._tail = []
        self._output_buffer = ""
        self._callback_job = None
        self.failure = None          # details of the failed step, for error reports
        self._cmd = None
        self.finished.connect(self._mark_end)

    def _mark_end(self, *_):
        self._ended = True

    def _fail(self, msg, exit_code=None, crashed=False, exc=None):
        import traceback as _tb
        label = self.steps[self.idx][0] if 0 <= self.idx < len(self.steps) else ""
        args = self.steps[self.idx][1] if 0 <= self.idx < len(self.steps) else None
        self.failure = {"step": label, "message": msg, "exit_code": exit_code, "crashed": crashed,
                        "command": self._cmd or (list(args) if isinstance(args, (list, tuple)) else None),
                        "traceback": "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)) if exc else "",
                        "tail": list(self._tail)}
        self.finished.emit(False, msg)

    def start(self):
        self._next()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def _next(self):
        if self.cancelled:
            self.finished.emit(False, "Cancelled by user")
            return
        if self.idx >= 0:
            self.done_weight += self.steps[self.idx][2]
        self.idx += 1
        if self.idx >= len(self.steps):
            self.progress.emit(100, "Finished")
            self.finished.emit(True, "Finished")
            return
        label, args, _ = self.steps[self.idx]
        self._cmd = None
        if label == "log":
            self.log.emit(f"[fastpipe] {args}")
            self.idx += 0
            return self._next()
        if label != "gate":
            self.stageProgress.emit(-1.0, label)
            self.progress.emit(int(100 * self.done_weight / self.total), label)
        if callable(args):
            from .bgjob import BackgroundJob
            job = BackgroundJob(lambda report, cancelled: args() or [], name=label, parent=self)
            self._callback_job = job
            def complete(extra, error):
                self._callback_job = None
                if self.cancelled:
                    self.finished.emit(False, "Cancelled by user")
                elif error:
                    self._fail(f"'{label}' failed: {error}", exc=job.exception)
                else:
                    self.steps[self.idx + 1:self.idx + 1] = extra or []
                    self.total += sum(w for _, _, w in (extra or []))
                    self._next()
                job.deleteLater()
            job.done.connect(complete)
            job.start()
            return
        try:
            prog, pargs = resolve_cmd(self.exe, args)
        except Exception as e:
            if isinstance(args, Try):
                return self._fallback(args, str(e))
            self._fail(f"'{label}' failed: {e}", exc=e); return
        self._cmd = [prog] + list(pargs)
        self.log.emit(f"> {os.path.basename(prog)} {' '.join(pargs)}")
        self._tail = []
        self._output_buffer = ""
        self.proc = QProcess(self)
        if args and args[0] == PYTHON:
            from PySide6.QtCore import QProcessEnvironment
            env = QProcessEnvironment()
            for k, v in task_env().items():
                env.insert(k, v)
            self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self.proc.start(prog, pargs)

    def _read(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "ignore")
        label, _, w = self.steps[self.idx]
        meter = getattr(self.steps[self.idx][1], "meter", None)
        self._output_buffer += text.replace("\r", "\n")
        lines = self._output_buffer.split("\n")
        self._output_buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.log.emit(line.rstrip())
                self._tail = (self._tail + [line.rstrip()])[-40:]
            if meter is not None:
                try:
                    r = meter.feed(line)
                except Exception:   # noqa: BLE001 - a meter must never break a step
                    r = None
                if r:
                    frac, txt = r
                    self.stageProgress.emit(min(1.0, max(0.0, frac)), txt)
                    fine = 100.0 * (self.done_weight + w * frac) / self.total
                    self.progress.emit(min(int(fine), 99), label)
                    self.detail.emit(min(fine, 99.9), txt)
                continue
            m = re.search(r"\[(\d+)/(\d+)\]", line)
            if m and int(m.group(2)) > 0:
                frac = min(1.0, int(m.group(1)) / int(m.group(2)))
                self.stageProgress.emit(frac, line.strip())
                pct = int(100 * (self.done_weight + w * frac) / self.total)
                self.progress.emit(min(pct, 99), label)

    def _fallback(self, args, why, code=None):
        label = self.steps[self.idx][0]
        args.error, args.exit_code, args.output_tail = why, code, list(self._tail)
        try:
            extra = args.fallback() if args.fallback else []
        except Exception as e:      # the fallback decided to give up: clear message
            self._fail(f"'{label}' failed: {e}", exit_code=code, exc=e)
            return
        self.log.emit(f"[fastpipe] '{label}' did not succeed ({why}) - "
                      + ("using fallback" if extra else "continuing"))
        self.steps[self.idx + 1:self.idx + 1] = extra
        self.total += sum(w for _, _, w in extra)
        self._next()

    def _error(self, err):
        if err == QProcess.FailedToStart and not self._ended:
            args = self.steps[self.idx][1]
            if isinstance(args, Try):    # finished() is not emitted after FailedToStart
                return self._fallback(args, "failed to start")
            self._fail(f"Failed to start: {self.exe}")

    def _done(self, code, status):
        if self._output_buffer:
            self._output_buffer += "\n"
            self._read()
        if self._ended:
            return
        if self.cancelled:
            self.finished.emit(False, "Cancelled by user")
            return
        if code != 0 or status != QProcess.NormalExit:
            label, args, _ = self.steps[self.idx]
            if isinstance(args, Try):
                return self._fallback(args, f"exit code {code}", code)
            from .dense import exit_code_text
            from .report import error_reason
            why = error_reason(self._tail) if status == QProcess.NormalExit else ""
            if why:                  # a Python task that stopped with a clear message (issue #3)
                self._fail(f"'{label}' failed: {why}", exit_code=code)
            else:
                self._fail(f"'{label}' failed (exit code {exit_code_text(code)}). See Console for details.",
                           exit_code=code, crashed=status != QProcess.NormalExit)
            return
        self._next()
