import os
import sqlite3
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
    duration REAL DEFAULT 0.0
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
        has_gps, gps_lat, gps_lon, is_favorite, tags, _indexed_at,
        media_type, duration,
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
        media_type=media_type or "image",
        duration=float(duration or 0.0),
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
                self._migrate_normalize_paths(conn)
                self._migrate_video_fields(conn)
                conn.commit()
            finally:
                conn.close()

    def _migrate_video_fields(self, conn) -> None:
        for stmt in (
            "ALTER TABLE photos ADD COLUMN media_type TEXT DEFAULT 'image'",
            "ALTER TABLE photos ADD COLUMN duration REAL DEFAULT 0.0",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass  # colonne déjà présente

    def _migrate_normalize_paths(self, conn) -> None:
        """Normalise les séparateurs de chemin dans les données existantes.
        Supprime les doublons qui apparaissent après normalisation (garde le premier vu)."""
        # Vérification rapide : s'il n'existe aucun chemin avec '/', la normalisation
        # est déjà faite — évite de charger toutes les lignes à chaque démarrage.
        if not conn.execute(
            "SELECT id FROM photos WHERE instr(path, '/') > 0 LIMIT 1"
        ).fetchone():
            return
        rows = conn.execute("SELECT id, path, directory FROM photos").fetchall()
        if not rows:
            return
        seen: dict[str, int] = {}   # norm_path → id conservé
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
            conn.execute("DELETE FROM photos WHERE id=?", (rid,))
        if to_update:
            conn.executemany(
                "UPDATE photos SET path=?, directory=? WHERE id=?", to_update
            )

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
                         has_gps, gps_lat, gps_lon, is_favorite, tags,
                         media_type, duration)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        photo.media_type, photo.duration,
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

    def count_photos_in_folder(self, folder: str) -> int:
        """Retourne le nombre de photos (et vidéos) indexées sous folder (récursivement)."""
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE directory=? OR directory LIKE ?",
                    (folder, like_pattern),
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else 0

    def get_photos_in_folder(self, folder: str) -> list[PhotoInfo]:
        folder = os.path.normpath(folder)
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

    def update_paths_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Met à jour tous les chemins dont le début correspond à old_prefix."""
        n = len(old_prefix)
        like_pattern = old_prefix + os.sep + "%"
        with self._lock:
            conn = self._conn()
            try:
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
            finally:
                conn.close()

    def move_photo(self, old_path: str, new_path: str) -> None:
        """Met à jour le chemin d'un fichier photo dans le catalogue."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        new_dir = str(Path(new_path).parent)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE photos SET path=?, directory=?, filename=? WHERE path=?",
                    (new_path, new_dir, os.path.basename(new_path), old_path),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_photo(self, path: str) -> None:
        """Supprime la photo du catalogue (ne touche pas au fichier disque)."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM photos WHERE path=?", (path,))
                conn.commit()
            finally:
                conn.close()

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

    def rename_photo(self, old_path: str, new_path: str) -> bool:
        new_p = Path(new_path)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE photos SET path=?, filename=?, directory=? WHERE path=?",
                    (new_path, new_p.name, str(new_p.parent), old_path),
                )
                conn.commit()
                return conn.execute("SELECT changes()").fetchone()[0] > 0
            finally:
                conn.close()

    def get_known_mtimes(self, folder: str) -> dict[str, float]:
        """Returns {path: mtime} for all photos at or below folder (recursive).

        Used by the scanner to skip files whose mtime hasn't changed.
        """
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT path, file_mtime FROM photos "
                    "WHERE directory=? OR directory LIKE ?",
                    (folder, like_pattern),
                ).fetchall()
            finally:
                conn.close()
        return {r[0]: r[1] for r in rows}

    def get_all_paths_under(self, folder: str) -> set[str]:
        """Returns the set of all indexed paths at or below folder (recursive).

        Used by the scanner to detect entries whose files no longer exist.
        """
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT path FROM photos WHERE directory=? OR directory LIKE ?",
                    (folder, like_pattern),
                ).fetchall()
            finally:
                conn.close()
        return {r[0] for r in rows}

    def delete_photos(self, paths: list[str]) -> None:
        """Supprime en une seule transaction les entrées dont les fichiers ont disparu."""
        if not paths:
            return
        with self._lock:
            conn = self._conn()
            try:
                conn.executemany(
                    "DELETE FROM photos WHERE path=?", [(p,) for p in paths]
                )
                conn.commit()
            finally:
                conn.close()

    def get_photos_by_paths(self, paths: list[str]) -> list[PhotoInfo]:
        """Returns PhotoInfo objects for the given paths (in catalog order)."""
        if not paths:
            return []
        placeholders = ",".join("?" * len(paths))
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    f"SELECT * FROM photos WHERE path IN ({placeholders})"
                    " ORDER BY date_taken DESC, filename",
                    paths,
                ).fetchall()
            finally:
                conn.close()
        return [_photo_from_row(r) for r in rows]

    # ------------------------------------------------------------------ persons

    def get_persons(self) -> list[PersonInfo]:
        """Returns all persons ordered by name. photo_count is 0; call face_db.enrich_persons()."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, name FROM persons ORDER BY name"
                ).fetchall()
            finally:
                conn.close()
        return [PersonInfo(name=r[1], id=r[0]) for r in rows]

    def create_person(self, name: str) -> PersonInfo:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO persons (name) VALUES (?)", (name,)
                )
                conn.commit()
                person_id = cur.lastrowid
            finally:
                conn.close()
        return PersonInfo(name=name, id=person_id)

    def rename_person(self, person_id: int, name: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE persons SET name=? WHERE id=?", (name, person_id)
                )
                conn.commit()
            finally:
                conn.close()

    def delete_person(self, person_id: int) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
                conn.commit()
            finally:
                conn.close()
