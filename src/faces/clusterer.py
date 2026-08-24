# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import multiprocessing
import time
from typing import Callable

from PySide6.QtCore import QThread, Signal

from src.core.i18n import translate
from src.faces.face_database import FaceDatabase

logger = logging.getLogger(__name__)

_PCA_DIMS        = 32    # ball_tree is efficient below ~30 dims; 32 keeps >90 % of the ArcFace variance
_CLUSTER_TIMEOUT = 1800  # seconds max (30 min) before giving up

# Sentinel: N of unidentified faces at the last successful clustering.
# Lets the reclustering be skipped if nothing has changed.
_last_clustered_n: int = -1


def reset_clustering_cache() -> None:
    """Invalidates the "last N clustered" cache used by `_run_clustering` to skip
    the reclustering if nothing has changed.

    To be called after any `FaceDatabase.reset_clustering()`/`reset_index()`:
    those methods wipe `cluster_id` en masse without changing the number of
    unidentified faces (an identical re-indexing) — without this call,
    `_run_clustering` sees `n == _last_clustered_n`, believes nothing has
    changed and skips the grouping, leaving every face stuck with
    `cluster_id=NULL` indefinitely (bug seen in 2026-07 through
    `test_faces_reset_full`)."""
    global _last_clustered_n
    _last_clustered_n = -1

# Minimum cohesion (cosine) required between EVERY pair of faces of one same HDBSCAN
# group, aligned on _SIM_STRONG (people_panel.py, "very likely"). min_samples=1 makes
# HDBSCAN nearly equivalent to a single-linkage: two faces can end up in the same
# cluster through a chain of close neighbours without ever resembling each other.
_PURITY_MIN_SIM        = 0.60
# Beyond that, the "complete linkage" split (a full distance matrix, O(k²)) would cost
# too much for a single group — a pathological case, left as-is rather than slowing the
# whole clustering down.
_PURITY_MAX_CLUSTER_N  = 2000


def _purify_clusters(X_full, labels):
    """Splits the HDBSCAN clusters some pairs of faces of which are too dissimilar.

    Rechecks each cluster (noise excluded) with a "complete linkage" hierarchical
    clustering on the full-dimension embeddings (normalised, not reduced by PCA): that
    linkage bounds the MAXIMUM dissimilarity between two members — unlike the chaining of
    HDBSCAN — so a cluster only comes out of it intact if every pair it contains exceeds
    _PURITY_MIN_SIM. The resulting sub-groups receive new labels (never merged with one
    another beyond what HDBSCAN had already proposed)."""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    labels = np.asarray(labels)
    next_label = int(labels.max()) + 1 if labels.size else 0
    max_dist = float(np.sqrt(max(0.0, 2.0 * (1.0 - _PURITY_MIN_SIM))))

    for lbl in np.unique(labels):
        if lbl < 0:
            continue
        idx = np.where(labels == lbl)[0]
        if len(idx) < 2 or len(idx) > _PURITY_MAX_CLUSTER_N:
            continue
        sub_labels = AgglomerativeClustering(
            n_clusters=None,
            linkage="complete",
            metric="euclidean",
            distance_threshold=max_dist,
        ).fit_predict(X_full[idx])
        if sub_labels.max() == 0:
            continue  # group already cohesive: nothing to split
        for sub_id in np.unique(sub_labels):
            if sub_id == 0:
                continue  # keeps the original HDBSCAN label for the first sub-group
            labels[idx[sub_labels == sub_id]] = next_label
            next_label += 1

    return labels


_NB_SP = " "  # narrow no-break space used as a thousands separator


