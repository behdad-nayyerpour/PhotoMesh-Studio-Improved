# PhotoMesh Studio – dense / filter / mesh / GCP progress (2026-10-21)

Times are Asia/Yerevan (UTC+4). Project: C:\Users\Behdad\PhotoMeshStudio\bench_drone5\drone_DJI_001_fast.pmsx
(backup .pmsx.bak), workdir bench_drone5\fast, 640/641 aligned.

## Milestone A – dense cloud (DONE, run 11:56–12:50)
- Medium (1/4: 1320x989 wide, 1008x756 tele), Mild depth filtering, 8 sources/photo (median 8, min 3),
  window_step 2, 3 iterations, geom_consistency.
- Undistort 25.3 s | PatchMatch (photometric+geometric) 2990 s | fusion 221.7 s -> 28,695,265 points
  | filtering 53 s. DENSE TOTAL 3291.5 s. 1280 photometric maps removed afterwards.
- Cross-drive bug (commonpath ValueError, photos F:, project C:) fixed by is_inside(); the 11:45 run failed on it,
  the 11:56 run passed. Network monitor 11:55–13:04: 0 network entries.

## Milestone B – outlier filtering (DONE)
- Global radius filter (r = 3x median spacing): 28,695,265 -> 23,305,705 (confidence -829,038, elevation -8,881,
  statistical -5,744, radius -4,545,897). Render showed it removed periphery/steep slopes (= valid sparse areas).
- New density-adaptive filter pcfilter.local_outliers (default): 28,695,265 -> 27,806,818
  (confidence -829,038, elevation -8,881, statistical(local) -17,178, radius(local) -33,350), 64 s.

## Milestone C – mesh (DONE)
- From global-filtered cloud: voxel -> 9.65M pts, Poisson depth 12, 20.19M faces, decimate -> 1,553,643 faces, 815 s.
  Holes where the radius filter removed ground.
- From local-filtered cloud + target-aware Poisson depth: voxel -> 12.28M, depth 11, 5.79M faces ->
  1,853,765 faces (Medium = points/15), 204 s. -> fast\model_local.ply (project points to it).

## Milestone D – GCP (DONE on copies of the model)
- GCP.txt (Documents\Meta): Metashape Reference export, tab-separated, LOCAL_CS "Local Coordinates (m)",
  columns Label X Y Z Error_(m) X/Y/Z_error X/Y/Z_est; coordinates only (no projections);
  Metashape total error 0.118 m. Projections taken read-only from the earlier project
  F:\...\HLO1-2 INTAKE.psx -> gcp_data\HLO1-2_markers.xml (98 markers, 2947 projections, 2628 pinned, 80 enabled).
- New BA (LM + Schur, analytic pose/point Jacobians): 380 s -> ~27 s; markers no longer Huber-downweighted;
  check markers excluded from the adjustment.
- User flags (80 control / 8 check): control RMSE 0.251 m; check 2.35 m, dominated by 29 (5.90 m), 36 (2.57),
  25 (1.61) – Metashape: 5.92 / 2.48 / 1.64 (same bad markers).
- Holdout every 5th (64 control / 24 check): independent check RMSE on 16 good markers 0.165 m (Helmert-only 0.689 m).
- Only 6 control markers: check RMSE over 74 good markers 0.337 m (median 0.093 m).
- Auto-placement (6-control model, 75 markers, 2285 pinned projections, 47 s): prediction median 1.62 px
  (<=2 px 59%); checkerboard refinement + consensus median 0.88 px (<=2 px 80%); one-click template median
  1.03 px (<=2 px 85%, <=5 px 99%).

## Final checks (13:25)
- Box: 74 passed, 5 skipped (COLMAP/GPU tests). Windows: 79 passed (incl. PM_TEST_DENSE GPU test).
- e2e test fixes: face thresholds (Low preset = points/45 -> ~1k faces on the 11-photo set), Delaunay output now model.ply.
- UI smoke on the real project: opens with 98 markers, 27.8M-pt cloud, 1.85M-face mesh in 88 s (slow PLY load);
  photo overlay shows pinned + predicted markers.
