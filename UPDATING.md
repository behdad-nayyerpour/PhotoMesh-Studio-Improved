# Installing and updating PhotoMesh Studio / نصب و به‌روزرسانی

## English

### Installed with the EXE

1. On a new Windows computer, run `PhotoMeshStudio_Setup_<version>.exe`.
   It contains Python, the app, COLMAP, and GLOMAP. No ZIP extraction, Git, or
   system Python installation is needed.
2. To update, close PhotoMesh Studio and run
   `PhotoMeshStudio_Update_<new-version>.exe`. Select the existing installation
   folder if Windows does not find it automatically. The updater checks the
   installed runtime before replacing app files. When dependencies changed, the
   update EXE also carries the new runtime.
3. Open PhotoMesh Studio from the Start menu. Your `.pmsx` projects, their
   `.files` result folders, and source photos are outside the app installation
   and remain in place.

The installer and updater are both single offline EXE files. Build them for a
new release with `packaging/build_release.ps1` (see
`packaging/RELEASE.md`). Keep the two EXE files and their SHA-256 files
together when distributing a release.

### Working from source

Install Git for Windows and Python 3.10–3.13, then run:

```powershell
git clone https://github.com/behdad-nayyerpour/photomesh-studio-improved.git
cd photomesh-studio-improved
.\setup.bat
.\run.bat
```

For later source updates, close the app and run `update.bat`. This route is
for developers; installed EXE copies should use the versioned update EXE.
`setup_git_update.bat` remains only for older portable installations.

## فارسی

### نصب با فایل EXE

۱. روی رایانهٔ جدید، فایل `PhotoMeshStudio_Setup_<version>.exe` را اجرا کنید.
   این یک فایل شامل پایتون، برنامه، COLMAP و GLOMAP است. باز کردن ZIP، نصب
   جداگانهٔ Git یا نصب پایتون لازم نیست.
۲. برای به‌روزرسانی، برنامه را ببندید و فایل
   `PhotoMeshStudio_Update_<new-version>.exe` را اجرا کنید. اگر مسیر نصب
   خودکار پیدا نشد، همان پوشهٔ نصب قبلی را انتخاب کنید. به‌روزرسان، سازگاری
   محیط نصب‌شده را بررسی می‌کند و در صورت تغییر وابستگی‌ها، محیط جدید را هم
   همراه دارد.
۳. برنامه را از منوی Start باز کنید. پروژه‌های `.pmsx`، پوشه‌های نتیجهٔ
   `.files` و عکس‌های اصلی در محل خود باقی می‌مانند.

هر نسخه یک فایل نصب EXE و یک فایل به‌روزرسانی EXE دارد و هر دو آفلاین‌اند.
دستور ساخت آن‌ها در `packaging/RELEASE.md` آمده است.

### اجرای کد منبع

اگر می‌خواهید خود کد را اجرا کنید، Git for Windows و پایتون ۳٫۱۰ تا ۳٫۱۳
را نصب کنید و دستورهای بخش English را اجرا کنید. پس از آن برای به‌روزرسانی
نسخهٔ کد منبع از `update.bat` استفاده کنید. نسخه‌ای که با EXE نصب شده است
با فایل به‌روزرسانی همان نسخه به‌روز می‌شود.