def _clustering_worker_proc(X_bytes: bytes, n: int, d: int, conn) -> None:
    """
    PCA + HDBSCAN in an isolated subprocess.

    Sends progress messages through the pipe:
      ("pca",)                       — normalisation finished, PCA starting
      ("hdbscan",)                   — PCA finished, HDBSCAN starting
      ("result", n_clusters, n_singletons, labels)  — finished successfully
      ("error", message)             — unexpected exception

    Must be MODULE-LEVEL to be picklable on Windows (spawn).
    """
    import numpy as np
    from hdbscan import HDBSCAN
    from sklearn.decomposition import PCA

    try:
        X = np.frombuffer(X_bytes, dtype=np.float32).reshape(n, d).copy()

        # L2 normalisation → unit sphere
        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        X /= norms
        X_full = X  # full-dimension normalised embeddings — kept for _purify_clusters

        # PCA reduction
        if X.shape[1] > _PCA_DIMS and n > _PCA_DIMS:
            conn.send(("pca",))
            X = PCA(n_components=_PCA_DIMS, random_state=42).fit_transform(X).astype(np.float32)
            norms2 = np.linalg.norm(X, axis=1, keepdims=True)
            norms2[norms2 == 0] = 1.0
            X /= norms2

        conn.send(("hdbscan",))

        # hdbscan package (C++/Cython) — much faster than sklearn for large n.
        # boruvka_balltree: Borůvka MST O(n log²n) vs Prim O(n²) with sklearn.
        # core_dist_n_jobs=1: 1 thread → avoids duplicating the memory buffers.
        labels = HDBSCAN(
            min_cluster_size=2,
            min_samples=1,
            metric="euclidean",
            cluster_selection_method="leaf",
            algorithm="boruvka_balltree",
            leaf_size=100,
            core_dist_n_jobs=1,
        ).fit_predict(X)

        # min_samples=1 easily chains faces that are not very similar to one another
        # (single-linkage): recheck each cluster in full dimension before accepting it as-is.
        labels = _purify_clusters(X_full, labels)

        labels = labels.tolist()
        max_real = max((lbl for lbl in labels if lbl >= 0), default=-1)
        next_singleton = max_real + 1
        n_singletons = 0
        for i, lbl in enumerate(labels):
            if lbl == -1:
                labels[i] = next_singleton
                next_singleton += 1
                n_singletons += 1

        conn.send(("result", max_real + 1, n_singletons, labels))

    except MemoryError:
        conn.send(("error", "MemoryError : RAM insuffisante pour HDBSCAN"))
    except Exception as exc:
        conn.send(("error", str(exc)))
    finally:
        conn.close()


def _fmt_n(n: int) -> str:
    """Formats an integer with a narrow no-break space as a thousands separator."""
    return f"{n:,}".replace(",", _NB_SP)


