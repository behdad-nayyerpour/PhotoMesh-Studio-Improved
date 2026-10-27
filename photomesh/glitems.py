"""OpenGL items for textured display (pyqtgraph's GLMeshItem has no texture
support): a textured triangle mesh and a streaming LOD tiled model.

Both use VBOs + one GLSL 1.10 program (works on desktop GL 2.1+ and GLES2):
mode 0 textured (unlit, like Metashape's Textured view), 1 solid (Lambert
with per-vertex normals), 2 elevation colour ramp; wireframe via
glPolygonMode."""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from OpenGL import GL
from PySide6 import QtGui
from PySide6.QtOpenGL import QOpenGLBuffer
from PySide6.QtCore import QTimer

from pyqtgraph.opengl.GLGraphicsItem import GLGraphicsItem

from .lighting import GLSL_LIGHT, normal_matrix

VERT = """
attribute vec3 a_position;
attribute vec2 a_uv;
attribute vec3 a_normal;
attribute vec3 a_color;
uniform mat4 u_mvp;
uniform mat3 u_nmat;
uniform vec2 u_zrange;
varying vec2 v_uv;
varying vec3 v_normal;
varying float v_h;
varying vec3 v_color;
void main() {
    v_uv = a_uv;
    v_color = a_color;
    v_normal = u_nmat * a_normal;               // eye-space normal (camera-relative light)
    v_h = (a_position.z - u_zrange.x) / max(u_zrange.y - u_zrange.x, 1e-6);
    gl_Position = u_mvp * vec4(a_position, 1.0);
}
"""
FRAG = """
#ifdef GL_ES
precision highp float;
#endif
uniform sampler2D u_tex;
uniform int u_mode;
uniform vec3 u_tint;
uniform float u_alpha;
varying vec2 v_uv;
varying vec3 v_normal;
varying float v_h;
varying vec3 v_color;
""" + GLSL_LIGHT + """
void main() {
    if (u_mode == 3) {
        gl_FragColor = vec4(v_color, 1.0);
    } else if (u_mode == 0) {
        gl_FragColor = vec4(texture2D(u_tex, v_uv).rgb, 1.0);
    } else if (u_mode == 1) {
        gl_FragColor = vec4(vec3(0.78) * pm_light(v_normal), 1.0);
    } else {
        float h = clamp(v_h, 0.0, 1.0);
        vec3 c = mix(mix(vec3(0.1, 0.3, 0.9), vec3(0.2, 0.85, 0.3), smoothstep(0.0, 0.5, h)),
                     vec3(0.95, 0.35, 0.1), smoothstep(0.5, 1.0, h));
        gl_FragColor = vec4(c, 1.0);
    }
    gl_FragColor.rgb *= u_tint;
    gl_FragColor.a = u_alpha;                   // < 1 in the transparent view (blending on, no depth writes)
}
"""

_PROGRAMS = {}


def _program():
    ctx = QtGui.QOpenGLContext.currentContext()
    key = id(ctx)
    if key not in _PROGRAMS:
        from OpenGL.GL import shaders
        vs = shaders.compileShader(VERT, GL.GL_VERTEX_SHADER)
        fs = shaders.compileShader(FRAG, GL.GL_FRAGMENT_SHADER)
        prog = GL.glCreateProgram()
        GL.glAttachShader(prog, vs); GL.glAttachShader(prog, fs)
        GL.glBindAttribLocation(prog, 0, "a_position")
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(prog))
        _PROGRAMS[key] = prog
    return _PROGRAMS[key]


def max_texture_size():
    try:
        return int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE))
    except Exception:
        return 4096


def vertex_normals(P, I):
    f = I.astype(np.int64)
    n = np.cross(P[f[:, 1]] - P[f[:, 0]], P[f[:, 2]] - P[f[:, 0]])
    out = np.zeros_like(P, dtype=np.float64)
    for k in range(3):
        for ch in range(3):
            out[:, ch] += np.bincount(f[:, k], weights=n[:, ch], minlength=len(P))
    l = np.linalg.norm(out, axis=1, keepdims=True)
    return (out / np.where(l > 0, l, 1)).astype(np.float32)


