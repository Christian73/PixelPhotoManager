# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Background threads of MainWindow (the CLAUDE.md rule: the UI never
blocks). Extracted from main_window.py — the names prefixed with an underscore
are kept for the history and the existing tests; they stay implementation
details of MainWindow, not a plugin API."""

import logging
import os
from PySide6.QtCore import QThread, Signal

from src.core.i18n import translate
from src.faces.clusterer import reset_clustering_cache
from src.library.dedup_cache import DedupCache

logger = logging.getLogger(__name__)


class _CatalogLoadThread(QThread):
    """Loads get_all_photos() off the UI thread and emits the results in batches."""

    batch_ready = Signal(list)  # list[PhotoInfo]

    def __init__(self, catalog, batch_size: int = 300, reverse: bool = False, parent=None):
        super().__init__(parent)
        self._catalog = catalog
        self._batch_size = batch_size
        self._reverse = reverse
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        # get_all_photos() is sorted chronologically descending (SQL); "reverse"
        # flips it to ascending to follow the "Display order" setting — the "All
        # photos" view always stays chronological, only the direction is
        # configurable (cf. MainWindow._sort_photos_for_display).
        photos = self._catalog.get_all_photos()
        if self._reverse:
            photos = list(reversed(photos))
        for i in range(0, len(photos), self._batch_size):
            if self._stop:
                break
            self.batch_ready.emit(photos[i : i + self._batch_size])


class _PhotoQueryThread(QThread):
    """Runs a catalog/face_db query in a secondary thread.

    The display sort (O(n log n) over the whole library for "All photos") is
    done here too: the sort parameters are resolved by the caller on the UI
    thread (Config reads) and passed to the thread."""

    photos_ready = Signal(list, str)   # list[PhotoInfo], context_key

    def __init__(self, fn, context_key: str, sort_key_fn=None,
                 sort_reverse: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._fn           = fn
        self._context_key  = context_key
        self._sort_key_fn  = sort_key_fn
        self._sort_reverse = sort_reverse

    def run(self) -> None:
        try:
            photos = self._fn()
            if self._sort_key_fn is not None:
                photos = sorted(photos, key=self._sort_key_fn,
                                reverse=self._sort_reverse)
            self.photos_ready.emit(photos, self._context_key)
        except Exception:
            self.photos_ready.emit([], self._context_key)


class _DeleteWorkerThread(QThread):
    """Sends the files to the Windows recycle bin then purges the catalog/
    thumbnails/faces in batch, off the UI thread (the CLAUDE.md rule: the UI
    never blocks). Never a permanent unlink: if the recycle bin is unavailable
    (a network drive…), the file is left intact, its path goes into errors and
    the catalog is NOT purged for it."""

    progress        = Signal(int, int)     # done, total (status bar label)
    finished_delete = Signal(list, list)   # deleted_paths: list[str], errors: list[str]

    def __init__(self, paths: list[str], catalog, thumb_cache, face_db,
                 parent=None) -> None:
        super().__init__(parent)
        self._paths       = list(paths)
        self._catalog     = catalog
        self._thumb_cache = thumb_cache
        self._face_db     = face_db

    def run(self) -> None:
        from src.library.trash import move_to_trash
        deleted: list[str] = []
        errors:  list[str] = []
        for i, path in enumerate(self._paths):
            try:
                move_to_trash(path)
                deleted.append(path)
            except FileNotFoundError:
                # Already absent from the disk: purge the catalog anyway
                # (the equivalent of the former missing_ok=True).
                deleted.append(path)
            except Exception as e:
                errors.append(
                    translate("DeleteWorker",
                              "{name}: could not move to the recycle bin ({err}) — the file "
                              "was NOT deleted."
                              ).format(name=os.path.basename(path), err=e)
                )
            self.progress.emit(i + 1, len(self._paths))
        if deleted:
            try:
                # In batch: delete_photos also dissolves the duplicate groups
                # that have become singletons, in the same transaction.
                self._catalog.delete_photos(deleted)
                self._thumb_cache.invalidate_many(deleted)
                self._face_db.delete_for_paths(deleted)
            except Exception:
                logger.exception("Purge catalogue/vignettes/visages après suppression")
        self.finished_delete.emit(deleted, errors)


class _DupMigrationThread(QThread):
    """Runs the migration of the duplicate groups with conflicting EXIF dates
    then counts the remaining groups (sidebar badge), off the UI thread: on the
    first launch after an upgrade, the migration loads ALL the groups with
    their photos — run before that in MainWindow.__init__, it delayed the first
    display of the window by just as much.

    Migration: dissolves the existing groups containing at least two members
    whose EXIF date is known and different (cf.
    duplicate_detector.py::_dates_differ). Such a group can no longer be
    *created* today, but the incrementality of the detection
    (compared_tier1/compared_tier2, dedup_cache.py) never spontaneously
    recompares nor dissolves a group already formed before this rule was added
    — cf. dedup_exif_date_exclusion_2026-07 in memory.

    In addition to dissolving the group in the database
    (duplicate_group_id=NULL, like Catalog.ignore_duplicate_group), it also
    removes its members from compared_tier1/tier2 so that they are fully
    recompared on the next pass rather than staying "old×old pairs" that are
    never re-evaluated — without that, the dissolution would not last: the next
    seed_groups() would simply find them merged exactly as before, since
    nothing would ever have confronted them again.

    Naturally idempotent: once those groups are dissolved, the date rule
    definitively prevents their recreation, so this sweep finds nothing more on
    the following starts — no "already run" flag needed.

    Sequencing: _start_duplicate_detection must never start before the end of
    this migration (cf. _on_persons_thumbnails_ready_start_duplicates),
    otherwise seed_groups would be seeded with the groups not yet dissolved."""

    done = Signal(int)   # number of duplicate groups remaining (badge)

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog

    def run(self) -> None:
        try:
            self._migrate()
        except Exception:
            logger.exception("Migration des groupes de doublons à dates conflictuelles")
        try:
            self.done.emit(self._catalog.count_duplicate_groups())
        except Exception:
            self.done.emit(0)

    def _migrate(self) -> None:
        if self._catalog.count_duplicate_groups() == 0:
            return
        groups = self._catalog.get_duplicate_groups()
        conflicted_group_ids: list[int] = []
        conflicted_paths: list[str] = []
        for gid, photos in groups.items():
            known_dates = {p.date_taken for p in photos if p.date_taken is not None}
            if len(known_dates) > 1:
                conflicted_group_ids.append(gid)
                conflicted_paths.extend(p.path for p in photos)
        if not conflicted_group_ids:
            return
        for gid in conflicted_group_ids:
            self._catalog.ignore_duplicate_group(gid)
        cache = DedupCache()
        cache.open()
        try:
            cache.remove_compared(conflicted_paths)
        finally:
            cache.close()
        logger.info(
            "Migration doublons : %d groupe(s) à dates EXIF conflictuelles dissous "
            "(%d photo(s) remise(s) en file pour recomparaison complète).",
            len(conflicted_group_ids), len(conflicted_paths),
        )


class _PersonsRefreshThread(QThread):
    """Loads get_persons + enrich_persons + get_unnamed_clusters off the UI thread."""

    result_ready = Signal(list, int)   # persons, unnamed_cluster_count

    def __init__(self, catalog, face_db, parent=None) -> None:
        super().__init__(parent)
        self._catalog  = catalog
        self._face_db  = face_db

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            count = len(self._face_db.get_unnamed_clusters())
            self.result_ready.emit(persons, count)
        except Exception:
            self.result_ready.emit([], 0)


class _ResuggestThread(QThread):
    """Recomputes the suggestions after a cluster is rejected, in a secondary thread."""

    def __init__(self, face_db, cluster_ids: list, exclude_pid, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._cluster_ids = cluster_ids
        self._exclude_pid = exclude_pid

    def run(self) -> None:
        self._face_db.resuggest_clusters(self._cluster_ids, self._exclude_pid)


class _ResetWorkerThread(QThread):
    """
    Waits for the indexing/clustering threads in progress to stop,
    performs the requested DB reset, then emits done(choice).
    """

    done = Signal(int)   # choice: RESET_CLUSTERING or RESET_FULL

    def __init__(
        self,
        face_db,
        choice: int,
        threads_to_wait: list,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._choice  = choice
        self._threads = threads_to_wait   # strong Python refs → kept alive

    def run(self) -> None:
        for t in self._threads:
            try:
                if t.isRunning():
                    t.wait(10_000)   # 10 s max per thread
            except RuntimeError:
                pass   # C++ object already deleted
        if self._choice == 1:   # RESET_CLUSTERING
            self._face_db.reset_clustering()
        else:                    # RESET_FULL
            self._face_db.reset_index()
        # Both resets clear cluster_id in bulk without changing the number of
        # unidentified faces if the library has not moved in the meantime:
        # without invalidating this cache, the clustering that follows (triggered
        # by _on_reset_done) would silently be skipped (cf. reset_clustering_cache).
        reset_clustering_cache()
        self.done.emit(self._choice)
