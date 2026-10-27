# Import Shapes - progress notes (2026-10-21)

## Menus
- File > Import > Import Shapes...  (SHP, DXF R12/2000+ ASCII, KML/KMZ, GeoJSON, CSV)
- File > Export > Export Shapes...  (SHP / DXF / GeoJSON, world coordinates)
- Tools > Shapes > Measure Shape... / Remove Shapes
- Model > Show/Hide Items > Show Shapes (mirrored in the Photo and Ortho menus), toolbar "Shapes" toggle
- Workspace "Shapes (N)" > layer rows (checkbox = visibility) > shapes; right-click: Export Shapes..., Remove Layer, Remove Shapes, Measure Shape...

## Behaviour
- Files are in world coordinates; local = world - project offset. Far-away check offers
  Add Project Offset / Treat as WGS 84 / Import Anyway / Cancel.
- KML/KMZ are WGS 84 -> project CRS (UTM/WGS84); a local project gets a warning that it cannot be transformed.
- Shapes without Z: drape on the DEM / ortho height / mesh (mesh only for georeferenced chunks) or a fixed Z.
- Stored in <project>.shapes/<chunk>.json; the .pmsx keeps path, sources, and hidden layers.
- Overlays: model view (GL, on top), Ortho 2D (click selection), Photo tab: 3D vertices projected with
  pose + intrinsics + COLMAP distortion (incl. FULL_OPENCV, OPENCV_FISHEYE), densified segments,
  clipped behind the camera and at the frame, optional mesh occlusion (background raycast scene), cached per photo.
- Measurements: length/perimeter 2D/3D, area 2D/3D (surface), cut/fill/net volume above the best-fit plane.

## Real check (bench_drone5_shapes, a copy of the project)
- 98 GCP points (SHP), site polygon (SHP, draped), DXF 98 POINT + TEXT + route; 198 shapes, 0.7 s.
- DEM - GCP Z median -0.30 m, p90 |dz| 1.46 m. Marker 44 projected vs pinned 2.4 px (most 1-6 px).
- Exports SHP/DXF/GeoJSON re-read 198 shapes each; save/reload kept everything.

## Limitations
- No overlay on the thumbnail strip; binary DXF rejected; DXF R12 export writes holes as separate closed polylines.
- Without pyproj only WGS84/UTM/local transforms; the volume plane fits the boundary vertices.
- The Workspace shows at most 400 shape rows per layer.
