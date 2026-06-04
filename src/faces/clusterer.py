import logging

from PySide6.QtCore import QThread, Signal

from src.faces.face_database import FaceDatabase

logger = logging.getLogger(__name__)

_EPS = 0.4
_MIN_SAMPLES = 2


def _run_dbscan(face_db: FaceDatabase) -> int:
    """
    Core DBSCAN logic (no Qt, safe to call from any thread).
    Returns number of distinct clusters. Raises RuntimeError if deps missing.
    """
    try:
        import numpy as np
        from sklearn.cluster import DBSCAN
    except ImportError as exc:
        raise RuntimeError(f"Le clustering nécessite numpy et scikit-learn : {exc}")

    embeddings, face_ids = face_db.get_all_embeddings()
    n = len(embeddings)

    if n < _MIN_SAMPLES:
        logger.debug("ClusterThread: %d visage(s), pas assez", n)
        return 0

    logger.info("ClusterThread: clustérisation de %d visages", n)
    X = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X /= norms

    labels = DBSCAN(
        eps=_EPS, min_samples=_MIN_SAMPLES, metric="euclidean", n_jobs=-1
    ).fit_predict(X)

    face_db.update_clusters(face_ids, labels.tolist())

    n_clusters = int(len(set(labels) - {-1}))
    n_noise    = int((labels == -1).sum())
    logger.info("ClusterThread: %d cluster(s), %d bruit", n_clusters, n_noise)
    return n_clusters


class ClusterThread(QThread):
    """
    Off-UI-thread wrapper around DBSCAN clustering.

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
        try:
            n = _run_dbscan(self._face_db)
            self.finished.emit(n)
        except Exception as exc:
            logger.warning("ClusterThread erreur: %s", exc)
            self.error.emit(str(exc))
