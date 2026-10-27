"""Error reports (Help > Report a Problem..., and every failed step / crash).

A report is a Markdown file plus a .zip (report + anonymised logs + system
info) in %LOCALAPPDATA%\\PhotoMeshStudio\\reports (override with
PHOTOMESH_REPORT_DIR). The app never sends anything by itself: the error
dialog can open the user's browser at a GitHub "new issue" page with the
title and a (shortened) body prefilled, and the user submits it.

Privacy (also for later commercial use): no photos, no coordinates (only the
CRS name), the user home becomes %USERPROFILE%, project/photo folders become
<project>/<photos>, other absolute paths keep only the drive and file name,
user and computer names are removed.
"""
from __future__ import annotations

import datetime
import getpass
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

from . import APP_NAME, APP_VERSION

# ---------------------------------------------------------------- configuration
# Where "Report to GitHub" points. A customer build can point it to its own
# tracker or set it to None (or PHOTOMESH_ISSUES_URL=off) to hide the button.
ISSUES_URL = "https://github.com/behdad-nayyerpour/photomesh-studio-improved/issues/new"
ISSUE_LABELS = "bug"
MAX_URL = 7000            # browsers/GitHub cope with ~8 KB; keep a margin
LOG_LINES = 300           # console/log lines of the failed step in a report
SESSION_LOG_BYTES = 2 * 2 ** 20


def issues_url():
    v = os.environ.get("PHOTOMESH_ISSUES_URL")
    if v is not None:
        return None if v.strip().lower() in ("", "0", "off", "none", "disabled") else v.strip()
    return ISSUES_URL


def report_dir():
    d = os.environ.get("PHOTOMESH_REPORT_DIR")
    if d:
        return d
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "PhotoMeshStudio", "reports")
    return os.path.join(os.path.expanduser("~/.local/state"), "PhotoMeshStudio", "reports")


def display_dir(d=None):
    """Report folder as shown to the user / in an issue (anonymised)."""
    return Anonymizer().text(d or report_dir())


