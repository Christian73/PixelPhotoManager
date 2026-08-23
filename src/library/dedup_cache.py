# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Persistent cache of the fingerprints computed by duplicate detection
(Tier 1 pHash + Tier 2 ORB), so that a scan can be interrupted and resumed
between two starts of the application without recomputing everything.

A dedicated SQLite file (`dedup_cache.db`), on the same model as
`thumbnail_cache.py` — no per-thread connections here: the calling thread
(DuplicateDetectorThread) is the only consumer, and all the internal parallel
computation (ThreadPoolExecutor) only returns results in memory, never a
direct SQLite access from the workers."""
import logging
import sqlite3
import time
from pathlib import Path

from src.core.app_dirs import APP_DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "dedup_cache.db"

# Increment when constants affecting what is computed/stored change
# (e.g. _ORB_MAX_KP in duplicate_detector.py) — otherwise old cache entries
# would silently be reused as if they had been computed with the current
# constants.
_CACHE_VERSION = "1"

# SQLite default limit: 999 bound parameters per query.
_IN_CLAUSE_CHUNK = 900

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL,
    phash_hex  TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    micro      BLOB NOT NULL
);

-- image_jpeg : colonne héritée, conservée pour ne pas invalider les caches
-- existants (leur reconstruction coûte des heures) mais **plus jamais lue** ni
-- écrite depuis que le Tier 2 recharge son image de travail à la demande
-- depuis le fichier d'origine (cf. duplicate_detector._GrayImageCache) : la
-- relire systématiquement coûtait un décodage JPEG par photo à chaque
-- démarrage, pour une image qui ne sert qu'aux rares paires atteignant la
-- vérification post-RANSAC. Placée en dernière colonne : SQLite stocke le
-- débordement d'un enregistrement en fin de record, les SELECT ci-dessous
-- (qui ne la demandent pas) n'ont donc pas à parcourir les pages de
-- débordement.
CREATE TABLE IF NOT EXISTS orb_features (
    path         TEXT PRIMARY KEY,
    file_mtime   REAL NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    keypoints_xy BLOB NOT NULL,
    descriptors  BLOB NOT NULL,
    image_jpeg   BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS compared_tier1 (
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS compared_tier2 (
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS corrupted_files (
    path        TEXT PRIMARY KEY,
    detected_at REAL NOT NULL
);
"""


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class DedupCache:
    """SQLite cache of the Tier 1 (pHash) and Tier 2 (ORB) fingerprints per
    photo, keyed by path + mtime. Usage: instantiate, `open()` from the thread
    that will run the scan, use it, `close()` at the end of the scan (ideally
    in a `finally`)."""

    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=500")
            conn.executescript(_CREATE_TABLES)
            conn.commit()

            row = conn.execute(
                "SELECT value FROM meta WHERE key='cache_version'"
            ).fetchone()
            if row is None or row[0] != _CACHE_VERSION:
                logger.info(
                    "dedup_cache : version de cache obsolète (%s -> %s), purge complète.",
                    row[0] if row else None, _CACHE_VERSION,
                )
                conn.execute("DELETE FROM fingerprints")
                conn.execute("DELETE FROM orb_features")
                conn.execute("DELETE FROM compared_tier1")
                conn.execute("DELETE FROM compared_tier2")
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('cache_version', ?)",
                    (_CACHE_VERSION,),
                )
                conn.commit()
        finally:
            conn.close()

    def open(self) -> None:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-2048")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Tier 1 : fingerprints (pHash) ────────────────────────────────────────

    def get_fingerprints(self, paths: list[str]) -> dict[str, tuple]:
        """{path: (file_mtime, phash_hex, width, height, micro_blob)}"""
        result: dict[str, tuple] = {}
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT path, file_mtime, phash_hex, width, height, micro "
                f"FROM fingerprints WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for path, mtime, phash_hex, w, h, micro in rows:
                result[path] = (mtime, phash_hex, w, h, micro)
        return result

    def store_fingerprints(self, rows: list[tuple]) -> None:
        """rows : (path, file_mtime, phash_hex, width, height, micro_blob)"""
        if not rows:
            return
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO fingerprints
                (path, file_mtime, phash_hex, width, height, micro)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._conn.commit()

    # ── Tier 2 : ORB features ────────────────────────────────────────────────

    def get_orb_meta(self, paths: list[str]) -> dict[str, tuple[float, int, int]]:
        """{path: (file_mtime, width, height)} — the metadata alone, without a
        single blob. It lets Tier 2 decide *before* any loading which photos
        really take part in a pair to evaluate (cache validity + area for the
        `_ORB_AREA_FACTOR` prefilter), so that only those are then loaded
        through `get_orb_descriptors()`."""
        result: dict[str, tuple[float, int, int]] = {}
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT path, file_mtime, width, height "
                f"FROM orb_features WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for path, mtime, w, h in rows:
                result[path] = (mtime, w, h)
        return result

    def get_orb_descriptors(self, paths: list[str]) -> dict[str, tuple]:
        """{path: (file_mtime, width, height, keypoints_xy_blob, descriptors_blob)}

        The legacy `image_jpeg` column is deliberately not selected
        (cf. the comment on the schema)."""
        result: dict[str, tuple] = {}
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT path, file_mtime, width, height, keypoints_xy, descriptors "
                f"FROM orb_features WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for path, mtime, w, h, kp_xy, des in rows:
                result[path] = (mtime, w, h, kp_xy, des)
        return result

    def store_orb_features(self, rows: list[tuple]) -> None:
        """rows: (path, file_mtime, width, height, keypoints_xy_blob, descriptors_blob)

        `image_jpeg` is written empty: a legacy NOT NULL column, never read
        again (cf. the comment on the schema)."""
        if not rows:
            return
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO orb_features
                (path, file_mtime, width, height, keypoints_xy, descriptors, image_jpeg)
            VALUES (?, ?, ?, ?, ?, ?, x'')
            """,
            rows,
        )
        self._conn.commit()

    # ── Tier 1/2: completeness of the comparisons (incrementality) ───────────

    def _get_compared(self, table: str, paths: list[str]) -> dict[str, float]:
        """{path: file_mtime} — paths already compared with all the rest of the
        known library during an earlier full pass."""
        result: dict[str, float] = {}
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT path, file_mtime FROM {table} WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for path, mtime in rows:
                result[path] = mtime
        return result

    def _store_compared(self, table: str, rows: list[tuple[str, float]]) -> None:
        """rows : (path, file_mtime)"""
        if not rows:
            return
        self._conn.executemany(
            f"INSERT OR REPLACE INTO {table} (path, file_mtime) VALUES (?, ?)",
            rows,
        )
        self._conn.commit()

    def get_compared_tier1(self, paths: list[str]) -> dict[str, float]:
        return self._get_compared("compared_tier1", paths)

    def store_compared_tier1(self, rows: list[tuple[str, float]]) -> None:
        self._store_compared("compared_tier1", rows)

    def get_compared_tier2(self, paths: list[str]) -> dict[str, float]:
        return self._get_compared("compared_tier2", paths)

    def store_compared_tier2(self, rows: list[tuple[str, float]]) -> None:
        self._store_compared("compared_tier2", rows)

    def remove_compared(self, paths) -> None:
        """Removes specific paths from compared_tier1 AND compared_tier2, to force
        a full recomparison of them (Tier 1 + Tier 2) against the rest of the
        library on the next pass — the fingerprints/features already computed
        (fingerprints/orb_features) stay valid and are not touched, only the
        "already compared" mark is removed. Used by the one-off migrations that
        invalidate duplicate groups already formed
        (cf. MainWindow._migrate_dissolve_date_conflicted_duplicate_groups)."""
        paths = list(paths)
        if not paths:
            return
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            self._conn.execute(
                f"DELETE FROM compared_tier1 WHERE path IN ({placeholders})", chunk
            )
            self._conn.execute(
                f"DELETE FROM compared_tier2 WHERE path IN ({placeholders})", chunk
            )
        self._conn.commit()

    # ── Corrupted files ──────────────────────────────────────────────────────
    # Persisted here rather than only kept in memory (MainWindow) so as to
    # survive a restart of the application. A corrupted file never gets a
    # fingerprint/features (cf. duplicate_detector.py), so it is systematically
    # retried on every pass and falls back into that same set — the persisted
    # list is therefore replaced in full at the end of a pass
    # (`replace_corrupted_paths`), which keeps it up to date automatically
    # (repaired or deleted -> gone on the next pass) with no dedicated
    # reconciliation logic.

    def get_corrupted_paths(self) -> list[str]:
        return [row[0] for row in self._conn.execute(
            "SELECT path FROM corrupted_files ORDER BY path"
        )]

    def replace_corrupted_paths(self, paths) -> None:
        """Replaces the persisted list in full with `paths` (the complete, up to
        date state at the end of a Tier 1 + Tier 2 pass)."""
        now = time.time()
        self._conn.execute("DELETE FROM corrupted_files")
        if paths:
            self._conn.executemany(
                "INSERT OR REPLACE INTO corrupted_files (path, detected_at) VALUES (?, ?)",
                [(p, now) for p in paths],
            )
        self._conn.commit()

    def remove_corrupted_paths(self, paths) -> None:
        """Removes specific paths (a successful repair or manual deletion)
        without waiting for the end of the next full pass."""
        paths = list(paths)
        if not paths:
            return
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            self._conn.execute(
                f"DELETE FROM corrupted_files WHERE path IN ({placeholders})", chunk
            )
        self._conn.commit()

    # ── Maintenance ───────────────────────────────────────────────────────────

    def purge_missing(self, keep_paths: set[str]) -> int:
        """Removes from every table any entry whose path is not in keep_paths
        (photos deleted/moved/removed from the library since the last scan).
        Returns the number of rows deleted (fingerprints + orb_features only,
        as an indication — compared_tier1/2 and corrupted_files are purged the
        same way but do not count towards the returned total, as before this
        extension). A corrupted file never has a fingerprint (cf.
        `replace_corrupted_paths`): without including it explicitly here, its
        path would never be seen as "known" and therefore never purged from
        corrupted_files after the file/folder is deleted."""
        cached_paths: set[str] = set()
        for row in self._conn.execute("SELECT path FROM fingerprints"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM orb_features"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM compared_tier1"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM compared_tier2"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM corrupted_files"):
            cached_paths.add(row[0])

        stale = cached_paths - keep_paths
        if not stale:
            return 0

        stale_list = list(stale)
        deleted = 0
        for chunk in _chunks(stale_list, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            cur = self._conn.execute(
                f"DELETE FROM fingerprints WHERE path IN ({placeholders})", chunk
            )
            deleted += cur.rowcount if cur.rowcount > 0 else 0
            self._conn.execute(
                f"DELETE FROM orb_features WHERE path IN ({placeholders})", chunk
            )
            self._conn.execute(
                f"DELETE FROM compared_tier1 WHERE path IN ({placeholders})", chunk
            )
            self._conn.execute(
                f"DELETE FROM compared_tier2 WHERE path IN ({placeholders})", chunk
            )
            self._conn.execute(
                f"DELETE FROM corrupted_files WHERE path IN ({placeholders})", chunk
            )
        self._conn.commit()
        return deleted
