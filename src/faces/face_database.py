import logging
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from src.core.app_dirs import APP_DATA_DIR
from src.core.models import FaceInfo, PersonInfo

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "faces.db"

_CREATE_INDEXED = """
CREATE TABLE IF NOT EXISTS indexed_photos (
    photo_path TEXT PRIMARY KEY,
    indexed_at REAL NOT NULL,
    face_count INTEGER DEFAULT 0
)
"""

_CREATE_FACES = """
CREATE TABLE IF NOT EXISTS faces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path TEXT NOT NULL,
    bbox_x     INTEGER NOT NULL,
    bbox_y     INTEGER NOT NULL,
    bbox_w     INTEGER NOT NULL,
    bbox_h     INTEGER NOT NULL,
    embedding  BLOB,
    cluster_id INTEGER,
    person_id  INTEGER,
    ignored    INTEGER DEFAULT 0
)
"""


_CREATE_PICASA_ANNOTATIONS = """
CREATE TABLE IF NOT EXISTS picasa_annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path TEXT NOT NULL,
    bbox_x     INTEGER NOT NULL,
    bbox_y     INTEGER NOT NULL,
    bbox_w     INTEGER NOT NULL,
    bbox_h     INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    consumed   INTEGER DEFAULT 0
)
"""

_IOU_THRESHOLD = 0.30   # recouvrement minimum pour associer un visage Picasa à un visage détecté


