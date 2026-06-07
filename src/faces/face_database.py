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

    def get_all_embeddings(self) -> tuple[list[list[float]], list[int]]:
        """Returns (embeddings, face_ids) for non-pinned faces with stored embeddings."""
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
        face_ids = [r[0] for r in rows]
        embeddings = [_dec(r[1]) for r in rows]
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
        """Returns the largest-bbox face for a cluster or person."""
        with self._lock:
            conn = self._conn()
            try:
                if cluster_id is not None:
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

    def get_representative_embedding(
        self,
        cluster_id: Optional[int] = None,
        person_id: Optional[int] = None,
    ) -> Optional[list[float]]:
        """Return the embedding of the representative face for a cluster or person."""
        with self._lock:
            conn = self._conn()
            try:
                if cluster_id is not None:
                    row = conn.execute(
                        "SELECT embedding FROM faces"
                        " WHERE cluster_id=? AND embedding IS NOT NULL"
                        " ORDER BY (bbox_w * bbox_h) DESC LIMIT 1",
                        (cluster_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT embedding FROM faces"
                        " WHERE person_id=? AND embedding IS NOT NULL"
                        " ORDER BY (bbox_w * bbox_h) DESC LIMIT 1",
                        (person_id,),
                    ).fetchone()
            finally:
                conn.close()
        return _dec(row[0]) if row and row[0] else None

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
                rows = conn.execute(
                    "SELECT person_id, COUNT(DISTINCT photo_path)"
                    " FROM faces WHERE person_id IS NOT NULL GROUP BY person_id"
                ).fetchall()
            finally:
                conn.close()
        counts = {r[0]: r[1] for r in rows}
        for p in persons:
            if p.id in counts:
                p.photo_count = counts[p.id]
                rep = self.get_representative_face(person_id=p.id)
                if rep:
                    p.cover_path = rep.photo_path
                    p.cover_bbox = (rep.bbox_x, rep.bbox_y, rep.bbox_w, rep.bbox_h)

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
