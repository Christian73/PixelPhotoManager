import logging

from PySide6.QtCore import QThread, Signal

from src.core.config import Config
from src.faces.face_database import FaceDatabase

logger = logging.getLogger(__name__)

# Distance cosinus par défaut : deux visages sont dans le même groupe si leur
# distance cosinus est ≤ à ce seuil. ArcFace : même personne ≈ 0.10–0.40,
# personnes différentes > 0.50–0.70.
_DEFAULT_THRESHOLD = 0.60


def _run_clustering(face_db: FaceDatabase) -> int:
    """
    Agglomerative clustering (average linkage, cosine distance) sur les
    embeddings ArcFace. Safe to call from any thread.
    Returns number of distinct clusters. Raises RuntimeError if deps missing.
    """
    try:
        import numpy as np
        from sklearn.cluster import AgglomerativeClustering
    except ImportError as exc:
        raise RuntimeError(f"Le clustering nécessite numpy et scikit-learn : {exc}")

    X, face_ids = face_db.get_all_embeddings()
    n = len(face_ids)

    if n == 0:
        logger.debug("Clustering: aucun visage")
        return 0

    threshold = Config().get("faces.cluster_threshold", _DEFAULT_THRESHOLD)
    logger.info("Clustering: %d visages (seuil cosinus=%.2f)", n, threshold)

    X = X.astype(np.float32, copy=False)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X /= norms

    if n == 1:
        face_db.update_clusters(face_ids, [0])
        return 1

    labels = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=threshold,
    ).fit_predict(X)

    face_db.update_clusters(face_ids, labels.tolist())

    n_clusters = int(labels.max() + 1)
    logger.info("Clustering: %d groupe(s)", n_clusters)
    return n_clusters


class ClusterThread(QThread):
    """
    Off-UI-thread wrapper around agglomerative clustering.

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
