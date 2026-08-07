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

_PCA_DIMS        = 32    # ball_tree efficace sous ~30 dims ; 32 conserve >90 % variance ArcFace
_CLUSTER_TIMEOUT = 1800  # secondes max (30 min) avant abandon

# Sentinelle : N de visages non-identifiés au dernier clustering réussi.
# Permet de sauter le reclustering si rien n'a changé.
_last_clustered_n: int = -1


def reset_clustering_cache() -> None:
    """Invalide le cache de "dernier N regroupé" utilisé par `_run_clustering`
    pour sauter le reclustering si rien n'a changé.

    À appeler après tout `FaceDatabase.reset_clustering()`/`reset_index()` :
    ces méthodes vident `cluster_id` en masse sans changer le nombre de
    visages non identifiés (ré-indexation à l'identique) — sans cet appel,
    `_run_clustering` voit `n == _last_clustered_n`, croit qu'aucun changement
    n'est survenu et saute le regroupement, laissant tous les visages bloqués
    avec `cluster_id=NULL` indéfiniment (bug constaté 2026-07 via
    `test_faces_reset_full`)."""
    global _last_clustered_n
    _last_clustered_n = -1

# Cohésion minimale (cosine) exigée entre TOUTE paire de visages d'un même groupe HDBSCAN,
# alignée sur _SIM_STRONG (people_panel.py, "très probable"). min_samples=1 rend HDBSCAN
# quasi équivalent à un single-linkage : deux visages peuvent se retrouver dans le même
# cluster via une chaîne de voisins proches sans jamais se ressembler eux-mêmes.
_PURITY_MIN_SIM        = 0.60
# Au-delà, la scission "complete linkage" (matrice de distances complète, O(k²)) coûterait
# trop cher pour un seul groupe — cas pathologique, on le laisse tel quel plutôt que ralentir
# tout le clustering.
_PURITY_MAX_CLUSTER_N  = 2000


def _purify_clusters(X_full, labels):
    """Scinde les clusters HDBSCAN dont certaines paires de visages sont trop dissemblables.

    Revérifie chaque cluster (hors bruit) avec un clustering hiérarchique "complete linkage"
    sur les embeddings pleine dimension (normalisés, non réduits par PCA) : ce linkage borne
    la dissemblance MAXIMALE entre deux membres — contrairement au chaînage de HDBSCAN — donc
    un cluster n'en ressort intact que si toutes les paires qu'il contient dépassent
    _PURITY_MIN_SIM. Les sous-groupes obtenus reçoivent de nouveaux labels (jamais fusionnés
    entre eux au-delà de ce qu'HDBSCAN avait déjà proposé)."""
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
            continue  # groupe déjà cohérent : rien à scinder
        for sub_id in np.unique(sub_labels):
            if sub_id == 0:
                continue  # garde le label HDBSCAN d'origine pour le premier sous-groupe
            labels[idx[sub_labels == sub_id]] = next_label
            next_label += 1

    return labels


_NB_SP = " "  # espace fine insécable utilisée comme séparateur de milliers


def _clustering_worker_proc(X_bytes: bytes, n: int, d: int, conn) -> None:
    """
    PCA + HDBSCAN dans un sous-processus isolé.

    Envoie des messages de progression via le pipe :
      ("pca",)                       — normalisation terminée, PCA démarre
      ("hdbscan",)                   — PCA terminée, HDBSCAN démarre
      ("result", n_clusters, n_singletons, labels)  — terminé avec succès
      ("error", message)             — exception inattendue

    Doit être MODULE-LEVEL pour être picklable sur Windows (spawn).
    """
    import numpy as np
    from hdbscan import HDBSCAN
    from sklearn.decomposition import PCA

    try:
        X = np.frombuffer(X_bytes, dtype=np.float32).reshape(n, d).copy()

        # Normalisation L2 → sphère unité
        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        X /= norms
        X_full = X  # embeddings pleine dimension normalisés — conservés pour _purify_clusters

        # Réduction PCA
        if X.shape[1] > _PCA_DIMS and n > _PCA_DIMS:
            conn.send(("pca",))
            X = PCA(n_components=_PCA_DIMS, random_state=42).fit_transform(X).astype(np.float32)
            norms2 = np.linalg.norm(X, axis=1, keepdims=True)
            norms2[norms2 == 0] = 1.0
            X /= norms2

        conn.send(("hdbscan",))

        # hdbscan package (C++/Cython) — beaucoup plus rapide que sklearn pour grands n.
        # boruvka_balltree : MST Borůvka O(n log²n) vs Prim O(n²) avec sklearn.
        # core_dist_n_jobs=1 : 1 thread → évite duplication des buffers mémoire.
        labels = HDBSCAN(
            min_cluster_size=2,
            min_samples=1,
            metric="euclidean",
            cluster_selection_method="leaf",
            algorithm="boruvka_balltree",
            leaf_size=100,
            core_dist_n_jobs=1,
        ).fit_predict(X)

        # min_samples=1 chaîne facilement des visages peu similaires entre eux (single-linkage) :
        # revérifier chaque cluster en pleine dimension avant de l'accepter tel quel.
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
    """Formate un entier avec espace fine insécable comme séparateur de milliers."""
    return f"{n:,}".replace(",", _NB_SP)


