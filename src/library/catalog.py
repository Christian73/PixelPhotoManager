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
    duplicate_group_id INTEGER
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
        media_type, duration, *rest
    ) = row
    duplicate_group_id = rest[0] if rest else None

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
        duplicate_group_id=duplicate_group_id,
    )


class Catalog:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # Connexion SQLite par (instance, thread), créée une fois puis
        # réutilisée (pattern ThumbnailCache) : chaque méthode ouvrait avant
        # une connexion neuve + 2 PRAGMAs, payés à chaque requête — sur les
        # chemins chauds (scan, requêtes de vues, badge), ce coût dépassait
        # souvent celui de la requête elle-même. threading.local est porté par
        # l'instance : deux Catalog sur le même chemin (tests) gardent chacun
        # leur connexion.
        self._tls = threading.local()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _guard(self):
        """Verrou + connexion thread-local + rollback garanti sur exception.

        Remplace le motif répété « with self._lock: conn = self._conn();
        try: … except BaseException: conn.rollback(); raise » (cf. CLAUDE.md,
        pattern de connexion) : la connexion mise en cache ne doit JAMAIS
        rester dans une transaction ouverte, sinon toutes les écritures
        suivantes échouent en « database is locked »."""
        with self._lock:
            conn = self._conn()
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise

    def _conn(self) -> sqlite3.Connection:
        """Connexion SQLite du thread courant, créée une seule fois par thread.

        Les méthodes d'écriture ne ferment plus la connexion : en cas
        d'exception, leur garde `except BaseException: conn.rollback()`
        remplace le rollback implicite qu'assurait l'ancienne fermeture —
        une connexion mise en cache ne doit jamais rester au milieu d'une
        transaction ouverte (les écritures suivantes échoueraient en
        « database is locked »)."""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-2048")
            self._tls.conn = conn
        return conn

    def close(self) -> None:
        """Ferme la connexion du thread courant (tests, arrêt de l'application)."""
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_dup_group ON photos(duplicate_group_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_favorite ON photos(is_favorite)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type)"
            )
            # Filet de sécurité au démarrage : dissout les groupes de 1 exemplaire
            # déjà présents en base (ex. créés avant l'ajout de la dissolution
            # systématique dans delete_photo/delete_photos).
            self._dissolve_singleton_duplicate_groups(conn)
            conn.commit()

    def _migrate_video_fields(self, conn) -> None:
        for stmt in (
            "ALTER TABLE photos ADD COLUMN media_type TEXT DEFAULT 'image'",
            "ALTER TABLE photos ADD COLUMN duration REAL DEFAULT 0.0",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass  # colonne déjà présente
        # Rétro-remplissage : vidéos ajoutées avant le support vidéo ont media_type='image'
        video_exts = (
            ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm",
            ".m4v", ".3gp", ".flv", ".ts", ".mts", ".mpg", ".mpeg",
        )
        like_clauses = " OR ".join(
            f"LOWER(filename) LIKE '%{ext}'" for ext in video_exts
        )
        # Vérification rapide avant l'UPDATE : si aucune photo 'image' ne correspond
        # à une extension vidéo, la retrofill est déjà faite — évite de payer le coût
        # d'un UPDATE complet (écriture) à chaque démarrage une fois la migration faite.
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
        with self._guard() as conn:
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
        if row:
            return _photo_from_row(row)
        return photo

    def count_photos_in_folder(self, folder: str) -> int:
        """Retourne le nombre de photos (et vidéos) indexées sous folder (récursivement)."""
        folder = os.path.normpath(folder)
        like_pattern = folder + os.sep + "%"
        with self._guard() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE directory=? OR directory LIKE ?",
                (folder, like_pattern),
            ).fetchone()
        return row[0] if row else 0

    def get_photos_in_folder(self, folder: str) -> list[PhotoInfo]:
        folder = os.path.normpath(folder)
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE directory=? ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')) DESC, filename",
                (folder,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_all_photo_paths(self) -> list[str]:
        """Retourne uniquement les chemins de toutes les photos (plus léger que get_all_photos)."""
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
        """Met à jour tous les chemins dont le début correspond à old_prefix."""
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
        """Met à jour le chemin d'un fichier photo dans le catalogue."""
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
        """Supprime la photo du catalogue (ne touche pas au fichier disque)."""
        with self._guard() as conn:
            # Doit précéder le DELETE sur photos : la sous-requête a besoin que la
            # ligne existe encore pour résoudre son id (pas de FK/cascade déclarée).
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
        """Supprime un album et son contenu (album_photos). Les photos elles-mêmes
        ne sont pas affectées : seule l'association à l'album est retirée."""
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
        """Ajoute plusieurs photos à un album en une seule transaction.
        Retourne le nombre de photos réellement ajoutées (déjà présentes ignorées)."""
        if not photo_ids:
            return 0
        with self._guard() as conn:
            # total_changes (et non SELECT changes()) : executemany ne
            # rapporte sinon que la dernière ligne.
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?,?)",
                [(album_id, pid) for pid in photo_ids],
            )
            added = conn.total_changes - before
            conn.commit()
        return added

    def remove_photo_from_album(self, album_id: int, photo_id: int) -> None:
        """Retire une photo d'un album (le fichier et la photo elle-même ne sont pas touchés)."""
        with self._guard() as conn:
            conn.execute(
                "DELETE FROM album_photos WHERE album_id=? AND photo_id=?",
                (album_id, photo_id),
            )
            conn.commit()

    def remove_photos_from_album(self, album_id: int, photo_ids: list[int]) -> None:
        """Retire plusieurs photos d'un album en un seul DELETE (fichiers et
        photos non touchés)."""
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
        """Supprime du catalogue les fichiers dans des répertoires *_assets (assets logiciels
        type Lightroom/Capture One). Retourne les chemins supprimés."""
        to_delete: list[str] = []
        with self._guard() as conn:
            rows = conn.execute("SELECT path FROM photos").fetchall()
            to_delete = [
                r[0] for r in rows
                if any(part.endswith("_assets") for part in Path(r[0]).parts)
            ]
            if to_delete:
                conn.executemany("DELETE FROM photos WHERE path=?",
                                 [(p,) for p in to_delete])
                conn.commit()
        if to_delete:
            logger.info("cleanup_asset_dirs : %d entrée(s) supprimée(s)", len(to_delete))
        return to_delete

    def delete_photos(self, paths: list[str]) -> None:
        """Supprime en une seule transaction les entrées dont les fichiers ont disparu."""
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
        """Dissout tout groupe de doublons retombé à 0 ou 1 exemplaire suite à une
        suppression. Invariant nécessaire quel que soit le chemin de suppression
        emprunté (suppression manuelle, nettoyage d'entrées fantômes par le
        scanner, purge de dossier...) — voir dedup_singleton_groups_any_delete_path
        en mémoire pour le contexte : avant ce correctif seule la suppression
        manuelle via l'UI dissolvait ces groupes, laissant réapparaître des
        groupes de 1 exemplaire après un scan qui retire des fichiers disparus."""
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

    # ------------------------------------------------------------------ doublons

    def set_duplicate_groups(self, assignments: dict) -> None:
        """Enregistre les groupes de doublons détectés. assignments = {path: group_id}.

        Dissout aussi tout groupe retombé à 0/1 exemplaire par cet appel : un
        DuplicateDetectorThread en cours au moment d'une suppression calcule ses
        assignations sur un état capturé avant celle-ci, et peut donc réécrire ici
        un membre survivant seul dans son ancien groupe — sans ce garde-fou, le
        groupe de 1 réapparaît jusqu'au prochain delete_photo(s)/redémarrage (cf.
        dedup_singleton_groups_any_delete_path en mémoire, dont ce chemin n'était
        pas couvert)."""
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
        """Efface tous les marqueurs de doublons."""
        with self._guard() as conn:
            conn.execute("UPDATE photos SET duplicate_group_id=NULL")
            conn.commit()

    def get_duplicates_for_group(self, group_id: int) -> list:
        """Retourne toutes les photos du groupe de doublons donné."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE duplicate_group_id=? "
                "ORDER BY COALESCE(date_taken, datetime(file_mtime, 'unixepoch')), filename",
                (group_id,),
            ).fetchall()
        return [_photo_from_row(r) for r in rows]

    def get_duplicate_groups(self) -> dict:
        """Retourne tous les groupes de doublons {group_id: [PhotoInfo, ...]}."""
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
        """{path: group_id} pour toutes les photos actuellement groupées —
        version légère de get_duplicate_groups() (pas de PhotoInfo complet),
        utilisée pour amorcer (seed) une passe de détection incrémentale."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path, duplicate_group_id FROM photos "
                "WHERE duplicate_group_id IS NOT NULL"
            ).fetchall()
        return {path: gid for path, gid in rows}

    def count_duplicate_groups(self) -> int:
        """Nombre de groupes de doublons distincts actuellement enregistrés."""
        with self._guard() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT duplicate_group_id) FROM photos "
                "WHERE duplicate_group_id IS NOT NULL"
            ).fetchone()
        return row[0] if row else 0

    def ignore_duplicate_group(self, group_id: int) -> None:
        """Dissout un groupe de doublons. Avec la détection incrémentale
        (DuplicateDetectorThread ne recompare jamais deux fichiers déjà tous
        les deux vérifiés lors d'une passe complète antérieure, cf.
        dedup_cache.compared_tier1/2), ce groupe ne sera plus recréé tant
        qu'aucun de ses membres ne change — un nouveau fichier correspondant
        à l'un d'eux reste en revanche détecté normalement."""
        with self._guard() as conn:
            conn.execute(
                "UPDATE photos SET duplicate_group_id=NULL WHERE duplicate_group_id=?",
                (group_id,),
            )
            conn.commit()

    def get_all_photo_paths_for_dedup(self) -> list:
        """Retourne la liste de tous les chemins de photos pour la détection de doublons."""
        with self._guard() as conn:
            rows = conn.execute(
                "SELECT path FROM photos ORDER BY path"
            ).fetchall()
        return [r[0] for r in rows]

    def get_photo_dates_for_dedup(self) -> dict:
        """Retourne {path: datetime|None} (date_taken EXIF, précision sous-seconde
        si disponible) pour tous les chemins catalogués — utilisé par la détection
        de doublons pour ne pas fusionner deux photos dont la date de prise de vue
        diffère (ex. rafale : même scène, instants de capture différents)."""
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
