# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import os
import sqlite3
from contextlib import contextmanager
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.models import PhotoInfo, AlbumInfo, PersonInfo
from src.core.app_dirs import APP_DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "catalog.db"

_CREATE_PHOTOS = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    filename TEXT,
    directory TEXT,
    date_taken TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    file_mtime REAL,
    camera_make TEXT,
    camera_model TEXT,
    lens_model TEXT,
    iso INTEGER,
    exposure_time TEXT,
    aperture REAL,
    focal_length REAL,
    has_gps INTEGER DEFAULT 0,
    gps_lat REAL,
    gps_lon REAL,
    is_favorite INTEGER DEFAULT 0,
    tags TEXT,
    indexed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    media_type TEXT DEFAULT 'image',
    duration REAL DEFAULT 0.0,
    duplicate_group_id INTEGER,
    rating INTEGER DEFAULT 0
)
"""
# ⚠ Every new column is added AT THE END of _CREATE_PHOTOS (and through an
# ALTER TABLE migration): _photo_from_row unpacks positionally with *rest — the
# column order of a fresh database must match that of a migrated one.

_CREATE_ALBUMS = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_ALBUM_PHOTOS = """
CREATE TABLE IF NOT EXISTS album_photos (
    album_id INTEGER,
    photo_id INTEGER,
    PRIMARY KEY (album_id, photo_id)
)
"""

_CREATE_PERSONS = """
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _normalize_tags(tags: list[str]) -> list[str]:
    """Cleans a list of tags: strip, reject an empty one or one containing a
    comma (the comma is the storage separator), deduplicate while preserving
    the order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = t.strip()
        if not t or "," in t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    return cleaned


def _photo_from_row(row) -> PhotoInfo:
    (
        id_, path, filename, directory, date_taken, width, height,
        file_size, file_mtime, camera_make, camera_model, lens_model,
        iso, exposure_time, aperture, focal_length,
        has_gps, gps_lat, gps_lon, is_favorite, tags, _indexed_at,
        media_type, duration, *rest
    ) = row
    duplicate_group_id = rest[0] if len(rest) > 0 else None
    rating = int(rest[1] or 0) if len(rest) > 1 else 0

    dt = None
    if date_taken:
        try:
            dt = datetime.fromisoformat(date_taken)
        except ValueError:
            pass

    return PhotoInfo(
        path=path,
        filename=filename or "",
        directory=directory or "",
        date_taken=dt,
        width=width or 0,
        height=height or 0,
        file_size=file_size or 0,
        file_mtime=file_mtime or 0.0,
        camera_make=camera_make or "",
        camera_model=camera_model or "",
        lens_model=lens_model or "",
        iso=iso,
        exposure_time=exposure_time or "",
        aperture=aperture,
        focal_length=focal_length,
        has_gps=bool(has_gps),
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        is_favorite=bool(is_favorite),
        rating=rating,
        tags=tags.split(",") if tags else [],
        id=id_,
        media_type=media_type or "image",
        duration=float(duration or 0.0),
        duplicate_group_id=duplicate_group_id,
    )


class Catalog:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # One SQLite connection per (instance, thread), created once then
        # reused (the ThumbnailCache pattern): every method used to open a
        # fresh connection + 2 PRAGMAs, paid on every query — on the hot
        # paths (scan, view queries, badge) that cost often exceeded the one
        # of the query itself. threading.local is carried by the instance:
        # two Catalogs on the same path (tests) each keep their own
        # connection.
        self._tls = threading.local()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _guard(self):
        """Lock + thread-local connection + a rollback guaranteed on exception.

        Replaces the repeated pattern "with self._lock: conn = self._conn();
        try: … except BaseException: conn.rollback(); raise" (cf. CLAUDE.md,
        the connection pattern): the cached connection must NEVER stay inside
        an open transaction, or every subsequent write fails with
        "database is locked"."""
        with self._lock:
            conn = self._conn()
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise

    def _conn(self) -> sqlite3.Connection:
        """SQLite connection of the current thread, created once per thread.

        The write methods no longer close the connection: on an exception,
        their `except BaseException: conn.rollback()` guard replaces the
        implicit rollback the former close used to provide — a cached
        connection must never stay in the middle of an open transaction (the
        subsequent writes would fail with "database is locked")."""
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
            conn.execute(_CREATE_PHOTOS)
            conn.execute(_CREATE_ALBUMS)
            conn.execute(_CREATE_ALBUM_PHOTOS)
            conn.execute(_CREATE_PERSONS)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_directory ON photos(directory)"
            )
            self._migrate_normalize_paths(conn)
            self._migrate_video_fields(conn)
            self._migrate_duplicate_fields(conn)
            self._migrate_rating_field(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_dup_group ON photos(duplicate_group_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_favorite ON photos(is_favorite)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_rating ON photos(rating)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type)"
            )
            # Safety net at startup: dissolves the groups of a single copy already
            # present in the database (e.g. created before the systematic dissolution
            # was added to delete_photo/delete_photos).
            self._dissolve_singleton_duplicate_groups(conn)
            # Safety net at startup: purges the orphan album_photos entries (a photo
            # removed from the catalog without going through delete_photo/
            # delete_photos, e.g. cleanup_asset_dirs or a path migration before the
            # fix) — otherwise get_albums() overcounts photos that no longer exist.
            conn.execute(
                "DELETE FROM album_photos WHERE photo_id NOT IN (SELECT id FROM photos)"
            )
            conn.commit()

    def _migrate_video_fields(self, conn) -> None:
        for stmt in (
            "ALTER TABLE photos ADD COLUMN media_type TEXT DEFAULT 'image'",
            "ALTER TABLE photos ADD COLUMN duration REAL DEFAULT 0.0",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass  # column already present
        # Backfill: videos added before video support have media_type='image'
        video_exts = (
            ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm",
            ".m4v", ".3gp", ".flv", ".ts", ".mts", ".mpg", ".mpeg",
        )
        like_clauses = " OR ".join(
            f"LOWER(filename) LIKE '%{ext}'" for ext in video_exts
        )
        # A quick check before the UPDATE: if no 'image' photo matches a video
        # extension, the backfill is already done — this avoids paying the cost of a
        # full UPDATE (a write) on every start once the migration has been done.
        if not conn.execute(
            f"SELECT id FROM photos WHERE media_type='image' AND ({like_clauses}) LIMIT 1"
        ).fetchone():
            return
        conn.execute(
            f"UPDATE photos SET media_type='video' WHERE media_type='image' AND ({like_clauses})"
        )
        conn.commit()

    def _migrate_duplicate_fields(self, conn) -> None:
        try:
            conn.execute("ALTER TABLE photos ADD COLUMN duplicate_group_id INTEGER")
        except Exception:
            pass  # column already present

    def _migrate_rating_field(self, conn) -> None:
        try:
            conn.execute("ALTER TABLE photos ADD COLUMN rating INTEGER DEFAULT 0")
        except Exception:
            pass  # column already present

    def _migrate_normalize_paths(self, conn) -> None:
        """Normalises the path separators in the existing data.
        Removes the duplicates that appear after normalisation (keeps the first seen)."""
        # A quick check: if no path contains a '/', the normalisation is already
        # done — this avoids loading every row on every start.
        if not conn.execute(
            "SELECT id FROM photos WHERE instr(path, '/') > 0 LIMIT 1"
        ).fetchone():
            return
        rows = conn.execute("SELECT id, path, directory FROM photos").fetchall()
        if not rows:
            return
        seen: dict[str, int] = {}   # norm_path → the id kept
        to_delete: list[int] = []
        to_update: list[tuple] = []
        for rid, path, directory in rows:
            if not path:
                continue
            norm_path = os.path.normpath(path)
            norm_dir  = os.path.normpath(directory) if directory else ""
            if norm_path in seen:
                to_delete.append(rid)
            else:
                seen[norm_path] = rid
                if norm_path != path or norm_dir != (directory or ""):
                    to_update.append((norm_path, norm_dir, rid))
        for rid in to_delete:
            conn.execute("DELETE FROM album_photos WHERE photo_id=?", (rid,))
            conn.execute("DELETE FROM photos WHERE id=?", (rid,))
        if to_update:
            conn.executemany(
                "UPDATE photos SET path=?, directory=? WHERE id=?", to_update
            )

    def add_or_update_photo(self, photo: PhotoInfo) -> PhotoInfo:
        dt_str = photo.date_taken.isoformat() if photo.date_taken else None
        tags_str = ",".join(photo.tags) if photo.tags else ""
        with self._guard() as conn:
            conn.execute(
                """
                INSERT INTO photos
                    (path, filename, directory, date_taken, width, height,
                     file_size, file_mtime, camera_make, camera_model, lens_model,
                     iso, exposure_time, aperture, focal_length,
                     has_gps, gps_lat, gps_lon, is_favorite, tags,
                     media_type, duration, rating)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    directory=excluded.directory,
                    date_taken=excluded.date_taken,
                    width=excluded.width,
                    height=excluded.height,
                    file_size=excluded.file_size,
                    file_mtime=excluded.file_mtime,
                    camera_make=excluded.camera_make,
                    camera_model=excluded.camera_model,
                    lens_model=excluded.lens_model,
                    iso=excluded.iso,
                    exposure_time=excluded.exposure_time,
                    aperture=excluded.aperture,
                    focal_length=excluded.focal_length,
                    has_gps=excluded.has_gps,
                    gps_lat=excluded.gps_lat,
                    gps_lon=excluded.gps_lon,
                    media_type=excluded.media_type,
                    duration=excluded.duration,
                    indexed_at=CURRENT_TIMESTAMP
                """,
                (
                    photo.path, photo.filename, photo.directory, dt_str,
                    photo.width, photo.height, photo.file_size, photo.file_mtime,
                    photo.camera_make, photo.camera_model, photo.lens_model,
                    photo.iso, photo.exposure_time, photo.aperture, photo.focal_length,
                    int(photo.has_gps), photo.gps_lat, photo.gps_lon,
                    int(photo.is_favorite), tags_str,
                    photo.media_type, photo.duration, int(photo.rating),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM photos WHERE path=?", (photo.path,)
            ).fetchone()
        if row:
            return _photo_from_row(row)
        return photo

    def count_photos_in_folder(self, folder: str) -> int:
        """Returns the number of photos (and videos) indexed under folder (recursively)."""
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._guard() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE directory=? OR directory LIKE ?",
                (folder, like_pattern),
            ).fetchone()
        return row[0] if row else 0

    def get_recursive_photo_counts(self, folders: list[str]) -> dict[str, int]:
        """Returns, for each folder of folders, its number of photos (and videos),
        itself and its subfolders included. A single query grouped by exact folder
        (not a recursive query per requested folder) — used to populate the sidebar
        tree without multiplying the SQLite round trips at every level.

        A trap lived through: a first version filtered the query with a WHERE built
        from one "directory=? OR directory LIKE ?" condition per requested folder —
        a folder with several hundred subfolders then exceeds SQLite's maximum
        expression tree depth (1000, sqlite3.OperationalError: "Expression tree is
        too large"). The query therefore now groups over the WHOLE table (one row
        per distinct folder, not per photo), and the prefix filtering happens in
        Python — a negligible cost even on a large library (the number of distinct
        folders stays far below the number of photos)."""
        if not folders:
            return {}
        normed = [os.path.normpath(f) for f in folders]
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT directory, COUNT(*) FROM photos GROUP BY directory"
            ).fetchall()
        counts = {f: 0 for f in normed}
        for directory, cnt in rows:
            if not directory:
                continue
            for f in normed:
                if directory == f or directory.startswith(f + os.sep):
                    counts[f] += cnt
        return counts

    def get_photos_in_folder(self, folder: str) -> list[PhotoInfo]:
        folder = os.path.normpath(folder)
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE directory=? ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC, filename",
                (folder,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_all_photo_paths(self) -> list[str]:
        """Returns only the paths of every photo (lighter than get_all_photos)."""
        with self._guard() as conn:
            rows = conn.execute("SELECT path FROM photos").fetchall()
        return [r[0] for r in rows]

    def get_all_photos(self) -> list[PhotoInfo]:
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC, filename"
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def search(self, query: str) -> list[PhotoInfo]:
        pattern = f"%{query}%"
        with self._guard() as conn:
            rows = conn.execute(
                """
                SELECT * FROM photos
                WHERE filename LIKE ? OR camera_make LIKE ? OR camera_model LIKE ?
                ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC
                """,
                (pattern, pattern, pattern),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_photo_by_path(self, path: str) -> Optional[PhotoInfo]:
        with self._guard() as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE path=?", (path,)
            ).fetchone()
        return _photo_from_row(row) if row else None

    def update_paths_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Updates every path whose beginning matches old_prefix."""
        n = len(old_prefix)
        like_pattern = old_prefix + os.sep + "%"
        with self._guard() as conn:
            conn.execute(
                """
                UPDATE photos
                SET path      = ? || substr(path,      ?),
                    directory = ? || substr(directory, ?)
                WHERE path = ? OR path LIKE ?
                """,
                (new_prefix, n + 1, new_prefix, n + 1, old_prefix, like_pattern),
            )
            conn.commit()

    def move_photo(self, old_path: str, new_path: str) -> None:
        """Updates the path of a photo file in the catalog."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        new_dir = str(Path(new_path).parent)
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET path=?, directory=?, filename=? WHERE path=?",
                (new_path, new_dir, os.path.basename(new_path), old_path),
            )
            conn.commit()

    def delete_photo(self, path: str) -> None:
        """Removes the photo from the catalog (does not touch the file on disk)."""
        with self._guard() as conn:
            # Must precede the DELETE on photos: the subquery needs the row to still
            # exist to resolve its id (no FK/cascade declared).
            conn.execute(
                "DELETE FROM album_photos WHERE photo_id IN "
                "(SELECT id FROM photos WHERE path=?)",
                (path,),
            )
            conn.execute("DELETE FROM photos WHERE path=?", (path,))
            self._dissolve_singleton_duplicate_groups(conn)
            conn.commit()

    def set_favorite(self, photo_id: int, is_favorite: bool) -> None:
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET is_favorite=? WHERE id=?",
                (int(is_favorite), photo_id),
            )
            conn.commit()

    def set_rating(self, photo_id: int, rating: int) -> None:
        """A 0-5 star rating (0 = remove the rating)."""
        rating = max(0, min(5, int(rating)))
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET rating=? WHERE id=?", (rating, photo_id)
            )
            conn.commit()

    def set_rating_for_ids(self, photo_ids: list[int], rating: int) -> None:
        """Applies the same rating to several photos in a single transaction."""
        if not photo_ids:
            return
        rating = max(0, min(5, int(rating)))
        with self._guard() as conn:
            conn.executemany(
                "UPDATE photos SET rating=? WHERE id=?",
                [(rating, pid) for pid in photo_ids],
            )
            conn.commit()

    def get_photos_min_rating(self, min_rating: int = 1) -> list[PhotoInfo]:
        """Photos rated at least min_rating stars, chronological DESC order."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE rating >= ?"
                " ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC",
                (max(1, int(min_rating)),),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_all_tags(self) -> list[str]:
        """Deduplicated, sorted list of every tag used in the catalog."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tags FROM photos WHERE tags IS NOT NULL AND tags != ''"
            ).fetchall()
        all_tags: set[str] = set()
        for (tags_str,) in rows:
            all_tags.update(t.strip() for t in tags_str.split(",") if t.strip())
        return sorted(all_tags)

    def set_tags(self, photo_id: int, tags: list[str]) -> None:
        """Replaces the complete tag list of a photo."""
        tags_str = ",".join(_normalize_tags(tags))
        with self._guard() as conn:
            conn.execute("UPDATE photos SET tags=? WHERE id=?", (tags_str, photo_id))
            conn.commit()

    def add_tags_to_photos(self, photo_ids: list[int], tags: list[str]) -> None:
        """Adds tags (a union, without duplicates) to each listed photo."""
        new_tags = _normalize_tags(tags)
        if not photo_ids or not new_tags:
            return
        with self._guard() as conn:
            placeholders = ",".join("?" * len(photo_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM photos WHERE id IN ({placeholders})",
                photo_ids,
            ).fetchall()
            updates = []
            for pid, existing in rows:
                current = existing.split(",") if existing else []
                merged = _normalize_tags(current + new_tags)
                updates.append((",".join(merged), pid))
            conn.executemany("UPDATE photos SET tags=? WHERE id=?", updates)
            conn.commit()

    def remove_tag_from_photos(self, photo_ids: list[int], tag: str) -> None:
        """Removes one specific tag from each listed photo (the other tags survive)."""
        if not photo_ids:
            return
        with self._guard() as conn:
            placeholders = ",".join("?" * len(photo_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM photos WHERE id IN ({placeholders})",
                photo_ids,
            ).fetchall()
            updates = []
            for pid, existing in rows:
                current = existing.split(",") if existing else []
                remaining = [t for t in current if t != tag]
                updates.append((",".join(remaining), pid))
            conn.executemany("UPDATE photos SET tags=? WHERE id=?", updates)
            conn.commit()

    def get_photos_by_tag(self, tag: str) -> list[PhotoInfo]:
        """Photos carrying exactly this tag (no substring matching)."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE ',' || tags || ',' LIKE '%,' || ? || ',%'"
                " ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC",
                (tag,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_distinct_cameras(self) -> list[str]:
        """Sorted list of the distinct cameras ("make model"), to prefill the
        camera combo of the advanced search."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT DISTINCT camera_make, camera_model FROM photos"
                " WHERE COALESCE(camera_model, '') != ''"
            ).fetchall()
        labels: set[str] = set()
        for make, model in rows:
            label = f"{make} {model}".strip() if make else (model or "").strip()
            if label:
                labels.add(label)
        return sorted(labels)

    def search_advanced(self, criteria: dict) -> list[PhotoInfo]:
        """Multi-criteria search (dates, camera, folder, minimum rating, tags,
        favourites, media type). The person is NOT a SQL criterion here — two
        separate databases (catalog.db / faces.db), the intersection with
        face_db.get_photos_for_person() happens on the caller side (MainWindow)."""
        clauses: list[str] = []
        params: list = []
        date_expr = "COALESCE(date_taken, datetime(file_mtime, 'unixepoch'))"

        date_from = criteria.get("date_from")
        if date_from:
            clauses.append(f"{date_expr} >= ?")
            params.append(str(date_from))
        date_to = criteria.get("date_to")
        if date_to:
            clauses.append(f"{date_expr} <= ?")
            params.append(f"{date_to}T23:59:59")

        camera = criteria.get("camera")
        if camera:
            clauses.append("(camera_make || ' ' || camera_model) LIKE ?")
            params.append(f"%{camera}%")

        directory = criteria.get("directory")
        if directory:
            d = os.path.normpath(directory)
            clauses.append("(directory = ? OR directory LIKE ?)")
            params.extend([d, d + os.sep + "%"])

        min_rating = criteria.get("min_rating")
        if min_rating:
            clauses.append("rating >= ?")
            params.append(int(min_rating))

        if criteria.get("favorites_only"):
            clauses.append("is_favorite=1")

        media_type = criteria.get("media_type")
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)

        for tag in criteria.get("tags") or []:
            clauses.append("',' || tags || ',' LIKE '%,' || ? || ',%'")
            params.append(tag)

        where = " AND ".join(clauses) if clauses else "1=1"
        with self._guard() as conn:
            rows = conn.execute(
                f"SELECT * FROM photos WHERE {where} ORDER BY {date_expr} DESC",
                params,
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_albums(self) -> list[AlbumInfo]:
        with self._guard() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.name, a.description,
                       COUNT(ap.photo_id) as photo_count
                FROM albums a
                LEFT JOIN album_photos ap ON a.id = ap.album_id
                GROUP BY a.id
                ORDER BY a.name
                """
            ).fetchall()
        return [AlbumInfo(name=r[1], id=r[0], description=r[2], photo_count=r[3]) for r in rows]

    def create_album(self, name: str) -> AlbumInfo:
        with self._guard() as conn:
            cursor = conn.execute(
                "INSERT INTO albums (name) VALUES (?)", (name,)
            )
            conn.commit()
            album_id = cursor.lastrowid
        return AlbumInfo(name=name, id=album_id)

    def delete_album(self, album_id: int) -> None:
        """Deletes an album and its content (album_photos). The photos themselves
        are not affected: only the association with the album is removed."""
        with self._guard() as conn:
            conn.execute("DELETE FROM album_photos WHERE album_id = ?", (album_id,))
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            conn.commit()

    def add_photo_to_album(self, album_id: int, photo_id: int) -> None:
        with self._guard() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?,?)",
                (album_id, photo_id),
            )
            conn.commit()

    def add_photos_to_album(self, album_id: int, photo_ids: list[int]) -> int:
        """Adds several photos to an album in a single transaction.
        Returns the number of photos actually added (already present ones ignored)."""
        if not photo_ids:
            return 0
        with self._guard() as conn:
            # total_changes (and not SELECT changes()): executemany otherwise
            # only reports the last row.
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?,?)",
                [(album_id, pid) for pid in photo_ids],
            )
            added = conn.total_changes - before
            conn.commit()
        return added

    def remove_photo_from_album(self, album_id: int, photo_id: int) -> None:
        """Removes a photo from an album (the file and the photo itself are untouched)."""
        with self._guard() as conn:
            conn.execute(
                "DELETE FROM album_photos WHERE album_id=? AND photo_id=?",
                (album_id, photo_id),
            )
            conn.commit()

    def remove_photos_from_album(self, album_id: int, photo_ids: list[int]) -> None:
        """Removes several photos from an album in a single DELETE (files and
        photos untouched)."""
        if not photo_ids:
            return
        with self._guard() as conn:
            placeholders = ",".join("?" * len(photo_ids))
            conn.execute(
                f"DELETE FROM album_photos WHERE album_id=?"
                f" AND photo_id IN ({placeholders})",
                (album_id, *photo_ids),
            )
            conn.commit()

    def get_photos_in_album(self, album_id: int) -> list[PhotoInfo]:
        with self._guard() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM photos p
                JOIN album_photos ap ON p.id = ap.photo_id
                WHERE ap.album_id = ?
                ORDER BY COALESCE(p.date_taken, datetime(p.file_mtime, 'unixepoch')) DESC
                """,
                (album_id,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_favorites(self) -> list[PhotoInfo]:
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE is_favorite=1 ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC"
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_videos(self) -> list[PhotoInfo]:
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE media_type='video' ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC"
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_stats(self) -> dict:
        with self._guard() as conn:
            total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            total_size = conn.execute("SELECT SUM(file_size) FROM photos").fetchone()[0] or 0
            folders = conn.execute("SELECT COUNT(DISTINCT directory) FROM photos").fetchone()[0]
        return {"total_photos": total, "total_size": total_size, "folders": folders}

    def rename_photo(self, old_path: str, new_path: str) -> bool:
        new_p = Path(new_path)
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET path=?, filename=?, directory=? WHERE path=?",
                (new_path, new_p.name, str(new_p.parent), old_path),
            )
            conn.commit()
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def get_known_mtimes(self, folder: str) -> dict[str, float]:
        """Returns {path: mtime} for all photos at or below folder (recursive).

        Used by the scanner to skip files whose mtime hasn't changed.
        """
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path, file_mtime FROM photos "
                "WHERE directory=? OR directory LIKE ?",
                (folder, like_pattern),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_all_paths_under(self, folder: str) -> set[str]:
        """Returns the set of all indexed paths at or below folder (recursive).

        Used by the scanner to detect entries whose files no longer exist.
        """
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path FROM photos WHERE directory=? OR directory LIKE ?",
                (folder, like_pattern),
            ).fetchall()
        return {r[0] for r in rows}

    def cleanup_asset_dirs(self) -> list[str]:
        """Removes from the catalog the files inside *_assets directories (software
        assets of the Lightroom/Capture One kind). Returns the removed paths."""
        to_delete: list[str] = []
        with self._guard() as conn:
            rows = conn.execute("SELECT path FROM photos").fetchall()
            to_delete = [
                r[0] for r in rows
                if any(part.endswith("_assets") for part in Path(r[0]).parts)
            ]
            if to_delete:
                conn.executemany(
                    "DELETE FROM album_photos WHERE photo_id IN "
                    "(SELECT id FROM photos WHERE path=?)",
                    [(p,) for p in to_delete],
                )
                conn.executemany("DELETE FROM photos WHERE path=?",
                                 [(p,) for p in to_delete])
                conn.commit()
        if to_delete:
            logger.info("cleanup_asset_dirs : %d entrée(s) supprimée(s)", len(to_delete))
        return to_delete

    def delete_photos(self, paths: list[str]) -> None:
        """Removes in a single transaction the entries whose files have disappeared."""
        if not paths:
            return
        with self._guard() as conn:
            conn.executemany(
                "DELETE FROM album_photos WHERE photo_id IN "
                "(SELECT id FROM photos WHERE path=?)",
                [(p,) for p in paths],
            )
            conn.executemany(
                "DELETE FROM photos WHERE path=?", [(p,) for p in paths]
            )
            self._dissolve_singleton_duplicate_groups(conn)
            conn.commit()

    def _dissolve_singleton_duplicate_groups(self, conn) -> None:
        """Dissolves any duplicate group that has fallen back to 0 or 1 copy after a
        deletion. An invariant needed whatever the deletion path taken (manual
        deletion, cleanup of ghost entries by the scanner, folder purge...) — see
        dedup_singleton_groups_any_delete_path in memory for the context: before
        this fix only the manual deletion through the UI dissolved those groups,
        letting groups of a single copy reappear after a scan that removes
        vanished files."""
        conn.execute(
            """
            UPDATE photos SET duplicate_group_id = NULL
            WHERE duplicate_group_id IN (
                SELECT duplicate_group_id FROM photos
                WHERE duplicate_group_id IS NOT NULL
                GROUP BY duplicate_group_id
                HAVING COUNT(*) < 2
            )
            """
        )

    # ------------------------------------------------------------------ duplicates

    def set_duplicate_groups(self, assignments: dict) -> None:
        """Records the detected duplicate groups. assignments = {path: group_id}.

        Also dissolves any group fallen back to 0/1 copy by this call: a
        DuplicateDetectorThread running at the time of a deletion computes its
        assignments on a state captured before it, and can therefore rewrite here
        a member left alone in its former group — without this safeguard the group
        of 1 reappears until the next delete_photo(s)/restart (cf.
        dedup_singleton_groups_any_delete_path in memory, whose path this one was
        not covered by)."""
        if not assignments:
            return
        with self._guard() as conn:
            conn.executemany(
                "UPDATE photos SET duplicate_group_id=? WHERE path=?",
                [(gid, path) for path, gid in assignments.items()],
            )
            self._dissolve_singleton_duplicate_groups(conn)
            conn.commit()

    def clear_duplicate_groups(self) -> None:
        """Clears every duplicate marker."""
        with self._guard() as conn:
            conn.execute("UPDATE photos SET duplicate_group_id=NULL")
            conn.commit()

    def get_duplicates_for_group(self, group_id: int) -> list:
        """Returns every photo of the given duplicate group."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE duplicate_group_id=? "
                "ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')), filename",
                (group_id,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_duplicate_groups(self) -> dict:
        """Returns every duplicate group {group_id: [PhotoInfo, ...]}."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE duplicate_group_id IS NOT NULL "
                "ORDER BY duplicate_group_id, "
                "COALESCE(date_taken, datetime(file_mtime, 'unixepoch')), filename"
            ).fetchall()
        groups: dict[int, list] = {}
        for row in rows:
            photo = _photo_from_row(row)
            groups.setdefault(photo.duplicate_group_id, []).append(photo)
        return groups

    def get_duplicate_group_assignments(self) -> dict:
        """{path: group_id} for every photo currently grouped — a lightweight
        version of get_duplicate_groups() (no complete PhotoInfo), used to seed an
        incremental detection pass."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path, duplicate_group_id FROM photos "
                "WHERE duplicate_group_id IS NOT NULL"
            ).fetchall()
        return {path: gid for path, gid in rows}

    def count_duplicate_groups(self) -> int:
        """Number of distinct duplicate groups currently recorded."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT duplicate_group_id) FROM photos "
                "WHERE duplicate_group_id IS NOT NULL"
            ).fetchone()
        return row[0] if row else 0

    def ignore_duplicate_group(self, group_id: int) -> None:
        """Dissolves a duplicate group. With the incremental detection
        (DuplicateDetectorThread never recompares two files both already
        checked during an earlier full pass, cf.
        dedup_cache.compared_tier1/2), that group will not be recreated as
        long as none of its members changes — a new file matching one of them
        is, on the other hand, still detected normally."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET duplicate_group_id=NULL WHERE duplicate_group_id=?",
                (group_id,),
            )
            conn.commit()

    def get_all_photo_paths_for_dedup(self) -> list:
        """Returns the list of every photo path for duplicate detection."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path FROM photos ORDER BY path"
            ).fetchall()
        return [r[0] for r in rows]

    def get_photo_dates_for_dedup(self) -> dict:
        """Returns {path: datetime|None} (the EXIF date_taken, with sub-second
        precision when available) for every catalogued path — used by duplicate
        detection so as not to merge two photos whose capture date differs
        (e.g. a burst: the same scene, different capture instants)."""
        with self._guard() as conn:
            rows = conn.execute("SELECT path, date_taken FROM photos").fetchall()
        result: dict = {}
        for path, date_taken in rows:
            dt = None
            if date_taken:
                try:
                    dt = datetime.fromisoformat(date_taken)
                except ValueError:
                    pass
            result[path] = dt
        return result

    def get_photos_by_paths(self, paths: list[str]) -> list[PhotoInfo]:
        """Returns PhotoInfo objects for the given paths (in catalog order)."""
        if not paths:
            return []
        placeholders = ",".join("?" * len(paths))
        with self._guard() as conn:
            rows = conn.execute(
                f"SELECT * FROM photos WHERE path IN ({placeholders})"
                " ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC, filename",
                paths,
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    # ------------------------------------------------------------------ persons

    def get_persons(self) -> list[PersonInfo]:
        """Returns all persons ordered by name. photo_count is 0; call face_db.enrich_persons()."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT id, name FROM persons ORDER BY name"
            ).fetchall()
        return [PersonInfo(name=r[1], id=r[0]) for r in rows]

    def get_person(self, person_id: int) -> "PersonInfo | None":
        """Returns a single PersonInfo by id, or None if not found."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT id, name FROM persons WHERE id=?", (person_id,)
            ).fetchone()
        return PersonInfo(name=row[1], id=row[0]) if row else None

    def create_person(self, name: str) -> PersonInfo:
        with self._guard() as conn:
            cur = conn.execute(
                "INSERT INTO persons (name) VALUES (?)", (name,)
            )
            conn.commit()
            person_id = cur.lastrowid
        return PersonInfo(name=name, id=person_id)

    def rename_person(self, person_id: int, name: str) -> None:
        with self._guard() as conn:
            conn.execute(
                "UPDATE persons SET name=? WHERE id=?", (name, person_id)
            )
            conn.commit()

    def delete_person(self, person_id: int) -> None:
        with self._guard() as conn:
            conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
            conn.commit()
