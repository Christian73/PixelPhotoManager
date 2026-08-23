# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Complement to test_folder_watcher_suppression.py: the tree plumbing of
FolderWatcher -- _TreeScanThread (a real QThread),
set_folders/_apply_scan (stale generation included), _add_tree/_remove_tree,
subfolders appearing/disappearing in _process, real debounce through
QFileSystemWatcher, and the _is_hidden helpers."""
import os
import shutil

from src.library.folder_watcher import FolderWatcher, _TreeScanThread, _is_hidden


def _make_tree(root):
    """root/{a.jpg, sub/{b.jpg}, .hidden/{h.jpg}}"""
    (root / "a.jpg").write_bytes(b"x")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.jpg").write_bytes(b"x")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "h.jpg").write_bytes(b"x")


class TestIsHidden:
    def test_dot_prefix(self, tmp_path):
        d = tmp_path / ".dir"
        d.mkdir()
        assert _is_hidden(str(d)) is True

    def test_normal(self, tmp_path):
        assert _is_hidden(str(tmp_path)) is False


class TestTreeScanThread:
    def test_recursive_scan_skips_hidden(self, qtbot, tmp_path):
        _make_tree(tmp_path)
        thread = _TreeScanThread([str(tmp_path)])
        results: list = []
        thread.finished_scan.connect(results.extend)
        with qtbot.waitSignal(thread.finished_scan, timeout=5000):
            thread.start()
        thread.wait(5000)

        by_path = {path: (files, dirs) for path, files, dirs in results}
        assert str(tmp_path) in by_path
        assert str(tmp_path / "sub") in by_path
        assert str(tmp_path / ".hidden") not in by_path
        files, dirs = by_path[str(tmp_path)]
        assert files == frozenset({"a.jpg"})
        assert dirs == frozenset({"sub"})

    def test_nonexistent_folder_yields_nothing(self, qtbot, tmp_path):
        thread = _TreeScanThread([str(tmp_path / "absent")])
        results: list = []
        thread.finished_scan.connect(results.extend)
        with qtbot.waitSignal(thread.finished_scan, timeout=5000):
            thread.start()
        thread.wait(5000)
        assert results == []


class TestSetFolders:
    def test_snapshots_and_watch_paths_populated(self, qtbot, tmp_path):
        _make_tree(tmp_path)
        watcher = FolderWatcher()
        watcher.set_folders([str(tmp_path)])
        qtbot.waitUntil(lambda: len(watcher._snapshots) == 2, timeout=5000)
        assert set(watcher._snapshots) == {str(tmp_path), str(tmp_path / "sub")}
        assert set(watcher._watcher.directories()) == set(watcher._snapshots)

    def test_stale_generation_result_discarded(self, tmp_path):
        watcher = FolderWatcher()
        watcher._scan_generation = 5
        watcher._apply_scan(4, [(str(tmp_path), frozenset(), frozenset())])
        assert watcher._snapshots == {}

    def test_set_folders_replaces_previous(self, qtbot, tmp_path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        watcher = FolderWatcher()
        watcher.set_folders([str(d1)])
        qtbot.waitUntil(lambda: str(d1) in watcher._snapshots, timeout=5000)
        watcher.set_folders([str(d2)])
        qtbot.waitUntil(lambda: str(d2) in watcher._snapshots, timeout=5000)
        assert str(d1) not in watcher._snapshots


class TestAddRemoveTree:
    def test_add_tree_recursive_skips_hidden(self, qtbot, tmp_path):
        _make_tree(tmp_path)
        watcher = FolderWatcher()
        watcher._add_tree(str(tmp_path))
        assert set(watcher._snapshots) == {str(tmp_path), str(tmp_path / "sub")}

    def test_add_tree_nonexistent_noop(self, qtbot, tmp_path):
        watcher = FolderWatcher()
        watcher._add_tree(str(tmp_path / "absent"))
        assert watcher._snapshots == {}

    def test_remove_tree_removes_children(self, qtbot, tmp_path):
        _make_tree(tmp_path)
        watcher = FolderWatcher()
        watcher._add_tree(str(tmp_path))
        watcher._remove_tree(str(tmp_path))
        assert watcher._snapshots == {}

    def test_remove_tree_keeps_siblings(self, qtbot, tmp_path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        watcher = FolderWatcher()
        watcher._add_tree(str(d1))
        watcher._add_tree(str(d2))
        watcher._remove_tree(str(d1))
        assert set(watcher._snapshots) == {str(d2)}


class TestProcessSubfolders:
    def test_new_subfolder_emits_and_watches(self, qtbot, tmp_path):
        watcher = FolderWatcher()
        watcher._take_snapshot(str(tmp_path))
        emitted: list[str] = []
        watcher.subfolder_added.connect(emitted.append)

        new_sub = tmp_path / "nouveau"
        new_sub.mkdir()
        (new_sub / "p.jpg").write_bytes(b"x")
        watcher._process(str(tmp_path))

        assert emitted == [str(new_sub)]
        assert str(new_sub) in watcher._snapshots

    def test_removed_subfolder_unwatched(self, qtbot, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        watcher = FolderWatcher()
        watcher._take_snapshot(str(tmp_path))
        watcher._take_snapshot(str(sub))

        shutil.rmtree(sub)
        watcher._process(str(tmp_path))

        assert str(sub) not in watcher._snapshots

    def test_process_deleted_dir_is_noop(self, qtbot, tmp_path):
        watcher = FolderWatcher()
        gone = tmp_path / "gone"
        gone.mkdir()
        watcher._take_snapshot(str(gone))
        shutil.rmtree(gone)
        emitted: list[str] = []
        watcher.files_changed.connect(emitted.append)
        watcher._process(str(gone))
        assert emitted == []


class TestRealWatcherPlumbing:
    def test_file_creation_triggers_files_changed(self, qtbot, tmp_path):
        """The complete path: a real QFileSystemWatcher -> debounce -> files_changed."""
        (tmp_path / "existant.jpg").write_bytes(b"x")
        watcher = FolderWatcher()
        watcher.set_folders([str(tmp_path)])
        qtbot.waitUntil(lambda: str(tmp_path) in watcher._snapshots, timeout=5000)

        with qtbot.waitSignal(watcher.files_changed, timeout=5000) as blocker:
            (tmp_path / "nouveau.jpg").write_bytes(b"x")

        assert blocker.args == [str(tmp_path)]

    def test_debounce_coalesces_events(self, qtbot, tmp_path):
        """Several close events on the same folder -> a single emission."""
        watcher = FolderWatcher()
        watcher._take_snapshot(str(tmp_path))
        emitted: list[str] = []
        watcher.files_changed.connect(emitted.append)

        (tmp_path / "f1.jpg").write_bytes(b"x")
        watcher._on_dir_changed(str(tmp_path))
        (tmp_path / "f2.jpg").write_bytes(b"x")
        watcher._on_dir_changed(str(tmp_path))

        qtbot.waitUntil(lambda: emitted != [], timeout=5000)
        assert emitted == [str(tmp_path)]
