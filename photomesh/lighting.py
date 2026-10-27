"""Camera-relative lighting for the 3D Model view (Shaded / Solid modes).

A key light fixed to the camera (slightly above-left of the view direction),
a headlight fill along the view axis and an ambient term, evaluated in eye
space with two-sided normals, so a surface seen from above (terrain from a
drone survey) is lit whatever the orbit angle, and a mesh whose winding or
normals point down (e.g. after a mirrored transform) does not go black.

pyqtgraph's built-in 'shaded' program lights from eye-space (1, -1, -1),
i.e. from *behind* the surface as seen by the viewer: a face looking at the
camera got only its 0.2 ambient - the "dark from above" bug."""
from __future__ import annotations

import numpy as np

KEY_DIR = np.array([-0.35, 0.5, 1.0]) / np.linalg.norm([-0.35, 0.5, 1.0])   # eye space, towards the light
AMBIENT, KEY, FILL = 0.25, 0.55, 0.25

GLSL_LIGHT = f"""
float pm_light(vec3 n_eye) {{
    vec3 n = normalize(n_eye);
    if (n.z < 0.0) n = -n;                       // two-sided: normals facing away are flipped
    float key = max(dot(n, vec3({KEY_DIR[0]:.6f}, {KEY_DIR[1]:.6f}, {KEY_DIR[2]:.6f})), 0.0);
    float fill = max(n.z, 0.0);                  // headlight along the view axis
    return min({AMBIENT:.3f} + {KEY:.3f} * key + {FILL:.3f} * fill, 1.05);
}}
"""


def light_intensity(n_eye):
    """CPU reference of pm_light (tests): n_eye (..., 3) eye-space normals."""
    n = np.asarray(n_eye, float)
    n = n / np.linalg.norm(n, axis=-1, keepdims=True)
    n = np.where(n[..., 2:3] < 0, -n, n)
    key = np.clip(n @ KEY_DIR, 0, None)
    return np.minimum(AMBIENT + KEY * key + FILL * np.clip(n[..., 2], 0, None), 1.05)


def normal_matrix(model_view):
    """Inverse-transpose of the upper 3x3 of a 4x4 model-view matrix."""
    A = np.asarray(model_view, float)[:3, :3]
    return np.linalg.inv(A).T


def orient_normals_up(normals, z_up=True, min_fraction=0.5):
    """Terrain meshes are viewed from above: if most normals point down (a mesh
    with reversed winding or mirrored normals), flip them all. Returns
    (normals, flipped)."""
    N = np.asarray(normals, np.float32)
    if not z_up or not len(N):
        return N, False
    down = float(np.mean(N[:, 2] < 0))
    if down > min_fraction:
        return -N, True
    return N, False


_PROGRAM = None


def headlight_program():
    """pyqtgraph ShaderProgram (uses u_mvp, u_normal set by GLMeshItem)."""
    global _PROGRAM
    if _PROGRAM is None:
        from pyqtgraph.opengl import shaders

        class _PerContextProgram(shaders.ShaderProgram):
            """pyqtgraph compiles a ShaderProgram once, in whichever GL context is
            current; a second GL widget (another window, a recreated context)
            then used a program id of the first context -> GL_INVALID_VALUE
            and an empty frame. Compile once per context instead."""
            def program(self, **kw):
                from PySide6.QtGui import QOpenGLContext
                key = id(QOpenGLContext.currentContext())
                progs = self.__dict__.setdefault("_progs", {})
                self.prog = progs.get(key)
                progs[key] = super().program(**kw)
                return progs[key]

        _PROGRAM = _PerContextProgram("photomesh_headlight", [
            shaders.VertexShader("""
                uniform mat4 u_mvp;
                uniform mat3 u_normal;
                attribute vec4 a_position;
                attribute vec3 a_normal;
                attribute vec4 a_color;
                varying vec4 v_color;
                varying vec3 v_normal;
                void main() {
                    v_normal = u_normal * a_normal;
                    v_color = a_color;
                    gl_Position = u_mvp * a_position;
                }
            """),
            shaders.FragmentShader("""
                #ifdef GL_ES
                precision mediump float;
                #endif
                varying vec4 v_color;
                varying vec3 v_normal;
            """ + GLSL_LIGHT + """
                void main() {
                    gl_FragColor = vec4(v_color.rgb * pm_light(v_normal), v_color.a);
                }
            """)])
    return _PROGRAM
