import logging

from PySide6.QtCore import QThread, Signal

from src.core.config import Config
from src.faces.face_database import FaceDatabase

logger = logging.getLogger(__name__)

# Distance cosinus par défaut : même personne ArcFace ≈ 0.10–0.40,
# personnes différentes > 0.50–0.70.
_DEFAULT_THRESHOLD = 0.60


def _run_clustering(face_db: FaceDatabase) -> int:
    """
    HDBSCAN clustering sur les embeddings ArcFace normalisés.

    Pipeline :
    1. Normalisation L2 → sphère unité  (d_eucl = sqrt(2 * d_cosinus))
    2. PCA 512 → 64 dims  — contourne la malédiction de la dimensionnalité :
       le BallTree de HDBSCAN dégénère en O(N²) au-delà de ~20 dims,
       rendant le clustering impraticable sur 93 000+ visages à 512 dims.
       PCA conserve >90 % de la variance discriminante et réduit le temps
       de ~20 min (512 dims) à ~30 s (64 dims).
    3. Re-normalisation après PCA.
    4. HDBSCAN euclidien (≡ cosinus sur sphère unité après normalisation).

    Safe to call from any thread.
    Returns number of distinct clusters (singletons exclus).
    Raises RuntimeError if deps missing.
    """
    try:
        import numpy as np
        from sklearn.cluster import HDBSCAN
        from sklearn.decomposition import PCA
    except ImportError as exc:
        raise RuntimeError(f"Le clustering nécessite numpy et scikit-learn >= 1.3 : {exc}")

    X, face_ids = face_db.get_all_embeddings()
    n = len(face_ids)

    if n == 0:
        logger.debug("Clustering: aucun visage")
        return 0

    threshold = float(Config().get("faces.cluster_threshold", _DEFAULT_THRESHOLD))
    logger.info("Clustering HDBSCAN: %d visages (seuil cosinus=%.2f)", n, threshold)

    X = X.astype(np.float32, copy=False)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X /= norms

    if n == 1:
        face_db.update_clusters(face_ids, [0])
        return 1

    # ── Réduction PCA 512 → 64 dims ────────────────────────────────────────
    _PCA_DIMS = 64
    if X.shape[1] > _PCA_DIMS and n > _PCA_DIMS:
        logger.info("Clustering PCA: %d → %d dims", X.shape[1], _PCA_DIMS)
        X = PCA(n_components=_PCA_DIMS, random_state=42).fit_transform(X).astype(np.float32)
        norms2 = np.linalg.norm(X, axis=1, keepdims=True)
        norms2[norms2 == 0] = 1.0
        X /= norms2

    # Sur vecteurs unitaires : d_eucl = sqrt(2 * d_cosinus).
    # max(0, ...) évite sqrt d'un négatif si threshold invalide.
    eucl_eps = float(np.sqrt(max(0.0, 2.0 * threshold)))
    labels = HDBSCAN(
        min_cluster_size=2,
        min_samples=1,
        metric="euclidean",
        cluster_selection_epsilon=eucl_eps,
        copy=True,
    ).fit_predict(X)

    # HDBSCAN marque les visages isolés (bruit) comme -1.
    # On leur attribue un cluster singleton unique au-delà des vrais clusters
    # pour qu'ils restent accessibles dans l'interface.
    labels = labels.tolist()
    max_real = max((lbl for lbl in labels if lbl >= 0), default=-1)
    next_singleton = max_real + 1
    n_singletons = 0
    for i, lbl in enumerate(labels):
        if lbl == -1:
            labels[i] = next_singleton
            next_singleton += 1
            n_singletons += 1

    face_db.update_clusters(face_ids, labels)

    n_clusters = max_real + 1
    logger.info("Clustering: %d groupe(s), %d singleton(s)", n_clusters, n_singletons)
    return n_clusters


class ClusterThread(QThread):
    """
    Off-UI-thread wrapper around HDBSCAN clustering.

    Signals
    -------
    finished(n_clusters)   — clustering done, n distinct groups found
    error(message)         — dep missing or other failure
    """

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
            n = _run_clustering(self._face_db)
            journal.end("ClusterThread", f"{n} groupe(s) formé(s)", t0,
                        rss_mb=round(rss_mb(), 1))
            self.finished.emit(n)
        except Exception as exc:
            journal.error("ClusterThread", str(exc), t0)
            logger.warning("ClusterThread erreur: %s", exc)
            self.error.emit(str(exc))
