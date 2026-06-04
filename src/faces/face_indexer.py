import logging
import os

from PySide6.QtCore import QThread, Signal

from src.faces.face_database import FaceDatabase
from src.faces.detector import detect_and_embed
from src.library.catalog import Catalog

logger = logging.getLogger(__name__)


_CLUSTER_EVERY = 25   # relancer le clustering tous les N visages trouvés


class FaceIndexThread(QThread):
    """
    Background thread that detects faces in unindexed photos.

    Signals
    -------
    photo_indexed(path, face_count)
        Emitted after each photo is processed (only if at least 1 face found).
    progress(current, total)
        Emitted every photo.
    cluster_requested()
        Emitted every _CLUSTER_EVERY faces so clustering can run incrementally.
    finished(photos_indexed, faces_found)
        Emitted when the thread ends (normally or after stop()).
    unavailable()
        Emitted if deepface is not installed — indexing is aborted.
    error(path, message)
        Emitted for non-fatal per-photo errors.
    """

    photo_indexed     = Signal(str, int)
    progress          = Signal(int, int)
    cluster_requested = Signal()
    finished          = Signal(int, int)
    unavailable       = Signal()
    error             = Signal(str, str)

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog: Catalog,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._stop_flag = False

    def run(self) -> None:
        all_paths = [p.path for p in self._catalog.get_all_photos()]
        to_index = self._face_db.get_paths_to_index(all_paths)
        total = len(to_index)

        if total == 0:
            logger.debug("FaceIndexThread: aucune photo à indexer")
            self.finished.emit(0, 0)
            return

        logger.info("FaceIndexThread: %d photo(s) à analyser", total)
        indexed = 0
        faces_found = 0

        for i, path in enumerate(to_index):
            if self._stop_flag:
                break
            if not os.path.exists(path):
                continue

            self.progress.emit(i + 1, total)

            try:
                detections = detect_and_embed(path)
            except RuntimeError as exc:
                # deepface unavailable — abort gracefully
                logger.warning("FaceIndexThread: %s", exc)
                self.unavailable.emit()
                break
            except Exception as exc:
                logger.error("FaceIndexThread erreur %s: %s", path, exc)
                self.error.emit(path, str(exc))
                continue

            self._face_db.save_faces(path, detections)
            faces_found += len(detections)
            indexed += 1
            if detections:
                self.photo_indexed.emit(path, len(detections))
                # Déclencher un clustering intermédiaire tous les N visages
                if faces_found % _CLUSTER_EVERY == 0:
                    self.cluster_requested.emit()

        logger.info(
            "FaceIndexThread terminé : %d photo(s) analysée(s), %d visage(s)",
            indexed,
            faces_found,
        )
        self.finished.emit(indexed, faces_found)

    def stop(self) -> None:
        self._stop_flag = True
