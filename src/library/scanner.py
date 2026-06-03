import logging
import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.library.exif_reader import ExifReader
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.core.models import PhotoInfo

logger = logging.getLogger(__name__)

SUPPORTED_EXT = ExifReader.SUPPORTED


class ScanThread(QThread):
    photo_discovered = Signal(object)
    photos_removed   = Signal(list)   # list[str] — chemins supprimés du catalogue
    progress = Signal(int, str)
    finished = Signal(int)

    def __init__(
        self,
        folders: list[str],
        catalog: Catalog,
        thumb_cache: ThumbnailCache,
    ):
        super().__init__()
        self._folders = folders
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._stop_flag = False

    def run(self) -> None:
        total = 0
        processed = 0

        all_files: list[str] = []
        for folder in self._folders:
            for root, dirs, files in os.walk(folder):
                if self._stop_flag:
                    break
                # Exclure les dossiers de sauvegarde temporaires et les dossiers cachés
                dirs[:] = [d for d in dirs if not d.startswith(".tmp_")]
                for fname in files:
                    if Path(fname).suffix.lower() in SUPPORTED_EXT:
                        all_files.append(os.path.normpath(os.path.join(root, fname)))

        grand_total = len(all_files)

        known: dict[str, float] = {}
        for folder in self._folders:
            known.update(self._catalog.get_known_mtimes(folder))

        for filepath in all_files:
            if self._stop_flag:
                break
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                existing_mtime = known.get(filepath)
                if existing_mtime is not None and abs(existing_mtime - mtime) < 1.0:
                    processed += 1
                    if processed % 50 == 0:
                        pct = int(processed * 100 / grand_total) if grand_total else 100
                        self.progress.emit(pct, filepath)
                    continue

                exif = ExifReader.read(filepath)
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
                )
                photo = self._catalog.add_or_update_photo(photo)
                total += 1
                self.photo_discovered.emit(photo)
            except Exception as e:
                logger.error(f"Erreur scan {filepath}: {e}", exc_info=True)

            processed += 1
            if processed % 50 == 0:
                pct = int(processed * 100 / grand_total) if grand_total else 100
                self.progress.emit(pct, filepath)

        # Nettoyage des entrées fantômes (fichiers déplacés ou supprimés hors de l'app)
        # Seulement si le scan n'a pas été interrompu (stop_flag) pour éviter les
        # faux positifs : un scan partiel ne doit pas supprimer des entrées valides.
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

        self.finished.emit(total)

    def stop(self) -> None:
        self._stop_flag = True


class LibraryScanner:
    def __init__(self, catalog: Catalog, thumb_cache: ThumbnailCache):
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._thread: ScanThread | None = None

    def scan(self, folders: list[str]) -> ScanThread:
        self.stop()
        self._thread = ScanThread(folders, self._catalog, self._thumb_cache)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)

    @property
    def is_scanning(self) -> bool:
        return self._thread is not None and self._thread.isRunning()