class Batch:
    """GPU resources of one textured primitive."""
    def __init__(self, pos, uv, idx, image, normals=None, colors=None, points=False):
        self.points = points
        if points:
            idx = np.arange(len(pos), dtype=np.uint32)
            normals = np.tile(np.float32([0, 0, 1]), (len(pos), 1))
        self.col = np.ascontiguousarray(colors if colors is not None else np.full((len(pos), 3), 0.8), np.float32)
        if self.col.max(initial=0) > 1.5:
            self.col /= 255.0
        self.pos = np.ascontiguousarray(pos, np.float32)
        self.uv = np.ascontiguousarray(uv if uv is not None else np.zeros((len(pos), 2)), np.float32)
        self.idx = np.ascontiguousarray(idx, np.uint32).ravel()
        self.nrm = np.ascontiguousarray(normals if normals is not None else vertex_normals(self.pos, idx.reshape(-1, 3)),
                                        np.float32)
        self.image = image
        self.bufs = None; self.tex = None
        self.nbytes = self.pos.nbytes + self.uv.nbytes + self.idx.nbytes + self.nrm.nbytes + self.col.nbytes + \
            (image.nbytes if image is not None else 0)

    def upload(self):
        self.bufs = []
        for arr, kind in ((self.pos, QOpenGLBuffer.VertexBuffer), (self.uv, QOpenGLBuffer.VertexBuffer),
                          (self.nrm, QOpenGLBuffer.VertexBuffer), (self.idx, QOpenGLBuffer.IndexBuffer),
                          (self.col, QOpenGLBuffer.VertexBuffer)):
            b = QOpenGLBuffer(kind); b.create(); b.bind(); b.allocate(arr.tobytes(), arr.nbytes); b.release()
            self.bufs.append(b)
        if self.image is not None:
            img = self.image
            mx = max_texture_size()
            if max(img.shape[:2]) > mx:
                from PIL import Image
                s = mx / max(img.shape[:2])
                img = np.asarray(Image.fromarray(img).resize((int(img.shape[1] * s), int(img.shape[0] * s)),
                                                              Image.BILINEAR))
            self.tex = int(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, img.shape[1], img.shape[0], 0, GL.GL_RGB,
                            GL.GL_UNSIGNED_BYTE, np.ascontiguousarray(img, np.uint8))
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            try:
                GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            except Exception:
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self.image = None                          # CPU copy no longer needed
        self.pos = self.uv = self.nrm = self.col = None
        self.count = len(self.idx); self.idx = None

    def draw(self, prog):
        if self.bufs is None:
            self.upload()
        locs = []
        for b, name, k in ((self.bufs[0], "a_position", 3), (self.bufs[1], "a_uv", 2), (self.bufs[2], "a_normal", 3),
                             (self.bufs[4], "a_color", 3)):
            loc = GL.glGetAttribLocation(prog, name)
            if loc < 0:
                continue
            b.bind(); GL.glVertexAttribPointer(loc, k, GL.GL_FLOAT, False, 0, None); b.release()
            GL.glEnableVertexAttribArray(loc); locs.append(loc)
        if self.tex is not None:
            GL.glActiveTexture(GL.GL_TEXTURE0); GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        self.bufs[3].bind()
        GL.glDrawElements(GL.GL_POINTS if self.points else GL.GL_TRIANGLES, self.count, GL.GL_UNSIGNED_INT, None)
        self.bufs[3].release()
        for loc in locs:
            GL.glDisableVertexAttribArray(loc)
        if self.tex is not None:
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    def release(self):
        if self.bufs:
            for b in self.bufs:
                b.destroy()
        if self.tex is not None:
            try:
                GL.glDeleteTextures([self.tex])
            except Exception:
                pass
        self.bufs = None; self.tex = None


class _BaseItem(GLGraphicsItem):
    def __init__(self, mode="textured", zrange=(0.0, 1.0)):
        super().__init__()
        self.mode = mode; self.zrange = zrange
        self.setGLOptions("opaque")

    def set_mode(self, mode):
        self.mode = mode; self.update()

    colour_mode = False          # textured mode draws vertex colours (point tiles, untextured meshes)
    alpha = 1.0                  # surface opacity (ModelView.set_transparency)

    def _begin(self):
        self.setupGLState()
        try:
            GL.glPointSize(2.0)
        except Exception:
            pass
        prog = _program()
        GL.glUseProgram(prog)
        mvp = np.array(self.mvpMatrix().data(), np.float32)
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(prog, "u_mvp"), 1, False, mvp)
        mv = np.array(self.modelViewMatrix().data(), np.float64).reshape(4, 4).T    # column-major -> rows
        GL.glUniformMatrix3fv(GL.glGetUniformLocation(prog, "u_nmat"), 1, True,
                              np.ascontiguousarray(normal_matrix(mv), np.float32))
        GL.glUniform1i(GL.glGetUniformLocation(prog, "u_tex"), 0)
        m = {"textured": 0, "solid": 1, "elevation": 2, "wireframe": 1}.get(self.mode, 0)
        if m == 0 and self.colour_mode:
            m = 3
        GL.glUniform1i(GL.glGetUniformLocation(prog, "u_mode"), m)
        GL.glUniform2f(GL.glGetUniformLocation(prog, "u_zrange"), float(self.zrange[0]), float(self.zrange[1]))
        GL.glUniform3f(GL.glGetUniformLocation(prog, "u_tint"), 1.0, 1.0, 1.0)
        GL.glUniform1f(GL.glGetUniformLocation(prog, "u_alpha"), float(self.alpha))
        if self.mode == "wireframe":
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        return prog

    def _end(self):
        if self.mode == "wireframe":
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glUseProgram(0)


