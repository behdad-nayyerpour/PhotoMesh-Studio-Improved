# PhotoMesh Studio — changes / تغییرات

[English](#english) · [فارسی](#فارسی)

## English

### 0.1.15 (2026-10-27)

- Fixed File > New leaving the previous photo visible after the workspace becomes empty.
- Cleared stale Ortho points, markers, raster work and view position when creating or opening a project or switching chunks. Background view results from the previous project are ignored.
- Added a GUI regression test for clearing the Photo and Ortho tabs.
- No dependency changes; build a new Setup and Update EXE for this version.
- The release builder now defaults to a small app-only Update EXE and stops if the previous runtime differs. Bundling the full runtime requires an explicit option.

### 0.1.14 (2026-10-27)

- Keypoint selection now loads one image's feature and descriptor BLOBs from SQLite at a time, avoiding a memory spike proportional to the whole photo set. In a 40-image synthetic read, peak Python memory fell from about 22 MiB to 1.1 MiB; the read took 0.035 s instead of 0.021 s.
- When Windows cannot create photo links, unchanged staged photo copies are reused on later runs. Changed sources, replaced photos, and modified staged copies are refreshed.
- No dependency changes. The versioned update EXE contains the app changes; the full installer remains available for new installations.

### 0.1.13 (2026-10-27)

- Fixed CUDA detection when COLMAP fails to return a help banner: the app now reports CPU-only instead of assuming CUDA is available.
- Fixed reuse of a working folder after replacing a photo with another file of the same name; COLMAP now reads the current photo.
- New Windows setup and versioned update EXE build. The full installer includes Python, COLMAP, and GLOMAP; code-only updates check runtime compatibility and include the runtime when it has changed.
- Updated issue and update links for the new independent repository. The original repository is unchanged.
- Fixed first-run launcher behavior after a failed setup and made Windows path/link tests portable.

### 0.1.12 (2026-10-25)

- The loading screen (splash) now appears right after start, about 0.2 s instead of about 1.3 s, and stays until the main window is up.
  - Cause: Qt's built-in splash screen waits up to 1 s inside its own "show" step before it can appear. The splash is now a plain window that shows at once.
  - The system information line in the log (Python, NumPy, Pillow, GPU) is written after the splash is visible, not before it.
  - The main window is also ready about 1 s sooner.
- Simpler menus: every command that only said "not implemented yet" was removed, about 240 entries.
  - Examples: File > Cloud, masks, selection and drawing tools, region and transform tools, basemaps, laser scans, classification, batch processing, most Tools sub-menus, and the empty Timeline and Animation panes.
  - The Edit > Snap toggles were removed too; they did not snap anything.
  - Duplicate entries were removed: File > Import > Import Reference (same as Import Markers), Help > Online Knowledge Base (same as Contents), Model > DEM and Model > Orthomosaic (they remain in the Ortho menu).
  - The toolbar's navigation/selection button was removed; selection tools never existed, and navigation is always on.
  - Nothing that does work was removed. Processing (align, point cloud, mesh, texture, tiled model, DEM, orthomosaic, markers/GCPs, export) is unchanged. Shortcuts, saved view settings and projects work as before.
- Desktop and Start menu shortcut: it now uses the PhotoMesh Studio icon (photomesh\resources\photomesh.ico), not the Python icon.
  - An existing "PhotoMesh Studio.lnk" with the wrong icon is fixed automatically, once, the first time 0.1.12 starts, and by later updates. Only the icon is changed.
- No new dependencies. What to do: run update.bat.

### 0.1.11 (2026-10-24)

- Fixed: rebuilding the orthomosaic of a georeferenced project stopped with "exit 0x00000001 (generic failure)" / "Orthomosaic would be 197054x189531 px (37.3 Gpx)" (GitHub issue #3).
  - Cause: the texture had been built before Optimize Cameras (georeferencing) and kept its photo resolution in the old model units. Build Orthomosaic used that value as the default pixel size, about 10x too fine, and the size was only checked after the task had started.
  - Now Optimize Cameras rescales the texture's photo resolution and texel size to the new frame. The orthomosaic, DEM and tiled model built in the old frame are marked "outdated, rebuild".
  - The default pixel size comes from the cameras; the texture's value is used only when it agrees with them.
- The Build Orthomosaic dialog shows the total size in pixels (like Metashape) and warns when it is too large or finer than the photos. A size that cannot be built is caught before the task starts, with a suggested pixel size ("Use ..." or Cancel).
- The orthomosaic extent ignores stray geometry far outside the photos (mesh outliers), so a few bad triangles no longer make the raster huge. A Region still decides the extent when it is set.
- When a processing step stops with its own message, the error dialog and the report title show that message instead of "exit code 1 (generic failure)".
- A failed or cancelled rebuild of the orthomosaic or DEM shows the previous result again (it was never deleted).
- No new dependencies. What to do: run update.bat, then run Build Orthomosaic again. The dialog now proposes the right pixel size (about 2-3 cm for this project, not 0.0021 m). Rebuild the DEM and tiled model too if they were made before the last Optimize Cameras.

### 0.1.10 (2026-10-23)

- Build Model no longer loses the existing model. Prerequisites are checked first. The new mesh is built into a temporary file and replaces model.ply only when it is complete and has faces. A failed or cancelled build keeps the old model and its texture/product info.
- After a successful rebuild the old texture is dropped, and the tiled model, orthomosaic and DEM made from the old model are marked "outdated, rebuild" in the Workspace.
- Photos with the same file name in different folders (e.g. two IMG_001.JPG) are now matched to the right camera. Texture, orthomosaic and DEM used one photo for both and skipped the other. After Optimize Cameras / georeferencing both got the same pose. The exact link is saved in the project and survives save/open, moving the project folder and Change Image Paths.
- Older projects: the link is recovered from the working folder when possible. Otherwise photos that cannot be told apart are left out with a warning ("run Align Photos again") instead of being mismatched.
- Saving copies no longer re-links a photo copy to another photo with the same name and size.
- The active chunk is restored when a project is opened (it always came back as the first chunk). Invalid values and files without chunks are handled.
- Note: projects saved with 0.1.10 cannot be opened by 0.1.9 or older (rollback).
- No new dependencies. What to do: run update.bat. Projects whose photos share file names: run Align Photos again if the warning appears.

### 0.1.9 (2026-10-23)

- Save As into a sub-folder of the old data folder no longer copies the copy into itself ("File name too long").
- Build Point Cloud: new "Keep depth maps" option, off by default (like Metashape). After fusion the depth/normal maps and consistency graphs are deleted, but only once the point cloud has been checked as written. "Reuse depth maps" still works when they were kept.
- Save As shows a size breakdown (results, depth maps, photo copies). It offers "Copy results only (skip depth maps, can be recomputed)", the default, or "Copy everything".
- New Tools > Clean Up Intermediate Data...: lists the size of each part with checkboxes and asks before deleting. Finished products and photos are never deleted.
- Plain copies of photos in images\ are linked back to the original photos instead of being copied again, where possible.
- Saving a project inside a OneDrive, Dropbox or Google Drive synced folder shows a warning and suggests a local folder. You can still continue.
- A stale chunkN.partial or incomplete copy left by this app in the destination is detected, and you are asked before it is deleted.
- No new dependencies.
- What to do: run update.bat. On the next Save As of Test-Lot3-Drone, accept deleting the old chunk0.partial (or delete it yourself). Use Tools > Clean Up Intermediate Data... to free the depth maps of old projects. Prefer a local, non-synced folder for projects.

### 0.1.8 (2026-10-23)
Needs **no new Python packages**. Update with `update.bat`.

- **Fixed: Save As stopping with "Copying the project data failed … PermissionError: [WinError 5] Access is denied … chunk0.partial -> chunk0"** (GitHub issue #2).
  - **Cause:** the project data was copied into `chunk0.partial` and the folder was renamed at the end. In a OneDrive (or Dropbox) folder the sync client, and often the virus scanner, still has the new files open, and Windows then refuses to rename the folder.
  - **Now:** the data is copied straight into its final folder (`<name>.files\chunk0`); nothing is renamed afterwards. Until the copy is complete the folder holds `PHOTOMESH_COPY_INCOMPLETE.txt`. If a copy is cancelled or fails, that folder is removed again and the project keeps using its current data. A later attempt reuses an unfinished copy; a folder with other files is never overwritten.
  - Files that are locked for a moment (sync, virus scanner, indexer) are retried a few times. Read-only files no longer block the clean-up. If something cannot be removed, the error message names the folder.
  - A `chunk0.partial` folder left by 0.1.7 inside `<name>.files` is not used and can be deleted.

### 0.1.7 (2026-10-23)
Needs **no new Python packages**. Update with `update.bat`.

- **Project data lives next to the project file.**
  - Everything a project computes goes into `<name>.files` beside `<name>.pmsx`, one folder per chunk: alignment, point cloud, mesh, texture, tiles, orthomosaic, DEM, shapes and caches.
  - **Save the project before processing.** Align Photos and every other processing step asks you to save an unsaved project first, with the Save As dialog right there. Cancel = the step does not start. Temporary folders are no longer used.
  - The `.pmsx` stores paths **relative to its folder**, so the whole project folder can be copied or moved to another disk or computer.
  - Photos are referenced, never copied: relative when they are inside the project folder, otherwise absolute.
  - **Save As** to a new place offers to copy the project data into the new `<name>.files`. It checks free disk space first and shows progress with Cancel; a cancelled or failed copy leaves nothing behind. The originals are kept; only data in the old temporary folder of an unsaved project is moved.
  - **Older projects** open as before. On the next Save their paths are written relative. Data outside the project folder can be copied in or left where it is (you are asked). Data already inside the project folder, such as `bench_drone5\fast`, stays where it is.
  - Photos not found when a project opens: you are asked to locate their folder (Change Image Paths).
- **GCPs in the orthomosaic view.** Right-click > **Add Marker** in the Ortho tab places a marker at that point. Its X/Y come from the raster's georeferencing and its height from the DEM or the orthomosaic height (otherwise from the model below). It is projected into every photo that sees it, like markers added on photos or on the model. Then enter its reference coordinates, refine or pin it on photos, and run Optimize Cameras as usual. Markers without coordinates are now also shown in the Ortho tab.
- **CPU and GPU load in the status bar**, updated every 1.5 s in the background.
  - The GPU value (and GPU memory) comes from the NVIDIA driver (NVML) and is hidden without an NVIDIA GPU. `nvidia-smi` is never started.
- **Busy indicator.** While a step runs, the mouse shows the busy cursor and the status bar shows "Working: <step>..." with a small progress bar. The window stays usable; the cursor comes back after success, error or cancel.
- **Window title** shows the version: `PhotoMesh Studio 0.1.7 - <project>`.
- **App icon** (our own design) in the title bar, the taskbar (own taskbar button on Windows) and the desktop shortcut.
- **Splash screen** with progress messages while the program starts.

### 0.1.6 (2026-10-23)
Needs **no new Python packages**. Update with `update.bat`.

- **Fixed: Build Tiled Model failing with "texture atlas packing failed"** (exit code 1, GitHub issue #1).
  - **Cause:** on dense drone meshes the coarser levels of detail split into thousands of tiny texture pieces. Every piece needs a small border, whatever its size, and the tile texture was sized without counting those borders. The pieces could then never fit, however much they were shrunk.
  - **Now:** the tile texture size counts the borders, grows when needed (up to the maximum texture size), and as a last resort gives every triangle its own small cell. Coarse levels always finish; leaf tiles keep their full detail.
  - **Build Texture** has the same safety net: if the pieces do not fit in the chosen number of pages, more pages are used instead of failing.
- **Fixed: `setup_git_update.bat` stopping with "pms_update_setup.ps1 does not exist"** when the folder was already a git checkout without `packaging\update.ps1` (an older or unfinished checkout; the 0.1.4 portable zip has no `packaging` folder).
  - It now adds the GitHub remote if missing, downloads, and sets the program files to the GitHub version. Changed program files are kept in `git stash`; `.venv`, projects and other data are not touched. Then it runs the normal update.
  - If the download fails, the existing checkout is left as it was.

### 0.1.5 (2026-10-23)
Needs **no new Python packages**. Update with `update.bat`.

- **Error reports.** Every crash or failed step now opens one error dialog and saves a report on this computer: `%LOCALAPPDATA%\PhotoMeshStudio\reports\report_YYYYMMDD_HHMMSS.md`, plus a `.zip` with the logs.
  - **Covered:** COLMAP/GLOMAP or other step processes that end with an error code, Python errors in commands and background workers, threads and QThreads, out-of-memory (MemoryError), and serious Qt errors (these are saved without a dialog).
  - **Contents:**
    - time, app version and git commit;
    - the step and its settings, and the command line;
    - the exit code in decimal and hex, with its Windows meaning (e.g. 0xC0000409 = fail-fast, usually out of memory; 0xC0000005 = access violation);
    - the traceback and the last 300 lines of that step's log;
    - project statistics: photos, aligned, products, CRS name;
    - system info: Windows version, CPU, threads, RAM, GPU/VRAM/driver (nvidia-smi), free disk, Python and package versions, COLMAP version.
  - **Privacy:**
    - no photos and no coordinates (only the CRS name);
    - the user folder becomes `%USERPROFILE%`, and project and photo folders become `<project>` / `<photos>`; other paths keep only the drive and the file name;
    - user and computer names are removed.
- **Error dialog** with **Details**, **Copy report**, **Open report folder** and **Report to GitHub**. The dialog also has an optional box for "What were you doing?".
  - **Report to GitHub** opens your browser at a new GitHub issue with the title and report filled in (label `bug`). Nothing is sent until you press *Submit*; the app itself never sends anything and contains no token.
  - If the report is too long for a link, it is shortened and asks you to drag the `.zip` in.
  - No internet? The report stays saved; send it later.
- **Help > Report a Problem...** makes a report of the current session at any time.
- GitHub issue template (`.github/ISSUE_TEMPLATE/bug_report.md`) in the same format.
- The issue address is a setting in `photomesh/report.py` (`ISSUES_URL`; `PHOTOMESH_ISSUES_URL=off` hides the button).

### 0.1.4 (2026-10-22)
Needs **no new Python packages**. The update zip goes over an existing install, just like 0.1.3.

- **Fixed: Build Point Cloud failing in "Building point cloud (fusion)" with exit code -1073740791 (0xC0000409).**
  - **Cause:** at least one depth or normal map was an empty or cut-off file, for example after a full disk or a cancel while COLMAP was writing. COLMAP's fusion then stops with "Check failed: width_ > 0". COLMAP's own resume treats such a file as finished.
  - **Now:** before depth maps and before fusion, every map is checked (header, size, file length). Unusable maps are removed and **only those views** are recomputed, together with the neighbour photos they need. All valid depth maps are kept.
  - Views that still have no valid map are left out of the fusion and listed in the Console.
- **Disk-space check before depth maps.** Ultra high on 640 DJI photos needs about 400 GB while the depth maps are built. The app now stops at the start with a clear message instead of failing hours later.
- **Fusion is memory-aware.**
  - COLMAP's default loaded every full-size depth map, normal map and photo at once: about 250 GB for 640 Ultra-high views.
  - The fusion size, threads and cache are now chosen from the free RAM; the log line "Fusion plan" shows them.
  - After a crash the fusion is retried automatically with less memory. The depth maps are never recomputed for that.
- **Resume:** if a Build Point Cloud with the same settings did not finish, the next run reuses its depth maps, even if "Reuse depth maps" is not ticked.
- **Progress:**
  - The depth-map step shows the photometric and geometric pass, the view number and the remaining time.
  - Fusion shows the image number and the points so far.
  - The bar moves in 0.1 % steps.
- Clearer error messages for COLMAP crashes (exit code explained).
- **Updates from GitHub:** `update.bat` downloads and installs a new version (no more zips), `rollback.bat` goes back to an earlier one, and `setup_git_update.bat` switches an existing install over once. `.venv` and projects are kept. See UPDATING.md.

### 0.1.3 (2026-10-22)
Needs **no new Python packages** (same 74 pinned wheels as 0.1.2). An existing portable install can take the update zip; a new laptop uses the full portable zip.

- **Exports:** model and point cloud export in local coordinates (DXF, PLY) for CAD/Rhino, plus OBJ, glTF and 3D Tiles. Build Texture and Build Tiled Model are included too.
- **Orthomosaic and DEM:** Build Orthomosaic and Build DEM, an Ortho tab, and GeoTIFF + world file output, with placement notes and a Rhino placement script.
- **Import Shapes:** DXF, SHP, GeoJSON, KML and CSV, shown in the 3D view, on the orthomosaic and on every photo.
  - Per-layer and per-shape style: colour, width, dashed lines, fill and fill transparency, point symbols and labels.
  - Hidden parts are drawn dashed in the transparent view.
- **Markers and GCPs:** place and pin markers manually on photos, with predicted positions, a Reference pane and marker refinement.
- **Shading and overlay:** lighting for the 3D view, and readable labels with a background.
- **Toolbar:** original icons, 30 % larger (21 px); marker and shape toggles in the Photo tab.
- **Fixed: Build Point Cloud "MemoryError" during undistortion.**
  - Undistortion maps are now float32 and shared between workers.
  - The number of workers is capped by free RAM.
  - A failed image is retried on its own.
  - An interrupted run resumes where it stopped.
  - The log shows the peak memory.
- **Fixed:** the "Tie point statistics: … no attribute 'sparse_stats'" error when opening a project. Properties now shows projections, mean projections per photo and mean track length. Opening a project no longer marks it as modified.
- **Faster:** drawing shapes on a photo now takes about 0.16 s instead of 7–13 s for about 6000 shapes.
  - Projection is vectorised.
  - Styles and geometry are cached per version.
  - Occlusion is computed in the background after a fast first draw.
  - Lines are drawn as one path per pen.

<div dir="rtl">

## فارسی

### 0.1.15 (2026-10-27)

- پس از File > New، عکس پروژهٔ قبلی از نمای Photo پاک می‌شود.
- نقاط سفید و سایر داده‌های نمای Ortho هنگام ساختن یا بازکردن پروژهٔ جدید و تغییر Chunk پاک می‌شوند؛ نتیجهٔ بارگذاری قدیمی هم دیگر روی پروژهٔ جدید اعمال نمی‌شود.
- آزمون بازگشتی برای خالی‌شدن نماهای Photo و Ortho اضافه شد.
- وابستگی جدیدی لازم نیست؛ برای این نسخه فایل‌های Setup و Update تازه بسازید.
- ابزار ساخت اکنون به‌طور پیش‌فرض آپدیت کوچکِ فقط شامل برنامه می‌سازد و اگر محیط اجرایی با نسخهٔ قبل فرق کند، متوقف می‌شود. افزودن کل محیط اجرایی به آپدیت به گزینهٔ صریح نیاز دارد.

### 0.1.14 (2026-10-27)

- انتخاب نقاط کلیدی اکنون داده‌های هر عکس را جداگانه از SQLite می‌خواند تا حافظه با تعداد کل عکس‌ها رشد نکند. در نمونهٔ آزمایشی ۴۰عکسی، اوج حافظهٔ مرحلهٔ خواندن از حدود ۲۲ به ۱٫۱ مگابایت رسید.
- اگر ویندوز اجازهٔ ساخت لینک عکس را ندهد، کپی‌های بدون تغییر در اجرای بعدی دوباره کپی نمی‌شوند. با تغییر عکس اصلی یا فایل آماده‌شده، کپی تازه ساخته می‌شود.
- وابستگی جدیدی اضافه نشده است؛ فایل آپدیت نسخه‌دار فقط تغییرات برنامه را حمل می‌کند.

### 0.1.13 (2026-10-27)

- تشخیص CUDA در صورت پاسخ ندادن COLMAP اصلاح شد.
- اگر عکسی با نام تکراری جایگزین شود، اجرای بعدی از فایل جدید استفاده می‌کند.
- ساخت یک فایل نصب EXE و یک فایل به‌روزرسانی EXE برای هر نسخه اضافه شد؛ فایل نصب شامل پایتون، COLMAP و GLOMAP است.
- پیوندهای گزارش خطا و به‌روزرسانی به مخزن جدید منتقل شدند؛ مخزن اصلی تغییری نکرد.
- اجرای برنامه پس از نصب اولیهٔ ناموفق متوقف می‌شود.

### نسخهٔ 0.1.12 (۱۴۰۵/۰۸/۰۳ — 2026-10-25)

- صفحهٔ بارگذاری (اسپلش) حالا بلافاصله بعد از اجرا ظاهر می‌شود: حدود ۰٫۲ ثانیه به جای حدود ۱٫۳ ثانیه. تا بالا آمدن پنجرهٔ اصلی هم می‌ماند.
  - علت: صفحهٔ اسپلش آمادهٔ Qt پیش از نمایش، تا ۱ ثانیه در مرحلهٔ «نمایش» خودش منتظر می‌ماند. حالا اسپلش یک پنجرهٔ سادهٔ خودمان است که فوراً نشان داده می‌شود.
  - سطر اطلاعات سیستم در لاگ (پایتون، NumPy، Pillow، GPU) بعد از نمایش اسپلش نوشته می‌شود، نه قبل از آن.
  - پنجرهٔ اصلی هم حدود ۱ ثانیه زودتر آماده می‌شود.
- منوها ساده‌تر شدند: همهٔ فرمان‌هایی که فقط «not implemented yet» نشان می‌دادند حذف شدند (حدود ۲۴۰ مورد).
  - نمونه‌ها: File > Cloud، ماسک، ابزارهای انتخاب و ترسیم، ابزارهای Region و Transform، نقشهٔ پایه، اسکن لیزری، طبقه‌بندی، Batch Process، بیشتر زیرمنوهای Tools و پنل‌های خالی Timeline و Animation.
  - گزینه‌های Edit > Snap هم حذف شدند؛ کاری انجام نمی‌دادند.
  - موارد تکراری هم حذف شدند: Import Reference (همان Import Markers)، Online Knowledge Base (همان Contents)، Model > DEM و Model > Orthomosaic (در منوی Ortho هستند).
  - دکمهٔ ناوبری/انتخاب نوار ابزار حذف شد؛ ابزار انتخابی پیاده نشده بود و ناوبری همیشه فعال است.
  - هیچ فرمانی که کار واقعی انجام می‌دهد حذف نشده است. روند پردازش (هم‌ترازی، ابر نقاط، مش، بافت، مدل کاشی‌شده، DEM، اورتوموزاییک، مارکرها و GCP، خروجی) تغییری نکرده است. میان‌برهای صفحه‌کلید، تنظیمات ذخیره‌شدهٔ نما و پروژه‌ها مثل قبل کار می‌کنند.
- میان‌بر دسکتاپ و منوی Start حالا آیکون PhotoMesh Studio را دارد (photomesh\resources\photomesh.ico)، نه آیکون پایتون.
  - اگر «PhotoMesh Studio.lnk» موجود آیکون اشتباه داشته باشد، اولین بار که نسخهٔ 0.1.12 اجرا شود خودکار اصلاح می‌شود (یک بار). به‌روزرسانی‌های بعدی هم آن را اصلاح می‌کنند. فقط آیکون تغییر می‌کند.
- وابستگی جدیدی اضافه نشده است. کار لازم: update.bat را اجرا کنید.

### نسخهٔ 0.1.11 (۱۴۰۵/۰۸/۰۲ — 2026-10-24)

- رفع شد: ساخت دوبارهٔ اورتوموزاییک در پروژهٔ زمین‌مرجع با خطای «exit 0x00000001 (generic failure)» و «Orthomosaic would be 197054x189531 px (37.3 Gpx)» متوقف می‌شد (گزارش شمارهٔ ۳ در GitHub).
  - علت: بافت قبل از Optimize Cameras (زمین‌مرجع کردن) ساخته شده بود و دقت عکس (GSD) را با واحد قدیمی مدل نگه داشته بود. «ساخت اورتوموزاییک» همین عدد را اندازهٔ پیش‌فرض پیکسل می‌گرفت که حدود ۱۰ برابر ریزتر از مقدار درست بود. اندازهٔ خروجی هم فقط بعد از شروع کار بررسی می‌شد.
  - حالا Optimize Cameras دقت عکس و اندازهٔ تکسل بافت را به دستگاه جدید تبدیل می‌کند. اورتوموزاییک، DEM و مدل کاشی‌شده‌ای که در دستگاه قبلی ساخته شده‌اند «قدیمی، دوباره بسازید» علامت می‌خورند.
  - اندازهٔ پیش‌فرض پیکسل از روی دوربین‌ها حساب می‌شود و عدد بافت فقط وقتی با آن هم‌خوان باشد استفاده می‌شود.
- پنجرهٔ «ساخت اورتوموزاییک» حالا مثل Metashape اندازهٔ کل خروجی را به پیکسل نشان می‌دهد و اگر خیلی بزرگ یا ریزتر از عکس‌ها باشد هشدار می‌دهد. اندازه‌ای که ساختنش ممکن نیست قبل از شروع کار تشخیص داده می‌شود و یک اندازهٔ پیکسل مناسب پیشنهاد می‌شود («Use ...» یا Cancel).
- محدودهٔ اورتوموزاییک هندسهٔ پرتی را که خیلی بیرون از پوشش عکس‌ها است (مثلاً مثلث‌های اضافی مش) نادیده می‌گیرد. اگر Region تعیین شده باشد، همان محدوده را تعیین می‌کند.
- وقتی یک مرحلهٔ پردازش با پیام مشخصی متوقف می‌شود، پنجرهٔ خطا و عنوان گزارش همان پیام را نشان می‌دهند، نه «exit code 1 (generic failure)».
- اگر ساخت دوبارهٔ اورتوموزاییک یا DEM ناموفق باشد یا لغو شود، نتیجهٔ قبلی دوباره نمایش داده می‌شود (هیچ‌وقت حذف نشده بود).
- بستهٔ جدیدی لازم نیست. کار لازم: update.bat را اجرا کنید و دوباره Build Orthomosaic بزنید. پنجره حالا اندازهٔ پیکسل درست را پیشنهاد می‌دهد (برای این پروژه حدود ۲ تا ۳ سانتی‌متر، نه 0.0021 متر). اگر DEM و مدل کاشی‌شده قبل از آخرین Optimize Cameras ساخته شده‌اند، آن‌ها را هم دوباره بسازید.

### نسخهٔ 0.1.10 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)

- «ساخت مدل» دیگر مدل موجود را از دست نمی‌دهد. پیش‌نیازها اول بررسی می‌شوند. مش جدید در یک فایل موقت ساخته می‌شود و فقط وقتی کامل باشد و وجه داشته باشد جای model.ply را می‌گیرد. اگر ساخت ناموفق باشد یا لغو شود، مدل قبلی و اطلاعات بافت و محصولاتش حفظ می‌شود.
- پس از ساخت موفق، بافت قدیمی کنار گذاشته می‌شود و مدل کاشی‌شده، ارتوموزاییک و DEM ساخته‌شده از مدل قبلی در فضای کار با عبارت «outdated, rebuild» (قدیمی است، دوباره بسازید) علامت می‌خورند.
- عکس‌های هم‌نام در پوشه‌های مختلف (مثلاً دو IMG_001.JPG) اکنون به دوربین درست وصل می‌شوند. پیش‌تر بافت، ارتوموزاییک و DEM برای هر دو از یک عکس استفاده می‌کردند و دیگری را کنار می‌گذاشتند. پس از بهینه‌سازی دوربین‌ها / زمین‌مرجع‌سازی هم هر دو یک موقعیت می‌گرفتند. پیوند دقیق در پروژه ذخیره می‌شود و با ذخیره/باز کردن، جابه‌جایی پوشهٔ پروژه و «تغییر مسیر تصاویر» حفظ می‌شود.
- پروژه‌های قدیمی: در صورت امکان پیوند از پوشهٔ کاری بازیابی می‌شود. در غیر این صورت عکس‌هایی که قابل تشخیص نیستند با هشدار («Align Photos را دوباره اجرا کنید») کنار گذاشته می‌شوند تا اشتباه وصل نشوند.
- ذخیرهٔ کپی دیگر کپی یک عکس را به عکس دیگری با همان نام و حجم پیوند نمی‌دهد.
- هنگام باز کردن پروژه، چانک فعال درست بازیابی می‌شود (همیشه چانک اول باز می‌شد). مقدارهای نامعتبر و فایل‌های بدون چانک هم مدیریت می‌شوند.
- توجه: پروژه‌ای که با 0.1.10 ذخیره شود با 0.1.9 یا قدیمی‌تر باز نمی‌شود (بازگشت نسخه).
- بدون وابستگی جدید. کار لازم: update.bat را اجرا کنید. در پروژه‌هایی که عکس‌های هم‌نام دارند، اگر هشدار دیدید Align Photos را دوباره اجرا کنید.

### نسخهٔ 0.1.9 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)

- «ذخیره به‌عنوان» در زیرپوشه‌ای از پوشهٔ دادهٔ قبلی دیگر نسخهٔ کپی را درون خودش کپی نمی‌کند (خطای «نام فایل بیش از حد طولانی»).
- ساخت ابر نقاط: گزینهٔ جدید «نگه‌داشتن نقشه‌های عمق» (پیش‌فرض خاموش، مانند Metashape). پس از ادغام، نقشه‌های عمق و نرمال و گراف‌های سازگاری حذف می‌شوند، ولی فقط پس از اطمینان از نوشته‌شدن ابر نقاط. «استفادهٔ مجدد از نقشه‌های عمق» در صورت وجود آن‌ها همچنان کار می‌کند.
- «ذخیره به‌عنوان» حجم هر بخش (نتایج، نقشه‌های عمق، کپی عکس‌ها) را نشان می‌دهد. می‌توانید بین «فقط کپی نتایج (بدون نقشه‌های عمق که قابل محاسبهٔ دوباره‌اند)»، که پیش‌فرض است، و «کپی همه‌چیز» انتخاب کنید.
- منوی جدید «ابزارها > پاک‌سازی داده‌های میانی...»: حجم هر بخش را با چک‌باکس نشان می‌دهد و پیش از حذف تأیید می‌گیرد. محصولات نهایی و عکس‌ها هرگز حذف نمی‌شوند.
- کپی‌های سادهٔ عکس‌ها در images\ در صورت امکان به عکس‌های اصلی پیوند داده می‌شوند، به‌جای این‌که دوباره کپی شوند.
- ذخیرهٔ پروژه در پوشهٔ همگام‌شدهٔ OneDrive، Dropbox یا Google Drive یک هشدار نشان می‌دهد و پوشهٔ محلی را پیشنهاد می‌کند. همچنان می‌توانید ادامه دهید.
- کپی ناتمام یا chunkN.partial قدیمی‌ای که خود برنامه در مقصد جا گذاشته شناسایی می‌شود و پیش از حذف از شما پرسیده می‌شود.
- بدون وابستگی جدید.
- کارهای لازم: update.bat را اجرا کنید. در «ذخیره به‌عنوان» بعدیِ Test-Lot3-Drone حذف chunk0.partial قدیمی را بپذیرید (یا خودتان پاکش کنید). با «ابزارها > پاک‌سازی داده‌های میانی...» نقشه‌های عمق پروژه‌های قدیمی را پاک کنید. پروژه‌ها را ترجیحاً در یک پوشهٔ محلیِ غیرهمگام ذخیره کنید.

### نسخهٔ 0.1.8 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)
به **بستهٔ پایتونی جدیدی نیاز ندارد**. با `update.bat` به‌روزرسانی کنید.

- **رفع خطا: Save As با پیام «Copying the project data failed … PermissionError: [WinError 5] Access is denied … chunk0.partial -> chunk0» متوقف می‌شد** (Issue شمارهٔ ۲ در GitHub).
  - **علت:** داده‌های پروژه در `chunk0.partial` کپی می‌شد و در پایان نام پوشه تغییر می‌کرد. در پوشهٔ OneDrive (یا Dropbox)، برنامهٔ همگام‌سازی و اغلب آنتی‌ویروس هنوز فایل‌های تازه را باز نگه داشته‌اند و ویندوز اجازهٔ تغییر نام پوشه را نمی‌دهد.
  - **اکنون:** داده‌ها مستقیماً در پوشهٔ نهایی (`<name>.files\chunk0`) کپی می‌شوند و پس از آن نام هیچ پوشه‌ای تغییر نمی‌کند. تا پایان کپی، فایل `PHOTOMESH_COPY_INCOMPLETE.txt` در آن پوشه قرار دارد. اگر کپی لغو شود یا ناموفق باشد، آن پوشه دوباره حذف می‌شود و پروژه همچنان از داده‌های فعلی خود استفاده می‌کند. تلاش بعدی از کپی ناتمام دوباره استفاده می‌کند؛ پوشه‌ای که فایل‌های دیگری دارد هرگز بازنویسی نمی‌شود.
  - فایل‌هایی که برای لحظه‌ای قفل هستند (همگام‌سازی، آنتی‌ویروس، نمایه‌ساز) چند بار دوباره امتحان می‌شوند. فایل‌های فقط‌خواندنی دیگر مانع پاک‌سازی نمی‌شوند. اگر چیزی قابل حذف نباشد، پیام خطا نام پوشه را می‌گوید.
  - پوشهٔ `chunk0.partial` که نسخهٔ 0.1.7 ممکن است در `<name>.files` به جا گذاشته باشد استفاده نمی‌شود و می‌توان آن را حذف کرد.

### نسخهٔ 0.1.7 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)
به **بستهٔ پایتونی جدیدی نیاز ندارد**. با `update.bat` به‌روزرسانی کنید.

- **داده‌های پروژه کنار فایل پروژه ذخیره می‌شوند.**
  - هرچه پروژه محاسبه می‌کند در پوشهٔ `<name>.files` کنار `<name>.pmsx` قرار می‌گیرد، برای هر چانک یک پوشه: هم‌ترازی، ابر نقاط، مش، بافت، کاشی‌ها، ارتوموزاییک، DEM، شکل‌ها و کش‌ها.
  - **پیش از پردازش، پروژه را ذخیره کنید.** Align Photos و هر مرحلهٔ پردازشی دیگر برای پروژهٔ ذخیره‌نشده ابتدا ذخیره را می‌خواهد و پنجرهٔ Save As همان‌جا باز می‌شود. با Cancel مرحله شروع نمی‌شود. دیگر از پوشهٔ موقت استفاده نمی‌شود.
  - فایل `.pmsx` مسیرها را **نسبت به پوشهٔ خودش** ذخیره می‌کند؛ بنابراین کل پوشهٔ پروژه را می‌توان به دیسک یا کامپیوتر دیگری کپی یا منتقل کرد.
  - عکس‌ها کپی نمی‌شوند و فقط به آن‌ها ارجاع داده می‌شود: اگر داخل پوشهٔ پروژه باشند با مسیر نسبی، وگرنه با مسیر کامل.
  - **Save As** در مکان جدید پیشنهاد می‌دهد داده‌های پروژه در `<name>.files` جدید کپی شوند. ابتدا فضای خالی دیسک بررسی می‌شود و پیشرفت کار با دکمهٔ Cancel نمایش داده می‌شود؛ کپی لغوشده یا ناموفق چیزی به جا نمی‌گذارد. فایل‌های اصلی حفظ می‌شوند؛ فقط داده‌های پوشهٔ موقت قدیمیِ پروژه‌های ذخیره‌نشده منتقل می‌شوند.
  - **پروژه‌های قدیمی** مثل قبل باز می‌شوند. در اولین ذخیره، مسیرهایشان نسبی نوشته می‌شود. داده‌های بیرون از پوشهٔ پروژه را می‌توان به داخل آن کپی کرد یا در جای فعلی نگه داشت (از شما پرسیده می‌شود). داده‌هایی که از قبل داخل پوشهٔ پروژه هستند، مثل `bench_drone5\fast`، سر جای خود می‌مانند.
  - اگر هنگام باز کردن پروژه عکس‌ها پیدا نشوند، از شما خواسته می‌شود پوشهٔ آن‌ها را نشان دهید (Change Image Paths).
- **GCP در نمای ارتوموزاییک.** در زبانهٔ Ortho با کلیک راست و **Add Marker** یک مارکر در همان نقطه قرار می‌گیرد. X و Y آن از زمین‌مرجع‌سازی رستر و ارتفاعش از DEM یا ارتفاع ارتوموزاییک (وگرنه از مدلِ زیر آن) گرفته می‌شود. مارکر مانند مارکرهای روی عکس یا مدل، در همهٔ عکس‌هایی که آن نقطه را می‌بینند تصویر می‌شود. سپس مختصات مرجع را وارد کنید، مارکر را روی عکس‌ها دقیق یا ثابت کنید و مثل همیشه Optimize Cameras را اجرا کنید. مارکرهای بدون مختصات هم اکنون در زبانهٔ Ortho نمایش داده می‌شوند.
- **بار CPU و GPU در نوار وضعیت**، هر ۱٫۵ ثانیه در پس‌زمینه به‌روز می‌شود.
  - مقدار GPU (و حافظهٔ GPU) از درایور NVIDIA (NVML) خوانده می‌شود و بدون GPU انویدیا پنهان است. `nvidia-smi` هرگز اجرا نمی‌شود.
- **نشانگر مشغول بودن.** هنگام اجرای یک مرحله، نشانگر ماوس حالت مشغول دارد و نوار وضعیت «Working: <step>...» را با یک نوار پیشرفت کوچک نشان می‌دهد. پنجره قابل استفاده می‌ماند و نشانگر پس از پایان، خطا یا لغو به حالت عادی برمی‌گردد.
- **عنوان پنجره** نسخه را نشان می‌دهد: `PhotoMesh Studio 0.1.7 - <project>`.
- **آیکون برنامه** (طراحی خودمان) در نوار عنوان، نوار وظیفه (در ویندوز با دکمهٔ جداگانه) و میان‌بر دسکتاپ.
- **صفحهٔ آغازین** با پیام‌های پیشرفت هنگام باز شدن برنامه.

### نسخهٔ 0.1.6 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)
به **بستهٔ پایتونی جدیدی نیاز ندارد**. با `update.bat` به‌روزرسانی کنید.

- **رفع خطا: Build Tiled Model با پیام «texture atlas packing failed» متوقف می‌شد** (کد خروج 1، Issue شمارهٔ ۱ در GitHub).
  - **علت:** در مش‌های متراکم پهپادی، سطح‌های جزئیات درشت‌تر به هزاران تکهٔ بافت ریز تقسیم می‌شوند. هر تکه، هر اندازه‌ای که داشته باشد، به یک حاشیهٔ کوچک نیاز دارد و اندازهٔ بافت هر کاشی بدون حساب این حاشیه‌ها انتخاب می‌شد؛ در نتیجه تکه‌ها هرچقدر هم کوچک می‌شدند جا نمی‌گرفتند.
  - **اکنون:** اندازهٔ بافت کاشی حاشیه‌ها را هم حساب می‌کند، در صورت نیاز بزرگ‌تر می‌شود (تا حداکثر اندازهٔ بافت) و در آخرین حالت به هر مثلث یک خانهٔ کوچک جداگانه می‌دهد. سطح‌های درشت همیشه کامل می‌شوند و کاشی‌های نهایی تمام جزئیات را نگه می‌دارند.
  - **Build Texture** هم همین محافظت را دارد: اگر تکه‌ها در تعداد صفحه‌های انتخاب‌شده جا نشوند، به‌جای خطا صفحه‌های بیشتری به کار می‌رود.
- **رفع خطا: `setup_git_update.bat` با پیام «pms_update_setup.ps1 does not exist» متوقف می‌شد** وقتی پوشه از قبل یک checkout گیت بود ولی `packaging\update.ps1` را نداشت (checkout قدیمی یا نیمه‌کاره؛ zip پرتابل 0.1.4 پوشهٔ `packaging` ندارد).
  - اکنون اگر remote گیت‌هاب نباشد آن را اضافه می‌کند، دانلود می‌کند و فایل‌های برنامه را به نسخهٔ GitHub می‌رساند. فایل‌های تغییرکردهٔ برنامه در `git stash` نگه داشته می‌شوند و به `.venv`، پروژه‌ها و داده‌های دیگر دست نمی‌خورد. سپس به‌روزرسانی معمولی اجرا می‌شود.
  - اگر دانلود ناموفق باشد، checkout موجود همان‌طور که بود می‌ماند.

### نسخهٔ 0.1.5 (۱۴۰۵/۰۸/۰۱ — 2026-10-23)
به **بستهٔ پایتونی جدیدی نیاز ندارد**. با `update.bat` به‌روزرسانی کنید.

- **گزارش خطا.** هر کرش یا مرحلهٔ ناموفق اکنون یک پنجرهٔ خطا باز می‌کند و گزارشی روی همین کامپیوتر ذخیره می‌کند: `%LOCALAPPDATA%\PhotoMeshStudio\reports\report_YYYYMMDD_HHMMSS.md` به‌همراه یک فایل `.zip` با لاگ‌ها.
  - **شامل:** فرایندهای COLMAP/GLOMAP یا مراحل دیگری که با کد خطا تمام می‌شوند، خطاهای پایتون در فرمان‌ها و کارهای پس‌زمینه، threadها و QThreadها، کمبود حافظه (MemoryError) و پیام‌های جدی Qt (این‌ها بدون پنجره ذخیره می‌شوند).
  - **محتوا:**
    - زمان، نسخهٔ برنامه و commit گیت؛
    - مرحله و تنظیمات آن، و خط فرمان؛
    - کد خروج به‌صورت دهدهی و هگز، با معنای آن در ویندوز (مثلاً 0xC0000409 = توقف اضطراری، معمولاً کمبود حافظه؛ 0xC0000005 = دسترسی غیرمجاز به حافظه)؛
    - traceback و ۳۰۰ خط آخر لاگ همان مرحله؛
    - آمار پروژه: تعداد عکس‌ها، عکس‌های هم‌تراز‌شده، محصولات، نام سیستم مختصات؛
    - اطلاعات سیستم: نسخهٔ ویندوز، CPU، تعداد thread، RAM، GPU/VRAM/درایور (nvidia-smi)، فضای آزاد دیسک، نسخهٔ پایتون و بسته‌ها، نسخهٔ COLMAP.
  - **حریم خصوصی:**
    - هیچ عکس و هیچ مختصاتی در گزارش نیست (فقط نام سیستم مختصات)؛
    - پوشهٔ کاربر به `%USERPROFILE%` و پوشه‌های پروژه و عکس به `<project>` / `<photos>` تبدیل می‌شوند؛ از مسیرهای دیگر فقط درایو و نام فایل می‌ماند؛
    - نام کاربر و نام کامپیوتر حذف می‌شوند.
- **پنجرهٔ خطا** با دکمه‌های **Details**، **Copy report**، **Open report folder** و **Report to GitHub**. در این پنجره یک کادر اختیاری هم برای «چه کاری می‌کردید؟» هست.
  - **Report to GitHub** مرورگر شما را روی یک Issue جدید در GitHub باز می‌کند که عنوان و گزارش در آن پر شده است (برچسب `bug`). تا *Submit* را نزنید چیزی فرستاده نمی‌شود؛ خود برنامه هرگز چیزی نمی‌فرستد و هیچ توکنی در آن نیست.
  - اگر گزارش برای لینک خیلی طولانی باشد، کوتاه می‌شود و از شما می‌خواهد فایل `.zip` را به صفحه بکشید.
  - اینترنت ندارید؟ گزارش ذخیره می‌ماند؛ بعداً بفرستید.
- **Help > Report a Problem...** هر زمان از جلسهٔ فعلی گزارش می‌سازد.
- قالب Issue در GitHub (`.github/ISSUE_TEMPLATE/bug_report.md`) با همان قالب گزارش.
- نشانی Issue یک تنظیم در `photomesh/report.py` است (`ISSUES_URL`؛ با `PHOTOMESH_ISSUES_URL=off` دکمه پنهان می‌شود).

### نسخهٔ 0.1.4 (۱۴۰۵/۰۷/۳۰ — 2026-10-22)
به **هیچ بستهٔ پایتونی جدیدی نیاز ندارد**. فایل update مثل نسخهٔ 0.1.3 روی نصب موجود باز می‌شود.

- **رفع خطای Build Point Cloud در مرحلهٔ «Building point cloud (fusion)» با کد خطای 1073740791- (0xC0000409):**
  - **علت:** دست‌کم یکی از فایل‌های depth map یا normal map خالی یا ناقص بود؛ مثلاً وقتی دیسک پر شده یا پردازش در میانهٔ نوشتن فایل لغو شده است. در این حالت fusion در COLMAP با پیام «Check failed: width_ > 0» متوقف می‌شود. ادامهٔ خودکار خود COLMAP هم چنین فایلی را «تمام‌شده» حساب می‌کند.
  - **حالا:** پیش از ساخت depth mapها و پیش از fusion همهٔ فایل‌ها بررسی می‌شوند (سرآیند، ابعاد و طول فایل). فایل‌های خراب حذف می‌شوند و **فقط همان عکس‌ها** (به‌همراه عکس‌های همسایهٔ لازم) دوباره پردازش می‌شوند. همهٔ depth mapهای سالم حفظ می‌شوند.
  - عکس‌هایی که باز هم map سالمی نداشته باشند از fusion کنار گذاشته می‌شوند و نامشان در Console نمایش داده می‌شود.
- **بررسی فضای دیسک پیش از ساخت depth mapها:** کیفیت Ultra high برای ۶۴۰ عکس DJI در حین ساخت depth mapها حدود ۴۰۰ گیگابایت فضا لازم دارد. برنامه حالا همان ابتدا پیام روشنی می‌دهد، به‌جای اینکه چند ساعت بعد خطا بدهد.
- **fusion با توجه به حافظهٔ آزاد:**
  - پیش‌فرض COLMAP همهٔ depth mapها، normal mapها و عکس‌ها را با اندازهٔ کامل یک‌جا در حافظه بارگذاری می‌کرد؛ برای ۶۴۰ عکس Ultra high حدود ۲۵۰ گیگابایت.
  - حالا اندازهٔ fusion، تعداد threadها و حافظهٔ cache بر اساس RAM آزاد انتخاب می‌شوند و در سطر «Fusion plan» گزارش نمایش داده می‌شوند.
  - اگر fusion خطا بدهد، خودکار با حافظهٔ کمتر دوباره اجرا می‌شود و depth mapها دوباره محاسبه نمی‌شوند.
- **ادامهٔ کار:** اگر اجرای قبلی Build Point Cloud با همین تنظیمات تمام نشده باشد، اجرای بعدی depth mapهای آن را دوباره استفاده می‌کند، حتی اگر گزینهٔ «Reuse depth maps» تیک نخورده باشد.
- **نوار پیشرفت:**
  - در مرحلهٔ depth map، گذر photometric یا geometric، شمارهٔ عکس و زمان باقی‌مانده نمایش داده می‌شود؛
  - در مرحلهٔ fusion، شمارهٔ عکس و تعداد نقطه‌ها تا آن لحظه نمایش داده می‌شود؛
  - نوار با دقت ۰٫۱٪ جلو می‌رود.
- پیام خطاهای COLMAP روشن‌تر شده و معنی کد خطا هم گفته می‌شود.
- **به‌روزرسانی از GitHub:** فایل `update.bat` نسخهٔ جدید را دانلود و نصب می‌کند (دیگر فایل zip لازم نیست)، `rollback.bat` به نسخهٔ قبلی برمی‌گردد و `setup_git_update.bat` یک بار نصب موجود را به این روش منتقل می‌کند. پوشهٔ `.venv` و پروژه‌ها حفظ می‌شوند. راهنما: UPDATING.md

### نسخهٔ 0.1.3 (۱۴۰۵/۰۷/۳۰ — 2026-10-22)
به **هیچ بستهٔ پایتونی جدیدی نیاز ندارد** (همان ۷۴ بستهٔ نسخهٔ 0.1.2). روی نصب موجود، فایل update کافی است. روی لپ‌تاپ جدید، از فایل portable کامل استفاده کنید.

- **خروجی‌ها:** خروجی مدل و ابر نقاط در مختصات محلی (DXF و PLY) برای CAD و Rhino، به‌علاوهٔ OBJ، glTF و 3D Tiles. ساخت بافت (Build Texture) و مدل کاشی‌شده (Build Tiled Model) هم در این نسخه هست.
- **ارتوموزاییک و DEM:** فرمان‌های Build Orthomosaic و Build DEM، زبانهٔ Ortho، و خروجی GeoTIFF همراه با world file. یادداشت جای‌گذاری و اسکریپت جای‌گذاری در Rhino هم ساخته می‌شود.
- **وارد کردن شکل‌ها (Import Shapes):** پشتیبانی از DXF، SHP، GeoJSON، KML و CSV. شکل‌ها در نمای سه‌بعدی، روی ارتوموزاییک و روی همهٔ عکس‌ها نمایش داده می‌شوند.
  - سبک هر لایه و هر شکل قابل تنظیم است: رنگ، ضخامت، خط‌چین، رنگ و شفافیت پرکردن، نماد نقطه و برچسب.
  - در نمای شفاف، بخش‌های پنهان به‌صورت خط‌چین نمایش داده می‌شوند.
- **مارکرها و GCP:** قرار دادن و ثابت کردن (pin) مارکر به‌صورت دستی روی عکس‌ها، نمایش موقعیت پیش‌بینی‌شده، پنل Reference و پالایش موقعیت مارکر.
- **نورپردازی و برچسب‌ها:** سایه‌زنی بهتر در نمای سه‌بعدی و برچسب‌های خواناتر با پس‌زمینه.
- **نوار ابزار:** آیکون‌های اختصاصی، ۳۰٪ بزرگ‌تر (۲۱ پیکسل)؛ دکمه‌های نمایش/پنهان کردن مارکرها و شکل‌ها در زبانهٔ Photo.
- **رفع خطای MemoryError در Build Point Cloud** (مرحلهٔ undistort):
  - نقشه‌های تصحیح اعوجاج حالا float32 هستند و میان پردازش‌ها مشترک‌اند؛
  - تعداد پردازش‌ها بر اساس RAM آزاد محدود می‌شود؛
  - تصویری که خطا بدهد جداگانه دوباره پردازش می‌شود؛
  - اجرای نیمه‌کاره از همان جایی که متوقف شده ادامه پیدا می‌کند؛
  - بیشترین مصرف حافظه در گزارش (log) ثبت می‌شود.
- **رفع خطای «Tie point statistics … sparse_stats»** هنگام باز کردن پروژه. پنل Properties حالا تعداد projectionها، میانگین projection برای هر عکس و میانگین طول track را نشان می‌دهد. باز کردن پروژه دیگر آن را «تغییریافته» علامت نمی‌زند.
- **سرعت بیشتر:** رسم حدود ۶۰۰۰ شکل روی یک عکس حالا حدود ۰٫۱۶ ثانیه طول می‌کشد (قبلاً ۷ تا ۱۳ ثانیه). محاسبهٔ پنهان بودن شکل‌ها پشت مدل (occlusion) در پس‌زمینه انجام می‌شود.

</div>
