# Orthomosaic + DEM (v0.1.2 work)

## Menus
- Workflow > Build DEM...        source Point cloud / Mesh, pixel size (auto), interpolation Enabled / Disabled / Extrapolated, region
- Workflow > Build Orthomosaic... surface Mesh / DEM, projection = project CRS (top XY), blending Mosaic / Average / Disabled,
  hole filling, ghosting filter, refine seamlines, pixel size (auto = photo GSD) or max. dimension, region, photo resolution
- File > Export > Export Orthomosaic... / Export DEM...   GeoTIFF (LZW / Deflate / JPEG / none, alpha or nodata, overviews,
  BigTIFF auto above 4 GB), JPG / PNG with world file, resolution 1/2^k or max. dimension, split into tiles
- File > Import > Import Orthomosaic... / Import DEM...   PhotoMesh output folder (info.json) or a georeferenced GeoTIFF
- Model > Orthomosaic / DEM, toolbar layer toggles (ortho, DEM), Workspace rows "Orthomosaic (W x H, X cm/pix)" / "DEM (...)"
- Ortho tab: pyramid tiles loaded in the background; live cursor X / Y / Z in REAL world coordinates
  (overlay bottom-right + status bar; Z from the DEM, else the orthomosaic height grid); right-click > Copy Coordinates;
  double-click = fit; wheel = zoom; drag = pan.

## Output (chunk folder/orthomosaic)
orthomosaic.tif (RGBA, tiled 512, Deflate, overviews, GeoTIFF tags in world coordinates = local + offset),
height.tif (float32 world Z, 1/4 resolution), info.json. Exports add .tfw/.jgw/.pgw, .prj (real CRS only),
<name>_placement.txt (Rhino / AutoCAD / QGIS) and <name>_rhino_place.py.

## Real run (bench_drone5 fast, 640 photos, mesh 1.85 M faces)
25,053 x 37,130 px at 4.48 cm/pix (auto GSD), 603 s, 1.31 GB Deflate (LZW export 1.51 GB, 61 s).
DEM from the 27.8 M point dense cloud: 2,869 x 4,356 at 39.1 cm, 7.7 s.
GCP check (98 GCPs from GCP.txt): all inside; visually the targets sit under the GCP crosses.

## Known limitations
- JPEG-compressed GeoTIFF has no alpha (nodata 0, valid pixels clamped to >= 1).
- Export resolution in power-of-two steps (max. dimension picks the largest level that fits).
- Seam lines follow a 16 px cell grid; steep walls are stretched (true ortho on a 2.5D view of a mesh).
- Average blending is slower than Mosaic.
- Imported LZW GeoTIFFs are decoded with a pure-Python LZW decoder (slow for large files).
