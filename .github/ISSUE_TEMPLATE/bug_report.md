---
name: Bug report
about: Report a crash or a failed step (PhotoMesh Studio fills this in for you)
title: "[0.x.y] <step> failed: <error>"
labels: bug
assignees: ''
---

<!--
Easiest way: in PhotoMesh Studio use the error dialog's "Report to GitHub" button,
or Help > Report a Problem... - the app fills in everything below.
Reports are saved in %LOCALAPPDATA%\PhotoMeshStudio\reports (report_YYYYMMDD_HHMMSS.md + .zip);
please drag the .zip into this issue. No photos, no coordinates, no personal paths are included.

ساده‌ترین راه: در PhotoMesh Studio دکمهٔ «Report to GitHub» در پنجرهٔ خطا یا Help > Report a Problem...
را بزنید؛ برنامه همه‌چیز را پر می‌کند. فایل zip گزارش را به این صفحه بکشید.
-->

# PhotoMesh Studio error report

| | |
|---|---|
| Report | report_YYYYMMDD_HHMMSS |
| Time | |
| App | PhotoMesh Studio 0.x.y (commit ...) |
| Kind | error / exception / thread / manual |
| Step | e.g. Build Point Cloud > Depth maps |
| Error | message shown in the error dialog |
| Exit code | e.g. -1073740791 (0xC0000409 STATUS_STACK_BUFFER_OVERRUN) - ... |

## What happened
What were you doing? What did you expect?

## Step settings
```json
{ "settings": { "quality": "..." } }
```

## Command
```text
colmap ... (paths anonymised)
```

## Traceback
```text
Traceback (most recent call last): ...
```

## Project
| | |
|---|---|
| Photos | N (enabled, aligned, with GPS) |
| Photo sizes | |
| Tie points / Dense points / Mesh faces | |
| CRS | name only |

## System
| | |
|---|---|
| OS | |
| CPU / Threads | |
| RAM | total, free |
| GPU | name, VRAM, driver |
| Disk | free on the project drive |
| Python / Packages | |
| COLMAP / GLOMAP | |

## Log (last 300 lines)
```text
...
```
