"""Display styles of imported shapes (Qt-free).

A style is a dict of DEFAULTS keys. Layer styles live in the chunk's shapes
info (saved in the .pmsx): ch.shapes["styles"][layer name] holds the keys the
user changed, ch.shapes["shape_styles"][str(shape index)] an optional per-shape
override (also only changed keys). resolve() merges defaults < layer < shape
and fills the colours that default to the layer / file colour."""
from __future__ import annotations

import copy

SYMBOLS = ["ring", "circle", "square", "diamond", "triangle", "cross"]
LINE_STYLES = ["solid", "dashed"]
LABEL_FIELD_FILE = ""             # label from the file (KML name, CSV label, ...)
LABEL_FIELD_NAME = "(name)"       # shape name as in the Workspace

DEFAULTS = {
    "line_color": None,           # None: the shape's own colour from the file, else the layer colour
    "line_width": 2.0,
    "line_style": "solid",
    "point_size": 14.0,           # symbol diameter, screen pixels
    "point_symbol": "ring",
    "fill_color": None,           # None: the line colour
    "fill_opacity": 0.15,         # polygons, 0..1
    "label": True,
    "label_field": LABEL_FIELD_FILE,
    "label_size": 9.0,            # points
    "label_color": None,          # None: the line colour
    "label_bg": True,
    "label_bg_color": [20, 20, 24],
    "on_top": True,               # 3D: drawn over the model; False: depth-tested, hidden parts dashed
}

_RANGES = {"line_width": (0.5, 20.0), "point_size": (2.0, 60.0), "fill_opacity": (0.0, 1.0),
           "label_size": (5.0, 48.0)}


def _rgb(c):
    if c is None:
        return None
    c = list(c)[:3]
    if len(c) != 3:
        raise ValueError(f"colour needs 3 components: {c!r}")
    return [int(min(255, max(0, round(float(v))))) for v in c]


def clean(style):
    """Validated copy of a (partial) style: unknown keys dropped, numbers
    clamped, colours as [r, g, b] ints, enums checked."""
    out = {}
    for k, v in (style or {}).items():
        if k not in DEFAULTS:
            continue
        if k.endswith("_color") or k == "label_bg_color":
            out[k] = _rgb(v)
        elif k in _RANGES:
            lo, hi = _RANGES[k]
            out[k] = float(min(hi, max(lo, float(v))))
        elif k in ("label", "label_bg", "on_top"):
            out[k] = bool(v)
        elif k == "line_style":
            out[k] = v if v in LINE_STYLES else "solid"
        elif k == "point_symbol":
            out[k] = v if v in SYMBOLS else "ring"
        else:
            out[k] = "" if v is None else str(v)
    return out


def diff(style, base=None):
    """Only the keys of `style` that differ from `base` (DEFAULTS)."""
    base = DEFAULTS if base is None else base
    s = clean(style)
    return {k: v for k, v in s.items() if v != base.get(k)}


def layer_style(info, name):
    """Full (unresolved: colours may be None) style of a layer."""
    st = copy.deepcopy(DEFAULTS)
    st.update(clean(((info or {}).get("styles") or {}).get(name)))
    return st


def shape_override(info, index):
    return clean(((info or {}).get("shape_styles") or {}).get(str(int(index))))


def resolve(sset, index, info=None, highlight=False):
    """Final style of shape `index` (colours filled in)."""
    s = sset["shapes"][index]
    L = sset["layers"][s["layer"]]
    st = layer_style(info, L["name"])
    lay_raw = clean(((info or {}).get("styles") or {}).get(L["name"]))
    if "line_width" not in lay_raw and L.get("width") is not None:
        st["line_width"] = float(L["width"])          # width imported with the layer
    over = shape_override(info, index)
    st.update(over)
    lc = over.get("line_color") or lay_raw.get("line_color") or s.get("color") or L.get("color") or [255, 140, 0]
    st["line_color"] = _rgb(lc)
    st["fill_color"] = _rgb(st.get("fill_color") or st["line_color"])
    st["label_color"] = _rgb(st.get("label_color") or st["line_color"])
    if highlight:
        st["line_color"] = [255, 255, 255]
        st["line_width"] = st["line_width"] + 2.0
    return st


def label_text(s, index, field):
    """Label of a shape for a label field: '' file label, '(name)' shape name,
    else an attribute."""
    if field == LABEL_FIELD_NAME:
        from .shapes import shape_name
        return shape_name(s, index)
    if not field:
        return str(s.get("label", "") or "")
    v = (s.get("attrs") or {}).get(field)
    return "" if v is None else str(v)


def attribute_fields(sset, layer_index=None):
    keys = []
    for s in sset["shapes"]:
        if layer_index is None or s["layer"] == layer_index:
            for k in (s.get("attrs") or {}):
                if k not in keys:
                    keys.append(k)
    return keys


def set_layer_style(info, name, style):
    """Store a layer style in the shapes info (None / {} = defaults)."""
    info = dict(info or {})
    styles = dict(info.get("styles") or {})
    d = diff(style or {})
    if d:
        styles[name] = d
    else:
        styles.pop(name, None)
    info["styles"] = styles
    return info


def set_shape_style(info, index, style, layer_full=None):
    """Per-shape override: keys differing from the layer style (None removes it)."""
    info = dict(info or {})
    ss = dict(info.get("shape_styles") or {})
    d = diff(style or {}, layer_full) if style else {}
    if d:
        ss[str(int(index))] = d
    else:
        ss.pop(str(int(index)), None)
    info["shape_styles"] = ss
    return info


def remap_after_layer_removal(info, sset_before, li):
    """Shape indices shift when a layer is removed (shapes.remove_layer)."""
    info = dict(info or {})
    ss = info.get("shape_styles") or {}
    if not ss:
        return info
    new, k = {}, 0
    for i, s in enumerate(sset_before["shapes"]):
        if s["layer"] == li:
            continue
        if str(i) in ss:
            new[str(k)] = ss[str(i)]
        k += 1
    info["shape_styles"] = new
    name = sset_before["layers"][li]["name"]
    info["styles"] = {n: v for n, v in (info.get("styles") or {}).items() if n != name}
    return info


def dash_segments(P, dash, gap=None):
    """Polyline (n,3) -> dash segment pairs (2k,3) of length `dash` (world units)."""
    import numpy as np
    P = np.asarray(P, float)
    gap = dash * 0.8 if gap is None else gap
    if len(P) < 2 or dash <= 0:
        return np.zeros((0, P.shape[1] if P.ndim == 2 else 3))
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    cum = np.r_[0.0, np.cumsum(seg)]
    total = cum[-1]
    if total <= 0:
        return np.zeros((0, P.shape[1]))
    period = dash + gap
    n = int(min(200000, np.ceil(total / period)))
    s0 = np.arange(n) * period
    s1 = np.minimum(s0 + dash, total)

    def at(s):
        j = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
        f = (s - cum[j]) / np.where(seg[j] > 0, seg[j], 1.0)
        return P[j] + (P[j + 1] - P[j]) * f[:, None]
    A, B = at(s0), at(s1)
    out = np.empty((2 * n, P.shape[1]))
    out[0::2], out[1::2] = A, B
    return out
