"""Status bar CPU / GPU load, sampled every ~1.5 s in a daemon thread (never
on the GUI thread) and delivered through a queued Qt signal.

* CPU: psutil.cpu_percent(None) (non-blocking, since the previous call) when
  psutil is installed, else the system CPU times (Windows GetSystemTimes via
  ctypes, Linux /proc/stat) - no extra package needed.
* GPU: NVIDIA NVML loaded with ctypes from the display driver (nvml.dll /
  libnvidia-ml.so.1, already installed with the driver): utilisation and
  memory of GPU 0. No NVIDIA driver -> the GPU field is hidden. nvidia-smi is
  never started. Everything is local / offline.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading

from PySide6.QtCore import QObject, Signal

INTERVAL = 1.5


# ---------------------------------------------------------------- CPU
class CpuSampler:
    def __init__(self):
        self.ps = None
        try:
            import psutil
            psutil.cpu_percent(None)                  # prime: the next call measures since now
            self.ps = psutil
        except Exception:  # noqa: BLE001
            self.ps = None
        self.prev = self._times()

    @staticmethod
    def _times():
        """(idle, total) cumulative CPU times, or None."""
        try:
            if sys.platform == "win32":
                ft = [ctypes.c_ulonglong() for _ in range(3)]
                if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(x) for x in ft)):
                    return None
                idle, kernel, user = (x.value for x in ft)       # kernel includes idle
                return idle, kernel + user
            with open("/proc/stat", encoding="ascii") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            return v[3] + (v[4] if len(v) > 4 else 0), sum(v[:8])
        except Exception:  # noqa: BLE001
            return None

    def percent(self):
        if self.ps is not None:
            try:
                return float(self.ps.cpu_percent(None))
            except Exception:  # noqa: BLE001
                self.ps = None
        cur = self._times()
        prev, self.prev = self.prev, cur
        if cur is None or prev is None:
            return None
        di, dt = cur[0] - prev[0], cur[1] - prev[1]
        return max(0.0, min(100.0, 100.0 * (1 - di / dt))) if dt > 0 else None


# ---------------------------------------------------------------- GPU (NVML)
class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Mem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def _nvml_candidates():
    if sys.platform == "win32":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [os.path.join(sysroot, "System32", "nvml.dll"),
                os.path.join(pf, "NVIDIA Corporation", "NVSMI", "nvml.dll"), "nvml.dll"]
    return ["libnvidia-ml.so.1", "libnvidia-ml.so"]


class GpuSampler:
    """GPU 0 via NVML; .available False when there is no NVIDIA driver."""

    def __init__(self, loader=None):
        self.lib = None; self.handle = None; self.name = ""
        try:
            self.lib = (loader or self._load)()
            if self.lib is None:
                return
            if self.lib.nvmlInit_v2() != 0:
                self.lib = None; return
            h = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                self.lib = None; return
            self.handle = h
            buf = ctypes.create_string_buffer(96)
            if self.lib.nvmlDeviceGetName(h, buf, 96) == 0:
                self.name = buf.value.decode(errors="replace")
        except Exception:  # noqa: BLE001
            self.lib = None

    @staticmethod
    def _load():
        for c in _nvml_candidates():
            if os.path.isabs(c) and not os.path.exists(c):
                continue
            try:
                return ctypes.CDLL(c)
            except OSError:
                continue
        return None

    @property
    def available(self):
        return self.lib is not None and self.handle is not None

    def sample(self):
        """(gpu %, used MB, total MB) or None."""
        if not self.available:
            return None
        try:
            u = _Util(); m = _Mem()
            if self.lib.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(u)) != 0:
                return None
            mem = (None, None)
            if self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(m)) == 0:
                mem = (m.used // (1024 * 1024), m.total // (1024 * 1024))
            return float(u.gpu), mem[0], mem[1]
        except Exception:  # noqa: BLE001
            return None

    def close(self):
        try:
            if self.lib is not None:
                self.lib.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass
        self.lib = None


# ---------------------------------------------------------------- monitor
class ResourceMonitor(QObject):
    """sampled(dict): {"cpu": % or None, "gpu": % or None, "gpu_mem": (used, total) MB, "gpu_name": str}"""
    sampled = Signal(object)

    def __init__(self, interval=INTERVAL, cpu=None, gpu=None, parent=None):
        super().__init__(parent)
        self.interval = interval
        self._cpu, self._gpu = cpu, gpu
        self._stop = threading.Event()
        self._thread = None
        self.last = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="resource-monitor", daemon=True)
            self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def _loop(self):
        cpu = self._cpu or CpuSampler()
        gpu = self._gpu if self._gpu is not None else GpuSampler()   # NVML init off the GUI thread
        try:
            while not self._stop.wait(self.interval):
                g = gpu.sample() if gpu.available else None
                self.last = {"cpu": cpu.percent(), "gpu": g[0] if g else None,
                             "gpu_mem": (g[1], g[2]) if g else None, "gpu_name": gpu.name if gpu.available else ""}
                try:
                    self.sampled.emit(self.last)
                except RuntimeError:                # receiver deleted at exit
                    return
        finally:
            if self._gpu is None:
                gpu.close()


def format_cpu(v):
    return f"CPU {v:3.0f}%" if v is not None else "CPU  --"


def format_gpu(v, mem=None):
    t = f"GPU {v:3.0f}%"
    if mem and mem[0] is not None and mem[1]:
        t += f"  {mem[0] / 1024:.1f}/{mem[1] / 1024:.0f} GB"
    return t
