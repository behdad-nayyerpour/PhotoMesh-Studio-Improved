"""0.1.8: Save As copying project data into a OneDrive folder failed with
"PermissionError: [WinError 5] Access is denied" when renaming
chunk0.partial -> chunk0 (GitHub issue 2). The copy now goes straight into
its final folder (no folder rename), brief locks are retried, and failures
clean up after themselves."""
import json
import os
import shutil

import pytest

from test_release_017 import _processed


@pytest.fixture
def fast_retry(monkeypatch):
    from photomesh import datafolder as dfo
    monkeypatch.setattr(dfo, "RETRY_DELAYS", (0, 0))


@pytest.fixture
def no_dir_rename(monkeypatch):
    """Like a OneDrive/AV-watched folder: renaming a folder is denied."""
    real_replace, real_rename = os.replace, os.rename

    def guard(real):
        def f(a, b, *k, **kw):
            if os.path.isdir(a):
                raise PermissionError(5, "Access is denied", a, None, b)
            return real(a, b, *k, **kw)
        return f
    monkeypatch.setattr(os, "replace", guard(real_replace))
    monkeypatch.setattr(os, "rename", guard(real_rename))


def _onedrive_project(tmp_path):
    """Like the report: spaces in the folder names (OneDrive - AFRY, Test Drone)."""
    return _processed(tmp_path / "OneDrive - AFRY" / "Backup Drawings" / "Test", name="Test Drone")


def test_save_as_copy_needs_no_folder_rename(tmp_path, no_dir_rename, fast_retry):
    from photomesh import datafolder as dfo
    from photomesh.project import Project
    prj = _onedrive_project(tmp_path)
    old = prj.chunk.workdir
    new = str(tmp_path / "OneDrive - AFRY" / "Backup Drawings" / "Test-Lot3-Drone" / "Test-Lot3-Drone.pmsx")
    items = dfo.plan(prj, new, save_as=True)
    dfo.apply(dfo.execute(items))
    prj.save(new)                                     # file replace (not a folder) still allowed
    wd = prj.chunk.workdir
    assert wd == os.path.join(os.path.dirname(new), "Test-Lot3-Drone.files", "chunk0")
    assert os.path.exists(os.path.join(wd, "dense", "point_cloud.ply")) and not dfo.is_incomplete(wd)
    assert not os.path.exists(wd + ".partial")
    assert os.path.exists(os.path.join(old, "dense", "point_cloud.ply"))          # original kept
    assert Project.load(new).chunk.workdir == wd
    assert json.load(open(new, encoding="utf-8"))["chunks"][0]["workdir"] == "Test-Lot3-Drone.files/chunk0"


def test_brief_file_lock_is_retried(tmp_path, monkeypatch, fast_retry):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))
    real, fails = shutil.copy2, [2]

    def flaky(s, d, *a, **k):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used", d)
        return real(s, d, *a, **k)
    monkeypatch.setattr(shutil, "copy2", flaky)
    done = dfo.execute(items)
    assert done and fails[0] == 0 and os.path.exists(os.path.join(items[0]["dst"], "dense", "point_cloud.ply"))


def test_failed_copy_is_removed_and_project_unchanged(tmp_path, monkeypatch, fast_retry):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    old = prj.chunk.workdir
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))

    def broken(s, d, *a, **k):
        raise PermissionError(5, "Access is denied", d)
    monkeypatch.setattr(shutil, "copy2", broken)
    with pytest.raises(PermissionError):
        dfo.execute(items)
    assert not os.path.exists(items[0]["dst"]) and prj.chunk.workdir == old


def test_leftover_is_named_when_cleanup_fails(tmp_path, monkeypatch, fast_retry):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    items = dfo.plan(prj, str(tmp_path / "b" / "c.pmsx"))
    monkeypatch.setattr(dfo, "remove_tree", lambda p: False)
    with pytest.raises(InterruptedError) as ei:
        dfo.execute(items, cancelled=lambda: True)
    assert any(items[0]["dst"] in n for n in getattr(ei.value, "__notes__", []))
    assert dfo.is_incomplete(items[0]["dst"])          # marked, so a later attempt can reuse it


def test_unfinished_copy_is_reused_never_a_foreign_folder(tmp_path, fast_retry):
    from photomesh import datafolder as dfo
    prj = _processed(tmp_path / "a")
    new = str(tmp_path / "b" / "c.pmsx")
    root = dfo.data_folder(new)
    stale = os.path.join(root, "chunk0"); os.makedirs(stale)
    open(os.path.join(stale, dfo.INCOMPLETE_MARK), "w").close()
    open(os.path.join(stale, "junk.bin"), "wb").write(b"x")
    items = dfo.plan(prj, new)
    assert items[0]["dst"] == stale                    # an earlier unfinished copy: reused
    dfo.execute(items)
    assert not os.path.exists(os.path.join(stale, "junk.bin")) and not dfo.is_incomplete(stale)
    # a folder with the user's own files is never reused or emptied
    mine = os.path.join(root, "chunk0_x"); os.makedirs(mine); open(os.path.join(mine, "keep.txt"), "w").close()
    assert dfo.unique_target(root, "chunk0_x") != mine
    with pytest.raises(FileExistsError):
        dfo.execute([dict(items[0], dst=mine)])
    assert os.path.exists(os.path.join(mine, "keep.txt"))


def test_remove_tree_handles_read_only_files(tmp_path):
    import stat
    from photomesh import datafolder as dfo
    d = tmp_path / "x"; (d / "s").mkdir(parents=True)
    f = d / "s" / "ro.bin"; f.write_bytes(b"1"); os.chmod(f, stat.S_IREAD)
    assert dfo.remove_tree(str(d)) and not d.exists()


def test_job_error_text_includes_notes():
    from photomesh.bgjob import BackgroundJob

    def fn(report, cancelled):
        e = PermissionError("Access is denied")
        e.add_note("Could not remove the unfinished copy (delete it later): X")
        raise e
    job = BackgroundJob(fn, name="t")
    job._run()
    assert "Access is denied" in job.error and "delete it later" in job.error


def test_gui_save_as_into_locked_folder_tree(window, no_dialogs, tmp_path, monkeypatch, no_dir_rename, fast_retry):
    from PySide6.QtWidgets import QMessageBox, QFileDialog
    prj = _onedrive_project(tmp_path)
    window.open_project(prj.path)
    new = tmp_path / "OneDrive - AFRY" / "Backup Drawings" / "Test-Lot3-Drone" / "Test-Lot3-Drone.pmsx"
    new.parent.mkdir(parents=True)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(new), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    assert window.save_project_as()
    assert no_dialogs == [] and window.project.path == str(new) and new.exists()
    assert window.chunk.workdir == str(new.parent / "Test-Lot3-Drone.files" / "chunk0")


def test_save_as_into_subfolder_of_old_data_does_not_nest(tmp_path):
    """dst inside the source working folder: copied once, no endless nesting."""
    from photomesh import datafolder as dfo
    from photomesh.project import Project, Chunk
    old = tmp_path / "old"; (old / "dense").mkdir(parents=True)
    (old / "dense" / "a.bin").write_bytes(b"x" * 1000)
    p = Project(); p.chunks = [Chunk(workdir=str(old))]
    items = dfo.plan(p, str(old / "sub" / "new.pmsx"), save_as=True)
    assert items[0]["bytes"] == 1000
    dfo.apply(dfo.execute(items))
    dst = items[0]["dst"]
    files = [os.path.relpath(os.path.join(r, f), dst) for r, _d, fs in os.walk(dst) for f in fs]
    assert sorted(files) == [os.path.join("dense", "a.bin")]
