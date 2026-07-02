# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
import logging
import os
import time
from collections import deque

from PySide6.QtCore import QThread, Signal

from src.faces.face_database import FaceDatabase
from src.faces.detector import detect_and_embed, detect_and_embed_auto, warmup_worker, warmup_worker_cpu
from src.library.catalog import Catalog

logger = logging.getLogger(__name__)


_WORKERS              = 4     # subprocesses en pipeline GPU/CPU
_CLUSTER_EVERY        = 1000  # relancer le clustering tous les N visages trouvés
_DETECT_TIMEOUT       = 60    # secondes max par photo avant de tuer le subprocess
_WARMUP_TIMEOUT       = 120   # secondes max pour le warmup initial (GPU peut être lent)
_MAX_CONSECUTIVE_FAIL = 5     # échecs consécutifs avant abandon définitif

# Mapping rotation (degrés CW) → valeur EXIF Orientation
_ROT_TO_EXIF_ORI = {90: 6, 180: 3, 270: 8}


def _fix_exif_orientation(photo_path: str, rotation: int) -> bool:
    """Écrit le tag EXIF Orientation dans le JPEG losslessly via piexif.

    Seulement pour les JPEG dont l'orientation EXIF est absente ou neutre (=1).
    Retourne True si le tag a été écrit avec succès, False sinon.
    """
    if rotation not in _ROT_TO_EXIF_ORI:
        return False
    ext = os.path.splitext(photo_path)[1].lower()
    if ext not in ('.jpg', '.jpeg'):
        return False
    try:
        import piexif
        try:
            exif_dict = piexif.load(photo_path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        current_ori = exif_dict.get("0th", {}).get(piexif.ImageIFD.Orientation, 1)
        if current_ori not in (None, 1):
            # Orientation non-triviale déjà présente — ne pas composer
            return False
        exif_dict.setdefault("0th", {})[piexif.ImageIFD.Orientation] = _ROT_TO_EXIF_ORI[rotation]
        piexif.insert(piexif.dump(exif_dict), photo_path)
        logger.info("EXIF orientation corrigée (%d°) : %s", rotation, os.path.basename(photo_path))
        return True
    except Exception as exc:
        logger.debug("_fix_exif_orientation %s : %s", os.path.basename(photo_path), exc)
        return False


def _kill_executor(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    """Tue de force tous les subprocesses de l'executor (nécessaire sur Windows :
    shutdown(wait=False) ne tue PAS les processus en cours d'exécution)."""
    try:
        for process in list(executor._processes.values()):
            try:
                process.kill()
            except Exception:
                pass
    except Exception:
        pass
    executor.shutdown(wait=False, cancel_futures=True)


def _fresh_executor_cpu() -> concurrent.futures.ProcessPoolExecutor:
    """Crée un executor propre pré-initialisé en mode CPU forcé (1 worker, conservateur)."""
    ex = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    try:
        ex.submit(warmup_worker_cpu).result(timeout=30)
        logger.info("FaceIndexThread: re-warmup CPU OK")
    except Exception as exc:
        logger.warning("FaceIndexThread: re-warmup CPU échoué (%s) — executor nu", exc)
        _kill_executor(ex)
        ex = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    return ex


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

        from src.library.exif_reader import VIDEO_EXT
        all_paths = self._catalog.get_all_photo_paths()
        to_index = [
            p for p in self._face_db.get_paths_to_index(all_paths)
            if os.path.splitext(p)[1].lower() not in VIDEO_EXT
        ]
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

        # Les DLLs nvidia sont ajoutées au PATH ici (processus parent) ;
        # les subprocesses les héritent automatiquement.
        from src.faces.detector import _register_nvidia_dll_dirs
        _register_nvidia_dll_dirs()
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=_WORKERS)
        try:
            # ── Phase 1 : warmup des _WORKERS subprocesses ─────────────────
            self.progress.emit(0, total)
            try:
                warmup_futs = [executor.submit(warmup_worker) for _ in range(_WORKERS)]
                for f in warmup_futs:
                    f.result(timeout=_WARMUP_TIMEOUT)
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

            # ── Phase 2 : boucle de détection FIFO (_WORKERS en vol) ───────
            # File d'attente ordonnée : (future, path, heure_soumission)
            # On maintient exactement _WORKERS futures en cours simultanément.
            # Pendant que le GPU traite l'image N (worker 1), le worker 2
            # charge et préprocesse l'image N+1 sur CPU → pipeline GPU plein.
            in_flight: deque = deque()
            path_iter = iter(to_index)
            processed = 0
            consecutive_fails = 0

            def _enqueue() -> None:
                """Remplit la file jusqu'à _WORKERS futures simultanées."""
                while len(in_flight) < _WORKERS:
                    path = next(path_iter, None)
                    if path is None:
                        break
                    if not os.path.exists(path):
                        continue
                    try:
                        fut = executor.submit(detect_and_embed_auto, path)
                        in_flight.append((fut, path, time.monotonic()))
                    except concurrent.futures.BrokenExecutor:
                        break

            _enqueue()

            while in_flight and not self._stop_flag:
                fut, path, t_submit = in_flight[0]

                # Temps restant avant timeout pour cette photo
                remaining = max(0.5, _DETECT_TIMEOUT - (time.monotonic() - t_submit))

                processed += 1
                self.progress.emit(processed, total)
                journal.step(
                    "FaceIndexThread",
                    f"[{processed}/{total}] {os.path.basename(path)}",
                    t0,
                )

                try:
                    detections, det_rotation = fut.result(timeout=remaining)

                except concurrent.futures.TimeoutError:
                    logger.error("FaceIndexThread: timeout %ds sur %s",
                                 _DETECT_TIMEOUT, os.path.basename(path))
                    journal.step("FaceIndexThread", f"TIMEOUT {os.path.basename(path)}", t0)
                    # Ne PAS appeler save_faces() ici : la photo resterait marquée
                    # "traitée" (indexed_photos, 0 visage) alors qu'elle n'a jamais
                    # été réellement analysée. À la place, on l'enregistre dans
                    # face_index_errors : elle ne sera plus retentée automatiquement
                    # (évite de repayer 60s à chaque scan), seulement via le menu
                    # contextuel "Retenter l'identification des visages".
                    self._face_db.mark_index_error(path, "timeout")
                    in_flight.popleft()
                    # Les autres futures en vol sont aussi invalides après kill
                    for f, p, _ in in_flight:
                        processed += 1
                        self.progress.emit(processed, total)
                    in_flight.clear()
                    _kill_executor(executor)
                    executor = _fresh_executor_cpu()
                    self.error.emit(path, f"timeout ({_DETECT_TIMEOUT}s)")
                    consecutive_fails += 1
                    if consecutive_fails >= _MAX_CONSECUTIVE_FAIL:
                        logger.error("FaceIndexThread: %d échecs consécutifs, abandon",
                                     consecutive_fails)
                        break
                    _enqueue()
                    continue

                except concurrent.futures.BrokenExecutor as exc:
                    logger.error("FaceIndexThread: subprocess crashé sur %s",
                                 os.path.basename(path))
                    journal.step("FaceIndexThread", f"CRASH {os.path.basename(path)}", t0)
                    # Ne PAS appeler save_faces() ici, pour la même raison qu'au timeout
                    # ci-dessus : éviter de marquer une photo jamais analysée comme faite.
                    self._face_db.mark_index_error(path, "crash")
                    in_flight.popleft()
                    for f, p, _ in in_flight:
                        processed += 1
                        self.progress.emit(processed, total)
                    in_flight.clear()
                    _kill_executor(executor)
                    executor = _fresh_executor_cpu()
                    self.error.emit(path, f"subprocess crash: {exc}")
                    consecutive_fails += 1
                    if consecutive_fails >= _MAX_CONSECUTIVE_FAIL:
                        logger.error("FaceIndexThread: %d crashs consécutifs, abandon",
                                     consecutive_fails)
                        break
                    _enqueue()
                    continue

                except RuntimeError as exc:
                    logger.warning("FaceIndexThread: %s", exc)
                    self.unavailable.emit()
                    break

                except Exception as exc:
                    logger.error("FaceIndexThread erreur %s: %s", path, exc)
                    self.error.emit(path, str(exc))
                    in_flight.popleft()
                    _enqueue()
                    continue

                # Succès
                in_flight.popleft()
                consecutive_fails = 0

                # Correction EXIF lossless : si les visages ont été trouvés avec
                # une rotation non nulle, on inscrit le tag EXIF Orientation dans le
                # fichier JPEG pour que les visionneuses l'affichent à l'endroit.
                # On ré-exécute ensuite la détection à rotation=0 afin que les
                # coordonnées bbox soient cohérentes avec la nouvelle orientation.
                if det_rotation != 0 and _fix_exif_orientation(path, det_rotation):
                    try:
                        redetect_fut = executor.submit(detect_and_embed, path, 0)
                        detections   = redetect_fut.result(timeout=_DETECT_TIMEOUT)
                        det_rotation = 0
                    except Exception as exc:
                        logger.warning(
                            "FaceIndexThread: ré-détection post-EXIF échouée pour %s : %s",
                            os.path.basename(path), exc,
                        )
                        detections   = []
                        det_rotation = 0

                self._face_db.save_faces(path, detections, rotation=det_rotation)
                faces_found += len(detections)
                indexed += 1
                if detections:
                    self.photo_indexed.emit(path, len(detections))
                    if faces_found % _CLUSTER_EVERY == 0:
                        self.cluster_requested.emit()
                _enqueue()

        finally:
            executor.shutdown(wait=False, cancel_futures=True)

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
        from src.library.exif_reader import VIDEO_EXT
        if os.path.splitext(self._photo_path)[1].lower() in VIDEO_EXT:
            return
        try:
            detections = detect_and_embed(self._photo_path, rotation=self._rotation)
        except RuntimeError:
            return   # insightface non installé
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


class RetryFaceIndexThread(QThread):
    """
    Retente l'identification des visages d'une seule photo précédemment en erreur
    (timeout ou nouveau crash du subprocess) — protection anti-blocage identique à
    FaceIndexThread (subprocess + timeout), contrairement à SingleFaceReindexThread
    qui appelle detect_and_embed() directement et pourrait bloquer indéfiniment sur
    un fichier ayant déjà causé un timeout.

    En cas de nouvel échec, la photo reste enregistrée dans face_index_errors
    (mark_index_error) pour proposer à l'utilisateur de la supprimer ou de
    l'exclure définitivement (voir MainWindow._on_retry_face_index_finished).

    Signals
    -------
    finished(photo_path, success, face_count)
    cluster_requested()
    """

    finished          = Signal(str, bool, int)
    cluster_requested = Signal()

    def __init__(self, face_db: FaceDatabase, photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self._face_db    = face_db
        self._photo_path = photo_path

    def run(self) -> None:
        path = self._photo_path
        from src.faces.detector import _register_nvidia_dll_dirs
        _register_nvidia_dll_dirs()
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        success = False
        unavailable = False
        detections: list = []
        det_rotation = 0
        try:
            try:
                executor.submit(warmup_worker).result(timeout=_WARMUP_TIMEOUT)
            except Exception as exc:
                logger.warning("RetryFaceIndexThread: warmup échoué sur %s (%s)",
                               os.path.basename(path), exc)
                self._face_db.mark_index_error(path, "timeout")
            else:
                try:
                    detections, det_rotation = executor.submit(
                        detect_and_embed_auto, path
                    ).result(timeout=_DETECT_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    logger.error("RetryFaceIndexThread: nouveau timeout sur %s",
                                 os.path.basename(path))
                    self._face_db.mark_index_error(path, "timeout")
                except concurrent.futures.BrokenExecutor as exc:
                    logger.error("RetryFaceIndexThread: nouveau crash sur %s (%s)",
                                 os.path.basename(path), exc)
                    self._face_db.mark_index_error(path, "crash")
                except RuntimeError:
                    unavailable = True  # insightface non installé
                except Exception as exc:
                    logger.error("RetryFaceIndexThread erreur %s: %s", path, exc)
                    self._face_db.mark_index_error(path, "crash")
                else:
                    if det_rotation != 0 and _fix_exif_orientation(path, det_rotation):
                        try:
                            detections = executor.submit(
                                detect_and_embed, path, 0
                            ).result(timeout=_DETECT_TIMEOUT)
                            det_rotation = 0
                        except Exception as exc:
                            logger.warning(
                                "RetryFaceIndexThread: ré-détection post-EXIF échouée pour %s : %s",
                                os.path.basename(path), exc,
                            )
                    success = True
        finally:
            _kill_executor(executor)

        if unavailable:
            return

        if not success:
            self.finished.emit(path, False, 0)
            return

        self._face_db.save_faces(path, detections, rotation=det_rotation)
        logger.info("RetryFaceIndexThread: %d visage(s) détecté(s) dans %s",
                    len(detections), os.path.basename(path))
        self.finished.emit(path, True, len(detections))
        self.cluster_requested.emit()


class RevaluateSizeIgnoredThread(QThread):
    """Réévalue les visages auto-ignorés par taille avec le seuil proportionnel actuel.

    Ne relance pas InsightFace : met simplement à jour le flag ignored=0 pour les
    faces dont la taille passe maintenant le seuil (recalculé à partir des dimensions
    réelles de chaque photo).
    """

    progress = Signal(int, int)   # (current, total_photos)
    finished = Signal(int, int)   # (unignored_count, total_photos)

    def __init__(self, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db

    def run(self) -> None:
        try:
            unignored, total = self._face_db.recalculate_size_ignored(
                progress_cb=lambda i, t: self.progress.emit(i, t)
            )
            self.finished.emit(unignored, total)
        except Exception as exc:
            logger.error("RevaluateSizeIgnoredThread: %s", exc)
            self.finished.emit(0, 0)


class SimilaritySearchThread(QThread):
    """Compare les clusters non identifiés aux centroïdes des personnes nommées.

    Ne relance pas InsightFace : compare uniquement les embeddings existants.

    Signals
    -------
    progress(current, total)    — avancement (par cluster vérifié)
    finished(suggestions, total) — nombre de suggestions créées / clusters vérifiés
    """

    progress = Signal(int, int)
    finished = Signal(int, int)

    def __init__(self, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db

    def run(self) -> None:
        try:
            made, total = self._face_db.find_similar_to_persons(
                progress_cb=lambda i, t: self.progress.emit(i, t)
            )
            self.finished.emit(made, total)
        except Exception as exc:
            logger.error("SimilaritySearchThread: %s", exc)
            self.finished.emit(0, 0)
