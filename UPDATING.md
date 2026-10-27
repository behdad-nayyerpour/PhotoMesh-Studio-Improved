# Installing new versions / نصب نسخه‌های جدید

## English

Download `PhotoMeshStudio_Setup_<version>.exe` from the
[latest release](https://github.com/behdad-nayyerpour/photomesh-studio-improved/releases/latest).
Close PhotoMesh Studio, then run Setup. For an existing EXE installation,
select the same installation folder if prompted. Setup replaces the app,
Python runtime, and bundled tools with the new version; there is no separate
Update EXE. On a new computer, run the same Setup file.

Keep `.pmsx` projects, their `.files` result folders, and source photos
outside the installation folder. Setup does not delete those external files.
No ZIP extraction, Git, or system Python installation is needed to run the
EXE release. A `.sha256` file is published beside Setup for integrity checks.

Developers running from source can clone the repository and run `setup.bat`
and `run.bat`. The older portable ZIP and Git update scripts are retained
for legacy source installations, not for the EXE release.

## فارسی

فایل `PhotoMeshStudio_Setup_<version>.exe` را از
[آخرین نسخه](https://github.com/behdad-nayyerpour/photomesh-studio-improved/releases/latest)
دریافت کنید. برنامه را ببندید و فایل Setup را اجرا کنید. اگر نسخهٔ EXE از
قبل نصب است، در صورت پرسش همان مسیر نصب قبلی را انتخاب کنید. نصب‌کننده
کد برنامه، محیط Python و ابزارهای همراه را با نسخهٔ جدید جایگزین می‌کند.
فایل جداگانه‌ای به نام Update منتشر نمی‌شود. برای رایانهٔ جدید هم همین
فایل Setup کافی است.

پروژه‌های `.pmsx`، پوشه‌های نتیجهٔ `.files` و عکس‌های اصلی را بیرون
از پوشهٔ نصب نگه دارید. نصب‌کننده آن فایل‌های بیرونی را پاک نمی‌کند.
برای نصب نسخهٔ EXE، بازکردن ZIP یا نصب جداگانهٔ Git و Python لازم نیست.
فایل `.sha256` کنار Setup برای بررسی سلامت دانلود منتشر می‌شود.

اسکریپت‌های قدیمیِ به‌روزرسانی Git فقط برای نصب‌های قدیمیِ کد منبع و
نسخه‌های portable باقی مانده‌اند و بخشی از انتشار EXE نیستند.
