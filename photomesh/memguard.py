"""RAM-aware parallelism: cap worker counts by the memory that is actually
available, and measure the peak memory of a processing step.

    n = cap_workers(16, per_worker_bytes=undistort_worker_bytes(...))

psutil is used when present (it is in the app's environment); without it the
caps fall back to the CPU-based limits."""
from __future__ import annotations

import os
import sys
import threading
import time

GB = 1024 ** 3
MB = 1024 ** 2
MAX_WORKERS = 16          # hard thread/process limit (unchanged)
SAFETY = 1.5              # estimates x this; the rest of the system keeps a reserve
RESERVE = 2.0 * GB        # never plan to use the last 2 GB (GUI, OS, file cache)


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:  # pragma: no cover - psutil is a dependency
        return None


def available_bytes():
    """Physical memory available to new allocations (None if unknown)."""
    ps = _psutil()
    if ps is None:
        return None
    try:
        return int(ps.virtual_memory().available)
    except Exception:  # pragma: no cover
        return None


def total_bytes():
    ps = _psutil()
    try:
        return int(ps.virtual_memory().total) if ps else None
    except Exception:  # pragma: no cover
        return None


def cap_workers(want, per_worker_bytes, available=None, reserve=RESERVE, safety=SAFETY,
                hard_max=MAX_WORKERS, base_bytes=0):
    """Number of workers that fit: min(want, hard_max, (available - reserve -
    base) / (per_worker * safety)), at least 1. `available` defaults to the
    current free physical memory."""
    want = max(1, min(int(want), int(hard_max)))
    avail = available_bytes() if available is None else available
    if not avail or per_worker_bytes <= 0:
        return want
    room = avail - reserve - base_bytes
    n = int(room // (per_worker_bytes * safety)) if room > 0 else 1
    return max(1, min(want, n))


def undistort_worker_bytes(work_w, work_h, out_w, out_h, decoded_w=None, decoded_h=None):
    """Estimated peak memory of one undistortion worker process:
    decoded photo (PIL image + numpy copy, uint8 RGB), one contiguous channel,
    the uint8 output and its JPEG encoder buffer, coordinate blocks of the
    shared float32 map (memory-mapped, counted once per block), and the
    interpreter with numpy/scipy/PIL (~120 MB)."""
    dw, dh = decoded_w or work_w, decoded_h or work_h
    dec = dw * dh * 3
    work = work_w * work_h * 3
    out = out_w * out_h * 3
    block = 2 * 4 * min(out_h, UNDISTORT_BLOCK_ROWS) * out_w * 2      # float32 rows, cols + scipy copy
    return int(2 * dec + (work if (dw, dh) != (work_w, work_h) else 0) + work_w * work_h
               + 2 * out + block + 120 * MB)


UNDISTORT_BLOCK_ROWS = 256


def process_peak_bytes(pid=None):
    """Peak resident memory of a process (Windows peak working set, Linux
    ru_maxrss for the current process / current RSS for others)."""
    ps = _psutil()
    try:
        if ps is not None:
            mi = ps.Process(pid or os.getpid()).memory_info()
            for k in ("peak_wset", "peak_rss"):
                if hasattr(mi, k):
                    return int(getattr(mi, k))
            if pid in (None, os.getpid()) and sys.platform != "win32":
                import resource
                r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                return int(r * (1 if sys.platform == "darwin" else 1024))
            return int(mi.rss)
    except Exception:
        pass
    return 0


class PeakMonitor:
    """Samples, every `interval` s, the RSS of this process + its children
    and the system's available memory; .stop() returns the peaks."""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.peak_tree = 0; self.min_available = None; self.peak_children = 0
        self._stop = threading.Event()
        self._t = None

    def _sample(self):
        ps = _psutil()
        if ps is None:
            return
        try:
            me = ps.Process()
            kids = me.children(recursive=True)
            ch = 0
            for k in kids:
                try:
                    ch += k.memory_info().rss
                except Exception:
                    pass
            tot = me.memory_info().rss + ch
            self.peak_tree = max(self.peak_tree, tot); self.peak_children = max(self.peak_children, ch)
            av = ps.virtual_memory().available
            self.min_available = av if self.min_available is None else min(self.min_available, av)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)

    def start(self):
        self._sample()
        self._t = threading.Thread(target=self._run, name="peak-monitor", daemon=True); self._t.start()
        return self

    def stop(self):
        self._stop.set()
        if self._t is not None:
            self._t.join(2)
        self._sample()
        return {"peak_tree": self.peak_tree, "peak_children": self.peak_children,
                "min_available": self.min_available}


def fmt_gb(b):
    return "?" if b is None else f"{b / GB:.2f} GB"