def _iou(a: tuple, b: tuple) -> float:
    """IoU entre deux bboxes (x, y, w, h)."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(ax2, bx2),   min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _enc(embedding: list[float]) -> bytes:
    return struct.pack(f"{len(embedding)}f", *embedding)


def _dec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _centroid(embeddings: list[list[float]]) -> list[float]:
    try:
        import numpy as np
        return np.mean(np.array(embeddings, dtype=np.float32), axis=0).tolist()
    except ImportError:
        n = len(embeddings)
        dim = len(embeddings[0])
        return [sum(e[d] for e in embeddings) / n for d in range(dim)]


class FaceDatabase:
    def __init__(self, db_path: str | Path = _DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(_CREATE_INDEXED)
                conn.execute(_CREATE_FACES)
                conn.execute(_CREATE_PICASA_ANNOTATIONS)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_faces_photo    ON faces(photo_path)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_faces_cluster  ON faces(cluster_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_faces_person   ON faces(person_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_picasa_photo   ON picasa_annotations(photo_path)"
                )
                # Migrations : ajouter les colonnes manquantes
                cols = {r[1] for r in conn.execute("PRAGMA table_info(faces)")}
                if "ignored" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN ignored INTEGER DEFAULT 0"
                    )
                if "pinned" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN pinned INTEGER DEFAULT 0"
                    )
                if "is_cover" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN is_cover INTEGER DEFAULT 0"
                    )
                # Migration indexed_photos
                ip_cols = {r[1] for r in conn.execute("PRAGMA table_info(indexed_photos)")}
                if "rotation" not in ip_cols:
                    conn.execute(
                        "ALTER TABLE indexed_photos ADD COLUMN rotation INTEGER DEFAULT 0"
                    )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ indexing

    def get_paths_to_index(self, all_paths: list[str]) -> list[str]:
        """Returns paths from all_paths that have not been indexed yet."""
        if not all_paths:
            return []
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT photo_path FROM indexed_photos"
                ).fetchall()
            finally:
                conn.close()
        indexed = {r[0] for r in rows}
        return [p for p in all_paths if os.path.normpath(p) not in indexed]

    def save_faces(self, photo_path: str, detections: list[dict], rotation: int = 0) -> None:
        """
        Persist detected faces for a photo.
        detections: list of {'bbox': (x,y,w,h), 'embedding': list[float]}
        rotation: CW degrees applied during detection (stored to reconstruct face crops).
        """
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM faces WHERE photo_path=?", (photo_path,)
                )
                # Remettre les annotations Picasa à consumed=0 pour qu'elles soient
                # ré-appliquées aux nouvelles détections ci-dessous.
                # Sans ça, une re-analyse efface les faces mais laisse consumed=1 :
                # les annotations ne seraient jamais ré-appliquées.
                conn.execute(
                    "UPDATE picasa_annotations SET consumed=0 WHERE photo_path=?",
                    (photo_path,)
                )
                for det in detections:
                    x, y, w, h = det["bbox"]
                    emb = det.get("embedding")
                    blob = _enc(emb) if emb else None
                    conn.execute(
                        "INSERT INTO faces"
                        " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, embedding)"
                        " VALUES (?,?,?,?,?,?)",
                        (photo_path, x, y, w, h, blob),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO indexed_photos"
                    " (photo_path, indexed_at, face_count, rotation) VALUES (?,?,?,?)",
                    (photo_path, time.time(), len(detections), rotation),
                )
                # Appliquer les annotations Picasa en attente (si présentes)
                self._apply_picasa_annotations(conn, photo_path)
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ clustering

    def get_all_embeddings(self) -> tuple["np.ndarray", list[int]]:
        """Returns (embeddings, face_ids) for non-pinned faces with stored embeddings.

        embeddings is a float32 ndarray of shape (N, D) built directly from the
        binary blobs — avoids creating N×D Python float objects as an intermediate.
        """
        import numpy as np
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, embedding FROM faces"
                    " WHERE embedding IS NOT NULL"
                    "   AND (pinned IS NULL OR pinned = 0)"
                ).fetchall()
            finally:
                conn.close()
        if not rows:
            return np.empty((0, 0), dtype=np.float32), []
        face_ids = [r[0] for r in rows]
        embeddings = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return embeddings, face_ids

    def update_clusters(self, face_ids: list[int], labels: list[int]) -> None:
        if not face_ids:
            return
        with self._lock:
            conn = self._conn()
            try:
                # Ne pas toucher les faces isolées manuellement (pinned=1)
                conn.execute(
                    "UPDATE faces SET cluster_id=NULL"
                    " WHERE pinned IS NULL OR pinned = 0"
                )
                conn.executemany(
                    "UPDATE faces SET cluster_id=? WHERE id=?",
                    [
                        (int(label) if label >= 0 else None, fid)
                        for fid, label in zip(face_ids, labels)
                    ],
                )
                # Nettoyer les faces ArcFace qui sont devenues bruit (cluster_id=NULL)
                # mais conservent un person_id résiduel d'un clustering précédent.
                # Les faces sans embedding (placeholders Picasa) sont préservées.
                conn.execute(
                    "UPDATE faces SET person_id=NULL"
                    " WHERE (pinned IS NULL OR pinned=0)"
                    "   AND cluster_id IS NULL"
                    "   AND person_id IS NOT NULL"
                    "   AND embedding IS NOT NULL"
                )
                # Propager le person_id aux faces sans person_id dans un cluster déjà nommé.
                # Couvre le cas d'une nouvelle face ajoutée par reclustering à un cluster
                # dont d'autres faces ont déjà un person_id (assignation antérieure).
                conn.execute("""
                    UPDATE faces
                    SET person_id = (
                        SELECT f2.person_id FROM faces f2
                        WHERE f2.cluster_id = faces.cluster_id
                          AND f2.person_id IS NOT NULL
                        LIMIT 1
                    )
                    WHERE cluster_id IS NOT NULL
                      AND person_id IS NULL
                      AND EXISTS (
                          SELECT 1 FROM faces f3
                          WHERE f3.cluster_id = faces.cluster_id
                            AND f3.person_id IS NOT NULL
                      )
                """)
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ queries

    def get_clusters(self) -> list[tuple[int, int]]:
        """Returns [(cluster_id, face_count)] for all clusters, ordered by size desc."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, COUNT(*) FROM faces"
                    " WHERE cluster_id IS NOT NULL"
                    " GROUP BY cluster_id ORDER BY COUNT(*) DESC"
                ).fetchall()
            finally:
                conn.close()
        return [(r[0], r[1]) for r in rows]

    def get_unnamed_clusters(self) -> list[tuple[int, int]]:
        """Returns [(cluster_id, face_count)] for clusters with no person assigned,
        not ignored and not pinned (isolated faces are excluded)."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, COUNT(*) FROM faces"
                    " WHERE cluster_id IS NOT NULL"
                    "   AND person_id IS NULL"
                    "   AND ignored = 0"
                    "   AND (pinned IS NULL OR pinned = 0)"
                    " GROUP BY cluster_id ORDER BY COUNT(*) DESC"
                ).fetchall()
            finally:
                conn.close()
        return [(r[0], r[1]) for r in rows]

    def ignore_cluster(self, cluster_id: int) -> None:
        """Mark all faces of a cluster as ignored so they won't appear for naming."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET ignored=1 WHERE cluster_id=?", (cluster_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def unignore_cluster(self, cluster_id: int) -> None:
        """Re-expose a previously ignored cluster."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET ignored=0 WHERE cluster_id=?", (cluster_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def get_representative_face(
        self,
        cluster_id: Optional[int] = None,
        person_id: Optional[int] = None,
    ) -> Optional[FaceInfo]:
        """Returns the cover face (is_cover=1) if set, otherwise the largest-bbox face."""
        with self._lock:
            conn = self._conn()
            try:
                if cluster_id is not None:
                    # Prefer manually chosen cover
                    row = conn.execute(
                        "SELECT id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                        "       cluster_id, person_id"
                        " FROM faces WHERE cluster_id=? AND is_cover=1 LIMIT 1",
                        (cluster_id,),
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            "SELECT id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                            "       cluster_id, person_id"
                            " FROM faces WHERE cluster_id=?"
                            " ORDER BY (bbox_w * bbox_h) DESC LIMIT 1",
                            (cluster_id,),
                        ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                        "       cluster_id, person_id"
                        " FROM faces WHERE person_id=?"
                        " ORDER BY (bbox_w * bbox_h) DESC LIMIT 1",
                        (person_id,),
                    ).fetchone()
            finally:
                conn.close()
        if row:
            return FaceInfo(
                id=row[0], photo_path=row[1],
                bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                cluster_id=row[6], person_id=row[7],
            )
        return None

    def set_cover_face(self, face_id: int) -> None:
        """Définit ce visage comme vignette du groupe (is_cover). Efface l'ancien cover."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT cluster_id FROM faces WHERE id=?", (face_id,)
                ).fetchone()
                if row is None or row[0] is None:
                    return
                cluster_id = row[0]
                conn.execute(
                    "UPDATE faces SET is_cover=0 WHERE cluster_id=?", (cluster_id,)
                )
                conn.execute(
                    "UPDATE faces SET is_cover=1 WHERE id=?", (face_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def get_representative_embedding(
        self,
        cluster_id: Optional[int] = None,
        person_id: Optional[int] = None,
    ) -> Optional[list[float]]:
        """Return the centroid (mean) of all embeddings for a cluster or person.

        Using the centroid rather than a single face captures the full visual
        diversity accumulated across merged groups and varied photos.
        """
        with self._lock:
            conn = self._conn()
            try:
                if cluster_id is not None:
                    rows = conn.execute(
                        "SELECT embedding FROM faces"
                        " WHERE cluster_id=? AND embedding IS NOT NULL",
                        (cluster_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT embedding FROM faces"
                        " WHERE person_id=? AND embedding IS NOT NULL",
                        (person_id,),
                    ).fetchall()
            finally:
                conn.close()
        if not rows:
            return None
        embeddings = [_dec(r[0]) for r in rows]
        n = len(embeddings)
        dim = len(embeddings[0])
        return [sum(embeddings[i][d] for i in range(n)) / n for d in range(dim)]

    def get_all_cluster_centroids(
        self, cluster_ids: list[int]
    ) -> dict[int, list[float]]:
        """Retourne {cluster_id: centroïde} pour tous les clusters demandés.
        Requête par lots de 500 pour respecter la limite SQLite des variables (999)."""
        if not cluster_ids:
            return {}
        _CHUNK = 500
        by_cluster: dict[int, list] = {}
        with self._lock:
            conn = self._conn()
            try:
                for i in range(0, len(cluster_ids), _CHUNK):
                    chunk = cluster_ids[i:i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT cluster_id, embedding FROM faces"
                        f" WHERE cluster_id IN ({ph}) AND embedding IS NOT NULL",
                        chunk,
                    ).fetchall()
                    for cid, blob in rows:
                        by_cluster.setdefault(cid, []).append(_dec(blob))
            finally:
                conn.close()
        return {cid: _centroid(embs) for cid, embs in by_cluster.items()}

    def get_all_person_centroids(
        self, person_ids: list[int]
    ) -> dict[int, list[float]]:
        """Retourne {person_id: centroïde} pour toutes les personnes demandées."""
        if not person_ids:
            return {}
        _CHUNK = 500
        by_person: dict[int, list] = {}
        with self._lock:
            conn = self._conn()
            try:
                for i in range(0, len(person_ids), _CHUNK):
                    chunk = person_ids[i:i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT person_id, embedding FROM faces"
                        f" WHERE person_id IN ({ph}) AND embedding IS NOT NULL",
                        chunk,
                    ).fetchall()
                    for pid, blob in rows:
                        by_person.setdefault(pid, []).append(_dec(blob))
            finally:
                conn.close()
        return {pid: _centroid(embs) for pid, embs in by_person.items()}

    def get_all_person_cluster_centroids(
        self, person_ids: list[int]
    ) -> dict[int, dict[int, list[float]]]:
        """
        Retourne {person_id: {cluster_id: centroïde}} pour toutes les personnes.

        Un nom pouvant être associé à plusieurs groupes distincts, chaque groupe
        conserve son propre centroïde plutôt que d'être fondu dans une moyenne
        globale.  Cela préserve la diversité visuelle de la personne et améliore
        la précision des suggestions de reconnaissance.
        """
        if not person_ids:
            return {}
        _CHUNK = 500
        by_pc: dict[tuple, list] = {}
        with self._lock:
            conn = self._conn()
            try:
                for i in range(0, len(person_ids), _CHUNK):
                    chunk = person_ids[i:i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT person_id, cluster_id, embedding FROM faces"
                        f" WHERE person_id IN ({ph})"
                        f"   AND embedding IS NOT NULL AND cluster_id IS NOT NULL",
                        chunk,
                    ).fetchall()
                    for pid, cid, blob in rows:
                        by_pc.setdefault((pid, cid), []).append(_dec(blob))
            finally:
                conn.close()
        result: dict[int, dict[int, list[float]]] = {}
        for (pid, cid), embs in by_pc.items():
            result.setdefault(pid, {})[cid] = _centroid(embs)
        return result

    def get_cluster_person(self, cluster_id: int) -> int | None:
        """Retourne le person_id déjà associé à ce groupe, ou None s'il n'est pas nommé."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT DISTINCT person_id FROM faces"
                    " WHERE cluster_id=? AND person_id IS NOT NULL LIMIT 1",
                    (cluster_id,),
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else None

    def get_cluster_persons(self, cluster_ids: list[int]) -> dict[int, int]:
        """Retourne {cluster_id: person_id} pour les clusters ayant au moins une face nommée.
        Utile pour afficher le nom d'une personne sur des faces ré-indexées après assignation."""
        if not cluster_ids:
            return {}
        _CHUNK = 500
        result: dict[int, int] = {}
        with self._lock:
            conn = self._conn()
            try:
                for i in range(0, len(cluster_ids), _CHUNK):
                    chunk = cluster_ids[i:i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT cluster_id, person_id FROM faces"
                        f" WHERE cluster_id IN ({ph})"
                        f"   AND person_id IS NOT NULL"
                        f" GROUP BY cluster_id",
                        chunk,
                    ).fetchall()
                    result.update({r[0]: r[1] for r in rows})
            finally:
                conn.close()
        return result

    def get_all_representative_faces(
        self, cluster_ids: list[int]
    ) -> "dict[int, FaceInfo]":
        """Retourne {cluster_id: FaceInfo} pour tous les clusters en une seule requête.
        Priorité : is_cover=1, sinon le visage avec la plus grande bbox."""
        if not cluster_ids:
            return {}
        placeholders = ",".join("?" * len(cluster_ids))
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    f"SELECT id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                    f"       cluster_id, person_id, is_cover,"
                    f"       (bbox_w * bbox_h) AS area"
                    f" FROM faces"
                    f" WHERE cluster_id IN ({placeholders})"
                    f" ORDER BY cluster_id, is_cover DESC, area DESC",
                    cluster_ids,
                ).fetchall()
            finally:
                conn.close()
        result: dict[int, FaceInfo] = {}
        for row in rows:
            cid = row[6]
            if cid not in result:  # première ligne = meilleure (cover ou plus grande bbox)
                result[cid] = FaceInfo(
                    id=row[0], photo_path=row[1],
                    bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                    cluster_id=cid, person_id=row[7],
                )
        return result

    def get_faces_for_photo(self, photo_path: str) -> list[FaceInfo]:
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "       f.cluster_id, f.person_id, f.ignored, f.pinned,"
                    "       COALESCE(ip.rotation, 0)"
                    " FROM faces f"
                    " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                    " WHERE f.photo_path=?",
                    (photo_path,),
                ).fetchall()
            finally:
                conn.close()
        return [
            FaceInfo(
                id=r[0], photo_path=r[1],
                bbox_x=r[2], bbox_y=r[3], bbox_w=r[4], bbox_h=r[5],
                cluster_id=r[6], person_id=r[7],
                ignored=bool(r[8]),
                pinned=bool(r[9]),
                detected_rotation=r[10],
            )
            for r in rows
        ]

    def get_photos_for_cluster(self, cluster_id: int) -> list[str]:
        """Returns distinct photo paths for a cluster."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT photo_path FROM faces WHERE cluster_id=?",
                    (cluster_id,),
                ).fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    def get_clusters_for_person(self, person_id: int) -> list[tuple[int, int]]:
        """Returns [(cluster_id, photo_count)] for clusters where this person has a face.
        photo_count = distinct photos WHERE THIS PERSON's face appears in the cluster.
        Ordered by photo_count descending."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, COUNT(DISTINCT photo_path)"
                    " FROM faces"
                    " WHERE person_id=? AND cluster_id IS NOT NULL"
                    " GROUP BY cluster_id"
                    " ORDER BY COUNT(DISTINCT photo_path) DESC",
                    (person_id,),
                ).fetchall()
            finally:
                conn.close()
        return [(r[0], r[1]) for r in rows]

    def unassign_person_from_cluster(self, person_id: int, cluster_id: int) -> None:
        """Clears person_id on all faces of cluster_id that belong to this person."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id = NULL"
                    " WHERE person_id = ? AND cluster_id = ?",
                    (person_id, cluster_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_photos_for_person(self, person_id: int) -> list[str]:
        """Returns distinct photo paths for a named person."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT photo_path FROM faces WHERE person_id=?",
                    (person_id,),
                ).fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ assignment

    def assign_person_to_face(self, face_id: int, person_id: int) -> None:
        """Assign a named person to a single face."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id)
                )
                conn.commit()
            finally:
                conn.close()

    def unassign_face(self, face_id: int) -> None:
        """Remove person and cluster from a single face (returns it to unknowns).
        Clears pinned so the face re-entre dans le clustering automatique."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=NULL, cluster_id=NULL, pinned=0"
                    " WHERE id=?",
                    (face_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def isolate_face(self, face_id: int) -> None:
        """Sépare une face de son groupe et la protège du re-clustering.
        Lui assigne un cluster_id négatif unique (isolé, invisible dans la grille)."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
                ).fetchone()
                min_pinned = row[0] if row and row[0] is not None else 0
                new_cluster_id = min(min_pinned, 0) - 1   # -1, -2, -3, ...
                conn.execute(
                    "UPDATE faces SET cluster_id=?, pinned=1, person_id=NULL"
                    " WHERE id=?",
                    (new_cluster_id, face_id),
                )
                conn.commit()
            finally:
                conn.close()

    def ignore_face(self, face_id: int) -> None:
        """Mark a single face as ignored."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET ignored=1 WHERE id=?", (face_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def merge_clusters(self, source_cluster_id: int, target_cluster_id: int) -> None:
        """Move all faces from source_cluster_id into target_cluster_id."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET cluster_id=? WHERE cluster_id=?",
                    (target_cluster_id, source_cluster_id),
                )
                conn.commit()
            finally:
                conn.close()

    def assign_person_to_cluster(self, cluster_id: int, person_id: int) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE cluster_id=?",
                    (person_id, cluster_id),
                )
                conn.commit()
            finally:
                conn.close()

    def unassign_person(self, person_id: int) -> None:
        """Remove person assignment from all faces (before deleting a person)."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=NULL WHERE person_id=?", (person_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def merge_persons(self, keep_id: int, remove_id: int) -> None:
        """
        Reassign all faces of remove_id to keep_id.
        The caller is responsible for deleting remove_id from catalog.persons.
        """
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE person_id=?",
                    (keep_id, remove_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ enrichment

    def enrich_persons(self, persons: list[PersonInfo]) -> None:
        """Fill photo_count and cover_path/cover_bbox in-place from face data."""
        if not persons:
            return
        with self._lock:
            conn = self._conn()
            try:
                # Compter les photos où cette personne a un visage détecté dans un cluster.
                # Cohérent avec get_clusters_for_person qui compte les photos par person_id,
                # pas toutes les photos du cluster (évite les fausses associations dues
                # aux clusters mixtes — deux personnes dans le même groupe HDBSCAN).
                count_rows = conn.execute(
                    "SELECT person_id, COUNT(DISTINCT photo_path)"
                    " FROM faces"
                    " WHERE person_id IS NOT NULL AND cluster_id IS NOT NULL"
                    " GROUP BY person_id"
                ).fetchall()
                # Une seule requête CTE pour toutes les faces représentatives
                # (remplace N appels get_representative_face → N connexions séparées)
                rep_rows = conn.execute(
                    "WITH ranked AS ("
                    "  SELECT person_id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                    "         ROW_NUMBER() OVER ("
                    "           PARTITION BY person_id"
                    "           ORDER BY bbox_w * bbox_h DESC"
                    "         ) AS rn"
                    "  FROM faces WHERE person_id IS NOT NULL"
                    ")"
                    " SELECT person_id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h"
                    " FROM ranked WHERE rn = 1"
                ).fetchall()
            finally:
                conn.close()
        counts = {r[0]: r[1] for r in count_rows}
        reps = {r[0]: r[1:] for r in rep_rows}
        for p in persons:
            if p.id in counts:
                p.photo_count = counts[p.id]
                rep = reps.get(p.id)
                if rep:
                    p.cover_path = rep[0]
                    p.cover_bbox = (rep[1], rep[2], rep[3], rep[4])

    # ------------------------------------------------------------------ cleanup

    # ------------------------------------------------------------------ Picasa annotations

    def save_picasa_annotations(
        self, photo_path: str, annotations: list[dict]
    ) -> None:
        """
        Persist Picasa face annotations for a photo.
        annotations: [{'bbox': (x,y,w,h), 'person_id': int}, ...]

        Les annotations remplacent les précédentes pour ce chemin.
        Si des visages détectés existent déjà, elles leur sont immédiatement
        associées par IoU ; sinon elles seront appliquées lors de la prochaine
        détection via save_faces().
        """
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM picasa_annotations WHERE photo_path=?", (photo_path,)
                )
                for ann in annotations:
                    x, y, w, h = ann["bbox"]
                    conn.execute(
                        "INSERT INTO picasa_annotations"
                        " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, person_id)"
                        " VALUES (?,?,?,?,?,?)",
                        (photo_path, x, y, w, h, ann["person_id"]),
                    )
                conn.commit()
                self._apply_picasa_annotations(conn, photo_path)

                # Si aucun visage ArcFace n'existe encore, insérer des placeholders
                # (sans embedding) pour que la personne soit visible immédiatement.
                # Les annotations restent non-consommées afin d'être ré-appliquées
                # proprement lors de la future analyse ArcFace (save_faces).
                has_faces = conn.execute(
                    "SELECT COUNT(*) FROM faces WHERE photo_path=?", (photo_path,)
                ).fetchone()[0]
                if has_faces == 0:
                    pending = conn.execute(
                        "SELECT bbox_x, bbox_y, bbox_w, bbox_h, person_id"
                        " FROM picasa_annotations"
                        " WHERE photo_path=? AND consumed=0",
                        (photo_path,),
                    ).fetchall()
                    for bx, by, bw, bh, pid in pending:
                        conn.execute(
                            "INSERT INTO faces"
                            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, person_id)"
                            " VALUES (?,?,?,?,?,?)",
                            (photo_path, bx, by, bw, bh, pid),
                        )

                conn.commit()
            finally:
                conn.close()

    def _apply_picasa_annotations(self, conn, photo_path: str) -> None:
        """
        Associe les annotations Picasa non consommées aux visages détectés
        du même chemin. Critère principal : le centre du visage ArcFace est
        à l'intérieur de la région Picasa (robuste car Picasa stocke une zone
        large englobant la tête/buste, alors qu'ArcFace donne une bbox serrée).
        Fallback : IoU > seuil si aucun centre ne tombe dans la région.
        Doit être appelée dans un contexte conn+lock déjà ouvert.
        Seuls les visages sans person_id existant sont candidats.
        """
        ann_rows = conn.execute(
            "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h, person_id"
            " FROM picasa_annotations"
            " WHERE photo_path=? AND consumed=0",
            (photo_path,),
        ).fetchall()
        if not ann_rows:
            return

        face_rows = conn.execute(
            "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h"
            " FROM faces"
            " WHERE photo_path=? AND person_id IS NULL AND ignored=0",
            (photo_path,),
        ).fetchall()
        if not face_rows:
            return

        used_face_ids: set[int] = set()
        for ann_id, ax, ay, aw, ah, person_id in ann_rows:
            best_score = -1.0
            best_face  = None
            for face_id, fx, fy, fw, fh in face_rows:
                if face_id in used_face_ids:
                    continue
                # Critère 1 : centre du visage ArcFace dans la région Picasa
                cx, cy = fx + fw // 2, fy + fh // 2
                if ax <= cx <= ax + aw and ay <= cy <= ay + ah:
                    # Score = surface de recouvrement / surface du visage (favorise le meilleur)
                    iou_score = _iou((ax, ay, aw, ah), (fx, fy, fw, fh))
                    score = 1.0 + iou_score  # > 1 pour toujours primer sur le fallback IoU
                else:
                    # Critère 2 (fallback) : IoU classique
                    score = _iou((ax, ay, aw, ah), (fx, fy, fw, fh))
                    if score < _IOU_THRESHOLD:
                        continue
                if score > best_score:
                    best_score = score
                    best_face  = face_id
            if best_face is not None:
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE id=?", (person_id, best_face)
                )
                conn.execute(
                    "UPDATE picasa_annotations SET consumed=1 WHERE id=?", (ann_id,)
                )
                used_face_ids.add(best_face)
                logger.debug(
                    "Picasa: visage %d → person %d (score=%.2f) dans %s",
                    best_face, person_id, best_score, os.path.basename(photo_path),
                )

    def reset_clustering(self) -> None:
        """Efface uniquement les cluster_id (regroupements HDBSCAN).
        Les embeddings, person_id et l'index des photos sont conservés :
        aucune re-détection n'est nécessaire.
        Après re-clustering, update_clusters propage les person_id existants
        aux nouvelles faces du même groupe — les associations sont ainsi
        largement reconstituées sans réimport Picasa."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET cluster_id=NULL"
                    " WHERE pinned IS NULL OR pinned=0"
                )
                conn.commit()
            finally:
                conn.close()

    def reset_index(self) -> None:
        """Efface toutes les détections et l'index des photos analysées.
        Les personnes nommées et les annotations Picasa sont conservées ;
        les annotations sont réinitialisées pour être ré-appliquées après
        la prochaine détection."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM faces")
                conn.execute("DELETE FROM indexed_photos")
                conn.execute("UPDATE picasa_annotations SET consumed=0")
                conn.commit()
            finally:
                conn.close()

    def delete_for_path(self, photo_path: str) -> None:
        """Remove all face data for a deleted photo."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM faces WHERE photo_path=?", (photo_path,))
                conn.execute(
                    "DELETE FROM indexed_photos WHERE photo_path=?", (photo_path,)
                )
                conn.execute(
                    "DELETE FROM picasa_annotations WHERE photo_path=?", (photo_path,)
                )
                conn.commit()
            finally:
                conn.close()

    def update_path(self, old_path: str, new_path: str) -> None:
        """Rename/move a single photo: update photo_path in both tables."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET photo_path=? WHERE photo_path=?",
                    (new_path, old_path),
                )
                conn.execute(
                    "UPDATE indexed_photos SET photo_path=? WHERE photo_path=?",
                    (new_path, old_path),
                )
                conn.execute(
                    "UPDATE picasa_annotations SET photo_path=? WHERE photo_path=?",
                    (new_path, old_path),
                )
                conn.commit()
            finally:
                conn.close()

    def update_paths_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Rename/move a folder: rewrite every path that starts with old_prefix."""
        old_prefix = os.path.normpath(old_prefix)
        new_prefix = os.path.normpath(new_prefix)
        n = len(old_prefix)
        # os.sep is '\\' on Windows — not a wildcard in SQLite LIKE, so safe as literal
        like_pattern = old_prefix + os.sep + "%"
        with self._lock:
            conn = self._conn()
            try:
                for table in ("faces", "indexed_photos"):
                    conn.execute(
                        f"UPDATE {table}"
                        "  SET photo_path = ? || substr(photo_path, ?)"
                        " WHERE photo_path = ? OR photo_path LIKE ?",
                        (new_prefix, n + 1, old_prefix, like_pattern),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_stats(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM indexed_photos"
                ).fetchone()[0]
                faces = conn.execute(
                    "SELECT COUNT(*) FROM faces"
                ).fetchone()[0]
                persons = conn.execute(
                    "SELECT COUNT(DISTINCT person_id) FROM faces"
                    " WHERE person_id IS NOT NULL"
                ).fetchone()[0]
                clusters = conn.execute(
                    "SELECT COUNT(DISTINCT cluster_id) FROM faces"
                    " WHERE cluster_id IS NOT NULL"
                ).fetchone()[0]
            finally:
                conn.close()
        return {
            "indexed_photos": indexed,
            "total_faces": faces,
            "named_persons": persons,
            "clusters": clusters,
        }