def _run_clustering(
    face_db: FaceDatabase,
    progress_cb: Callable[[str], None] | None = None,
) -> int:
    """
    HDBSCAN clustering sur les embeddings ArcFace normalisés.

    Pipeline :
    1. Assignation synthétique des cluster_ids pour les visages déjà identifiés.
    2. Normalisation L2 → sphère unité sur les visages non identifiés.
    3. PCA 512 → _PCA_DIMS dims + re-normalisation.
    4. HDBSCAN euclidien ball_tree.

    Le calcul s'exécute dans un subprocess isolé via multiprocessing.Process + Pipe.
    Les étapes sont remontées au thread appelant via progress_cb(message).

    Safe to call from any thread.
    Returns number of distinct clusters (singletons exclus).
    Raises RuntimeError if deps missing.
    """
    global _last_clustered_n

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(f"Le clustering nécessite numpy : {exc}")

    n_synthetic = face_db.assign_person_synthetic_clusters()
    if n_synthetic and progress_cb:
        progress_cb(translate("Clusterer", "Clustering : {n} visages identifiés pré-assignés…"
                              ).format(n=_fmt_n(n_synthetic)))

    X, face_ids = face_db.get_all_embeddings(only_unidentified=True)
    n = len(face_ids)

    if n == 0:
        logger.debug("Clustering: aucun visage (tous déjà identifiés)")
        return 0

    # Skip si aucun changement depuis le dernier clustering réussi :
    # même N de visages non-identifiés ET aucune assignation synthétique nouvelle.
    if n == _last_clustered_n and n_synthetic == 0:
        logger.debug("Clustering: %d visages inchangés — skip", n)
        return 0

    logger.info("Clustering HDBSCAN: %d visages non identifiés", n)
    if progress_cb:
        progress_cb(translate("Clusterer", "Clustering : normalisation ({n} visages)…"
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
    child_conn.close()   # ferme le bout enfant dans le processus parent

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
                            f"Clustering : PCA {d}→{_PCA_DIMS} dims…"
                        )
                elif tag == "hdbscan":
                    hdbscan_start = time.monotonic()
                    if progress_cb:
                        progress_cb(
                            translate("Clusterer",
                                      "Clustering : HDBSCAN ({n} visages) — {time}…"
                                      ).format(n=n_fmt, time="0:00")
                        )
                elif tag == "result":
                    _, n_clusters, n_singletons, result_labels = msg
                    break
                elif tag == "error":
                    logger.error("Clustering subprocess erreur : %s", msg[1])
                    return 0

            else:
                # poll timeout (1 s) — mise à jour du chronomètre pendant HDBSCAN
                if hdbscan_start is not None and progress_cb:
                    elapsed = int(time.monotonic() - hdbscan_start)
                    m, s = divmod(elapsed, 60)
                    progress_cb(
                        translate("Clusterer",
                                  "Clustering : HDBSCAN ({n} visages) — {time}…"
                                  ).format(n=n_fmt, time=f"{m}:{s:02d}")
                    )
                if not proc.is_alive():
                    exitcode = proc.exitcode
                    # exitcode < 0 → tué par signal (OOM=-9 sur Linux, ~-1073741819 sur Windows)
                    # exitcode > 0 → exception non rattrapée dans le worker
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
            f"Clustering : {_fmt_n(n_clusters)} groupes → sauvegarde…"
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
    progress(message)      — étape en cours (pour la barre de status)
    finished(n_clusters)   — clustering terminé, n groupes distincts trouvés
    error(message)         — dépendance manquante ou autre échec
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
