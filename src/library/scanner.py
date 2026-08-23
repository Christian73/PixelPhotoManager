# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.library.exif_reader import ExifReader, VideoMetadataReader, VIDEO_EXT
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.library.image_loader import RAW_EXT, is_raw_available
from src.core.models import PhotoInfo

logger = logging.getLogger(__name__)

SUPPORTED_EXT = ExifReader.SUPPORTED | VIDEO_EXT | (RAW_EXT if is_raw_available() else set())


# Re-export: shared implementation (cf. fs_utils), the alias is kept for the
# internal uses and the existing tests.
from src.library.fs_utils import is_hidden_path as _is_hidden  # noqa: E402


_BATCH_SIZE = 50   # photos grouped per emission, so as not to flood the event loop


class ScanThread(QThread):
    photos_batch     = Signal(list)   # list[PhotoInfo] — new/modified, in batches
    photos_removed   = Signal(list)   # list[str] — paths removed from the catalog
    progress         = Signal(int, str)
    finished         = Signal(int)

    def __init__(
        self,
        folders: list[str],
        catalog: Catalog,
        thumb_cache: ThumbnailCache,
        force: bool = False,
    ):
        super().__init__()
        self._folders = folders
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._stop_flag = False
        self._force = force

    def run(self) -> None:
        from src.core.thread_journal import journal
        t0 = journal.start("ScanThread", f"Scan de {len(self._folders)} dossier(s)", force=self._force)
        self.setPriority(QThread.LowestPriority)
        total = 0
        processed = 0

        all_files: list[str] = []
        for folder in self._folders:
            if self._stop_flag:
                break
            for root, dirs, files in os.walk(folder):
                if self._stop_flag:
                    break
                dirs[:] = [
                    d for d in dirs
                    if not _is_hidden(os.path.join(root, d))
                    and d != "Originals"
                    and not d.endswith("_assets")
                    and d.lower() != "thumbnails"
                ]
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if Path(fname).suffix.lower() in SUPPORTED_EXT and not _is_hidden(fpath):
                        all_files.append(os.path.normpath(fpath))

        grand_total = len(all_files)
        journal.step("ScanThread", f"{grand_total} fichier(s) découvert(s)", t0)

        known: dict[str, float] = {}
        if not self._force:
            for folder in self._folders:
                known.update(self._catalog.get_known_mtimes(folder))

        batch: list = []
        last_emit = 0.0
        _PROGRESS_INTERVAL = 0.15  # seconds — refresh the status bar often, whatever the processing speed

        for file_count, filepath in enumerate(all_files, 1):
            if self._stop_flag:
                break
            if file_count % 200 == 0:
                self.msleep(5)
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                existing_mtime = known.get(filepath)
                if existing_mtime is not None and abs(existing_mtime - mtime) < 1.0:
                    processed += 1
                    now = time.monotonic()
                    if now - last_emit >= _PROGRESS_INTERVAL:
                        last_emit = now
                        pct = int(processed * 100 / grand_total) if grand_total else 100
                        self.progress.emit(pct, filepath)
                    continue

                is_video = Path(filepath).suffix.lower() in VIDEO_EXT
                exif = VideoMetadataReader.read(filepath) if is_video else ExifReader.read(filepath)
                photo = PhotoInfo(
                    path=filepath,
                    file_size=stat.st_size,
                    file_mtime=mtime,
                    date_taken=exif.get("date_taken"),
                    width=exif.get("width", 0),
                    height=exif.get("height", 0),
                    camera_make=exif.get("camera_make", ""),
                    camera_model=exif.get("camera_model", ""),
                    lens_model=exif.get("lens_model", ""),
                    iso=exif.get("iso"),
                    exposure_time=exif.get("exposure_time", ""),
                    aperture=exif.get("aperture"),
                    focal_length=exif.get("focal_length"),
                    has_gps=exif.get("has_gps", False),
                    gps_lat=exif.get("gps_lat"),
                    gps_lon=exif.get("gps_lon"),
                    media_type="video" if is_video else "image",
                    duration=float(exif.get("duration", 0.0)),
                )
                photo = self._catalog.add_or_update_photo(photo)
                total += 1
                batch.append(photo)
                if len(batch) >= _BATCH_SIZE:
                    self.photos_batch.emit(batch)
                    batch = []
            except Exception as e:
                logger.error(f"Erreur scan {filepath}: {e}", exc_info=True)

            processed += 1
            now = time.monotonic()
            if now - last_emit >= _PROGRESS_INTERVAL:
                last_emit = now
                pct = int(processed * 100 / grand_total) if grand_total else 100
                self.progress.emit(pct, filepath)

        if batch and not self._stop_flag:
            self.photos_batch.emit(batch)

        # Clean up the ghost entries (files moved or deleted outside the app)
        # Only if the scan was not interrupted (stop_flag), to avoid false
        # positives: a partial scan must not remove valid entries.
        if not self._stop_flag:
            all_files_set = set(all_files)
            removed: list[str] = []
            for folder in self._folders:
                stale = self._catalog.get_all_paths_under(folder) - all_files_set
                for path in stale:
                    logger.info("Entrée catalogue supprimée (fichier absent) : %s", path)
                removed.extend(stale)
            if removed:
                self._catalog.delete_photos(removed)
                self._thumb_cache.invalidate_many(removed)
                self.photos_removed.emit(removed)

        journal.end("ScanThread",
                    f"{total} nouvelle(s) photo(s), {len(removed) if not self._stop_flag else '?'} supprimée(s)",
                    t0)
        self.finished.emit(total)

    def stop(self) -> None:
        self._stop_flag = True


class LibraryScanner:
    def __init__(self, catalog: Catalog, thumb_cache: ThumbnailCache):
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._thread: ScanThread | None = None

    def scan(self, folders: list[str], force: bool = False) -> ScanThread:
        self.stop()
        self._thread = ScanThread(folders, self._catalog, self._thumb_cache, force=force)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.request_stop()
        self.wait_stopped()

    def request_stop(self) -> None:
        """Signals the stop without waiting. Lets MainWindow.closeEvent signal
        every background thread before waiting on them, so that they stop in
        parallel rather than one after the other."""
        if self._thread and self._thread.isRunning():
            self._thread.stop()

    def wait_stopped(self, timeout_ms: int = 3000) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.wait(timeout_ms)

    @property
    def is_scanning(self) -> bool:
        return self._thread is not None and self._thread.isRunning()
