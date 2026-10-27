"""Synthetic textured scene for Build Texture / Tiled Model tests: a height
field with a raised block (steep walls), a procedural albedo, two sensors
(SIMPLE_RADIAL with distortion + PINHOLE), photos rendered by ray casting."""
import os

import numpy as np

from photomesh import colmap_io as cio


def albedo(P):
    x, y, z = P[:, 0], P[:, 1], P[:, 2]
    chk = (np.floor(x) + np.floor(y)) % 2
    r = 60 + 150 * chk
    g = 40 + 8 * x + 30 * np.clip(z, 0, 3)
    b = 40 + 8 * y
    return np.clip(np.stack([r, g, b], 1), 0, 255)


def height(x, y):
    h = 0.4 * np.sin(x / 3.0) * np.cos(y / 4.0)
    block = (np.abs(x - 10) < 2.5) & (np.abs(y - 10) < 2.5)
    return h + np.where(block, 2.0, 0.0)


def make_mesh(n=80, extent=20.0):
    g = np.linspace(0, extent, n)
    X, Y = np.meshgrid(g, g, indexing="ij")
    Z = height(X, Y)
    V = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    F = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    return V, F


def look_at(C, target):
    z = target - C; z /= np.linalg.norm(z)
    x = np.cross(z, [0, 1.0, 0]); x /= np.linalg.norm(x)    # image x ~ world x
    y = np.cross(z, x)
    R = np.stack([x, y, z])                                  # world -> camera rows
    return R, -R @ C


def render(scene, cam, R, t, ghost=None):
    import open3d as o3d
    W, H = cam.width, cam.height
    u, v = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    x, y = cio.undistort_points(cam, np.stack([u.ravel(), v.ravel()], 1))
    d = np.stack([x, y, np.ones_like(x)], 1) @ R
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    C = -R.T @ t
    rays = np.concatenate([np.repeat(C[None], len(d), 0), d], 1).astype(np.float32)
    th = scene.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy()
    P = C + d * np.where(np.isfinite(th), th, 0)[:, None]
    col = albedo(P)
    col[~np.isfinite(th)] = 0
    img = col.reshape(H, W, 3)
    if ghost is not None:                     # a "car" that is only in this photo
        gx, gy, gr = ghost
        hit = np.hypot(P[:, 0] - gx, P[:, 1] - gy).reshape(H, W) < gr
        img[hit] = (250, 0, 250)
    return img.astype(np.uint8)


def make_scene(d, ghost=False, n_mesh=80):
    import open3d as o3d
    from PIL import Image
    from photomesh.plyio import save_ply
    os.makedirs(d, exist_ok=True)
    V, F = make_mesh(n_mesh)
    save_ply(os.path.join(d, "model.ply"), V.astype(np.float32), None, F)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
    cams = {1: cio.Camera(2, 320, 240, np.array([260.0, 160.0, 120.0, -0.05])),     # SIMPLE_RADIAL
            2: cio.Camera(1, 400, 300, np.array([300.0, 300.0, 200.0, 150.0]))}     # PINHOLE
    imgs, photos = {}, {}
    k = 0
    for i, cx in enumerate(np.linspace(3, 17, 4)):
        for j, cy in enumerate(np.linspace(3, 17, 4)):
            k += 1
            C = np.array([cx + 1.5 * np.sin(k), cy, 14.0 + (k % 3)])
            tgt = np.array([cx + 2.0 * np.cos(k), cy + 1.0 * np.sin(2 * k), 0.0])
            R, t = look_at(C, tgt)
            cid = 1 if k % 2 else 2
            name = f"IMG_{k:03d}.jpg"
            g = (10.0, 4.0, 1.2) if (ghost and k == 6) else None
            img = render(scene, cams[cid], R, t, g)
            p = os.path.join(d, "photos", name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Image.fromarray(img).save(p, quality=95)
            imgs[k] = cio.Image(name, cid, cio.rot_to_qvec(R), t, np.zeros((0, 2)), np.zeros(0, np.int64))
            photos[name] = p
    md = os.path.join(d, "sparse"); os.makedirs(md, exist_ok=True)
    cio.write_cameras(os.path.join(md, "cameras.bin"), cams)
    cio.write_images(os.path.join(md, "images.bin"), imgs)
    return {"mesh": os.path.join(d, "model.ply"), "model": md, "photos": photos, "V": V, "F": F}


def texture_error(info, V, F, n=4000):
    """Median |texture - albedo| at random surface points of textured faces."""
    from PIL import Image
    z = np.load(info["uv_npz"])
    uv, page = z["uv"], z["page"]
    pages = [np.asarray(Image.open(os.path.join(info["dir"], p))).astype(np.float32) for p in info["pages"]]
    rng = np.random.default_rng(3)
    f = rng.integers(0, len(F), n)
    b = rng.dirichlet([3, 3, 3], n)
    P = (V[F[f]] * b[:, :, None]).sum(1)
    q = (uv[f] * b[:, :, None]).sum(1)
    col = np.array([pages[page[i]][int(q[k, 1]), int(q[k, 0])] for k, i in enumerate(f)])
    ref = albedo(P)
    # skip points on checker edges (bilinear mixing is expected there)
    fx, fy = P[:, 0] % 1, P[:, 1] % 1
    inner = (np.minimum(fx, 1 - fx) > 0.12) & (np.minimum(fy, 1 - fy) > 0.12)
    return float(np.median(np.abs(col - ref)[inner])), col, ref, P
