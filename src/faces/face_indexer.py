import concurrent.futures
import logging
import os

from PySide6.QtCore import QThread, Signal

from src.faces.face_database import FaceDatabase
from src.faces.detector import detect_and_embed, warmup_worker
from src.library.catalog import Catalog

logger = logging.getLogger(__name__)


_CLUSTER_EVERY   = 1000  # relancer le clustering tous les N visages trouvés
_DETECT_TIMEOUT  = 180   # secondes max par photo — au-delà le subprocess est tué et relancé


class TFWarmUpThread(QThread):
    """Conservé pour compatibilité — se termine immédiatement.

    InsightFace/ONNX n'a pas de warmup UI-bloquant : le chargement des modèles
    se fait dans le ProcessPoolExecutor worker via warmup_worker().
    """

    def run(self) -> None:
        pass


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
        Emitted if insightface is not installed — indexing is aborted.
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
        from src.core.thread_journal import journal, rss_mb
        self.setPriority(QThread.LowestPriority)

        all_paths = self._catalog.get_all_photo_paths()
        to_index = self._face_db.get_paths_to_index(all_paths)
        total = len(to_index)

        if total == 0:
            logger.debug("FaceIndexThread: aucune photo à indexer")
            self.finished.emit(0, 0)
            return

        t0 = journal.start("FaceIndexThread", f"{total} photo(s) à analyser (visages)",
                           rss_mb=round(rss_mb(), 1))
        logger.info("FaceIndexThread: %d photo(s) à analyser", total)
        indexed = 0
        faces_found = 0

        # ── Sous-processus dédié pour detect_and_embed ─────────────────────
        # Géré manuellement (pas de `with`) pour pouvoir recréer l'executor
        # quand un subprocess se plante ou dépasse _DETECT_TIMEOUT secondes.
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        try:
            # Phase 1 — warmup dans le subprocess (TF s'initialise ~20 s).
            self.progress.emit(0, total)
            try:
                executor.submit(warmup_worker).result(timeout=300)
            except concurrent.futures.TimeoutError:
                logger.error("FaceIndexThread: warmup timeout, abandon")
                journal.error("FaceIndexThread", "warmup timeout", t0)
                self.finished.emit(0, 0)
                return
            except concurrent.futures.BrokenExecutor as exc:
                logger.warning("FaceIndexThread: sous-processus crashé au warmup: %s", exc)
                journal.error("FaceIndexThread", f"warmup crash: {exc}", t0)
                return
            except Exception as exc:
                logger.warning("FaceIndexThread: warmup avertissement: %s", exc)

            # Phase 2 — boucle de détection
            for i, path in enumerate(to_index):
                if self._stop_flag:
                    break
                if not os.path.exists(path):
                    continue

                self.progress.emit(i + 1, total)
                journal.step(
                    "FaceIndexThread",
                    f"[{i + 1}/{total}] {os.path.basename(path)}",
                    t0,
                )

                try:
                    future = executor.submit(detect_and_embed, path)
                    try:
                        detections = future.result(timeout=_DETECT_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        # Subprocess planté — le tuer et en relancer un propre.
                        logger.error(
                            "FaceIndexThread: timeout %ds sur %s — subprocess relancé",
                            _DETECT_TIMEOUT, os.path.basename(path),
                        )
                        journal.step(
                            "FaceIndexThread",
                            f"TIMEOUT {os.path.basename(path)}",
                            t0,
                        )
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
                        try:
                            executor.submit(warmup_worker).result(timeout=300)
                        except Exception as exc2:
                            logger.warning("FaceIndexThread: re-warmup: %s", exc2)
                        self.error.emit(path, f"timeout ({_DETECT_TIMEOUT}s)")
                        continue
                except concurrent.futures.BrokenExecutor as exc:
                    logger.error("FaceIndexThread: pool cassé, abandon: %s", exc)
                    break
                except RuntimeError as exc:
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
                    if faces_found % _CLUSTER_EVERY == 0:
                        self.cluster_requested.emit()

        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Clustering final — une passe complète à la fin de l'indexation
        if indexed > 0:
            self.cluster_requested.emit()

        journal.end(
            "FaceIndexThread",
            f"{indexed} photo(s) analysée(s), {faces_found} visage(s)",
            t0,
            rss_mb=round(rss_mb(), 1),
        )
        logger.info(
            "FaceIndexThread terminé : %d photo(s) analysée(s), %d visage(s)",
            indexed,
            faces_found,
        )
        self.finished.emit(indexed, faces_found)

    def stop(self) -> None:
        self._stop_flag = True


class SingleFaceReindexThread(QThread):
    """
    Re-détecte les visages d'une seule photo (typiquement après une rotation 90°).

    Signals
    -------
    finished(photo_path, face_count)
    cluster_requested()
    error(photo_path, message)
    """

    finished          = Signal(str, int)
    cluster_requested = Signal()
    error             = Signal(str, str)

    def __init__(
        self,
        face_db: FaceDatabase,
        photo_path: str,
        rotation: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db    = face_db
        self._photo_path = photo_path
        self._rotation   = rotation

    def run(self) -> None:
        try:
            detections = detect_and_embed(self._photo_path, rotation=self._rotation)
        except RuntimeError:
            return   # deepface non installé
        except Exception as exc:
            logger.error("SingleFaceReindexThread erreur %s: %s", self._photo_path, exc)
            self.error.emit(self._photo_path, str(exc))
            return
        self._face_db.save_faces(self._photo_path, detections, rotation=self._rotation)
        logger.info(
            "SingleFaceReindexThread: %d visage(s) détecté(s) dans %s (rotation=%d°)",
            len(detections), os.path.basename(self._photo_path), self._rotation,
        )
        self.finished.emit(self._photo_path, len(detections))
        self.cluster_requested.emit()