class TexturedMeshItem(_BaseItem):
    """A mesh with a multi-page texture atlas (data from texture.load_display)."""
    def __init__(self, batches, mode="textured", zrange=(0, 1)):
        super().__init__(mode, zrange)
        self.batches = batches

    def paint(self):
        if not self.batches:
            return
        prog = self._begin()
        for b in self.batches:
            b.draw(prog)
        self._end()


def load_textured_display(mesh_path, tex_dir, flip=None):
    """CPU side of the textured mesh: one Batch per texture page."""
    from PIL import Image
    from .texture import read_mesh
    from .gltf import split_corners
    Image.MAX_IMAGE_PIXELS = None
    V, F = read_mesh(mesh_path)
    z = np.load(os.path.join(tex_dir, "uv.npz"))
    with open(os.path.join(tex_dir, "info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    uv, page, size = z["uv"], z["page"].astype(np.int64), int(z["size"])
    if len(uv) != len(F):
        raise ValueError("texture does not belong to this model (face count differs)")
    Vd = V * (flip if flip is not None else 1)
    out = []
    for k, name in enumerate(info["pages"]):
        sel = np.flatnonzero(page == k)
        if not len(sel):
            continue
        pos, uvs, idx = split_corners(Vd, F[sel], uv[sel] / size)
        img = np.asarray(Image.open(os.path.join(tex_dir, name)).convert("RGB"))
        out.append(Batch(pos, uvs, idx, img))
    return out, (float(Vd[:, 2].min()), float(Vd[:, 2].max()))


LEVEL_TINTS = [(1.0, 0.55, 0.55), (0.55, 1.0, 0.55), (0.55, 0.65, 1.0), (1.0, 1.0, 0.5), (1.0, 0.6, 1.0),
               (0.5, 1.0, 1.0)]


def read_pnts(data):
    """Positions (+RTC_CENTER) and RGB of a 3D Tiles 1.0 pnts tile."""
    import struct
    _m, _v, _n, fjl, fbl, _bj, _bb = struct.unpack("<4sIIIIII", data[:28])
    ft = json.loads(data[28:28 + fjl].decode())
    body = data[28 + fjl:28 + fjl + fbl]
    n = int(ft["POINTS_LENGTH"])
    P = np.frombuffer(body, np.float32, n * 3, ft["POSITION"]["byteOffset"]).reshape(n, 3).astype(np.float64)
    P = P + np.asarray(ft.get("RTC_CENTER", [0, 0, 0]), float)
    C = np.frombuffer(body, np.uint8, n * 3, ft["RGB"]["byteOffset"]).reshape(n, 3) if "RGB" in ft else None
    return P, C


class TiledModelItem(_BaseItem):
    """Streams 3D Tiles produced by tiles.build_tiles, choosing levels by
    screen-space error (SSE = geometricError x viewport height / (2 d tan(fov/2)))."""
    def __init__(self, tiles_dir, display_matrix, sse=16.0, budget_mb=1200, mode="textured"):
        with open(os.path.join(tiles_dir, "photomesh_tiles.json"), encoding="utf-8") as fh:
            self.index = json.load(fh)
        nodes = self.index["nodes"]
        z0 = min(n["bmin"][2] for n in nodes); z1 = max(n["bmax"][2] for n in nodes)
        super().__init__(mode, (z0, z1))
        self.dir = tiles_dir
        self.nodes = {n["key"]: n for n in nodes}
        for n in nodes:
            lo, hi = np.array(n["bmin"]), np.array(n["bmax"])
            n["c"] = (lo + hi) / 2; n["r"] = float(np.linalg.norm(hi - lo) / 2)
        self.root = nodes[0]["key"]
        self.colour_mode = self.index.get("source") == "Point cloud" or \
            any(str(n.get("file", "")).endswith(".pnts") for n in nodes[:1])
        self.M = np.asarray(display_matrix, float)        # tile frame -> display frame (4x4)
        self.Minv = np.linalg.inv(self.M)
        from PySide6.QtGui import QMatrix4x4
        self.setTransform(QMatrix4x4(*self.M.ravel().tolist()))
        self.sse = sse; self.budget = budget_mb * 1e6
        self.loaded = {}; self.pending = set(); self.used = {}; self.frame = 0
        self.q = queue.Queue(); self.pool = ThreadPoolExecutor(3)
        self.stats = {"drawn": 0, "loaded": 0, "requests": 0, "max_depth_drawn": 0}
        self._timer = None
        self.show_levels = False         # tint tiles by LOD level (debug / screenshots)

    def _load(self, key):
        from io import BytesIO
        from PIL import Image
        from .gltf import read_glb
        n = self.nodes[key]
        try:
            with open(os.path.join(self.dir, "tiles", n["file"]), "rb") as fh:
                data = fh.read()
            if data[:4] == b"pnts":
                P, C = read_pnts(data)
                self.q.put((key, [Batch(P, None, None, None, colors=C, points=True)]))
                return
            prims = read_glb(data, z_up=True)
            out = []
            for p in prims:
                img = np.asarray(Image.open(BytesIO(p["image"])).convert("RGB")) if p["image"] else None
                out.append(Batch(p["positions"], p["uv"], p["indices"], img))
            self.q.put((key, out))
        except Exception as e:                 # broken tile: never retried in this session
            self.q.put((key, e))

    def request(self, key):
        if key not in self.loaded and key not in self.pending and self.nodes[key].get("file"):
            self.pending.add(key); self.stats["requests"] += 1
            self.pool.submit(self._load, key)

    def _drain(self, limit=6):
        for _ in range(limit):
            try:
                key, data = self.q.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(key)
            self.loaded[key] = data if not isinstance(data, Exception) else []
            self.stats["loaded"] += 1

    def _evict(self):
        size = sum(b.nbytes for bs in self.loaded.values() for b in bs)
        if size <= self.budget:
            return
        for key in sorted(self.loaded, key=lambda k: self.used.get(k, -1)):
            if self.used.get(key, -1) >= self.frame - 2 or key == self.root:
                continue
            for b in self.loaded.pop(key):
                size -= b.nbytes; b.release()
            if size <= self.budget * 0.8:
                break

    def select(self, eye, planes, height_px, fov_deg):
        """Tiles to draw (keys) for camera eye (tile frame) and frustum planes."""
        k = height_px / (2 * math.tan(math.radians(fov_deg) / 2))
        draw, stack = [], [self.root]
        while stack:
            key = stack.pop()
            n = self.nodes[key]
            c, r = n["c"], n["r"]
            if planes is not None and any(p[:3] @ c + p[3] < -r for p in planes):
                continue
            d = max(float(np.linalg.norm(eye - c)) - r, 1e-6)
            sse = n["error"] * k / d
            kids = n["children"]
            if sse > self.sse and kids:
                if all(ch in self.loaded or not self.nodes[ch].get("file") for ch in kids):
                    stack.extend(kids); continue
                for ch in kids:
                    self.request(ch)
            if key in self.loaded:
                draw.append(key)
            else:
                self.request(key)
                # draw the nearest loaded ancestor meanwhile
                anc = key[:-1]
                while anc and anc not in self.loaded:
                    anc = anc[:-1]
                if anc and anc not in draw:
                    draw.append(anc)
        return draw

    def paint(self):
        self._drain()
        view = self.view()
        mvp = np.array(self.mvpMatrix().data(), np.float64).reshape(4, 4).T   # row-major
        planes = [mvp[3] + mvp[0], mvp[3] - mvp[0], mvp[3] + mvp[1], mvp[3] - mvp[1], mvp[3] + mvp[2], mvp[3] - mvp[2]]
        planes = [p / np.linalg.norm(p[:3]) for p in planes]
        cp = view.cameraPosition()
        eye = (self.Minv @ np.array([cp.x(), cp.y(), cp.z(), 1.0]))[:3]
        dpr = view.devicePixelRatioF() if hasattr(view, "devicePixelRatioF") else 1.0
        keys = self.select(eye, planes, view.height() * dpr, view.opts.get("fov", 30))
        self.frame += 1
        prog = self._begin()
        drawn = 0
        tint = GL.glGetUniformLocation(prog, "u_tint")
        for key in keys:
            if self.show_levels:
                GL.glUniform3f(tint, *LEVEL_TINTS[self.nodes[key]["depth"] % len(LEVEL_TINTS)])
            self.used[key] = self.frame
            for b in self.loaded.get(key, []):
                b.draw(prog); drawn += 1
        self._end()
        self.stats["drawn"] = len(keys)
        self.stats["max_depth_drawn"] = max([self.nodes[k]["depth"] for k in keys] or [0])
        self._evict()
        if self.pending or not self.q.empty():
            if self._timer is None:
                self._timer = QTimer(); self._timer.setSingleShot(True)
                self._timer.timeout.connect(lambda: self.view() and self.view().update())
            if not self._timer.isActive():
                self._timer.start(40)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
