"""Build Tiled Model on rough drone meshes (issue #1, 0.1.5): coarse levels of
detail split into thousands of tiny charts; their fixed border made the shelf
packer give up with "texture atlas packing failed"."""
import os

import numpy as np
import pytest

from photomesh import tiles
from photomesh.plyio import save_ply

UP = np.array([0, 0, 1.0])


def rough(n=173, amp=3.0, seed=1):
    """~59k-face height field with noise like a dense drone mesh."""
    rng = np.random.default_rng(seed)
    x, y = np.meshgrid(np.linspace(0, 10, n), np.linspace(0, 10, n))
    z = rng.normal(0, amp * 10 / n, x.shape)
    V = np.stack([x.ravel(), y.ravel(), z.ravel()], 1)
    i = np.arange(n - 1); I, J = np.meshgrid(i, i)
    a = (I * n + J).ravel(); b = a + 1; c = a + n; d = c + 1
    return V, np.concatenate([np.stack([a, b, d], 1), np.stack([a, d, c], 1)])


def _valid(uvp, size, F):
    assert uvp.shape == (len(F), 3, 2) and np.isfinite(uvp).all()
    assert uvp.min() >= 0 and uvp.max() <= size


@pytest.mark.parametrize("texel,amp", [(0.05, 0.6), (0.2, 0.6), (0.2, 3.0), (0.002, 3.0)])
def test_rough_mesh_any_level_packs(texel, amp):
    V, F = rough(amp=amp)
    uvp, size, te = tiles.tile_atlas(V, F, UP, texel, 2048)
    _valid(uvp, size, F)
    assert 64 <= size <= 2048 and te >= texel * 0.999


def test_face_grid_fallback_when_page_too_small():
    V, F = rough(amp=3.0)
    uvp, size, te = tiles.tile_atlas(V, F, UP, 0.2, 256)    # tiny max texture: per-triangle grid
    _valid(uvp, size, F)
    cell = size // int(np.ceil(np.sqrt(len(F))))
    idx = np.floor(uvp.min(1) / cell).astype(np.int64)
    assert (np.floor((uvp.max(1) - 1e-6) / cell).astype(np.int64) == idx).all()   # each stays in its cell
    assert len(np.unique(idx[:, 0] * 100000 + idx[:, 1])) == len(F)                 # one cell each


def test_fallback_used_when_packer_fails(monkeypatch):
    V, F = rough(n=40)
    def boom(*a, **k):
        raise RuntimeError("texture atlas packing failed")
    monkeypatch.setattr(tiles, "pack", boom)
    uvp, size, te = tiles.tile_atlas(V, F, UP, 0.05, 1024)
    _valid(uvp, size, F)
    assert size <= 1024 and te > 0


def test_build_tiled_model_rough_mesh(tmp_path):
    """The whole Build Tiled Model run of issue #1's kind of mesh finishes."""
    V, F = rough(amp=3.0)
    save_ply(str(tmp_path / "m.ply"), V.astype(np.float32), None, F)
    r = tiles.build_tiles(dict(out_dir=str(tmp_path / "t"), mesh=str(tmp_path / "m.ply"), tile_faces=8000,
                               up=[0, 0, 1]), log=lambda m: None)
    assert r["levels"] >= 2 and os.path.exists(r["tileset"])


def test_build_texture_pack_fit_adds_pages():
    """Build Texture's atlas: too many tiny charts for the requested pages ->
    more pages instead of 'texture atlas packing failed'."""
    from photomesh import texture as tx
    rng = np.random.default_rng(3)
    n = 6000                                     # 6000 separate 2x2 px triangles
    base = rng.uniform(0, 1, (n, 1, 2)) * 5
    uv = base + np.array([[0, 0], [2, 0], [0, 2.0]])
    comp = np.arange(n)
    with pytest.raises(RuntimeError):
        tx.pack(uv, comp, 256, 1, 2, 1.0, log=lambda m: None)
    msgs = []
    page, uvp, s = tx.pack_fit(uv, comp, 256, 1, 2, log=msgs.append)
    assert page.max() >= 1 and uvp.min() >= 0 and uvp.max() <= 256
    assert any("not enough" in m for m in msgs)
    with pytest.raises(RuntimeError, match="larger texture size"):
        tx.pack_fit(np.repeat(uv, 20, 0), np.arange(n * 20), 64, 1, 2, log=lambda m: None)