_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def pct(s):
    """RFC 3986 percent-encoding of UTF-8 text (no network module needed)."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in str(s).encode("utf-8"))


def query(params):
    return "&".join(f"{pct(k)}={pct(v)}" for k, v in params.items())


# ---------------------------------------------------------------- exit codes
# NTSTATUS / CRT codes a crashing Windows program returns (as unsigned 32 bit)
WINDOWS_CODES = {
    0x00000001: ("ERROR", "generic failure; see the log lines above"),
    0x00000003: ("ABORT", "abort() called (CRT), often a failed assertion or out of memory"),
    0x80000003: ("STATUS_BREAKPOINT", "breakpoint / debug assertion"),
    0xC0000005: ("STATUS_ACCESS_VIOLATION", "access violation (crash in native code, a driver or a bad input file)"),
    0xC0000008: ("STATUS_INVALID_HANDLE", "invalid handle"),
    0xC0000017: ("STATUS_NO_MEMORY", "out of memory"),
    0xC000001D: ("STATUS_ILLEGAL_INSTRUCTION", "illegal instruction (CPU lacks an instruction set, e.g. AVX)"),
    0xC0000094: ("STATUS_INTEGER_DIVIDE_BY_ZERO", "integer division by zero"),
    0xC0000096: ("STATUS_PRIVILEGED_INSTRUCTION", "privileged instruction"),
    0xC000009A: ("STATUS_INSUFFICIENT_RESOURCES", "insufficient system resources (memory, handles)"),
    0xC00000FD: ("STATUS_STACK_OVERFLOW", "stack overflow"),
    0xC0000135: ("STATUS_DLL_NOT_FOUND", "a required DLL was not found (missing runtime or CUDA DLL)"),
    0xC0000139: ("STATUS_ENTRYPOINT_NOT_FOUND", "DLL entry point not found (wrong DLL version)"),
    0xC0000142: ("STATUS_DLL_INIT_FAILED", "DLL initialisation failed"),
    0xC000013A: ("STATUS_CONTROL_C_EXIT", "terminated (Ctrl+C / console closed)"),
    0xC0000374: ("STATUS_HEAP_CORRUPTION", "heap corruption"),
    0xC0000409: ("STATUS_STACK_BUFFER_OVERRUN", "fail-fast / abort; with COLMAP usually out of memory (std::bad_alloc)"),
    0xC000041D: ("STATUS_FATAL_USER_CALLBACK_EXCEPTION", "fatal exception in a callback"),
    0xC0000420: ("STATUS_ASSERTION_FAILURE", "assertion failure"),
    0xE06D7363: ("CPP_EXCEPTION", "unhandled C++ exception (often std::bad_alloc = out of memory)"),
    0x40010004: ("DBG_TERMINATE_PROCESS", "process was terminated (killed)"),
    0xFFFFFFFF: ("-1", "process ended with -1 (killed or failed to start)"),
}
POSIX_SIGNALS = {6: "SIGABRT (abort)", 9: "SIGKILL (killed, e.g. by the out-of-memory killer)",
                 11: "SIGSEGV (segmentation fault)", 15: "SIGTERM (terminated)", 4: "SIGILL (illegal instruction)"}


def exit_code_info(code, crashed=False):
    """{'code', 'hex', 'name', 'meaning', 'text'} for a process exit code."""
    if code is None:
        return {"code": None, "hex": "", "name": "", "meaning": "", "text": "none"}
    try:
        c = int(code)
    except (TypeError, ValueError):
        return {"code": code, "hex": "", "name": "", "meaning": "", "text": str(code)}
    u = c & 0xFFFFFFFF
    hx = f"0x{u:08X}"
    name, meaning = WINDOWS_CODES.get(u, ("", ""))
    if not name and c < 0 and -c in POSIX_SIGNALS:
        name, meaning = f"signal {-c}", POSIX_SIGNALS[-c]
    if not meaning and crashed:
        meaning = "the process crashed"
    if not meaning and (u & 0xC0000000) == 0xC0000000:
        meaning = "Windows error status (NTSTATUS)"
    text = f"{c} ({hx}" + (f" {name}" if name and name not in ("ERROR", "-1") else "") + ")"
    if meaning:
        text += f" - {meaning}"
    return {"code": c, "hex": hx, "name": name, "meaning": meaning, "text": text}


# ---------------------------------------------------------------- anonymisation
_SEP = r"[\\/]+"
_WIN_USERS = re.compile(r"(?i)\b[a-z]:" + _SEP + r"(?:users|documents and settings)" + _SEP + r"[^\\/\s\"'<>|:*?]+")
_POSIX_HOME = re.compile(r"(?<![\w.])/(?:home|Users)/[^/\s\"']+")
_WIN_ABS = re.compile(r"(?i)\b([a-z]:)[\\/]+((?:[^\\/:*?\"<>|\r\n]+[\\/]+)+)")
_POSIX_ABS = re.compile(r"(?<![\w.:/~%>\]-])/(?:[^/\s\"'<>|:]+/)+")
_UNC = re.compile(r"\\\\[^\\/\s]+\\[^\\/\s]+")
# coordinates: 2-3 numbers with >= 3 decimals, numbers >= 100000 with decimals
# (UTM / ECEF), and labelled lat/lon/alt/easting/northing values
_COORD_SEQ = re.compile(r"(?<![\w.])-?\d+\.\d{3,}(?:(?:\s*[,;]\s*|\s+)-?\d+\.\d{3,}){1,2}(?![\d.])")
_BIG = re.compile(r"(?<![\w.])-?\d{6,}\.\d+(?![\d.])")
_LABELLED = re.compile(r"(?i)\b(lat|lon|lng|latitude|longitude|alt|altitude|easting|northing|elevation)"
                       r"(\s*[=:]\s*|\s+)(-?\d+(?:\.\d+)?)")
COORD_KEYS = {"lat", "lon", "lng", "alt", "latitude", "longitude", "altitude", "gps", "est", "rot", "ref",
              "offset", "origin", "center", "centre", "bounds", "bbox", "region", "transform", "similarity",
              "matrix", "positions", "projections", "xyz", "coords", "coordinates", "geotransform", "extent",
              "up", "position", "location", "markers", "points", "wkt", "crs_wkt", "proj4"}


class Anonymizer:
    """Removes personal data from text: user home, known folders, user and
    computer name, absolute paths (drive + file name are kept), coordinates."""

    def __init__(self, roots=None, coords=True):
        r = {}
        home = os.path.expanduser("~")
        for env, ph in (("LOCALAPPDATA", "%LOCALAPPDATA%"), ("APPDATA", "%APPDATA%"), ("TEMP", "%TEMP%"),
                        ("TMP", "%TEMP%"), ("USERPROFILE", "%USERPROFILE%")):
            v = os.environ.get(env)
            if v and len(v) > 3:
                r[v] = ph
        if home and len(home) > 3:
            r.setdefault(home, "%USERPROFILE%")
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r[app] = "<app>"
        for k, v in (roots or {}).items():
            if k and len(str(k)) > 3:
                r[str(k)] = v
        self.roots = []
        for path, ph in sorted(r.items(), key=lambda kv: -len(kv[0])):
            parts = [re.escape(p) for p in re.split(r"[\\/]+", path.rstrip("\\/")) if p != ""]
            lead = _SEP if path[:1] in "\\/" else ""
            pat = lead + _SEP.join(parts) + r"(?![^\\/\s\"'<>|:*?,;)\]])"
            self.roots.append((re.compile(pat, re.I), ph))
        names = set()
        for n in (_safe(getpass.getuser), _safe(platform.node), os.environ.get("USERNAME"),
                  os.environ.get("COMPUTERNAME")):
            if n and len(n) >= 3 and n.lower() not in ("box", "root", "user", "admin", "localhost"):
                names.add(n)
        self.names = [re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])")
                      for n in sorted(names, key=len, reverse=True)]
        self.coords = coords

    def text(self, s):
        if s is None:
            return ""
        s = str(s)
        for rx, ph in self.roots:
            s = rx.sub(lambda m, ph=ph: ph, s)
        s = _WIN_USERS.sub("%USERPROFILE%", s)
        s = _POSIX_HOME.sub("~", s)
        s = _UNC.sub(r"\\\\<server>\\<share>", s)
        s = _WIN_ABS.sub(lambda m: m.group(1) + "\\...\\", s)
        s = _POSIX_ABS.sub("/.../", s)
        for rx in self.names:
            s = rx.sub("<user>", s)
        if self.coords:
            s = scrub_coordinates(s)
        return s

    def lines(self, lines):
        return [self.text(l) for l in lines]

    def params(self, obj, depth=0):
        """Settings dict -> JSON-safe, anonymised, without coordinate values."""
        if depth > 6:
            return "..."
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                ks = str(k)
                if ks.lower() in COORD_KEYS:
                    out[ks] = _summary(v)
                else:
                    out[ks] = self.params(v, depth + 1)
            return out
        if isinstance(obj, (list, tuple)):
            if len(obj) > 20:
                return f"<{len(obj)} items>"
            return [self.params(v, depth + 1) for v in obj]
        if isinstance(obj, bool) or obj is None or isinstance(obj, int):
            return obj
        if isinstance(obj, float):
            return obj if abs(obj) < 1e5 else "<hidden>"
        return self.text(obj)


def _summary(v):
    if v is None or v == "" or v == [] or v == {}:
        return v
    if isinstance(v, (list, tuple, dict)):
        return f"<{len(v)} items hidden>"
    return "<hidden>"


def scrub_coordinates(s):
    s = _LABELLED.sub(lambda m: m.group(1) + m.group(2) + "<hidden>", s)
    s = _COORD_SEQ.sub("<coords>", s)
    return _BIG.sub("<coord>", s)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def crs_name(crs):
    """'EPSG:32638', the name of a WKT CRS, or a short label - never coordinates."""
    if not crs:
        return "none (local coordinates)"
    s = str(crs).strip()
    m = re.search(r'(?i)\bEPSG\D{0,3}(\d{4,5})', s) if not s.upper().startswith(("PROJCS", "GEOGCS", "PROJCRS", "GEOGCRS", "COMPD")) else None
    if m and len(s) < 40:
        return f"EPSG:{m.group(1)}"
    m = re.match(r'\s*[A-Z_0-9]+\[\s*"([^"]+)"', s)
    if m:
        code = re.findall(r'(?:AUTHORITY|ID)\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]\s*\]?\s*$', s)
        return m.group(1) + (f" (EPSG:{code[-1]})" if code else "")
    return scrub_coordinates(s[:60])


# ---------------------------------------------------------------- system info
_CACHE = {}


def _run(cmd, timeout=10):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        return (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _cpu_name():
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _ram():
    """(total GB, free GB)"""
    if os.name == "nt":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                            ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
            m = MS(); m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.total / 2 ** 30, 1), round(m.avail / 2 ** 30, 1)
        except Exception:
            return None, None
    try:
        vals = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) * 1024
        return round(vals["MemTotal"] / 2 ** 30, 1), round(vals.get("MemAvailable", 0) / 2 ** 30, 1)
    except Exception:
        return None, None


def _nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        c = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = c if os.path.exists(c) else None
    return exe


def gpu_info():
    exe = _nvidia_smi()
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"], 8)
    gpus = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 4 and p[1].replace(".", "").isdigit():
            gpus.append({"name": p[0], "vram_total_mb": int(float(p[1])), "vram_free_mb": int(float(p[2])),
                         "driver": p[3]})
    return gpus


def colmap_version(exe=None):
    if "colmap" in _CACHE:
        return _CACHE["colmap"]
    v = "not found"
    try:
        from . import colmap as cm
        exe = exe or cm.find_colmap()
        if exe:
            prog, args = cm.resolve_cmd(exe, ["help"])
            out = _run([prog] + list(args), 20)
            m = re.search(r"COLMAP\s+[\d.]+[^\r\n]*", out)
            v = m.group(0).strip() if m else "found (version unknown)"
            v += ", CUDA" if cm.has_cuda(exe) else ", no CUDA"
    except Exception as e:  # noqa: BLE001
        v = f"unknown ({type(e).__name__})"
    _CACHE["colmap"] = v
    return v


def glomap_status():
    try:
        from . import colmap as cm
        return "found" if cm.find_glomap() else "not found"
    except Exception:
        return "unknown"


PACKAGES = ("numpy", "scipy", "open3d", "PySide6", "pyqtgraph", "PyOpenGL", "Pillow", "opencv-python",
            "opencv-python-headless", "trimesh", "pyproj", "rasterio", "tifffile", "psutil", "numba")


def package_versions():
    from importlib import metadata
    out = {}
    for p in PACKAGES:
        try:
            out[p] = metadata.version(p)
        except Exception:
            pass
    return out


def windows_version():
    if os.name != "nt":
        return platform.platform()
    rel, ver, _sp, _t = platform.win32_ver()
    ed = _safe(platform.win32_edition, "") or ""
    build = ver.split(".")[-1] if ver else ""
    name = f"Windows {'11' if build.isdigit() and int(build) >= 22000 else rel}"
    return f"{name} {ed} (build {ver}, {platform.machine()})".replace("  ", " ")


def disk_free(path):
    try:
        p = path if path and os.path.exists(path) else os.getcwd()
        u = shutil.disk_usage(p)
        drive = os.path.splitdrive(os.path.abspath(p))[0] or "/"
        return {"drive": drive, "free_gb": round(u.free / 2 ** 30, 1), "total_gb": round(u.total / 2 ** 30, 1)}
    except Exception:
        return {}


def system_info(project_dir=None):
    if "static" not in _CACHE:
        _CACHE["static"] = {"os": windows_version(), "cpu": _cpu_name(), "threads": os.cpu_count(),
                            "python": f"{platform.python_version()} ({platform.architecture()[0]})",
                            "packages": package_versions()}
    info = dict(_CACHE["static"])
    t, f = _ram()
    info["ram_total_gb"], info["ram_free_gb"] = t, f
    info["gpus"] = gpu_info()
    info["disk"] = disk_free(project_dir)
    info["colmap"] = colmap_version()
    info["glomap"] = glomap_status()
    return info


# ---------------------------------------------------------------- app / project
def app_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_commit(root=None):
    """Short commit (and branch) of a git checkout, read from .git without git."""
    g = os.path.join(root or app_root(), ".git")
    try:
        if os.path.isfile(g):
            with open(g, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt.startswith("gitdir:"):
                g = os.path.join(os.path.dirname(g), txt.split(":", 1)[1].strip())
        if not os.path.isdir(g):
            return ""
        with open(os.path.join(g, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head.split(":", 1)[1].strip()
        p = os.path.join(g, *ref.split("/"))
        sha = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                sha = f.read().strip()
        elif os.path.exists(os.path.join(g, "packed-refs")):
            with open(os.path.join(g, "packed-refs"), encoding="utf-8") as f:
                for line in f:
                    if line.strip().endswith(" " + ref):
                        sha = line.split()[0]
        return (sha[:7] + " on " + ref.rsplit("/", 1)[-1]) if sha else ""
    except OSError:
        return ""


def project_stats(project):
    if project is None:
        return {}
    try:
        ch = project.chunk
        cams = list(getattr(ch, "cameras", []) or [])
        sizes = {}
        for c in cams:
            if getattr(c, "width", 0):
                k = f"{c.width}x{c.height}"
                sizes[k] = sizes.get(k, 0) + 1
        steps = {}
        for step, m in (getattr(ch, "meta", None) or {}).items():
            if isinstance(m, dict):
                p = m.get("params") or {}
                q = {k: p[k] for k in ("quality", "accuracy", "mode", "face_count", "source", "filtering",
                                       "depth_filtering", "key_point_limit", "tie_point_limit") if k in p}
                steps[step] = dict(q, seconds=m.get("seconds"))
        return {
            "saved": bool(getattr(project, "path", "")),
            "chunks": len(project.chunks), "photos": len(cams),
            "enabled": sum(1 for c in cams if getattr(c, "enabled", True)),
            "aligned": sum(1 for c in cams if getattr(c, "aligned", False)),
            "with_gps": sum(1 for c in cams if getattr(c, "lat", None) is not None),
            "photo_sizes": dict(sorted(sizes.items(), key=lambda kv: -kv[1])[:3]),
            "markers": len(getattr(ch, "markers", []) or []),
            "tie_points": getattr(ch, "tie_point_count", 0), "dense_points": getattr(ch, "point_count", 0),
            "mesh_faces": getattr(ch, "face_count", 0),
            "texture": bool(getattr(ch, "texture", None)), "tiled_model": bool(getattr(ch, "tiled_model", None)),
            "orthomosaic": bool(getattr(ch, "orthomosaic", None)), "dem": bool(getattr(ch, "dem", None)),
            "shapes": (getattr(ch, "shapes", None) or {}).get("count", 0),
            "crs": crs_name(getattr(ch, "crs", "")), "georeferenced": bool((getattr(ch, "georef", None) or {})),
            "steps": steps,
        }
    except Exception as e:  # noqa: BLE001 - a report must never fail
        return {"error": f"{type(e).__name__}: {e}"}


def project_roots(project):
    """Folders to replace by placeholders: project folder, working folders, photo folders."""
    r = {}
    if project is None:
        return r
    try:
        if getattr(project, "path", ""):
            r[os.path.dirname(os.path.abspath(project.path))] = "<project>"
        dirs = {}
        for ch in project.chunks:
            if getattr(ch, "workdir", ""):
                r[os.path.abspath(ch.workdir)] = "<workdir>"
            for c in ch.cameras:
                d = os.path.dirname(c.path)
                dirs[d] = dirs.get(d, 0) + 1
        for k, (d, _n) in enumerate(sorted(dirs.items(), key=lambda kv: -kv[1])[:20]):
            r[d] = "<photos>" if k == 0 else f"<photos{k + 1}>"
    except Exception:
        pass
    return r


# ---------------------------------------------------------------- report
_TB_START = re.compile(r"^\s*(?:\S+\s+)?Traceback \(most recent call last\):")


def extract_traceback(lines):
    """Last Python traceback in a list of log lines (child process output)."""
    lines = list(lines or [])
    for i in range(len(lines) - 1, -1, -1):
        if _TB_START.match(lines[i]):
            out = [lines[i]]
            for l in lines[i + 1:]:
                out.append(l)
                if l and not l.startswith((" ", "\t")) and not _TB_START.match(l) and len(out) > 1:
                    break
            return "\n".join(out)
    return ""


_EXC_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?(RuntimeError|Cancelled|[A-Z]\w*(?:Error|Exception)): (.+)$")


def error_reason(lines):
    """The message of the exception that ended a Python task (the last line of
    its traceback, e.g. 'RuntimeError: Orthomosaic would be ...'), else ''."""
    tail = [l.strip() for l in (lines or []) if l and l.strip()][-3:]
    for line in reversed(tail):
        m = _EXC_LINE.match(line)
        if m:
            why = m.group(2).strip()
            return why if m.group(1) in ("RuntimeError", "Cancelled") else f"{m.group(1)}: {why}"
    return ""


def task_config(command):
    """The JSON config of a `python -m photomesh.tasks <cmd> <cfg.json>` step."""
    try:
        for a in reversed(list(command or [])):
            if isinstance(a, str) and a.lower().endswith(".json") and os.path.exists(a):
                with open(a, encoding="utf-8-sig") as f:
                    return json.load(f)
    except Exception:
        pass
    return None


def new_id(now=None, directory=None):
    now = now or datetime.datetime.now()
    rid = f"report_{now:%Y%m%d_%H%M%S}"
    d = directory or report_dir()
    k, out = 1, rid
    while os.path.exists(os.path.join(d, out + ".md")) or os.path.exists(os.path.join(d, out + ".zip")):
        k += 1; out = f"{rid}_{k}"
    return out


def build_report(title, message="", *, kind="error", step="", params=None, command=None, exit_code=None,
                 crashed=False, traceback_text="", log_lines=(), project=None, description="", now=None,
                 system=None, directory=None):
    """Everything in the result is already anonymised."""
    now = now or datetime.datetime.now().astimezone()
    an = Anonymizer(project_roots(project))
    log_lines = [l for l in (log_lines or []) if l is not None][-LOG_LINES:]
    if not traceback_text:
        traceback_text = extract_traceback(log_lines)
    cfg = task_config(command)
    proj_dir = os.path.dirname(project.path) if project is not None and getattr(project, "path", "") else None
    sysinfo = system if system is not None else _safe(lambda: system_info(proj_dir), {})
    commit = git_commit()
    p = {}
    if params:
        p["settings"] = an.params(params)
    if cfg is not None:
        p["task_config"] = an.params(cfg)
    r = {
        "id": new_id(now.replace(tzinfo=None) if now.tzinfo else now, directory),
        "kind": kind,
        "time": now.strftime("%Y-%m-%d %H:%M:%S") + (f" (UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})"
                                                       if now.tzinfo else ""),
        "app": f"{APP_NAME} {APP_VERSION}", "version": APP_VERSION, "commit": commit or "not a git checkout",
        "title": an.text(title), "message": an.text(message), "step": an.text(step),
        "params": p, "command": an.text(" ".join(str(a) for a in command)) if command else "",
        "exit": exit_code_info(exit_code, crashed) if (exit_code is not None or crashed) else None,
        "traceback": an.text(traceback_text).rstrip(),
        "log": an.lines(log_lines),
        "project": project_stats(project), "system": _anon_sys(an, sysinfo or {}),
        "description": an.text(description or ""),
        "platform": sys.platform,
    }
    return r


def _anon_sys(an, s):
    out = json.loads(json.dumps(s, default=str))
    for k in ("os", "cpu", "colmap"):
        if k in out:
            out[k] = an.text(out[k])
    return out


def issue_title(r):
    base = r["title"] or "Error"
    msg = r["message"].splitlines()[0] if r["message"] else ""
    t = f"[{r['version']}] {base}"
    why = msg.split(" failed: ", 1)[1] if " failed: " in msg else ""
    if why:                                    # the task's own message beats "exit 0x1 (generic failure)"
        t += f": {why}"
    elif r.get("exit") and r["exit"].get("hex"):
        t += f": exit {r['exit']['hex']}" + (f" ({r['exit']['meaning'].split(';')[0].split(',')[0]})"
                                              if r["exit"]["meaning"] else "")
    elif msg and msg != base:
        t += f": {msg}"
    return t[:150]


def _fence(text):
    return "```text\n" + str(text).replace("```", "'''") + "\n```"


def _kv_table(rows):
    out = ["| | |", "|---|---|"]
    for k, v in rows:
        if v not in (None, ""):
            out.append(f"| {k} | {str(v).replace('|', '/').replace(chr(10), ' ')} |")
    return "\n".join(out)


def _system_md(s):
    gp = s.get("gpus") or []
    gpu = "; ".join(f"{g['name']} ({g['vram_total_mb']} MB VRAM, {g['vram_free_mb']} MB free, driver {g['driver']})"
                    for g in gp) or "no NVIDIA GPU / nvidia-smi not available"
    d = s.get("disk") or {}
    pk = ", ".join(f"{k} {v}" for k, v in (s.get("packages") or {}).items())
    return _kv_table([
        ("OS", s.get("os")), ("CPU", s.get("cpu")), ("Threads", s.get("threads")),
        ("RAM", f"{s.get('ram_total_gb')} GB total, {s.get('ram_free_gb')} GB free" if s.get("ram_total_gb") else ""),
        ("GPU", gpu),
        ("Disk", f"{d.get('drive')} {d.get('free_gb')} GB free of {d.get('total_gb')} GB" if d else ""),
        ("Python", s.get("python")), ("COLMAP", s.get("colmap")), ("GLOMAP", s.get("glomap")),
        ("Packages", pk)])


def _project_md(p):
    if not p:
        return "no project"
    if "error" in p:
        return p["error"]
    rows = [("Photos", f"{p['photos']} ({p['enabled']} enabled, {p['aligned']} aligned, {p['with_gps']} with GPS)"),
            ("Photo sizes", ", ".join(f"{k} x{v}" for k, v in p["photo_sizes"].items())),
            ("Chunks", p["chunks"]), ("Markers", p["markers"]),
            ("Tie points", f"{p['tie_points']:,}"), ("Dense points", f"{p['dense_points']:,}"),
            ("Mesh faces", f"{p['mesh_faces']:,}"),
            ("Products", ", ".join(k for k in ("texture", "tiled_model", "orthomosaic", "dem") if p.get(k)) or "-"),
            ("Shapes", p["shapes"]), ("CRS", p["crs"] + (" (georeferenced)" if p["georeferenced"] else "")),
            ("Project saved", "yes" if p["saved"] else "no")]
    for step, q in (p.get("steps") or {}).items():
        rows.append((f"Last {step}", ", ".join(f"{k}={v}" for k, v in q.items())))
    return _kv_table(rows)


def render_markdown(r, log_lines=None, tb_chars=None, note=""):
    """log_lines / tb_chars limit the log tail and traceback (for the issue URL)."""
    e = r.get("exit")
    head = [f"# {APP_NAME} error report" if r["kind"] != "manual" else f"# {APP_NAME} problem report"]
    if note:
        head.append("> " + note)
    head.append(_kv_table([
        ("Report", r["id"]), ("Time", r["time"]), ("App", f"{r['app']} (commit {r['commit']})"),
        ("Kind", r["kind"]), ("Step", r["step"] or r["title"]), ("Error", r["message"]),
        ("Exit code", e["text"] if e else ""), ("Platform", r["platform"])]))
    parts = ["\n\n".join(head)]
    parts.append("## What happened\n" + (r["description"] or "_(describe what you were doing, optional)_"))
    if r["params"]:
        parts.append("## Step settings\n```json\n" + json.dumps(r["params"], indent=1, default=str)
                     .replace("```", "'''") + "\n```")
    if r["command"]:
        parts.append("## Command\n" + _fence(r["command"]))
    tb = r["traceback"]
    if tb:
        if tb_chars is not None and len(tb) > tb_chars:
            tb = "...\n" + tb[-tb_chars:]
        parts.append("## Traceback\n" + _fence(tb))
    parts.append("## Project\n" + _project_md(r["project"]))
    parts.append("## System\n" + _system_md(r["system"]))
    log = r["log"] if log_lines is None else (r["log"][-log_lines:] if log_lines > 0 else [])
    if log:
        parts.append(f"## Log (last {len(log)} of {len(r['log'])} lines)\n" + _fence("\n".join(log)))
    elif r["log"]:
        parts.append(f"## Log\n_{len(r['log'])} lines - see the attached .zip_")
    return "\n\n".join(parts) + "\n"


def issue_url(r, base=None, max_len=MAX_URL, zip_name=None):
    """GitHub new-issue URL with title, body and labels, at most max_len chars.
    The body is shortened (log, then traceback, then cut) if needed, with a
    note asking to drag the report .zip into the issue."""
    base = base or issues_url()
    if not base:
        return None
    title = issue_title(r)
    zip_name = zip_name or (r["id"] + ".zip")
    where = display_dir()

    def url(body):
        q = query({"title": title, "body": body, "labels": ISSUE_LABELS})
        return base + ("&" if "?" in base else "?") + q

    full = render_markdown(r)
    u = url(full + f"\n_Full report and logs: {zip_name} (in {where})._\n")
    if len(u) <= max_len:
        return u
    note = (f"Shortened to fit in the link - please drag **{zip_name}** (in `{where}`) into this issue "
            f"to attach the full report and logs.")
    lo, hi = 0, len(r["log"])
    best = None
    while lo <= hi:                         # most log lines that still fit
        mid = (lo + hi) // 2
        cand = url(render_markdown(r, log_lines=mid, note=note))
        if len(cand) <= max_len:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    if best:
        return best
    for tb in (3000, 1500, 600, 200):
        cand = url(render_markdown(r, log_lines=0, tb_chars=tb, note=note))
        if len(cand) <= max_len:
            return cand
    body = render_markdown(r, log_lines=0, tb_chars=200, note=note)
    lo, hi = 0, len(body)
    while lo < hi:                          # hard cut, whole characters only
        mid = (lo + hi + 1) // 2
        if len(url(body[:mid] + "\n...")) <= max_len:
            lo = mid
        else:
            hi = mid - 1
    return url(body[:lo] + "\n...")


def save_report(r, directory=None, session_log=None):
    """Write <id>.md and <id>.zip (report.md, log.txt, session.log, system.json).
    Returns (md_path, zip_path)."""
    d = directory or report_dir()
    os.makedirs(d, exist_ok=True)
    md = render_markdown(r)
    mdp = os.path.join(d, r["id"] + ".md")
    with open(mdp, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    zp = os.path.join(d, r["id"] + ".zip")
    an = None
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.md", md)
        z.writestr("step_log.txt", "\n".join(r["log"]) + "\n")
        z.writestr("report.json", json.dumps(r, indent=1, default=str))
        if session_log and os.path.exists(session_log):
            an = Anonymizer()
            z.writestr("session_log.txt", an.text(read_tail(session_log, SESSION_LOG_BYTES)))
    return mdp, zp


def read_tail(path, nbytes=SESSION_LOG_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
        txt = data.decode("utf-8", "replace")
        if size > nbytes:
            txt = txt.split("\n", 1)[-1]
        return txt
    except OSError:
        return ""


def tail_lines(path, n=LOG_LINES):
    return read_tail(path, 256 * 1024).splitlines()[-n:]
