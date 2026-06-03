import sqlite3
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.models import PhotoInfo, AlbumInfo

logger = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".photomanager" / "catalog.db"

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
    indexed_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

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


def _photo_from_row(row) -> PhotoInfo:
    (
        id_, path, filename, directory, date_taken, width, height,
        file_size, file_mtime, camera_make, camera_model, lens_model,
        iso, exposure_time, aperture, focal_length,
        has_gps, gps_lat, gps_lon, is_favorite, tags, _indexed_at
    ) = row

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
        tags=tags.split(",") if tags else [],
        id=id_,
    )


class Catalog:
    def __init__(self, db_path: str | Path = _DB_PATH):
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
                conn.execute(_CREATE_PHOTOS)
                conn.execute(_CREATE_ALBUMS)
                conn.execute(_CREATE_ALBUM_PHOTOS)
                conn.execute(_CREATE_PERSONS)
                conn.commit()
            finally:
                conn.close()

    def add_or_update_photo(self, photo: PhotoInfo) -> PhotoInfo:
        dt_str = photo.date_taken.isoformat() if photo.date_taken else None
        tags_str = ",".join(photo.tags) if photo.tags else ""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO photos
                        (path, filename, directory, date_taken, width, height,
                         file_size, file_mtime, camera_make, camera_model, lens_model,
                         iso, exposure_time, aperture, focal_length,
                         has_gps, gps_lat, gps_lon, is_favorite, tags)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        tags=excluded.tags,
                        indexed_at=CURRENT_TIMESTAMP
                    """,
                    (
                        photo.path, photo.filename, photo.directory, dt_str,
                        photo.width, photo.height, photo.file_size, photo.file_mtime,
                        photo.camera_make, photo.camera_model, photo.lens_model,
                        photo.iso, photo.exposure_time, photo.aperture, photo.focal_length,
                        int(photo.has_gps), photo.gps_lat, photo.gps_lon,
                        int(photo.is_favorite), tags_str,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM photos WHERE path=?", (photo.path,)
                ).fetchone()
            finally:
                conn.close()
        if row:
            return _photo_from_row(row)
        return photo

    def get_photos_in_folder(self, folder: str) -> list[PhotoInfo]:
        folder = folder.rstrip("/\\")
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM photos WHERE directory=? ORDER BY date_taken DESC, filename",
                    (folder,),
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    def get_all_photos(self) -> list[PhotoInfo]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM photos ORDER BY date_taken DESC, filename"
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    def search(self, query: str) -> list[PhotoInfo]:
        pattern = f"%{query}%"
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM photos
                    WHERE filename LIKE ? OR camera_make LIKE ? OR camera_model LIKE ?
                    ORDER BY date_taken DESC
                    """,
                    (pattern, pattern, pattern),
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    def get_photo_by_path(self, path: str) -> Optional[PhotoInfo]:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM photos WHERE path=?", (path,)
                ).fetchone()
            finally:
                conn.close()
        return _photo_from_row(row) if row else None

    def set_favorite(self, photo_id: int, is_favorite: bool) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE photos SET is_favorite=? WHERE id=?",
                    (int(is_favorite), photo_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_albums(self) -> list[AlbumInfo]:
        with self._lock:
            conn = self._conn()
            try:
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
            finally:
                conn.close()
        return [AlbumInfo(name=r[1], id=r[0], description=r[2], photo_count=r[3]) for r in rows]

    def create_album(self, name: str) -> AlbumInfo:
        with self._lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    "INSERT INTO albums (name) VALUES (?)", (name,)
                )
                conn.commit()
                album_id = cursor.lastrowid
            finally:
                conn.close()
        return AlbumInfo(name=name, id=album_id)

    def add_photo_to_album(self, album_id: int, photo_id: int) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?,?)",
                    (album_id, photo_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_photos_in_album(self, album_id: int) -> list[PhotoInfo]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT p.* FROM photos p
                    JOIN album_photos ap ON p.id = ap.photo_id
                    WHERE ap.album_id = ?
                    ORDER BY p.date_taken DESC
                    """,
                    (album_id,),
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    def get_favorites(self) -> list[PhotoInfo]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM photos WHERE is_favorite=1 ORDER BY date_taken DESC"
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    def get_stats(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
                total_size = conn.execute("SELECT SUM(file_size) FROM photos").fetchone()[0] or 0
                folders = conn.execute("SELECT COUNT(DISTINCT directory) FROM photos").fetchone()[0]
            finally:
                conn.close()
        return {"total_photos": total, "total_size": total_size, "folders": folders}

    def get_known_mtimes(self, folder: str) -> dict[str, float]:
        """Returns {path: mtime} for all photos in a folder — used by scanner to skip unchanged files."""
        folder = folder.rstrip("/\\")
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT path, file_mtime FROM photos WHERE directory=?", (folder,)
                ).fetchall()
            finally:
                conn.close()
        return {r[0]: r[1] for r in rows}