def _run_clustering(
    face_db: FaceDatabase,
    progress_cb: Callable[[str], None] | None = None,
) -> int:
    """
    HDBSCAN clustering on the normalised ArcFace embeddings.

    Pipeline:
    1. Synthetic assignment of the cluster_ids for the already identified faces.
    2. L2 normalisation → unit sphere on the unidentified faces.
    3. PCA 512 → _PCA_DIMS dims + re-normalisation.
    4. Euclidean ball_tree HDBSCAN.

    The computation runs in an isolated subprocess through multiprocessing.Process + Pipe.
    The stages are reported back to the calling thread through progress_cb(message).

    Safe to call from any thread.
    Returns number of distinct clusters (singletons excluded).
    Raises RuntimeError if deps missing.
    """
    global _last_clustered_n

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(f"Le clustering nécessite numpy : {exc}")

    n_synthetic = face_db.assign_person_synthetic_clusters()
    if n_synthetic and progress_cb:
        progress_cb(translate("Clusterer", "Clustering: {n} identified faces pre-assigned…"
                              ).format(n=_fmt_n(n_synthetic)))

    X, face_ids = face_db.get_all_embeddings(only_unidentified=True)
    n = len(face_ids)

    if n == 0:
        logger.debug("Clustering: aucun visage (tous déjà identifiés)")
        return 0

    # Skip if nothing has changed since the last successful clustering:
    # the same N of unidentified faces AND no new synthetic assignment.
    if n == _last_clustered_n and n_synthetic == 0:
        logger.debug("Clustering: %d visages inchangés — skip", n)
        return 0

    logger.info("Clustering HDBSCAN: %d visages non identifiés", n)
    if progress_cb:
        progress_cb(translate("Clusterer", "Clustering: normalisation ({n} faces)…"
                              ).format(n=_fmt_n(n)))

    if n == 1:
        face_db.update_clusters(face_ids, [0])
        return 1

    d = X.shape[1]
    logger.info("Clustering PCA: %d → %d dims", d, min(_PCA_DIMS, d))

    X_bytes = X.astype(np.float32).tobytes()
    n_fmt = _fmt_n(n)

    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    proc = multiprocessing.Process(
        target=_clustering_worker_proc,
        args=(X_bytes, n, d, child_conn),
        daemon=True,
    )
    proc.start()
    child_conn.close()   # closes the child end in the parent process

    deadline = time.monotonic() + _CLUSTER_TIMEOUT
    result_labels = None
    n_clusters = n_singletons = 0
    hdbscan_start: float | None = None

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "Clustering timeout (%ds) pour %d visages — abandon",
                    _CLUSTER_TIMEOUT, n,
                )
                proc.kill()
                return 0

            if parent_conn.poll(min(remaining, 1.0)):
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    proc.join(timeout=2)
                    exitcode = proc.exitcode
                    logger.error(
                        "Clustering subprocess : pipe fermé prématurément"
                        " (exitcode=%s, %d visages)",
                        exitcode, n,
                    )
                    return 0

                tag = msg[0]
                if tag == "pca":
                    hdbscan_start = None
                    if progress_cb:
                        progress_cb(
                            translate("Clusterer",
                                      "Clustering: PCA {src}→{dst} dims…"
                                      ).format(src=d, dst=_PCA_DIMS)
                        )
                elif tag == "hdbscan":
                    hdbscan_start = time.monotonic()
                    if progress_cb:
                        progress_cb(
                            translate("Clusterer",
                                      "Clustering: HDBSCAN ({n} faces) — {time}…"
                                      ).format(n=n_fmt, time="0:00")
                        )
                elif tag == "result":
                    _, n_clusters, n_singletons, result_labels = msg
                    break
                elif tag == "error":
                    logger.error("Clustering subprocess erreur : %s", msg[1])
                    return 0

            else:
                # poll timeout (1 s) — updates the stopwatch during HDBSCAN
                if hdbscan_start is not None and progress_cb:
                    elapsed = int(time.monotonic() - hdbscan_start)
                    m, s = divmod(elapsed, 60)
                    progress_cb(
                        translate("Clusterer",
                                  "Clustering: HDBSCAN ({n} faces) — {time}…"
                                  ).format(n=n_fmt, time=f"{m}:{s:02d}")
                    )
                if not proc.is_alive():
                    exitcode = proc.exitcode
                    # exitcode < 0 → killed by a signal (OOM=-9 on Linux, ~-1073741819 on Windows)
                    # exitcode > 0 → uncaught exception in the worker
                    logger.error(
                        "Clustering subprocess mort prématurément"
                        " (exitcode=%s, %d visages, hdbscan_elapsed=%ss)",
                        exitcode,
                        n,
                        int(time.monotonic() - hdbscan_start) if hdbscan_start else "N/A",
                    )
                    return 0

    finally:
        try:
            proc.kill()
        except Exception:
            pass
        proc.join(timeout=5)
        parent_conn.close()

    if result_labels is None:
        return 0

    if progress_cb:
        progress_cb(
            translate("Clusterer", "Clustering: {n} groups → saving…"
                      ).format(n=_fmt_n(n_clusters))
        )

    face_db.update_clusters(face_ids, result_labels, progress_cb=progress_cb)

    _last_clustered_n = n
    logger.info("Clustering: %d groupe(s), %d singleton(s)", n_clusters, n_singletons)
    return n_clusters


class ClusterThread(QThread):
    """
    Off-UI-thread wrapper around HDBSCAN clustering.

    Signals
    -------
    progress(message)      — current stage (for the status bar)
    finished(n_clusters)   — clustering finished, n distinct groups found
    error(message)         — a missing dependency or another failure
    """

    progress = Signal(str)
    finished = Signal(int)
    error    = Signal(str)

    def __init__(self, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db

    def run(self) -> None:
        from src.core.thread_journal import journal, rss_mb
        t0 = journal.start("ClusterThread", "Clustering des visages",
                           rss_mb=round(rss_mb(), 1))
        try:
            n = _run_clustering(self._face_db, progress_cb=self.progress.emit)
            journal.end("ClusterThread", f"{n} groupe(s) formé(s)", t0,
                        rss_mb=round(rss_mb(), 1))
            self.finished.emit(n)
        except Exception as exc:
            journal.error("ClusterThread", str(exc), t0)
            logger.warning("ClusterThread erreur: %s", exc)
            self.error.emit(str(exc))
