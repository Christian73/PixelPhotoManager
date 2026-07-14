# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QThread, QTimer, Signal

logger = logging.getLogger(__name__)

_DEBOUNCE_MS = 400


def _is_hidden(path: str) -> bool:
    if os.path.basename(path).startswith("."):
        return True
    try:
        return bool(os.stat(path).st_file_attributes & 0x2)
    except (AttributeError, OSError):
        return False


class _TreeScanThread(QThread):
    """Parcourt récursivement les dossiers racine hors thread UI.

    os.scandir répété sur une grosse arborescence (ou un lecteur réseau lent)
    peut largement dépasser le budget de 50ms — cf. règle "l'UI ne bloque
    jamais" (CLAUDE.md).
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
    Surveille un ensemble de dossiers racine (récursivement) via QFileSystemWatcher.

    Signaux :
        files_changed(path)   — des fichiers ont été ajoutés ou supprimés dans path
        subfolder_added(path) — un nouveau sous-dossier est apparu à path
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

    # ------------------------------------------------------------------ public

    def set_folders(self, folders: list[str]) -> None:
        """Remplace l'ensemble surveillé par ces dossiers racine (récursif).

        Le parcours récursif se fait dans un QThread (cf. _TreeScanThread) —
        une grosse arborescence ou un lecteur réseau lent rendrait sinon cet
        appel bloquant pour l'UI bien au-delà de 50ms."""
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

    def _apply_scan(self, generation: int, results: list) -> None:
        if generation != self._scan_generation:
            return  # un set_folders() plus récent a eu lieu entretemps — résultat obsolète
        for path, files, dirs in results:
            self._snapshots[path] = (files, dirs)
            self._watcher.addPath(path)
        logger.debug("FolderWatcher : %d dossier(s) surveillé(s)", len(self._snapshots))

    # ------------------------------------------------------------------ internal

    def _add_tree(self, path: str) -> None:
        """Ajoute path et tous ses sous-dossiers non cachés au watcher."""
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
        """Supprime path et tous ses sous-snapshots du watcher."""
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
        self._debounce.start()  # repart à chaque nouvel événement

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

        if new_files != old_files:
            logger.debug("FolderWatcher : fichiers modifiés dans %s", path)
            self.files_changed.emit(path)

        for name in (new_dirs - old_dirs):
            subdir = os.path.join(path, name)
            logger.debug("FolderWatcher : nouveau sous-dossier %s", subdir)
            self._add_tree(subdir)
            self.subfolder_added.emit(subdir)

        for name in (old_dirs - new_dirs):
            self._remove_tree(os.path.join(path, name))
