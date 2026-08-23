# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
import logging
import os
import time
from collections import deque

from PySide6.QtCore import QThread, Signal

from src.core.cpu_throttle import (
    init_background_process,
    limit_cv2_threads,
    lower_current_thread_priority,
    throttle_tick,
    throttled_worker_count,
)
from src.faces.face_database import FaceDatabase
from src.faces.detector import detect_and_embed, detect_and_embed_auto, warmup_worker, warmup_worker_cpu
from src.library.catalog import Catalog

logger = logging.getLogger(__name__)


_WORKERS              = throttled_worker_count()  # subprocesses in a GPU/CPU pipeline, ~30 % of the cores
_CLUSTER_EVERY        = 1000  # restart the clustering every N faces found
_DETECT_TIMEOUT       = 60    # seconds max per photo before killing the subprocess
_WARMUP_TIMEOUT       = 120   # seconds max for the initial warmup (the GPU can be slow)
_MAX_CONSECUTIVE_FAIL = 5     # consecutive failures before giving up for good
_GPU_RETRY_AFTER      = 50    # consecutive successes on the CPU fallback before retrying the GPU
                               # (a single isolated timeout/crash must not condemn all the rest
                               # of the scan to run on 1 CPU worker)

def _kill_executor(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    """Forcibly kills every subprocess of the executor (necessary on Windows:
    shutdown(wait=False) does NOT kill the processes that are running)."""
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
    """Creates a clean executor pre-initialised in forced CPU mode (1 worker, conservative)."""
    ex = concurrent.futures.ProcessPoolExecutor(
        max_workers=1, initializer=init_background_process
    )
    try:
        ex.submit(warmup_worker_cpu).result(timeout=30)
        logger.info("FaceIndexThread: re-warmup CPU OK")
    except Exception as exc:
        logger.warning("FaceIndexThread: re-warmup CPU échoué (%s) — executor nu", exc)
        _kill_executor(ex)
        ex = concurrent.futures.ProcessPoolExecutor(
            max_workers=1, initializer=init_background_process
        )
    return ex


def _fresh_executor_gpu() -> "concurrent.futures.ProcessPoolExecutor | None":
    """Tries to recreate a full GPU executor (_WORKERS workers) after a CPU fallback.
    Returns None if the warmup fails, to let the caller stay on the CPU without
    interrupting the scan in progress."""
    ex = concurrent.futures.ProcessPoolExecutor(
        max_workers=_WORKERS, initializer=init_background_process
    )
    try:
        futs = [ex.submit(warmup_worker) for _ in range(_WORKERS)]
        for f in futs:
            f.result(timeout=_WARMUP_TIMEOUT)
        logger.info("FaceIndexThread: re-warmup GPU OK, retour au pipeline GPU")
        return ex
    except Exception as exc:
        logger.warning("FaceIndexThread: tentative de retour au GPU échouée (%s) — reste sur CPU", exc)
        _kill_executor(ex)
        return None


class TFWarmUpThread(QThread):
    """Kept for compatibility — terminates immediately.

    InsightFace/ONNX has no UI-blocking warmup: the models are loaded in the
    ProcessPoolExecutor worker through warmup_worker().
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
        self._executor: "concurrent.futures.ProcessPoolExecutor | None" = None

    def run(self) -> None:
        from src.core.thread_journal import journal, rss_mb
        self.setPriority(QThread.LowestPriority)
        # setPriority() only goes down to THREAD_PRIORITY_LOWEST (-2); IDLE (-15)
        # puts this thread below nearly everything else on the system.
        lower_current_thread_priority()
        # This thread does no heavy OpenCV computation itself (everything goes
        # to subprocesses, where init_background_process takes care of it), but
        # it decodes and saves face thumbnails through cv2/PIL — and the
        # setting is global to the process anyway.
        limit_cv2_threads(1)

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

        # The nvidia DLLs are added to the PATH here (the parent process);
        # the subprocesses inherit them automatically.
        from src.faces.detector import _register_nvidia_dll_dirs
        _register_nvidia_dll_dirs()
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=_WORKERS, initializer=init_background_process
        )
        self._executor = executor
        try:
            # ── Phase 1: warmup of the _WORKERS subprocesses ───────────────
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

            # ── Phase 2: FIFO detection loop (_WORKERS in flight) ──────────
            # An ordered queue: (future, path, submission time)
            # Exactly _WORKERS futures are kept running simultaneously.
            # While the GPU processes image N (worker 1), worker 2 loads and
            # preprocesses image N+1 on the CPU → the GPU pipeline stays full.
            in_flight: deque = deque()
            path_iter = iter(to_index)
            processed = 0
            consecutive_fails = 0
            on_cpu_fallback = False
            cpu_recovery_successes = 0

            def _enqueue() -> None:
                """Fills the queue up to _WORKERS simultaneous futures."""
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

                # Time left before this photo times out
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
                    if self._stop_flag:
                        break
                    logger.error("FaceIndexThread: timeout %ds sur %s",
                                 _DETECT_TIMEOUT, os.path.basename(path))
                    journal.step("FaceIndexThread", f"TIMEOUT {os.path.basename(path)}", t0)
                    # Do NOT call save_faces() here: the photo would stay marked as
                    # "processed" (indexed_photos, 0 faces) although it has never
                    # really been analysed. Instead it is recorded in
                    # face_index_errors: it will no longer be retried automatically
                    # (avoids paying 60 s again at every scan), only through the
                    # "Retry the face identification" context menu.
                    self._face_db.mark_index_error(path, "timeout")
                    in_flight.popleft()
                    # The other futures in flight are invalid too after a kill
                    for f, p, _ in in_flight:
                        processed += 1
                        self.progress.emit(processed, total)
                    in_flight.clear()
                    _kill_executor(executor)
                    executor = _fresh_executor_cpu()
                    self._executor = executor
                    on_cpu_fallback = True
                    cpu_recovery_successes = 0
                    self.error.emit(path, f"timeout ({_DETECT_TIMEOUT}s)")
                    consecutive_fails += 1
                    if consecutive_fails >= _MAX_CONSECUTIVE_FAIL:
                        logger.error("FaceIndexThread: %d échecs consécutifs, abandon",
                                     consecutive_fails)
                        break
                    _enqueue()
                    continue

                except concurrent.futures.BrokenExecutor as exc:
                    if self._stop_flag:
                        # Executor killed by stop() (a shutdown requested while
                        # fut.result() was blocking, for up to _DETECT_TIMEOUT=60 s
                        # without this): not a real crash, do not count it as a
                        # failure nor start a fallback executor.
                        break
                    logger.error("FaceIndexThread: subprocess crashé sur %s",
                                 os.path.basename(path))
                    journal.step("FaceIndexThread", f"CRASH {os.path.basename(path)}", t0)
                    # Do NOT call save_faces() here, for the same reason as at the timeout
                    # above: avoid marking a photo that was never analysed as done.
                    self._face_db.mark_index_error(path, "crash")
                    in_flight.popleft()
                    for f, p, _ in in_flight:
                        processed += 1
                        self.progress.emit(processed, total)
                    in_flight.clear()
                    _kill_executor(executor)
                    executor = _fresh_executor_cpu()
                    self._executor = executor
                    on_cpu_fallback = True
                    cpu_recovery_successes = 0
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

                # Success
                in_flight.popleft()
                consecutive_fails = 0

                self._face_db.save_faces(path, detections, rotation=det_rotation)
                faces_found += len(detections)
                indexed += 1
                if detections:
                    self.photo_indexed.emit(path, len(detections))
                    if faces_found % _CLUSTER_EVERY == 0:
                        self.cluster_requested.emit()

                if on_cpu_fallback:
                    cpu_recovery_successes += 1
                    if cpu_recovery_successes >= _GPU_RETRY_AFTER:
                        # A single isolated timeout/crash must not condemn all the rest of
                        # the scan to run on 1 CPU worker: after a number of consecutive
                        # successes, retry the GPU. Purge first the futures still in flight
                        # on the CPU executor (1 worker, so cheap) before killing it, so as
                        # not to lose work already submitted.
                        while in_flight and not self._stop_flag:
                            f2, p2, _ = in_flight.popleft()
                            processed += 1
                            self.progress.emit(processed, total)
                            try:
                                d2, r2 = f2.result(timeout=_DETECT_TIMEOUT)
                                self._face_db.save_faces(p2, d2, rotation=r2)
                                faces_found += len(d2)
                                indexed += 1
                                if d2:
                                    self.photo_indexed.emit(p2, len(d2))
                            except Exception as exc:
                                logger.warning(
                                    "FaceIndexThread: échec purge avant retour GPU sur %s : %s",
                                    os.path.basename(p2), exc,
                                )
                                self._face_db.mark_index_error(p2, "error")
                        new_executor = _fresh_executor_gpu()
                        _kill_executor(executor)
                        if new_executor is not None:
                            executor = new_executor
                            on_cpu_fallback = False
                        else:
                            executor = _fresh_executor_cpu()
                        self._executor = executor
                        cpu_recovery_successes = 0

                # Duty cycle of the pipeline: the pause is taken *before* the queue
                # is refilled, so the _WORKERS subprocesses drain and stay idle for
                # the whole sleep — that is what really throttles the load, the work
                # itself taking place outside this thread. The regulator measures
                # wall-clock time here (the wait on fut.result() included) and not
                # CPU time: on a saturated pipeline, which is the case aimed at, the
                # two are the same.
                throttle_tick(lambda: self._stop_flag)

                _enqueue()

        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

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
        # Without this, run() can stay blocked for up to _DETECT_TIMEOUT (60 s) or
        # _WARMUP_TIMEOUT (120 s) inside a fut.result() in progress before even
        # noticing the flag — killing the executor makes that call fail
        # immediately (BrokenExecutor), which unblocks the loop without waiting.
        # Contributes directly to a fast shutdown of the application (cf.
        # MainWindow.closeEvent).
        executor = self._executor
        if executor is not None:
            _kill_executor(executor)


class SingleFaceReindexThread(QThread):
    """
    Re-detects the faces of a single photo (typically after a 90° rotation).

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
            return   # insightface not installed
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


class ForceRedetectThread(QThread):
    """
    The "Force a new detection with no size limit" context menu of the viewer:
    re-detects the faces of a single photo, short-circuiting the automatic
    size-based ignore threshold (FaceDatabase.save_faces(force_no_limit=True))
    — no face is marked ignored=1 on that photo any more. The previously
    identified faces (person_id) are re-associated with the closest new
    detection (IoU) by save_faces() itself; the manually added faces (never seen
    by InsightFace) are left untouched.

    Tries two orientations and keeps the more fruitful one:
      1. the current edit rotation (edits.db) — the one the user sees;
      2. the rotation of the last indexing (indexed_photos.rotation).

    Sticking to the indexed rotation (the original behaviour) re-detected
    forever in a stale orientation when the two had diverged (a rotation undone
    by Ctrl+Z, or two rotations chained too fast to both be indexed) — symptom:
    2 faces found out of 8, even in forced detection, with no action of the UI
    able to get out of it. But sticking to the edit rotation alone would break
    the opposite case, which is legitimate and frequent: `detect_and_embed_auto`
    deliberately switches to 90/180/270 when 0° finds nothing (a photo taken
    sideways), and the indexed rotation is then the right one although the photo
    is displayed at 0°. Hence trying both; on an equal number of faces the edit
    rotation wins (a frame of reference consistent with what is displayed, cf.
    detected_rotation).

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
        self, face_db: FaceDatabase, photo_path: str, parent=None, edit_db=None,
    ) -> None:
        super().__init__(parent)
        self._face_db    = face_db
        self._photo_path = photo_path
        self._edit_db    = edit_db

    def _edit_rotation(self) -> "int | None":
        """Current edit rotation, or None if edits.db is unreadable."""
        try:
            db = self._edit_db
            if db is None:
                from src.processing.edit_database import EditDatabase
                db = EditDatabase()
            return int(db.load(self._photo_path).rotation) % 360
        except Exception as exc:
            logger.warning(
                "ForceRedetectThread : rotation d'édition illisible pour %s (%s)",
                os.path.basename(self._photo_path), exc,
            )
            return None

    def _rotation_candidates(self) -> list[int]:
        """Orientations to try, in order of preference (cf. the docstring)."""
        indexed = self._face_db.get_indexed_rotation(self._photo_path) % 360
        edited = self._edit_rotation()
        if edited is None:
            return [indexed]
        return [edited] if edited == indexed else [edited, indexed]

    def run(self) -> None:
        from src.library.exif_reader import VIDEO_EXT
        if os.path.splitext(self._photo_path)[1].lower() in VIDEO_EXT:
            return
        candidates = self._rotation_candidates()
        detections: list = []
        rotation = candidates[0]
        try:
            for candidate in candidates:
                found = detect_and_embed(self._photo_path, rotation=candidate)
                # A strict `>`: on a tie, the first candidate (the edit
                # rotation) is kept.
                if len(found) > len(detections):
                    detections, rotation = found, candidate
        except RuntimeError:
            return   # insightface not installed
        except Exception as exc:
            logger.error("ForceRedetectThread erreur %s: %s", self._photo_path, exc)
            self.error.emit(self._photo_path, str(exc))
            return
        self._face_db.save_faces(
            self._photo_path, detections, rotation=rotation, force_no_limit=True
        )
        logger.info(
            "ForceRedetectThread: %d visage(s) détecté(s) sans limite de taille "
            "dans %s (rotation=%d°, essais=%s)",
            len(detections), os.path.basename(self._photo_path), rotation, candidates,
        )
        self.finished.emit(self._photo_path, len(detections))
        self.cluster_requested.emit()


class RetryFaceIndexThread(QThread):
    """
    Retries the face identification of a single photo previously in error (a
    timeout or a fresh crash of the subprocess) — the same anti-blocking
    protection as FaceIndexThread (subprocess + timeout), unlike
    SingleFaceReindexThread, which calls detect_and_embed() directly and could
    block indefinitely on a file that has already caused a timeout.

    On a fresh failure, the photo stays recorded in face_index_errors
    (mark_index_error) so as to offer the user to delete it or to exclude it for
    good (see MainWindow._on_retry_face_index_finished).

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
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=1, initializer=init_background_process
        )
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
                    unavailable = True  # insightface not installed
                except Exception as exc:
                    logger.error("RetryFaceIndexThread erreur %s: %s", path, exc)
                    self._face_db.mark_index_error(path, "crash")
                else:
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
    """Re-evaluates the faces auto-ignored by size with the current proportional threshold.

    Does not restart InsightFace: simply updates the ignored=0 flag for the faces
    whose size now passes the threshold (recomputed from the real dimensions of
    each photo).
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
    """Compares the unidentified clusters with the centroids of the named people.

    Does not restart InsightFace: only compares the existing embeddings.

    Signals
    -------
    progress(current, total)    — progress (per cluster checked)
    finished(suggestions, total) — number of suggestions created / clusters checked
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
