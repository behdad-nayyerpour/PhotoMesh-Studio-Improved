# PhotoMesh Studio — installing on another Windows laptop (offline)

> **Current installation:** run `PhotoMeshStudio_Setup_<version>.exe`. For every later release, run `PhotoMeshStudio_Update_<version>.exe`. Both are single offline files; see [UPDATING.md](UPDATING.md). The ZIP instructions below document older portable installations.
>
> **روش فعلی نصب:** فایل `PhotoMeshStudio_Setup_<version>.exe` را اجرا کنید و برای نسخه‌های بعدی فایل `PhotoMeshStudio_Update_<version>.exe` را به کار ببرید. دستورهای ZIP زیر مربوط به نصب‌های قدیمی است.


[English](#english) · [فارسی](#فارسی)

---

## English

### 1. What is in the package
`PhotoMeshStudio_portable_<date>.zip` (e.g. `PhotoMeshStudio_portable_20261022.zip`) holds the exact version that runs on the development PC. It needs no internet, either to install or to run.

| Folder / file | Contents |
|---|---|
| `photomesh-studio\` | App code |
| `python\` | CPython 3.13 (64-bit). Nothing needs to be installed system-wide. |
| `wheelhouse\` + `requirements.lock` | Every Python package at its pinned version: PySide6, pyqtgraph, PyOpenGL, numpy, scipy, open3d, plyfile, Pillow, pyproj, pytest and their dependencies |
| `tools\colmap\` | COLMAP 3.8, CUDA build (CUDA 11 runtime `cudart64_110.dll` included) |
| `tools\glomap\` | GLOMAP (fast global alignment, CPU) |
| `redist\` | Visual C++ 2015-2022 x64 installer (if it was available when the package was built). App-local copies of the VC++ DLLs are already in `tools\*\bin`. |
| `install_offline.bat` | Creates the Python environment from `wheelhouse\` (pip `--no-index`) and a desktop shortcut |
| `PhotoMesh Studio.bat` | Starts the app (runs the installer on first start) |
| `run_tests_offline.bat` | Self-test |
| `create_shortcut.ps1` | Re-creates the desktop and Start-menu shortcut |

### 2. Requirements
- **Windows 10 or 11, 64-bit.** The app does not run on 32-bit Windows or ARM.
- **Disk space:**
  - about 3 GB for the installed package;
  - plus room for projects. A 641-photo drone project with a dense cloud and mesh needs tens of GB in its working folder.
- **Memory:** 16 GB RAM minimum; 32 GB or more is recommended for large point clouds (about 28 M points).
- **GPU (recommended):** an NVIDIA GPU with a driver that supports **CUDA 11.x**, which means driver version **452.39 or newer**. Any current NVIDIA driver qualifies.
  - The development PC uses an RTX 3080 Laptop GPU with driver 512.36.
  - Use 4 GB of VRAM or more; 8 GB is recommended for depth maps.
  - Very new GPU generations may not be supported by a CUDA 11 build of COLMAP 3.8 (not verified). If Build Point Cloud fails with a CUDA "no kernel image" error, use a newer CUDA build of COLMAP via Tools > Preferences.
- **Without an NVIDIA GPU** (Intel/AMD graphics), the app detects the missing NVIDIA driver and runs in CPU-only mode. The status bar shows "COLMAP: found (CPU only …)".
  - Align Photos works (GPU SIFT is switched off automatically), just more slowly.
  - Tie points, markers/GCPs, Optimize Cameras and filtering work.
  - Build Model from an existing point cloud works.
  - **Build Point Cloud (depth maps) is not available.** COLMAP's dense stereo requires CUDA; the app says so instead of failing.
- **3D view:** needs OpenGL 2.1 or newer, which any normal graphics driver provides. On a remote desktop without OpenGL, the 3D view shows a message and everything else still works.

### 3. Install
1. Copy `PhotoMeshStudio_portable.zip` to the laptop.
2. Right-click it, choose **Properties**, tick **Unblock** if that box is shown, and click OK. This stops Windows SmartScreen from blocking the scripts.
3. Extract it to a folder **you can write to** with a **short path**, for example `C:\PhotoMeshStudio` or `D:\Apps\PhotoMeshStudio`. Avoid `C:\Program Files`. The result is `C:\PhotoMeshStudio\PhotoMeshStudio\...`.
   - Windows limits file paths to 260 characters, and some Qt files inside the environment are deeply nested. The `photomesh-studio` folder path may be at most about 73 characters, so the extracted `PhotoMeshStudio` folder path must be about 56 characters or less. The installer checks this and stops with a clear message if the folder path is too long (for example, Desktop or Downloads folders inside a long user profile path). Enabling *Win32 long paths* in Windows also removes the limit.
4. Double-click **`install_offline.bat`**. It takes about 1–3 minutes, needs no internet and no administrator rights, and prints:
   - "Packages OK";
   - "COLMAP OK";
   - whether an NVIDIA driver was found.
   It also creates the **PhotoMesh Studio** shortcut on the desktop and in the Start menu.
5. Start the app from the shortcut, or with `PhotoMesh Studio.bat`.
6. Optional: run `run_tests_offline.bat` to self-test. Expect "… passed"; COLMAP tests are skipped unless you pass a photo folder.

> If you move the folder later, run `install_offline.bat` again. The Python environment stores its own location.

### 4. Moving projects from the old computer
A project is a **`.pmsx` file** plus its **working folder**. For example, `bench_drone5\drone_DJI_001_fast.pmsx` uses `bench_drone5\fast\`, which holds COLMAP data, depth maps, point clouds, the mesh and the logs.

1. **Save** the project on the old PC and close the app.
2. Copy the **whole project folder** (e.g. `bench_drone5\`, which contains both the `.pmsx` and `fast\`) to the new laptop, to any drive or folder.
3. Copy the **photos** too (e.g. `F:\Drone\...\DJI_001`), anywhere you like.
4. Open the `.pmsx` in PhotoMesh Studio.
   - **Results** (working folder, tie points, point cloud, mesh, thumbnails) are found automatically next to the new `.pmsx` location. The Console logs "Project folder was moved: product paths updated".
   - **Photos:** if they are not at the old path (a different drive letter, for example), the status bar says "N photo(s) not found…". Choose **Tools > Change Image Paths...** (also in the Workspace right-click menu on the chunk, Images or a photo) and select the folder that now holds the photos. Subfolders are searched; photos are matched by file name, and the best-matching folder path wins when names repeat. The app reports how many photos it relinked and lists any it could not find.
5. **File > Save** to store the new paths.

Notes:
- The app never writes into the photo folders. Read-only photo drives work.
- Texture and tiled model built from the app live in the working folder and move with it. If they were built in a separate folder (for example `bench_drone5_texture\...` or `bench_drone5_tiles\...`), copy those folders too and attach them with **File > Import > Import Texture...** (folder with `info.json` and `texture_0.jpg`) and **File > Import > Import Tiled Model...** (folder with `tileset.json`), then Save.
- Processing metadata (Workspace > Properties) moves with the `.pmsx`.
- Preferences (window layout, layer toggles, COLMAP path) are per-user settings and start at defaults on the new laptop.

### 5. Troubleshooting
| Symptom | Fix |
|---|---|
| Windows protected your PC / scripts blocked | Unblock the zip before extracting (step 3.2), or click *More info → Run anyway*. |
| *ERROR: this folder path is too long for Windows* | Move the extracted `PhotoMeshStudio` folder to a short path (e.g. `C:\PhotoMeshStudio`) and run `install_offline.bat` again. |
| `install_offline.bat` reports *ERROR … missing* | The zip was extracted only partly. Extract it again with Windows Explorer or 7-Zip. |
| Message: *colmap.exe does not start* | Run `redist\vc_redist.x64.exe` (needs admin), or install "Microsoft Visual C++ 2015-2022 Redistributable (x64)". |
| Status bar shows *COLMAP: not found* | Check that `tools\colmap\bin\colmap.exe` exists next to `photomesh-studio\`, or set its path in Tools > Preferences. |
| *Build Point Cloud needs CUDA* | No NVIDIA driver was found. Install or update the NVIDIA driver (452.39 or newer). On non-NVIDIA laptops, dense reconstruction is not possible; process dense on a GPU PC and copy the project back. |
| CUDA error / out of memory during depth maps | Choose a lower quality (Medium/Low) in Build Point Cloud, close other GPU apps, and update the driver. |
| 3D view is black or says "OpenGL unavailable" | Update the graphics driver. On laptops with two GPUs, set *pythonw.exe* (in `photomesh-studio\.venv\Scripts`) to "High performance" in Windows Settings > Display > Graphics. |
| *N photos not found* after opening | Use Tools > Change Image Paths... (section 4). |
| App started from a moved folder fails | Run `install_offline.bat` again. |
| Window freezes while adding many photos (older versions) | Install the update (section 6). Photos are now added in a few seconds; thumbnails fill in the background (status bar, ✕ to stop). |
| Add Photos / Open dialog itself hangs | Tools > Preferences > *Use built-in file dialogs* (section 7). |
| Anything else | Send the newest log from Help > Open Log Folder (section 7). Also check Console (View > Console) and the logs in the project working folder (`*_log.txt`). |

### 6. Updating an existing installation (no reinstall)
A code update comes as a small `PhotoMeshStudio_update_<date>.zip` (about 1 MB).
1. Close PhotoMesh Studio.
2. Unblock the zip (Properties → **Unblock**), then extract it **into** the installed `PhotoMeshStudio` folder — the one that contains `install_offline.bat` and the `photomesh-studio` folder (e.g. `C:\PhotoMeshStudio\PhotoMeshStudio`). Choose **Replace the files in the destination**.
3. Double-click **`check_update.bat`**. It must print *Update OK*.
4. Start the app from the desktop shortcut as usual.

The Python environment (`photomesh-studio\.venv`), COLMAP/GLOMAP and the shortcut are kept. Run `install_offline.bat` again only if an update note says new packages are needed.

Version 0.1.3 (exports, orthomosaic/DEM, Import Shapes with styles, manual markers, toolbar icons, the Build Point Cloud memory fix, faster shapes on photos; see `CHANGES.md`) also needs **no new packages**. Extract the update zip over an existing install as above; a laptop without PhotoMesh Studio uses the full `PhotoMeshStudio_portable_<date>.zip` (section 3).

Version 0.1.2 (Build Texture, Build Tiled Model, textured view, OBJ/glTF/3D Tiles export) needs **no new packages**. Texturing a large model uses a lot of memory (each 8192×8192 page is held in memory as floating point while it is built); 8192×4 on a 1.85 M-face model ran fine on a 32 GB laptop, so on 16 GB machines close other programs or use fewer pages. Real textures need a working OpenGL 2.1 graphics driver; with the built-in Windows "Basic Display" driver large textures are scaled down for display (the files are not).

### 7. Log files (for bug reports)
Every session writes a log file to `%LOCALAPPDATA%\PhotoMeshStudio\logs\photomesh_<date>_<time>_<pid>.log`. Open the folder with **Help → Open Log Folder**. The log contains:
- system info (Windows, CPU/RAM, Python/Qt/Pillow versions, NVIDIA driver);
- every Console message, and the time of each Add Photos phase;
- errors with full tracebacks;
- a **hang report**: if the window stops responding for 20 s, the stack of every thread is written to the log (and repeated every 20 s while the hang lasts).

If something goes wrong, send the newest `photomesh_*.log` (after closing the app, or while it hangs). The last 30 logs are kept.

If **File → Add Photos / Open** itself hangs before a dialog appears (broken Explorer shell extensions, disconnected network or OneDrive drives), tick **Tools → Preferences → General → Use built-in file dialogs**. You can also start the app once with the environment variable `PHOTOMESH_QT_DIALOGS=1`.

---

<div dir="rtl">

## فارسی

### ۱. محتوای بسته
فایل `PhotoMeshStudio_portable.zip` دقیقاً همان نسخه‌ای است که روی کامپیوتر فعلی کار می‌کند. نه برای نصب و نه برای اجرا به اینترنت نیاز دارد.

- `photomesh-studio\`: کد برنامه
- `python\`: پایتون 3.13 نسخهٔ ۶۴ بیتی؛ لازم نیست پایتون روی سیستم نصب شود
- `wheelhouse\` و `requirements.lock`: همهٔ بسته‌های پایتون با نسخه‌های دقیق:
  - PySide6، pyqtgraph، PyOpenGL، numpy، scipy، open3d، plyfile، Pillow، pyproj، pytest و وابستگی‌هایشان
- `tools\colmap\`: COLMAP 3.8 نسخهٔ CUDA (همراه با فایل `cudart64_110.dll`)
- `tools\glomap\`: GLOMAP برای هم‌ترازی سریع
- `redist\`: نصب‌کنندهٔ Visual C++ 2015-2022 (x64)؛ فایل‌های DLL لازم از قبل در `tools\*\bin` گذاشته شده‌اند
- `install_offline.bat`: نصب آفلاین و ساختن میانبر روی دسکتاپ
- `PhotoMesh Studio.bat`: اجرای برنامه
- `run_tests_offline.bat`: آزمون خودکار

### ۲. پیش‌نیازها
- **ویندوز ۱۰ یا ۱۱ نسخهٔ ۶۴ بیتی.**
- **فضای دیسک:**
  - حدود ۳ گیگابایت برای خود بسته؛
  - به‌علاوهٔ فضای پروژه‌ها؛ یک پروژهٔ ۶۴۱ عکسی با ابر نقاط متراکم و مش، چند ده گیگابایت فضا لازم دارد.
- **حافظه:** دست‌کم ۱۶ گیگابایت RAM؛ برای ابر نقاط بزرگ ۳۲ گیگابایت یا بیشتر توصیه می‌شود.
- **کارت گرافیک (توصیه‌شده):** کارت NVIDIA با درایوری که از **CUDA 11.x** پشتیبانی کند، یعنی درایور **452.39 یا جدیدتر**.
  - کامپیوتر فعلی RTX 3080 Laptop با درایور 512.36 دارد.
  - حافظهٔ گرافیک دست‌کم ۴ گیگابایت باشد؛ ۸ گیگابایت توصیه می‌شود.
  - ممکن است کارت‌های خیلی جدید با نسخهٔ CUDA 11 از COLMAP 3.8 کار نکنند (این مورد آزمایش نشده است).
- **بدون کارت NVIDIA** (گرافیک Intel یا AMD)، برنامه نبودِ درایور NVIDIA را تشخیص می‌دهد و در حالت فقط CPU کار می‌کند:
  - Align Photos کار می‌کند (کندتر)؛ مارکرها، GCP، Optimize Cameras و فیلترها هم کار می‌کنند؛
  - Build Model از ابر نقاط موجود کار می‌کند؛
  - **Build Point Cloud (نقشه‌های عمق) ممکن نیست**، چون به CUDA نیاز دارد. برنامه به‌جای خطا دادن، پیام روشنی نشان می‌دهد.
- **نمای سه‌بعدی:** به OpenGL 2.1 یا بالاتر نیاز دارد که هر درایور گرافیک معمولی دارد.

### ۳. نصب
1. فایل zip را به لپ‌تاپ منتقل کنید.
2. روی فایل راست‌کلیک کنید، **Properties** را بزنید، اگر گزینهٔ **Unblock** هست تیکش را بزنید و OK کنید.
3. فایل را در پوشه‌ای که در آن اجازهٔ نوشتن دارید و مسیرش کوتاه است باز کنید، مثلاً `C:\PhotoMeshStudio`. در `Program Files` باز نکنید.
   - ویندوز طول مسیر فایل‌ها را به ۲۶۰ کاراکتر محدود می‌کند. اگر مسیر پوشه خیلی طولانی باشد (مثلاً داخل Downloads یا Desktop)، نصب‌کننده با پیام «path is too long» متوقف می‌شود. در این صورت پوشه را به مسیر کوتاهی مثل `C:\PhotoMeshStudio` ببرید و دوباره نصب کنید.
4. روی **`install_offline.bat`** دوبار کلیک کنید. نصب ۱ تا ۳ دقیقه طول می‌کشد و به اینترنت یا دسترسی Administrator نیازی ندارد. در پایان:
   - پیام‌های «Packages OK» و «COLMAP OK» را می‌بینید؛
   - برنامه می‌گوید درایور NVIDIA پیدا شد یا نه؛
   - میانبر **PhotoMesh Studio** روی دسکتاپ و در منوی Start ساخته می‌شود.
5. برنامه را از میانبر یا با `PhotoMesh Studio.bat` اجرا کنید.
6. اختیاری: `run_tests_offline.bat` را برای آزمون خودکار اجرا کنید.

> اگر پوشه را بعداً جابه‌جا کردید، `install_offline.bat` را دوباره اجرا کنید.

### ۴. انتقال پروژه‌ها
هر پروژه شامل یک **فایل `.pmsx`** و **پوشهٔ کاری** آن است. مثلاً `bench_drone5\drone_DJI_001_fast.pmsx` از پوشهٔ `bench_drone5\fast\` استفاده می‌کند.

1. روی کامپیوتر قبلی پروژه را **Save** کنید و برنامه را ببندید.
2. **کل پوشهٔ پروژه** را (مثلاً `bench_drone5\` که هم `.pmsx` و هم `fast\` را دارد) به هر درایوی در لپ‌تاپ کپی کنید.
3. **عکس‌ها** را هم (مثلاً `F:\Drone\...\DJI_001`) به هر جای دلخواه کپی کنید.
4. فایل `.pmsx` را در برنامه باز کنید.
   - **نتایج پردازش** (پوشهٔ کاری، Tie Points، ابر نقاط، مش) خودکار در محل جدید پیدا می‌شوند.
   - **عکس‌ها:** اگر حرف درایو فرق کند، در نوار وضعیت پیام «N photo(s) not found» نمایش داده می‌شود. از منوی **Tools > Change Image Paths...** (یا با راست‌کلیک روی Chunk یا Images در پنل Workspace)، پوشه‌ای را که عکس‌ها اکنون در آن هستند انتخاب کنید.
     - زیرپوشه‌ها هم جست‌وجو می‌شوند و عکس‌ها از روی نام فایل پیدا می‌شوند.
     - برنامه تعداد عکس‌های پیداشده و پیدانشده را گزارش می‌دهد.
5. با **File > Save** مسیرهای جدید را ذخیره کنید.

بافت و مدل کاشی‌شده‌ای که از داخل برنامه ساخته شده‌اند در پوشهٔ کاری هستند و همراه پروژه منتقل می‌شوند. اگر در پوشهٔ جداگانه‌ای ساخته شده‌اند (مثلاً `bench_drone5_texture\...` یا `bench_drone5_tiles\...`)، آن پوشه‌ها را هم کپی کنید و با **File > Import > Import Texture...** (پوشه‌ای که `info.json` و `texture_0.jpg` دارد) و **File > Import > Import Tiled Model...** (پوشه‌ای که `tileset.json` دارد) به پروژه وصل کنید و Save بزنید.

برنامه هرگز چیزی در پوشهٔ عکس‌ها نمی‌نویسد. تنظیمات شخصی (چیدمان پنجره‌ها، نمایش لایه‌ها و مسیر COLMAP) روی لپ‌تاپ جدید از مقدار پیش‌فرض شروع می‌شوند.

### ۵. رفع اشکال
- **ویندوز اسکریپت را مسدود کرد:** قبل از باز کردن zip آن را Unblock کنید، یا *More info ← Run anyway* را بزنید.
- **پیام ERROR … missing:** فایل zip ناقص باز شده است؛ دوباره و کامل بازش کنید.
- **پیام «colmap.exe does not start»:** فایل `redist\vc_redist.x64.exe` را اجرا کنید (به دسترسی Administrator نیاز دارد).
- **پیام «COLMAP: not found»:** مطمئن شوید پوشهٔ `tools` کنار `photomesh-studio` قرار دارد، یا مسیر COLMAP را در Tools > Preferences تنظیم کنید.
- **پیام «Build Point Cloud needs CUDA»:** درایور NVIDIA پیدا نشد. درایور را نصب یا به‌روز کنید. روی لپ‌تاپ بدون کارت NVIDIA، بخش متراکم را روی کامپیوتر دارای GPU پردازش کنید و پروژه را برگردانید.
- **خطای CUDA یا کمبود حافظه:** کیفیت Medium یا Low را انتخاب کنید، برنامه‌های دیگری را که از کارت گرافیک استفاده می‌کنند ببندید و درایور را به‌روز کنید.
- **نمای سه‌بعدی سیاه است:** درایور گرافیک را به‌روز کنید. در لپ‌تاپ‌های دو کارت گرافیکه، در Settings > Display > Graphics برای `pythonw.exe` گزینهٔ «High performance» را انتخاب کنید.
- **عکس‌ها پیدا نمی‌شوند:** از Tools > Change Image Paths... استفاده کنید.
- **سایر موارد:** پنل Console (View > Console) و فایل‌های `*_log.txt` در پوشهٔ کاری پروژه را بررسی کنید.

### ۶. به‌روزرسانی نصب موجود (بدون نصب مجدد)
به‌روزرسانی کد به‌صورت یک فایل کوچک `PhotoMeshStudio_update_<date>.zip` (حدود ۱ مگابایت) ارائه می‌شود.
۱. برنامه را ببندید.
۲. فایل zip را Unblock کنید (Properties ← **Unblock**) و آن را **داخل** پوشه‌ی نصب‌شده‌ی `PhotoMeshStudio` استخراج کنید؛ یعنی پوشه‌ای که `install_offline.bat` و پوشه‌ی `photomesh-studio` در آن است (مثلاً `C:\PhotoMeshStudio\PhotoMeshStudio`). گزینه‌ی **Replace the files in the destination** را انتخاب کنید.
۳. روی **`check_update.bat`** دوبار کلیک کنید؛ باید پیام *Update OK* نمایش داده شود.
۴. برنامه را مثل همیشه از میان‌بر دسکتاپ اجرا کنید.

محیط پایتون (`photomesh-studio\.venv`)، COLMAP/GLOMAP و میان‌بر حفظ می‌شوند. فقط اگر در توضیحات به‌روزرسانی گفته شد که بسته‌ی جدیدی لازم است، `install_offline.bat` را دوباره اجرا کنید.

نسخهٔ 0.1.3 (خروجی‌ها، ارتوموزاییک و DEM، وارد کردن شکل‌ها با سبک‌ها، مارکرهای دستی، آیکون‌های نوار ابزار، رفع مشکل حافظه در Build Point Cloud و رسم سریع‌تر شکل‌ها روی عکس‌ها؛ فهرست کامل در `CHANGES.md`) هم به **بستهٔ جدیدی نیاز ندارد**. روی نصب موجود، فایل update را طبق مراحل بالا باز کنید. روی لپ‌تاپی که برنامه را ندارد، از فایل کامل `PhotoMeshStudio_portable_<date>.zip` استفاده کنید (بخش ۳).

نسخه‌ی 0.1.2 (ساخت بافت، مدل کاشی‌شده، نمایش بافت‌دار و خروجی OBJ/glTF/3D Tiles) به **بسته‌ی جدیدی نیاز ندارد**. بافت‌دهی مدل‌های بزرگ حافظه‌ی زیادی مصرف می‌کند؛ 8192×4 برای مدل ۱٫۸۵ میلیون وجهی روی لپ‌تاپ ۳۲ گیگابایتی بدون مشکل اجرا شد. روی سیستم‌های ۱۶ گیگابایتی برنامه‌های دیگر را ببندید یا تعداد صفحه‌ها را کم کنید.

### ۷. فایل‌های گزارش (لاگ) برای گزارش اشکال
هر بار اجرای برنامه یک فایل لاگ در `%LOCALAPPDATA%\PhotoMeshStudio\logs\` می‌سازد. با **Help ← Open Log Folder** این پوشه باز می‌شود. لاگ شامل این موارد است:
- مشخصات سیستم؛
- پیام‌های Console و زمان هر مرحله‌ی Add Photos؛
- خطاها با جزئیات کامل؛
- **گزارش هنگ**: اگر پنجره ۲۰ ثانیه پاسخ ندهد، وضعیت همه‌ی رشته‌ها در لاگ نوشته می‌شود.

در صورت بروز مشکل، جدیدترین فایل `photomesh_*.log` را ارسال کنید.

اگر خود پنجره‌ی **File ← Add Photos / Open** قبل از نمایش هنگ می‌کند (مثلاً به‌خاطر درایو شبکه یا OneDrive قطع‌شده)، گزینه‌ی **Tools ← Preferences ← General ← Use built-in file dialogs** را فعال کنید.

</div>
