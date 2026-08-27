# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import sqlite3
from contextlib import contextmanager
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from src.core.app_dirs import APP_DATA_DIR
from src.core.i18n import translate
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

_CREATE_INDEX_ERRORS = """
CREATE TABLE IF NOT EXISTS face_index_errors (
    photo_path   TEXT PRIMARY KEY,
    error_type   TEXT NOT NULL,
    last_attempt REAL NOT NULL,
    excluded     INTEGER DEFAULT 0
)
"""

_IOU_THRESHOLD = 0.30   # minimum overlap to associate a Picasa face with a detected face


def _iou(a: tuple, b: tuple) -> float:
    """IoU between two bboxes (x, y, w, h)."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(ax2, bx2),   min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# Confidence tiers for the recognition (cosine similarity of an embedding vs
# the centroid of a person), from the lowest to the highest:
#   [0.00, 0.55[  no automatic action (unidentified face)
#   [0.55, 0.70[  a suggestion is recorded (suggestion_person_id/score): the
#                 group appears as "awaiting verification" under the person
#                 concerned, to be confirmed manually
#   [0.70, 1.00]  automatic assignment of the person, without confirmation
#                 (cf. set_cluster_suggestions below)
# _SIM_STRONG (0.50) and _SIM_WEAK (0.45, src/ui/people_panel.py) are separate
# display thresholds (a blue "Probably X" label >= _SIM_STRONG, a grey
# "Maybe X" on [_SIM_WEAK, _SIM_STRONG[) for the faces that have not reached
# _SIM_SUGGEST yet — do not confuse those two thresholds with the ones
# above.
_SIM_SUGGEST     = 0.55  # minimum threshold to create an "awaiting verification" suggestion
_SIM_AUTO_ASSIGN = 0.70  # threshold for the automatic assignment of the person, without confirmation


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
        # One SQLite connection per (instance, thread) — cf. _conn().
        self._tls = threading.local()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _guard(self):
        """Lock + thread-local connection + guaranteed rollback on an exception.

        Replaces the repeated pattern "with self._lock: conn = self._conn();
        try: … except BaseException: conn.rollback(); raise" (cf. CLAUDE.md,
        the connection pattern): the cached connection must NEVER stay inside
        an open transaction, failing which every subsequent write fails with
        "database is locked"."""
        with self._lock:
            conn = self._conn()
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
        # Cache of the centroid of each person (used for the recognition suggestions,
        # e.g. face_panel._AssignPrepLoader). Invalidated as soon as the fingerprint
        # (COUNT + SUM of the assigned person_id) changes — much cheaper (the
        # idx_faces_person index, a few ms) than the full recomputation, which has to
        # decode every embedding (~60k on a large library, >5 s).
        self._person_centroid_cache: "dict[int, list[float]] | None" = None
        # {person_id: (face_count, sum_of_face_ids)} — one fingerprint per person,
        # so that an identification only invalidates the person it concerns.
        self._person_centroid_cache_fp: "dict[int, tuple] | None" = None

    def _conn(self) -> sqlite3.Connection:
        """SQLite connection of the current thread, created once per thread
        (the ThumbnailCache/Catalog pattern). Gains WAL + synchronous NORMAL +
        a timeout along the way, all of them entirely absent before: in the
        default rollback-journal mode, every write of the face indexer blocked
        the reads of the UI (and the other way round).

        The write methods no longer close the connection: on an exception,
        their `except BaseException: conn.rollback()` guard replaces the
        implicit rollback the former close provided."""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-2048")
            self._tls.conn = conn
        return conn

    def close(self) -> None:
        """Closes the connection of the current thread (tests, application shutdown)."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

    def _init_db(self) -> None:
        with self._guard() as conn:
            conn.execute(_CREATE_INDEXED)
            conn.execute(_CREATE_FACES)
            conn.execute(_CREATE_PICASA_ANNOTATIONS)
            conn.execute(_CREATE_INDEX_ERRORS)
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
            # Migrations: add the missing columns
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
            # After the migration (the column does not exist in _CREATE_FACES):
            # serves get_suggested_clusters_for_person and get_persons_pending_count,
            # which otherwise scanned the whole faces table (~60k rows).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_faces_suggestion"
                " ON faces(suggestion_person_id)"
            )
            # Migration indexed_photos
            ip_cols = {r[1] for r in conn.execute("PRAGMA table_info(indexed_photos)")}
            if "rotation" not in ip_cols:
                conn.execute(
                    "ALTER TABLE indexed_photos ADD COLUMN rotation INTEGER DEFAULT 0"
                )
            # Migration: delete the faces with corrupted bboxes (stored as a BLOB
            # instead of an INTEGER by an earlier version of the code).  The photos
            # concerned are removed from indexed_photos so as to be re-analysed.
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
            # Migration: purge the residual suggestions laid on already identified
            # faces. They are invisible (get_persons_pending_count requires
            # person_id IS NULL) but block their cluster for good: every producer
            # of suggestions filters on `suggestion_person_id IS NULL`. Origin: the
            # "pending" branch of set_cluster_suggestions did not check person_id
            # (fixed).
            cur = conn.execute(
                "UPDATE faces SET suggestion_person_id=NULL, suggestion_score=NULL"
                " WHERE suggestion_person_id IS NOT NULL AND person_id IS NOT NULL"
            )
            if cur.rowcount:
                logger.info(
                    "Migration: %d suggestion(s) résiduelle(s) purgée(s) "
                    "sur des visages déjà identifiés",
                    cur.rowcount,
                )
            # Migration: catch up the Picasa annotations left at consumed=0 although
            # the person was in fact identified afterwards (an accepted suggestion, a
            # manual identification…) on a face overlapping the annotation — paths
            # that did not update consumed before
            # _consume_matching_picasa_annotations() was added.
            stale_paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT photo_path FROM picasa_annotations WHERE consumed=0"
            ).fetchall()]
            if stale_paths:
                self._consume_matching_picasa_annotations(conn, stale_paths)
            conn.commit()

    # ------------------------------------------------------------------ indexing

    def get_paths_to_index(self, all_paths: list[str]) -> list[str]:
        """Returns paths from all_paths that have not been indexed yet.
        The video files are always excluded (no face detection).
        The files that failed (timeout/crash, the face_index_errors table) are
        excluded too: they are no longer retried automatically at every scan,
        only through the "Retry the face identification" context menu."""
        if not all_paths:
            return []
        from src.library.exif_reader import VIDEO_EXT
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT photo_path FROM indexed_photos"
            ).fetchall()
            error_rows = conn.execute(
                "SELECT photo_path FROM face_index_errors"
            ).fetchall()
        indexed = {r[0] for r in rows} | {r[0] for r in error_rows}
        return [
            p for p in all_paths
            if os.path.normpath(p) not in indexed
            and os.path.splitext(p)[1].lower() not in VIDEO_EXT
        ]

    # ------------------------------------------------------------------ erreurs d'indexation

    def mark_index_error(self, photo_path: str, error_type: str) -> None:
        """Records a detection failure (a timeout or a crash of the subprocess) for
        photo_path. As long as an error is recorded here, get_paths_to_index()
        excludes that file from the automatic scans."""
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            cur = conn.execute(
                "UPDATE face_index_errors SET error_type=?, last_attempt=?"
                " WHERE photo_path=?",
                (error_type, time.time(), photo_path),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO face_index_errors"
                    " (photo_path, error_type, last_attempt, excluded)"
                    " VALUES (?,?,?,0)",
                    (photo_path, error_type, time.time()),
                )
            conn.commit()

    def clear_index_error(self, photo_path: str) -> None:
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            conn.execute(
                "DELETE FROM face_index_errors WHERE photo_path=?", (photo_path,)
            )
            conn.commit()

    def get_index_error(self, photo_path: str) -> Optional[dict]:
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            row = conn.execute(
                "SELECT error_type, last_attempt, excluded"
                " FROM face_index_errors WHERE photo_path=?",
                (photo_path,),
            ).fetchone()
        if not row:
            return None
        return {"error_type": row[0], "last_attempt": row[1], "excluded": bool(row[2])}

    def get_error_paths(self, include_excluded: bool = False) -> list[str]:
        """Paths that failed the face indexing (timeout/crash).
        By default, does not include the files marked as definitively excluded:
        the user has already decided for them, no need to draw their attention
        again."""
        with self._guard() as conn:
            if include_excluded:
                rows = conn.execute(
                    "SELECT photo_path FROM face_index_errors"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT photo_path FROM face_index_errors WHERE excluded=0"
                ).fetchall()
        return [r[0] for r in rows]

    def set_index_excluded(self, photo_path: str, excluded: bool = True) -> None:
        """Definitively excludes (or reinstates) a photo from the scan and from the
        face recognition. Unlike the auto-ignore filter (faces.ignored,
        proportional to the size), this is an explicit decision of the user
        following repeated failures — never automatic."""
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            cur = conn.execute(
                "UPDATE face_index_errors SET excluded=? WHERE photo_path=?",
                (1 if excluded else 0, photo_path),
            )
            if cur.rowcount == 0 and excluded:
                conn.execute(
                    "INSERT INTO face_index_errors"
                    " (photo_path, error_type, last_attempt, excluded)"
                    " VALUES (?,?,?,1)",
                    (photo_path, "excluded", time.time()),
                )
            conn.commit()

    def get_indexed_rotation(self, photo_path: str) -> int:
        """Rotation (CW degrees) used during the last successful indexing of this
        photo, 0 if never indexed."""
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            row = conn.execute(
                "SELECT rotation FROM indexed_photos WHERE photo_path=?",
                (photo_path,),
            ).fetchone()
        return row[0] if row and row[0] is not None else 0

    # Auto-ignore thresholds for the poor quality faces.
    # Below these values, the face is saved with ignored=1:
    # it stays visible in the interface but does not take part in the clustering.
    _AUTO_IGNORE_MIN_SIDE_RATIO    = 0.03  # 3 % — base threshold (no face in the foreground)
    _AUTO_IGNORE_MIN_SIDE_FG_RATIO = 0.20  # 20 % — threshold qualifying a face as "foreground"
    _AUTO_IGNORE_FG_FRACTION       = 0.25  # once a foreground face qualifies, ignore the
                                            # faces < 1/4 of the smallest foreground face
    _AUTO_IGNORE_MIN_SIDE_ABS      = 22    # absolute floor (px) for the very small images
    _AUTO_IGNORE_MIN_SIDE          = 121   # fallback if the dimensions are unreadable
    _AUTO_IGNORE_MIN_SCORE         = 0.65  # InsightFace detection score (0–1)

    def save_faces(
        self,
        photo_path: str,
        detections: list[dict],
        rotation: int = 0,
        force_no_limit: bool = False,
    ) -> None:
        """
        Persist detected faces for a photo.
        detections: list of {'bbox': (x,y,w,h), 'embedding': list[float], 'det_score': float}
        rotation: CW degrees applied during detection (stored to reconstruct face crops).
        Faces with min(w,h) < effective threshold or det_score < _AUTO_IGNORE_MIN_SCORE
        are saved with ignored=1. The size threshold is proportional to the image:
        base = max(ABS, short_side * 3 %) ; fg_qualify = max(base*2, short_side * 20 %).
        A face qualifies as "foreground" when min(w,h) >= fg_qualify. If at least one
        foreground face is present, the effective threshold becomes 1/4 of the smallest
        foreground face's min(w,h) — faces smaller than that are ignored.
        Otherwise (all faces small — old scanned photo, distant group) base is used.
        Fallback to _AUTO_IGNORE_MIN_SIDE if image dimensions cannot be read.

        "An identified face stays identified" is an invariant applied to *every* call
        (not only force_no_limit): the manually added faces (embedding NULL, pinned=1,
        cf. add_manual_face) are never deleted — they have never been seen by
        InsightFace, so a new detection cannot find them again — and the auto-detected
        faces already identified (person_id not NULL) are erased then reinserted like
        the other detections, their identification being carried over to the new face
        whose bbox overlaps the old one best (IoU > _IOU_THRESHOLD). Failing which a
        mere re-analysis (e.g. SingleFaceReindexThread after every 90° rotation in the
        preview, even before anything is saved) would silently erase every
        identification on the photo.

        force_no_limit=True ("Force a new detection with no size limit") only changes
        the auto-ignore threshold (above), entirely short-circuited in that mode, so no
        face comes out with ignored=1 (the hard filter of
        detector.py::detect_and_embed stays unchanged — CLAUDE.md forbids touching it).
        """
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            # embedding IS NOT NULL excludes both the manually added faces
            # (pinned=1, never findable again by a new detection) and the Picasa
            # placeholders (a wide bbox enclosing head/bust, not a real ArcFace
            # detection) — the latter have their own preservation mechanism, better
            # suited (centre-inside-region), through
            # _apply_picasa_annotations()/"still_pending" below; mixing them into
            # this strict IoU re-association would risk too low an IoU score (very
            # different shapes) and a missed match.
            preserved_ids = conn.execute(
                "SELECT bbox_x, bbox_y, bbox_w, bbox_h, person_id, pinned"
                " FROM faces"
                " WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL",
                (photo_path,),
            ).fetchall()
            delete_sql = (
                "DELETE FROM faces WHERE photo_path=?"
                " AND NOT (embedding IS NULL AND pinned=1)"
            )
            conn.execute(delete_sql, (photo_path,))
            # A success erases any previous error (timeout/crash): the photo has
            # really been analysed, it no longer needs attention.
            conn.execute(
                "DELETE FROM face_index_errors WHERE photo_path=?", (photo_path,)
            )
            # Put the Picasa annotations back to consumed=0 so that they are
            # re-applied to the new detections below.
            # Without this, a re-analysis erases the faces but leaves consumed=1:
            # the annotations would never be re-applied.
            conn.execute(
                "UPDATE picasa_annotations SET consumed=0 WHERE photo_path=?",
                (photo_path,)
            )
            # Thresholds proportional to the resolution of the image.
            # A face qualifies the photo as "foreground" if it reaches _fg_qualify
            # (20 % of the short side). If that is the case, every face smaller than
            # 1/4 of the smallest foreground face is ignored (the other faces, even
            # foreground ones, are never themselves ignored by this criterion).
            # Otherwise (an old scanned photo, a distant group…), the base threshold
            # (3 %) is used so as not to lose legitimate faces.
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(photo_path) as _img:
                    _iw, _ih = _img.size
                _shortest = min(_iw, _ih)
                _base_threshold = max(
                    self._AUTO_IGNORE_MIN_SIDE_ABS,
                    int(_shortest * self._AUTO_IGNORE_MIN_SIDE_RATIO),
                )
                _fg_qualify = max(
                    _base_threshold * 2,
                    int(_shortest * self._AUTO_IGNORE_MIN_SIDE_FG_RATIO),
                )
            except Exception:
                _base_threshold = self._AUTO_IGNORE_MIN_SIDE
                _fg_qualify     = self._AUTO_IGNORE_MIN_SIDE
            _foreground_sides = [
                min(int(d["bbox"][2]), int(d["bbox"][3]))
                for d in detections
                if min(int(d["bbox"][2]), int(d["bbox"][3])) >= _fg_qualify
            ]
            effective_min_side = (
                min(_foreground_sides) * self._AUTO_IGNORE_FG_FRACTION
                if _foreground_sides else _base_threshold
            )
            new_faces = []  # (face_id, x, y, w, h) — for the person_id re-association below
            for det in detections:
                x, y, w, h = (int(v) for v in det["bbox"])
                emb = det.get("embedding")
                blob = _enc(emb) if emb else None
                score = det.get("det_score", 1.0)
                low_quality = (
                    not force_no_limit
                    and (
                        min(w, h) < effective_min_side
                        or score < self._AUTO_IGNORE_MIN_SCORE
                    )
                )
                cur = conn.execute(
                    "INSERT INTO faces"
                    " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                    "  embedding, ignored, det_score)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (photo_path, x, y, w, h, blob,
                     1 if low_quality else 0, score),
                )
                new_faces.append((cur.lastrowid, x, y, w, h))
            if preserved_ids:
                for pbx, pby, pbw, pbh, pid, ppinned in preserved_ids:
                    best_id, best_iou = None, 0.0
                    for face_id, x, y, w, h in new_faces:
                        score = _iou((pbx, pby, pbw, pbh), (x, y, w, h))
                        if score > best_iou:
                            best_id, best_iou = face_id, score
                    if best_id is not None and best_iou > _IOU_THRESHOLD:
                        conn.execute(
                            "UPDATE faces SET person_id=?, pinned=? WHERE id=?",
                            (pid, ppinned, best_id),
                        )
                        new_faces = [f for f in new_faces if f[0] != best_id]
            conn.execute(
                "INSERT OR REPLACE INTO indexed_photos"
                " (photo_path, indexed_at, face_count, rotation) VALUES (?,?,?,?)",
                (photo_path, time.time(), len(detections), rotation),
            )
            # Apply the pending Picasa annotations (if any)
            self._apply_picasa_annotations(conn, photo_path)
            # Consume the annotations whose person is already carried by an InsightFace
            # face (with an embedding) on this photo — avoids duplicate placeholders if
            # the annotation could not be matched by bbox (sizes too different) but the
            # person is identified on the photo anyway.
            conn.execute(
                "UPDATE picasa_annotations SET consumed=1"
                " WHERE photo_path=? AND consumed=0"
                "   AND person_id IN ("
                "     SELECT DISTINCT person_id FROM faces"
                "     WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL"
                "   )",
                (photo_path, photo_path),
            )
            # Create placeholders for the annotations matched to no InsightFace face
            # (a face not detected: pose, quality, too low a score…).
            # Without this, the placeholder created at Picasa import time is deleted
            # by the DELETE above and never recreated — the person disappears.
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

    # ------------------------------------------------------------------ clustering

    def count_embeddings(self) -> int:
        """Total number of faces with an embedding (not pinned)."""
        with self._guard() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM faces"
                " WHERE embedding IS NOT NULL"
                "   AND (pinned IS NULL OR pinned = 0)"
            ).fetchone()[0]

    def count_identified_faces(self) -> int:
        """Number of faces with an embedding AND an assigned person_id (not pinned)."""
        with self._guard() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM faces"
                " WHERE embedding IS NOT NULL"
                "   AND (pinned IS NULL OR pinned = 0)"
                "   AND person_id IS NOT NULL"
            ).fetchone()[0]

    def get_all_embeddings(
        self,
        only_unidentified: bool = False,
    ) -> tuple["np.ndarray", list[int]]:
        """Returns (embeddings, face_ids) for non-pinned faces with stored embeddings.

        only_unidentified=True: only includes the faces without a person_id, so that
        HDBSCAN only runs on the faces not identified yet (~20 % fewer).

        embeddings is a float32 ndarray of shape (N, D) built directly from the
        binary blobs — avoids creating N×D Python float objects as an intermediate.
        """
        import numpy as np
        extra = " AND person_id IS NULL" if only_unidentified else ""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM faces"
                " WHERE embedding IS NOT NULL"
                "   AND (ignored IS NULL OR ignored = 0)"
                f"   AND (pinned IS NULL OR pinned = 0){extra}"
            ).fetchall()
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
        with self._guard() as conn:
            # Resets only the unidentified faces.
            # The faces with a person_id keep their synthetic cluster (10M+)
            # and stay visible in PersonClusterView during the clustering.
            # The pending suggestions are invalidated too, since the cluster_ids change.
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
                    progress_cb(translate(
                        "FaceDatabase", "Clustering: saving {done}/{total} faces…"
                    ).format(done=f"{done:,}".replace(",", " "),
                             total=f"{total:,}".replace(",", " ")))
            # Clean up the ArcFace faces that have become noise (cluster_id=NULL)
            # but keep a residual person_id from a previous clustering.
            # The faces without an embedding (Picasa placeholders) are preserved.
            orphaned = conn.execute(
                "SELECT DISTINCT photo_path, person_id FROM faces"
                " WHERE (pinned IS NULL OR pinned=0)"
                "   AND cluster_id IS NULL"
                "   AND person_id IS NOT NULL"
                "   AND embedding IS NOT NULL"
            ).fetchall()
            conn.execute(
                "UPDATE faces SET person_id=NULL"
                " WHERE (pinned IS NULL OR pinned=0)"
                "   AND cluster_id IS NULL"
                "   AND person_id IS NOT NULL"
                "   AND embedding IS NOT NULL"
            )
            for photo_path, person_id in orphaned:
                self._release_picasa_annotation(conn, photo_path, person_id)
            # Propagate the person_id to the faces without a person_id in an already named
            # cluster. Covers the case of a new face added by reclustering to a cluster
            # other faces of which already have a person_id (an earlier assignment).
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
            # After the propagation, deduplicate on every photo concerned.
            self._dedup_in_transaction(conn)
            conn.commit()

    # ------------------------------------------------------------------ queries

    def get_clusters(self) -> list[tuple[int, int]]:
        """Returns [(cluster_id, face_count)] for all clusters, ordered by size desc."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT cluster_id, COUNT(*) FROM faces"
                " WHERE cluster_id IS NOT NULL"
                " GROUP BY cluster_id ORDER BY COUNT(*) DESC"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def get_unnamed_clusters(self) -> list[tuple[int, int]]:
        """Returns [(cluster_id, face_count)] for clusters with no person assigned,
        not ignored, not pinned and not pending verification."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT cluster_id, COUNT(*) FROM faces"
                " WHERE cluster_id IS NOT NULL"
                "   AND person_id IS NULL"
                "   AND suggestion_person_id IS NULL"
                "   AND ignored = 0"
                "   AND (pinned IS NULL OR pinned = 0)"
                " GROUP BY cluster_id ORDER BY COUNT(*) DESC"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def ignore_cluster(self, cluster_id: int) -> None:
        """Mark all faces of a cluster as ignored so they won't appear for naming."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET ignored=1 WHERE cluster_id=?", (cluster_id,)
            )
            conn.commit()

    def unignore_cluster(self, cluster_id: int) -> None:
        """Re-expose a previously ignored cluster."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET ignored=0 WHERE cluster_id=?", (cluster_id,)
            )
            conn.commit()

    # ------------------------------------------------------------------ pending suggestions

    def set_cluster_suggestions(self, suggestions: "dict[int, tuple[int, float]]") -> None:
        """Batch-set suggestion_person_id/score for multiple clusters.

        suggestions: {cluster_id: (person_id, score)}
        The single entry point of every producer of suggestions
        (resuggest_clusters, find_similar_to_persons, isolate_and_suggest, the
        auto-promotion of the group grid): a score >= _SIM_AUTO_ASSIGN assigns
        the person directly, without going through the manual verification step
        (the same side effects as accept_cluster_suggestion: dedup, consumption
        of the pending Picasa annotations). Below it, only the suggestion is
        recorded ("awaiting verification"), idempotent: does not touch the
        clusters already carrying a suggestion or already assigned.
        """
        if not suggestions:
            return
        auto    = {cid: v for cid, v in suggestions.items() if v[1] >= _SIM_AUTO_ASSIGN}
        pending = {cid: v for cid, v in suggestions.items() if cid not in auto}
        with self._guard() as conn:
            for cluster_id, (person_id, score) in pending.items():
                # `person_id IS NULL` is essential: without it, a partially (or
                # entirely) identified cluster ended up with a suggestion laid on
                # already named faces — invisible in the UI, but blocking any later
                # suggestion on that cluster for ever (the `suggestion_person_id IS
                # NULL` guard below).
                conn.execute(
                    "UPDATE faces SET suggestion_person_id=?, suggestion_score=?"
                    " WHERE cluster_id=? AND person_id IS NULL"
                    "   AND suggestion_person_id IS NULL",
                    (person_id, score, cluster_id),
                )
            if auto:
                all_paths: list[str] = []
                for cluster_id, (person_id, _score) in auto.items():
                    paths = [r[0] for r in conn.execute(
                        "SELECT DISTINCT photo_path FROM faces WHERE cluster_id=?",
                        (cluster_id,),
                    ).fetchall()]
                    # The same idempotence guard as the "pending" branch: a cluster
                    # already assigned or already suggested (by a previous call) is not
                    # rewritten — "the first call wins", whatever the tier.
                    cur = conn.execute(
                        "UPDATE faces SET person_id=?, suggestion_person_id=NULL,"
                        " suggestion_score=NULL"
                        " WHERE cluster_id=? AND person_id IS NULL"
                        "   AND suggestion_person_id IS NULL",
                        (person_id, cluster_id),
                    )
                    if cur.rowcount:
                        all_paths.extend(paths)
                self._dedup_in_transaction(conn, all_paths)
                self._consume_matching_picasa_annotations(conn, all_paths)
            conn.commit()

    def clear_cluster_suggestion(self, cluster_id: int) -> None:
        """Clear suggestion (reject). The cluster returns to the unnamed list."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET suggestion_person_id=NULL, suggestion_score=NULL"
                " WHERE cluster_id=?",
                (cluster_id,),
            )
            conn.commit()

    def resuggest_clusters(
        self, cluster_ids: "list[int]", exclude_person_id: "int | None" = None
    ) -> None:
        """Clears the suggestions of the given clusters and recomputes the best person for each.

        Called after a rejection so that the isolated faces can be offered to
        another person (exclude_person_id apart).
        """
        if not cluster_ids:
            return

        # 1. Clear the suggestions and fetch the embeddings by cluster
        cid_to_embs: "dict[int, list]" = {}
        with self._guard() as conn:
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

        if not cid_to_embs:
            return

        # 2. Load the embeddings of every person (the excluded one apart)
        by_person: "dict[int, list]" = {}
        with self._guard() as conn:
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

        for pid, blob in pers_rows:
            by_person.setdefault(pid, []).append(_dec(blob))

        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return

        # 3. For each cluster, compute the centroid and find the best person
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
        with self._guard() as conn:
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
            self._consume_matching_picasa_annotations(conn, paths)
            conn.commit()

    def get_suggested_clusters_for_person(
        self, person_id: int
    ) -> "list[tuple[int, int, float]]":
        """Returns [(cluster_id, face_count, score)] for clusters pending verification
        for this person, ordered by score descending."""
        with self._guard() as conn:
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
        return [(r[0], r[1], r[2] or 0.0) for r in rows]

    def get_persons_pending_count(self) -> "dict[int, int]":
        """Returns {person_id: pending_cluster_count} for all persons with suggestions."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT suggestion_person_id, COUNT(DISTINCT cluster_id)"
                " FROM faces"
                " WHERE suggestion_person_id IS NOT NULL AND person_id IS NULL"
                " GROUP BY suggestion_person_id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_representative_face(
        self,
        cluster_id: Optional[int] = None,
        person_id: Optional[int] = None,
    ) -> Optional[FaceInfo]:
        """Returns the cover face (is_cover=1) if set, otherwise the largest-bbox face."""
        with self._guard() as conn:
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
        if row:
            return FaceInfo(
                id=row[0], photo_path=row[1],
                bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                cluster_id=row[6], person_id=row[7],
            )
        return None

    def set_cover_face(self, face_id: int) -> None:
        """Sets this face as the thumbnail of the group (is_cover). Clears the old cover."""
        with self._guard() as conn:
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

    def get_face_by_id(self, face_id: int) -> Optional[FaceInfo]:
        """Returns FaceInfo for a single face_id, or None if not found."""
        with self._guard() as conn:
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
        with self._guard() as conn:
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
        if not rows:
            return None
        embeddings = [_dec(r[0]) for r in rows]
        return _centroid(embeddings)

    def get_all_cluster_centroids(
        self, cluster_ids: list[int]
    ) -> dict[int, list[float]]:
        """Returns {cluster_id: centroid} for every requested cluster.
        Queried in batches of 500 to respect the SQLite variable limit (999)."""
        if not cluster_ids:
            return {}
        _CHUNK = 500
        by_cluster: dict[int, list] = {}
        with self._guard() as conn:
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
        return {cid: _centroid(embs) for cid, embs in by_cluster.items()}

    def get_all_person_centroids(
        self, person_ids: list[int]
    ) -> dict[int, list[float]]:
        """Returns {person_id: centroid} for every requested person.

        The result is cached in memory and refreshed **person by person**: one
        fingerprint per person (COUNT + SUM of the face ids, a grouped read on
        idx_faces_person taking a few ms) tells which people really moved, and
        only those get their embeddings decoded again.

        A single global fingerprint (the earlier scheme) was changed by any
        identification whatsoever, so confirming one suggestion re-decoded the
        ~60k embeddings of the whole library — several seconds — to recompute
        centroids that had not moved. That cost was paid on the spot, in front of
        the user: the faces panel reloads right after an identification, and its
        loading thread calls this method."""
        if not person_ids:
            return {}
        # The lock is only held during the SQL reads: the decoding of the
        # embeddings happens outside the lock so as not to block the other threads
        # (e.g. the face queries of the UI thread). If two threads rebuild at the
        # same time, the result is identical — the last write wins.
        rows = None
        with self._guard() as conn:
            fps: dict[int, tuple] = {
                pid: (count, id_sum)
                for pid, count, id_sum in conn.execute(
                    "SELECT person_id, COUNT(*), IFNULL(SUM(id), 0) FROM faces"
                    " WHERE person_id IS NOT NULL GROUP BY person_id"
                ).fetchall()
            }
            cached     = self._person_centroid_cache or {}
            cached_fps = self._person_centroid_cache_fp or {}
            # SUM(id) and not SUM(person_id): a face moving from one person to
            # another (merge_persons, reassignment) changes the count of neither
            # of them, but does change the sum of their face ids on both sides.
            stale = [pid for pid, fp in fps.items() if cached_fps.get(pid) != fp]
            if stale:
                rows = conn.execute(
                    "SELECT person_id, embedding FROM faces"
                    " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
                    "   AND person_id IN (%s)" % ",".join("?" * len(stale)),
                    stale,
                ).fetchall()

        if stale or len(cached_fps) != len(fps):
            # A new dict, never a mutation of the shared one: another thread may
            # be reading it. The untouched people keep their centroid as is — the
            # whole point of the per-person fingerprint.
            cache = {
                pid: emb for pid, emb in cached.items()
                if pid in fps and pid not in stale
            }
            if rows:
                import numpy as np
                sums: dict[int, "np.ndarray"] = {}
                counts: dict[int, int] = {}
                for pid, blob in rows:
                    vec = np.frombuffer(blob, dtype=np.float32)
                    if pid in sums:
                        sums[pid] += vec
                        counts[pid] += 1
                    else:
                        sums[pid] = vec.copy()
                        counts[pid] = 1
                for pid in sums:
                    cache[pid] = (sums[pid] / counts[pid]).tolist()
            self._person_centroid_cache    = cache
            self._person_centroid_cache_fp = fps
        else:
            cache = cached

        wanted = set(person_ids)
        return {pid: emb for pid, emb in cache.items() if pid in wanted}

    def get_all_person_cluster_centroids(
        self, person_ids: list[int]
    ) -> dict[int, dict[int, list[float]]]:
        """
        Returns {person_id: {cluster_id: centroid}} for every person.

        Since one name can be associated with several distinct groups, each group
        keeps its own centroid rather than being melted into a global average.
        That preserves the visual diversity of the person and improves the
        accuracy of the recognition suggestions.
        """
        if not person_ids:
            return {}
        _CHUNK = 500
        all_rows: list = []
        with self._guard() as conn:
            for i in range(0, len(person_ids), _CHUNK):
                chunk = person_ids[i:i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                all_rows.extend(conn.execute(
                    f"SELECT person_id, cluster_id, embedding FROM faces"
                    f" WHERE person_id IN ({ph})"
                    f"   AND embedding IS NOT NULL AND cluster_id IS NOT NULL",
                    chunk,
                ).fetchall())
        # Decoding of the embeddings outside the lock (cf. get_all_person_centroids).
        by_pc: dict[tuple, list] = {}
        for pid, cid, blob in all_rows:
            by_pc.setdefault((pid, cid), []).append(_dec(blob))
        result: dict[int, dict[int, list[float]]] = {}
        for (pid, cid), embs in by_pc.items():
            result.setdefault(pid, {})[cid] = _centroid(embs)
        return result

    def get_cluster_person(self, cluster_id: int) -> int | None:
        """Returns the person_id already associated with this group, or None if it is not named."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT DISTINCT person_id FROM faces"
                " WHERE cluster_id=? AND person_id IS NOT NULL LIMIT 1",
                (cluster_id,),
            ).fetchone()
        return row[0] if row else None

    def get_cluster_persons(self, cluster_ids: list[int]) -> dict[int, int]:
        """Returns {cluster_id: person_id} for the clusters having at least one named face.
        Of use to display the name of a person on faces re-indexed after an assignment."""
        if not cluster_ids:
            return {}
        _CHUNK = 500
        result: dict[int, int] = {}
        with self._guard() as conn:
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
        return result

    def get_all_representative_faces(
        self, cluster_ids: list[int]
    ) -> "dict[int, FaceInfo]":
        """Returns {cluster_id: FaceInfo} for every cluster in a single query.
        Priority: is_cover=1, otherwise the face with the largest bbox."""
        if not cluster_ids:
            return {}
        _CHUNK = 500
        all_rows = []
        with self._guard() as conn:
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
        result: dict[int, FaceInfo] = {}
        for row in all_rows:
            cid = row[6]
            if cid not in result:  # the first row = the best one (cover or largest bbox)
                result[cid] = FaceInfo(
                    id=row[0], photo_path=row[1],
                    bbox_x=row[2], bbox_y=row[3], bbox_w=row[4], bbox_h=row[5],
                    cluster_id=cid, person_id=row[7],
                    detected_rotation=row[10],
                )
        return result

    def get_faces_for_photo(self, photo_path: str) -> list[FaceInfo]:
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT f.id, f.photo_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,"
                "       f.cluster_id, f.person_id, f.ignored, f.pinned,"
                "       CASE WHEN f.embedding IS NULL THEN 0"
                "            ELSE COALESCE(ip.rotation, 0) END,"
                "       f.suggestion_person_id, f.suggestion_score"
                " FROM faces f"
                " LEFT JOIN indexed_photos ip ON f.photo_path = ip.photo_path"
                " WHERE f.photo_path=?",
                (photo_path,),
            ).fetchall()
        return [
            FaceInfo(
                id=r[0], photo_path=r[1],
                bbox_x=r[2], bbox_y=r[3], bbox_w=r[4], bbox_h=r[5],
                cluster_id=r[6], person_id=r[7],
                ignored=bool(r[8]),
                pinned=bool(r[9]),
                detected_rotation=r[10],
                suggestion_person_id=r[11],
                suggestion_score=r[12] or 0.0,
            )
            for r in rows
        ]

    def get_photos_for_cluster(self, cluster_id: int) -> list[str]:
        """Returns distinct photo paths for a cluster (non-ignored faces only)."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT photo_path FROM faces"
                " WHERE cluster_id=? AND ignored=0",
                (cluster_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_clusters_for_person(self, person_id: int) -> list[tuple[int, int]]:
        """Returns [(cluster_id, photo_count)] for clusters where this person has a face.
        photo_count = distinct photos WHERE THIS PERSON's face appears in the cluster.
        Ordered by photo_count descending."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT cluster_id, COUNT(DISTINCT photo_path)"
                " FROM faces"
                " WHERE person_id=? AND cluster_id IS NOT NULL"
                " GROUP BY cluster_id"
                " ORDER BY COUNT(DISTINCT photo_path) DESC",
                (person_id,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def unassign_person_from_cluster(self, person_id: int, cluster_id: int) -> None:
        """Clears person_id on all faces of cluster_id that belong to this person."""
        with self._guard() as conn:
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT photo_path FROM faces"
                " WHERE person_id = ? AND cluster_id = ?",
                (person_id, cluster_id),
            ).fetchall()]
            conn.execute(
                "UPDATE faces SET person_id = NULL"
                " WHERE person_id = ? AND cluster_id = ?",
                (person_id, cluster_id),
            )
            for photo_path in paths:
                self._release_picasa_annotation(conn, photo_path, person_id)
            conn.commit()

    def get_photos_for_person(self, person_id: int) -> list[str]:
        """Returns distinct photo paths for a named person."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT photo_path FROM faces WHERE person_id=?",
                (person_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_faces_for_person(self, person_id: int) -> list["FaceInfo"]:
        """Returns all FaceInfo for a person, ordered by photo then bbox position."""
        with self._guard() as conn:
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
        with self._guard() as conn:
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
        """Ignores the redundant faces (same person, same photo) in the active transaction.

        For each (photo_path, person_id) with several non-ignored faces, keeps the one
        whose bbox area is the largest (= the most prominent, most reliable face) and
        marks the others ignored=1.

        photo_paths: if given, limits the deduplication to those photos only.
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
        with self._guard() as conn:
            row = conn.execute(
                "SELECT photo_path FROM faces WHERE id=?", (face_id,)
            ).fetchone()
            conn.execute(
                "UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id)
            )
            if row:
                self._dedup_in_transaction(conn, [row[0]])
                self._consume_matching_picasa_annotations(conn, [row[0]])
            conn.commit()

    def assign_person_to_faces(self, face_ids: list[int], person_id: int) -> None:
        """Assign a named person to multiple faces in a single transaction."""
        if not face_ids:
            return
        with self._guard() as conn:
            ph = ",".join("?" * len(face_ids))
            paths = [r[0] for r in conn.execute(
                f"SELECT DISTINCT photo_path FROM faces WHERE id IN ({ph})", face_ids
            ).fetchall()]
            conn.executemany(
                "UPDATE faces SET person_id=? WHERE id=?",
                [(person_id, fid) for fid in face_ids],
            )
            self._dedup_in_transaction(conn, paths)
            self._consume_matching_picasa_annotations(conn, paths)
            conn.commit()

    def unassign_face(self, face_id: int) -> None:
        """Remove person and cluster from a single face (returns it to unknowns).
        Clears pinned so the face re-enters the automatic clustering."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT photo_path, person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
            conn.execute(
                "UPDATE faces SET person_id=NULL, cluster_id=NULL, pinned=0"
                " WHERE id=?",
                (face_id,),
            )
            if row and row[1] is not None:
                self._release_picasa_annotation(conn, row[0], row[1])
            conn.commit()

    def isolate_face(self, face_id: int) -> None:
        """Separates a face from its group and protects it from the reclustering.
        Assigns it a unique negative cluster_id (isolated, invisible in the grid)."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
            ).fetchone()
            min_pinned = row[0] if row and row[0] is not None else 0
            new_cluster_id = min(min_pinned, 0) - 1   # -1, -2, -3, ...
            face_row = conn.execute(
                "SELECT photo_path, person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
            conn.execute(
                "UPDATE faces SET cluster_id=?, pinned=1, person_id=NULL"
                " WHERE id=?",
                (new_cluster_id, face_id),
            )
            if face_row and face_row[1] is not None:
                self._release_picasa_annotation(conn, face_row[0], face_row[1])
            conn.commit()

    def isolate_and_assign_face(self, face_id: int, person_id: int) -> None:
        """Separates a face from its group and assigns it to a person in one transaction.
        Result: pinned=1, a unique negative cluster_id, person_id=person_id."""
        with self._guard() as conn:
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
                self._consume_matching_picasa_annotations(conn, [path_row[0]])
            conn.commit()

    def add_manual_face(self, photo_path: str, bbox: tuple, person_id: int) -> int:
        """Inserts a manually positioned face (a bbox drawn by the user, never passed
        through InsightFace) and assigns it to person_id straight away.

        embedding=NULL by construction: guarantees that detected_rotation will
        always resolve to 0 when read back (cf. get_faces_for_photo), hence that
        the bbox is reinterpreted exactly in the EXIF-corrected space where it
        was positioned (cf. _Canvas._bbox_from_screen_rect on the UI side).
        pinned=1 and a unique negative cluster_id isolate it definitively from
        the (re)clustering, as for isolate_and_assign_face().
        Returns the id of the created face.
        """
        photo_path = os.path.normpath(photo_path)
        bx, by, bw, bh = (int(v) for v in bbox)
        with self._guard() as conn:
            row = conn.execute(
                "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
            ).fetchone()
            min_pinned = row[0] if row and row[0] is not None else 0
            new_cluster_id = min(min_pinned, 0) - 1
            cur = conn.execute(
                "INSERT INTO faces"
                " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
                "  embedding, cluster_id, person_id, ignored, pinned, det_score)"
                " VALUES (?,?,?,?,?,NULL,?,?,0,1,1.0)",
                (photo_path, bx, by, bw, bh, new_cluster_id, person_id),
            )
            face_id = cur.lastrowid
            self._dedup_in_transaction(conn, [photo_path])
            conn.commit()
            return face_id

    def delete_face(self, face_id: int) -> None:
        """Permanently deletes a face (a hard delete).

        Reserved for undoing a recent manual addition (add_manual_face): a face
        detected by InsightFace must never be deleted this way, use
        unassign_face()/isolate_face() to keep it and leave it recoverable.
        """
        with self._guard() as conn:
            conn.execute("DELETE FROM faces WHERE id=?", (face_id,))
            conn.commit()

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

        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT photo_path FROM faces"
                " WHERE ignored=1 AND embedding IS NOT NULL"
                "   AND (det_score IS NULL OR det_score >= ?)",
                (self._AUTO_IGNORE_MIN_SCORE,),
            ).fetchall()

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
                _fg_qualify = max(
                    _base * 2,
                    int(_shortest * self._AUTO_IGNORE_MIN_SIDE_FG_RATIO),
                )
            except Exception:
                _base       = self._AUTO_IGNORE_MIN_SIDE
                _fg_qualify = self._AUTO_IGNORE_MIN_SIDE

            with self._lock:
                conn = self._conn()
                try:
                    all_faces = conn.execute(
                        "SELECT id, bbox_w, bbox_h, ignored, det_score, embedding"
                        " FROM faces WHERE photo_path=?",
                        (photo_path,),
                    ).fetchall()

                    foreground_sides = [
                        min(r[1], r[2]) for r in all_faces if min(r[1], r[2]) >= _fg_qualify
                    ]
                    effective = (
                        min(foreground_sides) * self._AUTO_IGNORE_FG_FRACTION
                        if foreground_sides else _base
                    )

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
                    conn.rollback()   # cf. _conn(): never an open transaction
                    logger.warning(
                        "recalculate_size_ignored: erreur %s : %s", photo_path, exc
                    )

        if progress_cb:
            progress_cb(total, total)
        return unignored, total

    def find_similar_to_persons(
        self, progress_cb: "Callable[[int, int], None] | None" = None
    ) -> tuple[int, int]:
        """Compares every unidentified cluster with the centroids of the named people.

        For each cluster without a person_id or an existing suggestion, computes its
        centroid and compares it with every centroid of the named people. If the cosine
        similarity reaches _SIM_SUGGEST (0.55), a suggestion is created and will appear
        in the "Pending" section of the view of the person concerned (or the person is
        assigned directly if the score reaches _SIM_AUTO_ASSIGN — cf.
        set_cluster_suggestions).

        Returns (suggestions_created, clusters_checked).

        A vectorised comparison (a single matrix product clusters × people).
        The loop-over-loop version called `_cosine_sim` once per pair — on a real
        library (22 000 groups × 490 people, i.e. ~11 M calls each allocating two
        numpy arrays) the pass took several minutes, which made triggering it
        automatically out of the question.
        """
        # 1. Every embedding of unidentified clusters without an existing suggestion
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT cluster_id, embedding FROM faces"
                " WHERE cluster_id IS NOT NULL"
                "   AND person_id IS NULL"
                "   AND suggestion_person_id IS NULL"
                "   AND ignored = 0"
                "   AND embedding IS NOT NULL"
            ).fetchall()

        if not rows:
            return 0, 0

        cid_to_embs: "dict[int, list]" = {}
        for cid, blob in rows:
            cid_to_embs.setdefault(cid, []).append(_dec(blob))

        total = len(cid_to_embs)

        # 2. Centroids of every named person
        with self._guard() as conn:
            pers_rows = conn.execute(
                "SELECT person_id, embedding FROM faces"
                " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
            ).fetchall()

        by_person: "dict[int, list]" = {}
        for pid, blob in pers_rows:
            by_person.setdefault(pid, []).append(_dec(blob))
        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return 0, total

        # 3. For each cluster, find the best person
        suggestions = self._best_person_per_cluster(
            cid_to_embs, person_centroids, progress_cb
        )

        if suggestions:
            self.set_cluster_suggestions(suggestions)

        return len(suggestions), total

    @staticmethod
    def _best_person_per_cluster(
        cid_to_embs: "dict[int, list]",
        person_centroids: "dict[int, list]",
        progress_cb: "Callable[[int, int], None] | None" = None,
    ) -> "dict[int, tuple[int, float]]":
        """{cluster_id: (person_id, score)} for the clusters reaching _SIM_SUGGEST.

        Extracted from find_similar_to_persons so as to be testable without a database."""
        total = len(cid_to_embs)
        cids = list(cid_to_embs)
        pids = list(person_centroids)
        # No named person: nothing to offer. An essential guard before the numpy
        # branch — `np.array([])` is 1-D and `_unit()` asks for `axis=1` on it
        # (AxisError). The caller already filters that case, but the helper is
        # called directly elsewhere (tests, future callers).
        if not cids or not pids:
            return {}
        try:
            import numpy as np
        except ImportError:                       # scalar fallback (cf. _cosine_sim)
            suggestions: "dict[int, tuple[int, float]]" = {}
            for i, cid in enumerate(cids):
                if progress_cb:
                    progress_cb(i + 1, total)
                centroid = _centroid(cid_to_embs[cid])
                best_sim, best_pid = 0.0, None
                for pid in pids:
                    sim = _cosine_sim(centroid, person_centroids[pid])
                    if sim > best_sim:
                        best_sim, best_pid = sim, pid
                if best_pid is not None and best_sim >= _SIM_SUGGEST:
                    suggestions[cid] = (best_pid, best_sim)
            return suggestions

        def _unit(mat):
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            return mat / np.where(norms > 1e-8, norms, 1.0)

        persons = _unit(np.array([person_centroids[p] for p in pids], dtype=np.float32))

        suggestions = {}
        # By slices: bounds the memory of the matrix product (a real library
        # exceeds 20 000 clusters) and gives something to report progress with,
        # the progress being otherwise invisible until the very end.
        chunk = 512
        for start in range(0, total, chunk):
            block = cids[start:start + chunk]
            centroids = _unit(np.array(
                [np.mean(np.array(cid_to_embs[c], dtype=np.float32), axis=0)
                 for c in block],
                dtype=np.float32,
            ))
            sims = centroids @ persons.T
            best_idx = sims.argmax(axis=1)
            best_sim = sims[np.arange(len(block)), best_idx]
            for offset, cid in enumerate(block):
                score = float(best_sim[offset])
                if score >= _SIM_SUGGEST:
                    suggestions[cid] = (pids[int(best_idx[offset])], score)
            if progress_cb:
                progress_cb(min(start + chunk, total), total)
        return suggestions

    def ignore_face(self, face_id: int) -> None:
        """Mark a single face as ignored."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET ignored=1 WHERE id=?", (face_id,)
            )
            conn.commit()

    def unignore_face(self, face_id: int) -> None:
        """Restore a previously ignored face."""
        with self._guard() as conn:
            conn.execute("UPDATE faces SET ignored=0 WHERE id=?", (face_id,))
            conn.commit()

    def unassign_person_from_face(self, face_id: int) -> None:
        """Clear person_id from a single face without touching cluster or pinned."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT photo_path, person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
            conn.execute("UPDATE faces SET person_id=NULL WHERE id=?", (face_id,))
            if row and row[1] is not None:
                self._release_picasa_annotation(conn, row[0], row[1])
            conn.commit()

    def isolate_and_suggest(
        self, face_ids: list[int], exclude_person_id: "int | None" = None
    ) -> None:
        """Isolates each face in a unique negative cluster (pinned=1, person_id=NULL)
        and computes a suggestion by cosine similarity against every known person,
        optionally excluding exclude_person_id (the person just left).
        If the best match reaches _SIM_SUGGEST, suggestion_person_id is recorded."""
        if not face_ids:
            return

        # 1. Isolate each face and fetch its embedding
        face_embs: dict[int, list[float]] = {}  # new_cluster_id → embedding
        with self._guard() as conn:
            row = conn.execute(
                "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
            ).fetchone()
            next_cid = (min(row[0], 0) - 1) if row and row[0] is not None else -1

            for face_id in face_ids:
                cid = next_cid
                next_cid -= 1
                prior = conn.execute(
                    "SELECT photo_path, person_id FROM faces WHERE id=?", (face_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE faces SET cluster_id=?, pinned=1, person_id=NULL,"
                    " suggestion_person_id=NULL, suggestion_score=NULL WHERE id=?",
                    (cid, face_id),
                )
                if prior and prior[1] is not None:
                    self._release_picasa_annotation(conn, prior[0], prior[1])
                emb_row = conn.execute(
                    "SELECT embedding FROM faces WHERE id=?", (face_id,)
                ).fetchone()
                if emb_row and emb_row[0]:
                    face_embs[cid] = _dec(emb_row[0])
            conn.commit()

        if not face_embs:
            return

        # 2. Fetch the embeddings of every person (exclude_person_id apart)
        by_person: dict[int, list] = {}
        with self._guard() as conn:
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

        for pid, blob in rows:
            by_person.setdefault(pid, []).append(_dec(blob))

        person_centroids = {pid: _centroid(embs) for pid, embs in by_person.items()}
        if not person_centroids:
            return

        # 3. For each isolated face, look for the most similar person
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
        with self._guard() as conn:
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
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET cluster_id=? WHERE cluster_id=?",
                (target_cluster_id, source_cluster_id),
            )
            conn.commit()

    def assign_person_to_cluster(self, cluster_id: int, person_id: int) -> None:
        with self._guard() as conn:
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT photo_path FROM faces WHERE cluster_id=?", (cluster_id,)
            ).fetchall()]
            conn.execute(
                "UPDATE faces SET person_id=? WHERE cluster_id=?",
                (person_id, cluster_id),
            )
            self._dedup_in_transaction(conn, paths)
            self._consume_matching_picasa_annotations(conn, paths)
            conn.commit()

    def unassign_person(self, person_id: int) -> None:
        """Remove person assignment from all faces (before deleting a person)."""
        with self._guard() as conn:
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT photo_path FROM faces WHERE person_id=?", (person_id,)
            ).fetchall()]
            conn.execute(
                "UPDATE faces SET person_id=NULL WHERE person_id=?", (person_id,)
            )
            for photo_path in paths:
                self._release_picasa_annotation(conn, photo_path, person_id)
            conn.commit()

    def merge_persons(self, keep_id: int, remove_id: int) -> None:
        """
        Reassign all faces of remove_id to keep_id.
        The caller is responsible for deleting remove_id from catalog.persons.

        Also reassigns picasa_annotations.person_id: without it, remove_id is
        deleted from catalog.persons right after this call, and any Picasa
        annotation still linked to remove_id (consumed or not) becomes an orphan
        for ever — nothing is left to tell that it in fact corresponded to
        keep_id (bug found on 2026-07-04: person_id 154 merged into 512 had left
        orphan annotations, destroyed afterwards by cleanup_orphan_person_ids).
        """
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT photo_path FROM faces WHERE person_id=?",
                (remove_id,),
            ).fetchall()
            affected_paths = [r[0] for r in rows]
            conn.execute(
                "UPDATE faces SET person_id=? WHERE person_id=?",
                (keep_id, remove_id),
            )
            conn.execute(
                "UPDATE picasa_annotations SET person_id=? WHERE person_id=?",
                (keep_id, remove_id),
            )
            # keep_id and remove_id can each have a non-ignored face on one same
            # shared photo: without a dedup here, the merge would leave two
            # non-ignored faces for the same person on that photo.
            self._dedup_in_transaction(conn, affected_paths)
            conn.commit()

    def get_person_photo_count(self, person_id: int) -> int:
        """Count distinct photos where person_id has a face. Fast single query."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT photo_path) FROM faces"
                " WHERE person_id=? AND cluster_id IS NOT NULL",
                (person_id,),
            ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------ enrichment

    def enrich_persons_photo_count(self, persons: list[PersonInfo]) -> None:
        """Fills photo_count in-place ONLY (not cover_path/cover_bbox/pending_count).

        A lighter variant of enrich_persons() for the cases that only display the
        number of photos (e.g. the name assignment popup): avoids the CTE with a
        window function over the whole faces table (computing the cover photo)
        and the get_persons_pending_count() query, which together dominated the
        opening time of the popup (~0.7 s on a base of ~370 people) although that
        result is never displayed there."""
        if not persons:
            return
        with self._guard() as conn:
            count_rows = conn.execute(
                "SELECT person_id, COUNT(DISTINCT photo_path)"
                " FROM faces"
                " WHERE person_id IS NOT NULL AND cluster_id IS NOT NULL"
                " GROUP BY person_id"
            ).fetchall()
        counts = {r[0]: r[1] for r in count_rows}
        for p in persons:
            if p.id in counts:
                p.photo_count = counts[p.id]

    def enrich_persons(self, persons: list[PersonInfo]) -> None:
        """Fill photo_count and cover_path/cover_bbox in-place from face data."""
        if not persons:
            return
        with self._guard() as conn:
            # Count the photos where this person has a face detected in a cluster.
            # Consistent with get_clusters_for_person, which counts the photos by
            # person_id, not every photo of the cluster (avoids the false associations
            # due to mixed clusters — two people in the same HDBSCAN group).
            count_rows = conn.execute(
                "SELECT person_id, COUNT(DISTINCT photo_path)"
                " FROM faces"
                " WHERE person_id IS NOT NULL AND cluster_id IS NOT NULL"
                " GROUP BY person_id"
            ).fetchall()
            # A single CTE query for every representative face
            # (replaces N get_representative_face calls → N separate connections)
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

        The annotations replace the previous ones for this path.
        If detected faces already exist, they are associated with them straight
        away by IoU; otherwise they will be applied during the next detection
        through save_faces().
        """
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
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
            # Consume the annotations whose person is already carried by an InsightFace
            # face (with an embedding) on this photo — avoids duplicate placeholders.
            conn.execute(
                "UPDATE picasa_annotations SET consumed=1"
                " WHERE photo_path=? AND consumed=0"
                "   AND person_id IN ("
                "     SELECT DISTINCT person_id FROM faces"
                "     WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL"
                "   )",
                (photo_path, photo_path),
            )
            # Consume the annotations spatially overlapping an ArcFace face
            # — covers the case where Picasa and InsightFace identify the same physical
            # face under different person_ids (Picasa contacts ≠ ArcFace cluster).
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
            # Delete the old Picasa placeholders (embedding IS NULL, not pinned)
            # before creating new ones — avoids duplicates on a re-import.
            conn.execute(
                "DELETE FROM faces"
                " WHERE photo_path=? AND embedding IS NULL AND (pinned IS NULL OR pinned=0)",
                (photo_path,),
            )
            # Insert placeholders (without an embedding) for the annotations not
            # consumed, whether the photo has been detected by InsightFace or not.
            # Covers two cases: (a) a photo not analysed yet — no face at all;
            # (b) InsightFace detected other faces but missed this person.
            # The annotations stay unconsumed so as to be re-matched during the
            # future ArcFace analysis (save_faces).
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

    def _release_picasa_annotation(self, conn, photo_path: str, person_id: "int | None") -> None:
        """When an identification is removed from a face, puts consumed=0 back on the
        matching Picasa annotation (same photo, same person) if it is marked
        consumed=1. Without this, the annotation stays blocked indefinitely: it is
        never retried by _apply_picasa_annotations() again, even if a free and
        compatible face exists on the photo afterwards — the original Picasa
        identification is then lost silently and for ever."""
        if person_id is None:
            return
        conn.execute(
            "UPDATE picasa_annotations SET consumed=0"
            " WHERE photo_path=? AND person_id=? AND consumed=1",
            (photo_path, person_id),
        )

    def _apply_picasa_annotations(self, conn, photo_path: str) -> None:
        """
        Associates the unconsumed Picasa annotations with the detected faces of
        the same path. Main criterion: the centre of the ArcFace face is inside
        the Picasa region (robust, because Picasa stores a wide area enclosing
        the head/bust, whereas ArcFace gives a tight bbox).
        Fallback: IoU > threshold if no centre falls inside the region.
        Must be called in an already open conn+lock context.
        Only the faces without an existing person_id are candidates.
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
                # Criterion 1a: the InsightFace centre inside the Picasa region
                cx_f, cy_f = fx + fw // 2, fy + fh // 2
                in_picasa = ax <= cx_f <= ax + aw and ay <= cy_f <= ay + ah
                # Criterion 1b: the Picasa centre inside the InsightFace bbox (symmetrical)
                cx_p, cy_p = ax + aw // 2, ay + ah // 2
                in_face = fx <= cx_p <= fx + fw and fy <= cy_p <= fy + fh
                if in_picasa or in_face:
                    iou_score = _iou((ax, ay, aw, ah), (fx, fy, fw, fh))
                    score = 1.0 + iou_score  # > 1 so as to always take precedence over the IoU fallback
                else:
                    # Criterion 2 (fallback): the classic IoU
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

    def _consume_matching_picasa_annotations(self, conn, photo_paths: "list[str]") -> None:
        """Marks consumed=1 the Picasa annotations whose person has just been
        identified after the fact (an accepted suggestion, a manual
        identification, a cluster assignment) on a face spatially overlapping the
        annotation. Without this, the "awaiting recognition" counter stays wrong
        indefinitely for those cases: the recognition did take place, only the
        Picasa tracking flag was never updated — those identification paths do
        not go through _apply_picasa_annotations() (which only matches the faces
        without a person_id), so nothing else synchronises them."""
        for photo_path in set(photo_paths):
            pending = conn.execute(
                "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h, person_id"
                " FROM picasa_annotations WHERE photo_path=? AND consumed=0",
                (photo_path,),
            ).fetchall()
            if not pending:
                continue
            faces = conn.execute(
                "SELECT bbox_x, bbox_y, bbox_w, bbox_h, person_id FROM faces"
                " WHERE photo_path=? AND person_id IS NOT NULL AND embedding IS NOT NULL",
                (photo_path,),
            ).fetchall()
            if not faces:
                continue
            for ann_id, ax, ay, aw, ah, pid in pending:
                for fx, fy, fw, fh, fpid in faces:
                    if fpid != pid:
                        continue
                    fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
                    cx_f, cy_f = fx + fw // 2, fy + fh // 2
                    cx_p, cy_p = ax + aw // 2, ay + ah // 2
                    in_picasa = ax <= cx_f <= ax + aw and ay <= cy_f <= ay + ah
                    in_face = fx <= cx_p <= fx + fw and fy <= cy_p <= fy + fh
                    if (
                        in_picasa or in_face
                        or _iou((ax, ay, aw, ah), (fx, fy, fw, fh)) > _IOU_THRESHOLD
                    ):
                        conn.execute(
                            "UPDATE picasa_annotations SET consumed=1 WHERE id=?",
                            (ann_id,),
                        )
                        break

    def reset_clustering(self) -> None:
        """Clears the HDBSCAN cluster_ids of the unidentified faces.
        The faces with a person_id keep their synthetic cluster (10M+):
        the people stay visible in PersonClusterView during/after the reset.
        The embeddings and the index of the photos are always preserved."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE faces SET cluster_id=NULL"
                " WHERE (pinned IS NULL OR pinned=0)"
                "   AND person_id IS NULL"
            )
            conn.commit()

    def cleanup_overlapping_placeholders(self) -> int:
        """Deletes the placeholder faces (embedding IS NULL, not pinned) spatially
        overlapping an ArcFace face (embedding IS NOT NULL) on the same photo.

        Of use after a Picasa re-import, to eliminate the existing duplicates before
        the person_id criterion has covered them (e.g. Picasa contacts ≠ ArcFace cluster).

        Before deleting, transfers the person_id of the placeholder to the real face if
        the latter is not identified yet — failing which the Picasa identification
        carried by the placeholder is lost silently (bug found on 2026-07-04: ~1067
        identifications would have been destroyed by a naive call). If the two faces
        carry different person_ids (a genuine disagreement), deletes nothing and logs
        the conflict for manual review.
        Returns the number of faces deleted."""
        deleted = 0
        conflicts = 0
        with self._guard() as conn:
            photos = conn.execute(
                "SELECT DISTINCT f1.photo_path FROM faces f1"
                " JOIN faces f2 ON f1.photo_path = f2.photo_path"
                " WHERE f1.embedding IS NOT NULL"
                "   AND f2.embedding IS NULL AND (f2.pinned IS NULL OR f2.pinned=0)"
            ).fetchall()
            for (photo_path,) in photos:
                af_rows = conn.execute(
                    "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h, person_id FROM faces"
                    " WHERE photo_path=? AND embedding IS NOT NULL",
                    (photo_path,),
                ).fetchall()
                ph_rows = conn.execute(
                    "SELECT id, bbox_x, bbox_y, bbox_w, bbox_h, person_id FROM faces"
                    " WHERE photo_path=? AND embedding IS NULL"
                    "   AND (pinned IS NULL OR pinned=0)",
                    (photo_path,),
                ).fetchall()
                for ph_id, ax, ay, aw, ah, ph_pid in ph_rows:
                    cx_p, cy_p = ax + aw // 2, ay + ah // 2
                    for f_id, fx, fy, fw, fh, f_pid in af_rows:
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
                            if ph_pid is not None and f_pid is not None and f_pid != ph_pid:
                                conflicts += 1
                                logger.warning(
                                    "cleanup_overlapping_placeholders: conflit non résolu"
                                    " (placeholder %d person=%s vs face %d person=%s) sur %s",
                                    ph_id, ph_pid, f_id, f_pid, photo_path,
                                )
                                break
                            if ph_pid is not None and f_pid is None:
                                conn.execute(
                                    "UPDATE faces SET person_id=? WHERE id=?",
                                    (ph_pid, f_id),
                                )
                            conn.execute("DELETE FROM faces WHERE id=?", (ph_id,))
                            deleted += 1
                            break
            if deleted or conflicts:
                conn.commit()
        if deleted:
            logger.info(
                "cleanup_overlapping_placeholders: %d placeholder(s) supprimé(s)", deleted
            )
        if conflicts:
            logger.warning(
                "cleanup_overlapping_placeholders: %d conflit(s) non résolu(s), laissés en l'état",
                conflicts,
            )
        if deleted:
            self.restore_orphaned_ignored_faces()
        return deleted

    def restore_orphaned_ignored_faces(self) -> int:
        """Reactivates (ignored=0) the face with the largest area of each
        (photo_path, person_id) group that no longer has a single visible face (all
        of them ignored=1).

        Happens when _dedup_in_transaction() had preferred a larger duplicate
        (typically a Picasa placeholder) and put that face at ignored=1, and that
        duplicate was then deleted (e.g. by cleanup_overlapping_placeholders) without
        re-evaluating the invariant — leaving the identification orphaned and invisible
        in the UI although person_id stays correct (bug found on 2026-07-04, the Jean
        Cirre case: 10 364 groups affected in the database). Returns the number of
        faces reactivated."""
        with self._guard() as conn:
            n = conn.execute(
                """
                UPDATE faces SET ignored=0
                WHERE ignored=1
                  AND person_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM faces f2
                      WHERE f2.photo_path=faces.photo_path
                        AND f2.person_id=faces.person_id
                        AND f2.ignored=0
                  )
                  AND id = (
                      SELECT f3.id FROM faces f3
                      WHERE f3.photo_path=faces.photo_path
                        AND f3.person_id=faces.person_id
                      ORDER BY f3.bbox_w * f3.bbox_h DESC, f3.id ASC
                      LIMIT 1
                  )
                """
            ).rowcount
            # Unconditional commit(): an UPDATE/DELETE opens a transaction
            # even with 0 rows affected; committing only if n>0 left the
            # connection of this thread inside an open transaction, which then
            # blocked every write of another thread with "database is locked"
            # (cf. CLAUDE.md, the connection pattern) — a real bug observed
            # through e2e (test_folder_management), a second FaceIndexThread
            # requeue vs ClusterThread.assign_person_synthetic_clusters.
            conn.commit()
        if n:
            logger.info(
                "restore_orphaned_ignored_faces: %d visage(s) réactivé(s) (identification orpheline)", n
            )
        return n

    def cleanup_stale_placeholder_faces(self) -> int:
        """Deletes the placeholder faces (embedding IS NULL, not pinned) whose
        person_id matches no current Picasa annotation for the same photo.

        Such residue appears when a Picasa re-import has changed the person_ids
        (e.g. after a reset of the catalog), leaving old placeholders at invalid
        positions. Returns the number of faces deleted."""
        with self._guard() as conn:
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
            # Unconditional commit() — cf. restore_orphaned_ignored_faces above.
            conn.commit()
        if n:
            logger.info(
                "cleanup_stale_placeholder_faces: %d placeholder(s) orphelin(s) supprimé(s)", n
            )
        return n

    # Prefix of the synthetic cluster_ids for the already identified faces.
    # A value chosen well above the realistic HDBSCAN max (~175 K faces max).
    _SYNTHETIC_CLUSTER_BASE = 10_000_000

    def assign_person_synthetic_clusters(self) -> int:
        """Migrates EVERY identified face to a synthetic cluster_id (10⁷ + person_id).

        This includes the faces that already have a non-synthetic cluster_id from a
        previous HDBSCAN run. Without this migration, HDBSCAN can reuse the same
        integer for a group of completely different faces in a later run, causing an
        incorrect merge with the faces of an already identified person.
        Returns the number of faces updated."""
        with self._guard() as conn:
            n = conn.execute(
                f"UPDATE faces SET cluster_id = {self._SYNTHETIC_CLUSTER_BASE} + person_id"
                " WHERE person_id IS NOT NULL"
                f"   AND (cluster_id IS NULL OR cluster_id < {self._SYNTHETIC_CLUSTER_BASE})"
            ).rowcount
            # Unconditional commit() — cf. restore_orphaned_ignored_faces above.
            conn.commit()
        if n:
            logger.info(
                "assign_person_synthetic_clusters: %d face(s) migrées vers cluster synthétique", n
            )
        return n

    def cleanup_orphan_person_ids(self, valid_person_ids: set[int]) -> tuple[int, int]:
        """Puts person_id=NULL back on the faces and deletes the Picasa annotations whose
        person_id is no longer present in catalog.db (orphans after a reset).

        Returns (n_faces_reset, n_annotations_deleted).
        Must be called before a Picasa re-import so that _apply_picasa_annotations
        can correctly re-associate the new annotations with the right people.
        """
        if not valid_person_ids:
            return 0, 0
        ph = ",".join("?" * len(valid_person_ids))
        vals = list(valid_person_ids)
        with self._guard() as conn:
            n_faces = conn.execute(
                f"UPDATE faces SET person_id=NULL"
                f" WHERE person_id IS NOT NULL AND person_id NOT IN ({ph})",
                vals,
            ).rowcount
            n_ann = conn.execute(
                f"DELETE FROM picasa_annotations WHERE person_id NOT IN ({ph})",
                vals,
            ).rowcount
            # Unconditional commit() — cf. restore_orphaned_ignored_faces above.
            conn.commit()
        if n_faces or n_ann:
            logger.info(
                "cleanup_orphan_person_ids: %d face(s) réinitialisées, "
                "%d annotation(s) Picasa supprimées",
                n_faces, n_ann,
            )
        return n_faces, n_ann

    def reset_index(self) -> None:
        """Clears every detection and the index of the analysed photos.
        The named people and the Picasa annotations are preserved;
        the annotations are reset so as to be re-applied after the next
        detection."""
        with self._guard() as conn:
            conn.execute("DELETE FROM faces")
            conn.execute("DELETE FROM indexed_photos")
            conn.execute("DELETE FROM face_index_errors")
            conn.execute("UPDATE picasa_annotations SET consumed=0")
            conn.commit()

    def delete_for_path(self, photo_path: str) -> None:
        """Remove all face data for a deleted photo."""
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            conn.execute("DELETE FROM faces WHERE photo_path=?", (photo_path,))
            conn.execute(
                "DELETE FROM indexed_photos WHERE photo_path=?", (photo_path,)
            )
            conn.execute(
                "DELETE FROM picasa_annotations WHERE photo_path=?", (photo_path,)
            )
            conn.execute(
                "DELETE FROM face_index_errors WHERE photo_path=?", (photo_path,)
            )
            conn.commit()

    def delete_for_paths(self, photo_paths: list[str]) -> None:
        """Deletes the face data of several photos in a single transaction
        (the batch variant of delete_for_path)."""
        if not photo_paths:
            return
        params = [(os.path.normpath(p),) for p in photo_paths]
        with self._guard() as conn:
            conn.executemany("DELETE FROM faces WHERE photo_path=?", params)
            conn.executemany(
                "DELETE FROM indexed_photos WHERE photo_path=?", params
            )
            conn.executemany(
                "DELETE FROM picasa_annotations WHERE photo_path=?", params
            )
            conn.executemany(
                "DELETE FROM face_index_errors WHERE photo_path=?", params
            )
            conn.commit()

    def remap_bboxes_after_save(
        self, photo_path: str, updates: dict, deletions: list,
    ) -> None:
        """After saving an edited photo that overwrites the original file
        (crop/rotation/straightening now baked into the pixels): realigns the
        bboxes of the existing faces in the new pixel frame of reference
        (`updates` = {face_id: (x, y, w, h)}) and purges those that fell out of
        frame (`deletions` = [face_id, ...]). Also puts indexed_photos.rotation
        back to 0: the file is now in its final orientation, there is no
        detection rotation left to compensate for when rebuilding a thumbnail
        (cf. detected_rotation, src/ui/face_panel.py)."""
        photo_path = os.path.normpath(photo_path)
        with self._guard() as conn:
            for face_id, (x, y, w, h) in updates.items():
                conn.execute(
                    "UPDATE faces SET bbox_x=?, bbox_y=?, bbox_w=?, bbox_h=? WHERE id=?",
                    (x, y, w, h, face_id),
                )
            if deletions:
                conn.executemany(
                    "DELETE FROM faces WHERE id=?", [(fid,) for fid in deletions]
                )
            conn.execute(
                "UPDATE indexed_photos SET rotation=0 WHERE photo_path=?",
                (photo_path,),
            )
            conn.commit()

    def update_path(self, old_path: str, new_path: str) -> None:
        """Rename/move a single photo: update photo_path in both tables."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        with self._guard() as conn:
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
            conn.execute(
                "UPDATE face_index_errors SET photo_path=? WHERE photo_path=?",
                (new_path, old_path),
            )
            conn.commit()

    def update_paths_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Rename/move a folder: rewrite every path that starts with old_prefix."""
        old_prefix = os.path.normpath(old_prefix)
        new_prefix = os.path.normpath(new_prefix)
        n = len(old_prefix)
        # os.sep is '\\' on Windows — not a wildcard in SQLite LIKE, so safe as literal
        like_pattern = old_prefix + os.sep + "%"
        with self._guard() as conn:
            for table in ("faces", "indexed_photos", "face_index_errors"):
                conn.execute(
                    f"UPDATE {table}"
                    "  SET photo_path = ? || substr(photo_path, ?)"
                    " WHERE photo_path = ? OR photo_path LIKE ?",
                    (new_prefix, n + 1, old_prefix, like_pattern),
                )
            conn.commit()

    def get_stats(self) -> dict:
        with self._guard() as conn:
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
        return {
            "indexed_photos": indexed,
            "total_faces": faces,
            "named_persons": persons,
            "clusters": clusters,
        }

    def get_recognition_counters(self) -> dict:
        """Detailed counters for the Faces › Counters… menu

        - identified_faces  : faces with an assigned person_id (Picasa or ArcFace), not ignored
        - recognized_faces  : the subset of identified_faces actually recognised by the
                              face analysis (embedding not NULL)
        - pending_faces     : faces carrying an unconfirmed person suggestion
        - unknown_faces     : detected faces, not ignored, without a person or a suggestion
        - picasa_*          : tracking of the annotations imported from Picasa
        """
        with self._guard() as conn:
            def scalar(query: str) -> int:
                return conn.execute(query).fetchone()[0]

            total_faces = scalar("SELECT COUNT(*) FROM faces")
            ignored_faces = scalar("SELECT COUNT(*) FROM faces WHERE ignored=1")
            identified_faces = scalar(
                "SELECT COUNT(*) FROM faces"
                " WHERE person_id IS NOT NULL AND ignored=0"
            )
            recognized_faces = scalar(
                "SELECT COUNT(*) FROM faces"
                " WHERE person_id IS NOT NULL AND embedding IS NOT NULL AND ignored=0"
            )
            pending_faces = scalar(
                "SELECT COUNT(*) FROM faces"
                " WHERE suggestion_person_id IS NOT NULL"
                "   AND person_id IS NULL AND ignored=0"
            )
            unknown_faces = scalar(
                "SELECT COUNT(*) FROM faces"
                " WHERE person_id IS NULL AND suggestion_person_id IS NULL"
                "   AND embedding IS NOT NULL AND ignored=0"
            )
            clusters = scalar(
                "SELECT COUNT(DISTINCT cluster_id) FROM faces"
                " WHERE cluster_id IS NOT NULL"
            )
            picasa_total = scalar("SELECT COUNT(*) FROM picasa_annotations")
            picasa_merged = scalar(
                "SELECT COUNT(*) FROM picasa_annotations WHERE consumed=1"
            )
            picasa_placeholder = scalar(
                "SELECT COUNT(*) FROM picasa_annotations WHERE consumed=0"
            )
        return {
            "total_faces": total_faces,
            "ignored_faces": ignored_faces,
            "identified_faces": identified_faces,
            "recognized_faces": recognized_faces,
            "pending_faces": pending_faces,
            "unknown_faces": unknown_faces,
            "clusters": clusters,
            "picasa_total": picasa_total,
            "picasa_merged": picasa_merged,
            "picasa_placeholder": picasa_placeholder,
        }
