"""0.1.12: splash from the first moment, simpler menus (no "not implemented"
placeholders), shortcut icon = the app .ico (and repaired by the updater)."""
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ 1 splash
def test_splash_is_not_qsplashscreen_and_shows_fast(qtbot):
    """QSplashScreen (Qt 6) blocks ~1 s in its Show event (waitForWidgetMapped)."""
    from PySide6.QtWidgets import QSplashScreen, QWidget
    from photomesh import splash
    t = time.perf_counter()
    s = splash.show()
    dt = time.perf_counter() - t
    qtbot.addWidget(s)
    assert isinstance(s, QWidget) and not isinstance(s, QSplashScreen)
    assert dt < 0.6, dt
    assert s.isVisible() and s.messages == ["Starting"] and "Starting" in s.message()
    w = QWidget(); qtbot.addWidget(w); w.show()
    s.finish(w)
    assert not s.isVisible()


def test_diag_setup_before_splash_is_light(tmp_path):
    """diag.setup(log_system=False) must not import NumPy / Pillow (they load after the splash)."""
    code = ("import sys; from photomesh import diag; diag.setup(log_system=False); "
            "print(int('numpy' in sys.modules), int('PIL' in sys.modules))")
    env = dict(os.environ, PHOTOMESH_LOG_DIR=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.split() == ["0", "0"], out.stderr


def test_main_order_splash_then_system_info():
    body = _read("main.py")
    body = body[body.index("def main"):]
    assert "diag.setup(log_system=False)" in body
    assert body.index("splash_mod.show()") < body.index("diag.log_system_info()")


def test_log_system_info_writes_line(tmp_path, monkeypatch):
    from photomesh import diag
    import logging
    rec = []
    h = logging.Handler(); h.emit = rec.append
    old = diag.log.level
    diag.log.addHandler(h); diag.log.setLevel(logging.INFO)
    try:
        diag.log_system_info()
    finally:
        diag.log.removeHandler(h); diag.log.setLevel(old)
    assert any("system:" in r.getMessage() for r in rec)


# ------------------------------------------------------------------ 2 menus
def test_no_menu_item_without_handler(window):
    assert window.unhandled_actions == []
    assert not hasattr(window, "not_implemented")
    assert all(a.isEnabled() for a in window.actions.values())


def test_removed_items_are_gone(window):
    """Every removed Menu > Item path is absent from the menu bar (a working item
    with the same name in another place, e.g. View > Layers, is fine)."""
    from photomesh.menus import REMOVED_0112

    def paths(menu, pre):
        for a in menu.actions():
            if a.isSeparator():
                continue
            if a.menu():
                yield from paths(a.menu(), pre + [a.text()])
            else:
                yield " > ".join(pre + [a.text()])
    present = set()
    for m in window.menuBar().actions():
        present |= set(paths(m.menu(), [m.text().strip("&")]))
    assert len(REMOVED_0112) > 200
    assert not present & set(REMOVED_0112)


def test_no_placeholder_panes_or_dead_tools(window):
    assert "Timeline" not in window.docks and "Animation" not in window.docks
    assert not hasattr(window, "selection_button")
    assert not hasattr(window.model_view, "snap_label")
    texts = {a.text() for a in window.actions.values()}
    for gone in ("Upload Project...", "Batch Process...", "Build Panorama...", "Check for Updates...",
                 "Axis Snap", "Rectangle Selection", "Navigation", "Online Knowledge Base", "Import Reference",
                 "Delete Selection", "Timeline", "Animation", "Show Basemap"):
        assert gone not in texts, gone


def test_triggering_menu_items_never_says_not_implemented(window, no_dialogs, monkeypatch):
    monkeypatch.setattr(window, "close", lambda: True)
    logged = []
    monkeypatch.setattr(window, "log", lambda t: logged.append(t))
    layers = window.layer_states()
    seen = set()
    for a in list(window.actions.values()):
        if id(a) in seen:
            continue
        seen.add(id(a)); a.trigger()
        if a.isCheckable():
            a.trigger()
    for k, on in layers.items():                 # Show All / Hide All persist in QSettings: restore
        window.set_layer_visible(k, on)
    if window.transparency_action.isChecked():
        window.transparency_action.setChecked(False)
    assert not [t for t in logged if "not implemented" in t]
    assert "not implemented" not in window.statusBar().currentMessage()


def test_shortcuts_kept(window):
    from PySide6.QtGui import QKeySequence
    for name, seq in (("Save", QKeySequence.Save), ("Undo", QKeySequence.Undo), ("Open...", QKeySequence.Open)):
        assert window.actions[name].shortcut() == QKeySequence(seq), name
    assert window.actions["Full Screen"].shortcut().toString() == "F11"
    assert window.actions["Open Next Photo"].shortcut().toString() == "PgDown"
    assert window.actions["Contents"].shortcut().toString() == "F1"
    assert window.transparency_action.shortcut().toString() == "Ctrl+Shift+T"


def test_processing_menus_unchanged(window):
    for n in ("Align Photos...", "Build Point Cloud...", "Build Model...", "Build Texture...",
              "Build Tiled Model...", "Build DEM...", "Build Orthomosaic...", "Optimize Cameras...",
              "Export Point Cloud...", "Export Model...", "Export Orthomosaic...", "Export DEM...",
              "Import Markers...", "Filter Point Cloud...", "Gradual Selection...", "Refine Markers"):
        assert n in window.actions, n


# ------------------------------------------------------------------ 3 shortcut icon
def test_create_shortcut_uses_app_ico():
    s = _read("packaging/create_shortcut.ps1")
    assert 'photomesh\\resources\\photomesh.ico' in s
    assert "IconLocation = \"$icon,0\"" in s
    assert "python.exe" not in s


def test_fix_shortcut_icon_only_changes_icon():
    s = _read("packaging/fix_shortcut_icon.ps1")
    assert all(ord(c) < 128 for c in s)
    assigned = {l.split("=")[0].strip() for l in s.splitlines() if l.strip().startswith("$lnk.") and "=" in l
                and "==" not in l and "-eq" not in l}
    assert assigned == {"$lnk.IconLocation"}
    assert "PhotoMesh Studio.lnk" in s and 'photomesh\\resources\\photomesh.ico' in s
    assert "Remove-Item" not in s and "Delete" not in s
    u = _read("packaging/update.ps1")
    assert "fix_shortcut_icon.ps1" in u


def test_app_runs_icon_fix_once_per_version(monkeypatch):
    from photomesh import shortcut_icon as si

    class S(dict):
        def value(self, k, d=None): return self.get(k, d)
        def setValue(self, k, v): self[k] = v
    calls = []
    st = S()
    assert si.maybe_fix(st, "0.1.12", popen=lambda *a, **k: calls.append(a), platform="linux") is False
    assert si.maybe_fix(st, "0.1.12", popen=lambda *a, **k: calls.append((a, k)), platform="win32") is True
    assert si.maybe_fix(st, "0.1.12", popen=lambda *a, **k: calls.append((a, k)), platform="win32") is False
    assert len(calls) == 1
    cmd, kw = calls[0][0][0], calls[0][1]
    assert cmd[0] == "powershell" and si.SCRIPT in cmd and kw["creationflags"] == 0x08000000
    assert si.maybe_fix(st, "0.1.13", popen=lambda *a, **k: calls.append((a, k)), platform="win32") is True


def test_version():
    from photomesh import APP_VERSION
    assert tuple(map(int, APP_VERSION.split("."))) >= (0, 1, 12)
