# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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


_SIM_SUGGEST = 0.50  # seuil minimum pour proposer une suggestion après dé-association


def _enc(embedding: list[float]) -> bytes:
    return struct.pack(f"{len(embedding)}f", *embedding)


def _dec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_sim(a: list[float], b: list[float]) -> float:
    try:
        import numpy as np
        va = np.array(a, dtype=np.float32)
        vb = np.array(b, dtype=np.float32)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        return float(np.dot(va, vb) / denom) if denom > 1e-8 else 0.0
    except ImportError:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0


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
                    "CREATE INDEX IF NOT EXISTS idx_faces_cluster_person"
                    " ON faces(cluster_id, person_id)"
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
                if "suggestion_person_id" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN suggestion_person_id INTEGER DEFAULT NULL"
                    )
                if "suggestion_score" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN suggestion_score REAL DEFAULT NULL"
                    )
                if "det_score" not in cols:
                    conn.execute(
                        "ALTER TABLE faces ADD COLUMN det_score REAL DEFAULT 1.0"
                    )
                # Migration indexed_photos
                ip_cols = {r[1] for r in conn.execute("PRAGMA table_info(indexed_photos)")}
                if "rotation" not in ip_cols:
                    conn.execute(
                        "ALTER TABLE indexed_photos ADD COLUMN rotation INTEGER DEFAULT 0"
                    )
                # Migration : supprimer les visages avec bbox corrompues (stockées en BLOB
                # au lieu d'INTEGER par une version antérieure du code).  Les photos
                # concernées sont supprimées de indexed_photos pour être re-analysées.
                bad_paths = conn.execute(
                    "SELECT DISTINCT photo_path FROM faces"
                    " WHERE typeof(bbox_x)='blob' OR typeof(bbox_y)='blob'"
                    "    OR typeof(bbox_w)='blob' OR typeof(bbox_h)='blob'"
                ).fetchall()
                if bad_paths:
                    placeholders = ",".join("?" * len(bad_paths))
                    bad_list = [r[0] for r in bad_paths]
                    conn.execute(
                        f"DELETE FROM faces WHERE photo_path IN ({placeholders})",
                        bad_list,
                    )
                    conn.execute(
                        f"DELETE FROM indexed_photos WHERE photo_path IN ({placeholders})",
                        bad_list,
                    )
                    logger.warning(
                        "Migration: %d photo(s) avec bbox corrompues supprimées "
                        "et marquées pour re-indexation",
                        len(bad_paths),
                    )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ indexing

    def get_paths_to_index(self, all_paths: list[str]) -> list[str]:
        """Returns paths from all_paths that have not been indexed yet.
        Les fichiers vidéo sont toujours exclus (pas de détection de visages)."""
        if not all_paths:
            return []
        from src.library.exif_reader import VIDEO_EXT
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT photo_path FROM indexed_photos"
                ).fetchall()
            finally:
                conn.close()
        indexed = {r[0] for r in rows}
        return [
            p for p in all_paths
            if os.path.normpath(p) not in indexed
            and os.path.splitext(p)[1].lower() not in VIDEO_EXT
        ]

    # Seuils d'auto-ignorance pour les faces de mauvaise qualité.
    # En dessous de ces valeurs, la face est sauvegardée avec ignored=1 :
    # elle reste visible dans l'interface mais ne participe pas au clustering.
    _AUTO_IGNORE_MIN_SIDE_RATIO    = 0.03  # 3 % — seuil de base (aucun visage au premier plan)
    _AUTO_IGNORE_MIN_SIDE_FG_RATIO = 0.20  # 20 % — seuil pour qualifier un visage de "grand"
    _AUTO_IGNORE_MIN_SIDE_ABS      = 22    # plancher absolu (px) pour les très petites images
    _AUTO_IGNORE_MIN_SIDE          = 121   # fallback si les dimensions sont illisibles
    _AUTO_IGNORE_MIN_SCORE         = 0.65  # score de détection InsightFace (0–1)

    def save_faces(self, photo_path: str, detections: list[dict], rotation: int = 0) -> None:
        """
        Persist detected faces for a photo.
        detections: list of {'bbox': (x,y,w,h), 'embedding': list[float], 'det_score': float}
        rotation: CW degrees applied during detection (stored to reconstruct face crops).
        Faces with min(w,h) < effective threshold or det_score < _AUTO_IGNORE_MIN_SCORE
        are saved with ignored=1. The size threshold is proportional to the image:
        base = max(ABS, short_side * 3 %) ; fg = max(base*2, short_side * 20 %).
        If any face reaches fg, the effective threshold is fg (small faces ignored).
        Otherwise (all faces small — old scanned photo, distant group) base is used.
        Fallback to _AUTO_IGNORE_MIN_SIDE if image dimensions cannot be read.
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
                # Seuils proportionnels à la résolution de l'image.
                # Si un visage atteint le seuil "premier plan" (12 %), on relève la
                # barre pour tous : les petits visages sont alors ignorés.
                # Sinon (vieille photo scannée, groupe distant…), on utilise le
                # seuil de base (3 %) pour ne pas perdre de visages légitimes.
                try:
                    from PIL import Image as _PILImage
                    with _PILImage.open(photo_path) as _img:
                        _iw, _ih = _img.size
                    _shortest = min(_iw, _ih)
                    _base_threshold = max(
                        self._AUTO_IGNORE_MIN_SIDE_ABS,
                        int(_shortest * self._AUTO_IGNORE_MIN_SIDE_RATIO),
                    )
                    _fg_threshold = max(
                        _base_threshold * 2,
                        int(_shortest * self._AUTO_IGNORE_MIN_SIDE_FG_RATIO),
                    )
                except Exception:
                    _base_threshold = self._AUTO_IGNORE_MIN_SIDE
                    _fg_threshold   = self._AUTO_IGNORE_MIN_SIDE
                has_foreground = any(
                    min(int(d["bbox"][2]), int(d["bbox"][3])) >= _fg_threshold
                    for d in detections
                )
                effective_min_side = _fg_threshold if has_foreground else _base_threshold
                for det in detections:
                    x, y, w, h = (int(v) for v in det["bbox"])
                    emb = det.get("embedding")
                    blob = _enc(emb) if emb else None
                    score = det.get("det_score", 1.0)
                    low_quality = (
                        min(w, h) < effective_min_side
                        or score < self._AUTO_IGNORE_MIN_SCORE
                    )
                    conn.execute(
                        "INSERT INTO faces"
                        " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                        "  embedding, ignored, det_score)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (photo_path, x, y, w, h, blob,
                         1 if low_quality else 0, score),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO indexed_photos"
                    " (photo_path, indexed_at, face_count, rotation) VALUES (?,?,?,?)",
                    (photo_path, time.time(), len(detections), rotation),
                )
                # Appliquer les annotations Picasa en attente (si présentes)
                self._apply_picasa_annotations(conn, photo_path)
                # Consommer les annotations dont la personne est déjà portée par une face
                # InsightFace (avec embedding) sur cette photo — évite les placeholders doublons
                # si l'annotation n'a pas pu être appariée par bbox (tailles trop différentes)
                # mais que la personne est quand même identifiée sur la photo.
                conn.execute(
                    "UPDATE picasa_annotations SET consumed=1"
                    " WHERE photo_path=? AND consumed=0"
                    "   AND person_id IN ("
                    "     SELECT DISTINCT person_id FROM faces"
                    "     WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL"
                    "   )",
                    (photo_path, photo_path),
                )
                # Créer des placeholders pour les annotations non appariées à aucun visage
                # InsightFace (face non détectée : pose, qualité, score trop bas…).
                # Sans ça, le placeholder créé au moment de l'import Picasa est supprimé
                # par le DELETE ci-dessus et n'est jamais recréé — la personne disparaît.
                still_pending = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_w, bbox_h, person_id"
                    " FROM picasa_annotations"
                    " WHERE photo_path=? AND consumed=0",
                    (photo_path,),
                ).fetchall()
                for bx, by, bw, bh, pid in still_pending:
                    conn.execute(
                        "INSERT INTO faces"
                        " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, person_id)"
                        " VALUES (?,?,?,?,?,?)",
                        (photo_path, bx, by, bw, bh, pid),
                    )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ clustering

    def count_embeddings(self) -> int:
        """Nombre total de faces avec embedding (non épinglées)."""
        with self._lock:
            conn = self._conn()
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM faces"
                    " WHERE embedding IS NOT NULL"
                    "   AND (pinned IS NULL OR pinned = 0)"
                ).fetchone()[0]
            finally:
                conn.close()

    def count_identified_faces(self) -> int:
        """Nombre de faces avec embedding ET person_id assigné (non épinglées)."""
        with self._lock:
            conn = self._conn()
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM faces"
                    " WHERE embedding IS NOT NULL"
                    "   AND (pinned IS NULL OR pinned = 0)"
                    "   AND person_id IS NOT NULL"
                ).fetchone()[0]
            finally:
                conn.close()

    def get_all_embeddings(
        self,
        only_unidentified: bool = False,
    ) -> tuple["np.ndarray", list[int]]:
        """Returns (embeddings, face_ids) for non-pinned faces with stored embeddings.

        only_unidentified=True : n'inclut que les faces sans person_id, pour que
        HDBSCAN ne tourne que sur les visages non encore identifiés (~20 % de moins).

        embeddings is a float32 ndarray of shape (N, D) built directly from the
        binary blobs — avoids creating N×D Python float objects as an intermediate.
        """
        import numpy as np
        extra = " AND person_id IS NULL" if only_unidentified else ""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, embedding FROM faces"
                    " WHERE embedding IS NOT NULL"
                    "   AND (ignored IS NULL OR ignored = 0)"
                    f"   AND (pinned IS NULL OR pinned = 0){extra}"
                ).fetchall()
            finally:
                conn.close()
        if not rows:
            return np.empty((0, 0), dtype=np.float32), []
        face_ids = [r[0] for r in rows]
        embeddings = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return embeddings, face_ids

    def update_clusters(
        self,
        face_ids: list[int],
        labels: list[int],
        progress_cb=None,
    ) -> None:
        if not face_ids:
            return
        _CHUNK = 10_000
        pairs = [
            (int(label) if label >= 0 else None, fid)
            for fid, label in zip(face_ids, labels)
        ]
        total = len(pairs)
        with self._lock:
            conn = self._conn()
            try:
                # Réinitialise uniquement les faces non identifiées.
                # Les faces avec person_id gardent leur cluster synthétique (10M+)
                # et restent visibles dans PersonClusterView pendant le clustering.
                # Les suggestions en attente sont également invalidées car les cluster_ids changent.
                conn.execute(
                    "UPDATE faces SET cluster_id=NULL, suggestion_person_id=NULL, suggestion_score=NULL"
                    " WHERE (pinned IS NULL OR pinned = 0)"
                    "   AND person_id IS NULL"
                )
                for start in range(0, total, _CHUNK):
                    chunk = pairs[start:start + _CHUNK]
                    conn.executemany("UPDATE faces SET cluster_id=? WHERE id=?", chunk)
                    if progress_cb:
                        done = min(start + _CHUNK, total)
                        progress_cb(f"Clustering : sauvegarde {done:,}/{total:,} visages…".replace(",", " "))
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
                # Après propagation, dédupliquer sur toutes les photos concernées.
                self._dedup_in_transaction(conn)
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
        not ignored, not pinned and not pending verification."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, COUNT(*) FROM faces"
                    " WHERE cluster_id IS NOT NULL"
                    "   AND person_id IS NULL"
                    "   AND suggestion_person_id IS NULL"
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

    # ------------------------------------------------------------------ pending suggestions

    def set_cluster_suggestions(self, suggestions: "dict[int, tuple[int, float]]") -> None:
        """Batch-set suggestion_person_id/score for multiple clusters.

        suggestions: {cluster_id: (person_id, score)}
        Only sets suggestions for clusters that don't already have one (idempotent).
        """
        if not suggestions:
            return
        with self._lock:
            conn = self._conn()
            try:
                for cluster_id, (person_id, score) in suggestions.items():
                    conn.execute(
                        "UPDATE faces SET suggestion_person_id=?, suggestion_score=?"
                        " WHERE cluster_id=?"
                        "   AND suggestion_person_id IS NULL",
                        (person_id, score, cluster_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def clear_cluster_suggestion(self, cluster_id: int) -> None:
        """Clear suggestion (reject). The cluster returns to the unnamed list."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET suggestion_person_id=NULL, suggestion_score=NULL"
                    " WHERE cluster_id=?",
                    (cluster_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def resuggest_clusters(
        self, cluster_ids: "list[int]", exclude_person_id: "int | None" = None
    ) -> None:
        """Vide les suggestions des clusters donnés et recalcule la meilleure personne pour chacun.

        Appelé après un rejet pour que les faces isolées puissent être proposées
        à une autre personne (hors exclude_person_id).
        """
        if not cluster_ids:
            return

        # 1. Vider les suggestions et récupérer les embeddings par cluster
        cid_to_embs: "dict[int, list]" = {}
        with self._lock:
            conn = self._conn()
            try:
                placeholders = ",".join("?" * len(cluster_ids))
                conn.execute(
                    f"UPDATE faces SET suggestion_person_id=NULL, suggestion_score=NULL"
                    f" WHERE cluster_id IN ({placeholders})",
                    cluster_ids,
                )
                conn.commit()
                for cid in cluster_ids:
                    rows = conn.execute(
                        "SELECT embedding FROM faces WHERE cluster_id=? AND embedding IS NOT NULL",
                        (cid,),
                    ).fetchall()
                    embs = [_dec(r[0]) for r in rows]
                    if embs:
                        cid_to_embs[cid] = embs
            finally:
                conn.close()

        if not cid_to_embs:
            return

        # 2. Charger les embeddings de toutes les personnes (hors exclu)
        by_person: "dict[int, list]" = {}
        with self._lock:
            conn = self._conn()
            try:
                if exclude_person_id is not None:
                    pers_rows = conn.execute(
                        "SELECT person_id, embedding FROM faces"
                        " WHERE person_id IS NOT NULL AND person_id != ?"
                        "   AND embedding IS NOT NULL",
                        (exclude_person_id,),
                    ).fetchall()
                else:
                    pers_rows = conn.execute(
                        "SELECT person_id, embedding FROM faces"
                        " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
                    ).fetchall()
            finally:
                conn.close()

        for pid, blob in pers_rows:
            by_person.setdefault(pid, []).append(_dec(blob))

        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return

        # 3. Pour chaque cluster, calculer le centroid et trouver la meilleure personne
        suggestions: "dict[int, tuple[int, float]]" = {}
        for cid, face_embs in cid_to_embs.items():
            cluster_centroid = _centroid(face_embs)
            best_sim, best_pid = 0.0, None
            for pid, centroid in person_centroids.items():
                sim = _cosine_sim(cluster_centroid, centroid)
                if sim > best_sim:
                    best_sim, best_pid = sim, pid
            if best_pid is not None and best_sim >= _SIM_SUGGEST:
                suggestions[cid] = (best_pid, best_sim)

        if suggestions:
            self.set_cluster_suggestions(suggestions)

    def accept_cluster_suggestion(self, cluster_id: int) -> None:
        """Confirm a pending suggestion: assign the suggested person and clear the flag."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT DISTINCT suggestion_person_id FROM faces"
                    " WHERE cluster_id=? AND suggestion_person_id IS NOT NULL LIMIT 1",
                    (cluster_id,),
                ).fetchone()
                if row is None:
                    return
                person_id = row[0]
                paths = [r[0] for r in conn.execute(
                    "SELECT DISTINCT photo_path FROM faces WHERE cluster_id=?", (cluster_id,)
                ).fetchall()]
                conn.execute(
                    "UPDATE faces SET person_id=?, suggestion_person_id=NULL, suggestion_score=NULL"
                    " WHERE cluster_id=?",
                    (person_id, cluster_id),
                )
                self._dedup_in_transaction(conn, paths)
                conn.commit()
            finally:
                conn.close()

    def get_suggested_clusters_for_person(
        self, person_id: int
    ) -> "list[tuple[int, int, float]]":
        """Returns [(cluster_id, face_count, score)] for clusters pending verification
        for this person, ordered by score descending."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, COUNT(*), MAX(suggestion_score)"
                    " FROM faces"
                    " WHERE suggestion_person_id=?"
                    "   AND person_id IS NULL"
                    "   AND ignored=0"
                    " GROUP BY cluster_id"
                    " ORDER BY MAX(suggestion_score) DESC",
                    (person_id,),
                ).fetchall()
            finally:
                conn.close()
        return [(r[0], r[1], r[2] or 0.0) for r in rows]

    def get_persons_pending_count(self) -> "dict[int, int]":
        """Returns {person_id: pending_cluster_count} for all persons with suggestions."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT suggestion_person_id, COUNT(DISTINCT cluster_id)"
                    " FROM faces"
                    " WHERE suggestion_person_id IS NOT NULL AND person_id IS NULL"
                    " GROUP BY suggestion_person_id"
                ).fetchall()
            finally:
                conn.close()
        return {r[0]: r[1] for r in rows}

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

    def get_face_by_id(self, face_id: int) -> Optional[FaceInfo]:
        """Returns FaceInfo for a single face_id, or None if not found."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "       f.cluster_id, f.person_id,"
                    "       CASE WHEN f.embedding IS NULL THEN 0"
                    "            ELSE COALESCE(ip.rotation, 0) END"
                    " FROM faces f"
                    " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                    " WHERE f.id=?",
                    (face_id,),
                ).fetchone()
            finally:
                conn.close()
        if row:
            return FaceInfo(
                id=row[0], photo_path=row[1],
                bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                cluster_id=row[6], person_id=row[7],
                detected_rotation=row[8],
            )
        return None

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
        _CHUNK = 500
        all_rows = []
        with self._lock:
            conn = self._conn()
            try:
                for i in range(0, len(cluster_ids), _CHUNK):
                    chunk = cluster_ids[i:i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    all_rows.extend(conn.execute(
                        f"SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                        f"       f.cluster_id, f.person_id, f.is_cover,"
                        f"       (f.bbox_w * f.bbox_h) AS area,"
                        f"       CASE WHEN f.embedding IS NULL THEN 0"
                        f"            ELSE COALESCE(ip.rotation, 0) END AS detected_rotation"
                        f" FROM faces f"
                        f" LEFT JOIN indexed_photos ip ON ip.photo_path = f.photo_path"
                        f" WHERE f.cluster_id IN ({ph}) AND f.ignored = 0"
                        f" ORDER BY f.cluster_id, f.is_cover DESC, area DESC",
                        chunk,
                    ).fetchall())
            finally:
                conn.close()
        result: dict[int, FaceInfo] = {}
        for row in all_rows:
            cid = row[6]
            if cid not in result:  # première ligne = meilleure (cover ou plus grande bbox)
                result[cid] = FaceInfo(
                    id=row[0], photo_path=row[1],
                    bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                    cluster_id=cid, person_id=row[7],
                    detected_rotation=row[10],
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
                    "       CASE WHEN f.embedding IS NULL THEN 0"
                    "            ELSE COALESCE(ip.rotation, 0) END"
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
        """Returns distinct photo paths for a cluster (non-ignored faces only)."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT photo_path FROM faces"
                    " WHERE cluster_id=? AND ignored=0",
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

    def get_faces_for_person(self, person_id: int) -> list["FaceInfo"]:
        """Returns all FaceInfo for a person, ordered by photo then bbox position."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "       f.cluster_id, f.person_id, f.ignored, f.pinned,"
                    "       CASE WHEN f.embedding IS NULL THEN 0"
                    "            ELSE COALESCE(ip.rotation, 0) END"
                    " FROM faces f"
                    " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                    " WHERE f.person_id=?"
                    " ORDER BY f.photo_path, f.bbox_x",
                    (person_id,),
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

    def get_faces_by_cluster(self, cluster_id: int) -> "list[FaceInfo]":
        """Returns all FaceInfo for a given cluster_id."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "       f.cluster_id, f.person_id, f.ignored, f.pinned,"
                    "       CASE WHEN f.embedding IS NULL THEN 0"
                    "            ELSE COALESCE(ip.rotation, 0) END"
                    " FROM faces f"
                    " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                    " WHERE f.cluster_id=?"
                    " ORDER BY f.photo_path, f.bbox_x",
                    (cluster_id,),
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

    # ------------------------------------------------------------------ assignment

    @staticmethod
    def _dedup_in_transaction(conn, photo_paths: "list[str] | None" = None) -> None:
        """Ignore les visages redondants (même personne, même photo) dans la transaction active.

        Pour chaque (photo_path, person_id) avec plusieurs faces non-ignorées, garde celle
        dont l'aire bbox est la plus grande (= visage le plus prominent, le plus fiable)
        et marque les autres ignored=1.

        photo_paths : si fourni, limite la dédupplication à ces photos seulement.
        """
        if photo_paths is not None:
            if not photo_paths:
                return
            ph = ",".join("?" * len(photo_paths))
            sql = f"""
                UPDATE faces SET ignored=1
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY photo_path, person_id
                                   ORDER BY bbox_w * bbox_h DESC
                               ) AS rn
                        FROM faces
                        WHERE person_id IS NOT NULL AND ignored=0
                          AND photo_path IN ({ph})
                    )
                    WHERE rn > 1
                )
            """
            conn.execute(sql, photo_paths)
        else:
            conn.execute("""
                UPDATE faces SET ignored=1
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY photo_path, person_id
                                   ORDER BY bbox_w * bbox_h DESC
                               ) AS rn
                        FROM faces
                        WHERE person_id IS NOT NULL AND ignored=0
                    )
                    WHERE rn > 1
                )
            """)

    def assign_person_to_face(self, face_id: int, person_id: int) -> None:
        """Assign a named person to a single face."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT photo_path FROM faces WHERE id=?", (face_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id)
                )
                if row:
                    self._dedup_in_transaction(conn, [row[0]])
                conn.commit()
            finally:
                conn.close()

    def assign_person_to_faces(self, face_ids: list[int], person_id: int) -> None:
        """Assign a named person to multiple faces in a single transaction."""
        if not face_ids:
            return
        with self._lock:
            conn = self._conn()
            try:
                ph = ",".join("?" * len(face_ids))
                paths = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT photo_path FROM faces WHERE id IN ({ph})", face_ids
                ).fetchall()]
                conn.executemany(
                    "UPDATE faces SET person_id=? WHERE id=?",
                    [(person_id, fid) for fid in face_ids],
                )
                self._dedup_in_transaction(conn, paths)
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

    def isolate_and_assign_face(self, face_id: int, person_id: int) -> None:
        """Sépare un visage de son groupe et l'assigne à une personne en une transaction.
        Résultat : pinned=1, cluster_id négatif unique, person_id=person_id."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
                ).fetchone()
                min_pinned = row[0] if row and row[0] is not None else 0
                new_cluster_id = min(min_pinned, 0) - 1
                path_row = conn.execute(
                    "SELECT photo_path FROM faces WHERE id=?", (face_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE faces SET cluster_id=?, pinned=1, person_id=?"
                    " WHERE id=?",
                    (new_cluster_id, person_id, face_id),
                )
                if path_row:
                    self._dedup_in_transaction(conn, [path_row[0]])
                conn.commit()
            finally:
                conn.close()

    def recalculate_size_ignored(
        self, progress_cb=None
    ) -> tuple[int, int]:
        """Re-evaluate faces auto-ignored by size using the current proportional threshold.

        Only candidates with ignored=1, embedding IS NOT NULL, and det_score >= threshold
        are reconsidered — manually-ignored faces (user ✕ action) that lack a det_score
        or score too low are left untouched.
        Does NOT re-run InsightFace detection.
        Returns (unignored_count, photos_evaluated).
        """
        from PIL import Image as _PILImage

        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT photo_path FROM faces"
                    " WHERE ignored=1 AND embedding IS NOT NULL"
                    "   AND (det_score IS NULL OR det_score >= ?)",
                    (self._AUTO_IGNORE_MIN_SCORE,),
                ).fetchall()
            finally:
                conn.close()

        photos = [r[0] for r in rows]
        total = len(photos)
        unignored = 0

        for i, photo_path in enumerate(photos):
            if progress_cb:
                progress_cb(i, total)
            if not os.path.exists(photo_path):
                continue

            # Proportional thresholds (read only the image header)
            try:
                with _PILImage.open(photo_path) as _img:
                    _iw, _ih = _img.size
                _shortest = min(_iw, _ih)
                _base = max(
                    self._AUTO_IGNORE_MIN_SIDE_ABS,
                    int(_shortest * self._AUTO_IGNORE_MIN_SIDE_RATIO),
                )
                _fg = max(
                    _base * 2,
                    int(_shortest * self._AUTO_IGNORE_MIN_SIDE_FG_RATIO),
                )
            except Exception:
                _base = self._AUTO_IGNORE_MIN_SIDE
                _fg   = self._AUTO_IGNORE_MIN_SIDE

            with self._lock:
                conn = self._conn()
                try:
                    all_faces = conn.execute(
                        "SELECT id, bbox_w, bbox_h, ignored, det_score, embedding"
                        " FROM faces WHERE photo_path=?",
                        (photo_path,),
                    ).fetchall()

                    has_foreground = any(
                        min(r[1], r[2]) >= _fg for r in all_faces
                    )
                    effective = _fg if has_foreground else _base

                    for fid, bw, bh, is_ignored, score, emb in all_faces:
                        if (is_ignored == 1
                                and emb is not None
                                and (score is None or score >= self._AUTO_IGNORE_MIN_SCORE)
                                and min(bw, bh) >= effective):
                            conn.execute(
                                "UPDATE faces SET ignored=0 WHERE id=?", (fid,)
                            )
                            unignored += 1
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "recalculate_size_ignored: erreur %s : %s", photo_path, exc
                    )
                finally:
                    conn.close()

        if progress_cb:
            progress_cb(total, total)
        return unignored, total

    def find_similar_to_persons(
        self, progress_cb: "Callable[[int, int], None] | None" = None
    ) -> tuple[int, int]:
        """Compare chaque cluster non identifié aux centroïdes des personnes nommées.

        Pour chaque cluster sans person_id ni suggestion existante, calcule son centroïde
        et le compare à tous les centroïdes de personnes nommées. Si la similarité cosinus
        atteint _SIM_SUGGEST (0.50), une suggestion est créée et apparaîtra dans la section
        « En attente » de la vue de la personne concernée.

        Retourne (suggestions_créées, clusters_vérifiés).
        """
        # 1. Tous les embeddings de clusters non identifiés sans suggestion existante
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT cluster_id, embedding FROM faces"
                    " WHERE cluster_id IS NOT NULL"
                    "   AND person_id IS NULL"
                    "   AND suggestion_person_id IS NULL"
                    "   AND ignored = 0"
                    "   AND embedding IS NOT NULL"
                ).fetchall()
            finally:
                conn.close()

        if not rows:
            return 0, 0

        cid_to_embs: "dict[int, list]" = {}
        for cid, blob in rows:
            cid_to_embs.setdefault(cid, []).append(_dec(blob))

        total = len(cid_to_embs)

        # 2. Centroïdes de toutes les personnes nommées
        with self._lock:
            conn = self._conn()
            try:
                pers_rows = conn.execute(
                    "SELECT person_id, embedding FROM faces"
                    " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
                ).fetchall()
            finally:
                conn.close()

        by_person: "dict[int, list]" = {}
        for pid, blob in pers_rows:
            by_person.setdefault(pid, []).append(_dec(blob))
        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return 0, total

        # 3. Pour chaque cluster, trouver la meilleure personne
        suggestions: "dict[int, tuple[int, float]]" = {}
        for i, (cid, face_embs) in enumerate(cid_to_embs.items()):
            if progress_cb:
                progress_cb(i + 1, total)
            centroid = _centroid(face_embs)
            best_sim, best_pid = 0.0, None
            for pid, pc in person_centroids.items():
                sim = _cosine_sim(centroid, pc)
                if sim > best_sim:
                    best_sim, best_pid = sim, pid
            if best_pid is not None and best_sim >= _SIM_SUGGEST:
                suggestions[cid] = (best_pid, best_sim)

        if suggestions:
            self.set_cluster_suggestions(suggestions)

        return len(suggestions), total

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

    def unignore_face(self, face_id: int) -> None:
        """Restore a previously ignored face."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE faces SET ignored=0 WHERE id=?", (face_id,))
                conn.commit()
            finally:
                conn.close()

    def unassign_person_from_face(self, face_id: int) -> None:
        """Clear person_id from a single face without touching cluster or pinned."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE faces SET person_id=NULL WHERE id=?", (face_id,))
                conn.commit()
            finally:
                conn.close()

    def isolate_and_suggest(
        self, face_ids: list[int], exclude_person_id: "int | None" = None
    ) -> None:
        """Isole chaque visage dans un cluster négatif unique (pinned=1, person_id=NULL)
        et calcule une suggestion par similarité cosinus contre toutes les personnes connues,
        en excluant optionnellement exclude_person_id (la personne qu'on vient de quitter).
        Si le meilleur match atteint _SIM_SUGGEST, suggestion_person_id est enregistré."""
        if not face_ids:
            return

        # 1. Isoler chaque visage et récupérer son embedding
        face_embs: dict[int, list[float]] = {}  # new_cluster_id → embedding
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
                ).fetchone()
                next_cid = (min(row[0], 0) - 1) if row and row[0] is not None else -1

                for face_id in face_ids:
                    cid = next_cid
                    next_cid -= 1
                    conn.execute(
                        "UPDATE faces SET cluster_id=?, pinned=1, person_id=NULL,"
                        " suggestion_person_id=NULL, suggestion_score=NULL WHERE id=?",
                        (cid, face_id),
                    )
                    emb_row = conn.execute(
                        "SELECT embedding FROM faces WHERE id=?", (face_id,)
                    ).fetchone()
                    if emb_row and emb_row[0]:
                        face_embs[cid] = _dec(emb_row[0])
                conn.commit()
            finally:
                conn.close()

        if not face_embs:
            return

        # 2. Récupérer les embeddings de toutes les personnes (hors exclude_person_id)
        by_person: dict[int, list] = {}
        with self._lock:
            conn = self._conn()
            try:
                if exclude_person_id is not None:
                    rows = conn.execute(
                        "SELECT person_id, embedding FROM faces"
                        " WHERE person_id IS NOT NULL AND person_id != ?"
                        "   AND embedding IS NOT NULL",
                        (exclude_person_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT person_id, embedding FROM faces"
                        " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
                    ).fetchall()
            finally:
                conn.close()

        for pid, blob in rows:
            by_person.setdefault(pid, []).append(_dec(blob))

        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return

        # 3. Pour chaque visage isolé, chercher la personne la plus similaire
        suggestions: dict[int, tuple[int, float]] = {}
        for cid, face_emb in face_embs.items():
            best_sim, best_pid = 0.0, None
            for pid, centroid in person_centroids.items():
                sim = _cosine_sim(face_emb, centroid)
                if sim > best_sim:
                    best_sim, best_pid = sim, pid
            if best_pid is not None and best_sim >= _SIM_SUGGEST:
                suggestions[cid] = (best_pid, best_sim)

        if suggestions:
            self.set_cluster_suggestions(suggestions)

    def get_ignored_faces_for_photo(self, photo_path: str) -> list:
        """Return all FaceInfo with ignored=True for this photo."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "       f.cluster_id, f.person_id, f.ignored, f.pinned,"
                    "       CASE WHEN f.embedding IS NULL THEN 0"
                    "            ELSE COALESCE(ip.rotation, 0) END"
                    " FROM faces f"
                    " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                    " WHERE f.photo_path=? AND f.ignored=1",
                    (photo_path,),
                ).fetchall()
            finally:
                conn.close()
        from src.core.models import FaceInfo
        return [
            FaceInfo(
                id=r[0], photo_path=r[1],
                bbox_x=r[2], bbox_y=r[3], bbox_w=r[4], bbox_h=r[5],
                cluster_id=r[6], person_id=r[7],
                ignored=True, pinned=bool(r[9]),
                detected_rotation=r[10],
            )
            for r in rows
        ]

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
                paths = [r[0] for r in conn.execute(
                    "SELECT DISTINCT photo_path FROM faces WHERE cluster_id=?", (cluster_id,)
                ).fetchall()]
                conn.execute(
                    "UPDATE faces SET person_id=? WHERE cluster_id=?",
                    (person_id, cluster_id),
                )
                self._dedup_in_transaction(conn, paths)
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

    def get_person_photo_count(self, person_id: int) -> int:
        """Count distinct photos where person_id has a face. Fast single query."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT photo_path) FROM faces"
                    " WHERE person_id=? AND cluster_id IS NOT NULL",
                    (person_id,),
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else 0

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
                    "  SELECT f.person_id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                    "         CASE WHEN f.embedding IS NULL THEN 0"
                    "              ELSE COALESCE(ip.rotation, 0) END AS detected_rotation,"
                    "         ROW_NUMBER() OVER ("
                    "           PARTITION BY f.person_id"
                    "           ORDER BY f.is_cover DESC, f.bbox_w * f.bbox_h DESC"
                    "         ) AS rn"
                    "  FROM faces f"
                    "  LEFT JOIN indexed_photos ip ON ip.photo_path = f.photo_path"
                    "  WHERE f.person_id IS NOT NULL"
                    ")"
                    " SELECT person_id, photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                    "        detected_rotation"
                    " FROM ranked WHERE rn = 1"
                ).fetchall()
            finally:
                conn.close()
        pending_counts = self.get_persons_pending_count()
        counts = {r[0]: r[1] for r in count_rows}
        reps = {r[0]: r[1:] for r in rep_rows}
        for p in persons:
            if p.id in counts:
                p.photo_count = counts[p.id]
                rep = reps.get(p.id)
                if rep:
                    p.cover_path = rep[0]
                    p.cover_bbox = (rep[1], rep[2], rep[3], rep[4])
                    p.cover_detected_rotation = int(rep[5] or 0)
            p.pending_count = pending_counts.get(p.id, 0)

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
                # Consommer les annotations dont la personne est déjà portée par une face
                # InsightFace (avec embedding) sur cette photo — évite les placeholders doublons.
                conn.execute(
                    "UPDATE picasa_annotations SET consumed=1"
                    " WHERE photo_path=? AND consumed=0"
                    "   AND person_id IN ("
                    "     SELECT DISTINCT person_id FROM faces"
                    "     WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL"
                    "   )",
                    (photo_path, photo_path),
                )
                # Consommer les annotations qui chevauchent spatialement une face ArcFace
                # — couvre le cas où Picasa et InsightFace identifient le même visage
                # physique sous des person_id différents (contacts Picasa ≠ cluster ArcFace).
                _arcface = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_w, bbox_h FROM faces"
                    " WHERE photo_path=? AND embedding IS NOT NULL",
                    (photo_path,),
                ).fetchall()
                if _arcface:
                    _pending = conn.execute(
                        "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h"
                        " FROM picasa_annotations"
                        " WHERE photo_path=? AND consumed=0",
                        (photo_path,),
                    ).fetchall()
                    for _aid, ax, ay, aw, ah in _pending:
                        _cx_p, _cy_p = ax + aw // 2, ay + ah // 2
                        for fx, fy, fw, fh in _arcface:
                            try:
                                fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
                            except (TypeError, ValueError):
                                continue
                            _cx_f, _cy_f = fx + fw // 2, fy + fh // 2
                            if (
                                (ax <= _cx_f <= ax + aw and ay <= _cy_f <= ay + ah)
                                or (fx <= _cx_p <= fx + fw and fy <= _cy_p <= fy + fh)
                                or _iou((ax, ay, aw, ah), (fx, fy, fw, fh)) > 0.3
                            ):
                                conn.execute(
                                    "UPDATE picasa_annotations SET consumed=1 WHERE id=?",
                                    (_aid,),
                                )
                                break
                # Supprimer les anciens placeholders Picasa (embedding IS NULL, non épinglés)
                # avant d'en créer de nouveaux — évite les doublons lors d'un re-import.
                conn.execute(
                    "DELETE FROM faces"
                    " WHERE photo_path=? AND embedding IS NULL AND (pinned IS NULL OR pinned=0)",
                    (photo_path,),
                )
                # Insérer des placeholders (sans embedding) pour les annotations non
                # consommées, que la photo ait été détectée ou non par InsightFace.
                # Couvre deux cas : (a) photo pas encore analysée — aucun visage ;
                # (b) InsightFace a détecté d'autres visages mais raté cette personne.
                # Les annotations restent non-consommées pour être ré-appariées lors
                # de la future analyse ArcFace (save_faces).
                still_pending = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_w, bbox_h, person_id"
                    " FROM picasa_annotations"
                    " WHERE photo_path=? AND consumed=0",
                    (photo_path,),
                ).fetchall()
                for bx, by, bw, bh, pid in still_pending:
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
                try:
                    fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
                except (TypeError, ValueError):
                    continue
                # Critère 1a : centre InsightFace dans la région Picasa
                cx_f, cy_f = fx + fw // 2, fy + fh // 2
                in_picasa = ax <= cx_f <= ax + aw and ay <= cy_f <= ay + ah
                # Critère 1b : centre Picasa dans la bbox InsightFace (symétrique)
                cx_p, cy_p = ax + aw // 2, ay + ah // 2
                in_face = fx <= cx_p <= fx + fw and fy <= cy_p <= fy + fh
                if in_picasa or in_face:
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
        """Efface les cluster_id HDBSCAN des faces non identifiées.
        Les faces avec person_id conservent leur cluster synthétique (10M+) :
        les personnes restent visibles dans PersonClusterView pendant/après le reset.
        Les embeddings et l'index des photos sont toujours conservés."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE faces SET cluster_id=NULL"
                    " WHERE (pinned IS NULL OR pinned=0)"
                    "   AND person_id IS NULL"
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup_overlapping_placeholders(self) -> int:
        """Supprime les faces placeholder (embedding IS NULL, non épinglées) qui chevauchent
        spatialement une face ArcFace (embedding IS NOT NULL) sur la même photo.

        Utile après un ré-import Picasa pour éliminer les doublons existants avant
        que le critère person_id ne les ait couverts (ex. : contacts Picasa ≠ cluster ArcFace).
        Retourne le nombre de faces supprimées."""
        deleted = 0
        with self._lock:
            conn = self._conn()
            try:
                photos = conn.execute(
                    "SELECT DISTINCT f1.photo_path FROM faces f1"
                    " JOIN faces f2 ON f1.photo_path = f2.photo_path"
                    " WHERE f1.embedding IS NOT NULL"
                    "   AND f2.embedding IS NULL AND (f2.pinned IS NULL OR f2.pinned=0)"
                ).fetchall()
                for (photo_path,) in photos:
                    af_rows = conn.execute(
                        "SELECT bbox_x, bbox_y, bbox_w, bbox_h FROM faces"
                        " WHERE photo_path=? AND embedding IS NOT NULL",
                        (photo_path,),
                    ).fetchall()
                    ph_rows = conn.execute(
                        "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h FROM faces"
                        " WHERE photo_path=? AND embedding IS NULL"
                        "   AND (pinned IS NULL OR pinned=0)",
                        (photo_path,),
                    ).fetchall()
                    for ph_id, ax, ay, aw, ah in ph_rows:
                        cx_p, cy_p = ax + aw // 2, ay + ah // 2
                        for fx, fy, fw, fh in af_rows:
                            try:
                                fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
                            except (TypeError, ValueError):
                                continue
                            cx_f, cy_f = fx + fw // 2, fy + fh // 2
                            if (
                                (ax <= cx_f <= ax + aw and ay <= cy_f <= ay + ah)
                                or (fx <= cx_p <= fx + fw and fy <= cy_p <= fy + fh)
                                or _iou((ax, ay, aw, ah), (fx, fy, fw, fh)) > 0.3
                            ):
                                conn.execute("DELETE FROM faces WHERE id=?", (ph_id,))
                                deleted += 1
                                break
                if deleted:
                    conn.commit()
            finally:
                conn.close()
        if deleted:
            logger.info(
                "cleanup_overlapping_placeholders: %d placeholder(s) supprimé(s)", deleted
            )
        return deleted

    def cleanup_stale_placeholder_faces(self) -> int:
        """Supprime les faces placeholder (embedding IS NULL, non épinglées) dont le
        person_id ne correspond à aucune annotation Picasa actuelle pour la même photo.

        Ces résidus apparaissent quand un ré-import Picasa a changé les person_id
        (ex. : après reset du catalogue), laissant d'anciens placeholders à des
        positions invalides. Retourne le nombre de faces supprimées."""
        with self._lock:
            conn = self._conn()
            try:
                n = conn.execute(
                    "DELETE FROM faces"
                    " WHERE embedding IS NULL"
                    "   AND (pinned IS NULL OR pinned=0)"
                    "   AND person_id IS NOT NULL"
                    "   AND EXISTS ("
                    "     SELECT 1 FROM picasa_annotations pa"
                    "     WHERE pa.photo_path = faces.photo_path"
                    "   )"
                    "   AND person_id NOT IN ("
                    "     SELECT pa2.person_id FROM picasa_annotations pa2"
                    "     WHERE pa2.photo_path = faces.photo_path"
                    "   )"
                ).rowcount
                if n:
                    conn.commit()
            finally:
                conn.close()
        if n:
            logger.info(
                "cleanup_stale_placeholder_faces: %d placeholder(s) orphelin(s) supprimé(s)", n
            )
        return n

    # Préfixe des cluster_id synthétiques pour les faces déjà identifiées.
    # Valeur choisie bien au-dessus du max réaliste d'HDBSCAN (~175 K faces max).
    _SYNTHETIC_CLUSTER_BASE = 10_000_000

    def assign_person_synthetic_clusters(self) -> int:
        """Migre TOUTES les faces identifiées vers un cluster_id synthétique (10⁷ + person_id).

        Ceci inclut les faces qui ont déjà un cluster_id non-synthétique issu d'un
        précédent run HDBSCAN. Sans cette migration, HDBSCAN peut réutiliser le même
        entier pour un groupe de faces totalement différentes dans un run ultérieur,
        provoquant une fusion incorrecte avec des faces d'une personne déjà identifiée.
        Retourne le nombre de faces mises à jour."""
        with self._lock:
            conn = self._conn()
            try:
                n = conn.execute(
                    f"UPDATE faces SET cluster_id = {self._SYNTHETIC_CLUSTER_BASE} + person_id"
                    " WHERE person_id IS NOT NULL"
                    f"   AND (cluster_id IS NULL OR cluster_id < {self._SYNTHETIC_CLUSTER_BASE})"
                ).rowcount
                if n:
                    conn.commit()
            finally:
                conn.close()
        if n:
            logger.info(
                "assign_person_synthetic_clusters: %d face(s) migrées vers cluster synthétique", n
            )
        return n

    def cleanup_orphan_person_ids(self, valid_person_ids: set[int]) -> tuple[int, int]:
        """Remet person_id=NULL sur les faces et supprime les annotations Picasa dont
        le person_id n'est plus présent dans catalog.db (orphelins après réinitialisation).

        Retourne (n_faces_reset, n_annotations_deleted).
        Doit être appelé avant un ré-import Picasa pour que _apply_picasa_annotations
        puisse ré-associer correctement les nouvelles annotations aux bonnes personnes.
        """
        if not valid_person_ids:
            return 0, 0
        ph = ",".join("?" * len(valid_person_ids))
        vals = list(valid_person_ids)
        with self._lock:
            conn = self._conn()
            try:
                n_faces = conn.execute(
                    f"UPDATE faces SET person_id=NULL"
                    f" WHERE person_id IS NOT NULL AND person_id NOT IN ({ph})",
                    vals,
                ).rowcount
                n_ann = conn.execute(
                    f"DELETE FROM picasa_annotations WHERE person_id NOT IN ({ph})",
                    vals,
                ).rowcount
                if n_faces or n_ann:
                    conn.commit()
            finally:
                conn.close()
        if n_faces or n_ann:
            logger.info(
                "cleanup_orphan_person_ids: %d face(s) réinitialisées, "
                "%d annotation(s) Picasa supprimées",
                n_faces, n_ann,
            )
        return n_faces, n_ann

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
