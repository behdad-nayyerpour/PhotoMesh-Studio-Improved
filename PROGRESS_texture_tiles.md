# Build Texture / Build Tiled Model - progress (times UTC+4)

## 17:44 milestone: GUI integration + tests (box)
- Workflow > Build Texture (dialog: type, source, mapping Generic/Adaptive orthophoto/Orthophoto/Keep uv,
  blending Mosaic/Average/Max/Min/Disabled, size x count, hole filling, ghosting, working/full res, JPEG/PNG)
  runs `python -m photomesh.tasks build_texture` in a child process (progress bar, cancel).
- Workflow > Build Tiled Model (source Model / Point cloud, pixel size, b3dm/glb, tile size, max tile texture).
- Chunk.texture / Chunk.tiled_model saved in the .pmsx (paths rebased on load); texture dropped when the model changes.
- Workspace rows "3D Model (N faces, ..., Texture 8192x8192)" and "Tiled Model (N levels, X cm/pix)"
  ("units/pix (not georeferenced)" otherwise); Properties rows.
- Viewer: Model - Textured = GLSL textured shader (glitems.TexturedMeshItem), not vertex colours;
  Tiled Model - Textured/Solid/Wireframe/Elevation = TiledModelItem (SSE 16 px LOD streaming, 3 loader threads, LRU).
- Export Model: OBJ+MTL+JPG, GLB, textured PLY, plain PLY. Export Tiled Model: zip or folder.
- tests/test_texture_tiles.py: 17 tests (+1 real-project test, opt-in). Box: 136 passed, 6 skipped.

## 17:55 milestone: real runs on bench_drone5 (PC, outputs in new folders, .pmsx untouched)
- Texture Generic/Mosaic 8192x1 cold: 167.2 s, 630 photos, texel 25.7 cm (photo GSD 4.09 cm/pix).
- Texture Generic/Mosaic 8192x4 (cached visibility): 156.8 s, texel 12.9 cm.
- Texture Adaptive orthophoto 8192x1 (cached visibility): 99.8 s, texel 24.9 cm.
- Tiled model from textured mesh: 106 tiles, 5 levels, 25.72 cm/pix, 115 MB, 44.5 s.
- Tiled model from dense cloud (27.8 M pts): 345 tiles, 7 levels, 7.59 cm/pix, 748 MB, 12.2 s.

## 18:50-19:10 resume after the laptop restart
- PC check: no leftover colmap/python jobs (only the user's own pythonw main.py, started 18:47). All 4 texture
  folders and both tiles folders are complete (finished 17:37-17:57), no *.tmp/partial files; nothing to clean up.
  The interrupted step was the code sync/test run, not a build.
- Sync: 6 files were out of date on the PC (tiles.py zip-path fix, __init__ 0.1.2, check_update.bat, README,
  INSTALL, this note). After the sync: 70 files, 0 SHA-256 mismatches.
- Visual check (box, Xvfb + Mesa GL, real outputs generic_mosaic_v2 8192x1 + mesh_textured tiles):
  /workspace/texture_real.png, /workspace/texture_real_close.png, /workspace/tiles_real.png (LOD tint far/near).
  No seams, colour jumps, flipped UVs or wrong-photo patches at normal zoom; thin straight lines on the cliff are
  real power lines (pylon visible). Atlas check: UV overlap 0.06 % of texels (shared edges only), 4.4 % of faces
  are slivers < 1 texel (drawn from their neighbours' texels), 11,830 faces (0.6 %) hole-filled from vertex colours
  (visible as slightly flat triangles only at extreme zoom). Tiles: LOD depth 2 far / 4 near, no cracks.
- New: File > Import > Import Texture... / Import Tiled Model... (were "not implemented"): attach existing output
  folders without rebuilding; texture refused when the face count differs. Tiled timing read from *_result.json.
  Test test_gui_import_texture_and_tiles. Real PC folders import in 0.01 s (8192x4 -> 12.9 cm/pix).
- Tests: box 137 passed / 6 skipped; Windows (PM_TESTDATA=testdata\sceaux) see below.
- Packaging (update/portable zip) skipped at the user's request.
- Windows 19:05: 141 passed, 2 skipped (PM_TEST_DENSE GPU test, opt-in real-project test), 68 s. Code synced (70 files, 0 mismatches).
