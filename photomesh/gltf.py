"""Minimal glTF 2.0 binary (.glb) writer/reader and 3D Tiles b3dm wrapper.

Meshes are written as one primitive per texture page with POSITION, TEXCOORD_0
(and optionally NORMAL / COLOR_0) and uint32 indices; textures are embedded
JPEG/PNG bytes. Coordinates: glTF is y-up, so z-up data is rotated
(x, y, z) -> (x, z, -y) when `z_up=True` (3D Tiles convention)."""
from __future__ import annotations

import json
import struct

import numpy as np

ZUP_TO_YUP = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], np.float64)


def _pad(b, ch=b"\0"):
    return b + ch * ((4 - len(b) % 4) % 4)


def split_corners(V, F, uv):
    """Unique (vertex, uv) pairs -> (positions, uvs, indices)."""
    m = len(F)
    key_v = F.ravel()
    q = np.round(uv.reshape(-1, 2).astype(np.float64) * 65536).astype(np.int64)
    key = np.stack([key_v, q[:, 0], q[:, 1]], 1)
    uk, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    first = np.zeros(len(uk), np.int64); first[inv] = np.arange(len(inv))
    pos = V[key_v[first]]
    uvs = uv.reshape(-1, 2)[first]
    return pos, uvs, inv.reshape(m, 3)


def write_glb(path, primitives, z_up=True, extras=None, translation=None):
    """primitives: list of dict(positions (n,3), indices (m,3), uv (n,2) in 0..1
    image coords (origin top-left) or None, colors (n,3) uint8 or None,
    image bytes or None, mime). Returns bytes written."""
    data = to_glb(primitives, z_up, extras, translation)
    from .export import atomic_write
    with atomic_write(path) as fh:
        fh.write(data)
    return len(data)


def to_glb(primitives, z_up=True, extras=None, translation=None):
    """translation: (3,) model-frame offset put on the root node (converted to
    glTF Y-up like the positions when z_up), e.g. the georeference offset."""
    bin_parts, views, accessors, meshes_prims, images, textures, materials = [], [], [], [], [], [], []
    off = 0

    def add_view(b, target=None):
        nonlocal off
        b = _pad(b)
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(b)}
        if target:
            v["target"] = target
        views.append(v); bin_parts.append(b); off += len(b)
        return len(views) - 1

    for p in primitives:
        P = np.asarray(p["positions"], np.float64)
        if z_up:
            P = P @ ZUP_TO_YUP.T
        P = P.astype(np.float32)
        attrs = {}
        vi = add_view(P.tobytes(), 34962)
        accessors.append({"bufferView": vi, "componentType": 5126, "count": len(P), "type": "VEC3",
                          "min": P.min(0).tolist() if len(P) else [0, 0, 0],
                          "max": P.max(0).tolist() if len(P) else [0, 0, 0]})
        attrs["POSITION"] = len(accessors) - 1
        if p.get("normals") is not None:
            N = np.asarray(p["normals"], np.float64)
            if z_up:
                N = N @ ZUP_TO_YUP.T
            vi = add_view(N.astype(np.float32).tobytes(), 34962)
            accessors.append({"bufferView": vi, "componentType": 5126, "count": len(N), "type": "VEC3"})
            attrs["NORMAL"] = len(accessors) - 1
        if p.get("uv") is not None:
            vi = add_view(np.asarray(p["uv"], np.float32).tobytes(), 34962)
            accessors.append({"bufferView": vi, "componentType": 5126, "count": len(p["uv"]), "type": "VEC2"})
            attrs["TEXCOORD_0"] = len(accessors) - 1
        if p.get("colors") is not None:
            vi = add_view(np.asarray(p["colors"], np.uint8)[:, :3].tobytes(), 34962)
            accessors.append({"bufferView": vi, "componentType": 5121, "normalized": True,
                              "count": len(p["colors"]), "type": "VEC3"})
            attrs["COLOR_0"] = len(accessors) - 1
        I = np.asarray(p["indices"], np.uint32).ravel()
        vi = add_view(I.tobytes(), 34963)
        accessors.append({"bufferView": vi, "componentType": 5125, "count": len(I), "type": "SCALAR"})
        prim = {"attributes": attrs, "indices": len(accessors) - 1, "mode": 4}
        if p.get("image") is not None:
            ivi = add_view(p["image"])
            images.append({"bufferView": ivi, "mimeType": p.get("mime", "image/jpeg")})
            textures.append({"source": len(images) - 1, "sampler": 0})
            materials.append({"pbrMetallicRoughness": {"baseColorTexture": {"index": len(textures) - 1},
                                                       "metallicFactor": 0.0, "roughnessFactor": 1.0},
                              "doubleSided": True})
            prim["material"] = len(materials) - 1
        else:
            materials.append({"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0,
                                                       "roughnessFactor": 1.0}, "doubleSided": True})
            prim["material"] = len(materials) - 1
        meshes_prims.append(prim)
    node = {"mesh": 0}
    if translation is not None:
        T = np.asarray(translation, np.float64)
        node["translation"] = [float(x) for x in (ZUP_TO_YUP @ T if z_up else T)]
    gl = {"asset": {"version": "2.0", "generator": "PhotoMesh Studio"},
          "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [node],
          "meshes": [{"primitives": meshes_prims}], "accessors": accessors, "bufferViews": views,
          "buffers": [{"byteLength": off}], "materials": materials}
    if images:
        gl.update(images=images, textures=textures,
                  samplers=[{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}])
    if extras:
        gl["extras"] = extras
    js = _pad(json.dumps(gl, separators=(",", ":")).encode("utf-8"), b" ")
    binb = b"".join(bin_parts)
    total = 12 + 8 + len(js) + 8 + len(binb)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(binb), b"BIN\0") + binb)