- Code synced box -> PC (SHA-256, 41 files, 0 mismatches). No leftover colmap/python processes
  (only the user's pythonw main.py from 10:13).

## 14:15 — Bug fix: Tools > Optimize Cameras "OSError [Errno 22] Invalid argument" in ply.write
- Root cause: georef.transform_ply read the PLY with plyfile's default memory map, then reopened the same path 'wb'.
  Windows cannot truncate a mapped file -> Errno 22. Reproduced outside the GUI (repro_ply.py). The GUI wasn't the cause.
- Second bug: run_task wrote the georeferenced sparse/0 BEFORE transforming the PLYs, so the failure left sparse/0
  georeferenced while the cloud and mesh were still local.
- Fix: transform_ply streams binary PLYs in 2M-vertex chunks (no mmap) into <file>.georef_tmp, fsyncs, then os.replace
  (with retries); faces are copied verbatim; CRLF headers are supported. run_task is now transactional (stage the model
  in sparse/0_georef_tmp plus all PLY temps, then commit; any error rolls back and removes the temps).
  plyio.load_ply and pcfilter.read_cloud no longer memory-map (fast numpy reader, plyfile fallback).
- Data: PLYs and mesh untouched (13:09/13:14/12:49 files unchanged). sparse/0 restored from sparse/0_original (hash-identical).
- E2E on a full copy (bench_drone5/georef_e2e): exit 0 in 43 s (it was 640 s via plyfile). All 3 PLYs transformed (no NaNs, counts unchanged).
  Control RMSE 0.251 m (n=80), check 2.350 m (n=8); tie points 620,917, reprojection RMS 1.251 px.
- Tests: box 87 passed/5 skipped; PC 92 passed. Code synced (41 files, 0 mismatches).

## 14:45 — Bug fix: File > Save froze the window ("Not responding", ~2 min CPU)
- Root cause: save_project wrote the .pmsx (0.2 s) and then called refresh_all(). That refilled the 641-row Reference table,
  whose columns were QHeaderView.ResizeToContents, so every setItem re-measured the column. That's O(rows^2):
  116 s on the box, measured with cProfile (6553 setItem calls = 117.3 s). The same refill ran after every set_modified() (each edit).
- Fix: bulk_fill() (no resize/paint/signals during the fill, size columns once). Save writes only the file (atomic .tmp + os.replace)
  and updates the title. Save now takes 0.04 s on the real project; a pane refill takes 0.07 s.
- Open: PLYs, mesh normals and marker triangulation load in a background thread with a status-bar progress bar.
  Mesh normals are vectorised (pyqtgraph computed them in a Python loop during the first paint), the cloud is
  decimated to 2M points before any copies, marker positions are cached, and load_result reads counts from the header.
  PC (real project, offscreen): reload_view returns in 0.002 s, data is ready in 4.0 s, the longest UI stall is 0.37 s.
- Saved .pmsx (14:30:22, 1,145,340 B) is valid: 641 cams (640 aligned, identical to .bak), 98 markers / 2947 projections,
  cloud + mesh refs, georef empty (consistent with the restored local sparse/0).
- Tests: box 92 passed/5 skipped; PC 97 passed. Code synced (42 files, 0 mismatches).

## 2026-10-21 15:15 (Asia/Yerevan) - Layer toggles, Metashape-like toolbar/workspace, processing metadata
- Layer visibility: one shared checkable QAction per layer (Tie Points, Point Cloud, Model, Cameras,
  Camera Labels, Markers, Marker Labels, Grid, Region, Trackball) used by Model > Show/Hide Items,
  View > Layers, the main toolbar, the eye "Show/Hide Items" dropdown and the Workspace checkboxes.
  The states persist in QSettings (`view/show_<key>`).
- Display modes: Model - Shaded/Solid/Textured/Wireframe solo the mesh; Point Cloud - Colors/Solid
  solo the cloud; Tie Points - Colors solo the tie points. The toggles follow. `show_layer` reuses the
  loaded arrays (no load_ply on the GUI thread).
- Main toolbar: one 16 px row with dropdowns (selection tools, point-cloud modes, model modes, show items).
  Icons are original QPainter drawings (photomesh/icons.py).
- Workspace: a pane toolbar (add chunk / photos / folder, enable/disable, remove) and Metashape-style rows
  (Workspace / Chunk [R] / Images a/n aligned / Markers / Tie Points / Depth Maps / Point Cloud / 3D Model).
  Only existing products get a row. Properties sit below the tree and fill on selection.
- Processing metadata: chunk.meta; procmeta.record runs after each step (align, dense, model,
  filter, optimize cameras). The backfill from logs happens on open, in memory only (saved on the next
  Save); unknown values stay None ("unknown").
- 3D view: "Perspective 30°"/"Orthographic" label top-left, "Snap: …" top-right (from Edit > * Snap),
  XYZ gizmo bottom-right, dark grey background, faint grid; markers drawn on top of the mesh.
- Tests: tests/test_layers_workspace.py (11). Box: 103 passed, 5 skipped (COLMAP-only).
- Screenshot: /workspace/photomesh-studio_screenshot.png

## 2026-10-21 15:35 (Asia/Yerevan) - Larger display-mode buttons
- New photomesh/toolbuttons.py ModeButton: split button (MenuButtonPopup), 20 px icon, short label,
  16 px arrow area with a separator and a painted arrow; it follows mode changes from menus or code.
- Toolbar: separate Tie Points / Point Cloud (Colors, Solid) / Model (Shaded, Solid, Textured, Wireframe)
  mode buttons; the selection-tool button keeps a 16 px icon but gets the wide arrow; other icons stay 16 px.
- Test: test_mode_buttons_are_large_split_buttons. Box 104 passed, 5 skipped.
- Screenshots: /workspace/toolbar_modes.png (1:1), toolbar_modes_2x.png, toolbar_modes_menu.png.

## 2026-10-21 16:05 (Asia/Yerevan) - Portable offline package for another laptop
- packaging/build_portable.ps1 (run on the dev PC) produces C:\Users\Behdad\PhotoMeshStudio\PhotoMeshStudio_portable.zip
  containing: app code, a copy of CPython 3.13.15 x64, a wheelhouse (74 wheels = pip freeze of the dev venv),
  tools\colmap (3.8 CUDA) + tools\glomap with app-local VC++ DLLs, redist\vc_redist.x64.exe,
  install_offline.bat (venv from the wheelhouse with --no-index, MAX_PATH check, COLMAP/GLOMAP/NVIDIA checks,
  shortcut), "PhotoMesh Studio.bat", create_shortcut.ps1, run_tests_offline.bat, INSTALL_OTHER_LAPTOP.md (EN + FA).
- App: bundled tools\colmap is preferred over PATH; NVIDIA driver detection (nvcuda.dll) -> CPU-only mode
  (GPU SIFT off, dense refused with a message, status bar shows the mode); Tools > Change Image Paths...
  (also in the Workspace context menu) relinks photos by file name; Project.load rebases product paths when
  the project folder was moved. Tests: tests/test_portability.py.


## 16:50 — Add Photos freeze on the other laptop (641 × 22 MB DJI photos)
Cause: old Add Photos ran everything inside the GUI call — a spawned process pool decoded all 641
full-size JPEGs (thumbnail + descriptor) while the window only pumped events from the progress
callback; if spawn workers were slow/killed (AV, few cores) every image "failed" and the fallback
ran read_exif_gps per photo and the Photos pane decoded every 22 MB original on the GUI thread
(78 s blocked here; minutes on a laptop). 10 photos = fast, 641 = "hang".
Fix: header-only scan (threads, cancellable dialog) → cameras/panes → background thumbnail job
(status bar progress + ✕); Photos pane never decodes originals; diag log + hang watchdog;
Help > Open Log Folder; Preferences "Use built-in file dialogs".
PC repro (641 DJI, read-only F:): add_photos returns 0.39 s (header scan 0.33 s, panes 0.05 s),
max GUI gap 0.10 s, thumbnails done in background in 16.7 s (cold). Box 119 passed/5 skipped,
PC 124 passed.
