# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Cache persistant des empreintes calculées par la détection de doublons
(Tier 1 pHash + Tier 2 ORB), pour permettre l'interruption et la reprise
d'un scan entre deux démarrages de l'application sans tout recalculer.

Fichier SQLite dédié (`dedup_cache.db`), sur le même modèle que
`thumbnail_cache.py` — pas de connexions par-thread ici : le thread appelant
(DuplicateDetectorThread) est le seul consommateur, tout le calcul parallèle
interne (ThreadPoolExecutor) ne fait que renvoyer des résultats en mémoire,
jamais d'accès SQLite direct depuis les workers."""
import logging
import sqlite3
from pathlib import Path

from src.core.app_dirs import APP_DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "dedup_cache.db"

# Incrémenter si des constantes affectant ce qui est calculé/stocké changent
# (ex. _ORB_MAX_KP dans duplicate_detector.py) — sinon d'anciennes entrées de
# cache seraient silencieusement réutilisées comme si elles avaient été
# calculées avec les constantes actuelles.
_CACHE_VERSION = "1"

# Limite SQLite par défaut : 999 paramètres liés par requête.
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
"""


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class DedupCache:
    """Cache SQLite des empreintes Tier 1 (pHash) et Tier 2 (ORB) par photo,
    clé par chemin + mtime. Usage : instancier, `open()` depuis le thread qui
    fera le scan, utiliser, `close()` en fin de scan (idéalement dans un
    `finally`)."""

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

    def get_orb_features(self, paths: list[str]) -> dict[str, tuple]:
        """{path: (file_mtime, width, height, keypoints_xy_blob, descriptors_blob, image_jpeg_blob)}"""
        result: dict[str, tuple] = {}
        for chunk in _chunks(paths, _IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT path, file_mtime, width, height, keypoints_xy, descriptors, image_jpeg "
                f"FROM orb_features WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for path, mtime, w, h, kp_xy, des, img in rows:
                result[path] = (mtime, w, h, kp_xy, des, img)
        return result

    def store_orb_features(self, rows: list[tuple]) -> None:
        """rows : (path, file_mtime, width, height, keypoints_xy_blob, descriptors_blob, image_jpeg_blob)"""
        if not rows:
            return
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO orb_features
                (path, file_mtime, width, height, keypoints_xy, descriptors, image_jpeg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._conn.commit()

    # ── Tier 1/2 : complétude des comparaisons (incrémentalité) ────────────────

    def _get_compared(self, table: str, paths: list[str]) -> dict[str, float]:
        """{path: file_mtime} — chemins déjà comparés à tout le reste de la
        bibliothèque connue lors d'une passe complète antérieure."""
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

    # ── Maintenance ───────────────────────────────────────────────────────────

    def purge_missing(self, keep_paths: set[str]) -> int:
        """Supprime de toutes les tables toute entrée dont le chemin n'est pas
        dans keep_paths (photos supprimées/déplacées/retirées de la
        bibliothèque depuis le dernier scan). Retourne le nombre de lignes
        supprimées (fingerprints + orb_features uniquement, à titre
        indicatif — compared_tier1/2 sont purgées de la même façon mais ne
        comptent pas dans le total retourné, comme avant cette extension)."""
        cached_paths: set[str] = set()
        for row in self._conn.execute("SELECT path FROM fingerprints"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM orb_features"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM compared_tier1"):
            cached_paths.add(row[0])
        for row in self._conn.execute("SELECT path FROM compared_tier2"):
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
        self._conn.commit()
        return deleted