def read_glb(data, z_up=True):
    """Parse our own .glb bytes -> list of dict(positions, indices, uv, colors, image bytes)."""
    if data[:4] == b"b3dm":
        data = b3dm_glb(data)
    magic, _ver, _total = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("not a glb file")
    jl, _jt = struct.unpack_from("<I4s", data, 12)
    gl = json.loads(data[20:20 + jl])
    bo = 20 + jl
    bl, _bt = struct.unpack_from("<I4s", data, bo)
    binb = data[bo + 8: bo + 8 + bl]
    def acc(i):
        a = gl["accessors"][i]; v = gl["bufferViews"][a["bufferView"]]
        dt = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}[a["componentType"]]
        k = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
        arr = np.frombuffer(binb, dt, a["count"] * k, v.get("byteOffset", 0) + a.get("byteOffset", 0))
        return arr.reshape(-1, k) if k > 1 else arr
    out = []
    for prim in gl["meshes"][0]["primitives"]:
        at = prim["attributes"]
        P = acc(at["POSITION"]).astype(np.float64)
        if z_up:
            P = P @ ZUP_TO_YUP              # inverse rotation (orthonormal)
        d = {"positions": P, "indices": acc(prim["indices"]).reshape(-1, 3).astype(np.int64),
             "uv": acc(at["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in at else None,
             "colors": acc(at["COLOR_0"]) if "COLOR_0" in at else None, "image": None}
        mat = gl["materials"][prim["material"]] if "material" in prim else {}
        t = (mat.get("pbrMetallicRoughness") or {}).get("baseColorTexture")
        if t is not None:
            img = gl["images"][gl["textures"][t["index"]]["source"]]
            v = gl["bufferViews"][img["bufferView"]]
            d["image"] = bytes(binb[v.get("byteOffset", 0): v.get("byteOffset", 0) + v["byteLength"]])
        out.append(d)
    return out


def b3dm(glb, batch_length=0):
    """3D Tiles 1.0 Batched 3D Model wrapper around a glb."""
    ft = _pad(json.dumps({"BATCH_LENGTH": batch_length}).encode(), b" ")
    # header is 28 bytes; feature table JSON must end on an 8-byte boundary
    while (28 + len(ft)) % 8:
        ft += b" "
    total = 28 + len(ft) + len(glb)
    return struct.pack("<4sIIIIII", b"b3dm", 1, total, len(ft), 0, 0, 0) + ft + glb


def b3dm_glb(data):
    _m, _v, total, ftj, ftb, btj, btb = struct.unpack_from("<4sIIIIII", data, 0)
    return data[28 + ftj + ftb + btj + btb: total]
