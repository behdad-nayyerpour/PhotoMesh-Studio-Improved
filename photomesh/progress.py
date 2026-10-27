"""Progress meters for long COLMAP steps: smooth fraction, pass and ETA parsed
from the tool's console output (PipelineRunner feeds every line).

PatchMatchMeter  patch_match_stereo prints "Processing view i / N for <name>"
                 once per computed view and pass: with geometric consistency
                 first a photometric pass over all views, then a geometric one.
                 Views whose maps already exist are skipped silently, so the
                 expected counts are passed in (see dense.patch_match_meter).
FusionMeter      stereo_fusion prints "Reading workspace...", then
                 "Fusing image [k/N] with index i ... in 1.2s (123456 points)".
"""
from __future__ import annotations

import re
import time

_VIEW = re.compile(r"Processing view\s+(\d+)\s*/\s*(\d+)")
_FUSE = re.compile(r"Fusing image \[(\d+)/(\d+)\]")
_POINTS = re.compile(r"\((\d+) points\)")


def fmt_eta(seconds):
    if seconds is None or seconds != seconds or seconds < 0:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


class _Meter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.t0 = None          # time of the first counted item
        self.n0 = 0

    def _eta(self, done, total):
        """Remaining time from the rate since the first counted item."""
        now = self.clock()
        if self.t0 is None:
            self.t0, self.n0 = now, done
            return None
        k = done - self.n0
        if k <= 0 or now <= self.t0:
            return None
        return (now - self.t0) / k * max(0, total - done)


class PatchMatchMeter(_Meter):
    def __init__(self, todo_photometric, todo_geometric, views=None, clock=time.monotonic):
        super().__init__(clock)
        self.todo_p = max(0, int(todo_photometric))
        self.todo_g = max(0, int(todo_geometric))
        self.views = views
        self.count = 0

    @property
    def total(self):
        return max(1, self.todo_p + self.todo_g)

    def feed(self, line):
        m = _VIEW.search(line)
        if not m:
            return None
        self.count += 1
        if self.views is None:
            self.views = int(m.group(2))
        # the view being processed now counts as half done
        frac = min(1.0, (self.count - 0.5) / self.total)
        if self.count <= self.todo_p:
            what = f"photometric pass {self.count}/{self.todo_p}"
        else:
            what = f"geometric pass {self.count - self.todo_p}/{self.todo_g}"
        eta = self._eta(self.count - 1, self.total)
        return frac, f"Depth maps: {what}" + (f" - about {fmt_eta(eta)} left" if eta else "")


class FusionMeter(_Meter):
    def __init__(self, images=None, clock=time.monotonic):
        super().__init__(clock)
        self.images = images
        self.points = 0

    def feed(self, line):
        p = _POINTS.search(line)
        if p:
            self.points = int(p.group(1))
        if "Reading workspace" in line:
            return 0.0, "Fusion: loading depth maps"
        m = _FUSE.search(line)
        if not m:
            return None
        k, n = int(m.group(1)), int(m.group(2))
        self.images = n
        eta = self._eta(k - 1, n)
        txt = f"Fusion: image {k}/{n}"
        if self.points:
            txt += f", {self.points / 1e6:.1f} M points"
        return min(1.0, (k - 0.5) / max(1, n)), txt + (f" - about {fmt_eta(eta)} left" if eta else "")
