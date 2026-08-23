# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import time

from PySide6.QtCore import QFileSystemWatcher, QObject, QThread, QTimer, Signal

logger = logging.getLogger(__name__)

_DEBOUNCE_MS = 400


# Re-export: shared implementation (cf. fs_utils), the alias is kept for the
# internal uses and the existing tests.
from src.library.fs_utils import is_hidden_path as _is_hidden  # noqa: E402


class _TreeScanThread(QThread):
    """Walks the root folders recursively, outside the UI thread.

    A repeated os.scandir on a large tree (or a slow network drive) can go
    well beyond the 50 ms budget — cf. the "the UI never blocks" rule
    (CLAUDE.md).
    """

    finished_scan = Signal(list)  # list[tuple[str, frozenset, frozenset]]

    def __init__(self, folders: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._folders = folders

    def run(self) -> None:
        results: list[tuple[str, frozenset, frozenset]] = []
        for folder in self._folders:
            self._scan(folder, results)
        self.finished_scan.emit(results)

    def _scan(self, path: str, results: list) -> None:
        if not os.path.isdir(path):
            return
        try:
            entries = list(os.scandir(path))
            files = frozenset(e.name for e in entries if e.is_file())
            dirs = frozenset(
                e.name for e in entries
                if e.is_dir(follow_symlinks=False) and not _is_hidden(e.path)
            )
        except OSError:
            files, dirs = frozenset(), frozenset()
        results.append((path, files, dirs))
        for name in dirs:
            self._scan(os.path.join(path, name), results)


class FolderWatcher(QObject):
    """
    Watches a set of root folders (recursively) through QFileSystemWatcher.

    Signals:
        files_changed(path)   — files were added to or removed from path
        subfolder_added(path) — a new subfolder appeared at path
    """

    files_changed   = Signal(str)
    subfolder_added = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        # path -> (frozenset filenames, frozenset subdir names)
        self._snapshots: dict[str, tuple[frozenset, frozenset]] = {}
        self._pending: set[str] = set()
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._flush_pending)
        self._scan_thread: "_TreeScanThread | None" = None
        self._scan_generation = 0
        # Self-inflicted changes to absorb: dir -> (file names,
        # time.monotonic deadline). When the application deletes/moves files
        # itself (the Del key, drag & drop), the watcher event that follows only
        # reports what the UI has already handled — without this absorption,
        # every deletion triggered a full rescan of the folder followed by a
        # redundant album/person refresh.
        self._self_deleted: dict[str, tuple[set[str], float]] = {}
        self._self_added: dict[str, tuple[set[str], float]] = {}

    # ------------------------------------------------------------------ public

    def set_folders(self, folders: list[str]) -> None:
        """Replaces the watched set with these root folders (recursive).

        The recursive walk happens in a QThread (cf. _TreeScanThread) — a
        large tree or a slow network drive would otherwise make this call
        block the UI well beyond 50 ms."""
        self._scan_generation += 1
        generation = self._scan_generation

        existing = self._watcher.directories()
        if existing:
            self._watcher.removePaths(existing)
        self._snapshots.clear()
        self._pending.clear()

        thread = _TreeScanThread(list(folders), self)
        thread.finished_scan.connect(
            lambda results, g=generation: self._apply_scan(g, results)
        )
        thread.finished.connect(thread.deleteLater)
        self._scan_thread = thread
        thread.start()

    def notify_self_deletions(self, paths: list[str], ttl_s: float = 10.0) -> None:
        """Declares files the application is about to delete itself: the watcher
        events that merely report those disappearances will be absorbed (no
        files_changed → no redundant rescan). An EXTERNAL change in the same
        folder (another file added or removed) is still emitted. The TTL
        bounds the case where the deletion eventually fails (the name would
        otherwise stay absorbed indefinitely)."""
        self._notify_self(self._self_deleted, paths, ttl_s)

    def notify_self_additions(self, paths: list[str], ttl_s: float = 10.0) -> None:
        """Counterpart of the above for files the application is about to create
        itself (the destination of a drag & drop move)."""
        self._notify_self(self._self_added, paths, ttl_s)

    @staticmethod
    def _notify_self(table: dict, paths: list[str], ttl_s: float) -> None:
        deadline = time.monotonic() + ttl_s
        for path in paths:
            directory = os.path.dirname(os.path.normpath(path))
            names, _ = table.get(directory, (set(), 0.0))
            names.add(os.path.basename(path))
            table[directory] = (names, deadline)

    @staticmethod
    def _consume_suppressed(table: dict, directory: str, names: frozenset) -> frozenset:
        """Returns the intersection of names with the names declared for this
        folder (if the deadline has not passed) and removes the consumed
        names from the table."""
        entry = table.get(directory)
        if entry is None:
            return frozenset()
        declared, deadline = entry
        if time.monotonic() > deadline:
            del table[directory]
            return frozenset()
        consumed = names & declared
        declared -= consumed
        if declared:
            table[directory] = (declared, deadline)
        else:
            del table[directory]
        return frozenset(consumed)

    def _apply_scan(self, generation: int, results: list) -> None:
        if generation != self._scan_generation:
            return  # a more recent set_folders() happened in the meantime — stale result
        for path, files, dirs in results:
            self._snapshots[path] = (files, dirs)
            self._watcher.addPath(path)
        logger.debug("FolderWatcher : %d dossier(s) surveillé(s)", len(self._snapshots))

    # ------------------------------------------------------------------ internal

    def _add_tree(self, path: str) -> None:
        """Adds path and all of its non-hidden subfolders to the watcher."""
        if not os.path.isdir(path):
            return
        self._take_snapshot(path)
        self._watcher.addPath(path)
        try:
            for entry in os.scandir(path):
                if entry.is_dir(follow_symlinks=False) and not _is_hidden(entry.path):
                    self._add_tree(entry.path)
        except PermissionError:
            pass

    def _remove_tree(self, path: str) -> None:
        """Removes path and all of its sub-snapshots from the watcher."""
        prefix = path + os.sep
        to_remove = [p for p in self._snapshots if p == path or p.startswith(prefix)]
        for p in to_remove:
            del self._snapshots[p]
        if to_remove:
            self._watcher.removePaths(to_remove)

    def _take_snapshot(self, path: str) -> None:
        try:
            entries = list(os.scandir(path))
            files = frozenset(e.name for e in entries if e.is_file())
            dirs  = frozenset(
                e.name for e in entries
                if e.is_dir(follow_symlinks=False) and not _is_hidden(e.path)
            )
        except OSError:
            files, dirs = frozenset(), frozenset()
        self._snapshots[path] = (files, dirs)

    def _on_dir_changed(self, path: str) -> None:
        self._pending.add(path)
        self._debounce.start()  # restarts on every new event

    def _flush_pending(self) -> None:
        pending = list(self._pending)
        self._pending.clear()
        for path in pending:
            self._process(path)

    def _process(self, path: str) -> None:
        old_files, old_dirs = self._snapshots.get(path, (frozenset(), frozenset()))

        try:
            entries = list(os.scandir(path))
        except OSError:
            return

        new_files = frozenset(e.name for e in entries if e.is_file())
        new_dirs  = frozenset(
            e.name for e in entries
            if e.is_dir(follow_symlinks=False) and not _is_hidden(e.path)
        )

        self._snapshots[path] = (new_files, new_dirs)

        # Absorb the changes the application caused itself
        # (cf. notify_self_deletions/notify_self_additions): any other change
        # in the same event is emitted normally.
        disappeared = old_files - new_files
        appeared    = new_files - old_files
        sup_del = self._consume_suppressed(self._self_deleted, path, disappeared)
        sup_add = self._consume_suppressed(self._self_added, path, appeared)
        if (disappeared - sup_del) or (appeared - sup_add):
            logger.debug("FolderWatcher : fichiers modifiés dans %s", path)
            self.files_changed.emit(path)
        elif disappeared or appeared:
            logger.debug(
                "FolderWatcher : changement auto-infligé absorbé dans %s", path
            )

        for name in (new_dirs - old_dirs):
            subdir = os.path.join(path, name)
            logger.debug("FolderWatcher : nouveau sous-dossier %s", subdir)
            self._add_tree(subdir)
            self.subfolder_added.emit(subdir)

        for name in (old_dirs - new_dirs):
            self._remove_tree(os.path.join(path, name))
